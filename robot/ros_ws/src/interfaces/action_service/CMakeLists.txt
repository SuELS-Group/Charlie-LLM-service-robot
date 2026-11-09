cmake_minimum_required(VERSION 3.8)
project(action_interfaces)

if(CMAKE_COMPILER_IS_GNUCXX OR CMAKE_CXX_COMPILER_ID MATCHES "Clang")
  add_compile_options(-Wall -Wextra -Wpedantic)
endif()

find_package(ament_cmake REQUIRED)
find_package(rosidl_default_generators REQUIRED)
find_package(tool_status REQUIRED)

rosidl_generate_interfaces(${PROJECT_NAME}
  "srv/ExecuteAction.srv"
  DEPENDENCIES tool_status
)

ament_export_dependencies(rosidl_default_runtime)

ament_package()
