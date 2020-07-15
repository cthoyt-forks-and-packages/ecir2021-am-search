import logging
from typing import Optional, Tuple

import numpy as np
import random
from torch import nn
from torch.utils.data import DataLoader
from torch.nn import CrossEntropyLoss, BCELoss, BCEWithLogitsLoss
import torch.nn.functional as F
import torch

from arclus.evaluation import accuracy


def set_random_seed(seed: int):
    """Set the random seed on numpy, torch, and python."""
    return (
        np.random.seed(seed=seed),
        torch.manual_seed(seed=seed),
        random.seed(seed),
    )


class TrainTestHandler:
    """
    A training and test handler.
    Handles batch-wise training on a train set, early stopping on a validation set
    and provides function for prediction
    """

    def __init__(
        self,
        base_model: nn.Module,
        optimizer,
        criterion=CrossEntropyLoss(),
        epochs: int = 2000,
        device: Optional[torch.device] = None,
        random_seed: Optional[int] = 0,
        patience: int = 3,
        min_delta: float = 1.0e-05,
    ) -> None:
        """Initialize the train handler."""
        set_random_seed(random_seed)
        # max number of epochs
        self.epochs = epochs

        # Initialize the device
        self.device = device
        # early stopping params
        self.patience = patience
        self.min_delta = min_delta

        self.history = None
        self.reset_history()

        # Move model to device
        self.model = base_model.to(device=self.device)
        self.optimizer = optimizer
        self.loss = criterion

    def reset_history(self):
        self.history = []

    def train(
        self,
        train_loader: DataLoader,
        val_loader: DataLoader
    ):
        """
        Fit the model batch wise on train set and perform early stopping on validation set.
        :param train_loader: train set
        :param val_loader: validation set
        :return: None
        """
        self.reset_history()
        for epoch in range(self.epochs):
            self.model.train()

            for iteration, batch in enumerate(train_loader, 0):
                # torch *accumulates* the gradient; hence we need to zero it before computing new gradients
                self.optimizer.zero_grad()

                train_batch_x = batch[0].to(self.device)
                train_batch_y = batch[1].to(self.device)
                # predict probability for each class
                batch_pred_y = self.model.forward(train_batch_x).to(self.device)
                # get loss
                loss_value = self.loss(batch_pred_y, train_batch_y)

                # compute gradients
                loss_value.backward()

                # update parameters
                self.optimizer.step()

            # Validation phase
            result = self.evaluate(val_loader)
            logging.info(f"Epoch [{epoch}], val_loss: {result['val_loss']:.4f}, val_acc: {result['val_acc']:.4f}")
            self.history.append(result["val_loss"])

            # Early stopping on validation set
            if len(self.history) > self.patience:
                if result["val_loss"] >= (1 - self.min_delta) * max(self.history[-self.patience:]):
                    logging.info('Early stopping')
                    break

    def predict(
        self,
        x: torch.tensor
    ) -> torch.tensor:
        """
        Predict probability for each label.
        :param x: the data
        :return: tensor containing probability for class 0 and 1 for each entry
        """
        self.model.eval()
        with torch.no_grad():
            y_pred_prob = self.model.forward(x.to(self.device)).cpu()
        return y_pred_prob

    def evaluate(
        self,
        val_loader: DataLoader
    ) -> dict:
        """
        Evaluation on validation set.
        :param val_loader:
        :return: dict: Return loss and accuracy for this epoch
        """
        self.model.eval()
        outputs = [self.validation_step(batch[0].to(self.device), batch[1].to(self.device)) for batch in val_loader]
        return self.validation_epoch_end(outputs)

    def validation_step(
        self,
        batch_x: torch.tensor,
        batch_y: torch.tensor
    ) -> dict:
        """
        Calculate loss and accuracy for a given batch.
        :param batch_x: input values
        :param batch_y: output values
        :return: dict: contains validation loss and accuracy
        """
        pred_y = self.model.forward(batch_x).to(self.device)  # Generate predictions
        loss_value = self.loss(pred_y, batch_y)
        acc = accuracy(pred_y, batch_y)  # Calculate accuracy
        return {'val_loss': loss_value.detach(), 'val_acc': acc}

    @staticmethod
    def validation_epoch_end(
        outputs
    ) -> dict:
        """
        Calculate mean loss and accuracy for all batches.
        :param outputs:
        :return: dict: contains validation loss and accuracy
        """
        batch_losses = [x['val_loss'] for x in outputs]
        avg_epoch_loss = torch.stack(batch_losses).mean()  # Avg over val losses
        batch_accs = [x['val_acc'] for x in outputs]
        avg_epoch_acc = torch.stack(batch_accs).mean()  # Avg over val accuracies
        return {'val_loss': avg_epoch_loss.item(), 'val_acc': avg_epoch_acc.item()}


class NEndModel(nn.Module):
    """
    Simple MLP Model (Not-End-to-End-Model) with a single layer.
    """

    def __init__(
        self,
        input_shape: int,
        out: int,
        dropout_rate: float
    ):
        super().__init__()
        self.model_name = "simple_forward"
        self.l1 = nn.Linear(in_features=input_shape, out_features=out, bias=True)
        self.dropout = nn.Dropout(dropout_rate)
        self.sig = nn.Sigmoid()

    def forward(self, x):
        x = self.l1(x)
        x = self.dropout(x)
        x = self.sig(x)
        return x

    def get_model_name(self) -> str:
        return self.model_name
