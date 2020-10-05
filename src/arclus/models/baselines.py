import inspect
import logging
import pathlib
from abc import ABC, abstractmethod
from logging import Logger
from typing import Any, Mapping, Sequence, Tuple

import torch
from sklearn.cluster import KMeans

from arclus.settings import CLAIMS_TEST_FEATURES, PREMISES_TEST_FEATURES, PREP_ASSIGNMENTS_TEST, PREP_TEST_SIMILARITIES, PREP_TEST_SIMILARITIES_SOFTMAX
from arclus.similarity import Similarity
from arclus.utils import get_subclass_by_name
from arclus.utils_am import inference_no_args, load_bert_model_and_data_no_args

logger: Logger = logging.getLogger(__name__)


class RankingMethod:
    """Base class for ranking methods."""

    @abstractmethod
    def rank(
        self,
        claim_id: int,
        premise_ids: Sequence[str],
        k: int,
    ) -> Sequence[str]:
        """
        Return a sorted list of k premise IDs for each query claim.

        :param claim_id:
            The query claim ID.
        :param premise_ids:
            The candidate premise IDs.
        :param k: >0
            The number of premises to return for each claim.

        :return:
            The sorted list of k premise IDs for each query claim.
        """
        raise NotImplementedError


class ZeroShotRanking(RankingMethod, ABC):
    """Abstract base class for zero-shot methods."""

    # Pre-computed representations
    claims: Mapping[int, torch.FloatTensor]
    premises: Mapping[str, torch.FloatTensor]

    def __init__(
        self,
        similarity: Similarity,
        claims_path: pathlib.Path = CLAIMS_TEST_FEATURES,
        premises_path: pathlib.Path = PREMISES_TEST_FEATURES,
    ):
        """
        Initialize the method.

        :param similarity:
            The similarity to use for the representations.
        :param claims_path:
            The path to the pre-computed claims representations.
        :param premises_path:
            The path to the pre-computed premises representations.
        """
        self.similarity = similarity

        # Load pre-computed representations
        self.claims = torch.load(claims_path)
        self.premises = torch.load(premises_path)


class ZeroShotKNN(ZeroShotRanking):
    """Rank according to similarity of pre-trained BERT representations."""

    def rank(self, claim_id: int, premise_ids: Sequence[str], k: int) -> Sequence[str]:  # noqa: D102
        # get the claim representation
        claim_repr = self.claims[claim_id].unsqueeze(dim=0)
        # get premise representations
        premise_repr = torch.stack([self.premises[premise_id] for premise_id in premise_ids], dim=0)
        # find most similar
        top_ids = self.similarity.sim(
            claims=claim_repr,
            premises=premise_repr,
        ).topk(k=k, largest=True, sorted=True).indices.squeeze(dim=0)
        # re-translate to original IDs
        return [premise_ids[i] for i in top_ids.tolist()]


def _num_clusters(ratio: float, num_premises: int, k: int) -> int:
    n_clusters = int(round(ratio * num_premises))
    n_clusters = max(n_clusters, k)
    n_clusters = min(n_clusters, num_premises)
    return n_clusters


