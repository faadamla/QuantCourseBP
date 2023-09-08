from __future__ import annotations
from abc import ABC
from src.model import *
import numpy as np
from src.contract import Contract, EuropeanContract
import scipy

class NumericalMethod(ABC):
    @abstractmethod
    def __init__(self, contract: Contract, model: MarketModel, params: MCParams | PDEParams | TreeParams):
        self._contract = contract
        self._model = model
        self._params = params

    @staticmethod
    def get_numerical_methods() -> dict[str, NumericalMethod]:
        return {cls.__name__: cls for cls in NumericalMethod.__subclasses__()}


class MCMethod(NumericalMethod):
    def __init__(self, contract: Contract, model: MarketModel, params: MCParams):
        if not isinstance(params, MCParams):
            raise TypeError(f'Params must be of type MCParams but received {type(params).__name__}')
        super().__init__(contract, model, params)

    def find_simulation_tenors(self) -> list[float]:
        if self._params.tenor_frequency == 0:
            model_tenors = [.0]
        else:
            final_tenor = max(self._contract.get_timeline())
            dt = 1 / self._params.tenor_frequency
            num_of_tenors = int(final_tenor / dt)
            model_tenors = [i * dt for i in range(num_of_tenors)]
        all_simul_tenors = sorted(set(model_tenors + self._contract.get_timeline()))
        return all_simul_tenors

    def generate_std_norm(self, num_of_tenors: int) -> np.ndarray:
        np.random.seed(self._params.seed)
        if self._params.antithetic:
            rnd1 = np.random.standard_normal(size=(int(self._params.num_of_paths / 2), num_of_tenors))
            rnd2 = -rnd1
            rnd = np.concatenate((rnd1, rnd2), axis=0)
            if self._params.num_of_paths % 2 == 1:
                zeros = np.zeros((1, num_of_tenors))
                rnd = np.concatenate((rnd, zeros), axis=0)
        else:
            rnd = np.random.standard_normal(size=(self._params.num_of_paths, num_of_tenors))
        if self._params.standardize:
            mean = np.mean(rnd)
            std = np.std(rnd, ddof=1)
            rnd = (rnd - mean) / std
        return rnd

    @abstractmethod
    def simulate_spot_paths(self) -> np.ndarray:
        pass


class MCMethodFlatVol(MCMethod):
    def __int__(self, contract: Contract, model: FlatVolModel, params: MCParams):
        super().__init__(contract, model, params)

    def simulate_spot_paths(self) -> np.ndarray:
        model = self._model
        contract_tenors = self._contract.get_timeline()
        simulation_tenors = self.find_simulation_tenors()
        num_of_tenors = len(simulation_tenors)
        num_of_paths = self._params.num_of_paths
        rnd_num = self.generate_std_norm(num_of_tenors)
        spot_paths = np.empty(shape=(num_of_paths, num_of_tenors))
        spot = model.spot
        vol = model.get_vol(self._contract.strike, self._contract.expiry)
        for path in range(num_of_paths):
            spot_paths[path, 0] = spot
            for t_idx in range(1, num_of_tenors):
                t_from = simulation_tenors[t_idx - 1]
                t_to = simulation_tenors[t_idx]
                spot_from = spot_paths[path, t_idx - 1]
                z = rnd_num[path, t_idx]
                spot_paths[path, t_idx] = model.evolve_simulated_spot(vol, t_from, t_to, spot_from, z)
        contract_tenor_idx = [idx for idx in range(num_of_tenors) if simulation_tenors[idx] in contract_tenors]
        return spot_paths[:, contract_tenor_idx]


class PDEMethod(NumericalMethod):
    def __init__(self, contract: Contract, model: MarketModel, params: Params):
        if not isinstance(params, PDEParams):
            raise TypeError(f'Params must be of type PDEParams but received {type(params).__name__}')
        super().__init__(contract, model, params)


