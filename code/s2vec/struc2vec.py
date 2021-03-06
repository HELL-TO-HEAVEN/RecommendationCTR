# -*- coding:utf-8 -*-

import os
import time
import math
import shutil
from collections import ChainMap, deque

import dgl
import torch
import numpy as np
import pandas as pd
from fastdtw import fastdtw
from gensim.models import Word2Vec
from joblib import Parallel, delayed
from tqdm import tqdm

from .utils import partition_dict, partition_list, preprocess_nxgraph


class Struc2Vec():
    def __init__(self, graph, n_users, workers=1, verbose=0, opt1_reduce_len=True, opt2_reduce_sim_calc=True, opt3_num_layers=None, temp_path='./temp_struc2vec_ng/', reuse=False):
    # def __init__(self, graph, n_users, workers=1, verbose=0, opt1_reduce_len=True, opt2_reduce_sim_calc=True, opt3_num_layers=None, temp_path='../temp_struc2vec_ng/', reuse=False):
        self.graph = graph
        self.n_users = n_users
        self.idx2node, self.node2idx = preprocess_nxgraph(graph)
        self.idx = list(range(len(self.idx2node)))

        self.opt1_reduce_len = opt1_reduce_len
        self.opt2_reduce_sim_calc = opt2_reduce_sim_calc
        self.opt3_num_layers = opt3_num_layers

        self.resue = reuse
        self.temp_path = temp_path

        if not os.path.exists(self.temp_path):
            os.mkdir(self.temp_path)
        if not reuse:
            shutil.rmtree(self.temp_path)
            os.mkdir(self.temp_path)

        if os.path.exists(self.temp_path + 'layers_adj.pkl') and os.path.exists(self.temp_path + 'layers_sim_scores.pkl'):
            print('----- reuse exist layers_adj and layers_sim_scores')
            self.layers_adj = pd.read_pickle(self.temp_path + 'layers_adj.pkl')
            self.layers_sim_scores = pd.read_pickle(self.temp_path + 'layers_sim_scores.pkl')
        else:
            self.layers_adj, self.layers_sim_scores = self.create_context_graph(self.opt3_num_layers, workers, verbose)
            pd.to_pickle(self.layers_adj, self.temp_path + 'layers_adj.pkl')
            pd.to_pickle(self.layers_sim_scores, self.temp_path + 'layers_sim_scores.pkl')

    # def get_sumed_struc_graph(self):
    #     # build dgl graph of each layer and sum the weight to one
    #     n_nodes = len(self.idx)
    #     edge_dict = {}
    #     for layer in self.layers_adj:
    #         g = dgl.DGLGraph()
    #         g.add_nodes(n_nodes)
    #         neighbors_dict = self.layers_adj[layer]
    #         layer_sim_scores = self.layers_sim_scores[layer]
    #         for v, neighbors in neighbors_dict.items():
    #             sum_score = 0.0
    #             for n in neighbors:
    #                 if (v, n) in layer_sim_scores:
    #                     sim_score = layer_sim_scores[v, n]
    #                 else:
    #                     sim_score = layer_sim_scores[n, v]
    #                 sum_score += sim_score
    #             if sum_score == 0:
    #                 continue
    #             for n in neighbors:
    #                 if (v, n) in layer_sim_scores:
    #                     normed_sim_score = layer_sim_scores[v, n] / sum_score
    #                 else:
    #                     normed_sim_score = layer_sim_scores[n, v] / sum_score
    #                 edge_dict.setdefault((n, v), 0)
    #                 edge_dict[n, v] += normed_sim_score
    #     edge_list = []
    #     edge_weight_list = []
    #     for key in edge_dict:
    #         edge_list.append(key)
    #         edge_weight_list.append(edge_dict[key] / len(self.layers_adj.keys()))
    #     edge_list = np.array(edge_list, dtype=int)
    #     g.add_edges(edge_list[:, :1].squeeze(), edge_list[:, 1:].squeeze())
    #     g.readonly()
    #     g.ndata['id'] = torch.arange(n_nodes, dtype=torch.long)
    #     g.edata['weight'] = torch.tensor(edge_weight_list).float().unsqueeze(-1)
    #     return g

    def get_pruned_struc_graph(self, layers=[-1]):
        # build dgl graph of last layer and prune the low weight
        n_nodes = len(self.idx)
        struc_graphs = []
        index_to_layer = list(range(max(self.layers_adj.keys()) + 1))
        if layers is None:
            layers = range(len(index_to_layer)) # all layers
        for index in layers:
            layer = index_to_layer[index]
            g = dgl.DGLGraph()
            g.add_nodes(n_nodes)
            edge_list = []
            edge_weight_list = []
            neighbors_dict = self.layers_adj[layer]
            layer_sim_scores = self.layers_sim_scores[layer]
            for v, neighbors in neighbors_dict.items():
                sum_score = 0.0
                for n in neighbors:
                    if (v, n) in layer_sim_scores:
                        sim_score = layer_sim_scores[v, n]
                    else:
                        sim_score = layer_sim_scores[n, v]
                    sum_score += sim_score
                mid_score = (sum_score / len(neighbors)) / 2
                if mid_score == 0:
                    continue
                # mid_score = 0 # not cut

                edge_dict_temp = {} # {(n1, n2): sim_score}
                for n in neighbors:
                    if (v, n) in layer_sim_scores:
                        sim_score = layer_sim_scores[v, n]
                    else:
                        sim_score = layer_sim_scores[n, v]
                    if sim_score > mid_score:
                        edge_list.append((n, v)) # form n to v
                        edge_weight_list.append(sim_score)
                #         edge_dict_temp[(n, v)] = sim_score
                # for pair_node in edge_dict_temp:
                #     edge_list.append(pair_node) # form n to v
                #     edge_weight_list.append(edge_dict_temp[pair_node])

            edge_list = np.array(edge_list, dtype=int)
            g.add_edges(edge_list[:, :1].squeeze(), edge_list[:, 1:].squeeze())
            g.readonly()
            g.ndata['id'] = torch.arange(n_nodes, dtype=torch.long)
            g.edata['weight'] = torch.tensor(edge_weight_list).float().unsqueeze(-1)
            # g.edata['weight'] = (g.edata['weight'] - g.edata['weight'].mean()) / torch.sqrt(g.edata['weight'].var() + 1e-10) * 0.075 + 0.7
            g.ndata['out_sqrt_degree'] = 1 / torch.sqrt(g.out_degrees().float().unsqueeze(-1))
            g.ndata['in_sqrt_degree'] = 1 / torch.sqrt(g.in_degrees().float().unsqueeze(-1))

            g.ndata['out_sqrt_degree'][torch.isinf(g.ndata['out_sqrt_degree'])] = 0
            g.ndata['in_sqrt_degree'][torch.isinf(g.ndata['in_sqrt_degree'])] = 0
            assert torch.sum(torch.isinf(g.ndata['out_sqrt_degree'])) == 0
            assert torch.sum(torch.isinf(g.ndata['in_sqrt_degree'])) == 0
            struc_graphs.append(g)
        return struc_graphs

    def create_context_graph(self, max_num_layers, workers=1, verbose=0,):
        print(str(time.asctime(time.localtime(time.time()))) + ' create_context_graph')
        pair_distances = self._compute_structural_distance(max_num_layers, workers, verbose)
        layers_adj, layers_sim_scores = self._get_layer_rep(pair_distances)
        return layers_adj, layers_sim_scores

    def _compute_structural_distance(self, max_num_layers, workers=1, verbose=0,):
        print(str(time.asctime(time.localtime(time.time()))) + ' _compute_structural_distance')

        if os.path.exists(self.temp_path+'structural_dist.pkl'):
            structural_dist = pd.read_pickle(
                self.temp_path+'structural_dist.pkl')
        else:
            if self.opt1_reduce_len:
                dist_func = cost_max
            else:
                dist_func = cost

            if os.path.exists(self.temp_path + 'degreelist.pkl'):
                print('----- read degreelist')
                degreeList = pd.read_pickle(self.temp_path + 'degreelist.pkl')
            else:
                print('----- train degreelist')
                degreeList = self._compute_ordered_degreelist(max_num_layers, workers, verbose)
                pd.to_pickle(degreeList, self.temp_path + 'degreelist.pkl')

            if self.opt2_reduce_sim_calc:
                print('start len_nbs_list')

                # degrees = self._create_vectors()
                # vertices = {}
                # len_nbs_list = []
                # for v in tqdm(self.idx): # list of vertex
                #     nbs = get_vertices(
                #         v, len(self.graph[self.idx2node[v]]), degrees)
                #     vertices[v] = nbs  # store nbs
                #     len_nbs_list.append(len(nbs))

                user_degrees, user_upper_boundary = self._create_vectors(self.idx[:self.n_users])
                item_degrees, item_upper_boundary = self._create_vectors(self.idx[self.n_users:])
                # print(user_upper_boundary, item_upper_boundary)
                user_upper_nums = self._get_upper_nums(degreeList, user_upper_boundary, self.idx[:self.n_users])
                item_upper_nums = self._get_upper_nums(degreeList, item_upper_boundary, self.idx[self.n_users:])
                nb_sets = self.get_nb_sets()

                vertices = {}
                len_nbs_list = []
                for v in tqdm(self.idx[:self.n_users]): # list of user
                    nbs = get_vertices(v, len(self.graph[self.idx2node[v]]), user_degrees, user_upper_nums, nb_sets)
                    vertices[v] = nbs  # store nbs
                    len_nbs_list.append(len(nbs))
                for v in tqdm(self.idx[self.n_users:]): # list of item
                    nbs = get_vertices(v, len(self.graph[self.idx2node[v]]), item_degrees, item_upper_nums, nb_sets)
                    vertices[v] = nbs  # store nbs
                    len_nbs_list.append(len(nbs))
                print('mean len nbs 1:', np.mean(len_nbs_list).item())

                # for v in tqdm(self.idx):
                #     if v not in vertices:
                #         break
                #     vertices[v] = [nb for nb in vertices[v] if nb > v]
                # len_nbs_list = []
                # for v in vertices:
                #     len_nbs_list.append(len(vertices[v]))
                # print('mean len nbs 2:', np.mean(len_nbs_list).item())
                # exit(0) # len_nbs_list
            else:
                vertices = {}
                for v in degreeList:
                    vertices[v] = [vd for vd in degreeList.keys() if vd > v]

            print(str(time.asctime(time.localtime(time.time()))) + ' compute_dtw_dist')
            workers_limit = min(2, workers) # 16GB RAM only support 2 workers
            results = Parallel(n_jobs=workers_limit, verbose=verbose,)(
                delayed(compute_dtw_dist)(
                    part_list, degreeList, dist_func, job_id + 1) for job_id, part_list in enumerate(
                        partition_dict(vertices, workers_limit)))
            dtw_dist = dict(ChainMap(*results))

            structural_dist = convert_dtw_struc_dist(dtw_dist)
            pd.to_pickle(structural_dist, self.temp_path + 'structural_dist.pkl')

        return structural_dist

    def _compute_ordered_degreelist(self, max_num_layers, workers=1, verbose=0):
        print(str(time.asctime(time.localtime(time.time()))) + ' _compute_ordered_degreelist')

        degreeList = {}
        vertices = self.idx  # self.g.nodes()
        # for v in vertices:
        #     degreeList[v] = self._get_order_degreelist_node(v, max_num_layers)
        results = Parallel(n_jobs=workers, verbose=verbose,)(
            delayed(self._get_order_degreelist_node_parallel)(
                part_list, job_id + 1, max_num_layers) for job_id, part_list in enumerate(
                    partition_list(vertices, workers, shuffle=True)))
        degreeList = dict(ChainMap(*results))
        return degreeList

    def _get_order_degreelist_node_parallel(self, part_list, job_id, max_num_layers=None):
        part_degreeList = {}
        time_start = time.time()
        for i, (_, v) in enumerate(part_list):
            part_degreeList[v] = self._get_order_degreelist_node(v, max_num_layers)
            if (i+1) % 100 == 0:
                print('GODNP job_id: ' + str(job_id) + '; finish: ' + str(i+1) + '/' + str(len(part_list)) + '; time spend: ' + str(time.time() - time_start))
                time_start = time.time()
        return part_degreeList

    def _get_order_degreelist_node(self, root, max_num_layers=None):
        if max_num_layers is None:
            max_num_layers = float('inf') # max number in python

        ordered_degree_sequence_dict = {}
        visited = [False] * len(self.graph.nodes())
        queue = deque()
        level = 0
        queue.append(root)
        visited[root] = True

        while (len(queue) > 0 and level <= max_num_layers):
            count = len(queue)
            if self.opt1_reduce_len:
                degree_list = {}
            else:
                degree_list = []
            while (count > 0):
                top = queue.popleft()
                node = self.idx2node[top]
                degree = len(self.graph[node])

                if self.opt1_reduce_len:
                    degree_list[degree] = degree_list.get(degree, 0) + 1
                else:
                    degree_list.append(degree)

                for nei in self.graph[node]:
                    nei_idx = self.node2idx[nei]
                    if not visited[nei_idx]:
                        visited[nei_idx] = True
                        queue.append(nei_idx)
                count -= 1
            if self.opt1_reduce_len:
                orderd_degree_list = [(degree, freq)
                                      for degree, freq in degree_list.items()]
                orderd_degree_list.sort(key=lambda x: x[0])
            else:
                orderd_degree_list = sorted(degree_list)
            ordered_degree_sequence_dict[level] = orderd_degree_list
            level += 1

        return ordered_degree_sequence_dict

    def _create_vectors(self, part_idx=None):
        if part_idx is None:
            part_idx = self.idx
        print(str(time.asctime(time.localtime(time.time()))) + ' _create_vectors')
        degrees = {}  # sotre v list of degree
        degrees_sorted = set()  # store degree
        degree_list = []
        G = self.graph
        for v in part_idx:
            degree = len(G[self.idx2node[v]])
            degrees_sorted.add(degree)
            degree_list.append(degree)
            if (degree not in degrees):
                degrees[degree] = {}
                degrees[degree]['vertices'] = []
            degrees[degree]['vertices'].append(v)
        degrees_sorted = np.array(list(degrees_sorted), dtype='int')
        degrees_sorted = np.sort(degrees_sorted)

        l = len(degrees_sorted)
        for index, degree in enumerate(degrees_sorted):
            if (index > 0):
                degrees[degree]['before'] = degrees_sorted[index - 1]
            if (index < (l - 1)):
                degrees[degree]['after'] = degrees_sorted[index + 1]

        degree_list = np.sort(np.array(degree_list, dtype='int'))
        upper_boundary = degree_list[int(len(degree_list) * 0.9)].item()
        return degrees, upper_boundary

    def _get_upper_nums(self, degree_list, upper_boundary, part_idx=None):
        assert self.opt1_reduce_len
        if part_idx is None:
            part_idx = self.idx
        upper_nums = {}
        for v in part_idx:
            temp_num = 0
            reorderd_degree_list = degree_list[v][1][::-1]
            for degree, times in reorderd_degree_list:
                if degree >= upper_boundary:
                    temp_num += times
                else:
                    break
            upper_nums[v] = temp_num
        return upper_nums

    def get_nb_sets(self):
        nb_sets = {}
        for i in self.idx:
            nbs_i = set()
            for nb in self.graph[self.idx2node[i]]:
                nbs_i.add(self.node2idx[nb])
            nb_sets[i] = nbs_i
        return nb_sets

    def _get_layer_rep(self, pair_distances):
        print(str(time.asctime(time.localtime(time.time()))) + ' _get_layer_rep')
        layers_sim_scores = {}
        layers_adj = {}
        for v_pair, layer_dist in pair_distances.items():
            for layer, distance in layer_dist.items():
                vx = v_pair[0]
                vy = v_pair[1]

                layers_sim_scores.setdefault(layer, {})
                # layers_sim_scores[layer][vx, vy] = np.exp(-float(distance))
                layers_sim_scores[layer][vx, vy] = np.exp(-float(distance) / 2)

                layers_adj.setdefault(layer, {})
                layers_adj[layer].setdefault(vx, [])
                layers_adj[layer].setdefault(vy, [])
                layers_adj[layer][vx].append(vy)
                layers_adj[layer][vy].append(vx)

        return layers_adj, layers_sim_scores


