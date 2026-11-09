import math


def normalize_heading_degrees(angle: float) -> float:
    """Return an absolute heading in the half-open range [0, 360)."""
    return float(angle) % 360.0


def shortest_turn_degrees(current_heading: float, target_heading: float) -> float:
    """Return the shortest signed in-place rotation to an absolute heading."""
    delta = (
                    normalize_heading_degrees(target_heading) - normalize_heading_degrees(current_heading) + 180.0) % 360.0 - 180.0
    # Make a positive 180 degree request turn left rather than unexpectedly
    # changing its sign at the wrap boundary.
    if math.isclose(delta, -180.0, abs_tol=1e-9):
        return 180.0
    return delta
