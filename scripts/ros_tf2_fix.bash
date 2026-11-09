#!/usr/bin/env bash
# Humble 0.25.23 can deadlock Nav2's TF listener (ros2/geometry2#990).
# Build the ABI-compatible upstream tf2 0.25.24 library in a project overlay.
# No system packages are replaced; newer distro packages take precedence.

pg_tf2_fix_needed() {
    local version
    version=$(dpkg-query -W -f='${Version}' ros-humble-tf2 2>/dev/null) || return 1
    [[ "$version" == 0.25.23-* || "$version" == 0.25.23 ]]
}

pg_source_tf2_fix() {
    local prefix="$1/.ros_tf2/0.25.24"
    # A workspace built with the fix can chain this overlay from setup.bash.
    # Remove inherited entries first so a later apt upgrade uses distro TF2,
    # and an incomplete/unverified overlay cannot remain active after failure.
    local env_name entry cleaned
    local -a entries
    for env_name in AMENT_PREFIX_PATH CMAKE_PREFIX_PATH COLCON_PREFIX_PATH LD_LIBRARY_PATH; do
        [[ -v "$env_name" ]] || continue
        IFS=: read -r -a entries <<< "${!env_name}"
        cleaned=""
        for entry in "${entries[@]}"; do
            case "$entry" in
                "$prefix/install"|"$prefix/install/"*|"") continue ;;
            esac
            cleaned+="${cleaned:+:}$entry"
        done
        if [[ -n "$cleaned" ]]; then
            export "$env_name=$cleaned"
        else
            unset "$env_name"
        fi
    done
    pg_tf2_fix_needed || return 0
    if [[ ! -f "$prefix/verified" || ! -f "$prefix/install/local_setup.bash" || ! -f "$prefix/install/lib/libtf2.so" ]]; then
        echo "TF2 0.25.23 has a Nav2 deadlock. Run: bash $1/scripts/ros_tf2_fix.bash --install" >&2
        return 1
    fi
    source "$prefix/install/local_setup.bash"
}

pg_install_tf2_fix() (
    set -e -o pipefail
    # Callers may use `pg_install_tf2_fix ... || return 1`, which disables Bash
    # errexit even inside this subshell. Explicitly stop on each failed step.
    local project_root="$1" prefix archive
    pg_tf2_fix_needed || return 0
    prefix="$project_root/.ros_tf2/0.25.24"
    mkdir -p "$prefix" || exit 1
    exec 9>"$prefix/build.lock" || exit 1
    flock 9 || exit 1
    if [[ -f "$prefix/verified" && -f "$prefix/install/lib/libtf2.so" && -f "$prefix/install/local_setup.bash" ]]; then
        echo "Using verified TF2 0.25.24 overlay: $prefix"
        return 0
    fi
    rm -f "$prefix/verified" || exit 1
    archive="$prefix/geometry2-0.25.24.tar.gz"
    if [[ ! -f "$archive" ]]; then
        curl --fail --location --retry 3 --max-time 120 \
            https://github.com/ros2/geometry2/archive/refs/tags/0.25.24.tar.gz \
            -o "$archive.download" || exit 1
        mv "$archive.download" "$archive" || exit 1
    fi
    printf '%s  %s\n' 9fb5244616d06029bd7510a52e2fd5ab6f9aae6995e1d8bcda14fbe5140b979a "$archive" | sha256sum --check || exit 1
    mkdir -p "$prefix/src" || exit 1
    tar -xzf "$archive" -C "$prefix/src" || exit 1
    # Build against distro headers only, never an already sourced project overlay.
    unset AMENT_PREFIX_PATH CMAKE_PREFIX_PATH COLCON_PREFIX_PATH PYTHONPATH LD_LIBRARY_PATH
    source /opt/ros/humble/setup.bash || exit 1
    # The fix changes the implementation only. Refuse an unexpected ABI/header change.
    diff -qr /opt/ros/humble/include/tf2/tf2 "$prefix/src/geometry2-0.25.24/tf2/include/tf2" || exit 1
    cd "$prefix" || exit 1
    /usr/bin/colcon build --merge-install --packages-select tf2 --allow-overriding tf2 \
        --executor sequential --cmake-args -DBUILD_TESTING=OFF -DCMAKE_BUILD_TYPE=RelWithDebInfo \
        -DPython3_EXECUTABLE=/usr/bin/python3 || exit 1
    # Run upstream's buffer tests against the new library and the installed tf2_ros.
    # In particular this includes the deterministic two-thread deadlock regression.
    mkdir -p regression || exit 1
    cat > regression/CMakeLists.txt <<'CMAKE' || exit 1
cmake_minimum_required(VERSION 3.16)
project(tf2_deadlock_regression)
find_package(ament_cmake REQUIRED)
find_package(ament_cmake_gtest REQUIRED)
find_package(tf2_ros REQUIRED)
ament_add_gtest(test_buffer ../src/geometry2-0.25.24/tf2_ros/test/test_buffer.cpp)
target_link_libraries(test_buffer tf2_ros::tf2_ros)
CMAKE
    source "$prefix/install/local_setup.bash" || exit 1
    cmake -S regression -B regression/build -DPython3_EXECUTABLE=/usr/bin/python3 || exit 1
    cmake --build regression/build --parallel 2 || exit 1
    timeout 60 regression/build/test_buffer || exit 1
    printf '%s\n' 'geometry2 0.25.24; upstream buffer tests passed' > "$prefix/verified"
)

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
    set -e -o pipefail
    tf2_project_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
    case "${1:-}" in
        --install) pg_install_tf2_fix "$tf2_project_root" ;;
        --check) pg_source_tf2_fix "$tf2_project_root" ;;
        *) echo "Usage: bash scripts/ros_tf2_fix.bash --install|--check" >&2; exit 2 ;;
    esac
fi