def get_vertices(v, degree_v, degrees, upper_nums, nb_sets):
    def has_same_neighbor(a, b):
        nbs_b = nb_sets[b]
        for na in nb_sets[a]:
            if na in nbs_b:
                return True
        return False

    def choice_degree(n, a, b):
        if(b == -1):
            degree_now = a
        elif(a == -1):
            degree_now = b
        elif((n-b)/b < (a-n)/n):
        # elif(abs(b - n) < abs(a - n)):
            degree_now = b
        else:
            degree_now = a
        return degree_now

    vertices = []

    for v2 in degrees[degree_v]['vertices']:
        if (v < v2):
            uv = upper_nums[v]
            uv2 = upper_nums[v2]
            if max(uv, uv2) < min(uv, uv2) * 2:
                if has_same_neighbor(v, v2):
                    vertices.append(v2)  # same degree
    if ('before' not in degrees[degree_v]):
        degree_b = -1
    else:
        degree_b = degrees[degree_v]['before']
    if ('after' not in degrees[degree_v]):
        degree_a = -1
    else:
        degree_a = degrees[degree_v]['after']
    assert not (degree_b == -1 and degree_a == -1)
    degree_now = choice_degree(degree_v, degree_a, degree_b)

    while max(degree_now, degree_v) <= 1.5 * min(degree_now, degree_v):
        for v2 in degrees[degree_now]['vertices']:
            if (v < v2):
                uv = upper_nums[v]
                uv2 = upper_nums[v2]
                if max(uv, uv2) < min(uv, uv2) * 2:
                    if has_same_neighbor(v, v2):
                        vertices.append(v2)  # same degree
        if (degree_now == degree_b):
            if ('before' not in degrees[degree_b]):
                degree_b = -1
            else:
                degree_b = degrees[degree_b]['before']
        else:
            if ('after' not in degrees[degree_a]):
                degree_a = -1
            else:
                degree_a = degrees[degree_a]['after']
        if (degree_b == -1 and degree_a == -1):
            break  # not anymore v
        degree_now = choice_degree(degree_v, degree_a, degree_b)
    return vertices


