# pylint: disable=invalid-name
"""
Flux-vector control for synchronous motor drives.

This implements a simplified version of stator-flux-vector control [1]_. The
rotor coordinates are used in this implementation [2]_. One control variable is
the stator-flux magnitude and another is the electromagnetic torque. The latter
choice differs from [1]_ and [2]_, where the torque-producing current component
is used. Furthermore, here proportional control is used for simplicity. The
magnetic saturation is not considered in this implementation.

References
----------
.. [1] Pellegrino, Armando, Guglielmi, “Direct flux field-oriented control of
   IPM drives with variable DC link in the field-weakening region,” IEEE Trans.
   Ind. Appl., 2009, https://doi.org/10.1109/TIA.2009.2027167

.. [2] Awan, Hinkkanen, Bojoi, Pellegrino, "Stator-flux-oriented control of
   synchronous motors: A systematic design procedure," IEEE Trans. Ind. Appl.,
   2019, https://doi.org/10.1109/TIA.2019.2927316

"""
from typing import Callable
from dataclasses import dataclass, field
import numpy as np

from motulator.helpers import abc2complex, Bunch
from motulator.control.common import Ctrl, SpeedCtrl, PWM
from motulator.control.sm_vector import SensorlessObserver
from motulator.control.sm_obs_vhz import FluxTorqueRef


# %%
@dataclass
class SynchronousMotorFluxVectorCtrlPars:
    """Control parameters: flux-vector control for synchronous motor drives."""

    # pylint: disable=too-many-instance-attributes
    # Speed reference (in electrical rad/s)
    w_m_ref: Callable[[float], float] = field(
        repr=False, default=lambda t: (t > .2)*(2*np.pi*75))
    # Mode
    sensorless: bool = True
    # Sampling period
    T_s: float = 250e-6
    # Flux reference limits
    psi_s_min: float = None
    psi_s_max: float = None
    # Voltage marginal
    k_u: float = .9
    # Bandwidths
    alpha_psi: float = 2*np.pi*100
    alpha_tau: float = 2*np.pi*400
    alpha_s: float = 2*np.pi*4
    # Maximum values
    tau_M_max: float = 1.5*14
    i_s_max: float = 1.5*np.sqrt(2)*5.
    # Motor parameter estimates
    R_s: float = 3.6
    L_d: float = .036
    L_q: float = .051
    psi_f: float = .545
    p: int = 3
    J: float = .015
    # Sensorless observer (used only in the sensorless mode)
    w_o: float = 2*np.pi*40
    zeta_inf: float = .2
    # Sensored observer (used only in the sensored mode)
    g: float = 2*np.pi*15


# %%
class SynchronousMotorFluxVectorCtrl(Ctrl):
    """
    Flux-vector control for synchronous motor drives.

    This class interconnects the subsystems of the control system and
    provides the interface to the solver.

    Parameters
    ----------
    pars : SynchronousMotoroFluxVectorCtrlPars
        Control parameters.

    """

    # pylint: disable=too-many-instance-attributes
    def __init__(self, pars):
        super().__init__()
        self.T_s = pars.T_s
        self.w_m_ref = pars.w_m_ref
        self.p = pars.p
        self.sensorless = pars.sensorless
        self.flux_torque_ctrl = FluxTorqueCtrl(pars)
        self.speed_ctrl = SpeedCtrl(pars)
        self.pwm = PWM(pars)
        if pars.sensorless:
            self.observer = SensorlessObserver(pars)
        else:
            self.observer = Observer(pars)
        self.flux_torque_ref = FluxTorqueRef(pars)

    def __call__(self, mdl):
        """
        Run the main control loop.

        Parameters
        ----------
        mdl : SynchronousMotorDrive
            Continuous-time model of a synchronous motor drive for getting the
            feedback signals.

        Returns
        -------
        T_s : float
            Sampling period.
        d_abc_ref : ndarray, shape (3,)
            Duty ratio references.

        """
        # Get the speed reference
        w_m_ref = self.w_m_ref(self.t)

        # Feedback signals
        i_s_abc = mdl.motor.meas_currents()  # Phase currents
        u_dc = mdl.conv.meas_dc_voltage()  # DC-bus voltage
        u_s = self.pwm.realized_voltage  # Realized voltage from PWM

        if self.sensorless:
            # Get the rotor speed and position estimates
            w_m, theta_m = self.observer.w_m, self.observer.theta_m
        else:
            # Measure the rotor speed
            w_m = self.p*mdl.mech.meas_speed()
            # Limit the electrical rotor position into [-pi, pi)
            theta_m = np.mod(
                self.p*mdl.mech.meas_position() + np.pi, 2*np.pi) - np.pi

        # Current vector in estimated rotor coordinates
        i_s = np.exp(-1j*theta_m)*abc2complex(i_s_abc)

        # Flux and torque estimates
        psi_s = self.observer.psi_s

        # Outputs
        tau_M_ref = self.speed_ctrl.output(w_m_ref/self.p, w_m/self.p)
        psi_s_ref, tau_M_ref_lim = self.flux_torque_ref(tau_M_ref, w_m, u_dc)
        u_s_ref = self.flux_torque_ctrl(
            psi_s_ref, tau_M_ref_lim, psi_s, i_s, w_m)
        d_abc_ref, u_s_ref_lim = self.pwm.output(u_s_ref, u_dc, theta_m, w_m)

        # Data logging
        data = Bunch(
            i_s=i_s,
            psi_s=psi_s,
            psi_s_ref=psi_s_ref,
            t=self.t,
            tau_M_ref_lim=tau_M_ref_lim,
            theta_m=theta_m,
            u_dc=u_dc,
            u_s=u_s,
            w_m=w_m,
            w_m_ref=w_m_ref,
        )
        self.save(data)

        # Update states
        self.observer.update(u_s, i_s, w_m)
        self.speed_ctrl.update(tau_M_ref_lim)
        self.pwm.update(u_s_ref_lim)
        self.update_clock(self.T_s)

        return self.T_s, d_abc_ref


