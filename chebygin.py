import numpy as np
import torch
import torch.sparse
import torch.nn as nn
import torch.nn.functional as F
from torch.nn.parameter import Parameter


class ChebyGINLayer(nn.Module):
    '''
    General Graph Neural Network layer that depending on arguments can be:
    1. Graph Convolution Layer (T. Kipf and M. Welling, ICLR 2017)
    2. Chebyshev Graph Convolution Layer (M. Defferrard et al., NeurIPS 2017)
    3. GIN Layer (K. Xu et al., ICLR 2019)
    4. ChebyGIN Layer (B. Knyazev et al., ICLR 2019 Workshop on Representation Learning on Graphs and Manifolds)
    The first three types (1-3) of layers are particular cases of the fourth (4) case.
    '''
    def __init__(self,
                 in_features,
                 out_features,
                 K,
                 n_hidden=0,
                 aggregation='mean',
                 activation=nn.ReLU(True)):
        super(ChebyGINLayer, self).__init__()
        self.in_features = in_features
        self.out_features = out_features
        assert K > 0, 'order is assumed to be > 0'
        self.K = K
        assert n_hidden >= 0, ('invalid n_hidden value', n_hidden)
        self.n_hidden = n_hidden
        assert aggregation in ['mean', 'sum'], ('invalid aggregation', aggregation)
        self.aggregation = aggregation

        n_in = self.in_features * self.K
        if self.n_hidden == 0:
            self.fc = nn.Sequential(nn.Linear(n_in, self.out_features),
                                    activation)
        else:
            self.fc = nn.Sequential(nn.Linear(n_in, n_hidden),
                                    nn.ReLU(True),
                                    nn.Linear(n_hidden, self.out_features),
                                    activation)

        # print weight norms for reproducibility analysis
        print('ChebyGINLayer', list(self.fc.children())[0].weight.shape,
              torch.norm(list(self.fc.children())[0].weight, dim=1)[:10])

    def __repr__(self):
        return 'ChebyGINLayer(in_features={}, out_features={}, K={}, n_hidden={}, aggregation={})\nfc={}'.format(
            self.in_features,
            self.out_features,
            self.K,
            self.n_hidden,
            self.aggregation,
            str(self.fc))

    def chebyshev_basis(self, L, X, K):
        '''
        Return T_k X where T_k are the Chebyshev polynomials of order up to K.
        :param L: graph Laplacian, batch (B), nodes (N), nodes (N)
        :param X: input of size batch (B), nodes (N), features (F)
        :param K: Chebyshev polynomial order, i.e. filter size (number of hopes)
        :return: Tensor of size (B,N,K,F) as a result of multiplying T_k(L) by X for each order
        '''
        if K > 1:
            Xt = [X]
            Xt.append(torch.bmm(L, X))  # B,N,F
            for k in range(2, K):
                Xt.append(2 * torch.bmm(L, Xt[k - 1]) - Xt[k - 2])  # B,N,F
            Xt = torch.stack(Xt, 2)  # B,N,K,F
            return Xt
        else:
            # GCN
            assert K == 1, K
            return torch.bmm(L, X).unsqueeze(2)  # B,N,1,F

    def laplacian_batch(self, A, add_identity=False):
        '''
        Computes normalized Laplacian transformed so that its eigenvalues are in range [-1, 1].
        Note that sum of all eigenvalues = trace(L) = 0.
        :param A: Tensor of size (B,N,N) containing batch (B) of adjacency matrices of shape N,N
        :return: Normalized Laplacian of size (B,N,N)
        '''
        batch, N = A.shape[:2]
        if add_identity:
            A = A + torch.eye(N, device=A.get_device() if A.is_cuda else 'cpu').unsqueeze(0)
        D = torch.sum(A, 1)  # nodes degree (B,N)
        D_hat = (D + 1e-5) ** (-0.5)
        L = D_hat.view(batch, N, 1) * A * D_hat.view(batch, 1, N)
        if not add_identity:
            L = -L  # for ChebyNet to make a valid Chebyshev basis
        return D, L

    def forward(self, data):
        x, A, mask = data[:3]
        B, N, F = x.shape
        assert N == A.shape[1] == A.shape[2], ('invalid shape', N, x.shape, A.shape)

        D, L = self.laplacian_batch(A, add_identity=self.K == 1)
        y = self.chebyshev_basis(L, x, self.K)  # B,N,K,F

        if self.aggregation == 'sum':
            # Sum features of neighbors
            if self.K == 1:
                # GIN
                y = y * D.view(B, N, 1, 1)
            else:
                # ChebyGIN
                D_GIN = torch.ones(B, N, self.K, device=x.get_device() if x.is_cuda else 'cpu')
                D_GIN[:, :, 1:] = D.view(B, N, 1).expand(-1, -1, self.K - 1)  # keep self-loop features the same
                y = y * D_GIN.view(B, N, self.K, 1)  # apply summation for other scales

        y = self.fc(y.view(B, N, -1))  # B,N,F

        if len(mask.shape) == 2:
            mask = mask.unsqueeze(2)

        y = y * mask

        return [y, A, mask, *data[3:]]


