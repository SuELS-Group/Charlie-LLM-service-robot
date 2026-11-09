from unittest.mock import Mock

import pytest
from mcp_server.tools import navigation, manipulation
from mcp_server.call_state import operation_budget, remaining_budget
import mcp_server.call_state as state


@pytest.mark.parametrize('function,args', [
    (navigation.move_to, {'x': float('nan'), 'y': 1, 'orientation': 0}),
    (navigation.move_to, {'x': None, 'y': 1, 'orientation': 0}),
    (navigation.move_relative, {'distance': float('inf')}),
    (navigation.move_relative, {'distance': -1}),
    (navigation.drive_to_person, {'distance': .2}),
    (navigation.turn_to_relative, {'angle': 361}),
    (navigation.move_to_waypoint, {'name': ''}),
    (manipulation.take_from_hand, {'person': 123}),
    (manipulation.give_to_person, {'person': {'person': 'TestUserA'}}),
    (manipulation.give_to_person, {'person': 'TestUserA', 'unexpected': True}),
    (manipulation.take_from_hand, {'timeout_seconds': True}),
    (manipulation.place_item, {'height': 100}),
])
def test_invalid_physical_arguments_cannot_call_ros(monkeypatch, function, args):
    service = Mock(side_effect=AssertionError('No RPC allowed'))
    monkeypatch.setattr(navigation, 'call_service', service)
    monkeypatch.setattr(navigation, 'call_action', service)
    monkeypatch.setattr(manipulation, 'call_service', service)
    result = function(**args)
    assert result['status'] == 'failure' and result['error']['code'] == 'INVALID_ARGUMENTS'
    assert result['error']['retryable'] is False
    assert result['error']['execution_started'] is False
    assert result['error']['completion_confirmed'] is True
    assert result['error']['execution_uncertain'] is False
    service.assert_not_called()


def test_nested_budget_can_only_shorten_the_outer_deadline(monkeypatch):
    clock = [0.]
    monkeypatch.setattr(state, 'time', Mock(monotonic=lambda: clock[0]))
    with operation_budget('take_from_hand'):
        assert remaining_budget(999) == 240
        clock[0] = 200
        with operation_budget('drive_to_person'):
            assert remaining_budget(999) == 40
            clock[0] = 241
            assert remaining_budget(10) == 0
        assert remaining_budget(10) == 0
    assert remaining_budget(10) == 10


def test_service_budget_expired_during_availability_never_dispatches(monkeypatch):
    from types import SimpleNamespace
    from mcp_server import ros_utils
    from mcp_server.call_state import capture_call_faults
    client = SimpleNamespace(wait_for_service=lambda **_: True, call_async=Mock())
    monkeypatch.setattr(ros_utils, 'get_node', lambda: SimpleNamespace(create_client=lambda *_: client))
    monkeypatch.setattr(ros_utils, 'time', SimpleNamespace(monotonic=Mock(side_effect=[0, 2])))
    with capture_call_faults() as faults:
        result = ros_utils.call_service('/test', object, object(), timeout_sec=1)
    assert ros_utils.response_status(result) == ros_utils.STATUS_TIMEOUT
    assert faults[0]['execution_uncertain'] is False
    client.call_async.assert_not_called()
