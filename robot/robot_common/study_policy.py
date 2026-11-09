"""Deterministic K2 study admission. ROS clients are trusted system components."""
import math
import os
from types import SimpleNamespace

READ_TOOLS = frozenset({'current_time', 'current_date', 'battery_status', 'get_waypoints',
    'get_transport_state', 'query_knowledge_base', 'speak', 'get_tools_registry',
    'list_categories', 'list_tools_by_category', 'get_study_profile', 'ping'})
STUDY_TOOLS = READ_TOOLS | {'take_from_hand', 'give_to_person', 'move_to_waypoint', 'stop_following',
                          'report_empty_gripper', 'report_holding_gripper'}
HANDOFFS = frozenset({'take_from_hand', 'give_to_person'})


def plain(value):
    if isinstance(value, SimpleNamespace):
        return {k: plain(v) for k, v in vars(value).items() if not k.startswith('_')}
    if isinstance(value, dict):
        return {k: plain(v) for k, v in value.items()}
    if isinstance(value, list):
        return [plain(v) for v in value]
    return value


def operation_mode():
    mode = os.environ.get('ROBOT_OPERATION_MODE', 'development')
    if mode not in {'development', 'study'}:
        raise ValueError('ROBOT_OPERATION_MODE must be development or study')
    return mode


class StudyPolicy:
    def __init__(self, config, mode=None):
        self.mode = operation_mode() if mode is None else mode
        if self.mode not in {'development', 'study'}:
            raise ValueError('Invalid robot mode')
        self.config = plain(config)
        self.waypoints = self.config.get('waypoints', {})
        self.objects = self.config.get('approved_objects', [])
        self.object_aliases = self.config.get('object_aliases', {})
        self.active_object = self.config.get('active_object', '')
        self.routes = self.config.get('approved_routes', [])
        self.radius = float(self.config.get('service_radius_m', .75))
        if not math.isfinite(self.radius) or not .1 <= self.radius <= 1.:
            raise ValueError('service_radius_m must be 0.1–1.0 m')
        if not isinstance(self.waypoints, dict) or not isinstance(self.objects, list):
            raise ValueError('Invalid study waypoint/object configuration')
        normalized = {str(name).strip().lower(): point for name, point in self.waypoints.items()}
        if len(normalized) != len(self.waypoints):
            raise ValueError('Approved waypoint names must be distinct after normalization')
        self.waypoints = normalized
        for name, point in self.waypoints.items():
            if not isinstance(name, str) or not name.strip() or not isinstance(point, dict):
                raise ValueError('Every approved waypoint needs a name and pinned pose')
            if not all(isinstance(point.get(k), (int, float)) and not isinstance(point[k], bool)
                       and math.isfinite(point[k]) for k in ('x', 'y', 'orientation')):
                raise ValueError(f'Invalid approved pose for {name}')
        if any(not isinstance(item, str) or not item.strip() for item in self.objects):
            raise ValueError('Approved objects must be nonempty identifiers')
        if not isinstance(self.object_aliases, dict) or any(key not in self.objects or
                not isinstance(aliases, list) or any(not isinstance(alias, str) or not alias.strip() for alias in aliases)
                for key, aliases in self.object_aliases.items()):
            raise ValueError('Object aliases must refer to approved objects')
        if isinstance(self.routes, list):
            self.routes = [[name.strip().lower() if isinstance(name, str) else name for name in route]
                           if isinstance(route, list) else route for route in self.routes]
        if not isinstance(self.routes, list) or any(not isinstance(route, list) or len(route) != 2 or
                any(name not in self.waypoints for name in route) for route in self.routes):
            raise ValueError('Approved routes must contain pairs of approved service-point names')

    def summary(self):
        return {'mode': self.mode, 'allowed_tools': sorted(STUDY_TOOLS) if self.mode == 'study' else None,
            'approved_waypoints': self.waypoints, 'approved_objects': self.objects,
            'object_aliases': self.object_aliases,
            'active_object': self.active_object, 'service_radius_m': self.radius,
            'approved_routes': self.routes,
            'possession_basis': 'workflow_assumption', 'configured': bool(self.waypoints and
                self.active_object in self.objects), 'map_name': self.config.get('map_name', '')}

    def tool_error(self, tool, args, root=None):
        if self.mode != 'study':
            return None
        internal = root in HANDOFFS and tool == 'drive_to_person'
        if tool not in STUDY_TOOLS and not internal:
            return 'POLICY_DENIED', 'This action is not enabled in the study profile'
        if tool in HANDOFFS | {'move_to_waypoint', 'drive_to_person'}:
            if not self.waypoints:
                return 'STUDY_PROFILE_NOT_READY', 'No service points have been approved'
            if self.active_object not in self.objects:
                return 'STUDY_PROFILE_NOT_READY', 'Select an approved test object before physical trials'
        if tool == 'move_to_waypoint' and str(args.get('name', '')).strip().lower() not in self.waypoints:
            return 'WAYPOINT_NOT_APPROVED', 'The destination is not an approved service point'
        if tool == 'take_from_hand' and args.get('item') is not None:
            names = [self.active_object, *self.object_aliases.get(self.active_object, [])]
            if str(args['item']).strip().casefold() not in {name.strip().casefold() for name in names}:
                return 'OBJECT_NOT_APPROVED', 'This object is not selected for the current study trial'
        return None

    def nearest_service(self, x, y):
        if not all(math.isfinite(v) for v in (x, y)):
            return None
        names = [name for name, p in self.waypoints.items()
                 if math.hypot(x - p['x'], y - p['y']) <= self.radius]
        return min(names, key=lambda name: math.hypot(x - self.waypoints[name]['x'],
                                                    y - self.waypoints[name]['y'])) if names else None

    def base_error(self, *, action, root, target, goal, current, internal=False):
        if not isinstance(goal, (list, tuple)) or not isinstance(current, (list, tuple)) or len(goal) != 3 or len(current) != 3:
            return 'Current and target navigation poses are required'
        if not all(isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)
                   for v in (*goal, *current)):
            return 'Navigation needs finite current and target coordinates'
        if self.mode != 'study':
            return None
        if not self.summary()['configured']:
            return 'Study profile is not configured for transport'
        if action == 'move_to_waypoint':
            expected = self.waypoints.get(target)
            if not expected:
                return 'Destination is not an approved service point'
            heading_error = abs((goal[2] - expected['orientation'] + 180) % 360 - 180)
            if math.hypot(goal[0] - expected['x'], goal[1] - expected['y']) > .05 or heading_error > 5.:
                return 'Saved waypoint has changed since route approval'
            start = self.nearest_service(*current[:2])
            if start is None:
                return 'Start from an approved service point before a study route'
            if start != target and [start, target] not in self.routes:
                return 'This service-point route has not been approved'
            return None
        if root in HANDOFFS and action in {'move_to', 'turn_to', 'turn_to_relative'}:
            start = self.nearest_service(*current[:2])
            if start is None or self.nearest_service(*goal[:2]) != start:
                return 'Person approach must stay at the same approved service point'
            return None
        if internal and action in {'turn_to', 'turn_to_relative'}:
            if self.nearest_service(*current[:2]) is not None:
                return None
        return 'Free base movement is not enabled in the study profile'

    def action_error(self, action, params):
        if self.mode != 'study':
            return None
        root = params.get('_request_tool', action)
        if action in HANDOFFS:
            return self.tool_error(action, params, root)
        if action in {'get_visible_objects', 'get_tracked_people', 'get_look_around_objects'} and root in HANDOFFS:
            return None
        return 'POLICY_DENIED', 'This robot action is not enabled in the study profile'
