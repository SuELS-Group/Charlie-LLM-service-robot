import json
import threading
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from unittest.mock import Mock, patch

import pytest

from workstation_common.battery_announcements import BatteryAnnouncementSettings
from web_bridge_node.web_bridge_node import WebBridgeNode


@pytest.fixture
def bridge(tmp_path):
    # Exercise the real HTTP handler without a ROS graph, executor or robot.
    node = object.__new__(WebBridgeNode)
    node._lock = threading.Lock()
    node._battery_announcement_settings = BatteryAnnouncementSettings(tmp_path / 'settings.json')
    node._battery_announcement_publisher = Mock()
    node._battery_estimator = Mock()
    node._battery_estimator.snapshot.return_value = {'type': 'battery', 'available': False}
    node.host, node.http_port = '127.0.0.1', 0
    node.llm_input_path = '/api/llm-input'
    node.get_logger = Mock()
    node._http_thread = None
    node._start_http_server()
    url = 'http://127.0.0.1:' + str(node._http_server.server_port)
    yield node, url
    node._http_server.shutdown()
    node._http_server.server_close()
    node._http_thread.join(timeout=2)


def post(url, payload):
    request = Request(url + '/api/battery/announcements', data=json.dumps(payload).encode(),
        headers={'Content-Type': 'application/json'}, method='POST')
    return urlopen(request, timeout=2)


def test_http_persists_and_publishes_mode_even_without_battery_data(bridge):
    node, url = bridge
    with urlopen(url + '/api/battery', timeout=2) as response:
        assert json.load(response) == {'type': 'battery', 'available': False, 'announcement_mode': 'reduced'}
    for mode in ['off', 'full', 'reduced']:
        with post(url, {'mode': mode}) as response:
            assert json.load(response) == {'mode': mode}
        assert BatteryAnnouncementSettings(node._battery_announcement_settings.path).mode == mode
        assert node._battery_announcement_publisher.publish.call_args.args[0].data == mode
        with urlopen(url + '/api/battery', timeout=2) as response:
            assert json.load(response)['announcement_mode'] == mode


@pytest.mark.parametrize('payload', [{'mode': 'bad'}, {'mode': []}, {}, [], None, {'mode': 'off', 'extra': True}])
def test_bad_http_request_cannot_change_or_publish_mode(bridge, payload):
    node, url = bridge
    with pytest.raises(HTTPError) as caught:
        post(url, payload)
    assert caught.value.code == 400
    assert node._battery_announcement_settings.mode == 'reduced'
    node._battery_announcement_publisher.publish.assert_not_called()


def test_failed_persistence_is_reported_without_publishing(bridge):
    node, url = bridge
    with patch.object(node._battery_announcement_settings, 'save', side_effect=OSError):
        with pytest.raises(HTTPError) as caught:
            post(url, {'mode': 'off'})
    assert caught.value.code == 500
    node._battery_announcement_publisher.publish.assert_not_called()
