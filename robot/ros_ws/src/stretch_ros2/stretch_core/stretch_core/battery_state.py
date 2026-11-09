"""Charging evidence without opening a Stretch device."""
from sensor_msgs.msg import BatteryState


def charging_status(pimu_status, board_info, filtered_state, previous_state):
    protocol=str(board_info.get('protocol_version',''))
    number=int(protocol[1:]) if protocol.startswith('p') and protocol[1:].isdigit() else -1
    connected=pimu_status.get('charger_connected')
    if not isinstance(connected,bool):
        return BatteryState.POWER_SUPPLY_STATUS_UNKNOWN
    if number>=5 and isinstance(pimu_status.get('charger_is_charging'),bool):
        if connected is not True:
            return BatteryState.POWER_SUPPLY_STATUS_DISCHARGING
        return (BatteryState.POWER_SUPPLY_STATUS_CHARGING if pimu_status['charger_is_charging'] else
            BatteryState.POWER_SUPPLY_STATUS_NOT_CHARGING)
    hardware=board_info.get('hardware_id')
    if hardware==0:
        return filtered_state if filtered_state!=BatteryState.POWER_SUPPLY_STATUS_UNKNOWN else previous_state
    if hardware in (1,2,3):
        if connected is False:
            return BatteryState.POWER_SUPPLY_STATUS_DISCHARGING
        if connected is True and filtered_state==BatteryState.POWER_SUPPLY_STATUS_CHARGING:
            return filtered_state
        if connected is False and filtered_state==BatteryState.POWER_SUPPLY_STATUS_DISCHARGING:
            return filtered_state
    return previous_state
