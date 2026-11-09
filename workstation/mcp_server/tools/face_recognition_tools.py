"""
Face recognition tools for learning, identifying, and forgetting known people.
"""
from mcp_server.decorators import ToolStatus, tool_result, tool_with_meta
from workstation_common import get_logger

log = get_logger('mcp.face_recognition')
from mcp_server.ros_utils import (call_service, response_status, response_message, STATUS_SUCCESS, STATUS_STOPPED, STATUS_TIMEOUT, STATUS_FAILURE)
from vision_interfaces.srv import IdentifyFaces, ForgetFace


@tool_with_meta(wait_duration=12.0, )
def get_identified_faces() -> dict:
    """
    Identify known faces visible in the current head camera view.
    Returns:
        Known face matches and summary counters.
    """
    try:
        log.info("[TOOL] get_identified_faces()")
        request = IdentifyFaces.Request()
        response = call_service('/vision_node/identify_faces', IdentifyFaces, request, timeout_sec=20.0)
        status_val = response_status(response)
        if status_val == STATUS_STOPPED:
            return tool_result(ToolStatus.STOPPED, error={"code": "GLOBAL_STOP", "message": "Stopped by user"})
        if status_val == STATUS_SUCCESS:
            faces = list(response.faces) if response else []
            known_names = list(response.known_names) if response else []
            known_count = len(known_names)
            total_count = response.count if response else len(faces)
            unknown_count = response.unknown_count if response else 0
            if known_count == 1:
                announcement = f"I can see {known_names[0]}."
            elif known_count > 1:
                announcement = f"I can see {', '.join(known_names)}."
            elif total_count > 0 or unknown_count > 0:
                visible_count = total_count if total_count > 0 else unknown_count
                person_word = 'person' if visible_count == 1 else 'people'
                announcement = f"I can see {visible_count} {person_word}, but I don't recognize them yet."
            else:
                announcement = "I don't see anyone right now."
            return tool_result(ToolStatus.SUCCESS, result={
                "faces": faces,
                "known_names": known_names,
                "known_count": known_count,
                "count": total_count,
                "unknown_count": unknown_count,
                "message": response_message(response, "Face identification completed."), }, meta={
                "announcement": announcement}, )
        error_msg = response_message(response, "Could not identify faces")
        return tool_result(ToolStatus.FAILURE, error={
            "code": "IDENTIFY_FACES_FAILED",
            "message": error_msg,
            "retryable": True}, )
    except Exception as e:
        return tool_result(ToolStatus.FAILURE, error={"code": "EXCEPTION", "message": str(e), "retryable": True})


@tool_with_meta(wait_duration=12.0, )
def forget_face(name: str = '') -> dict:
    """
    Remove a known face entry from the face database, or clear all stored faces.
    Args:
        name: Person name to remove.
              Omit it (or use "all", "*", "everyone") to forget all faces.
    Notes:
        This operation updates stored face data directly and does not require
        the person to be visible in the current camera image.
    """
    try:
        person_name = (name or "").strip()
        normalized = person_name.lower()
        forget_all = normalized in {'', '*', 'all', 'all faces', 'everyone', 'everybody', 'alle', 'alle gesichter'}
        request_name = 'all' if forget_all else person_name
        if not request_name:
            return tool_result(ToolStatus.FAILURE, error={"code": "INVALID_ARGUMENT", "message": "name is required"})
        log.info(f"[TOOL] forget_face(name={request_name})")
        request = ForgetFace.Request()
        request.name = request_name
        response = call_service('/vision_node/forget_face', ForgetFace, request, timeout_sec=20.0)
        status_val = response_status(response)
        if status_val == STATUS_STOPPED:
            return tool_result(ToolStatus.STOPPED, error={"code": "GLOBAL_STOP", "message": "Stopped by user"})
        if status_val == STATUS_SUCCESS:
            resolved_name = response.name if response else request_name
            removed_all = str(resolved_name).strip().lower() in {'all', '*'}
            if removed_all:
                announcement = "I forgot all known faces."
            else:
                announcement = f"I forgot {resolved_name}."
            return tool_result(ToolStatus.SUCCESS, result={
                "name": resolved_name,
                "removed_all": removed_all,
                "message": response_message(response, "Face removed."), }, meta={"announcement": announcement}, )
        error_msg = response_message(response, "Could not forget face")
        return tool_result(ToolStatus.FAILURE, error={
            "code": "FORGET_FACE_FAILED",
            "message": error_msg,
            "retryable": True})
    except Exception as e:
        return tool_result(ToolStatus.FAILURE, error={"code": "EXCEPTION", "message": str(e), "retryable": True})
