"""Trainer for the AnoGAN model."""

import json
from pathlib import Path
from typing import Dict, Set, Tuple

import tensorflow as tf
import tensorflow.keras as k

from anomaly_toolbox.datasets.dataset import AnomalyDetectionDataset
from anomaly_toolbox.models.anogan import Discriminator, Generator
from anomaly_toolbox.trainers.trainer import Trainer


def residual_image(x: tf.Tensor, g_z: tf.Tensor) -> tf.Tensor:
    """Residual image. The absolute value of the difference
    beteen x and g_z.
    Args:
        x: The input image.
        g_z: The generated image.
    Returns:
        The residual image.
    """
    return tf.math.abs(x - g_z)


def residual_loss(x: tf.Tensor, g_z: tf.Tensor) -> tf.Tensor:
    """Residual loss. The mean of the residual image.
    Args:
        x: The input image.
        g_z: The generated image.
    Returns:
        a scalar, the computed mean.
    """
    return tf.reduce_mean(residual_image(x, g_z))


class AdversarialLoss(k.losses.Loss):
    """The Min-Max loss, used to train the discriminator."""

    def __init__(self):
        super().__init__()
        self._bce = k.losses.BinaryCrossentropy(from_logits=True)

    def call(self, y_true: tf.Tensor, y_pred: tf.Tensor) -> tf.Tensor:
        """Compute the 2 cross entropies and sum them.
        Args:
            y_true: MUST be D(x).
            y_pred: MUST be D(G(z)).
        Returns:
            bce(1, D(x)) + bce(0, D(G(z))
        """
        d_real = y_true
        d_gen = y_pred
        real_loss = self._bce(tf.ones_like(d_real), d_real)
        generated_loss = self._bce(tf.zeros_like(d_gen), d_gen)
        return real_loss + generated_loss