# %%
class FluxTorqueCtrl:
    """
    Stator flux and torque controller.

    Parameters
    ----------
    pars : SynchronousMotoroFluxVectorCtrlPars
        Control parameters.

    """

    # pylint: disable=too-few-public-methods
    def __init__(self, pars):
        self.T_s = pars.T_s
        self.R_s = pars.R_s
        self.p = pars.p
        self.alpha_psi = pars.alpha_psi
        # Gain k_tau
        G = (pars.L_d - pars.L_q)/(pars.L_d*pars.L_q)
        psi_s0 = pars.psi_f if pars.psi_f > 0 else pars.psi_s_min
        if pars.psi_f > 0:  # PMSM or PM-SyRM
            c_delta0 = 1.5*pars.p*(pars.psi_f*psi_s0/pars.L_d - G*psi_s0**2)
        else:  # SyRM
            c_delta0 = 1.5*pars.p*G*psi_s0**2
        self.k_tau = pars.alpha_tau/c_delta0

    def __call__(self, psi_s_ref, tau_M_ref, psi_s, i_s, w_m):
        """
        Compute the unlimited voltage reference.

        Parameters
        ----------
        psi_s_ref : float
            Stator flux reference (magnitude).
        tau_M_ref : float
            Torque reference.
        psi_s : complex
            Stator flux estimate.
        i_s : complex
            Stator current.
        w_m : float
            Rotor speed (in electrical rad/s).

        Returns
        -------
        u_s_ref : complex
            Unlimited voltage reference.

        """
        # Torque estimate
        tau_M = 1.5*self.p*np.imag(i_s*np.conj(psi_s))

        # Stator frequency
        w_s = w_m + self.k_tau*(tau_M_ref - tau_M)

        # Voltage reference
        e_psi = psi_s_ref - np.abs(psi_s)
        delta = np.angle(psi_s)
        u_s_ref = (
            self.R_s*i_s + 1j*w_s*psi_s +
            self.alpha_psi*e_psi*np.exp(1j*delta))

        return u_s_ref


# %%
class Observer:
    """
    Sensored observer.

    Parameters
    ----------
    pars : SynchronousMotoroFluxVectorCtrlPars
        Control parameters.

    """

    # pylint: disable=too-few-public-methods
    def __init__(self, pars):
        self.T_s = pars.T_s
        self.R_s = pars.R_s
        self.L_d = pars.L_d
        self.L_q = pars.L_q
        self.psi_f = pars.psi_f
        self.g = pars.g
        # Initial state
        self.psi_s = pars.psi_f

    def update(self, u_s, i_s, w_m):
        """
        Update the states for the next sampling period.

        Parameters
        ----------
        u_s : complex
            Stator voltage in estimated rotor coordinates.
        i_s : complex
            Stator current in estimated rotor coordinates.
        w_m : float
            Rotor speed (in electrical rad/s).

        """
        # Estimation error
        e = self.L_d*i_s.real + 1j*self.L_q*i_s.imag + self.psi_f - self.psi_s

        # Update the state
        self.psi_s += self.T_s*(
            u_s - self.R_s*i_s - 1j*w_m*self.psi_s + self.g*e)
