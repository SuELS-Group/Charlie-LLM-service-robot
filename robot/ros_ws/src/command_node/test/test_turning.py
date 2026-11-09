import pytest
from command_node.turning import (normalize_heading_degrees,
                                  shortest_turn_degrees)


@pytest.mark.parametrize(('angle', 'expected'), [(0.0, 0.0), (360.0, 0.0), (-10.0, 350.0), (725.0, 5.0), ])
def test_normalize_heading_degrees(angle, expected):
    assert normalize_heading_degrees(angle) == pytest.approx(expected)


@pytest.mark.parametrize(('current', 'target', 'expected'), [(350.0, 10.0, 20.0),
    (10.0, 350.0, -20.0),
    (38.4, 255.1, -143.3),
    (0.0, 180.0, 180.0), ])
def test_shortest_turn_degrees(current, target, expected):
    assert shortest_turn_degrees(current, target) == pytest.approx(expected)
