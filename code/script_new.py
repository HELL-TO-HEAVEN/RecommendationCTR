import time
import pickle
import logging

import dgl
import tqdm
import torch
from torch.utils.data import DataLoader
import numpy as np

from cf_dataset import DataOnlyCF
from gcn_model import CFGCN
from metrics import precision_and_recall, ndcg, auc

CODE_VERSION = '0721-1655'
USE_PRETRAIN = True
RETRAIN_PRETRAIN = False
PRETRAIN_VERSION = 'lr0005_1e4_500epoch'
# PRETRAIN_VERSION = 'LightGCN_Pretrain'
PRETRAIN_EPOCH = 500
GCN_EPOCH = 2
STRUC_STEP = 30
ITRA_STEP = 30
LR = 0.001
EDIM = 64
LAYERS = 3
LAM = 1e-4
TOPK = 20
M3LAYERS = [-1] # build_struc_graphs mode3_layers (layers of prune graph)
BMODE = 3 # build_struc_graphs mode (3 for prune)
CMODE = 0 # combine_multi_graph_embedding mode (1 for concat)
ATYPE = 'graphsage' # gcn graphsage bi-interaction
WFUSE = False # whether use diff weight to fuse(get mean) each step embedding of GCN

# GPU / CPU
device = torch.device("cuda" if torch.cuda.is_available() else "cpu")

# register logging logger
logger = logging.getLogger()
logger.setLevel(level=logging.INFO)
time_line = time.strftime('%Y%m%d_%H:%M', time.localtime(time.time()))
logfile = time_line + '_snew.log'
print('logfile', logfile)
formatter = logging.Formatter('%(asctime)s - %(message)s', datefmt='%d%b %H:%M')
console_h = logging.StreamHandler()
console_h.setLevel(logging.INFO)
console_h.setFormatter(formatter)
logger.addHandler(console_h)
if device == 'cuda':
    logfile_h = logging.FileHandler(logfile, mode='w')
    logfile_h.setLevel(logging.INFO)
    logfile_h.setFormatter(formatter)
    logger.addHandler(logfile_h)

def train(model, data_loader, optimizer, use_dummy_gcn=False, use_struc=None):
    model.train()
    total_loss = 0
    for i, (user_ids, pos_ids, neg_ids) in enumerate(tqdm.tqdm(data_loader)):
    # for i, (user_ids, pos_ids, neg_ids) in enumerate(data_loader):
        user_ids = user_ids.to(device)
        pos_ids = pos_ids.to(device)
        neg_ids = neg_ids.to(device)
        loss = model.bpr_loss(user_ids, pos_ids, neg_ids, use_dummy_gcn, use_struc)
        # logging.info('train loss ' + str(i) + '/' + str(len(data_loader)) + ': ' + str(loss))
        model.zero_grad()
        loss.backward()
        optimizer.step()
        total_loss += loss.cpu().item()
    logging.info('train loss:' + str(total_loss / len(data_loader)))

def evaluate(model, data_loader, use_dummy_gcn=False, use_struc=None):
    with torch.no_grad():
        # logging.info('----- start_evaluate -----')
        model.eval()
        total_loss = 0
        # for i, (user_ids, pos_ids, neg_ids) in enumerate(tqdm.tqdm(data_loader)):
        for i, (user_ids, pos_ids, neg_ids) in enumerate(data_loader):
            user_ids = user_ids.to(device)
            pos_ids = pos_ids.to(device)
            neg_ids = neg_ids.to(device)
            loss = model.bpr_loss(user_ids, pos_ids, neg_ids, use_dummy_gcn, use_struc)
            total_loss += loss.cpu().item()
        avg_loss = total_loss / len(data_loader)
        logging.info('evaluate loss:' + str(avg_loss))

def test(data_set, model, data_loader, show_auc = False, use_dummy_gcn=False, use_struc=None):
    with torch.no_grad():
        logging.info('----- start_test -----')
        model.eval()
        precision = []
        recall = []
        ndcg_score = []
        auc_score = []
        for user_ids, _, __ in data_loader:
            user_ids = user_ids.to(device)
            ratings = model.get_users_ratings(user_ids, use_dummy_gcn, use_struc)
            ground_truths = []
            for i, user_id_t in enumerate(user_ids):
                user_id = user_id_t.item()
                ground_truths.append(data_set.test_user_dict[user_id])
                train_pos = data_set.train_user_dict[user_id]
                for pos_item in train_pos:
                    ratings[i][pos_item] = -1 # delete train data in ratings
            # Precision, Recall, NDCG
            ___, index_k = torch.topk(ratings, k=TOPK) # index_k.shape = (batch_size, TOPK), dtype=torch.int
            batch_predict_items = index_k.cpu().tolist()
            batch_precision, batch_recall = precision_and_recall(batch_predict_items, ground_truths)
            batch_ndcg = ndcg(batch_predict_items, ground_truths)
            # AUC
            if show_auc:
                ratings = ratings.cpu().numpy()
                batch_auc = auc(ratings, data_set.get_item_num(), ground_truths)
                auc_score.append(batch_auc)

            precision.append(batch_precision)
            recall.append(batch_recall)
            ndcg_score.append(batch_ndcg)
        precision = np.mean(precision)
        recall = np.mean(recall)
        ndcg_score = np.mean(ndcg_score)
        if show_auc: # Calculate AUC scores spends a long time
            auc_score = np.mean(auc_score)
            logging.info('test result: precision ' + str(precision) + '; recall ' + str(recall) + '; ndcg ' + str(ndcg_score) + '; auc ' + str(auc_score))
        else:
            logging.info('test result: precision ' + str(precision) + '; recall ' + str(recall) + '; ndcg ' + str(ndcg_score))


