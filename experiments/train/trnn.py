from __future__ import print_function

import tensorflow as tf
from tensorflow.python.ops import variable_scope as vs
from tensorflow.python.ops import array_ops
from tensorflow.python.ops import nn_ops
from tensorflow.python.ops.math_ops import tanh
from tensorflow.contrib.rnn import RNNCell

import numpy as np
import copy
from collections import deque


def _hidden_to_output(h, hidden_size, input_size):
    out_w = tf.get_variable("out_w", [hidden_size, input_size], dtype= tf.float32)
    out_b = tf.get_variable("out_b", [input_size], dtype=tf.float32)
    output = tf.matmul(h, out_w) + out_b
    return output


def rnn_with_feed_prev(cell, inputs, feed_prev, config):
    
    prev = None
    outputs = []

    if feed_prev:
      print("Creating model --> Feeding output back into input.")
    else:
      print("Creating model input = ground truth each timestep.")

    with tf.variable_scope("rnn") as varscope:
        if varscope.caching_device is None:
            varscope.set_caching_device(lambda op: op.device)

        inputs_shape = inputs.get_shape().with_rank_at_least(3)
        batch_size = tf.shape(inputs)[0] 
        num_steps = inputs_shape[1]
        input_size = inputs_shape[2]
        


        burn_in_steps = config.burn_in_steps
        # print('batch size','input_size', batch_size, input_size)
        output_size = cell.output_size
        initial_state = cell.zero_state(batch_size, dtype= tf.float32)
        state = initial_state

        for time_step in range(num_steps):

            if time_step > 0:
                tf.get_variable_scope().reuse_variables()
            inp = inputs[:, time_step, :]

            if feed_prev and prev is not None and time_step >= burn_in_steps:
                inp = _hidden_to_output(prev, output_size, input_size)
                print('feed_prev inp shape', inp.get_shape())
                print("t", time_step, ">=", burn_in_steps, "--> feeding back output into input.")

            (cell_output, state) = cell(inp, state)

            if feed_prev:
              prev = cell_output

            output = _hidden_to_output(cell_output, output_size, input_size)
            outputs.append(output)

    outputs = tf.stack(outputs, 1)

    return outputs, state


def tensor_train_contraction(states_tensor, cores):
    # print("input:", states_tensor.name, states_tensor.get_shape().as_list())
    # print("mat_dims", mat_dims)
    # print("mat_ranks", mat_ranks)
    # print("mat_ps", mat_ps)
    # print("mat_size", mat_size)

    abc = "abcdefgh"
    ijk = "ijklmnopqrstuvwxy"

    def _get_indices(r):
        indices = "%s%s%s" % (abc[r], ijk[r], abc[r+1])
        return indices

    def _get_einsum(i, s2):
        #
        s1 = _get_indices(i)
        _s1 = s1.replace(s1[1], "")
        _s2 = s2.replace(s2[1], "")
        _s3 = _s2 + _s1
        _s3 = _s3[:-3] + _s3[-1:]
        s3 = s1 + "," + s2 + "->" + _s3
        return s3, _s3

    num_orders = len(cores)
    # first factor
    x = "z" + ijk[:num_orders] # "z" is the batch dimension
    # print mat_core.get_shape().as_list()

    _s3 = x[:1] + x[2:] + "ab"
    einsum = "aib," + x + "->" + _s3
    x = _s3
    # print "order", i, einsum

    out_h = tf.einsum(einsum, cores[0], states_tensor)
    # print(out_h.name, out_h.get_shape().as_list())

    # 2nd - penultimate latent factor
    for i in range(1, num_orders):

        # We now compute the tensor inner product W * H, where W is decomposed
        # into a tensor-train with D factors A^i. Each factor A^i is a 3-tensor,
        # with dimensions [mat_rank[i], hidden_size, mat_rank[i+1] ]
        # The lag index, indexing the components of the state vector H,
        # runs from 1 <= i < K.

        # print mat_core.get_shape().as_list()

        einsum, x = ss, _s3 = _get_einsum(i, x)

        # print "order", i, ss

        out_h = tf.einsum(einsum, cores[i], out_h)
        # print(out_h.name, out_h.get_shape().as_list())

    # print "Squeeze out the dimension-1 dummy dim (first dim of 1st latent factor)"
    out_h = tf.squeeze(out_h, [1])
    return out_h


