# Copyright 2019 Robert Csordas. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
# ==============================================================================

import torch
import torch.nn.functional as F
import math

class Graph:
    def __init__(self, batch_size, state_size, device):
        if batch_size is None:
            return
        self.batch_size = batch_size
        self.device = device

        self.nodes = torch.zeros(0, state_size, dtype=torch.float32, device=device)
        self.node_types = torch.zeros(0, dtype=torch.uint8, device=device)
        self.edge_source = torch.zeros(0, dtype=torch.long, device=device)
        self.edge_dest = torch.zeros(0, dtype=torch.long, device=device)
        self.edge_features = torch.zeros(0, state_size, dtype=torch.float, device=device)
        self.edge_types = torch.zeros(0, dtype=torch.uint8, device=device)
        self.owner_masks = torch.zeros(batch_size, 0, dtype=torch.uint8, device=device)
        self.last_inserted_node = torch.zeros(batch_size, dtype=torch.long, device=device)

        self.running = torch.ones(batch_size, device=device, dtype=torch.uint8)

    def get_final_graph(self, device=torch.device("cpu")):
        needed = ["node_types", "edge_source", "edge_dest", "edge_types", "owner_masks"]
        res = Graph(self.batch_size, 1, device)
        for k, v in self.__dict__.items():      
            if torch.is_tensor(v):
                res.__dict__[k] = v.to(device) if k in needed else None
        return res

def sample_softmax(tensor, dim=-1):
    eps=1e-20

    # Built in gumbel softmax could end up with lots of nans. Do it manually here.
    noise = -torch.log(-torch.log(torch.rand_like(tensor)+eps) + eps)
    res = F.softmax(tensor + noise, dim=-1)
    _, res = res.max(dim=dim)
    return res

def mask_softmax_input(tensor, mask):
    return torch.where(mask, tensor, torch.full([1], float("-inf"), dtype=tensor.dtype, device=tensor.device))

def masked_softmax(tensor, mask):
    tensor = mask_softmax_input(tensor, mask)
    return sample_softmax(tensor)

def loss_running_gate(l, running):
    return torch.where(running, l, torch.zeros([1], dtype=l.dtype, device=l.device)).mean()

def masked_cross_entropy_loss(tensor, mask, target, enabled):
    tensor = mask_softmax_input(tensor, mask) if mask is not None else tensor
    l = F.cross_entropy(tensor, target.long(), reduction="none")
    return loss_running_gate(l, enabled)

def remap_pad(t, pad_char, transform = lambda x: x+1):
    return torch.where(t != pad_char, transform(t), torch.zeros(1, dtype=t.dtype, device=t.device))

def masked_bce_loss(tensor, target, enabled):
    l = F.binary_cross_entropy_with_logits(tensor, target.float(), reduction="none")
    return loss_running_gate(l, enabled)

def sample_binary(tensor):
    tensor = torch.sigmoid(tensor)
    return torch.rand_like(tensor) < tensor

def xavier_init(layer, scale, n_inputs=None, n_outputs=None):
    n_inputs = n_inputs if n_inputs is not None else layer.weight.shape[1]
    n_outputs = n_outputs if n_outputs is not None else layer.weight.shape[0]
    limits = scale * math.sqrt(6.0 / (n_inputs + n_outputs))
    layer.weight.data.uniform_(-limits, limits)

    if layer.bias is not None:
        torch.nn.init.normal_(layer.bias)


class Aggregator(torch.nn.Module):
    def __init__(self, state_size, aggregated_size, dropout, bias_if_empty=False):
        super().__init__()

        self.transform = torch.nn.Linear(state_size, aggregated_size)
        self.gate = torch.nn.Sequential(
            torch.nn.Linear(state_size, aggregated_size),
            torch.nn.Sigmoid()
        )

        self.bias_if_empty = torch.nn.Parameter(torch.Tensor(1,aggregated_size)) if bias_if_empty else None

        self.drop = torch.nn.Dropout(dropout)

        self.aggregated_size = aggregated_size
        self._reset_parameters()

    def forward(self, graph: Graph):
        if graph.nodes.shape[0]==0:
            if self.bias_if_empty is not None:
                return self.bias_if_empty.expand(graph.batch_size, -1)
            else:
                return torch.zeros(graph.batch_size, self.aggregated_size, dtype=torch.float32, device=graph.device)

        gates = self.gate(graph.nodes)
        data = self.transform(graph.nodes)

        fmask = graph.owner_masks.float()
        res = torch.mm(fmask, data * gates)

        # Normalize the result with the number of nodes.
        return self.drop(res)

    def _reset_parameters(self):
        xavier_init(self.transform, 1)
        xavier_init(self.gate[0], 1)
        self.gate[0].bias.data.fill_(1)
        if self.bias_if_empty is not None:
            torch.nn.init.normal_(self.bias_if_empty)