class SimpleBinomialTree(NumericalMethod):
    def __init__(self, contract: Contract, model: MarketModel, params: Params):
        if not isinstance(params, TreeParams):
            raise TypeError(f'Params must be of type TreeParams but received {type(params).__name__}')
        super().__init__(contract, model, params)
        self._spot_tree_built = False
        self._df_computed = False
        self._prob_computed = False

    def init_tree(self):
        self.build_spot_tree()
        self.compute_df()
        self.compute_prob()

    def build_spot_tree(self):
        if self._spot_tree_built:
            pass
        self._down_log_step = np.log(self._params.down_step_mult)
        self._up_log_step = np.log(self._params.up_step_mult)
        tree = []
        log_spot = np.log(self._model.spot)
        previous_level = [log_spot]
        tree += [previous_level]
        for _ in range(self._params.nr_steps):
            new_level = [s + self._down_log_step for s in previous_level]
            new_level += [previous_level[-1] + self._up_log_step]
            tree += [new_level]
            previous_level = new_level
        self._spot_tree = tree
        self._spot_tree_built = True

    def compute_df(self):
        if self._df_computed:
            pass
        delta_t = self._contract.expiry / self._params.nr_steps
        df_1_step = self._model.calc_df(delta_t)
        self._df = [df_1_step ** k for k in range(self._params.nr_steps + 1)]
        self._df_computed = True

    def compute_prob(self):
        if self._prob_computed:
            pass
        if not self._df_computed:
            self._compute_df()
        p = (1 / self._df[1] - np.exp(self._down_log_step)) / (np.exp(self._up_log_step) - np.exp(self._down_log_step))
        q = 1 - p
        self._prob = (p, q)
        self._prob_computed = True


class BalancedSimpleBinomialTree(SimpleBinomialTree):
    def __init__(self, contract: Contract, model: MarketModel, params: Params):
        if not isinstance(params, TreeParams):
            raise TypeError(f'Params must be of type TreeParams but received {type(params).__name__}')
        up = BalancedSimpleBinomialTree.calc_up_step_mult(
            model.risk_free_rate,
            model.get_vol(contract.strike, contract.expiry),
            params.nr_steps,
            contract.expiry)
        down = BalancedSimpleBinomialTree.calc_down_step_mult(
            model.risk_free_rate,
            model.get_vol(contract.strike, contract.expiry),
            params.nr_steps,
            contract.expiry)
        p = TreeParams(params.nr_steps, up, down)
        super().__init__(contract, model, p)

    @staticmethod
    def calc_up_step_mult(rate: float, vol: float, nr_steps: int, exp: float) -> float:
        delta_t = exp / nr_steps
        log_mean = rate * delta_t - 0.5 * vol ** 2 * delta_t
        return np.exp(log_mean + vol * np.sqrt(delta_t))

    @staticmethod
    def calc_down_step_mult(rate: float, vol: float, nr_steps: int, exp: float) -> float:
        delta_t = exp / nr_steps
        log_mean = rate * delta_t - 0.5 * vol ** 2 * delta_t
        return np.exp(log_mean - vol * np.sqrt(delta_t))


class Params(ABC):
    def to_dict(self) -> dict[str, any]:
        return vars(self)


class MCParams(Params):
    def __init__(self, seed: int = 1, num_of_path: int = 10000, tenor_frequency: int = 12,
                 standardize: bool = True, antithetic: bool = True, control_variate: bool = False) -> None:
        self.seed = seed
        self.num_of_paths = num_of_path
        self.tenor_frequency = tenor_frequency
        self.standardize = standardize
        self.antithetic = antithetic
        self.control_variate = control_variate


class PDEParams(Params):
    def __init__(self, und_step: int = 2, time_step: float = 1/1200, stock_min_mult: float = 0, stock_max_mult: float = 2,
                 method: BSPDEMethod = BSPDEMethod.EXPLICIT) -> None:
        self.und_step = und_step  # dS
        self.time_step = time_step  # dt
        self.stock_min_mult = stock_min_mult
        self.stock_max_mult = stock_max_mult
        self.method = method


class TreeParams(Params):
    def __init__(self, nr_steps: int = 1, up_step_mult: float = np.nan, down_step_mult: float = np.nan) -> None:
        self.nr_steps = nr_steps
        self.up_step_mult = up_step_mult
        self.down_step_mult = down_step_mult