def tensor_network_tt_einsum(inputs, states, output_size, rank_vals, bias, bias_start=0.0, scope=None):

    # print("Using Einsum Tensor-Train decomposition.")

    """tensor train decomposition for the full tenosr """
    num_orders = len(rank_vals)+1#alpha_1 to alpha_{K-1}
    num_lags = len(states)
    batch_size = tf.shape(inputs)[0] 
    state_size = hidden_size = output_size #hidden layer size
    input_size= inputs.get_shape()[1].value

    with vs.variable_scope(scope or "tensor_network_tt"):

        total_state_size = (state_size * num_lags + 1 )

        # These bookkeeping variables hold the dimension information that we'll
        # use to store and access the transition tensor W efficiently.
        mat_dims = np.ones((num_orders,)) * total_state_size

        # The latent dimensions used in our tensor-train decomposition.
        # Each factor A^i is a 3-tensor, with dimensions [a_i, hidden_size, a_{i+1}]
        # with dimensions [mat_rank[i], hidden_size, mat_rank[i+1] ]
        # The last
        # entry is the output dimension, output_size: that dimension will be the
        # output.
        mat_ranks = np.concatenate(([1], rank_vals, [output_size]))

        # This stores the boundary indices for the factors A. Starting from 0,
        # each index i is computed by adding the number of weights in the i'th
        # factor A^i.
        mat_ps = np.cumsum(np.concatenate(([0], mat_ranks[:-1] * mat_dims * mat_ranks[1:])),dtype=np.int32)
        mat_size = mat_ps[-1]

        # Compute U * x
        weights_x = vs.get_variable("weights_x", [input_size, output_size] )
        out_x = tf.matmul(inputs, weights_x)

        # Get a variable that holds all the weights of the factors A^i of the
        # transition tensor W. All weights are stored serially, so we need to do
        # some bookkeeping to keep track of where each factor is stored.
        mat = vs.get_variable("weights_h", mat_size) # h_z x h_z... x output_size

        #mat = tf.Variable(mat, name="weights")
        states_vector = tf.concat(states, 1)
        states_vector = tf.concat( [states_vector, tf.ones([batch_size, 1])], 1)
        """form high order state tensor"""
        states_tensor = states_vector
        for order in range(num_orders-1):
            states_tensor = _outer_product(batch_size, states_tensor, states_vector)

        # print("tensor product", states_tensor.name, states_tensor.get_shape().as_list())
        cores = []
        for i in range(num_orders):
                # Fetch the weights of factor A^i from our big serialized variable weights_h.
                mat_core = tf.slice(mat, [mat_ps[i]], [mat_ps[i + 1] - mat_ps[i]])
                mat_core = tf.reshape(mat_core, [mat_ranks[i], total_state_size, mat_ranks[i + 1]])   
                cores.append(mat_core)
        out_h = tensor_train_contraction(states_tensor, cores)
        # Compute h_t = U*x_t + W*H_{t-1}
        res = tf.add(out_x, out_h)

        # print "END OF CELL CONSTRUCTION"
        # print "========================"
        # print ""

        if not bias:
            return res
        biases = vs.get_variable("biases", [output_size])

    return nn_ops.bias_add(res,biases)