class Propagator(torch.nn.Module):
    def __init__(self, state_size, dropout):
        super().__init__()

        self.message_size = state_size * 2

        self.node_update_fn = torch.nn.GRUCell(self.message_size, state_size)

        # The first layer of message function (fe) can be decomposed to 3 parts, which makes it easier to
        # claculate
        self.message_node = torch.nn.Linear(state_size, self.message_size, bias=False)
        self.message_features = torch.nn.Linear(state_size, self.message_size, bias=False)

        self.message_layer_2 = torch.nn.Sequential(
            torch.nn.Tanh(),
            torch.nn.Linear(self.message_size, self.message_size)
        )

        self.dropout = torch.nn.Dropout(dropout)
        self._reset_parameters(state_size)

    @staticmethod
    def _node_update_mask(graph: Graph, mask_override: torch.ByteTensor):
        return graph.owner_masks[graph.running if mask_override is None else mask_override].sum(0)>0

    def forward(self, graph: Graph, mask_override: torch.ByteTensor = None):
        if graph.nodes.shape[0]==0 or graph.edge_features.shape[0]==0:
            return graph

        edge_features = self.message_features(graph.edge_features)
        node_features = self.message_node(graph.nodes)

        e1 = node_features.index_select(dim=0, index=graph.edge_source)
        e2 = node_features.index_select(dim=0, index=graph.edge_dest)

        messages = e1 + e2 + edge_features
        messages = self.message_layer_2(messages)
        messages = self.dropout(messages)

        # Sum the messages for each node
        inputs = torch.zeros(graph.nodes.shape[0], self.message_size, device=graph.nodes.device,
                             dtype=graph.nodes.dtype).index_add_(0, graph.edge_dest, messages).\
                             index_add_(0, graph.edge_source, messages)

        inputs = self.dropout(inputs)

        # Transform node state of running nodes
        new_nodes = self.node_update_fn(inputs, graph.nodes)

        graph.nodes = torch.where(self._node_update_mask(graph, mask_override).unsqueeze(-1), new_nodes, graph.nodes)
        return graph

    def _reset_parameters(self, state_size):
        # msg_gain = 1
        msg_gain = torch.nn.init.calculate_gain("tanh")
        xavier_init(self.message_node, msg_gain, state_size * 3, self.message_size)
        xavier_init(self.message_features, msg_gain, state_size * 3, self.message_size)
        xavier_init(self.message_layer_2[1], 1)
        
        self.node_update_fn.bias_hh.data.fill_(0)
        self.node_update_fn.bias_ih.data.fill_(0)
        self.node_update_fn.bias_hh[:state_size].data.fill_(1)


class MultilayerPropagator(torch.nn.Module):
    def __init__(self, state_size, n_steps, dropout):
        super().__init__()
        self.propagators = torch.nn.ModuleList([Propagator(state_size, dropout) for i in range(n_steps)])

    def forward(self, graph: Graph, *args, **kwargs):
        for p in self.propagators:
            graph = p(graph, *args, **kwargs)
        return graph


