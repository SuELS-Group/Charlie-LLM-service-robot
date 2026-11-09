from types import SimpleNamespace
from unittest.mock import Mock

from mcp_server import ros_utils
from mcp_server.call_state import capture_call_faults
from mcp_server.tools import manipulation


def pending_node():
    future = SimpleNamespace(done=lambda: False, cancel=Mock())
    client = SimpleNamespace(wait_for_service=lambda **_: True, call_async=Mock(return_value=future))
    node = SimpleNamespace(create_client=lambda *_: client)
    return node, client, future


def test_service_wait_timeout_is_typed_and_retains_uncertainty(monkeypatch):
    node, client, future = pending_node()
    monkeypatch.setattr(ros_utils, 'get_node', lambda: node)
    monkeypatch.setattr(ros_utils, 'time', SimpleNamespace(monotonic=Mock(side_effect=[0, 0, 2]), sleep=lambda _: None))
    with capture_call_faults() as faults:
        response = ros_utils.call_service('/execute_action', object, SimpleNamespace(parameters='{}'), timeout_sec=1)
    assert ros_utils.response_status(response) == ros_utils.STATUS_TIMEOUT
    assert faults[0]['execution_uncertain'] is True
    future.cancel.assert_called_once()
    assert client.call_async.call_count == 1


def test_tool_wrapper_preserves_service_timeout_and_blocks_nested_reentry(monkeypatch):
    node, client, _ = pending_node()
    monkeypatch.setattr(ros_utils, 'get_node', lambda: node)
    monkeypatch.setattr(ros_utils, 'time', SimpleNamespace(monotonic=Mock(side_effect=[0, 0, 200]), sleep=lambda _: None))
    result = manipulation.grab_item('bottle')
    assert result['status'] == 'timeout'
    assert result['error']['execution_uncertain']
    assert result['error']['retryable'] is False
    with capture_call_faults():
        from mcp_server.call_state import record_call_fault
        record_call_fault({'execution_uncertain': True})
        ros_utils.call_service('/execute_action', object, object())
    assert client.call_async.call_count == 1


def test_state_rpc_timeout_does_not_claim_unknown_motor_motion(monkeypatch):
    node, client, _ = pending_node()
    monkeypatch.setattr(ros_utils, 'get_node', lambda: node)
    monkeypatch.setattr(ros_utils, 'time', SimpleNamespace(monotonic=Mock(side_effect=[0, 0, 2]), sleep=lambda _: None))
    with capture_call_faults() as faults:
        result = ros_utils.call_service('/k2/object_state', object, object(), timeout_sec=1.)
    assert ros_utils.response_status(result) == ros_utils.STATUS_TIMEOUT
    assert faults and faults[0]['execution_uncertain'] is False
    client.call_async.assert_called_once()
