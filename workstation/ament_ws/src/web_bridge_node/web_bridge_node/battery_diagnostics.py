"""Read the separate Stretch 3 PIMU charging-current sensor from diagnostics."""
import math


def charging_measurement(message, *, now, max_age=3.):
    stamp = float(message.header.stamp.sec) + float(message.header.stamp.nanosec) * 1e-9
    if not -.5 <= now - stamp <= max_age:
        return None
    readings = []
    for status in message.status:
        # On older robots the field exists in software as an unmeasured zero.
        if not status.name.endswith(': Pimu/Device') or not status.hardware_id.startswith('stretch-se3-'):
            continue
        values = {entry.key: entry.value for entry in status.values}
        try:
            charge = float(values['current_charge'])
            load = float(values['current'])
        except (KeyError, ValueError, TypeError):
            continue
        if (not all(math.isfinite(value) and 0 <= value <= 20 for value in (charge, load))
                or values.get('charger_connected') not in {'True', 'False'}
                or values.get('charger_is_charging') not in {'True', 'False'}):
            continue
        readings.append({'sampled_at': stamp, 'charging_current': charge, 'load_current': load,
                         'charger_connected': values['charger_connected'] == 'True',
                         'charging': values['charger_is_charging'] == 'True',
                         'hardware_id': status.hardware_id})
    return readings[0] if len(readings) == 1 else None