class GraphReadout(nn.Module):
    def __init__(self,
                 pool_type):
        super(GraphReadout, self).__init__()
        self.pool_type = pool_type
        dim = 1  # pooling over nodes
        if pool_type == 'max':
            self.readout_layer = lambda x, mask: torch.max(x, dim=dim)[0]
        elif pool_type in ['avg', 'mean']:
            # sum over all nodes, then divide by the number of valid nodes in each sample of the batch
            self.readout_layer = lambda x, mask: torch.sum(x, dim=dim) / torch.sum(mask, dim=1, keepdim=True)
        elif pool_type in ['sum']:
            self.readout_layer = lambda x, mask: torch.sum(x, dim=dim)
        else:
            raise NotImplementedError(pool_type)

    def __repr__(self):
        return 'GraphReadout({})'.format(self.pool_type)

    def forward(self, data):
        x, A, mask = data[:3]
        x = self.readout_layer(x, mask)
        return x


class AttentionPooling(nn.Module):
    def __init__(self,
                 pool_type):
        super(AttentionPooling, self).__init__()
        self.pool_type = pool_type

    def __repr__(self):
        return 'AttentionPooling({})'.format(self.pool_type)

    def forward(self, data):
        if self.pool_type[1] == 'gt':
            if 'node_attn' in data[4]:
                node_attn = data[4]['node_attn']
                # print(data[0].shape, data[2].shape, node_attn.shape)
                data[0] = data[0] * node_attn.unsqueeze(2)
                if len(node_attn.shape) < len(data[2].shape):
                    node_attn = node_attn.unsqueeze(2)
                data[2] = data[2] * (node_attn > 0).float()
            else:
                raise ValueError('ground truth node attention values node_attn required for this case')
        else:
            raise NotImplementedError('todo')
        return data


class ChebyGIN(nn.Module):
    def __init__(self,
                 in_features,
                 out_features,
                 filters,
                 K,
                 n_hidden=0,
                 aggregation='mean',
                 dropout=0,
                 readout='max',
                 pool='attn_gt_threshold_0_skip_skip'):
        super(ChebyGIN, self).__init__()

        self.pool = None if pool is None else pool.split('_')

        # Graph convolution layers
        layers = []
        for layer, f in enumerate(filters):

            if len(self.pool) > len(filters) + layer and self.pool[layer + 3] != 'skip':
                print(self.pool, layer, self.pool[layer], self.pool[layer + 3])
                layers.append(AttentionPooling(pool_type=self.pool[:3] + [self.pool[layer + 3]]))

            layers.append(ChebyGINLayer(in_features=in_features if layer == 0 else filters[layer - 1],
                                                    out_features=f,
                                                    K=K,
                                                    n_hidden=n_hidden,
                                                    aggregation=aggregation,
                                                    activation=nn.ReLU(inplace=True)))


        self.layers = nn.Sequential(*layers)
        # Global pooling over nodes
        self.readout = GraphReadout(readout)

        # Fully connected (classification/regression) layers
        fc = []
        if dropout > 0:
            fc.append(nn.Dropout(p=dropout))
        fc.append(nn.Linear(filters[-1], out_features))
        self.fc = nn.Sequential(*fc)

    def forward(self, data):
        data = self.layers(data)
        B, N, F = data[0].shape  # node features shape
        x = self.readout(data).view(B, F)  # global pooling over nodes
        x = self.fc(x)  # B,out_features
        return x, []