if __name__ == "__main__":
    print('CODE_VERSION: ' + CODE_VERSION)
    logging.info(str(time.asctime(time.localtime(time.time()))))
    data_set = DataOnlyCF('data_for_test/gowalla/train.txt', 'data_for_test/gowalla/test.txt')
    itra_G = data_set.get_interaction_graph()
    # print('itra_G: nodes', itra_G.number_of_nodes(), ',edges', itra_G.number_of_edges(), ',degree mean&var', itra_G.out_degrees().float().mean(), itra_G.out_degrees().float().var())
    # import matplotlib.pyplot as plt
    # n_users = data_set.get_user_num()
    # deu = itra_G.out_degrees().reshape(-1).cpu().numpy()[:n_users]
    # dei = itra_G.out_degrees().reshape(-1).cpu().numpy()[n_users:]
    # print(deu.max())
    # plt.hist(x = dei, range=(0, 99), bins=100, color='steelblue', edgecolor='black')
    # plt.show()
    # exit(0)

    # move graph data to target device
    itra_G.ndata['id'] = itra_G.ndata['id'].to(device)
    itra_G.ndata['sqrt_degree'] = itra_G.ndata['sqrt_degree'].to(device)
    t1 = time.time()
    struc_Gs = data_set.build_struc_graphs(mode=BMODE, mode3_layers=M3LAYERS)
    print('build_struc_graphs time:', time.time() - t1)
    for g in struc_Gs:
        g.ndata['id'] = g.ndata['id'].to(device)
        g.edata['weight'] = g.edata['weight'].to(device)
        if 'out_sqrt_degree' in g.ndata and 'in_sqrt_degree' in g.ndata:
            g.ndata['out_sqrt_degree'] = g.ndata['out_sqrt_degree'].to(device)
            g.ndata['in_sqrt_degree'] = g.ndata['in_sqrt_degree'].to(device)
        else:
            assert False # only use pruned_struc_graph
    # struc_Gs = None

    # for struc_G in struc_Gs:
    #     print()
    #     ma = torch.max(struc_G.edata['weight']).cpu().item()
    #     mi = torch.min(struc_G.edata['weight']).cpu().item()
    #     me = torch.mean(struc_G.edata['weight']).cpu().item()
    #     v = torch.var(struc_G.edata['weight']).cpu().item()
    #     print(f'weight max: {ma}, min: {mi}, mean: {me}, var: {v}, edge_nums: {struc_G.number_of_edges()}')
    #     print('struc_G: nodes', struc_G.number_of_nodes(), ',edges', struc_G.number_of_edges(), ',degree mean&var&max', struc_G.out_degrees().float().mean(), struc_G.out_degrees().float().var(), struc_G.out_degrees().max())
    #     print('struc_G: nodes', struc_G.number_of_nodes(), ',edges', struc_G.number_of_edges(), ',degree mean&var', struc_G.in_degrees().float().mean(), struc_G.in_degrees().float().var())
    #     import matplotlib.pyplot as plt
    #     plt.hist(x = g.out_degrees().reshape(-1).cpu().numpy(), range=(0, 199), bins=100, color='steelblue', edgecolor='black')
    #     # plt.hist(x = g.edata['weight'].reshape(-1).cpu().numpy(), bins=100, color='steelblue', edgecolor='black')
    #     plt.show()
    # exit(0)

    n_users = data_set.get_user_num()
    n_items = data_set.get_item_num()
    model = CFGCN(n_users, n_items, itra_G, struc_Gs=struc_Gs, embed_dim=EDIM, n_layers=LAYERS,
                  lam=LAM, weighted_fuse=WFUSE, combine_mode=CMODE, aggregator_type=ATYPE).to(device)
    train_data_loader = DataLoader(data_set, batch_size=2048, shuffle=True, num_workers=2)
    evaluate_data_loader = DataLoader(data_set.get_evaluate_dataset(), batch_size=4096, num_workers=2)
    test_data_loader = DataLoader(data_set.get_test_dataset(), batch_size=4096 * 8, num_workers=2)
    optimizer = torch.optim.Adam(params=model.parameters(), lr=LR)

    # pretrain mf model
    if USE_PRETRAIN:
        logging.info('load pretrain model, pretrain_version: ' + PRETRAIN_VERSION)
        pretrained_data, saved_args = torch.load(PRETRAIN_VERSION + '.pth', device)
        assert (PRETRAIN_EPOCH, EDIM, CMODE) == saved_args, 'saved_args not match' + str(saved_args)
        model.load_pretrained_embedding(pretrained_data)
    elif RETRAIN_PRETRAIN:
        for epoch_i in range(PRETRAIN_EPOCH):
            logging.info('Pretrain mf - epoch ' + str(epoch_i + 1) + '/' + str(PRETRAIN_EPOCH))
            train(model, train_data_loader, optimizer, use_dummy_gcn=True)
            evaluate(model, evaluate_data_loader, use_dummy_gcn=True)
            if (epoch_i + 1) % 10 == 0:
                test(data_set, model, test_data_loader, use_dummy_gcn=True)
            logging.info('--------------------------------------------------')
        dump_obj = (model.get_pretrained_embedding(), (PRETRAIN_EPOCH, EDIM, CMODE))
        torch.save(dump_obj, CODE_VERSION + '.pth')
        logging.info('==================================================')

    # train gcn
    # test(data_set, model, test_data_loader, use_dummy_gcn=True)
    test(data_set, model, test_data_loader, use_dummy_gcn=False)
    logging.info('==================================================')
    for i in range(GCN_EPOCH):

        for epoch_i in range(STRUC_STEP):
            logging.info('----- use_struc=True')
            logging.info('Train lgcn - epoch ' + str(i * (STRUC_STEP + ITRA_STEP) + epoch_i + 1) + '/' + str(GCN_EPOCH * (STRUC_STEP + ITRA_STEP)))
            train(model, train_data_loader, optimizer, use_dummy_gcn=False, use_struc=True)
            evaluate(model, evaluate_data_loader, use_dummy_gcn=False, use_struc=True)
            if (epoch_i + 1) % 2 == 0:
                test(data_set, model, test_data_loader, use_dummy_gcn=False, use_struc=True)
            logging.info('--------------------------------------------------')

        for epoch_i in range(ITRA_STEP):
            logging.info('----- use_struc=False')
            logging.info('Train lgcn - epoch ' + str(i * (STRUC_STEP + ITRA_STEP) + STRUC_STEP + epoch_i + 1) + '/' + str(GCN_EPOCH * (STRUC_STEP + ITRA_STEP)))
            train(model, train_data_loader, optimizer, use_dummy_gcn=False, use_struc=False)
            evaluate(model, evaluate_data_loader, use_dummy_gcn=False, use_struc=False)
            if (epoch_i + 1) % 10 == 0:
                test(data_set, model, test_data_loader, use_dummy_gcn=False, use_struc=False)
            logging.info('--------------------------------------------------')

    logging.info('==================================================')
    test(data_set, model, test_data_loader, use_dummy_gcn=False, use_struc=False)

