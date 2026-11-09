import json
from pathlib import Path

from mcp_server.decorators import ToolStatus, tool_result, tool_with_meta


def _get_users_db_path() -> Path:
    # Resolve the path to the workspace controller config directory
    return Path(__file__).resolve().parent.parent.parent.parent / 'controller' / 'config' / 'users_db.json'


@tool_with_meta(wait_duration=2.0, provides=[{'state': 'persons', 'path': 'result.persons[]'}])
def get_persons() -> dict:
    """
    Return all saved person names from the user profile database.
    Use when you need to look up a person's details.
    """
    try:
        db_path = _get_users_db_path()
        if not db_path.exists():
            return tool_result(ToolStatus.SUCCESS, result={'count': 0, 'persons': []})
        with db_path.open('r', encoding='utf-8') as f:
            data = json.load(f)
        names = list(data.get('users', {}).keys())
        return tool_result(ToolStatus.SUCCESS, result={'count': len(names), 'persons': names})
    except Exception as e:
        return tool_result(ToolStatus.FAILURE, error={'code': 'EXCEPTION', 'message': str(e), 'retryable': True})


@tool_with_meta(wait_duration=2.0, requires=[{
    'state': 'persons',
    'provider': 'get_persons',
    'validate': {'arg': 'name', 'in': 'result.persons[]'}}])
def get_person_details(name: str) -> dict:
    """
    Get profile information for a specific person. Returns every field for the user.
    Use the exact saved person name from get_persons().
    Args:
        name: The name of the person you want to find details for.
    """
    try:
        db_path = _get_users_db_path()
        if not db_path.exists():
            return tool_result(ToolStatus.FAILURE, error={
                "code": "DB_NOT_FOUND",
                "message": "Users database file not found"})
        with db_path.open('r', encoding='utf-8') as f:
            data = json.load(f)
        users = data.get('users', {})
        if name not in users:
            return tool_result(ToolStatus.SUCCESS, result={
                "message": f"No stored user profile found for {name}.",
                "exists": False})
        profile_str = ', '.join(f"{k}: {v}" for k, v in users[name].items())
        return tool_result(ToolStatus.SUCCESS, result={"name": name, "profile": users[name], "exists": True}, meta={
            "announcement": f"Here's what I found about {name}: {profile_str}"})
    except Exception as e:
        return tool_result(ToolStatus.FAILURE, error={"code": "EXCEPTION", "message": str(e), "retryable": True})


@tool_with_meta(wait_duration=2.0)
def update_person_details(name: str, field: str, input: str) -> dict:
    """
    Update or add specific profile information for a person.
    Common fields to store include "size" and "favorite_color".
    Args:
        name: The name of the person.
        field: The specific profile field to update (e.g. "size" or "favorite_color").
        input: The value to set for the field.
    """
    try:
        db_path = _get_users_db_path()
        # Initialize if it doesn't exist
        if not db_path.exists():
            data = {"users": {}}
            db_path.parent.mkdir(parents=True, exist_ok=True)
        else:
            with db_path.open('r', encoding='utf-8') as f:
                data = json.load(f)
        users = data.setdefault('users', {})
        person_data = users.setdefault(name, {})
        # Update specific field
        person_data[field] = input
        with db_path.open('w', encoding='utf-8') as f:
            json.dump(data, f, indent=2)
        return tool_result(ToolStatus.SUCCESS, result={
            "message": f"Successfully updated {field} for {name}.",
            "profile": person_data}, meta={"announcement": f"I've updated the {field} for {name}."})
    except Exception as e:
        return tool_result(ToolStatus.FAILURE, error={"code": "EXCEPTION", "message": str(e), "retryable": True})


@tool_with_meta(wait_duration=2.0, requires=[{
    'state': 'persons',
    'provider': 'get_persons',
    'validate': {'arg': 'name', 'in': 'result.persons[]'}}])
def add_person_workflow(name: str, actions: list) -> dict:
    """
    Add a custom workflow of actions for a specific person.
    Args:
        name: The name of the person.
        actions: A list of actions that make up the custom workflow.
    """
    try:
        db_path = _get_users_db_path()
        # Initialize if it doesn't exist
        if not db_path.exists():
            data = {"users": {}}
            db_path.parent.mkdir(parents=True, exist_ok=True)
        else:
            with db_path.open('r', encoding='utf-8') as f:
                data = json.load(f)
        users = data.setdefault('users', {})
        person_data = users.setdefault(name, {})
        # Write to custom_workflow field
        person_data['custom_workflow'] = actions
        with db_path.open('w', encoding='utf-8') as f:
            json.dump(data, f, indent=2)
        return tool_result(ToolStatus.SUCCESS, result={
            "message": f"Successfully added custom workflow for {name}.",
            "profile": person_data}, meta={"announcement": f"I've added the custom workflow for {name}."})
    except Exception as e:
        return tool_result(ToolStatus.FAILURE, error={"code": "EXCEPTION", "message": str(e), "retryable": True})
