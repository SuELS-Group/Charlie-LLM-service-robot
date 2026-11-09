from network_config import select_discovery_ip


def test_configured_workstation_address_wins_over_other_private_interfaces():
    addresses = ['172.17.0.1', '192.168.50.156', '192.168.50.50']
    assert select_discovery_ip(addresses, preferred_hosts=['192.168.50.50', '192.168.50.49'], ) == '192.168.50.50'


def test_falls_back_to_first_usable_private_address():
    assert select_discovery_ip(['127.0.0.1', '169.254.1.2', '10.1.2.3'], ) == '10.1.2.3'


def test_returns_none_without_usable_address():
    assert select_discovery_ip(['127.0.0.1', 'not-an-address']) is None
