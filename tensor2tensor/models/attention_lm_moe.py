# Copyright 2017 Google Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Self-attention based language model.

Like transformer.py, but no encoder

decoder: [Self-Attention, Feed-forward] x n

"""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

# Dependency imports

from six.moves import xrange  # pylint: disable=redefined-builtin

from tensor2tensor.models import common_attention
from tensor2tensor.models import common_hparams
from tensor2tensor.models import common_layers
from tensor2tensor.utils import registry
from tensor2tensor.utils import t2t_model

import tensorflow as tf


@registry.register_model
class AttentionLmMoe(t2t_model.T2TModel):
  """Attention net.  See file docstring."""

  def model_fn_body_sharded(self, sharded_features):
    # Remove dropout if not training
    hparams = self._hparams
    dp = self._data_parallelism
    targets = sharded_features["targets"]
    targets = dp(tf.squeeze, targets, 2)

    (decoder_input, decoder_self_attention_bias) = dp(
        attention_lm_moe_prepare_decoder, targets, hparams)

    def residual_fn(x, y):
      return common_layers.layer_norm(x + tf.nn.dropout(
          y, 1.0 - hparams.residual_dropout))

    x = dp(tf.nn.dropout, decoder_input, 1.0 - hparams.residual_dropout)
    extra_loss = 0.0
    for layer in xrange(hparams.num_hidden_layers):
      with tf.variable_scope("layer_%d" % layer):
        with tf.variable_scope("attention"):
          y = dp(common_attention.multihead_attention,
                 x,
                 None,
                 decoder_self_attention_bias,
                 hparams.attention_key_channels or hparams.hidden_size,
                 hparams.attention_value_channels or hparams.hidden_size,
                 hparams.hidden_size,
                 hparams.num_heads,
                 hparams.attention_dropout,
                 summaries=True,
                 name="decoder_self_attention")
          x = dp(residual_fn, x, y)
        with tf.variable_scope("ffn"):
          if str(layer) in hparams.moe_layers.split(","):
            y, loss = common_layers.moe_layer(
                dp, self._ps_devices, x,
                hparams.mode == tf.contrib.learn.ModeKeys.TRAIN,
                hparams.hidden_size,
                hparams.moe_hidden_size, hparams.moe_n1, hparams.moe_n2,
                hparams.moe_loss_coef)
            extra_loss += loss
          else:
            y = dp(common_layers.conv_hidden_relu,
                   x,
                   hparams.filter_size,
                   hparams.hidden_size,
                   dropout=hparams.relu_dropout)
          x = dp(residual_fn, x, y)
    decoder_output = dp(tf.expand_dims, x, 2)
    return decoder_output, extra_loss


def attention_lm_moe_prepare_decoder(targets, hparams):
  """Prepare one shard of the model for the decoder.

  Args:
    targets: a Tensor.
    hparams: run hyperparameters

  Returns:
    decoder_input: a Tensor, bottom of decoder stack
    decoder_self_attention_bias: a Tensor, containing large negative values
    to implement masked attention and possibly baises for diagonal alignments
  """
  decoder_self_attention_bias = (
      common_attention.attention_bias_lower_triangle(tf.shape(targets)[1]))
  decoder_input = common_layers.shift_left_3d(targets)
  if hparams.pos == "timing":
    decoder_input = common_attention.add_timing_signal_1d(decoder_input)
  return (decoder_input, decoder_self_attention_bias)


@registry.register_hparams
def attention_lm_moe_base():
  """Set of hyperparameters.

  suitable for 1 gpu.
  on lm1b_16k:
     ~337M params
     1.1 steps/sec on  [GeForce GTX TITAN X]

  Returns:
    a hparams object
  """
  hparams = common_hparams.basic_params1()
  hparams.hidden_size = 1024
  hparams.batch_size = 8192
  hparams.max_length = 256
  hparams.dropout = 0.0
  hparams.clip_grad_norm = 0.  # i.e. no gradient clipping
  hparams.optimizer_adam_epsilon = 1e-9
  hparams.learning_rate_decay_scheme = "noam"
  hparams.learning_rate = 0.1
  hparams.learning_rate_warmup_steps = 1000
  hparams.initializer_gain = 1.0
  hparams.num_hidden_layers = 4
  hparams.initializer = "uniform_unit_scaling"
  hparams.weight_decay = 0.0
  hparams.optimizer_adam_beta1 = 0.9
  hparams.optimizer_adam_beta2 = 0.98
  hparams.num_sampled_classes = 0
  hparams.label_smoothing = 0.0
  hparams.shared_embedding_and_softmax_weights = int(False)
  hparams.add_hparam("filter_size", 2948)  # Add new ones like this.
  # comma-separated list of layer numbers.
  # At each of these layers, we replace the ffn with a mixture of experts.
  hparams.add_hparam("moe_layers", "2")
  # If moe_n2 is None, then use a flat MoE with moe_n1 experts.
  # If moe_n2 is an integer, then use a hierarchical MoE
  #   consisting of moe_n1 groups of moe_n2 experts each.
  hparams.add_hparam("moe_n1", 64)
  hparams.add_hparam("moe_n2", 0)
  hparams.add_hparam("moe_hidden_size", 2048)
  hparams.add_hparam("moe_loss_coef", 1e-2)
  # attention-related flags
  hparams.add_hparam("num_heads", 8)
  hparams.add_hparam("attention_key_channels", 0)
  hparams.add_hparam("attention_value_channels", 0)
  # All hyperparameters ending in "dropout" are automatically set to 0.0
  # when not in training mode.
  hparams.add_hparam("attention_dropout", 0.0)
  hparams.add_hparam("relu_dropout", 0.0)
  hparams.add_hparam("residual_dropout", 0.1)
  hparams.add_hparam("pos", "timing")  # timing, none
  return hparams


@registry.register_hparams
def attention_lm_moe_small():
  """Cheap model for single-gpu training.

  on lm1b_16k:
     ~295M params
     2 steps/sec on  [GeForce GTX TITAN X]

  Returns:
    an hparams object.
  """
  hparams = attention_lm_moe_base()
  hparams.num_hidden_layers = 4
  hparams.hidden_size = 512
  hparams.filter_size = 2048
  hparams.moe_n1 = 128
  hparams.moe_layers = "2"
  hparams.moe_hidden_size = 2048
  return hparams


@registry.register_hparams
def attention_lm_moe_large():
  """Large model for distributed training.

  Over 1B parameters, so requires multi-gpu training due to memory
   requirements.

  Returns:
    an hparams object.
  """
  hparams = attention_lm_moe_base()
  hparams.num_hidden_layers = 5
  hparams.moe_layers = "3"
  hparams.hidden_size = 1024
  hparams.num_heads = 16
  hparams.filter_size = 4096
  hparams.moe_hidden_size = 4096
  hparams.moe_n1 = 128
  hparams.residual_dropout = 0.2
  return hparams
