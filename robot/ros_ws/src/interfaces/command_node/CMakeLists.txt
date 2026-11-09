cmake_minimum_required(VERSION 3.8)
project(command_interfaces)

if(CMAKE_COMPILER_IS_GNUCXX OR CMAKE_CXX_COMPILER_ID MATCHES "Clang")
  add_compile_options(-Wall -Wextra -Wpedantic)
endif()

find_package(ament_cmake REQUIRED)
find_package(rosidl_default_generators REQUIRED)
find_package(tool_status REQUIRED)

rosidl_generate_interfaces(${PROJECT_NAME}
  "action/MoveTo.action"
  "action/TurnTo.action"
  "action/TurnToRelative.action"
  "action/MoveToWaypoint.action"
  "action/PositionForAction.action"
  "action/PositionForPlace.action"
  "srv/GetPosition.srv"
  "srv/StartFollowing.srv"
  "srv/StopFollowing.srv"
  DEPENDENCIES tool_status
)

ament_export_dependencies(rosidl_default_runtime)

ament_package()
