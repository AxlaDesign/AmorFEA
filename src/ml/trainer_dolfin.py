import torch
from torch import optim
import numpy as np
from .trainer import Trainer, batch_mat_vec, normalize_adj
from .models import LinearRegressor, MLP_0, MLP_1, MLP_2, MixedNetwork, TensorNet
from ..pde.poisson_dolfin import PoissonDolfin
from .. import arguments
from ..graph.visualization import scalar_field_paraview


class TrainerDolfin(Trainer):

    def __init__(self, args):
        super(TrainerDolfin, self).__init__(args)
        self.poisson = PoissonDolfin(self.args)
        self.initialization()

    def loss_function(self, x_control, x_state, y_state):
        if self.args.supvervised_flag:
            return self.loss_function_supervised(x_state, y_state)
        else:
            return self.loss_function_amortized(x_control, x_state)

    def loss_function_amortized_sub(self, x_control, x_state):
        # x_control should be torch tensor with shape (batch, input_size)
        # x_state should be torch tensor with shape (batch, input_size)
        assert(x_control.shape == x_state.shape and len(x_control.shape) == 2)

        term1 = batch_mat_vec(self.A_sp, x_state)
        term1 = 0.5 * term1 * x_state

        term2 = batch_mat_vec(self.B_sp, x_state)
        term2 = 0.5 * term2 * x_state

        term3 = 0.25 * x_state**4 * self.weight_area

        term4 = batch_mat_vec(self.B_sp, x_control)
        term4 = term4 * x_state

        batch_loss = term1.sum(dim=1) + 10 * term2.sum(dim=1) + \
            10 * term3.sum(dim=1) - term4.sum(dim=1)

        return batch_loss

    def loss_function_amortized(self, x_control, x_state):
        batch_loss = self.loss_function_amortized_sub(x_control, x_state)
        return batch_loss.sum()

    def amortization_gap(self, x_control, x_state, y_state):
        loss_amortized = self.loss_function_amortized_sub(x_control, x_state)
        loss_fem = self.loss_function_amortized_sub(x_control, y_state)
        gap = loss_amortized - loss_fem
        return gap.sum()

    def normed_L2_error(self, x_control, x_state, y_state):
        error = x_state - y_state
        tmp1 = batch_mat_vec(self.B_sp, error)
        tmp1 = tmp1 * error
        L2_error = tmp1.sum(dim=1).sqrt()

        tmp2 = batch_mat_vec(self.B_sp, y_state)
        tmp2 = tmp2 * y_state
        L2_solution = tmp2.sum(dim=1).sqrt()

        normalize_error = L2_error / L2_solution

        return normalize_error.sum()

    def loss_function_supervised(self, x_state, y_state):
        diff = (x_state - y_state)
        tmp = batch_mat_vec(self.B_sp, diff)
        loss = diff * tmp
        loss = loss.sum()
        return loss

    def initialization(self):
        self.args.supvervised_flag = False
        self.data_X = np.load(self.args.root_path + '/' + self.args.numpy_path + '/' + self.poisson.name +
                              '/Gaussian-30000-' + str(self.poisson.num_dofs) + '.npy')

        self.args.load_fem_data = True
        if self.args.load_fem_data:
            self.data_Y = np.load(self.args.root_path + '/' + self.args.numpy_path + '/' + self.poisson.name +
                                  '/fem_solution.npy')
        else:
            self.FEM_evaluation_all()

        self.args.input_size = self.data_X.shape[1]
        self.train_loader, self.test_loader = self.shuffle_data()

        A_np, B_np = self.poisson.compute_operators()
        A = torch.tensor(A_np).float()
        B = torch.tensor(B_np).float()
        self.A_sp = A.to_sparse()
        self.B_sp = B.to_sparse()
        self.weight_area = torch.tensor(self.poisson.get_weight_area()).float()

        # Can be more general
        bc_flag_1 = torch.tensor(self.poisson.boundary_flags_list[0]).float()
        bc_value_1 = 1. * bc_flag_1
        bc_flag_2 = torch.tensor(self.poisson.boundary_flags_list[1]).float()
        bc_value_2 = 1. * bc_flag_2
        bc_value = bc_value_1 + bc_value_2
        interior_flag = torch.ones(
            self.poisson.num_vertices) - bc_flag_1 - bc_flag_2
        adjacency_matrix = self.poisson.get_adjacency_matrix()
        A_normalized = normalize_adj(adjacency_matrix)

        self.graph_info = [bc_value, interior_flag, A_normalized, self.B_sp]
        self.FEM_evaluation()

    def test_by_loss(self, epoch):
        self.model.eval()
        test_gap = 0
        test_loss = 0
        test_error = 0
        with torch.no_grad():
            for i, data in enumerate(self.test_loader):
                data_x = data[0].float()
                data_y = data[1].float()
                recon_batch = self.model(data_x)
                test_loss += self.loss_function(data_x,
                                                recon_batch, data_y).item()
                test_gap += self.amortization_gap(data_x,
                                                  recon_batch, data_y).item()
                test_error += self.normed_L2_error(data_x,
                                                   recon_batch, data_y).item()

        test_loss /= len(self.test_loader.dataset)
        test_gap /= len(self.test_loader.dataset)
        test_error /= len(self.test_loader.dataset)
        print('====> Epoch: {} Test set loss: {:.6f}'.format(epoch, test_loss))
        print('====> Epoch: {} Test set gap: {:.6f}'.format(epoch, test_gap))
        print('====> Epoch: {} Test set error: {:.6f}'.format(epoch, test_error))
        return test_loss

    def run(self):
        self.model = MLP_2(self.args, self.graph_info)
        self.model.load_state_dict(torch.load(self.args.root_path + '/' +
                                              self.args.model_path + '/' + self.poisson.name + '/model_mlp_2'))
        self.optimizer = optim.Adam(self.model.parameters(), lr=1e-5)

        for epoch in range(self.args.epochs):
            train_loss = self.train(epoch)
            mean_L2_error = self.test_by_FEM(epoch)
            test_loss = self.test_by_loss(epoch)
            print('\n\n')
            torch.save(self.model.state_dict(), self.args.root_path + '/' +
                       self.args.model_path + '/' + self.poisson.name + '/model_0')

    def debug(self):
        source = torch.ones(self.poisson.num_dofs).unsqueeze(0)
        solution = self.model(source)
        scalar_field_paraview(
            self.args, solution.data.numpy().flatten(), self.poisson, "debug_u")


if __name__ == "__main__":
    args = arguments.args
    trainer = TrainerDolfin(args)
    trainer.run()
