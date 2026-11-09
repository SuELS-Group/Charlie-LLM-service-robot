#!/usr/bin/env bash

# Clean generated ROS interface artifacts only when their source definition set
# changes. CMake cache cleaning alone does not remove code generated for deleted
# .msg/.srv/.action files.

declare -Ag PG_INTERFACE_FINGERPRINTS=()

_pg_interface_fingerprint() {
  local package_path="$1"
  (
    cd "$package_path" || return 1
    find . -type f \
      \( -name CMakeLists.txt -o -name package.xml \
      -o -path './msg/*' -o -path './srv/*' -o -path './action/*' \) \
      -print0 |
      LC_ALL=C sort -z |
      xargs -0 sha256sum
  ) | sha256sum | awk '{print $1}'
}

pg_prepare_interface_packages() {
  local workspace="$1"
  shift
  local requested=("$@")
  local -A package_paths=()
  local name path package_type

  PG_INTERFACE_FINGERPRINTS=()
  while IFS=$'\t' read -r name path package_type; do
    package_paths["$name"]="$path"
  done < <(cd "$workspace" && colcon list)

  local package package_path fingerprint stamp previous
  for package in "${requested[@]}"; do
    package_path="${package_paths[$package]:-}"
    [[ -n "$package_path" ]] || continue
    [[ "$package_path" == /* ]] || package_path="$workspace/$package_path"
    [[ -f "$package_path/package.xml" ]] || continue
    grep -Fq '<member_of_group>rosidl_interface_packages</member_of_group>' \
      "$package_path/package.xml" || continue

    fingerprint=$(_pg_interface_fingerprint "$package_path")
    stamp="$workspace/build/$package/.pg_interface_source.sha256"
    previous=""
    [[ -f "$stamp" ]] && previous=$(<"$stamp")
    PG_INTERFACE_FINGERPRINTS["$package"]="$fingerprint"

    if [[ "$previous" != "$fingerprint" || ! -d "$workspace/install/$package" ]]; then
      echo "Cleaning changed ROS interface package: $package"
      rm -rf "$workspace/build/$package" "$workspace/install/$package"
    fi
  done
}

pg_record_interface_fingerprints() {
  local workspace="$1"
  local package stamp
  for package in "${!PG_INTERFACE_FINGERPRINTS[@]}"; do
    stamp="$workspace/build/$package/.pg_interface_source.sha256"
    mkdir -p "$(dirname "$stamp")"
    printf '%s\n' "${PG_INTERFACE_FINGERPRINTS[$package]}" >"$stamp"
  done
  PG_INTERFACE_FINGERPRINTS=()
}
