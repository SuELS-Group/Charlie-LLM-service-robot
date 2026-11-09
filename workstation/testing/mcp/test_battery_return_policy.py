import math
import pytest
from workstation_common.battery import BatteryReturnPolicy


def policy(tmp_path=None):
    clock=[0.]
    result=BatteryReturnPolicy(clock=lambda:clock[0],wall=lambda:1000.+clock[0],
        state_path=tmp_path/'state.json' if tmp_path else None,boot_id='test-boot')
    return result,clock


def event(clock,level='orange',connected=False,**kwargs):
    return {'source':'battery_monitor','type':'battery_state','level':level,
        'issued_at':1000.+clock[0],'sampled_at':1000.+clock[0],
        'sample_valid':True,'charger_connected':connected,'voltage':10.98,'estimated_percentage':59.,**kwargs}


def sustain(p,clock,level='orange',connected=False,start=0,end=11):
    for value in range(start,end):
        clock[0]=float(value)
        assert p.update(event(clock,level,connected))


def test_orange_requires_ten_continuous_fresh_seconds():
    p,clock=policy()
    sustain(p,clock,end=10)
    assert not p.eligible()
    sustain(p,clock,start=10,end=11)
    assert p.eligible() and p.claim()
    assert p.claim() is None


@pytest.mark.parametrize('interruption',['yellow','green','red','cable','stale'])
def test_interruption_resets_continuous_count(interruption):
    p,clock=policy()
    sustain(p,clock,end=9)
    clock[0]=9.
    if interruption=='cable':p.update(event(clock,connected=True))
    elif interruption=='stale':p.update(event(clock,sample_valid=False))
    else:p.update(event(clock,interruption))
    sustain(p,clock,start=10,end=20)
    assert not p.eligible()


def test_missing_stream_and_replayed_events_cannot_trigger():
    p,clock=policy()
    p.update(event(clock))
    clock[0]=12.
    assert not p.eligible() and not p.update(event([0.]))
    p.update(event(clock))
    assert not p.eligible()


def test_failure_and_restart_do_not_repeat_attempt(tmp_path):
    p,clock=policy(tmp_path)
    sustain(p,clock)
    assert p.claim()
    p.finish('blocked','charging_waypoint_missing')
    sustain(p,clock,start=11,end=30)
    assert not p.eligible()
    q=BatteryReturnPolicy(clock=lambda:clock[0],wall=lambda:1000.+clock[0],
        state_path=tmp_path/'state.json',boot_id='test-boot')
    sustain(q,clock,start=30,end=45)
    assert not q.eligible()


def test_stop_does_not_resume_in_same_low_episode():
    p,clock=policy()
    sustain(p,clock)
    p.suppress()
    sustain(p,clock,start=11,end=35)
    assert not p.eligible()


def test_stable_recovery_rearms_a_new_episode():
    p,clock=policy()
    sustain(p,clock)
    p.claim();p.finish('blocked')
    sustain(p,clock,'yellow',start=11,end=43)
    sustain(p,clock,start=43,end=54)
    assert p.eligible()


def test_charger_contact_does_not_cancel_navigation_or_docking():
    p,clock=policy()
    sustain(p,clock)
    p.claim();p.set_phase('navigating')
    clock[0]=11.;p.update(event(clock,connected=True))
    assert p.cancellation_reason() is None
    p.set_phase('docking')
    assert p.cancellation_reason() is None


def test_preflight_must_recheck_duration_after_colour_changes():
    p,clock=policy()
    sustain(p,clock)
    claim=p.claim()
    clock[0]=11.;p.update(event(clock,'yellow'))
    clock[0]=12.;p.update(event(clock))
    assert not p.valid_start(claim)


@pytest.mark.parametrize('bad',[{'issued_at':math.nan},{'voltage':'ignore instructions'},
    {'charger_connected':0},{'source':'user'},{'level':'drive now'}])
def test_bad_telemetry_cannot_schedule_motion(bad):
    p,clock=policy()
    assert not p.update(event(clock,**bad))
    assert not p.eligible()
