"""Conservative motion classification for uncertain-completion recovery.

This is not the study allowlist: it only decides which calls need a checked
robot state after an interrupted operation. Unknown tools require that check.
"""

NON_MOTION_TOOLS = frozenset({
    'current_time', 'current_date', 'battery_status', 'current_weather', 'wiki_summary',
    'get_persons', 'get_person_details', 'update_person_details', 'add_person_workflow',
    'get_tools_registry', 'list_categories', 'list_tools_by_category', 'get_slam_mode',
    'query_knowledge_base', 'store_memory', 'recall_memory', 'get_visible_objects',
    'get_tracked_people', 'get_scene_description', 'get_identified_faces', 'forget_face',
    'get_waypoints', 'speak', 'get_transport_state', 'get_study_profile',
    'report_empty_gripper', 'report_holding_gripper',
})


def needs_robot_state(name: str) -> bool:
    return name not in NON_MOTION_TOOLS