def cost(a, b):
    ep = 0.5
    m = max(a, b) + ep
    mi = min(a, b) + ep
    return ((m / mi) - 1)


def cost_min(a, b):
    ep = 0.5
    m = max(a[0], b[0]) + ep
    mi = min(a[0], b[0]) + ep
    return ((m / mi) - 1) * min(a[1], b[1])


def cost_max(a, b):
    ep = 0.5
    m = max(a[0], b[0]) + ep
    mi = min(a[0], b[0]) + ep
    return ((m / mi) - 1) * max(a[1], b[1])


def convert_dtw_struc_dist(distances, startLayer=1):
    """

    :param distances: dict of dict
    :param startLayer:
    :return:
    """
    for vertices, layers in distances.items():
        keys_layers = sorted(layers.keys())
        startLayer = min(len(keys_layers), startLayer)
        for layer in range(0, startLayer):
            keys_layers.pop(0)

        for layer in keys_layers:
            layers[layer] += layers[layer - 1] # accumulate the distance
    return distances


def compute_dtw_dist(part_list, degreeList, dist_func, job_id):
    dtw_dist = {}
    time_start = time.time()
    for i, (v1, nbs) in enumerate(part_list):
        lists_v1 = degreeList[v1]  # lists_v1 :orderd degree list of v1
        for v2 in nbs:
            lists_v2 = degreeList[v2]  # lists_v1 :orderd degree list of v2
            max_layer = min(len(lists_v1), len(lists_v2))  # valid layer
            dtw_dist[v1, v2] = {}
            for layer in range(0, max_layer):
                dist, path = fastdtw(
                    lists_v1[layer], lists_v2[layer], radius=1, dist=dist_func)
                dtw_dist[v1, v2][layer] = dist
        if (i+1) % 100 == 0:
            print('CDD job_id: ' + str(job_id) + '; finish: ' + str(i+1) + '/' + str(len(part_list)) + '; time spend: ' + str(time.time() - time_start))
            time_start = time.time()
    return dtw_dist
