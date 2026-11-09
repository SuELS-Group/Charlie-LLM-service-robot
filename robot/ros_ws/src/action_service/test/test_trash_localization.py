import json
import math
from types import SimpleNamespace
from unittest.mock import Mock

import numpy as np
import pytest
import yaml
from action_service import place_item_action as module
from action_service.place_item_action import PlaceItemAction
from test_take_from_hand_action import _node


def marker(**values):
    return {'class_id': 'trash_can', 'marker_id': 18, 'source': 'aruco',
            'x': 1.5, 'y': .2, 'z': .48, 'score': 1., **values}


def action():
    result = PlaceItemAction(_node())
    result.motion = Mock()
    result.node.set_success.side_effect = lambda response, message, data='': ('success', message, data)
    result.node.set_failure.side_effect = lambda response, message, data='': ('failure', message, data)
    return result


@pytest.mark.parametrize('values', [dict(marker_id=5), dict(marker_id=19),
    dict(marker_id=None), dict(marker_id=True), dict(source='obj'), dict(source=''),
    dict(class_id='table')])
def test_trash_rejects_other_markers_and_non_aruco_detections(values):
    candidate = action()
    assert candidate._check_for_surface_detections('trash_can', [marker(**values)])[0] is None


def test_only_configured_id_can_win_head_or_gripper_selection():
    candidate = action()
    wanted = marker(score=.4)
    raw = [marker(marker_id=5), marker(source='obj'), wanted]
    candidate._scan_detections = raw
    candidate._gripper_detections = raw
    assert candidate._check_for_surface_detections('trash_can')[0] is wanted
    assert candidate._check_for_surface_detections('trash_can', candidate._gripper_detections)[0] is wanted


def test_visible_marker_is_used_before_any_head_movement():
    candidate = action()
    candidate.motion.sleep.side_effect = lambda seconds: candidate._scan_detections.append(marker())
    assert candidate._look_at_surface_and_refresh_hint('trash_can') == .48
    np.testing.assert_allclose(candidate._place_hint_map, [1.5, .2, .48])
    candidate.motion.move_head.assert_not_called()
    assert not candidate._scan_active


def test_search_after_left_turn_aims_at_the_observed_world_target():
    candidate = action()
    candidate._hint_in_base_frame = Mock(return_value=[.35, -.65, .48])
    def collect(seconds):
        if candidate.motion.move_head.called:
            candidate._scan_detections.append(marker())
    candidate.motion.sleep.side_effect = collect
    assert candidate._look_at_surface_and_refresh_hint('trash_can') == .48
    candidate.motion.move_head.assert_called_once()
    assert candidate.motion.move_head.call_args.args[0] == pytest.approx(math.atan2(-.65, .35))


def test_stop_in_current_view_never_starts_a_sweep():
    candidate = action()
    candidate.motion.sleep.side_effect = lambda seconds: candidate._stop_evt.set()
    assert candidate._look_at_surface_and_refresh_hint('trash_can') is None
    candidate.motion.move_head.assert_not_called()
    assert not candidate._scan_active


@pytest.mark.parametrize('stamp', [98., 100.1, float('nan'), None])
def test_stale_future_and_invalid_detection_stamps_are_not_transformed(monkeypatch, stamp):
    candidate = action()
    candidate._scan_surface = 'trash_can'
    transform = Mock()
    monkeypatch.setattr(module, 'collect_transformed_detection_records', transform)
    candidate._collect_surface_detections(SimpleNamespace(data=json.dumps({
        'header': {'stamp': stamp}, 'detections': []})), 'map', [])
    transform.assert_not_called()


def test_fresh_marker_payload_is_transformed(monkeypatch):
    candidate = action()
    candidate._scan_surface = 'trash_can'
    transform = Mock(return_value=[marker()])
    monkeypatch.setattr(module, 'collect_transformed_detection_records', transform)
    records = []
    candidate._collect_surface_detections(SimpleNamespace(data=json.dumps({
        'header': {'stamp': 99.9}, 'detections': []})), 'map', records)
    assert records == [marker()]
    transform.assert_called_once()


@pytest.mark.parametrize('hint,positioned', [([1.66, -.04, .48], False),
    ([.65, 0., .48], False), ([0., -.65, .48], True)])
def test_localization_returns_guarded_approach_target_without_arm_motion(hint, positioned):
    candidate = action()
    def observe(surface):
        candidate._place_hint_map = np.array([1.5, .2, .48])
        return .48
    candidate._look_at_surface_and_refresh_hint = Mock(side_effect=observe)
    candidate._hint_in_base_frame = Mock(return_value=hint)
    result = candidate.localize_trash(None, SimpleNamespace())
    assert result[0] == 'success'
    target = json.loads(result[2])
    assert target['positioned'] is positioned
    assert target['marker_id'] == 18 and target['source'] == 'aruco'
    assert target['x'] == 1.5 and target['approach_distance'] == .65
    assert not candidate.place_active
    candidate.motion.send_full_trajectory.assert_not_called()
    candidate.motion.open_gripper.assert_not_called()
    candidate.motion.rotate_base.assert_not_called()


def test_missing_marker_cannot_reuse_target_from_a_previous_request():
    candidate = action()
    candidate._place_hint_map = np.array([1.5, .2, .48])
    candidate._look_at_surface_and_refresh_hint = Mock(return_value=None)
    assert candidate.localize_trash(None, SimpleNamespace())[0] == 'failure'
    assert candidate._place_hint_map is None
    candidate.motion.open_gripper.assert_not_called()


def test_marker_configuration_matches_vision_label():
    from pathlib import Path
    root = Path(__file__).resolve().parents[5]
    vision = yaml.safe_load((root / 'workstation/ament_ws/src/vision_node/config/aruco_processor.yaml').read_text())
    assert [entry['id'] for entry in vision['markers'] if entry['label'] == 'trash_can'] == [module.config.trash.marker_id]