class AnoGAN(Trainer):
    """AnoGAN Trainer."""

    def __init__(
        self,
        dataset: AnomalyDetectionDataset,
        hps: Dict,
        summary_writer: tf.summary.SummaryWriter,
        log_dir: Path,
    ):
        """Initialize AnoGAN Trainer."""
        super().__init__(
            dataset=dataset, hps=hps, summary_writer=summary_writer, log_dir=log_dir
        )

        # Models
        self.discriminator = Discriminator(n_channels=dataset.channels)
        self.generator = Generator(
            n_channels=dataset.channels, input_dimension=hps["latent_vector_size"]
        )
        self._validate_models((28, 28, dataset.channels), hps["latent_vector_size"])

        # Optimizers
        self.optimizer_g = k.optimizers.Adam(
            learning_rate=hps["learning_rate"], beta_1=0.5, beta_2=0.999
        )
        self.optimizer_d = k.optimizers.Adam(
            learning_rate=hps["learning_rate"], beta_1=0.5, beta_2=0.999
        )
        self.optimizer_z = k.optimizers.Adam(
            learning_rate=hps["learning_rate"], beta_1=0.5, beta_2=0.999
        )

        # Losses
        self._minmax = AdversarialLoss()
        self._bce = k.losses.BinaryCrossentropy(from_logits=True)

        # Metrics
        self.epoch_d_loss_avg = k.metrics.Mean(name="epoch_discriminator_loss")
        self.epoch_g_loss_avg = k.metrics.Mean(name="epoch_generator_loss")

        self._auc = k.metrics.AUC(num_thresholds=500)

        self.keras_metrics = {
            metric.name: metric
            for metric in [self.epoch_d_loss_avg, self.epoch_g_loss_avg, self._auc]
        }

        # Variables and constants
        self._z_gamma = tf.Variable(tf.zeros((hps["latent_vector_size"],)))
        self._lambda = tf.constant(0.1)

    @staticmethod
    def hyperparameters() -> Set[str]:
        """List of the hyperparameters name used by the trainer."""
        return {"learning_rate", "latent_vector_size"}

    def _validate_models(
        self, input_dimension: Tuple[int, int, int], latent_vector_size: int
    ):
        fake_latent_vector = (1, latent_vector_size)
        self.generator(tf.zeros(fake_latent_vector), training=False)
        self.generator.summary()

        fake_batch_size = (1, *input_dimension)
        self.discriminator(tf.zeros(fake_batch_size), training=False)
        self.discriminator.summary()

    def _select_and_save(self, current_auc: tf.Tensor) -> None:
        """Saves the models (generator and discriminator) and the
        AUC thresholds and value.
        Args:
            current_auc: The current value for the AUC.
        """
        base_path = self._log_dir / "results" / "best"
        self.discriminator.save(
            str(base_path / "discriminator"),
            overwrite=True,
            include_optimizer=False,
        )
        self.generator.save(
            str(base_path / "generator"),
            overwrite=True,
            include_optimizer=False,
        )

        with open(base_path / "auc.json", "w") as fp:
            json.dump(
                {
                    "value": float(current_auc),
                    "thresholds": self._auc.thresholds,
                },
                fp,
            )

    @tf.function
    def train(
        self,
        epochs: tf.Tensor,
        step_log_frequency: tf.Tensor,
    ) -> None:
        """Train the model for the desider number of epochs.
        Calls the `train_step` function in loop.
        Also performs model selection on AUC using a subset of the test set.

        Args:
            epochs: The number of training epochs.
            step_log_frequency: Number of steps to use for loging on CLI and
                                tensorboard.
        """
        best_auc = -1.0
        for epoch in tf.range(epochs):
            for batch in self._dataset.train_normal:
                # Perform the train step
                x, _ = batch
                x_hat, d_loss, g_loss = self.train_step(x)

                # Update the losses metrics
                self.epoch_d_loss_avg.update_state(d_loss)
                self.epoch_g_loss_avg.update_state(g_loss)
                step = self.optimizer_d.iterations

                if tf.math.equal(tf.math.mod(step, step_log_frequency), 0):
                    with self._summary_writer.as_default():
                        tf.summary.scalar(
                            "learning_rate", self.optimizer_g.learning_rate, step=step
                        )
                        tf.summary.scalar(
                            "g_loss", self.epoch_g_loss_avg.result(), step=step
                        )
                        tf.summary.scalar(
                            "d_loss", self.epoch_d_loss_avg.result(), step=step
                        )

                        tf.summary.image("generated", x_hat, step=step)

                    tf.print(
                        "Step ",
                        step,
                        ": d_loss: ",
                        self.epoch_d_loss_avg.result(),
                        ", g_loss: ",
                        self.epoch_g_loss_avg.result(),
                        ", lr: ",
                        self.optimizer_g.learning_rate,
                    )
            tf.print("Epoch ", epoch, " completed.")

            # Reset the metrics at the end of every epoch
            self._reset_keras_metrics()

            # Model selection every model_selection epochs because the test phase is
            # terribly slow.
            model_selection = tf.constant(10)
            if tf.not_equal(tf.math.mod(epoch, model_selection), 0):
                continue

            # Model selection using a subset of the test set
            # Keep "batches" number of batch of positives, them same for the negatives
            # then unbatch them, and process every element indipendently.
            batches = 1
            validation_set = self._dataset.test_normal.take(batches).concatenate(
                self._dataset.test_anomalous.take(batches)
            )
            # We need to search for z, hence we do this 1 element at a time (slow!)
            validation_set = validation_set.unbatch().batch(1)

            step = self.optimizer_d.iterations
            for idx, sample in enumerate(validation_set):
                x, y = sample
                # self._z_gamma should be the z value that's likely
                # to produce x (from what the generator knows)
                anomaly_score = self.latent_search(x)
                self._auc.update_state(
                    y_true=y, y_pred=tf.expand_dims(anomaly_score, axis=0)
                )
                with self._summary_writer.as_default():
                    g_z = self.generator(tf.expand_dims(self._z_gamma, axis=0))
                    tf.summary.image(
                        "test/inoutres",
                        tf.concat(
                            [x, g_z, residual_image(x, g_z)],
                            axis=2,
                        ),
                        step=step + idx,
                    )
            current_auc = self._auc.result()
            with self._summary_writer.as_default():
                tf.summary.scalar("auc", current_auc, step=step)
                tf.print("Validation AUC: ", current_auc)

            if best_auc < current_auc:
                tf.py_function(self._select_and_save, [current_auc], [])
                best_auc = current_auc

    @tf.function
    def train_step(
        self,
        x: tf.Tensor,
    ) -> Tuple[tf.Tensor, tf.Tensor, tf.Tensor]:
        """Single training step.
        Args:
            x: A batch of images.
        Returns:
            x_hat: A batch of generated images.
            d_loss: The discriminator loss.
            g_loss: The generator loss.
        """
        noise = tf.random.normal((tf.shape(x)[0], self._hps["latent_vector_size"]))
        with tf.GradientTape(persistent=True) as tape:
            x_hat = self.generator(noise, training=True)

            d_x, _ = self.discriminator(x, training=True)
            d_x_hat, _ = self.discriminator(x_hat, training=True)

            # Losses
            d_loss = self._minmax(d_x, d_x_hat)
            g_loss = self._bce(tf.ones_like(d_x_hat), d_x_hat)

        d_grads = tape.gradient(d_loss, self.discriminator.trainable_variables)
        g_grads = tape.gradient(g_loss, self.generator.trainable_variables)
        del tape

        self.optimizer_d.apply_gradients(
            zip(d_grads, self.discriminator.trainable_variables)
        )
        self.optimizer_g.apply_gradients(
            zip(g_grads, self.generator.trainable_variables)
        )

        return x_hat, d_loss, g_loss

    def latent_search(
        self, x: tf.Tensor, gamma: tf.Tensor = tf.constant(500)
    ) -> tf.Tensor:
        """The test step searches in the latent space
        the z value that's likely to be mapped with the input image x.
        This step returns the value of the latent vector.
        NOTE: this is slow, since it performs gamma optimization steps
        to find the value of z.

        Args:
            x: Test image.
            gamma: Number of optimization steps.
        Returns:
            anomaly_score at the end of the gamma steps.
        """
        tf.print("Searching z with ", gamma, " opt steps...")

        @tf.function
        def opt_step():
            """Optimization steps that optimizes the value of self._z_gamma."""

            with tf.GradientTape(watch_accessed_variables=False) as tape:
                tape.watch(self._z_gamma)
                x_hat = self.generator(tf.expand_dims(self._z_gamma, axis=0))
                residual_score = residual_loss(x, x_hat)

                d_x, _ = self.discriminator(x, training=False)
                d_x_hat, _ = self.discriminator(x_hat, training=False)
                discrimination_score = self._minmax(d_x, d_x_hat)

                anomaly_score = (
                    1.0 - self._lambda
                ) * residual_score + self._lambda * discrimination_score

            # we want to minimize the anomamly score
            grads = tape.gradient(anomaly_score, [self._z_gamma])
            self.optimizer_z.apply_gradients(zip(grads, [self._z_gamma]))
            return anomaly_score

        self._z_gamma.assign(tf.zeros_like(self._z_gamma))
        for _ in tf.range(gamma):
            anomaly_score = opt_step()
        return anomaly_score