class ZeroShotClusterKNN(ZeroShotRanking):
    """Rank according to similarity of pre-trained BERT representations, return at most one premise for each cluster."""

    def __init__(
        self,
        similarity: Similarity,
        cluster_ratio: float = 0.5,
        claims_path: pathlib.Path = CLAIMS_TEST_FEATURES,
        premises_path: pathlib.Path = PREMISES_TEST_FEATURES,
        cluster_representative: str = 'closest-to-center',
    ):
        """
        Initialize the method.

        :param similarity:
            The similarity to use for the representations.
        :param cluster_ratio: >0
            The relative number of clusters to use.
        :param claims_path:
            The path to the pre-computed claims representations.
        :param premises_path:
            The path to the pre-computed premises representations.
        :param cluster_representative:
            The method to choose a cluster representative. From {'closest-to-center', 'closest-to-claim'}.
        """
        super().__init__(similarity=similarity, claims_path=claims_path, premises_path=premises_path)
        self.ratio = cluster_ratio
        self.cluster_representative = cluster_representative

    def _get_cluster_representatives(
        self,
        claim_repr: torch.FloatTensor,
        premise_repr: torch.FloatTensor,
        assignment: torch.LongTensor,
        centroids: torch.FloatTensor,
    ) -> torch.LongTensor:
        n_clusters = centroids.shape[0]
        local_premise_ids = torch.arange(premise_repr.shape[0])
        repr_ids = torch.full(size=(n_clusters,), fill_value=-1, dtype=torch.long)
        for i in range(n_clusters):
            if self.cluster_representative == 'closest-to-center':
                anchor = centroids[i].unsqueeze(dim=0)
            elif self.cluster_representative == 'closest-to-claim':
                anchor = claim_repr
            else:
                raise NotImplementedError(self.cluster_representative)
            mask = assignment == i
            if not mask.any():
                continue
            premises_in_cluster = premise_repr[mask]
            idx = self.similarity.sim(
                claims=anchor,
                premises=premises_in_cluster,
            ).argmax(dim=1)[0]
            repr_ids[i] = local_premise_ids[mask][idx]
        return repr_ids

    def rank(self, claim_id: int, premise_ids: Sequence[str], k: int) -> Sequence[str]:  # noqa: D102
        # get the claim representation
        claim_repr = self.claims[claim_id].unsqueeze(dim=0)

        # get premise representations
        premise_repr = torch.stack([self.premises[premise_id] for premise_id in premise_ids], dim=0)
        # cluster premises
        algorithm = KMeans(n_clusters=_num_clusters(ratio=self.ratio, num_premises=len(premise_ids), k=k))
        cluster_assignment = torch.as_tensor(algorithm.fit_predict(premise_repr.numpy()))
        cluster_centers = torch.as_tensor(algorithm.cluster_centers_)

        # choose representatives
        cluster_repr_id = self._get_cluster_representatives(
            claim_repr=claim_repr,
            premise_repr=premise_repr,
            assignment=cluster_assignment,
            centroids=cluster_centers,
        )

        # find most similar clusters
        non_empty_clusters = cluster_repr_id >= 0
        top_cluster_id = self.similarity.sim(
            claims=claim_repr,
            premises=premise_repr[cluster_repr_id[non_empty_clusters]],
        ).topk(k=k, largest=True, sorted=True).indices.squeeze(dim=0)

        # re-translate to local (batch) premise ID
        top_ids = [cluster_repr_id[non_empty_clusters][i] for i in top_cluster_id.tolist()]

        # re-translate to original IDs
        return [premise_ids[i] for i in top_ids]


class LearnedSimilarityKNN(RankingMethod):
    """Rank premises according to precomputed fine-tuned BERT similarities for concatenation of premise and claim."""

    #: The precomputed similarities.
    precomputed_similarities: Mapping[Tuple[int, str], float]

    def __init__(
        self,
        softmax: bool = True,
        model_path: str = '/nfs/data3/fromm/argument_clustering/models/d3d4a9c7c23a4b85a20836a754e3aa56',
        cache_root: str = '/tmp/arclus/bert',
    ):
        """
        Initialize the method.

        :param softmax:
            Whether to apply softmax on the scores for the pairwise similarity model.
        :param model_path:
            Directory where the fine-tuned bert similarity model checkpoint is located.
        :param cache_root:
            The directory where temporary BERT inference files are stored.
        """
        buffer_path = PREP_TEST_SIMILARITIES_SOFTMAX if softmax else PREP_TEST_SIMILARITIES
        if not buffer_path.is_file():
            logger.info('computing similarities')
            # load bert model and the data
            batch_size = 128
            logger.info('Load data')
            loader, data, model, guids = load_bert_model_and_data_no_args(
                model_path=model_path,
                task_name="SIM",
                batch_size=batch_size,
                data_dir=PREP_ASSIGNMENTS_TEST,
                overwrite_cache=True,
                max_seq_length=512,
                model_type="bert",
                cache_root=cache_root,
            )

            # generate logits for all claims-premise pairs
            # predictions = inference(args, data, loader, logger, model)
            logger.info('Run inference')
            predictions = inference_no_args(
                data=data,
                loader=loader,
                logger=logger,
                model=model,
                batch_size=batch_size,
                softmax=softmax,
            )

            # TODO: How to map guids to claim_id, premise_id?
            # Are premise_ids unique?=
            # d = dict(zip(guids, predictions))
            precomputed_similarities = dict(zip(guids, predictions))
            torch.save(precomputed_similarities, buffer_path)

        self.precomputed_similarities = torch.load(buffer_path)

    def rank(self, claim_id: int, premise_ids: Sequence[str], k: int) -> Sequence[str]:  # noqa: D102
        # def lookup_similarity(premise_id: str) -> float:
        #     return self.precomputed_similarities[claim_id, premise_id]

        # TODO: Why don't we need the claim_id?
        def lookup_similarity(premise_id: str) -> float:
            return self.precomputed_similarities[premise_id]

        return sorted(premise_ids, key=lookup_similarity, reverse=True)[:k]


