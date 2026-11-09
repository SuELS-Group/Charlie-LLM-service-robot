from __future__ import annotations

import ipaddress
import shutil
import subprocess
from collections.abc import Iterable
from typing import Optional


def select_discovery_ip(interface_addresses: Iterable[str], preferred_hosts: Iterable[str] = (), ) -> Optional[str]:
    addresses = list(dict.fromkeys(interface_addresses))
    active = set(addresses)
    for host in preferred_hosts:
        if host in active:
            return host
    for address in addresses:
        try:
            parsed = ipaddress.ip_address(address)
        except ValueError:
            continue
        if parsed.is_private and not parsed.is_loopback and not parsed.is_link_local:
            return address
    return None


def active_ipv4_interface_addresses() -> list[str]:
    ip_command = shutil.which('ip')
    if not ip_command:
        return []
    try:
        result = subprocess.run([ip_command,
                                 '-4',
                                 '-o',
                                 'addr',
                                 'show',
                                 'up',
                                 'scope',
                                 'global'], check=False, capture_output=True, text=True, )
    except OSError:
        return []
    if result.returncode != 0:
        return []
    addresses = []
    for line in result.stdout.splitlines():
        parts = line.split()
        if 'inet' not in parts:
            continue
        try:
            address = parts[parts.index('inet') + 1].split('/', 1)[0]
        except (IndexError, ValueError):
            continue
        if address not in addresses:
            addresses.append(address)
    return addresses


def detect_private_discovery_ip(preferred_hosts: Iterable[str] = (), ) -> Optional[str]:
    return select_discovery_ip(active_ipv4_interface_addresses(), preferred_hosts, )