# run data_lgcn/gowalla gowalla
# at epoch 50 precision 0.0406273132632997; recall 0.13624640704870125; ndcg 0.11335605664660738
# at epoch 100 precision 0.043916843150558604; recall 0.1494013824889015; ndcg 0.12420528799591576
# at epoch 200 precision 0.04701247577120442; recall 0.16131859933809437; ndcg 0.13461157598002454
# at epoch 300 precision 0.04827474044105054; recall 0.1658744830844509; ndcg 0.13881823365718912
# at epoch 400 precision 0.04889910796038417; recall 0.16776382478552415; ndcg 0.14091457113502115
# at epoch 500 precision 0.04972081819634011; recall 0.17046915726601; ndcg 0.14296358168788104
# epoch 300 (old)
# train loss 0.015; evaluate loss 0.134
# test result: precision 0.047575934218717066; recall 0.16351703048292573; ndcg 0.13673274095554458
# max
# precision 0.051 recall 0.174; ndcg 0.147

# Paper code at epoch 50 gowalla
# {'precision': array([0.04382075]), 'recall': array([0.14503336]), 'ndcg': array([0.12077126]), 'auc': 0.9587075653077938}
# Paper code at epoch 80 gowalla
# {'precision': array([0.0468166]), 'recall': array([0.15585551]), 'ndcg': array([0.13010746]), 'auc': 0.9582199598920466}
# Paper code at epoch 400 gowalla
# {'precision': array([0.05400898]), 'recall': array([0.17730673]), 'ndcg': array([0.15099276]), 'auc': 0.9508640011701547}
# max in paper
# precision 0.055 recall 0.182; ndcg 0.154