class NodeAdder(torch.nn.Module):
    def __init__(self, state_size, aggregated_size, propagate_steps, n_node_types, pad_char, dropout):
        super().__init__()

        self.pad_char = pad_char

        self.propagator = MultilayerPropagator(state_size, propagate_steps, dropout)
        self.decision_aggregator = Aggregator(state_size, aggregated_size, dropout, bias_if_empty=True)
        self.init_aggregator = Aggregator(state_size, aggregated_size, dropout, bias_if_empty=True)

        self.node_type_decision = torch.nn.Linear(aggregated_size, n_node_types+1)

        self.node_type_embedding = torch.nn.Parameter(torch.Tensor(n_node_types, state_size))

        self.f_init_part1 = torch.nn.Linear(state_size, state_size)
        self.f_init_part2 = torch.nn.Linear(aggregated_size, state_size, bias=False)
        self._reset_parameters(state_size, aggregated_size)

    def forward(self, graph: Graph, reference: torch.ByteTensor):
        loss = 0
        graph = self.propagator(graph)

        new_node_types = self.node_type_decision(self.decision_aggregator(graph))
        if reference is not None:
            selected_type = remap_pad(reference, self.pad_char)
            loss = loss + masked_cross_entropy_loss(new_node_types, None, selected_type, graph.running)
        else:
            # Prevent generating empty graph. Set termination probability to 0 if generating the first element.
            if graph.nodes.shape[0]==0:
                new_node_types[:, 0]=float("-inf")
            selected_type = sample_softmax(new_node_types)

        # Update running flags. If no new node is generated, the graph generation is stopped
        graph.running = (selected_type != 0) & graph.running
        if graph.running.any():
            # Initialize new nodes
            new_type_embedding = self.node_type_embedding.index_select(0, (selected_type.long() - 1).clamp(min=0))
            init_features = self.init_aggregator(graph)

            new_features = self.f_init_part1(new_type_embedding) + self.f_init_part2(init_features)

            # Add the new nodes
            mask = graph.running
            index_seq = torch.arange(mask.long().sum(), device = graph.device, dtype = torch.long) + \
                        (graph.nodes.shape[0] if graph.nodes is not None else 0)
            last_nodes = torch.zeros(graph.batch_size, device = graph.device, dtype = torch.long)
            last_nodes[mask] = index_seq

            # Select last node if updated
            graph.last_inserted_node = torch.where(mask, last_nodes, graph.last_inserted_node)

            # Merge new nodes to the node list
            new_nodes = new_features[mask]
            owner_masks = F.one_hot(mask.nonzero().squeeze(-1), graph.batch_size).transpose(0,1).byte()

            graph.nodes = torch.cat((graph.nodes, new_nodes), dim=0)
            graph.owner_masks = torch.cat((graph.owner_masks, owner_masks), dim=1)
            graph.node_types = torch.cat((graph.node_types, selected_type[mask].byte()-1), dim=0)

        return graph, loss

    def _reset_parameters(self, state_size, aggregated_size):
        torch.nn.init.normal_(self.node_type_embedding)
        xavier_init(self.f_init_part1, 1, state_size + aggregated_size, state_size)
        xavier_init(self.f_init_part2, 1, state_size + aggregated_size, state_size)
        xavier_init(self.node_type_decision, 1)


