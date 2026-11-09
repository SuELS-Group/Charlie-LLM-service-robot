from mcp_server.decorators import ToolStatus, tool_result, tool_with_meta
from mcp_server.study_policy import query_policy


@tool_with_meta(returns_data=True, wait_duration=2.)
def get_study_profile() -> dict:
    """Read the active study profile, approved service points/routes and test object.

    Staff maintain configuration; this tool cannot approve or enable anything.
    Possession in the first trial is a workflow assumption, not sensor proof.
    """
    summary, error = query_policy()
    if error:
        return tool_result(ToolStatus.FAILURE, error={'code': error[0], 'message': error[1], 'retryable': False})
    return tool_result(ToolStatus.SUCCESS, result=summary)
