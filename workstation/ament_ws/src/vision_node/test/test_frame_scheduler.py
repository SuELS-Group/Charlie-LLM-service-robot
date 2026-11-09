from vision_node.frame_scheduler import FrameRateLimiter, LatestFrameScheduler


def test_scheduler_replaces_stale_frame_for_same_camera():
    scheduler = LatestFrameScheduler(['head', 'gripper'])
    assert not scheduler.put('head', 'old')
    assert scheduler.put('head', 'new')
    assert scheduler.take(timeout=0) == ('head', 'new')
    assert scheduler.take(timeout=0) is None


def test_scheduler_preserves_camera_fairness():
    scheduler = LatestFrameScheduler(['head', 'gripper'])
    scheduler.put('head', 'head-1')
    scheduler.put('gripper', 'gripper-1')
    scheduler.put('head', 'head-2')
    assert scheduler.take(timeout=0) == ('head', 'head-2')
    assert scheduler.take(timeout=0) == ('gripper', 'gripper-1')


def test_rate_limiter_caps_secondary_camera_without_limiting_head():
    now = [0.0]
    limiter = FrameRateLimiter({'head': 0, 'gripper': 10}, clock=lambda: now[0])
    assert limiter.accept('head') is True
    assert limiter.accept('gripper') is True
    now[0] = 0.05
    assert limiter.accept('head') is True
    assert limiter.accept('gripper') is False
    now[0] = 0.10
    assert limiter.accept('gripper') is True
    now[0] = 1.0
    assert limiter.accept('gripper') is True
    now[0] = 1.05
    assert limiter.accept('gripper') is False


def test_rate_limiter_averages_ten_fps_from_fifteen_fps_input():
    now = [0.0]
    limiter = FrameRateLimiter({'gripper': 10}, clock=lambda: now[0])
    accepted = 0
    for frame_index in range(151):
        now[0] = frame_index / 15.0
        accepted += int(limiter.accept('gripper'))
    assert accepted == 101
