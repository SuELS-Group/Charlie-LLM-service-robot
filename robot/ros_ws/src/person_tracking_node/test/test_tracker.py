import pytest
from person_tracking_node.tracker import Observation, PersonTracker, TrackerSettings


def observation(timestamp, source_id, x, y=0.0, name='', distance=None):
    return Observation(timestamp=timestamp, source='head_camera', source_track_id=source_id, x=x, y=y, face_name=name, face_distance=distance, )


def test_named_person_survives_loss_but_navigation_expires_first():
    tracker = PersonTracker()
    tracker.update([observation(10.0, 1, 2.0, name='TestUserB', distance=0.42)])
    recent = tracker.snapshots(30.0)[0]
    assert recent.face_name == 'TestUserB'
    assert recent.state == 'last_known'
    assert recent.navigation_eligible is True
    stale = tracker.snapshots(131.0)[0]
    assert stale.navigation_eligible is False
    assert tracker.snapshots(311.0) == []


def test_new_yolo_id_reacquires_named_track_by_position():
    tracker = PersonTracker()
    first = tracker.update([observation(1.0, 10, 1.0, name='TestUserC', distance=0.38)])[0]
    reacquired = tracker.update([observation(2.0, 11, 1.35)])[0]
    assert reacquired.track_id == first.track_id
    assert reacquired.face_name == 'TestUserC'
    assert tracker.source_bindings[('head_camera', 11)][0] == first.track_id


def test_named_track_can_be_spatially_reacquired_for_two_minutes():
    tracker = PersonTracker()
    named = tracker.update([observation(0.0, 10, 1.0, name='TestUserC', distance=0.38)])[0]
    reacquired = tracker.update([observation(120.0, 11, 1.5)])[0]
    assert reacquired.track_id == named.track_id
    assert reacquired.face_name == 'TestUserC'


def test_named_track_is_not_spatially_reacquired_after_two_minutes():
    tracker = PersonTracker(TrackerSettings(anonymous_observations=1))
    named = tracker.update([observation(0.0, 10, 1.0, name='TestUserC', distance=0.38)])[0]
    anonymous = tracker.update([observation(120.01, 11, 1.5)])[0]
    assert anonymous.track_id != named.track_id
    assert anonymous.face_name == ''
    assert ('head_camera', 11) in tracker.source_bindings
    assert tracker.source_bindings[('head_camera', 11)][0] == (anonymous.track_id)


def test_position_updates_velocity_while_source_track_is_visible():
    tracker = PersonTracker()
    tracker.update([observation(1.0, 3, 0.0, name='TestUserA', distance=0.4)])
    track = tracker.update([observation(2.0, 3, 0.8)])[0]
    assert track.state[0] > 0.5
    assert track.state[2] > 0.0


def test_ambiguous_crossing_does_not_transfer_either_name():
    settings = TrackerSettings(anonymous_observations=1)
    tracker = PersonTracker(settings)
    tracker.update([observation(1.0, 1, 0.0, name='TestUserC', distance=0.4),
        observation(1.0, 2, 0.4, name='TestUserB', distance=0.4), ])
    new_track = tracker.update([observation(1.1, 3, 0.2)])[0]
    snapshots = tracker.snapshots(1.1)
    assert new_track.face_name == ''
    assert new_track.ambiguous is True
    assert {item.face_name for item in snapshots if item.face_name} == {'TestUserC', 'TestUserB'}
    assert all(not item.navigation_eligible for item in snapshots)


def test_crowd_near_one_last_position_does_not_guess_identity():
    settings = TrackerSettings(anonymous_observations=1)
    tracker = PersonTracker(settings)
    tracker.update([observation(0.0, 1, 0.0, name='TestUserB', distance=0.4)])
    tracker.update([observation(5.0, 20, 0.15), observation(5.0, 21, 0.25), ])
    snapshots = tracker.snapshots(5.0)
    testuserb = next(item for item in snapshots if item.face_name == 'TestUserB')
    assert testuserb.age_seconds == 5.0
    assert testuserb.state == 'ambiguous'
    assert testuserb.navigation_eligible is False


def test_duplicate_face_name_in_one_frame_names_only_best_observation():
    settings = TrackerSettings(anonymous_observations=1)
    tracker = PersonTracker(settings)
    tracks = tracker.update([observation(1.0, 1, 0.0, name='TestUserB', distance=0.52),
        observation(1.0, 2, 2.0, name='TestUserB', distance=0.41), ])
    named = [track for track in tracks if track.face_name == 'TestUserB']
    assert len(named) == 1
    assert named[0].state[0] == pytest.approx(2.0)


def test_anonymous_track_requires_three_observations():
    tracker = PersonTracker()
    tracker.update([observation(1.0, 7, 1.0)])
    assert tracker.snapshots(1.0) == []
    tracker.update([observation(1.1, 7, 1.05)])
    assert tracker.snapshots(1.1) == []
    tracker.update([observation(1.2, 7, 1.10)])
    assert len(tracker.snapshots(1.2)) == 1


def test_anonymous_track_is_hidden_when_unseen_and_expires_quickly():
    settings = TrackerSettings(anonymous_observations=1, visible_seconds=0.5, anonymous_track_expiry_seconds=3.0, )
    tracker = PersonTracker(settings)
    tracker.update([observation(1.0, 7, 1.0)])
    assert len(tracker.snapshots(1.4)) == 1
    assert tracker.snapshots(1.6) == []
    assert len(tracker.tracks) == 1
    assert tracker.snapshots(4.1) == []
    assert tracker.tracks == {}


def test_recognition_merges_temporary_anonymous_source_track():
    settings = TrackerSettings(anonymous_observations=1)
    tracker = PersonTracker(settings)
    named = tracker.update([observation(1.0, 1, 0.0, name='TestUserB', distance=0.4)])[0]
    anonymous = tracker.update([observation(2.0, 8, 3.0)])[0]
    assert anonymous.track_id != named.track_id
    assert len(tracker.tracks) == 2
    promoted = tracker.update([observation(2.1, 8, 3.0, name='TestUserB', distance=0.38)])[0]
    assert promoted.track_id == named.track_id
    assert anonymous.track_id not in tracker.tracks
    assert set(tracker.tracks) == {named.track_id}
    assert tracker.source_bindings[('head_camera', 8)][0] == named.track_id
