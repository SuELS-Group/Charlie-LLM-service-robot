import math
import unittest

from web_bridge_node.battery_estimator import BatteryEstimator, EstimatorConfig


class BatteryEstimatorTests(unittest.TestCase):
    def setUp(self):
        self.seconds = 0.
        self.estimator = BatteryEstimator(EstimatorConfig(warmup_seconds=5.),
                                          clock=lambda: self.seconds, wall=lambda: 1000. + self.seconds)

    def sample(self, *, voltage=11.5, current=-1.8, connected=False, state='discharging', step=1., charging_current=None):
        self.seconds += step
        return self.estimator.update(voltage=voltage, current=current, charger_connected=connected,
                                     charging_state=state, sampled_at=1000. + self.seconds,
                                     charging_current=charging_current)

    def run_samples(self, seconds, **kwargs):
        for _ in range(seconds):
            self.sample(**kwargs)

    def test_no_measurement_or_old_measurement_is_not_live(self):
        self.assertFalse(self.estimator.snapshot()['available'])
        self.sample()
        self.seconds += 4
        state = self.estimator.snapshot()
        self.assertFalse(state['available'])
        self.assertIsNone(state['percentage'])
        self.assertIsNone(state['runtime_to_recharge_seconds'])

    def test_isolated_voltage_spike_does_not_move_estimated_charge(self):
        self.run_samples(40)
        before = self.estimator.snapshot()
        self.sample(voltage=9.6)
        during = self.estimator.snapshot()
        self.assertEqual(during['percentage'], before['percentage'])
        self.assertEqual(during['filtered_voltage'], before['filtered_voltage'])
        self.assertEqual(during['voltage_warning'], 'critical')
        self.sample()
        self.assertEqual(self.estimator.snapshot()['percentage'], before['percentage'])

    def test_coulomb_counting_matches_known_charge_consumption(self):
        self.run_samples(10)
        before = self.estimator._soc
        self.run_samples(360)
        # 1.8 A for six minutes consumes 0.18 Ah = 1% of 18 Ah.
        self.assertAlmostEqual(before - self.estimator._soc, 1., places=8)
        self.assertEqual(self.estimator.snapshot()['method'], 'voltage_and_discharge_counting')

    def test_integration_uses_actual_elapsed_time(self):
        self.run_samples(10)
        before = self.estimator._soc
        for _ in range(180):
            self.sample(step=2.)
        self.assertAlmostEqual(before - self.estimator._soc, 1., places=8)

    def test_current_load_step_is_integrated_without_filter_lag(self):
        self.run_samples(10)
        before = self.estimator._soc
        self.sample(current=-9.)
        expected = (1.8 + 9.) / 2 / (18 * 3600) * 100
        self.assertAlmostEqual(before - self.estimator._soc, expected, places=8)

    def test_voltage_recovery_does_not_recharge_the_battery(self):
        self.run_samples(60, voltage=11.)
        before = self.estimator.snapshot()['percentage']
        self.run_samples(180, voltage=12.5)
        self.assertLessEqual(self.estimator.snapshot()['percentage'], before)

    def test_sustained_voltage_drop_eventually_corrects_estimate(self):
        self.run_samples(10)
        self.run_samples(900, voltage=10.8)
        state = self.estimator.snapshot()
        self.assertLess(state['percentage'], 65)
        self.assertEqual(state['voltage_warning'], 'low')

    def test_charging_current_is_not_falsely_integrated(self):
        self.run_samples(10)
        self.run_samples(500, voltage=13.4, current=8., connected=True, state='charging')
        state = self.estimator.snapshot()
        self.assertEqual(state['percentage'], 80)
        self.assertEqual(state['charging_state'], 'charging')
        self.assertEqual(state['quality'], 'estimate')
        self.assertIsNone(state['runtime_to_recharge_seconds'])

    def test_voltage_alone_never_claims_a_full_battery(self):
        self.run_samples(10, voltage=13.4)
        self.assertLess(self.estimator.snapshot()['percentage'], 100)
        self.sample(voltage=13.4, connected=True, state='not charging')
        self.assertLess(self.estimator.snapshot()['percentage'], 100)
        self.sample(voltage=13.4, connected=True, state='full')
        self.assertEqual(self.estimator.snapshot()['percentage'], 100)

    def test_unplugging_reinitializes_without_using_charger_voltage(self):
        self.run_samples(20, voltage=13.4, connected=True, state='charging')
        self.sample(voltage=11.5)
        state = self.estimator.snapshot()
        self.assertEqual(state['percentage'], 80)
        self.assertEqual(state['quality'], 'settling')
        self.assertEqual(state['runtime_status'], 'learning')

    def test_missing_data_resets_runtime_history_instead_of_extrapolating(self):
        self.run_samples(400)
        self.assertEqual(self.estimator.snapshot()['runtime_status'], 'estimate')
        self.seconds += 600
        self.sample(voltage=11.)
        state = self.estimator.snapshot()
        self.assertEqual(state['percentage'], 60)
        self.assertEqual(state['observed_seconds'], 0)
        self.assertEqual(len(state['history']), 1)

    def test_runtime_requires_five_minutes_of_valid_current(self):
        self.run_samples(300)
        self.assertIsNone(self.estimator.snapshot()['runtime_to_recharge_seconds'])
        self.sample()
        state = self.estimator.snapshot()
        self.assertEqual(state['runtime_status'], 'estimate')
        self.assertGreater(state['runtime_to_recharge_seconds'], 0)
        self.assertFalse(state['calibrated'])
        self.sample(current=float('nan'))
        state = self.estimator.snapshot()
        self.assertTrue(state['available'])
        self.assertEqual(state['method'], 'filtered_voltage')
        self.assertIsNone(state['runtime_to_recharge_seconds'])

    def test_invalid_or_out_of_order_samples_do_not_refresh_telemetry(self):
        self.sample()
        stamp = 1000. + self.seconds
        for kwargs in ({'voltage': float('nan')}, {'voltage': float('inf')}, {'voltage': 0.},
                       {'sampled_at': stamp - 100}, {'sampled_at': stamp}, {'sampled_at': stamp + 100}):
            sample = dict(voltage=12., current=-2., charger_connected=False,
                          charging_state='discharging', sampled_at=stamp + .1)
            sample.update(kwargs)
            self.assertFalse(self.estimator.update(**sample))
        self.seconds += 4
        self.assertFalse(self.estimator.snapshot()['available'])

    def test_history_is_bounded_and_configuration_is_validated(self):
        self.run_samples(2000)
        self.assertEqual(len(self.estimator.snapshot()['history']), 120)
        for options in ({'capacity_ah': 0}, {'voltage_tau_seconds': math.nan},
                        {'voltage_curve': ((12., 0.), (11., 100.))}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                EstimatorConfig(**options)

    def test_cold_start_while_charging_is_explicitly_a_rough_estimate(self):
        self.run_samples(20, voltage=12.5, connected=True, state='charging')
        state=self.estimator.snapshot()
        self.assertIsInstance(state['percentage'], int)
        self.assertEqual(state['quality'], 'rough_charge_estimate')
        self.assertIsNone(state['time_to_full_seconds'])
        self.assertEqual(state['charge_time_status'], 'current_unavailable')

    def test_separate_charging_current_is_integrated_with_retention_efficiency(self):
        self.run_samples(10)
        self.sample(voltage=13.4, connected=True, state='charging', charging_current=2.)
        before=self.estimator._soc
        self.run_samples(360, voltage=13.4, current=8., connected=True, state='charging', charging_current=2.)
        # 2 A x 0.1 h x .9 / 18 Ah = one percentage point.
        self.assertAlmostEqual(self.estimator._soc-before, 1., places=8)
        state=self.estimator.snapshot()
        self.assertEqual(state['method'], 'charge_current_counting')
        self.assertEqual(state['charging_current'], 2.)
        self.assertGreater(state['time_to_full_seconds'], 0)

    def test_charge_time_requires_fresh_uninterrupted_measured_current(self):
        self.run_samples(59, voltage=12.5, connected=True, state='charging', charging_current=2.)
        self.assertIsNone(self.estimator.snapshot()['time_to_full_seconds'])
        self.run_samples(2, voltage=12.5, connected=True, state='charging', charging_current=2.)
        self.assertIsNotNone(self.estimator.snapshot()['time_to_full_seconds'])
        self.sample(voltage=12.5, connected=True, state='charging')
        self.assertIsNone(self.estimator.snapshot()['time_to_full_seconds'])
        self.assertEqual(self.estimator.snapshot()['charge_time_status'], 'current_unavailable')

    def test_connected_but_not_charging_does_not_accumulate_charge(self):
        self.run_samples(10)
        before=self.estimator._soc
        self.run_samples(120, voltage=13.4, connected=True, state='not charging', charging_current=5.)
        self.assertEqual(self.estimator._soc,before)
        self.assertIsNone(self.estimator.snapshot()['time_to_full_seconds'])

    def test_measured_charge_survives_unplugging_without_a_percentage_jump(self):
        self.run_samples(10)
        self.run_samples(120, voltage=13.4, connected=True, state='charging', charging_current=2.)
        before=self.estimator.snapshot()['percentage']
        self.sample(voltage=11.8)
        self.assertEqual(self.estimator.snapshot()['percentage'], before)

    def test_charge_counting_never_claims_full_from_voltage_or_elapsed_time(self):
        self.run_samples(5000, voltage=14.5, connected=True, state='charging', charging_current=10.)
        self.assertLess(self.estimator.snapshot()['percentage'], 100)
        self.sample(voltage=14.5, connected=True, state='full')
        self.assertEqual(self.estimator.snapshot()['percentage'], 100)
        self.assertEqual(self.estimator.snapshot()['time_to_full_seconds'], 0)


if __name__ == '__main__':
    unittest.main()
