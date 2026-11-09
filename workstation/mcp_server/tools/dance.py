from mcp_server.decorators import ToolStatus, tool_result, tool_with_meta
from workstation_common import get_logger

log = get_logger('mcp.dance')


def _is_stopped(result: dict) -> bool:
    return (result or {}).get('status') == 'stopped'


@tool_with_meta(announcement='I\'ve finished my dance!', wait_duration=90.0, invalidates=['scene_objects',
                                                                                          'visible_objects'])
def perform_dance() -> dict:
    """
    Make the robot perform a short fun dance routine.
    The robot rocks forward and back, wiggles by turning left and right,
    and moves its head camera throughout to imitate dancing.
    Use when the user asks the robot to dance, celebrate, or show off.
    """
    # Lazy imports to avoid polluting the module namespace — the server registers
    # tools by checking hasattr(module, name), so top-level imports would cause
    # move_relative / turn_to_relative / move_head to be double-registered.
    from mcp_server.tools.navigation import move_relative, turn_to_relative
    from mcp_server.tools.perception import move_head

    steps_completed = 0

    def step(fn, **kwargs):
        nonlocal steps_completed
        result = fn(**kwargs)
        if _is_stopped(result):
            return result
        if result.get('status') != 'success':
            log.warning(f'Dance step {fn.__name__}({kwargs}) failed: {result.get("error")} — continuing')
        steps_completed += 1
        return None  # continue

    log.info('Starting dance routine')
    # Phase 1: Head nod (yes motion)
    for pan, tilt, dur in [(0.0, -0.35, 0.7), (0.0, 0.20, 0.7), (0.0, 0.00, 0.5), ]:
        r = step(move_head, pan=pan, tilt=tilt, duration=dur)
        if r is not None:
            return r
    # Phase 2: Head shake (no motion)
    for pan, tilt, dur in [(-0.8, 0.0, 0.6), (0.8, 0.0, 0.6), (0.0, 0.0, 0.5), ]:
        r = step(move_head, pan=pan, tilt=tilt, duration=dur)
        if r is not None:
            return r
    # Phase 3: Rock forward and back (once)
    for direction, distance in [('forward', 0.25), ('backward', 0.25), ]:
        r = step(move_relative, distance=distance, direction=direction)
        if r is not None:
            return r
    # Phase 4: Wiggle (turn left/right and back)
    for angle in [60, -120, 60]:
        r = step(turn_to_relative, angle=angle)
        if r is not None:
            return r
    # Phase 5: Head wave finale
    for pan, tilt, dur in [(-0.7, -0.2, 0.7), (0.7, 0.2, 0.7), (0.0, 0.0, 0.6), ]:
        r = step(move_head, pan=pan, tilt=tilt, duration=dur)
        if r is not None:
            return r
    log.info(f'Dance complete: {steps_completed} steps')
    return tool_result(ToolStatus.SUCCESS, result={
        'message': f'Dance complete! Performed {steps_completed} moves.',
        'steps_completed': steps_completed, })
