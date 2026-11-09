import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from command_node.command_node import CommandNode


def _pose(x=1.0, y=2.0):
    return SimpleNamespace(position=SimpleNamespace(x=x, y=y))


def test_absolute_turn_uses_one_map_pose_navigation_goal():
    node = SimpleNamespace(
        current_pose=_pose(),
        _prepare_turn_to=Mock(return_value=(True, 'Turn started', 90.0)),
        _reset_head_after_navigation=Mock(),
        _execute_navigation_action=AsyncMock(return_value='done'),
        _build_turn_to_result=Mock())
    goal = SimpleNamespace(request=SimpleNamespace(orientation=90.0))

    result = asyncio.run(CommandNode.execute_turn_to_action(node, goal))

    assert result == 'done'
    node._reset_head_after_navigation.assert_not_called()  # Head motion belongs to guarded execution.
    kwargs = node._execute_navigation_action.call_args.kwargs
    assert kwargs['action_name'] == 'turn_to'
    assert kwargs['goal_x'] == 1.0
    assert kwargs['goal_y'] == 2.0
    assert kwargs['goal_orientation'] == 90.0


def test_relative_turn_resolves_to_one_absolute_map_pose_goal():
    node = SimpleNamespace(
        current_pose=_pose(x=3.0, y=4.0),
        _prepare_turn_to_relative=Mock(
            return_value=(True, 'Turn started', 353.4)),
        _reset_head_after_navigation=Mock(),
        _execute_navigation_action=AsyncMock(return_value='done'),
        _build_turn_to_relative_result=Mock())
    goal = SimpleNamespace(request=SimpleNamespace(angle=90.0))

    result = asyncio.run(
        CommandNode.execute_turn_to_relative_action(node, goal))

    assert result == 'done'
    node._reset_head_after_navigation.assert_not_called()
    kwargs = node._execute_navigation_action.call_args.kwargs
    assert kwargs['action_name'] == 'turn_to_relative'
    assert kwargs['goal_x'] == 3.0
    assert kwargs['goal_y'] == 4.0
    assert kwargs['goal_orientation'] == 353.4
