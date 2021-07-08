"""Trainer for the AnoGAN model."""

import json
from pathlib import Path
from typing import Dict, Set, Tuple

import tensorflow as tf
import tensorflow.keras as k

from anomaly_toolbox.datasets.dataset import AnomalyDetectionDataset
from anomaly_toolbox.losses import (adversarial_loss, feature_matching_loss,
                                    residual_image, residual_loss)
from anomaly_toolbox.models.anogan import Discriminator, Generator
from anomaly_toolbox.trainers.trainer import Trainer


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
        # IMPORTANT! Call .numpy() otherwhise when using the depth as a tf.Tensor
        # in creating the Keras model, we endup with Serialization issues.
        depth = tf.shape(next(iter(dataset.train.take(1)))[0])[-1].numpy()
        self.discriminator = Discriminator(n_channels=depth)
        self.generator = Generator(
            n_channels=depth, input_dimension=hps["latent_vector_size"]
        )
        self._validate_models((28, 28, depth), hps["latent_vector_size"])

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

        fake_batch_size = (1,) + input_dimension
        self.discriminator(tf.zeros(fake_batch_size), training=False)
        self.discriminator.summary()

    def _select_and_save(self, current_auc):
        base_path = self._log_dir / "results" / "best"
        self.discriminator.save(
            str(base_path / "discriminator"),
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
    ):
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

            # Model selection every 10 epochs because the test phase is
            # terribly slow.
            if tf.not_equal(tf.math.mod(epoch, 10), 0):
                continue

            # Model selection using a subset of the test set
            validation_set = self._dataset.test_normal.take(1).concatenate(
                self._dataset.test_anomalous.take(1)
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
                    y_true=y, y_pred=tf.expand_dims(anomaly_score, axis=[0])
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
        x,
    ):
        """Single training step."""
        noise = tf.random.normal((tf.shape(x)[0], self._hps["latent_vector_size"]))
        with tf.GradientTape(persistent=True) as tape:
            x_hat = self.generator(noise, training=True)

            d_x, x_features = self.discriminator(x, training=True)
            d_x_hat, x_hat_features = self.discriminator(x_hat, training=True)

            # Losses
            d_loss = adversarial_loss(d_x, d_x_hat)
            g_loss = feature_matching_loss(x_hat_features, x_features)

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

    def latent_search(self, x, gamma=tf.constant(500)):
        """The test step searches in the latent space
        the z value that's likely to be mapped with the input image x.
        This step returns the value of the latent vector.
        NOTE: this is slow, since it performs gamma optimization steps
        to find the value of z.
        """
        tf.print("Searching z with ", gamma, " opt steps...")

        @tf.function
        def opt_step():
            with tf.GradientTape(watch_accessed_variables=False) as tape:
                tape.watch(self._z_gamma)
                x_hat = self.generator(tf.expand_dims(self._z_gamma, axis=[0]))
                residual_score = residual_loss(x, x_hat)
                _, x_features = self.discriminator(x, training=False)
                _, x_hat_features = self.discriminator(x_hat, training=False)
                discrimination_score = feature_matching_loss(x_hat_features, x_features)

                anomaly_score = (
                    1 - self._lambda
                ) * residual_score + self._lambda * discrimination_score

            # we want to minimize the anomamly score
            grads = tape.gradient(anomaly_score, [self._z_gamma])
            self.optimizer_z.apply_gradients(zip(grads, [self._z_gamma]))
            return anomaly_score

        for _ in tf.range(gamma):
            anomaly_score = opt_step()
        return anomaly_score
