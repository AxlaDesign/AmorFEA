'''Train the neural network to be a fast PDE solver.
Physical laws have been built into the loss
'''

import torch
from torch import optim
from torch.utils.data import Dataset, DataLoader, TensorDataset
import numpy as np
import os
import collections
from .trainer import Trainer, batch_mat_vec, normalize_adj
from .models import LinearRegressor, MLP, MixedNetwork, TensorNet
from ..pde.poisson_dolfin import PoissonDolfin
from .. import arguments
from ..graph.visualization import scalar_field_paraview


class TrainerDolfin(Trainer):
    def __init__(self, args):
        super(TrainerDolfin, self).__init__(args)
        self.poisson = PoissonDolfin(self.args)
        self.initialization()


    def loss_function(self, x_control, x_state):
        # loss function is defined so that PDE is satisfied

        # x_control should be torch tensor with shape (batch, input_size)
        # x_state should be torch tensor with shape (batch, input_size)
        assert(x_control.shape == x_state.shape and len(x_control.shape) == 2)
     
        term1 = batch_mat_vec(self.A_sp, x_state)
        term1 = 0.5*term1*x_state
        
        term2 = 0.5*x_state**2*self.weight_area
        term3 = 0.25*x_state**4*self.weight_area
        
        term4 = batch_mat_vec(self.B_sp, x_control)
        term4 = term4*x_state

        loss = term1.sum() + 10*term2.sum() + 10*term3.sum() - term4.sum()
        return loss


    def initialization(self):

        # Subject to change. Raw data generated by some distribution
        self.data_X = np.load(self.args.root_path + '/' + self.args.numpy_path + '/' + self.poisson.name +  
                              '/Gaussian-30000-' + str(self.poisson.num_dofs) + '.npy')
        # self.data_X = np.ones_like(self.data_X)
        self.args.input_size = self.data_X.shape[1]
        self.train_loader, self.test_loader = self.shuffle_data()

        A_np, B_np = self.poisson.compute_operators()
        A = torch.tensor(A_np).float()
        B = torch.tensor(B_np).float()
        self.A_sp = A.to_sparse()
        self.B_sp = B.to_sparse()

        self.weight_area = torch.tensor(self.poisson.get_weight_area()).float()

        # Can be much more general
        bc_flag_1 = torch.tensor(self.poisson.boundary_flags_list[0]).float()
        bc_value_1 = 1.*bc_flag_1
        bc_flag_2 = torch.tensor(self.poisson.boundary_flags_list[1]).float()
        bc_value_2 = 1.*bc_flag_2
        bc_value = bc_value_1 + bc_value_2
        interior_flag = torch.ones(self.poisson.num_vertices) - bc_flag_1 - bc_flag_2
        adjacency_matrix = self.poisson.get_adjacency_matrix()
        A_normalized = normalize_adj(adjacency_matrix)

        self.graph_info = [bc_value, interior_flag, A_normalized, self.B_sp]

        self.FEM_evaluation()  


    def run(self):
        self.model = MLP(self.args, self.graph_info)
        # self.model.load_state_dict(torch.load(self.args.root_path + '/' + 
        #                                       self.args.model_path + '/' + self.poisson.name + '/model_mlp_2'))

        self.optimizer = optim.Adam(self.model.parameters(), lr=1e-4)
        # self.optimizer = optim.LBFGS(self.model.parameters(), lr=1e-2, max_iter=20, history_size=40)
        # self.optimizer = optim.SGD(self.model.parameters(), lr=1e-6, momentum=0.85)

        for epoch in range(self.args.epochs):
            train_loss = self.train(epoch)
            test_loss = self.test_by_loss(epoch)
            mean_L2_error = self.test_by_FEM(epoch)
            print('\n\n')

            # if mean_L2_error < 1e-4:
            #     self.debug()
            #     exit()

            torch.save(self.model.state_dict(), self.args.root_path + '/' +
                       self.args.model_path + '/' + self.poisson.name + '/model_' + str(0))

    def debug(self):
        source = torch.ones(self.poisson.num_dofs).unsqueeze(0)
        solution = self.model(source)
        scalar_field_paraview(self.args, solution.data.numpy().flatten(), self.poisson, "debug_u")


if __name__ == "__main__":
    args = arguments.args
    trainer = TrainerDolfin(args)
    trainer.run()