class EdgeAdder(torch.nn.Module):
    def __init__(self, state_size, aggregated_size, n_edge_dtypes, pad_char, propagate_steps, n_max_edges, dropout):
        super().__init__()

        self.pad_char = pad_char
        self.n_edge_dtypes = n_edge_dtypes
        self.n_max_edges = n_max_edges

        self.propagator = MultilayerPropagator(state_size, propagate_steps, dropout)

        self.edge_decision_aggregator = Aggregator(state_size, aggregated_size, dropout)
        self.edge_init = torch.nn.Parameter(torch.Tensor(n_edge_dtypes, state_size))
        self.edge_init_aggregator = Aggregator(state_size, aggregated_size, dropout)

        self.f_addedge_aggregated = torch.nn.Linear(aggregated_size, 1)
        self.f_addedge_new = torch.nn.Linear(state_size, 1, bias=False)

        self.fs_layer1_target = torch.nn.Linear(state_size, n_edge_dtypes)
        self.fs_layer1_new = torch.nn.Linear(state_size, n_edge_dtypes, bias=False)

        # self.fs_layer1_target = torch.nn.Linear(state_size, (state_size+n_edge_dtypes)//2)
        # self.fs_layer1_new = torch.nn.Linear(state_size, (state_size+n_edge_dtypes)//2, bias=False)

        # self.fs_rest = torch.nn.Sequential(
        #     torch.nn.Tanh(),
        #     torch.nn.Linear((state_size+n_edge_dtypes)//2, n_edge_dtypes)
        # )

        self._reset_paramters(state_size, aggregated_size, n_edge_dtypes)

    def forward(self, graph: Graph, reference):
        # Decide whether to add an edge.
        loss = 0
        running = graph.running

        if reference is not None and not reference:
            return graph, loss

        add_index = 0

        new_nodes = graph.nodes.index_select(0, graph.last_inserted_node)

        while True:
            graph = self.propagator(graph, running)
            new_edge_needed = (self.f_addedge_aggregated(self.edge_decision_aggregator(graph)) +
                               self.f_addedge_new(new_nodes)).squeeze(-1)

            if reference is not None:
                assert self.n_max_edges is None or add_index < self.n_max_edges
                need_to_add = reference[add_index][1] != self.pad_char
                loss = loss + masked_bce_loss(new_edge_needed, need_to_add, running)
            else:
                need_to_add = sample_binary(new_edge_needed)

                # Force termination when the limit is reached.
                if self.n_max_edges is not None and add_index >= self.n_max_edges:
                    need_to_add = torch.zeros_like(need_to_add)

            # Stop if there are no more edges added
            running = running & need_to_add
            if not running.any():
                break

            # Decide where to add
            # The transform is fs(new_node, all_other_nodes). First layer of this can be decomposed to
            # fs_layer1_target(all_other_nodes) + fs_layer1_new(new_node).

            logits = self.fs_layer1_target(graph.nodes).unsqueeze(0) + self.fs_layer1_new(new_nodes).unsqueeze(1)
            logits = logits.view(logits.shape[0], -1)

            # Logits is a [batch_size, n_nodes * n_edge_types] tensor. A softmax over all of this is done, and
            # then sampled.
            owner_mask_expanded = graph.owner_masks.unsqueeze(-1).expand(-1,-1, self.n_edge_dtypes).contiguous().\
                                  view(graph.batch_size,-1)

            if reference is not None:
                selected_edge = reference[add_index][0].long() * self.n_edge_dtypes + \
                                 remap_pad(reference[add_index][1].long(), self.pad_char, lambda x: x)

                loss = loss + masked_cross_entropy_loss(logits, owner_mask_expanded, selected_edge, running)
            else:
                selected_edge = masked_softmax(logits, owner_mask_expanded)

            selected_type = selected_edge % self.n_edge_dtypes
            selected_other = selected_edge / self.n_edge_dtypes

            # Add the new edges.
            selected_src = graph.last_inserted_node[running]
            selected_other = selected_other[running]
            type = selected_type[running]

            feature = self.edge_init.index_select(0, (type.long()-1).clamp(min=0))

            type = type.byte()

            graph.edge_dest = torch.cat((graph.edge_dest, selected_src), 0)
            graph.edge_source = torch.cat((graph.edge_source, selected_other), 0)
            graph.edge_features = torch.cat((graph.edge_features, feature), 0)
            graph.edge_types = torch.cat((graph.edge_types, type), 0)

            add_index += 1

        return graph, loss

    def _reset_paramters(self, state_size, aggregated_size, n_edge_dtypes):
        torch.nn.init.normal_(self.edge_init)
        xavier_init(self.f_addedge_aggregated, 1, state_size + aggregated_size, 1)
        xavier_init(self.f_addedge_new, 1, state_size + aggregated_size, 1)
        xavier_init(self.fs_layer1_target, 1, state_size * 2, n_edge_dtypes)
        xavier_init(self.fs_layer1_new, 1, state_size * 2, n_edge_dtypes)


class GraphGen(torch.nn.Module):
    def __init__(self, n_node_types, n_edge_types, state_size, pad_char=255, propagate_steps=2,
                 n_max_nodes=None, n_max_edges=None, dropout=0.2):
        super().__init__()

        self.aggregated_size = state_size * 2
        self.state_size = state_size

        self.n_max_nodes = n_max_nodes

        self.edge_adder = EdgeAdder(state_size, self.aggregated_size, n_edge_types, pad_char, propagate_steps, n_max_edges, dropout)
        self.node_adder = NodeAdder(state_size, self.aggregated_size, propagate_steps, n_node_types, pad_char, dropout)

    def forward(self, ref_output, batch_size=None, device=None):
        assert ((ref_output is None) and (batch_size is not None and device is not None)) or \
               ((ref_output is not None) and (batch_size is None and device is None)), \
               "To generate, pass batch_size and device, to train, pass ref_output only."

        n_batch = ref_output[0].shape[0] if batch_size is None else batch_size
        device = ref_output[0].device if device is None else device

        loss = 0

        graph = Graph(n_batch, self.state_size, device)

        i = 0
        while True:
            if self.n_max_nodes is not None and self.n_max_nodes <= i//2:
                break

            graph, l_node = self.node_adder(graph, ref_output[i] if ref_output is not None else None)
            loss = loss + l_node

            if not graph.running.any():
                break

            graph, l_edge = self.edge_adder(graph, ref_output[i+1] if ref_output is not None else None)
            loss = loss + l_edge

            i+=2

        return graph, loss

    def generate(self, batch_size: int, device: torch.device):
        return self(None, batch_size, device)[0]
