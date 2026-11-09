#!/usr/bin/env python3
"""Keep a robot's fixed control address on its external USB Wi-Fi adapter.

Run --install locally as root (or in a detached systemd unit when using SSH).
Ethernet returns to DHCP for setup; no Wi-Fi credentials are read or changed.
"""
import argparse
import datetime
import json
import os
from pathlib import Path
import subprocess
import sys

PEERS = {"192.168.50.51": "192.168.50.49", "192.168.50.52": "192.168.50.50"}
PROFILE_FIELDS = ("connection.interface-name", "connection.autoconnect",
                  "connection.autoconnect-priority", "ipv4.method", "ipv4.addresses",
                  "ipv4.gateway", "ipv4.dns", "ipv4.routes", "ipv4.route-metric")


def run(*command, timeout=30):
    result = subprocess.run(command, capture_output=True, text=True, timeout=timeout)
    if result.returncode:
        raise RuntimeError(f"{command[0]} failed: {result.stderr.strip() or result.stdout.strip()}")
    return result.stdout.strip()


def validate_adapter(interface, sys_class=Path("/sys/class/net")):
    device = sys_class / interface
    path = (device / "device").resolve(strict=True)
    if not (device / "wireless").is_dir() or not any(part.startswith("usb") for part in path.parts):
        raise ValueError(f"{interface} is not an external USB Wi-Fi adapter")
    return (device / "address").read_text().strip()


def field(profile, name):
    return run("nmcli", "--escape", "no", "-g", name, "connection", "show", profile)


def inspect(args):
    mac = validate_adapter(args.interface)
    addresses = json.loads(run("ip", "-j", "-4", "address", "show"))
    owners = {device["ifname"] for device in addresses for address in device.get("addr_info", [])
              if address.get("local") == args.address}
    if owners != {args.interface}:
        raise RuntimeError(f"Control address {args.address} must belong only to {args.interface}; found {sorted(owners)}")
    route = json.loads(run("ip", "-j", "-4", "route", "get", PEERS[args.address]))[0]
    if route.get("dev") != args.interface or route.get("prefsrc") != args.address:
        raise RuntimeError(f"Control traffic is not using USB Wi-Fi: {route}")
    expected = {"connection.autoconnect": {"yes"},
                "connection.interface-name": {args.interface},
                "802-11-wireless.mac-address": {mac.upper(), mac.lower()},
                "802-11-wireless.powersave": {"2", "disable"},
                "802-11-wireless.band": {"a"}}
    for name, allowed in expected.items():
        value = field(args.profile, name)
        if value not in allowed:
            raise RuntimeError(f"{args.profile}: {name}={value!r}; expected {sorted(allowed)}")
    if field(args.wired_profile, "ipv4.method") != "auto":
        raise RuntimeError("Setup Ethernet must use DHCP")
    return {"address": args.address, "interface": args.interface, "mac": mac,
            "peer": PEERS[args.address], "route": route, "setup_ethernet": "DHCP",
            "wifi_autoconnect": True, "wifi_powersave": False, "wifi_band": "5 GHz"}


def install(args):
    mac = validate_adapter(args.interface)
    if field(args.profile, "connection.type") != "802-11-wireless":
        raise ValueError("The selected profile is not Wi-Fi")
    if field(args.wired_profile, "connection.type") != "802-3-ethernet":
        raise ValueError("The setup profile is not Ethernet")
    wired_device = getattr(args, "wired_interface", None) or field(args.wired_profile, "connection.interface-name")
    if not wired_device:
        # Inactive setup profiles may not bind an interface; find its active device.
        wired_device = field(args.wired_profile, "GENERAL.DEVICES")
    if not wired_device or wired_device == "--":
        raise ValueError("Cannot identify the setup Ethernet interface")

    backup = Path(args.backup_dir)
    backup.mkdir(parents=True, exist_ok=False, mode=0o700)
    previous = {}
    for profile in (args.profile, args.wired_profile):
        names = PROFILE_FIELDS + (("802-11-wireless.mac-address", "802-11-wireless.band",
                                   "802-11-wireless.powersave") if profile == args.profile else ())
        previous[profile] = {name: field(profile, name) for name in names}
    (backup / "before.json").write_text(json.dumps(previous, indent=2) + "\n")

    # Remove the fixed robot identity from Ethernet before activating it on Wi-Fi.
    run("nmcli", "connection", "modify", args.wired_profile,
        "ipv4.method", "auto", "ipv4.addresses", "", "ipv4.gateway", "",
        "ipv4.dns", "", "ipv4.routes", "", "ipv4.route-metric", "600",
        "connection.autoconnect", "yes")
    run("nmcli", "connection", "modify", args.profile,
        "connection.interface-name", args.interface, "802-11-wireless.mac-address", mac,
        "connection.autoconnect", "yes", "connection.autoconnect-priority", "100",
        "802-11-wireless.band", "a", "802-11-wireless.powersave", "2",
        "ipv4.method", "manual", "ipv4.addresses", args.address + "/24",
        "ipv4.gateway", "192.168.50.1", "ipv4.dns", "192.168.50.1",
        "ipv4.routes", PEERS[args.address] + "/32", "ipv4.route-metric", "100")
    # Activating the revised Ethernet profile removes the old live static address.
    # Setup DHCP failure must not prevent bringing up the operating Wi-Fi link.
    try:
        run("nmcli", "--wait", "20", "connection", "up", "id", args.wired_profile,
            "ifname", wired_device, timeout=25)
    except (RuntimeError, subprocess.TimeoutExpired):
        try:
            run("nmcli", "device", "disconnect", wired_device)
        except RuntimeError:
            addresses = json.loads(run("ip", "-j", "-4", "address", "show"))
            if any(device["ifname"] == wired_device and address.get("local") == args.address
                   for device in addresses for address in device.get("addr_info", [])):
                raise RuntimeError("Could not remove the control address from Ethernet")
    run("nmcli", "--wait", "25", "connection", "up", "id", args.profile,
        "ifname", args.interface, timeout=30)
    result = inspect(args)
    (backup / "after.json").write_text(json.dumps(result, indent=2) + "\n")
    return dict(result, backup=str(backup))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true")
    mode.add_argument("--install", action="store_true")
    parser.add_argument("--interface", required=True)
    parser.add_argument("--address", choices=PEERS, required=True)
    parser.add_argument("--profile", default="Stretch-WiFi-AP-3128")
    parser.add_argument("--wired-profile", default="Wired connection 1")
    parser.add_argument("--wired-interface", help="Ethernet interface, also when no setup cable is attached")
    stamp = datetime.datetime.now().strftime("%Y%m%dT%H%M%S")
    parser.add_argument("--backup-dir", default=f"/var/lib/pg-llm-service-robot/network/usb-wifi-{stamp}")
    args = parser.parse_args()
    if args.install and os.geteuid() != 0:
        parser.error("--install requires root; it changes persistent NetworkManager profiles")
    try:
        print(json.dumps(install(args) if args.install else inspect(args), indent=2))
    except (OSError, RuntimeError, ValueError, subprocess.TimeoutExpired) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
