from __future__ import annotations

from enum import Enum
from typing import Dict, List, Optional

from pydantic import BaseModel, Field


class MachineType(str, Enum):
    ROBOT = "robot"
    WORKSTATION = "workstation"


class NodeType(str, Enum):
    NODE = "node"
    LAUNCH = "launch"


class ServiceStatus(str, Enum):
    RUNNING = "running"
    STOPPED = "stopped"
    FAILED = "failed"
    ACTIVATING = "activating"
    DEACTIVATING = "deactivating"
    UNKNOWN = "unknown"


class MachineConfig(BaseModel):
    name: str
    host: str
    user: Optional[str] = None
    ssh_key_path: Optional[str] = None
    port: int = 22
    ros_domain_id: int = 0
    type: MachineType
    project_path: str = "~/pg-llm-service-robot"


class NodeConfig(BaseModel):
    id: str
    name: str
    description: str
    command: str
    working_dir: Optional[str] = None
    dependencies: List[str] = Field(default_factory=list)
    startup_delay: int = 0
    requires_build: bool = False
    requires_venv: bool = False
    venv_path: Optional[str] = None
    service_name: str
    type: NodeType = NodeType.NODE
    package: Optional[str] = None
    launch_file: Optional[str] = None
    launch_args: Optional[str] = None
    packages_to_build: List[str] = Field(default_factory=list)
    extra_env: Dict[str, str] = Field(default_factory=dict)


class Settings(BaseModel):
    log_lines_default: int = Field(default=100, ge=1, le=10000)
    connection_timeout: int = Field(default=10, ge=1, le=300)
    command_timeout: int = Field(default=30, ge=1, le=3600)
    network_interface: str = ""


class MachineStatus(BaseModel):
    id: str
    name: str
    host: str
    connected: bool
    error: Optional[str] = None
    type: MachineType
    ros_domain_id: int
    git_branch: Optional[str] = None
    git_error: Optional[str] = None


class NodeStatus(BaseModel):
    id: str
    name: str
    description: str
    service_name: str
    status: ServiceStatus
    active_state: str
    sub_state: str
    exit_code: Optional[int] = None
    pid: Optional[int] = None
    memory_current: Optional[str] = None
    started_at: Optional[str] = None
    error: Optional[str] = None


class CommandResult(BaseModel):
    success: bool
    message: str
    exit_code: Optional[int] = None
    stdout: Optional[str] = None
    stderr: Optional[str] = None


class RobotControlConnectionInfo(BaseModel):
    workstation_id: str
    host: str
    port: int
    path: str
    robot_id: str
    ros_domain_id: int


class LlmInputConnectionInfo(BaseModel):
    workstation_id: str
    host: str
    port: int
    path: str
    ros_domain_id: int


class SystemInfo(BaseModel):
    hostname: Optional[str] = None
    kernel: Optional[str] = None
    uptime: Optional[str] = None
    cpu_usage: Optional[str] = None
    memory_total: Optional[str] = None
    memory_used: Optional[str] = None
    memory_available: Optional[str] = None
    disk_total: Optional[str] = None
    disk_used: Optional[str] = None
    disk_available: Optional[str] = None
    disk_use_percent: Optional[str] = None
    gpu_usage: Optional[str] = None
    gpu_memory_used: Optional[str] = None
    gpu_memory_total: Optional[str] = None


class DiscoveryServerStatus(BaseModel):
    port: int
    endpoint: str
    running: bool
    pid: Optional[int] = None
    command: str
    last_error: Optional[str] = None
    started_at: Optional[float] = None
    log_file: str
