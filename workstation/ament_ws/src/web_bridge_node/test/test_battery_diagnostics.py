from types import SimpleNamespace as NS
import unittest

from web_bridge_node.battery_diagnostics import charging_measurement


def diagnostic(*, timestamp=100., hardware='stretch-se3-3152', **values):
    defaults = {'current': '5.0', 'current_charge': '2.0', 'charger_connected': 'True', 'charger_is_charging': 'True'}
    defaults.update(values)
    status = NS(name='stretch_driver: Pimu/Device', hardware_id=hardware,
                values=[NS(key=key, value=value) for key,value in defaults.items()])
    return NS(header=NS(stamp=NS(sec=int(timestamp),nanosec=0)), status=[status])


class ChargingDiagnosticsTests(unittest.TestCase):
    def test_reads_separate_charge_sensor_independently_of_load(self):
        power=charging_measurement(diagnostic(),now=100.)
        self.assertEqual(power['charging_current'],2.)
        self.assertEqual(power['load_current'],5.)
        self.assertTrue(power['charging'])

    def test_missing_or_unmeasured_legacy_sensor_is_rejected(self):
        self.assertIsNone(charging_measurement(diagnostic(hardware='stretch-re2-1234'),now=100.))
        msg=diagnostic()
        msg.status[0].values=[value for value in msg.status[0].values if value.key!='current_charge']
        self.assertIsNone(charging_measurement(msg,now=100.))

    def test_stale_future_and_invalid_current_are_rejected(self):
        for args in ({'timestamp':96.},{'timestamp':101.},{'current_charge':'nan'},
                     {'current_charge':'inf'},{'current_charge':'-1'},{'current_charge':'100'},
                     {'charger_is_charging':'unknown'}):
            with self.subTest(args=args):
                self.assertIsNone(charging_measurement(diagnostic(**args),now=100.))

    def test_ambiguous_pimu_and_unrelated_diagnostics_are_rejected(self):
        msg=diagnostic()
        msg.status.append(msg.status[0])
        self.assertIsNone(charging_measurement(msg,now=100.))
        msg=diagnostic()
        msg.status[0].name='some_node: CPU'
        self.assertIsNone(charging_measurement(msg,now=100.))
