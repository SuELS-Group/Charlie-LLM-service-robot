from types import SimpleNamespace

from command_node.navigation_readiness import occupancy_grid_is_usable


def _grid(width, height, cells):
    return SimpleNamespace(info=SimpleNamespace(width=width, height=height), data=[0] * cells)


def test_accepts_complete_nonempty_grid():
    assert occupancy_grid_is_usable(_grid(3, 2, 6))


def test_rejects_empty_grid():
    assert not occupancy_grid_is_usable(_grid(0, 0, 0))


def test_rejects_incomplete_grid():
    assert not occupancy_grid_is_usable(_grid(3, 2, 5))


def test_rejects_malformed_message():
    assert not occupancy_grid_is_usable(SimpleNamespace())
