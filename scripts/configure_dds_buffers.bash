#!/usr/bin/env bash

set -euo pipefail

SYSCTL_FILE=/etc/sysctl.d/60-pg-llm-robot-dds.conf
EXPECTED_BYTES=4194304
MODE=${1:---install}

show_values() {
  local receive_bytes send_bytes
  receive_bytes=$(sysctl -n net.core.rmem_max)
  send_bytes=$(sysctl -n net.core.wmem_max)
  echo "DDS socket buffer ceilings: receive=${receive_bytes}, send=${send_bytes}"
  [[ "$receive_bytes" -ge "$EXPECTED_BYTES" && "$send_bytes" -ge "$EXPECTED_BYTES" ]]
}

case "$MODE" in
  --check)
    show_values
    ;;
  --remove)
    sudo rm -f "$SYSCTL_FILE"
    sudo sysctl --system >/dev/null
    echo "Removed $SYSCTL_FILE"
    show_values || true
    ;;
  --install)
    config_tmp=$(mktemp)
    trap 'rm -f "$config_tmp"' EXIT
    printf '%s\n' \
      '# PG-LLM robot: allow Fast DDS to use its configured UDP buffers.' \
      "net.core.rmem_max = $EXPECTED_BYTES" \
      "net.core.wmem_max = $EXPECTED_BYTES" >"$config_tmp"
    if ! sudo cmp -s "$config_tmp" "$SYSCTL_FILE"; then
      sudo install -m 0644 "$config_tmp" "$SYSCTL_FILE"
    fi
    sudo sysctl -p "$SYSCTL_FILE" >/dev/null
    show_values
    ;;
  *)
    echo "Usage: $0 [--install|--check|--remove]" >&2
    exit 2
    ;;
esac
