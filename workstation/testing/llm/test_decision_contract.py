import json
from types import SimpleNamespace

import pytest
from llm_test_helpers import _workspace
from agent import Agent
from decision_contract import decision_schema, validation_error
from llm_client import LLMClient
from models import ExecutionContext, ExecutionStatus, ToolResult
from planner_telemetry import PlannerTelemetryClient
from tool_catalog import ToolCatalog


def catalog():
    return ToolCatalog([{'name': 'move_relative', 'parameters': [
        {'name': 'distance', 'type': 'float', 'required': True},
        {'name': 'direction', 'type': 'str', 'required': False, 'default': 'forward'}]},
        {'name': 'battery_status'}, {'name': 'turn_to_relative', 'parameters': [
            {'name': 'angle', 'type': 'int'}, {'name': 'degrees', 'type': 'int'}]}])


@pytest.mark.parametrize('payload', [
    {'action': 'call_tool'},
    {'action': 'call_tool', 'tool': 'invented', 'args': {}},
    {'action': 'call_tool', 'tool': 'move_relative', 'args': {'distance': True}},
    {'action': 'call_tool', 'tool': 'move_relative', 'args': {'distance': float('nan')}},
    {'action': 'call_tool', 'tool': 'move_relative', 'args': {'distance': 2, 'extra': 1}},
    {'action': 'ask_user'},
])
def test_incomplete_and_invalid_decisions_are_blocked(payload):
    assert validation_error(payload, decision_schema(catalog().tools))


@pytest.mark.parametrize('tool,args', [('get_waypoints', {}), ('move_to_waypoint', {'name': 'desk'})])
def test_generation_binds_every_tool_call_to_the_current_goal(tool, args):
    active_id = 'current-goal'
    requests=[]
    payload={'action':'call_tool', 'tool':tool, 'args':args, 'task_id':active_id}
    def chat(**kwargs):
        requests.append(kwargs)
        return SimpleNamespace(message=SimpleNamespace(content=json.dumps(payload)))
    tools=ToolCatalog([{'name':'get_waypoints'}, {'name':'move_to_waypoint','parameters':[
        {'name':'name','type':'str','required':True}]}])
    llm=LLMClient(SimpleNamespace(chat=chat));llm.bind_tools(tools.tools)
    state={'task_progress':[
        {'id':'1','completion_tool':'move_relative','status':'completed'},
        {'id':'2','completion_tool':'battery_status','status':'ready'},
        {'id':active_id,'completion_tool':'move_to_waypoint','status':'in_progress'}]}
    assert llm.decision([{'role':'user','content':json.dumps(state)}],
        ['move_to_waypoint','get_waypoints']).decision.task_id==active_id
    schema=requests[0]['format']
    for branch in schema['oneOf']:
        if branch['properties']['action']['const']=='call_tool':
            assert branch['properties']['task_id']=={'const':active_id}
    assert not validation_error(payload,schema)
    assert 'task_id' in validation_error({**payload,'task_id':'1'},schema)


@pytest.mark.asyncio
async def test_scan_position_place_sequence_rejects_old_id_before_dispatch_then_continues_once():
    tools=ToolCatalog([
        {'name':'get_look_around_objects'},
        {'name':'position_for_action','parameters':[{'name':'target','type':'str','required':True}]},
        {'name':'place_item','parameters':[{'name':'surface','type':'str','required':True}],
         'meta':{'workflow_before':['position_for_action']}}])
    plan=[{'id':str(i),'label':name,'completion_tool':name} for i,name in enumerate(
        ('get_look_around_objects','position_for_action','place_item'),1)]
    place={'action':'call_tool','tool':'place_item','args':{'surface':'trash_can'},'task_id':'3'}
    replies=iter([
        {'action':'select_tools','query':'trash_can','task_plan':plan},
        {'action':'call_tool','tool':'get_look_around_objects','args':{},'task_id':'1'},
        {'action':'call_tool','tool':'position_for_action','args':{'target':'trash_can'},'task_id':'2'},
        {**place,'task_id':'1'},place,{'action':'finish','answer':'Done'}])
    requests=[];calls=[]
    def chat(**kwargs):
        requests.append(kwargs)
        return SimpleNamespace(message=SimpleNamespace(content=json.dumps(next(replies))))
    async def execute(call):
        calls.append(call)
        return ToolResult(call.name,ExecutionStatus.SUCCESS,args=call.args)
    agent=Agent(LLMClient(SimpleNamespace(chat=chat)),tools,
        SimpleNamespace(execute_tool_call=execute),PlannerTelemetryClient(enabled=False))
    context=ExecutionContext('Charlie, wirf das Item in den Mülleimer.')
    try:
        assert (await agent.run(context))[0]==ExecutionStatus.SUCCESS
        assert [(c.name,c.task_id) for c in calls]==[
            ('get_look_around_objects','1'),('position_for_action','2'),('place_item','3')]
        assert len(context.planning_attempts)==1
        assert 'task_id' in context.planning_attempts[0]['message']
        for index,expected in [(1,'1'),(2,'2'),(3,'3'),(4,'3')]:
            branches=requests[index]['format']['oneOf']
            assert all(b['properties']['task_id']=={'const':expected} for b in branches
                if b['properties']['action']['const']=='call_tool')
        assert all(t['status']=='completed' for t in context.task_progress)
    finally:agent.close()


@pytest.mark.asyncio
async def test_invalid_decision_is_corrected_without_executing_partial_call():
    responses = iter([{'action': 'call_tool'},
        {'action': 'call_tool', 'tool': 'move_relative', 'args': {'distance': 2}, 'task_id': 'move',
            'task_plan': [{'id': 'move', 'label': 'Move two meters', 'completion_tool': 'move_relative'}]},
        {'action': 'finish', 'answer': 'Done'}])
    prompts, calls = [], []
    def chat(**kwargs):
        prompts.append(kwargs)
        return SimpleNamespace(message=SimpleNamespace(content=json.dumps(next(responses))))
    async def execute(call):
        calls.append(call)
        return ToolResult(call.name, ExecutionStatus.SUCCESS, args=call.args)
    agent = Agent(LLMClient(SimpleNamespace(chat=chat)), catalog(),
        SimpleNamespace(execute_tool_call=execute), PlannerTelemetryClient(enabled=False))
    try:
        status, _, _, _ = await agent.run(ExecutionContext('Move relative forward two meters'))
        assert status == ExecutionStatus.SUCCESS
        assert len(calls) == 1 and calls[0].args == {'distance': 2}
        assert 'Invalid planning output' in prompts[1]['messages'][-1]['content']
        branch = next(b for b in prompts[0]['format']['oneOf'] if b['properties'].get('tool') == {'const': 'move_relative'})
        assert branch['required'] == ['action', 'tool', 'args', 'task_id', 'task_plan']
        assert 'distance' in branch['properties']['args']['required']
    finally:
        agent.close()
