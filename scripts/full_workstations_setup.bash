#!/usr/bin/env bash

# =============================================================================
# setup_workstation_full.sh - Complete workstation setup from scratch
# =============================================================================
# This script sets up everything needed on an Ubuntu 22.04 workstation:
# - System dependencies
# - Python dependencies via uv
# - ROS2 workspace
#
# Prerequisites:
#   - Ubuntu 22.04
#   - Internet connection (ROS2 and uv are auto-installed if missing)
#
# Usage: ./setup_workstation_full.sh
# =============================================================================

set -e  # Exit on error

# =============================================================================
# Color codes and logging functions
# =============================================================================
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
NC='\033[0m' # No Color
BOLD='\033[1m'

STEP_COUNT=0
TOTAL_STEPS=10 # 1. Sanity, 2. GPU drivers, 3. System update, 4. ROS2, 5. uv, 6. System deps, 7. ROS2 config, 8. Python env, 9. ROS2 build, 10. Ollama
DDS_NETWORK_SKIPPED=false

log_info() {
    echo -e "${BLUE}[INFO]${NC} $1"
}

log_success() {
    echo -e "${GREEN}[SUCCESS]${NC} $1"
}

log_warning() {
    echo -e "${YELLOW}[WARNING]${NC} $1"
}

log_error() {
    echo -e "${RED}[ERROR]${NC} $1" >&2
}

log_step() {
    STEP_COUNT=$((STEP_COUNT + 1))
    echo ""
    echo -e "${CYAN}${BOLD}[Step ${STEP_COUNT}/${TOTAL_STEPS}]${NC} ${BOLD}$1${NC}"
    echo "----------------------------------------------"
}

log_substep() {
    echo -e "  ${BLUE}→${NC} $1"
}

log_check() {
    echo -e "  ${GREEN}✓${NC} $1"
}

log_warn() {
    echo -e "  ${YELLOW}⚠${NC} $1"
}

log_fail() {
    echo -e "  ${RED}✗${NC} $1"
}

# =============================================================================
# Error handling
# =============================================================================
trap 'log_error "Script failed at line $LINENO. Exit code: $?"' ERR

check_command() {
    if ! command -v "$1" &> /dev/null; then
        log_fail "$1 is not installed"
        return 1
    fi
    log_check "$1 is installed"
    return 0
}

check_file() {
    if [[ ! -f "$1" ]]; then
        log_fail "File not found: $1"
        return 1
    fi
    log_check "Found: $1"
    return 0
}

# =============================================================================
# Main script
# =============================================================================
PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WORKSTATION_DIR="$PROJECT_ROOT/workstation"

echo ""
echo -e "${BOLD}==============================================${NC}"
echo -e "${BOLD}PG-LLM Service Robot - Workstation Setup${NC}"
echo -e "${BOLD}==============================================${NC}"
echo ""
log_info "Starting setup at $(date)"
log_info "Project root: $PROJECT_ROOT"
log_info "Workstation dir: $WORKSTATION_DIR"
echo ""

# =============================================================================
# Sanity checks
# =============================================================================
log_step "Running sanity checks"

if [[ $EUID -eq 0 ]]; then
  log_error "Do not run this script as root."
  exit 1
fi
log_check "Running as non-root user: $(whoami)"

if [[ ! -f /etc/os-release ]] || ! grep -q "Ubuntu" /etc/os-release; then
  log_warning "This script is designed for Ubuntu 22.04. Proceed with caution."
else
  log_check "Running Ubuntu $(grep VERSION_ID /etc/os-release | cut -d'"' -f2)"
fi

# =============================================================================
# GPU driver check (must happen before display server is affected)
# =============================================================================
log_step "Checking GPU drivers"

NVIDIA_SETUP_DONE_FLAG="$HOME/.nvidia_driver_setup_done"
REBOOT_REQUIRED=false