class EinsumTensorRNNCell(RNNCell):
    """RNN cell with high order correlations"""
    def __init__(self, num_units, num_lags, rank_vals, input_size=None, state_is_tuple=True, activation=tanh):
            self._num_units = num_units
            self._num_lags = num_lags
    #rank of the tensor, tensor-train model is order+1
            self._rank_vals = rank_vals
            #self._num_orders = num_orders
            self._state_is_tuple= state_is_tuple
            self._activation = activation

    @property
    def state_size(self):
            return self._num_units

    @property
    def output_size(self):
            return self._num_units

    def __call__(self, inputs, states, scope=None):
            """Now we have multiple states, state->states"""

            with vs.variable_scope(scope or "tensor_rnn_cell"):
                    output = tensor_network_tt_einsum( inputs, states, self._num_units,self._rank_vals, True, scope=scope)
                    # dense = tf.contrib.layers.fully_connected(output, self._num_units, activation_fn=None, scope=scope)
                    # output = tf.contrib.layers.batch_norm(output, center=True, scale=True, 
                    #                               is_training=True, scope=scope)
                    new_state = self._activation(output)
            if self._state_is_tuple:
                    new_state = (new_state)
            return new_state, new_state


def _outer_product(batch_size, tensor, vector):
    """tensor-vector outer-product"""
    tensor_flat= tf.expand_dims(tf.reshape(tensor, [batch_size,-1]), 2)
    vector_flat = tf.expand_dims(vector, 1)
    res = tf.matmul(tensor_flat, vector_flat)
    new_shape =  [batch_size]+_shape_value(tensor)[1:]+_shape_value(vector)[1:]
    res = tf.reshape(res, new_shape )
    return res

def _shape_value(tensor):
    shape = tensor.get_shape()
    return [s.value for s in shape]

def _shift (input_list, new_item):
    """Update lag number of states"""
    output_list = copy.copy(input_list)
    output_list = deque(output_list)
    output_list.append(new_item)
    output_list.rotate(1) # The deque is now: [3, 1, 2]
    output_list.popleft() # deque == [2, 3]
    return output_list

def _list_to_states(states_list):
    """Transform a list of state tuples into an augmented tuple state
    customizable function, depends on how long history is used"""
    num_layers = len(states_list[0])# state = (layer1, layer2...), layer1 = (c,h), c = tensor(batch_size, num_steps)
    output_states = ()
    for layer in range(num_layers):
            output_state = ()
            for states in states_list:
                    #c,h = states[layer] for LSTM
                    output_state += (states[layer],)
            output_states += (output_state,)
            # new cell has s*num_lags states
    return output_states

def tensor_rnn_with_feed_prev(cell, inputs, feed_prev, config):
    """High Order Recurrent Neural Network Layer
    """
    #tuple of 2-d tensor (batch_size, s)
    outputs = []
    prev = None

    if feed_prev:
        print("Creating model @ not training --> Feeding output back into input.")
    else:
        print("Creating model @ training --> input = ground truth each timestep.")

    with tf.variable_scope("tensor_rnn") as varscope:
        if varscope.caching_device is None:
                    varscope.set_caching_device(lambda op: op.device)

        inputs_shape = inputs.get_shape().with_rank_at_least(3)
        batch_size = tf.shape(inputs)[0] 
        num_steps = inputs_shape[1]
        input_size = inputs_shape[2]
        output_size = cell.output_size

        initial_states =[]
        for lag in range(config.num_lags):
            initial_state =  cell.zero_state(batch_size, dtype= tf.float32)
            initial_states.append(initial_state)
        states_list = initial_states #list of high order states

    
        for time_step in range(num_steps):
            if time_step > 0:
                tf.get_variable_scope().reuse_variables()

            inp = inputs[:, time_step, :]


            if feed_prev and prev is not None and time_step >= config.burn_in_steps:
                inp = _hidden_to_output(prev, output_size, input_size)
                print("t", time_step, ">=", config.burn_in_steps, "--> feeding back output into input.")

            states = _list_to_states(states_list)
            """input tensor is [batch_size, num_steps, input_size]"""
            (cell_output, state)=cell(inp, states)

            states_list = _shift(states_list, state)

            if feed_prev:
                prev = cell_output

            output = _hidden_to_output(cell_output, output_size, input_size)
            outputs.append(output)

    outputs = tf.stack(outputs,1)

    return outputs, states



