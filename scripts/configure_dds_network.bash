#!/usr/bin/env bash

set -euo pipefail

SYSCTL_FILE=/etc/sysctl.d/61-pg-llm-robot-dds-network.conf
MODE=${1:---install}

detect_dedicated_address() {
  ip -o -4 address show | awk '
    $3 == "inet" {
      split($4, address, "/")
      if (address[1] ~ /^192\.168\.50\.(49|50|51|52)$/) {
        print $2, address[1]
        exit
      }
    }'
}

peer_for_address() {
  case "$1" in
    192.168.50.49) echo 192.168.50.51 ;;
    192.168.50.50) echo 192.168.50.52 ;;
    192.168.50.51) echo 192.168.50.49 ;;
    192.168.50.52) echo 192.168.50.50 ;;
    *) return 1 ;;
  esac
}

detected_address=$(detect_dedicated_address || true)
DDS_INTERFACE=""
DDS_LOCAL_ADDRESS=""
read -r DDS_INTERFACE DDS_LOCAL_ADDRESS <<<"$detected_address"
if [[ -z "$DDS_INTERFACE" || -z "$DDS_LOCAL_ADDRESS" ]]; then
  echo "No managed DDS address (192.168.50.49-52) is active." >&2
  exit 1
fi
DDS_PEER_ADDRESS=$(peer_for_address "$DDS_LOCAL_ADDRESS")
case "$DDS_LOCAL_ADDRESS" in
  192.168.50.51|192.168.50.52)
    device_path=$(readlink -f "/sys/class/net/$DDS_INTERFACE/device" || true)
    if [[ ! -d "/sys/class/net/$DDS_INTERFACE/wireless" || "$device_path" != */usb*/* ]]; then
      echo "Robot DDS address $DDS_LOCAL_ADDRESS must use the external USB Wi-Fi adapter; found $DDS_INTERFACE." >&2
      echo "Configure it with scripts/configure_robot_usb_wifi.py; Ethernet is for setup via DHCP." >&2
      exit 1
    fi
    ;;
esac
DDS_CONNECTION=$(nmcli -g GENERAL.CONNECTION device show "$DDS_INTERFACE")
if [[ -z "$DDS_CONNECTION" || "$DDS_CONNECTION" == "--" ]]; then
  echo "No NetworkManager connection owns $DDS_INTERFACE." >&2
  exit 1
fi

show_values() {
  local route arp_ignore arp_announce
  route=$(ip -4 route get "$DDS_PEER_ADDRESS")
  arp_ignore=$(sysctl -n net.ipv4.conf.all.arp_ignore)
  arp_announce=$(sysctl -n net.ipv4.conf.all.arp_announce)
  echo "DDS link: local=${DDS_LOCAL_ADDRESS} interface=${DDS_INTERFACE} peer=${DDS_PEER_ADDRESS}"
  echo "DDS route: $route"
  echo "net.ipv4.conf.all.arp_ignore = $arp_ignore"
  echo "net.ipv4.conf.all.arp_announce = $arp_announce"
  [[ "$route" == *"dev $DDS_INTERFACE"* &&
     "$route" == *"src $DDS_LOCAL_ADDRESS"* &&
     "$arp_ignore" == 1 &&
     "$arp_announce" == 2 ]]
}

case "$MODE" in
  --check)
    show_values
    ;;
  --install)
    route_spec="${DDS_PEER_ADDRESS}/32"
    if ! nmcli -g ipv4.routes connection show "$DDS_CONNECTION" | tr ',' '\n' | grep -Fq "$route_spec"; then
      sudo nmcli connection modify "$DDS_CONNECTION" +ipv4.routes "$route_spec"
      sudo nmcli device reapply "$DDS_INTERFACE"
    fi

    config_tmp=$(mktemp)
    trap 'rm -f "$config_tmp"' EXIT
    printf '%s\n' \
      '# PG-LLM robot: deterministic ARP behavior on the dedicated DDS WLAN.' \
      'net.ipv4.conf.all.arp_ignore = 1' \
      'net.ipv4.conf.all.arp_announce = 2' >"$config_tmp"
    if ! sudo cmp -s "$config_tmp" "$SYSCTL_FILE"; then
      sudo install -m 0644 "$config_tmp" "$SYSCTL_FILE"
    fi
    sudo sysctl -p "$SYSCTL_FILE" >/dev/null
    show_values
    ;;
  --remove)
    route_spec="${DDS_PEER_ADDRESS}/32"
    if nmcli -g ipv4.routes connection show "$DDS_CONNECTION" | tr ',' '\n' | grep -Fq "$route_spec"; then
      sudo nmcli connection modify "$DDS_CONNECTION" -ipv4.routes "$route_spec"
      sudo nmcli device reapply "$DDS_INTERFACE"
    fi
    sudo rm -f "$SYSCTL_FILE"
    sudo sysctl -w \
      net.ipv4.conf.all.arp_ignore=0 \
      net.ipv4.conf.all.arp_announce=0 >/dev/null
    show_values || true
    ;;
  *)
    echo "Usage: $0 [--install|--check|--remove]" >&2
    exit 2
    ;;
esac
