"""A fixed charging workflow using the normal executor, without LLM planning."""
from uuid import uuid4

from models import ExecutionContext, ExecutionPlan, ExecutionStatus, ToolCall
from workstation_common import get_logger

log=get_logger('automatic.charging')


async def run_automatic_charging(user, ticket):
    node=user.llm_node
    policy=node.battery_policy
    context=ExecutionContext('[System battery] Automatic return to charging station',request_id=str(uuid4()))
    context.planning_session_id=context.request_id
    context.request_source='automatic_charging'
    context.user_language=user.user_language
    node.begin_request(context,expected_generation=ticket['stop_generation'])
    user.response_generation=context.stop_generation
    user.executor.execution_context=context
    user.mcp.cancel_check=context.is_cancelled
    user.mcp.request_language=user.user_language
    user.mcp.request_id=context.request_id
    user.mcp.request_issued_at=context.issued_at
    user._start_live_session(context)
    steps,results=[],[]
    status=ExecutionStatus.FAILURE
    response=''
    reason='automatic_charging_failed'
    de=user.user_language=='de'

    def say(german,english):
        node.publish_message(german if de else english,user.user_language,generation=context.stop_generation)

    def data(result):
        raw=result.result if isinstance(result.result,dict) else {}
        value=raw.get('result',{})
        return value if isinstance(value,dict) else {}

    async def call(name,args=None):
        step=ToolCall(name,args or {})
        steps.append(step)
        user._record_live_plan(context,ExecutionPlan('Deterministic battery controller; no LLM invocation',[step]))
        user._mark_live_step_started(context,0,step)
        result=await user.executor.execute_tool_call(step)
        results.append(result)
        context.add_result(result)
        user._mark_live_step_completed(context,0,result)
        context.iteration+=1
        return result

    try:
        if context.is_cancelled() or not policy.valid_start(ticket['claim_id']):
            status,reason=ExecutionStatus.STOPPED,'battery_state_changed'
            return
        waypoints=await call('get_waypoints')
        if not waypoints.success:
            reason='waypoints_unavailable'
            response=('Ich kann die gespeicherten Ladeziele gerade nicht prüfen.' if de else
                'I cannot check the saved charging destination right now.')
            return
        # get_waypoints returns names, not waypoint records or coordinates.
        stations=[name for name in data(waypoints).get('waypoints',[])
            if isinstance(name,str) and ' '.join(name.split()).casefold()=='charging station']
        if len(stations)!=1:
            reason='charging_waypoint_missing' if not stations else 'charging_waypoint_ambiguous'
            if not stations:
                response=('Der Wegpunkt charging station ist noch nicht gespeichert. Bitte lege ihn für die automatische Ladefahrt an.' if de else
                    'The charging station waypoint is missing. Please save it before automatic charging return.')
            else:
                response=('Es gibt mehrere passende Lade-Wegpunkte. Bitte mache den Namen charging station eindeutig.' if de else
                    'Several charging waypoints match. Please make the charging station name unambiguous.')
            return
        state=await call('get_transport_state')
        info=data(state)
        if (not state.success or user.executor.recovery_required or
                info.get('controller_work_finished') is not True or info.get('driver_stop_acknowledged') is not True or
                info.get('joint_feedback_error') or info.get('travel_posture_error') or
                info.get('possession') not in {'empty','holding_assumed'} or info.get('charger_connected') is True):
            reason='robot_not_ready'
            response=('Ich bin noch nicht sicher fahrbereit. Bitte prüfe meinen Zustand vor der Ladefahrt.' if de else
                'I am not ready to drive safely. Please check my state before the charging return.')
            return
        if context.is_cancelled() or not policy.valid_start(ticket['claim_id']):
            status,reason=ExecutionStatus.STOPPED,'battery_state_changed'
            return
        say('Mein Akku ist länger als zehn Sekunden orange. Ich fahre zur Ladestation.',
            'My battery has been orange for over ten seconds. I am returning to the charging station.')
        policy.set_phase('navigating')
        navigation=await call('move_to_waypoint',{'name':stations[0]})
        if not navigation.success or context.is_cancelled():
            reason='navigation_failed'
            status=navigation.status
            response=('Die automatische Ladefahrt wurde nicht abgeschlossen. Ich wiederhole sie nicht automatisch.' if de else
                'The automatic charging return did not finish. I will not retry it automatically.')
            return
        snapshot=policy.snapshot()['battery']
        if not snapshot.get('fresh') or snapshot.get('charger_connected') is True or snapshot.get('level')=='red':
            status,reason=ExecutionStatus.STOPPED,'battery_state_changed'
            return
        policy.set_phase('docking')
        say('Ich bin am Ladepunkt und starte das Andocken.','I am at the charging waypoint and will dock now.')
        docking=await call('dock_robot')
        status=docking.status
        reason='charging_confirmed' if docking.success else 'docking_not_confirmed'
        response=(('Ich habe angedockt; der Ladezustand ist bestätigt.' if de else
            'I have docked; charging or a full battery is confirmed.') if docking.success else
            ('Das Andocken oder Laden konnte ich nicht bestätigen. Bitte prüfe den Anschluss. Ich wiederhole den Versuch nicht automatisch.' if de else
            'I could not confirm docking and charging. Please check the connection. I will not retry automatically.'))
    except Exception:
        log.exception('Deterministic automatic charging failed')
        reason='automatic_charging_exception'
        if policy.return_state in {'navigating','docking'}:
            node.request_stop(reason)
        response=('Die automatische Ladefahrt wurde wegen eines Fehlers beendet.' if de else
            'Automatic charging return ended because of an error.')
        return
    finally:
        if context.is_cancelled():status=ExecutionStatus.STOPPED
        policy.finish('completed' if status==ExecutionStatus.SUCCESS else
            'stopped' if status==ExecutionStatus.STOPPED else 'blocked',reason)
        user._record_completed_task(context.user_request,context.user_request,steps,results,
            response_text=response,status=status)
        user._finish_live_session(context,status,response_text=response)
        node.end_request(context)
        if response and not context.is_cancelled():
            node.publish_message(response,user.user_language,generation=context.stop_generation)
