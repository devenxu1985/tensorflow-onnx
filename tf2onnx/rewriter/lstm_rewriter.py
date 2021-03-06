# Copyright (c) Microsoft Corporation. All rights reserved.
# Licensed under the MIT license.

"""
tf2onnx.rewriter.lstm_rewriter - lstm support
"""

from __future__ import division
from __future__ import print_function

from tf2onnx.rewriter.unit_rewriter_base import *

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("tf2onnx.rewriter.lstm_rewriter")


class LSTMUnitRewriter(UnitRewriterBase):
    def __init__(self, g):
        super(LSTMUnitRewriter, self).__init__(g)

    def run(self):
        return super(LSTMUnitRewriter, self).run(RNNUnitType.LSTMCell)

    def get_rnn_scope_name(self, match):
        # take the cell output and go up 3 levels to find the scope:
        # name of h is like root/while/lstm_cell/mul_2
        # root is the dynamic rnn's scope name. 
        # root/while/lstm_cell is cell's scope name
        h_node = match.get_op("ht")
        parts = h_node.name.split('/')
        rnn_scope_name = '/'.join(parts[0:-3])
        return rnn_scope_name

    def get_weight_and_bias(self, match):
        # if one of them is not match, just return
        w_e = match.get_op("cell_kernel")
        w = get_weights_from_const_node(w_e)
        if not w:
            return

        # check https://www.tensorflow.org/versions/r1.8/api_docs/cc/class/tensorflow/ops/bias-add
        # for bias_add data format
        bias_add = match.get_op("bias_add")
        if bias_add.data_format != "NHWC":
            log.debug("BiasAdd data_format is not NHWC, SKIP")
            return

        b_e = match.get_op("cell_bias")
        b = get_weights_from_const_node(b_e)
        if not b or b.value.shape[0] != w.value.shape[1]:
            log.warning("cell_kernel and cell_bias's dimensions does not match, skip")
            return 

        ft_bias = match.get_op("ft_bias")
        ft = get_weights_from_const_node(ft_bias)
        if not ft:
            return

        if not (len(ft.value) == 1 and b_e.dtype == ft_bias.dtype):
            return

        return RnnWeights(w, b, ft)

    def ct_switch_check(self, enter_target_node_input_id, identity_consumers, match):
        # original we use c.inputs[0] == match.get_op("ft") to check c initializer for LSTMCell
        # but in BasicLSTMCell, c.inputs[1] is "ft", that's because BasicLSTMCell and LSTMCell's call 
        # function are defining the multiplication with different order. So we change to match.get_op("ft") in c.inputs
        mul_nodes = [c for c in identity_consumers if c.type == "Mul" and match.get_op("ft") in c.inputs]
        if len(mul_nodes) == 1:
            log.debug("find c initializer value at " + enter_target_node_input_id)
            return enter_target_node_input_id
        elif len(mul_nodes) > 1:
            raise ValueError("multiple Mul matching found, cannot identify c initializer")

    def ht_switch_check(self, enter_target_node_input_id, identity_consumers, match):
        concat_nodes = [c for c in identity_consumers if c == match.get_op("xh")]
        if len(concat_nodes) == 1:
            log.debug("find h initializer value at " + enter_target_node_input_id)
            return enter_target_node_input_id
        elif len(concat_nodes) > 1:
            raise ValueError(str(len(concat_nodes)) + "Concat matching found, cannot identify h initializer")

    def ct_ht_shared_switch_check(self, enter_target_node_input_id, identity_consumers, match):
        slices = [c for c in identity_consumers if c.type == "Slice"]
        if not slices:
            log.debug("find no switch_identity_slice nodes")
            return None

        c_slice = None
        h_slice = None
        hidden_size = None
        for s in slices:
            slice_consumers = self.g.find_output_consumers(s.output[0])
            if len(slice_consumers) != 1:
                continue

            s_begin = s.inputs[1].get_tensor_value()
            s_size = s.inputs[2].get_tensor_value()
            hidden_size = s_size[1]
            if list(s_begin) == [0, 0]:
                c_slice = s
            elif list(s_begin) == [0, hidden_size]:
                h_slice = s

        if c_slice and h_slice:
            log.debug("find c_h shared initializer value at " + enter_target_node_input_id) 
            return enter_target_node_input_id

    def process_weights_and_bias(self, rnn_weights):
        w_r_icfo = rnn_weights.kernel.value
        w_dtype = rnn_weights.kernel.dtype
        b_r_icfo = rnn_weights.bias.value
        b_dtype = rnn_weights.bias.dtype
        ft_bias_scalar = rnn_weights.forget_bias.value

        # split bias for each hidden unit
        # b_r_icfo: (4 * num_units,)
        bias_dim = b_r_icfo.shape[0]
        hidden_size = int(bias_dim/4)
        b_r_icfo = np.reshape(b_r_icfo, (1, bias_dim))
        bias_gates = np.split(b_r_icfo, 4, axis=1)
        ft_bias = np.add(bias_gates[2], ft_bias_scalar[0])
        wb_bias_iofc = np.concatenate((bias_gates[0], bias_gates[3], ft_bias, bias_gates[1]), axis=1)

        # fill Rb with empty since in TF, we have only one bias.
        rb_bias_iofc = np.zeros((1, bias_dim), dtype=b_dtype)
        B = np.concatenate((wb_bias_iofc, rb_bias_iofc), axis=1)
        assert B.shape == (1, 2 * bias_dim)

        [wx, wh] = np.split(w_r_icfo, [-1 * hidden_size])
        input_size = wx.shape[0]
        assert wx.shape[0] == input_size
        assert int(wx.shape[1]/4) == hidden_size

        # split weight for gates
        w_gates = np.split(wx, 4, axis=1)
        new_wx = np.concatenate((w_gates[0], w_gates[3], w_gates[2], w_gates[1]), axis=1)

        h_gates = np.split(wh, 4, axis=1)
        new_wh = np.concatenate((h_gates[0], h_gates[3], h_gates[2], h_gates[1]), axis=1)
        W_iofc = np.transpose(new_wx)
        R_iofc = np.transpose(new_wh)

        W = np.array([W_iofc], w_dtype)
        R = np.array([R_iofc], w_dtype)

        return W, R, B, input_size, hidden_size

    def process_output_connectors(self, match, lstm_node, rnn_props, rnn_scope_name):
        # There are 2 kinds of output nodes for dynamic_rnn
        # 1. output node, which would either ends with a Transpose (when time_major is False), or ends with TensorArrayGatherV3
        # 2. cell_state node, 
        #    2.1 if state_is_tuple is true:
        #        2.1.1 which would ends with a Pack<C, H> operator when cell_state is used.
        #        2.1.2 which would ends with "Exit" for c and h respectively, when cell_state.c/h is used.
        #    2.2 which would ends with "Exit" if state_is_tuple is false
        connector_nodes = set(rnn_props.connectors)
        for n in connector_nodes:
            log.debug("processing connector node called "+ n.name)
            # todo: change to another way
            if n.need_skip():
                log.debug("newly created nodes, won't consider as RNN outputs.")
                continue

            # Y handler for reverse lstm
            if rnn_props.is_backward:
                # Y handler
                if is_reverse_op(n):
                    output_node_in_scope = n.inputs[0]
                    if self._check_is_rnn_outputs_node(output_node_in_scope, rnn_props.time_major, rnn_scope_name):
                        last_node = self._create_transform_nodes_after_lstm_output(lstm_node, rnn_props.time_major)
                        n.input[0] = last_node.output[0]
                        continue

            # tupled Y_c/Y_h handling, use tuple directly
            # be noted: for reverse, unlike output node (who is followed by a reverse op), 
            # the Pack node generating cell_state don't have reverse op followed.
            if self._check_is_consumer_of_tupled_ch(n, match, rnn_scope_name):
                self._connect_rnn_with_tupled_ch_consumer_nodes(lstm_node, n)
            else:
                to_replace = {}
                for input_id, input_n in zip(n.input, n.inputs):
                    if not input_n:
                        log.debug("node " + input_id + " is none, skip")
                        continue
                    if not input_n.name.startswith(rnn_scope_name):
                        log.debug("skip " + input_n.name)
                        continue
                    else:
                        # Y handler for Non-reverse lstm
                        if not rnn_props.is_backward and self._check_is_rnn_outputs_node(input_n, rnn_props.time_major, rnn_scope_name):
                            log.debug("this is the rnn output node's consumer")
                            last_node = self._create_transform_nodes_after_lstm_output(lstm_node, rnn_props.time_major)
                            to_replace[input_id] = last_node.output[0]
                        else:
                            error_code = self._check_is_consumer_of_exit_after_ch(match, input_n, rnn_scope_name)
                            if error_code == 1: # tupled Y_c/Y_h handling, use tuple.c
                                self._connect_rnn_with_one_of_tupled_ch_consumer_nodes(lstm_node.output[2], input_id)
                            elif error_code == 2: # tupled Y_c/Y_h handling, use tuple.h
                                self._connect_rnn_with_one_of_tupled_ch_consumer_nodes(lstm_node.output[1], input_id)
                            elif error_code == 3: # non-tupled Y_c/Y_h handling. (shared same Exit)
                                self._connect_rnn_with_non_tupled_ch_consumer_nodes(lstm_node, n, input_id)
                            else:
                                raise ValueError("not match rnn output node, skip " + input_n.name)
                for input_id in to_replace:
                    self.g.replace_all_inputs(self.all_nodes, input_id, to_replace[input_id])

    # c: memory (in TF, it was called hidden state)
    # h: hidden state (in TF, it was called output)
    def _check_is_consumer_of_tupled_ch(self, pack_node, match, rnn_scope_name):
        # This Pack is generated when dynamic_rnn return cell_state as a tuple, e.g <c, h>.
        # Pack's name is not in the rnn scope.
        if not (pack_node.type == "Pack" and len(pack_node.inputs) == 2):
            log.debug("check_is_ch_output_node Pack check fail")
            return False

        exit_1 = pack_node.inputs[0]
        exit_2 = pack_node.inputs[1]
        if not (exit_1 and exit_1.type == "Exit" and exit_2 and exit_2.type == "Exit"):
            log.debug("check_is_ch_output_node Exit check fail")
            return False

        if not (exit_1.name.startswith(rnn_scope_name) and exit_2.name.startswith(rnn_scope_name)):
            log.debug("check_is_ch_output_node Exits rnn scope name is not consistent")
            return False

        switch_1 = exit_1.inputs[0]
        switch_2 = exit_2.inputs[0]
        if not (switch_1.type == "Switch" and switch_2.type == "Switch"):
            log.debug("check_is_ch_output_node Switch check fail")
            return False

        ct_enter_target_node = None
        ht_enter_target_node = None
        for s in [switch_1, switch_2]:
            enter_target_input_id = self.check_switch_by_usage_pattern(s, match, self.ct_switch_check)
            if enter_target_input_id:
                ct_enter_target_node = enter_target_input_id
                continue

            enter_target_input_id = self.check_switch_by_usage_pattern(s, match, self.ht_switch_check)
            if enter_target_input_id:
                ht_enter_target_node = enter_target_input_id
                continue

        if ct_enter_target_node and ht_enter_target_node:
            return True

        log.debug("fail to found ct and ht node based on pattern")
        return False

    def _check_is_consumer_of_exit_after_ch(self, match, connector_in_rnnscope, rnn_scope_name):
        if not (connector_in_rnnscope and connector_in_rnnscope.type == "Exit"):
            log.debug("_check_is_consumer_of_exit_after_ch Exit check fail")
            return False

        if not connector_in_rnnscope.name.startswith(rnn_scope_name):
            log.debug("_check_is_consumer_of_exit_after_ch Exit rnn scope name check fail")
            return False

        switch = connector_in_rnnscope.inputs[0]
        if not (switch.type == "Switch"):
            log.debug("_check_is_consumer_of_exit_after_ch Switch check fail")
            return False

        enter_target_input_id = self.check_switch_by_usage_pattern(switch, match, self.ct_switch_check)
        if enter_target_input_id:
            return 1
        
        enter_target_input_id = self.check_switch_by_usage_pattern(switch, match, self.ht_switch_check)
        if enter_target_input_id:
            return 2

        enter_target_input_id = self.check_switch_by_usage_pattern(switch, match, self.ct_ht_shared_switch_check)
        if enter_target_input_id:
            return 3

        log.debug("_check_is_consumer_of_exit_after_ch fail to found ct and ht node based on pattern")
        return False

    @staticmethod
    def _check_is_rnn_outputs_node(connector_in_rnnscope, time_major, rnn_scope_name):
        node_to_check = connector_in_rnnscope
        if not time_major:
            # in batch_major mode, rnn outputs will ends with a Tranpose. So
            # here we check the Transpose. 

            # Be noted, in TF, transpose has 2 inputs.
            if not (len(connector_in_rnnscope.inputs) == 2 and check_is_timemajor_transpose(connector_in_rnnscope)):
                log.debug("_check_is_rnn_outputs_node error, in batch_major mode, Transpose should be found but actually not.")
                return False

            if not connector_in_rnnscope.name.startswith(rnn_scope_name):
                log.debug("_check_is_rnn_outputs_node Tranpose not in current rnn scope")
                return False

            # the first input is data
            node_to_check = connector_in_rnnscope.inputs[0]

        if node_to_check.type in ["TensorArrayGatherV3"] and node_to_check.name.startswith(rnn_scope_name):
            log.debug("Find output node " + connector_in_rnnscope.name)
            return True

    def _create_transform_nodes_after_lstm_output(self, lstm_node, time_major):
        # here we gave up existing transpose, instead, add some ops based on lstm node's result (indirect or directly)
        # just make sure the final output is [batch, time, hidden]

        # insert Squeeze in axes 1
        # lstm's 1st output shape is [time, num_directions, batch, hidden]
        output_id = lstm_node.output[0]
        squeeze_node = make_onnx_node(self.g, "Squeeze", [output_id], attr={"axes": [1]})
        lstm_output_shape = self.g.get_shape(output_id)
        self.g.set_shape(squeeze_node.output[0], [lstm_output_shape[0], lstm_output_shape[2], lstm_output_shape[3]])

        if not time_major:
            # transpose to [batch, time, hidden], since node n originally use this
            attr = { "perm": np.array([1, 0, 2], dtype=np.int64) }
            new_trans = make_onnx_node(self.g, "Transpose", [squeeze_node.output[0]], attr)
            trans_input_shape = self.g.get_shape(squeeze_node.output[0])
            self.g.set_shape(new_trans.output[0], [trans_input_shape[1], trans_input_shape[0], trans_input_shape[2]])
            self.all_nodes.extend([squeeze_node, new_trans])
            return new_trans
        else:
            self.all_nodes.append(squeeze_node)
            return squeeze_node

    def _connect_rnn_with_tupled_ch_consumer_nodes(self, lstm_node, pack_node):
        # Pack_node's original two inputs in TF have shape: [batch_size, hidden_size]
        # BUT now, we have [num_directions, batch_size, hidden_size] for each input
        # since this branch handles forward/reverse only, num_directions = 1

        ch_outputer_node = self._prepare_y_ch_node_outputer(lstm_node, True)
        self.g.replace_all_inputs(self.all_nodes, pack_node.output[0], ch_outputer_node.output[0])
        # For all Pack's consumers, they originally expect data [tuple_size (e.g. 2), batch_size, hidden_size],

        # here we give up the original pack_node.
        self.all_nodes.remove(pack_node)

    def _connect_rnn_with_one_of_tupled_ch_consumer_nodes(self, lstm_output_id, exit_node_output_id):
        # For original consumers, they originally expect data [batch_size, hidden_size],
        # BUT now, we have [num_directions, batch_size, hidden_size]
        # since this branch handles forward only, num_directions = 1
        squeeze_node = make_onnx_node(self.g, "Squeeze", [lstm_output_id], {"axes" :[0]})
        lstm_output_shape = self.g.get_shape(lstm_output_id)
        self.g.set_shape(squeeze_node.output[0], [lstm_output_shape[1], lstm_output_shape[2]])
        self.g.replace_all_inputs(self.all_nodes, exit_node_output_id, squeeze_node.output[0])
        self.all_nodes.extend([squeeze_node])

    def _connect_rnn_with_non_tupled_ch_consumer_nodes(self, lstm_node, connector_node_outside_rnn_scope, exit_node_output_id):
        n = connector_node_outside_rnn_scope
        ch_outputer_node = self._prepare_y_ch_node_outputer(lstm_node, False)
        self.g.replace_input(n, exit_node_output_id, ch_outputer_node.output[0])

    def _prepare_y_ch_node_outputer(self, lstm_node, is_ch_tupled):
        axis = None
        # if original graph need tupled output, then we concat c and h with axis = 0
        axis = 0 if is_ch_tupled else 2
        concat = make_onnx_node(self.g, "Concat", [lstm_node.output[2], lstm_node.output[1]], attr={"axis": axis})
        self.all_nodes.append(concat)
        yc_shape = self.g.get_shape(lstm_node.output[2])
        if is_ch_tupled: 
            self.g.set_shape(concat.output[0], [yc_shape[0] * 2, yc_shape[1], yc_shape[2]])
            return concat
        else: 
            self.g.set_shape(concat.output[0], [yc_shape[0], yc_shape[1], yc_shape[2] * 2])

        # For all non-tupled-ch's consumers, they originally expect data [batch_size, hidden_size*2].
        # since this branch handles forward only, num_directions = 1
        squeeze_node = make_onnx_node(self.g, "Squeeze", [concat.output[0]], attr={"axes": [0]})
        concat_shape = self.g.get_shape(concat.output[0])
        self.g.set_shape(squeeze_node.output[0], [concat_shape[1], concat_shape[2]])
        self.all_nodes.append(squeeze_node)

        return squeeze_node
