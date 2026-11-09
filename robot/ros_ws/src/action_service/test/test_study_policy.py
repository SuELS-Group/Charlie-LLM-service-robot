from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from robot_common.study_policy import StudyPolicy
from action_service.action_service_node import ActionServiceNode
from action_interfaces.srv import ExecuteAction


def profile():
    return {'waypoints': {'Service A': {'x': 1., 'y': 2., 'orientation': 0.},
                         'Service B': {'x': 5., 'y': 2., 'orientation': 180.}},
        'approved_objects': ['cup', 'box'], 'active_object': 'cup',
        'approved_routes': [['Service A', 'Service B']], 'service_radius_m': .75}


@pytest.mark.parametrize('tool', ['delete_waypoint', 'move_to', 'release_item', 'grab_item',
    'start_following', 'calibrate_arm', 'store_memory', 'set_slam_mode'])
def test_study_excludes_unreviewed_actions(tool):
    assert StudyPolicy(profile(), 'study').tool_error(tool, {}, tool)[0] == 'POLICY_DENIED'


def test_profile_has_no_fixed_limit_on_waypoints_or_objects_and_normalizes_names():
    config = profile()
    config['waypoints']['Third Point'] = {'x': 10., 'y': 2., 'orientation': 0.}
    policy = StudyPolicy(config, 'study')
    assert len(policy.waypoints) == 3 and len(policy.objects) == 2
    assert policy.tool_error('move_to_waypoint', {'name': 'SERVICE A'}) is None
    assert policy.tool_error('take_from_hand', {'item': 'Cup'}) is None
    assert policy.tool_error('take_from_hand', {'item': 'heavy vase'})[0] == 'OBJECT_NOT_APPROVED'


def test_missing_configuration_blocks_physical_trials_but_allows_questions():
    policy = StudyPolicy({}, 'study')
    assert not policy.summary()['configured']
    assert policy.tool_error('take_from_hand', {})[0] == 'STUDY_PROFILE_NOT_READY'
    assert policy.tool_error('query_knowledge_base', {}) is None
    assert policy.tool_error('get_transport_state', {}) is None


def test_direct_person_approach_is_private_to_handover_workflows():
    policy = StudyPolicy(profile(), 'study')
    assert policy.tool_error('drive_to_person', {}, 'drive_to_person')
    assert policy.tool_error('drive_to_person', {}, 'take_from_hand') is None


@pytest.mark.parametrize('change', ['moved_goal', 'changed_heading', 'unapproved_name', 'unapproved_return',
    'start_outside', 'nan_goal', 'missing_pose'])
def test_base_admission_rejects_changed_waypoints_and_unapproved_routes(change):
    policy = StudyPolicy(profile(), 'study')
    params = dict(action='move_to_waypoint', root='move_to_waypoint', target='service b',
                  current=[1., 2., 0.], goal=[5., 2., 180.])
    if change == 'moved_goal': params['goal'][0] = 6.
    if change == 'changed_heading': params['goal'][2] = 90.
    if change == 'unapproved_name': params['target'] = 'elsewhere'
    if change == 'unapproved_return': params.update(target='service a', current=[5., 2., 180.], goal=[1., 2., 0.])
    if change == 'start_outside': params['current'] = [20., 20., 0.]
    if change == 'nan_goal': params['goal'][0] = float('nan')
    if change == 'missing_pose': params['current'] = []
    assert policy.base_error(**params)


def test_approved_route_and_local_person_approach_pass():
    policy = StudyPolicy(profile(), 'study')
    assert policy.base_error(action='move_to_waypoint', root='move_to_waypoint', target='service b',
        current=[1., 2., 0.], goal=[5., 2., 180.]) is None
    assert policy.base_error(action='move_to', root='take_from_hand', target='',
        current=[1., 2., 0.], goal=[1.3, 2., 10.]) is None
    assert policy.base_error(action='move_to', root='take_from_hand', target='',
        current=[1., 2., 0.], goal=[3., 2., 10.])


def test_development_inventory_remains_available():
    policy = StudyPolicy({}, 'development')
    assert policy.tool_error('grab_item', {'item': 'bottle'}) is None
    assert policy.action_error('release_item', {}) is None


def test_configured_german_object_alias_is_accepted_without_enabling_other_objects():
    config = profile()
    config['object_aliases'] = {'cup': ['Tasse', 'mug']}
    policy = StudyPolicy(config, 'study')
    assert policy.tool_error('take_from_hand', {'item': 'tasse'}) is None
    assert policy.tool_error('take_from_hand', {'item': 'heavy vase'})[0] == 'OBJECT_NOT_APPROVED'


@pytest.mark.parametrize('timeout', [True, float('inf'), -1, 1000])
def test_direct_robot_handover_arguments_rejected_before_handler(timeout):
    import json
    from test_action_admission import node
    n = node()
    handler = Mock()
    n._action_handlers['take_from_hand'] = handler
    result = ActionServiceNode.handle_action(n, SimpleNamespace(action_type='take_from_hand',
        parameters=json.dumps({'timeout_seconds': timeout})), ExecuteAction.Response())
    assert result.status.status != 1
    handler.assert_not_called()


def test_direct_robot_actions_cannot_bypass_study_profile():
    from test_action_admission import node
    n = node()
    n.study_policy = StudyPolicy(profile(), 'study')
    n._action_handlers['release_item'] = Mock()
    result = ActionServiceNode.handle_action(n, SimpleNamespace(action_type='release_item', parameters='{}'), ExecuteAction.Response())
    assert result.status.status != 1
    n._action_handlers['release_item'].assert_not_called()
