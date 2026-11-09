"""Display-only battery estimation. Never used to authorize robot motion.

Legacy BatteryState publishes load current with an artificial charge sign.
Integrate it only when disconnected; separately measured charge current may also
be integrated when charging. The starting charge and slow downward
correction use Hello Robot's coarse voltage curve under moderate load, not an
open-circuit-voltage model. Effective capacity and temperature are uncalibrated.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass
import math
import statistics
import time


@dataclass(frozen=True)
class EstimatorConfig:
    capacity_ah: float = 18.0
    median_window_seconds: float = 5.0
    voltage_tau_seconds: float = 30.0
    current_tau_seconds: float = 60.0
    correction_tau_seconds: float = 900.0
    warmup_seconds: float = 30.0
    runtime_min_history_seconds: float = 300.0
    max_sample_age_seconds: float = 3.0
    recharge_percentage: float = 60.0
    charge_efficiency: float = .9
    charge_time_min_history_seconds: float = 60.0
    # Manufacturer approximation under load; not a calibrated OCV/SOC curve.
    voltage_curve: tuple = ((9.5, 0.), (10.5, 40.), (11., 60.), (11.5, 80.), (13.4, 100.))

    def __post_init__(self):
        values = [value for key, value in vars(self).items() if key != 'voltage_curve']
        if (not all(isinstance(v, (int, float)) and math.isfinite(v) for v in values)
                or not 1 <= self.capacity_ah <= 200
                or not 1 <= self.median_window_seconds <= 30
                or not 1 <= self.voltage_tau_seconds <= 300
                or not 1 <= self.current_tau_seconds <= 300
                or not 30 <= self.correction_tau_seconds <= 3600
                or not 5 <= self.warmup_seconds <= 300
                or self.runtime_min_history_seconds < self.warmup_seconds
                or not .5 <= self.max_sample_age_seconds <= 10
                or not .5 <= self.charge_efficiency <= 1.
                or not 10 <= self.charge_time_min_history_seconds <= 600
                or not 0 <= self.recharge_percentage < 100):
            raise ValueError('Invalid battery estimator configuration')
        curve = self.voltage_curve
        if (len(curve) < 2 or any(len(pair) != 2 or not all(math.isfinite(v) for v in pair) for pair in curve)
                or curve[0][1] != 0 or curve[-1][1] != 100
                or any(a[0] >= b[0] or a[1] >= b[1] for a, b in zip(curve, curve[1:]))):
            raise ValueError('Battery voltage curve must increase from 0 to 100 percent')


class BatteryEstimator:
    def __init__(self, config=None, *, clock=time.monotonic, wall=time.time):
        self.config = config or EstimatorConfig()
        self.clock, self.wall = clock, wall
        self._samples = deque(maxlen=1000)
        self._history = deque(maxlen=120)
        self._last = None
        self._received = None
        self._started = None
        self._current_seconds = 0.
        self._voltage = self._load = self._soc = self._display = None
        self._previous_current = None
        self._previous_charge_current = None
        self._charge_rate = None
        self._charge_seconds = 0.
        self._charge_origin = None
        self._counted_charge = False
        self._last_history = -math.inf

    def voltage_percentage(self, voltage):
        curve = self.config.voltage_curve
        if voltage <= curve[0][0]:
            return 0.
        for (v0, p0), (v1, p1) in zip(curve, curve[1:]):
            if voltage <= v1:
                return p0 + (voltage - v0) * (p1 - p0) / (v1 - v0)
        return 100.

    @staticmethod
    def _finite(value):
        return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)

    def update(self, *, voltage, current, charger_connected, charging_state, sampled_at,
               charging_current=None):
        now, wall = self.clock(), self.wall()
        if (not self._finite(voltage) or not 8 <= voltage <= 16
                or not self._finite(sampled_at)
                or not -.5 <= wall - sampled_at <= self.config.max_sample_age_seconds
                or not isinstance(charger_connected, bool)
                or charging_state not in {'unknown', 'charging', 'discharging', 'not charging', 'full'}):
            return False
        if self._last and sampled_at <= self._last['sampled_at']:
            return False
        current = current if self._finite(current) and abs(current) <= 50 else None
        charging_current = charging_current if self._finite(charging_current) and 0 <= charging_current <= 20 else None
        if not charger_connected or charging_state != 'charging':
            charging_current = None
        dt = max(0., now - self._received) if self._received is not None else 0.
        gap = self._received is None or dt > self.config.max_sample_age_seconds
        transition = self._last is not None and charger_connected != self._last['charger_connected']
        retained_soc = self._soc if transition and not gap and (charger_connected or self._counted_charge) else None
        if gap or transition:
            # Missing consumption/charging cannot be reconstructed. Reinitialize
            # instead of integrating a stale current over the gap.
            self._samples.clear()
            self._history.clear()
            self._last_history = -math.inf
            self._started = now
            self._current_seconds = 0.
            self._voltage = self._load = self._soc = self._display = None
            self._previous_current = None
            self._previous_charge_current = None
            self._charge_rate = None
            self._charge_seconds = 0.
            self._counted_charge = False
            self._charge_origin = 'retained' if retained_soc is not None else 'charging_voltage' if charger_connected else None
            self._soc = retained_soc
            self._display = int(math.floor(retained_soc + .5)) if retained_soc is not None else None
            dt = 0.
        self._samples.append((now, voltage, abs(current) if current is not None else None))
        while self._samples and now - self._samples[0][0] >= self.config.median_window_seconds:
            self._samples.popleft()
        median_voltage = statistics.median(s[1] for s in self._samples)
        alpha = -math.expm1(-dt / self.config.voltage_tau_seconds)
        self._voltage = median_voltage if self._voltage is None else self._voltage + alpha * (median_voltage - self._voltage)
        if current is not None:
            median_current = statistics.median(s[2] for s in self._samples if s[2] is not None)
            load_alpha = -math.expm1(-dt / self.config.current_tau_seconds)
            self._load = median_current if self._load is None else self._load + load_alpha * (median_current - self._load)
        else:
            self._load = None
            self._current_seconds = 0.
        if charger_connected:
            if charging_current is None:
                self._charge_rate = None
                self._charge_seconds = 0.
            else:
                load_alpha = -math.expm1(-dt / self.config.current_tau_seconds)
                self._charge_rate = charging_current if self._charge_rate is None else self._charge_rate + load_alpha * (charging_current - self._charge_rate)
                if self._previous_charge_current is not None:
                    self._charge_seconds += dt
            if charging_state == 'full':
                self._soc = 100.
                self._charge_origin = 'full_confirmation'
                self._counted_charge = True
            elif self._soc is None:
                # Starting while already connected has no rested-voltage anchor.
                # Expose this as a rough estimate, never as confirmed charge.
                self._soc = min(99., self.voltage_percentage(self._voltage))
            elif charging_current is not None and self._previous_charge_current is not None:
                amps = (charging_current + self._previous_charge_current) / 2 * self.config.charge_efficiency
                self._soc = max(0., min(99., self._soc + amps * dt / (self.config.capacity_ah * 3600) * 100))
                self._counted_charge = True
            elif not self._counted_charge and self._charge_origin == 'charging_voltage':
                self._soc = min(99., self.voltage_percentage(self._voltage))
        else:
            target = min(99., self.voltage_percentage(self._voltage))
            if self._soc is None:
                self._soc = target
            elif self._charge_origin != 'retained' and now - self._started < self.config.warmup_seconds:
                self._soc = target
            else:
                # Trapezoidal coulomb counting uses unfiltered load samples;
                # smoothing the current would lose charge during load changes.
                if current is not None and self._previous_current is not None:
                    amps = (abs(current) + self._previous_current) / 2
                    self._soc -= amps * dt / (self.config.capacity_ah * 3600) * 100
                correction = -math.expm1(-dt / self.config.correction_tau_seconds)
                self._soc += min(0., (target - self._soc) * correction)
                self._soc = max(0., self._soc)
            if current is not None and self._previous_current is not None:
                self._current_seconds += dt
        # One-percent resolution with a Schmitt deadband avoids flickering.
        if self._display is None or abs(self._soc - self._display) >= .8:
            self._display = int(math.floor(self._soc + .5))
        self._previous_current = abs(current) if current is not None and not charger_connected else None
        self._previous_charge_current = charging_current
        self._last = {'voltage': voltage, 'current': current, 'charger_connected': charger_connected,
                      'charging_state': charging_state, 'sampled_at': sampled_at,
                      'charging_current': charging_current}
        self._received = now
        if now - self._last_history >= 10:
            self._history.append({'sampled_at': sampled_at, 'voltage': round(voltage, 3),
                                  'filtered_voltage': round(self._voltage, 3)})
            self._last_history = now
        return True

    def snapshot(self):
        now, wall = self.clock(), self.wall()
        age = wall - self._last['sampled_at'] if self._last else None
        available = bool(self._last and self._received is not None
                         and 0 <= now - self._received <= self.config.max_sample_age_seconds
                         and -.5 <= age <= self.config.max_sample_age_seconds)
        result = {'type': 'battery', 'available': available, 'sampled_at': self._last['sampled_at'] if self._last else None,
                  'sample_age_seconds': round(max(0., age), 2) if age is not None else None,
                  'percentage': None, 'runtime_to_recharge_seconds': None,
                  'time_to_full_seconds': None, 'charge_time_status': 'unavailable',
                  'runtime_status': 'unavailable', 'quality': 'unavailable',
                  'capacity_ah': self.config.capacity_ah, 'calibrated': False,
                  'warmup_seconds': self.config.warmup_seconds,
                  'runtime_min_history_seconds': self.config.runtime_min_history_seconds,
                  'charge_efficiency': self.config.charge_efficiency,
                  'charge_time_min_history_seconds': self.config.charge_time_min_history_seconds,
                  'recharge_percentage': self.config.recharge_percentage,
                  'history': list(self._history)}
        if not available:
            return result
        connected = self._last['charger_connected']
        state = self._last['charging_state']
        elapsed = now - self._started
        result.update(self._last)
        result.update(filtered_voltage=round(self._voltage, 3),
                      load_current=round(self._load, 2) if self._load is not None else None,
                      raw_percentage=round(self.voltage_percentage(self._last['voltage']), 1),
                      voltage_warning='critical' if self._last['voltage'] < 10.5 else
                          'low' if self._last['voltage'] < 11 else None,
                      observed_seconds=round(elapsed),
                      warmup_remaining_seconds=max(0, math.ceil(self.config.warmup_seconds - elapsed)),
                      method='voltage_and_discharge_counting' if self._load is not None else 'filtered_voltage')
        if connected:
            result['quality'] = 'full' if state == 'full' else 'rough_charge_estimate' if self._charge_origin == 'charging_voltage' else 'estimate'
            result['percentage'] = 100 if state == 'full' else min(99, self._display)
            result['runtime_status'] = 'connected'
            result['charge_estimate_origin'] = self._charge_origin
            if self._last['charging_current'] is not None:
                result['method'] = 'charge_current_counting'
                result['charging_current'] = round(self._charge_rate, 2)
            if state == 'full':
                result['time_to_full_seconds'] = 0
                result['charge_time_status'] = 'full'
            elif state != 'charging':
                result['charge_time_status'] = 'not_charging'
            elif self._last['charging_current'] is None:
                result['charge_time_status'] = 'current_unavailable'
            elif self._charge_rate <= .1:
                result['charge_time_status'] = 'current_too_low'
            elif self._charge_seconds < self.config.charge_time_min_history_seconds:
                result['charge_time_status'] = 'learning'
            else:
                seconds = ((100 - self._soc) / 100 * self.config.capacity_ah /
                           (self._charge_rate * self.config.charge_efficiency) * 3600)
                result['time_to_full_seconds'] = max(300, math.ceil(seconds / 300) * 300)
                result['charge_time_status'] = 'estimate_at_current_rate'
        else:
            result['percentage'] = self._display
            result['quality'] = 'settling' if elapsed < self.config.warmup_seconds else 'estimate'
            if self._soc <= self.config.recharge_percentage:
                result['runtime_to_recharge_seconds'] = 0
                result['runtime_status'] = 'recharge_recommended'
            elif self._load is None or self._load < .1:
                result['runtime_status'] = 'load_unavailable'
            elif self._current_seconds < self.config.runtime_min_history_seconds:
                result['runtime_status'] = 'learning'
            else:
                seconds = ((self._soc - self.config.recharge_percentage) / 100
                           * self.config.capacity_ah / self._load * 3600)
                result['runtime_to_recharge_seconds'] = round(seconds / 300) * 300
                result['runtime_status'] = 'estimate'
        return result
