import json

import pytest
from std_msgs.msg import String
from automatic_charging import run_automatic_charging
from test_clarification import make_context


def low_battery(node):
    clock=[0.]
    policy=node.battery_policy
    policy.clock=lambda:clock[0]
    policy.wall=lambda:1000.+clock[0]
    def update(level='orange',connected=False):
        event={'source':'battery_monitor','type':'battery_state','level':level,
            'issued_at':1000.+clock[0],'sampled_at':1000.+clock[0],
            'sample_valid':True,'charger_connected':connected,'voltage':10.98,'estimated_percentage':59.}
        node.battery_event_callback(String(data=json.dumps(event)))
    for second in range(11):clock[0]=float(second);update()
    return clock,update


def robot_ready():
    return {'controller_work_finished':True,'driver_stop_acknowledged':True,
        'possession':'holding_assumed','joint_feedback_error':None,'travel_posture_error':None}


@pytest.mark.asyncio
async def test_fixed_return_never_invokes_llm_and_is_visible_in_history(make_context):
    user,node,_,prompts=make_context([])
    low_battery(node)
    calls=[]
    async def call(name,args):
        calls.append((name,args))
        return {'status':'success','result':{'waypoints':['charging station']} if name=='get_waypoints' else
            robot_ready() if name=='get_transport_state' else {'completion_confirmed':True}}
    user.mcp.call_tool=call
    ticket=node.wait_for_user_message()
    await run_automatic_charging(user,ticket)
    assert calls==[('get_waypoints',{}),('get_transport_state',{}),
        ('move_to_waypoint',{'name':'charging station'}),('dock_robot',{})]
    assert prompts==[] and not user.conversation_history
    assert user.task_history[-1].original_request.startswith('[System battery]')
    assert node.battery_policy.return_state=='completed'
    assert not node.battery_policy.eligible()


@pytest.mark.asyncio
async def test_missing_station_is_spoken_once_without_navigation(make_context):
    user,node,_,_=make_context([])
    low_battery(node)
    calls=[]
    async def call(name,args):
        calls.append(name)
        return {'status':'success','result':{'waypoints':['grabbing point']}}
    user.mcp.call_tool=call
    await run_automatic_charging(user,node.wait_for_user_message())
    assert calls==['get_waypoints']
    assert node.battery_policy.return_reason=='charging_waypoint_missing'
    texts=[json.loads(item.data)['text'] for item in node.communication_publisher.published_messages]
    assert len(texts)==1 and 'charging station' in texts[0]
    assert node.battery_policy.claim() is None


@pytest.mark.asyncio
@pytest.mark.parametrize('fault',['cable','posture','recovery','navigation'])
async def test_preconditions_and_failed_navigation_never_start_docking(make_context,fault):
    user,node,_,_=make_context([])
    clock,update=low_battery(node)
    calls=[]
    async def call(name,args):
        calls.append(name)
        if name=='get_waypoints':return {'status':'success','result':{'waypoints':['charging station']}}
        if name=='get_transport_state':
            info=robot_ready()
            if fault=='posture':info['travel_posture_error']='extended arm'
            if fault=='recovery':user.executor.recovery_required=True
            if fault=='cable':clock[0]+=1;update(connected=True)
            return {'status':'success','result':info}
        return {'status':'failure','error':{'code':'NAV_FAILED','message':'blocked route','execution_uncertain':False}}
    user.mcp.call_tool=call
    await run_automatic_charging(user,node.wait_for_user_message())
    assert 'dock_robot' not in calls
    assert ('move_to_waypoint' in calls)==(fault=='navigation')
    assert node.battery_policy.claim() is None


def test_user_request_is_not_preempted_by_due_battery_return(make_context):
    user,node,_,_=make_context([])
    low_battery(node)
    node.robot_message_callback(String(data=json.dumps({'text':'Charlie, hallo','lang':'de'})))
    assert isinstance(node.wait_for_user_message(),str)
    assert node.battery_policy.claim_id is None


@pytest.mark.asyncio
async def test_charger_contact_does_not_stop_inflight_navigation_or_redock(make_context):
    user,node,_,_=make_context([])
    clock,update=low_battery(node)
    calls=[]
    async def call(name,args):
        calls.append(name)
        if name=='get_waypoints':return {'status':'success','result':{'waypoints':['charging station']}}
        if name=='get_transport_state':return {'status':'success','result':robot_ready()}
        clock[0]+=1;update(connected=True)
        node.battery_tick()
        assert not user.executor.execution_context.is_cancelled()
        return {'status':'success','result':{'completion_confirmed':True,'execution_uncertain':False}}
    user.mcp.call_tool=call
    await run_automatic_charging(user,node.wait_for_user_message())
    assert 'move_to_waypoint' in calls and 'dock_robot' not in calls
    assert len(node.stop_publisher.published_messages)==0
    assert node.battery_policy.return_state=='stopped' and node.battery_policy.claim() is None


@pytest.mark.parametrize('mode', ['reduced', 'full', 'off'])
def test_charging_battery_events_never_speak(make_context, mode):
    _, node, _, _ = make_context([])
    node.battery_announcement_mode_callback(String(data=mode))
    clock, update = low_battery(node)
    for level in ['green', 'yellow', 'orange', 'red']:
        clock[0] += 1
        update(level, connected=True)
        node.battery_tick()
    assert not node.communication_publisher.published_messages


def test_mode_change_discards_pending_notices_and_off_mutes_legacy_events(make_context):
    _, node, _, _ = make_context([])
    low_battery(node)
    node.battery_announcement_mode_callback(String(data='off'))
    assert node.battery_policy.notice is None
    assert node.battery_policy.legacy_notice('red') is None
    node.battery_announcement_mode_callback(String(data='reduced'))
    assert node.battery_policy.take_notice() is None
    node.battery_announcement_mode_callback(String(data='invalid'))
    assert node.battery_policy.announcements.mode == 'reduced'