if lspci | grep -qi nvidia; then
  log_check "NVIDIA GPU detected"
  if nvidia-smi &> /dev/null; then
    log_check "NVIDIA drivers are already installed and working"
    NVIDIA_DRIVER_VERSION=$(nvidia-smi --query-gpu=driver_version --format=csv,noheader 2>/dev/null | head -1)
    log_check "Driver version: $NVIDIA_DRIVER_VERSION"
  else
    log_warn "NVIDIA GPU found but drivers are NOT installed"
    log_warning "Installing NVIDIA drivers requires a REBOOT because it modifies kernel modules"
    log_warning "while the display server is running on the GPU."
    echo ""
    log_substep "Installing GPU drivers..."
    if sudo ubuntu-drivers autoinstall; then
      log_success "GPU drivers installed successfully"
      log_warning "=============================================="
      log_warning "A REBOOT IS REQUIRED to load the NVIDIA kernel modules."
      log_warning "After reboot, re-run this script to continue the setup:"
      log_warning ""
      log_warning "  cd ~/pg-llm-service-robot && ./scripts/full_workstations_setup.bash"
      log_warning "=============================================="
      # Mark that we need to continue after reboot
      touch "$NVIDIA_SETUP_DONE_FLAG"
      REBOOT_REQUIRED=true
    else
      log_error "GPU driver installation failed."
      log_error "Please install manually: sudo ubuntu-drivers autoinstall"
      log_error "Then reboot and re-run this script."
      exit 1
    fi
  fi
else
  log_warn "No NVIDIA GPU detected, skipping driver installation"
fi

if [[ "$REBOOT_REQUIRED" == true ]]; then
  echo ""
  log_info "Script will exit now. Please reboot and re-run to continue."
  exit 0
fi

# Clean up the flag file if it exists (we're past the reboot)
[[ -f "$NVIDIA_SETUP_DONE_FLAG" ]] && rm -f "$NVIDIA_SETUP_DONE_FLAG"

# =============================================================================
# System update
# =============================================================================
log_step "Updating system packages"