class BlackScholesPDE(PDEMethod):
    def __init__(self, contract: EuropeanContract, model: MarketModel, params: PDEParams):
        if not isinstance(contract, EuropeanContract):
            raise TypeError(f'Contract must be of type EuropeanContract but received {type(contract).__name__}')
        if not isinstance(params, PDEParams):
            raise TypeError(f'Params must be of type PDEParams but received {type(params).__name__}')
        super().__init__(contract, model, params)
        self.contract = contract
        self.exp = contract.expiry
        self.strike = contract.strike
        self.sigma = model.get_vol(contract.strike, contract.expiry)
        self.time_step = params.time_step
        self.und_step = params.und_step
        self.derivative_type = contract.derivative_type
        self.stock_min = params.stock_min_mult * model.spot
        self.stock_max = params.stock_max_mult * model.spot
        self.num_of_und_steps = int(np.round((self.stock_max - self.stock_min) / float(self.und_step)))  # Number of stock price steps
        self.num_of_time_steps = int(np.round(self.exp / float(self.time_step)))   # Number of time steps
        self.interest_rate = model.risk_free_rate
        self.grid = np.zeros((self.num_of_und_steps + 1, self.num_of_time_steps + 1))
        self.stock_disc = np.linspace(self.stock_min, self.stock_max, self.num_of_und_steps + 1)
        self.time_disc = np.linspace(0, self.exp, self.num_of_time_steps + 1)
        self.measure_of_stock = self.stock_disc / self.und_step
        self.df = model.calc_df(self.exp - self.time_disc)

    def setup_boundary_conditions(self):
        if self.derivative_type == PutCallFwd.CALL:
            # terminal condition
            self.grid[:, -1] = np.maximum(self.stock_disc - self.strike, 0)
            # right boundary
            self.grid[-1, :] = self.stock_max - self.strike * self.df

        elif self.derivative_type == PutCallFwd.PUT:
            # terminal condition
            self.grid[:, -1] = np.maximum(self.strike - self.stock_disc, 0)
            # left condition
            self.grid[0, :] = self.strike * self.df - self.stock_min

        else:
            self.contract.raise_incorrect_derivative_type_error()

    def explicit_method(self):
        self.setup_boundary_conditions()
        alpha = 0.5 * self.time_step * (self.sigma ** 2 * self.measure_of_stock ** 2 - self.interest_rate *
                                        self.measure_of_stock)
        beta = 1 - self.time_step * (self.sigma ** 2 * self.measure_of_stock ** 2 + self.interest_rate)
        gamma = 0.5 * self.time_step * (self.sigma ** 2 * self.measure_of_stock ** 2 + self.interest_rate *
                                        self.measure_of_stock)
        for j in range(self.num_of_time_steps-1, -1, -1):  # for t
            for i in range(1, self.num_of_und_steps):  # for S
                self.grid[i, j] = alpha[i] * self.grid[i - 1, j + 1] + beta[i] * self.grid[i, j + 1] + gamma[i] \
                                              * self.grid[i + 1, j + 1]

    def implicit_method(self):
        self.setup_boundary_conditions()
        alpha = 0.5 * self.time_step * (self.interest_rate * self.measure_of_stock - self.sigma ** 2 *
                                        self.measure_of_stock ** 2)
        beta = 1 + self.time_step * (self.sigma ** 2 * self.measure_of_stock ** 2 + self.interest_rate)
        gamma = - 0.5 * self.time_step * (self.sigma ** 2 * self.measure_of_stock ** 2 +
                                          self.interest_rate * self.measure_of_stock)
        upper_matrix = np.diag(alpha[2:-1], -1) + np.diag(beta[1:-1]) + np.diag(gamma[1:-2], 1)
        lower_matrix = np.eye(self.num_of_und_steps - 1)

        rhs_vector = np.zeros(self.num_of_und_steps-1)
        for j in range(self.num_of_time_steps-1, -1, -1):  # for t
            rhs_vector[0] = -alpha[1] * self.grid[0, j+1]
            rhs_vector[-1] = -gamma[-2] * self.grid[-1, j+1]
            self.grid[1:-1, j] = np.linalg.solve(lower_matrix, np.linalg.solve(upper_matrix, self.grid[1:-1, j + 1]
                                                                               + rhs_vector))

    def crank_nicolson_method(self):
        self.setup_boundary_conditions()

        alpha = 0.25 * self.time_step * (-self.interest_rate * self.measure_of_stock + self.sigma ** 2 *
                                         self.measure_of_stock ** 2)
        beta = -0.5 * self.time_step * (self.sigma ** 2 * self.measure_of_stock ** 2 + self.interest_rate)
        gamma = 0.25 * self.time_step * (self.sigma ** 2 * self.measure_of_stock ** 2 +
                                         self.interest_rate * self.measure_of_stock)
        upper_matrix = - np.diag(alpha[2:-1], -1) + np.diag(1-beta[1:-1]) - np.diag(gamma[1:-2], 1)
        lower_matrix = np.eye(self.num_of_und_steps - 1)
        rhs_matrix = np.diag(alpha[2:-1], -1) + np.diag(1+beta[1:-1]) + np.diag(gamma[1:-2], 1)

        rhs_vector = np.zeros(self.num_of_und_steps - 1)
        for j in range(self.num_of_time_steps-1, -1, -1):  # for t
            rhs_vector[0] = alpha[1] * (self.grid[0, j + 1] + self.grid[0, j])
            rhs_vector[-1] = gamma[-2] * (self.grid[-1, j + 1] + self.grid[-1, j])
            self.grid[1:-1, j] = np.linalg.solve(lower_matrix,
                                                 np.linalg.solve(upper_matrix,
                                                                 (rhs_matrix @ self.grid[1:-1, j + 1]) + rhs_vector))



