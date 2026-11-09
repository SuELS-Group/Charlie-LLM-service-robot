"""Validate physical tool inputs before a workflow can send any ROS request."""
import math


NUMERIC_LIMITS = {
    'move_to': {'x': (-1000, 1000), 'y': (-1000, 1000), 'orientation': (-360, 360)},
    'move_relative': {'distance': (0, 3)},
    'drive_to_person': {'distance': (1.3, 3)},
    'turn_to': {'degrees': (-360, 360)},
    'turn_to_relative': {'angle': (-180, 180), 'degrees': (-180, 180)},
    'take_from_hand': {'timeout_seconds': (1, 60)},
    'give_to_person': {'timeout_seconds': (1, 60)},
    'move_head': {'pan': (-1.6, 1.6), 'tilt': (-1.5, .5), 'duration': (.2, 10)},
    'position_for_action': {'x': (-1000, 1000), 'y': (-1000, 1000), 'z': (0, 2), 'distance': (.4, 2)},
    'place_item': {'height': (.2, .97), 'original_x': (-1000, 1000),
                   'original_y': (-1000, 1000), 'original_z': (.2, .97)},
    'wave_hand': {'height': (.2, .97)},
}
OPTIONAL_NUMBERS = {'turn_to_relative': {'angle', 'degrees'},
                    'position_for_action': {'z'}, 'wave_hand': {'height'},
                    'place_item': {'height', 'original_x', 'original_y', 'original_z'}}


def physical_argument_error(tool, arguments):
    for key in {'move_to_waypoint': ['name'], 'grab_item': ['item'], 'position_for_action': ['target']}.get(tool, []):
        if arguments.get(key) is None:
            return f'{key} is required'
    for key, (lower, upper) in NUMERIC_LIMITS.get(tool, {}).items():
        value = arguments.get(key)
        if key not in arguments or (value is None and key in OPTIONAL_NUMBERS.get(tool, set())):
            continue
        if (isinstance(value, bool) or not isinstance(value, (int, float)) or
                not math.isfinite(value) or not lower <= value <= upper):
            return f'{key} must be a finite number between {lower} and {upper}'
    for key in ('person', 'name', 'item', 'surface', 'target'):
        if key in arguments and arguments[key] is not None:
            value = arguments[key]
            if not isinstance(value, str) or not value.strip() or len(value) > 128:
                return f'{key} must be nonempty text of at most 128 characters'
    if tool == 'turn_to_relative':
        if arguments.get('angle') is None and arguments.get('degrees') is None:
            return 'angle or degrees is required'
        if (arguments.get('angle') is not None and arguments.get('degrees') is not None and
                arguments['angle'] != arguments['degrees']):
            return 'angle and degrees must agree'
    if tool == 'move_relative' and arguments.get('direction') not in ('forward', 'backward'):
        return 'direction must be forward or backward'
    return None