class LearnedSimilarityClusterKNN(LearnedSimilarityKNN):
    """Rank premises according to precomputed fine-tuned BERT similarities for concatenation of premise and claim, only returning one premise for each cluster."""

    def __init__(
        self,
        cluster_ratio: float = 0.5,
        premises_path: pathlib.Path = PREMISES_TEST_FEATURES,
        softmax: bool = True,
        model_path: str = '/nfs/data3/fromm/argument_clustering/models/d3d4a9c7c23a4b85a20836a754e3aa56',
        cache_root: str = '/tmp/arclus/bert',
    ):
        """
        Initialize the method.

        :param cluster_ratio:
            The reduction ratio. n_clusters is chosen according to

             .. math ::
                n_clusters = min(max(int(round(ratio * num_premises)), k), num_premises)

        :param premises_path:
            The path where the precomputed premise representations are stored.
        :param softmax:
            Whether to apply softmax on the scores for the pairwise similarity model.
        :param model_path:
            Directory where the fine-tuned bert similarity model checkpoint is located.
        :param cache_root:
            The directory where temporary BERT inference files are stored.
        """
        super().__init__(model_path=model_path, cache_root=cache_root, softmax=softmax)
        self.ratio = cluster_ratio
        self.premises = torch.load(premises_path)

    def rank(self, claim_id: int, premise_ids: Sequence[str], k: int) -> Sequence[str]:  # noqa: D102
        # get premise representations
        num_premises = len(premise_ids)
        premise_repr = torch.stack([self.premises[premise_id] for premise_id in premise_ids], dim=0)

        # cluster premises
        algorithm = KMeans(n_clusters=_num_clusters(ratio=self.ratio, num_premises=num_premises, k=k))
        cluster_assignment = algorithm.fit_predict(premise_repr.numpy())

        # TODO: Why don't we need the claim_id?
        def lookup_similarity(i: int) -> float:
            return self.precomputed_similarities[premise_ids[i]]

        seen_clusters = set()
        result = []
        for i in sorted(range(num_premises), key=lookup_similarity, reverse=True):
            cluster_id = int(cluster_assignment[i])
            if cluster_id not in seen_clusters:
                premise_id = premise_ids[i]
                result.append(premise_id)
            seen_clusters.add(cluster_id)
        return result[:k]


def name_normalizer(name: str) -> str:
    return name.lower().replace('_', '')


def get_baseline_method_by_name(
    name: str,
    **kwargs: Any,
) -> RankingMethod:
    cls = get_subclass_by_name(base_class=RankingMethod, name=name, normalizer=name_normalizer)
    real_kwargs = dict()
    for key, value in kwargs.items():
        signature = inspect.signature(cls.__init__)
        if key in signature.parameters:
            real_kwargs[key] = value
        else:
            print(f"Unused argument {key}={value}")
    return cls(**real_kwargs)