log_substep "Cleaning apt cache..."
sudo rm -rf /var/lib/apt/lists/*ros*

log_substep "Updating package lists..."
sudo apt update
log_substep "Upgrading packages..."
sudo apt upgrade -y
log_substep "Removing unused packages..."
sudo apt autoremove -y
log_success "System packages updated"

# =============================================================================
# Verify ROS2 Humble or auto-install
# =============================================================================
log_step "Checking for ROS2 Humble"

if [[ ! -f /opt/ros/humble/setup.bash ]]; then
  log_warn "ROS2 Humble is not installed"
  log_substep "Installing ROS2 Humble automatically..."

  log_substep "Installing prerequisites..."
  sudo apt install -y software-properties-common
  sudo add-apt-repository universe -y

  log_substep "Setting up ROS2 repository..."
  sudo apt update && sudo apt install -y curl
  export ROS_APT_SOURCE_VERSION=$(curl -s https://api.github.com/repos/ros-infrastructure/ros-apt-source/releases/latest | grep -F "tag_name" | awk -F'"' '{print $4}')
  if [[ -z "$ROS_APT_SOURCE_VERSION" ]]; then
    log_error "Failed to fetch ROS2 apt source version"
    exit 1
  fi
  log_substep "ROS2 apt source version: $ROS_APT_SOURCE_VERSION"

  curl -L -o /tmp/ros2-apt-source.deb "https://github.com/ros-infrastructure/ros-apt-source/releases/download/${ROS_APT_SOURCE_VERSION}/ros2-apt-source_${ROS_APT_SOURCE_VERSION}.$(. /etc/os-release && echo ${UBUNTU_CODENAME:-${VERSION_CODENAME}})_all.deb"
  sudo dpkg -i /tmp/ros2-apt-source.deb

  log_substep "Installing ROS2 packages (this may take a while)..."
  sudo apt update && sudo apt upgrade -y
  sudo apt install -y ros-humble-desktop
  sudo apt install -y ros-humble-ros-base
  sudo apt install -y ros-dev-tools
fi

if [[ ! -f /opt/ros/humble/setup.bash ]]; then
  log_error "Failed to install ROS2 Humble. Please install manually."
  log_error "See: https://docs.ros.org/en/humble/Installation.html"
  exit 1
fi
log_success "ROS2 Humble found at /opt/ros/humble"

# =============================================================================
# Install uv (auto-install if missing)
# =============================================================================
log_step "Checking for uv"

export PATH="$HOME/.local/bin:$PATH"
if ! command -v uv &> /dev/null; then
  log_warn "uv not found — installing automatically..."
  curl -LsSf https://astral.sh/uv/install.sh | sh
  export PATH="$HOME/.local/bin:$PATH"
  if ! command -v uv &> /dev/null; then
    log_error "uv installation failed. Please install manually:"
    log_error "  curl -LsSf https://astral.sh/uv/install.sh | sh"
    exit 1
  fi
fi
log_success "uv found at $(command -v uv)"
if [[ -f "$HOME/.local/bin/env" ]]; then
  source "$HOME/.local/bin/env"
fi
if ! grep -q 'export PATH="$HOME/.local/bin:$PATH"' ~/.bashrc; then
  echo '' >> ~/.bashrc
  echo '# Add uv to PATH' >> ~/.bashrc
  echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.bashrc
  log_check "Added ~/.local/bin to PATH in ~/.bashrc"
else
  log_check "~/.local/bin already in PATH in ~/.bashrc"
fi
# =============================================================================
# Install system dependencies
# =============================================================================
log_step "Installing system dependencies"

log_substep "Installing audio and system packages..."
sudo apt install -y \
  build-essential \
  cmake \
  curl \
  python3-colcon-common-extensions \
  libportaudio2 \
  portaudio19-dev \
  python3.10 \
  python3.10-venv \
  python3.10-dev \
  btop \
  fastdds-tools \
  openssh-server \
  nvtop \
  ffmpeg

# Install ROS2 specific packages
log_substep "Installing ROS2 packages..."
sudo apt install -y \
  ros-humble-vision-msgs \
  ros-humble-cv-bridge \
  ros-humble-rviz2 \
  ros-humble-nav2-bringup \
  ros-humble-slam-toolbox \
  ros-humble-control-msgs \
  ros-humble-ament-cmake-gtest

log_success "System dependencies installed"

echo "==> Configuring DDS socket buffer ceilings..."
"$PROJECT_ROOT/scripts/configure_dds_buffers.bash" --install
echo "==> Configuring the dedicated DDS network path..."
if "$PROJECT_ROOT/scripts/configure_dds_network.bash" --install; then
  log_success "DDS network path configured"
else
  log_warning "DDS network interface not active (192.168.50.49-52) — skipping."
  log_warning "Run 'scripts/configure_dds_network.bash --install' after connecting the DDS link."
  DDS_NETWORK_SKIPPED=true
fi

# =============================================================================
# Configure ROS2 environment
# =============================================================================
log_step "Configuring ROS2 environment"

# Add ROS2 setup to .bashrc if not already present
if ! grep -q "source /opt/ros/humble/setup.bash" ~/.bashrc; then
  log_substep "Adding ROS2 setup to ~/.bashrc"
  echo '' >> ~/.bashrc
  echo '# ROS2 Humble setup' >> ~/.bashrc
  echo 'source /opt/ros/humble/setup.bash' >> ~/.bashrc
  echo 'export ROS_DOMAIN_ID=42  # Must match the robot' >> ~/.bashrc
  echo 'export ROS_LOCALHOST_ONLY=0' >> ~/.bashrc
  log_check "ROS2 setup added to ~/.bashrc"
else
  log_check "ROS2 setup already in ~/.bashrc"
fi

# Add onnxruntime library path for sherpa_onnx (TTS/transcriber) if not already present
if ! grep -q "onnxruntime" ~/.bashrc; then
  log_substep "Adding onnxruntime library path to ~/.bashrc"
  echo '' >> ~/.bashrc
  echo '# onnxruntime library path for sherpa_onnx (TTS/transcriber)' >> ~/.bashrc
  printf 'export LD_LIBRARY_PATH=%q:"${LD_LIBRARY_PATH:-}"\n' "$WORKSTATION_DIR/.venv/lib/python3.10/site-packages/onnxruntime/capi" >> ~/.bashrc
  log_check "onnxruntime library path added to ~/.bashrc"
else
  log_check "onnxruntime library path already in ~/.bashrc"
fi

# Clear any stale ROS prefix paths from the environment before building.
# These linger from a previous session's sourced setup.bash and cause colcon
# to emit "path doesn't exist" warnings when the workspace has been deleted.
log_substep "Clearing stale ROS environment variables..."
unset AMENT_PREFIX_PATH
unset CMAKE_PREFIX_PATH
unset COLCON_PREFIX_PATH

# Source ROS2 for current session
log_substep "Sourcing ROS2 environment..."
source /opt/ros/humble/setup.bash
source "$PROJECT_ROOT/scripts/ros_tf2_fix.bash"
pg_install_tf2_fix "$PROJECT_ROOT"
pg_source_tf2_fix "$PROJECT_ROOT"
log_success "ROS2 environment configured"

# =============================================================================
# Setup Python environment with uv
# =============================================================================
log_step "Setting up Python environment with uv"

cd "$WORKSTATION_DIR"

# Create virtual environment (--allow-existing avoids the interactive
# "replace?" prompt when .venv already exists on re-runs).
log_substep "Creating Python virtual environment..."
uv venv --python 3.10 --allow-existing

# Sync dependencies
log_substep "Syncing Python dependencies (this may take a while)..."
uv sync

log_substep "Activating virtual environment..."
source "$WORKSTATION_DIR/.venv/bin/activate"

# Create symlink for libonnxruntime.so (sherpa_onnx needs this)
ONNX_CAPI_DIR="$WORKSTATION_DIR/.venv/lib/python3.10/site-packages/onnxruntime/capi"
if [[ -d "$ONNX_CAPI_DIR" ]] && [[ ! -e "$ONNX_CAPI_DIR/libonnxruntime.so" ]]; then
  log_substep "Creating libonnxruntime.so symlink..."
  ONNX_LIB_VERSIONED=$(ls "$ONNX_CAPI_DIR"/libonnxruntime.so.* 2>/dev/null | head -1)
  if [[ -n "$ONNX_LIB_VERSIONED" ]]; then
    ln -sf "$(basename "$ONNX_LIB_VERSIONED")" "$ONNX_CAPI_DIR/libonnxruntime.so"
    log_check "Created symlink for libonnxruntime.so"
  else
    log_warn "Could not find versioned libonnxruntime.so"
  fi
else
  log_check "libonnxruntime.so symlink already exists"
fi

log_success "Python environment configured"

# =============================================================================
# Build ROS2 workspace
# =============================================================================
# NOTE: build with the standard ROS/colcon toolchain so ament_python installs
# console scripts into the expected lib/<pkg> location for ros2 run.
log_step "Building ROS2 workspace"

cd "$WORKSTATION_DIR/ament_ws"

# Clean any previous builds
log_substep "Cleaning previous builds..."
rm -rf install build log

# Build
log_substep "Building ROS2 packages (this may take a while)..."
colcon build --cmake-clean-cache

log_success "ROS2 workspace built successfully"

# =============================================================================
# Install ollama
# =============================================================================
log_step "Installing Ollama and pulling models"

log_substep "Installing Ollama..."
if curl -fsSL https://ollama.com/install.sh | sh; then
  log_success "Ollama installed"
else
  log_error "Failed to install Ollama"
  exit 1
fi

log_substep "Pulling qwen3.5:9b model..."
if ollama pull qwen3.5:9b; then
  log_success "qwen3.5:9b model pulled"
else
  log_warning "Failed to pull qwen3.5:9b model"
fi

log_substep "Pulling multilingual E5 embedding model..."
if ollama pull qllama/multilingual-e5-base; then
  log_success "Multilingual E5 embedding model pulled"
else
  log_warning "Failed to pull multilingual E5 embedding model"
fi

# Build local knowledge now that the Python environment and model exist.
log_substep "Initializing searchable knowledge..."
RAG_SETUP_FAILED=false
if bash "$PROJECT_ROOT/scripts/ingest_knowledge.sh"; then
  log_success "Knowledge index initialized"
else
  RAG_SETUP_FAILED=true
  log_warning "RAG setup is incomplete. Retry: bash scripts/ingest_knowledge.sh"
fi

#ollama pull nemotron-3-nano
#ollama pull mistral-small3.2:24b

# =============================================================================
# Done!
# =============================================================================
echo ""
echo -e "${GREEN}${BOLD}==============================================${NC}"
if [[ "$RAG_SETUP_FAILED" == true ]]; then
  echo -e "${YELLOW}${BOLD}Setup finished; RAG initialization needs attention.${NC}"
else
  echo -e "${GREEN}${BOLD}Setup complete!${NC}"
fi
echo -e "${GREEN}${BOLD}==============================================${NC}"
echo ""
log_success "Workstation setup completed at $(date)"
echo ""
echo -e "${BOLD}Next steps:${NC}"
echo "1. Open a new terminal (or run: source ~/.bashrc)"
echo "2. Run scripts/start.sh"
echo ""
echo -e "${BOLD}Installed components:${NC}"
echo "  - ROS2 Humble: $(source /opt/ros/humble/setup.bash && echo $ROS_DISTRO)"
echo "  - uv: $(command -v uv)"
echo "  - Python: $(python3 --version)"
echo "  - Ollama: $(command -v ollama)"
echo ""
echo -e "${BOLD}Environment variables (added to ~/.bashrc):${NC}"
echo "  - ROS_DOMAIN_ID=42"
echo "  - ROS_LOCALHOST_ONLY=0"
echo "  - LD_LIBRARY_PATH includes onnxruntime"
echo ""

if [[ "$DDS_NETWORK_SKIPPED" == true ]]; then
  echo ""
  echo -e "${YELLOW}${BOLD}╔════════════════════════════════════════════════════════════╗${NC}"
  echo -e "${YELLOW}${BOLD}║  WARNING: Dedicated DDS network is not configured yet!     ║${NC}"
  echo -e "${YELLOW}${BOLD}║                                                            ║${NC}"
  echo -e "${YELLOW}${BOLD}║  The DDS network interface (192.168.50.49-52) was not      ║${NC}"
  echo -e "${YELLOW}${BOLD}║  active during setup. DDS discovery server and ROS2 nodes  ║${NC}"
  echo -e "${YELLOW}${BOLD}║  will fail to communicate until this is resolved.          ║${NC}"
  echo -e "${YELLOW}${BOLD}║                                                            ║${NC}"
  echo -e "${YELLOW}${BOLD}║  To fix this:                                              ║${NC}"
  echo -e "${YELLOW}${BOLD}║  1. Connect the dedicated DDS WLAN adapter.                ║${NC}"
  echo -e "${YELLOW}${BOLD}║  2. Set this machine's static IP to 192.168.50.50          ║${NC}"
  echo -e "${YELLOW}${BOLD}║     (or 192.168.50.49 for the laptop).                     ║${NC}"
  echo -e "${YELLOW}${BOLD}║  3. Re-run:  scripts/full_workstations_setup.bash          ║${NC}"
  echo -e "${YELLOW}${BOLD}║     or just:  scripts/configure_dds_network.bash --install ║${NC}"
  echo -e "${YELLOW}${BOLD}╚════════════════════════════════════════════════════════════╝${NC}"
  echo ""
fi
