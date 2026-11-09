from __future__ import annotations

import math
from dataclasses import dataclass, replace

import numpy as np
from scipy.optimize import linear_sum_assignment


def normalize_name(name: str | None) -> str:
    return ' '.join(str(name or '').split()).casefold()


@dataclass(frozen=True)
class Observation:
    timestamp: float
    source: str
    source_track_id: int
    x: float
    y: float
    z: float = 0.0
    confidence: float = 0.0
    face_name: str = ''
    face_distance: float | None = None

    @property
    def source_key(self) -> tuple[str, int]:
        return self.source, int(self.source_track_id)


@dataclass
class PersonTrack:
    track_id: int
    state: np.ndarray
    covariance: np.ndarray
    last_update: float
    last_seen: float
    z: float
    observations: int = 1
    face_name: str = ''
    face_distance: float | None = None
    last_source: str = ''
    ambiguous: bool = False
    confirmed: bool = False

    @staticmethod
    def transition(dt: float) -> np.ndarray:
        return np.array([[1.0, 0.0, dt, 0.0],
            [0.0, 1.0, 0.0, dt],
            [0.0, 0.0, 1.0, 0.0],
            [0.0, 0.0, 0.0, 1.0], ], dtype=np.float64)

    @staticmethod
    def process_noise(dt: float, acceleration_sigma: float) -> np.ndarray:
        dt2 = dt * dt
        base = np.array([[dt2 * dt2 / 4.0, 0.0, dt2 * dt / 2.0, 0.0],
            [0.0, dt2 * dt2 / 4.0, 0.0, dt2 * dt / 2.0],
            [dt2 * dt / 2.0, 0.0, dt2, 0.0],
            [0.0, dt2 * dt / 2.0, 0.0, dt2], ], dtype=np.float64)
        return base * float(acceleration_sigma) ** 2

    def predicted(self, timestamp: float, acceleration_sigma: float) -> tuple[np.ndarray, np.ndarray]:
        dt = max(0.0, float(timestamp) - float(self.last_update))
        transition = self.transition(dt)
        state = transition @ self.state
        covariance = transition @ self.covariance @ transition.T
        covariance += self.process_noise(dt, acceleration_sigma)
        return state, covariance

    def update(self, observation: Observation, measurement_sigma: float, acceleration_sigma: float) -> None:
        if observation.timestamp + 0.05 < self.last_update:
            return
        predicted_state, predicted_covariance = self.predicted(observation.timestamp, acceleration_sigma)
        measurement_matrix = np.array([[1.0, 0.0, 0.0, 0.0], [0.0, 1.0, 0.0, 0.0], ], dtype=np.float64)
        measurement_noise = np.eye(2, dtype=np.float64) * float(measurement_sigma) ** 2
        innovation = np.array([observation.x, observation.y], dtype=np.float64) - measurement_matrix @ predicted_state
        innovation_covariance = measurement_matrix @ predicted_covariance @ measurement_matrix.T + measurement_noise
        gain = predicted_covariance @ measurement_matrix.T @ np.linalg.inv(innovation_covariance)
        self.state = predicted_state + gain @ innovation
        identity = np.eye(4, dtype=np.float64)
        self.covariance = (identity - gain @ measurement_matrix) @ predicted_covariance
        self.last_update = float(observation.timestamp)
        self.last_seen = float(observation.timestamp)
        self.z = float(observation.z)
        self.observations += 1
        self.last_source = observation.source
        self.ambiguous = False
        if observation.face_name and normalize_name(observation.face_name) != 'unknown':
            self.face_name = observation.face_name.strip()
            self.face_distance = observation.face_distance
            self.confirmed = True


@dataclass
class TrackerSettings:
    visible_seconds: float = 0.5
    source_binding_seconds: float = 10.0
    spatial_reacquire_seconds: float = 120.0
    navigation_max_age_seconds: float = 120.0
    track_expiry_seconds: float = 300.0
    anonymous_track_expiry_seconds: float = 3.0
    base_gate_m: float = 0.35
    gate_growth_m_per_second: float = 0.6
    max_gate_m: float = 1.0
    ambiguity_margin_m: float = 0.25
    direct_track_max_jump_m: float = 2.0
    measurement_sigma_m: float = 0.15
    acceleration_sigma_mps2: float = 1.5
    navigation_max_stddev_m: float = 0.75
    anonymous_observations: int = 3


@dataclass
class TrackSnapshot:
    track_id: int
    face_name: str
    x: float
    y: float
    z: float
    velocity_x: float
    velocity_y: float
    last_seen: float
    age_seconds: float
    state: str
    source: str
    face_distance: float | None
    position_stddev: float
    navigation_eligible: bool


class PersonTracker:

    def __init__(self, settings: TrackerSettings | None = None):
        self.settings = settings or TrackerSettings()
        self.tracks: dict[int, PersonTrack] = {}
        self.source_bindings: dict[tuple[str, int], tuple[int, float]] = {}
        self.next_track_id = 1

    def update(self, observations: list[Observation], timestamp: float | None = None) -> list[PersonTrack]:
        if not observations:
            if timestamp is not None:
                self.expire(float(timestamp))
            return []
        observations = sorted(observations, key=lambda item: item.timestamp)
        now = float(timestamp if timestamp is not None else max(item.timestamp for item in observations))
        self.expire(now)
        observations = self._deduplicate_named_observations(observations)
        assignments: dict[int, int] = {}
        used_tracks: set[int] = set()
        ambiguous_observation_indices: set[int] = set()
        for index, observation in enumerate(observations):
            wanted_name = normalize_name(observation.face_name)
            if not wanted_name or wanted_name == 'unknown':
                continue
            bound = self._bound_track(observation, now)
            track = self._track_by_name(wanted_name)
            if (
                    track is not None and bound is not None and bound.track_id != track.track_id and not normalize_name(bound.face_name)):
                self._remove_track(bound.track_id)
                bound = None
            if track is None:
                if bound is not None and (not bound.face_name or normalize_name(bound.face_name) == wanted_name):
                    track = bound
            if track is None:
                track = self._new_track(observation)
            else:
                track.update(observation, self.settings.measurement_sigma_m, self.settings.acceleration_sigma_mps2)
            track.face_name = observation.face_name.strip()
            track.face_distance = observation.face_distance
            track.confirmed = True
            track.ambiguous = False
            assignments[index] = track.track_id
            used_tracks.add(track.track_id)
            self._bind(observation, track.track_id)
        for index, observation in enumerate(observations):
            if index in assignments:
                continue
            track = self._bound_track(observation, now)
            if track is None or track.track_id in used_tracks:
                continue
            predicted, _ = track.predicted(observation.timestamp, self.settings.acceleration_sigma_mps2)
            jump = math.hypot(observation.x - predicted[0], observation.y - predicted[1])
            if jump > self.settings.direct_track_max_jump_m:
                self.source_bindings.pop(observation.source_key, None)
                continue
            track.update(observation, self.settings.measurement_sigma_m, self.settings.acceleration_sigma_mps2)
            assignments[index] = track.track_id
            used_tracks.add(track.track_id)
            self._bind(observation, track.track_id)
        remaining_observations = [index for index in range(len(observations)) if index not in assignments]
        remaining_tracks = [track for track in self.tracks.values() if
                            track.track_id not in used_tracks and now - track.last_seen <= self.settings.spatial_reacquire_seconds]
        if remaining_observations and remaining_tracks:
            costs = np.full((len(remaining_observations), len(remaining_tracks)), np.inf, dtype=np.float64)
            for row, observation_index in enumerate(remaining_observations):
                observation = observations[observation_index]
                for column, track in enumerate(remaining_tracks):
                    elapsed = max(0.0, observation.timestamp - track.last_seen)
                    gate = min(self.settings.max_gate_m, self.settings.base_gate_m + self.settings.gate_growth_m_per_second * elapsed)
                    predicted, _ = track.predicted(observation.timestamp, self.settings.acceleration_sigma_mps2)
                    distance = math.hypot(observation.x - predicted[0], observation.y - predicted[1])
                    if distance <= gate:
                        costs[row, column] = distance
            ambiguous_rows = set()
            for row in range(costs.shape[0]):
                finite_columns = np.flatnonzero(np.isfinite(costs[row]))
                finite_costs = np.sort(costs[row, finite_columns])
                if finite_costs.size >= 2 and finite_costs[1] - finite_costs[0] < self.settings.ambiguity_margin_m:
                    ambiguous_rows.add(row)
                    for column in finite_columns:
                        remaining_tracks[column].ambiguous = True
            for column in range(costs.shape[1]):
                finite_rows = np.flatnonzero(np.isfinite(costs[:, column]))
                finite_costs = np.sort(costs[finite_rows, column])
                if finite_costs.size >= 2 and finite_costs[1] - finite_costs[0] < self.settings.ambiguity_margin_m:
                    best_cost = finite_costs[0]
                    close_rows = [row for row in finite_rows if
                        costs[row, column] - best_cost < self.settings.ambiguity_margin_m]
                    ambiguous_rows.update(close_rows)
                    remaining_tracks[column].ambiguous = True
            for row in ambiguous_rows:
                ambiguous_observation_indices.add(remaining_observations[row])
                costs[row, :] = np.inf
            finite_costs = np.where(np.isfinite(costs), costs, 1e6)
            rows, columns = linear_sum_assignment(finite_costs)
            for row, column in zip(rows, columns):
                if row in ambiguous_rows or not np.isfinite(costs[row, column]):
                    continue
                observation_index = remaining_observations[row]
                observation = observations[observation_index]
                track = remaining_tracks[column]
                if track.track_id in used_tracks:
                    continue
                track.update(observation, self.settings.measurement_sigma_m, self.settings.acceleration_sigma_mps2)
                assignments[observation_index] = track.track_id
                used_tracks.add(track.track_id)
                self._bind(observation, track.track_id)
        for index, observation in enumerate(observations):
            if index in assignments:
                continue
            track = self._new_track(observation)
            if index in ambiguous_observation_indices:
                track.ambiguous = True
            assignments[index] = track.track_id
            self._bind(observation, track.track_id)
        return [self.tracks[assignments[index]] for index in sorted(assignments)]

    def _deduplicate_named_observations(self, observations: list[Observation]) -> list[Observation]:
        best_by_name: dict[str, int] = {}
        result = list(observations)
        for index, observation in enumerate(observations):
            name = normalize_name(observation.face_name)
            if not name or name == 'unknown':
                continue
            current_index = best_by_name.get(name)
            current_distance = float('inf') if current_index is None or observations[
                current_index].face_distance is None else float(observations[current_index].face_distance)
            candidate_distance = float('inf') if observation.face_distance is None else float(observation.face_distance)
            if current_index is None or candidate_distance < current_distance:
                if current_index is not None:
                    result[current_index] = replace(result[current_index], face_name='', face_distance=None)
                best_by_name[name] = index
            else:
                result[index] = replace(result[index], face_name='', face_distance=None)
        return result

    def _new_track(self, observation: Observation) -> PersonTrack:
        named = bool(normalize_name(observation.face_name) and normalize_name(observation.face_name) != 'unknown')
        position_variance = self.settings.measurement_sigma_m ** 2
        track = PersonTrack(track_id=self.next_track_id, state=np.array([observation.x,
                                                                         observation.y,
                                                                         0.0,
                                                                         0.0], dtype=np.float64), covariance=np.diag([position_variance,
                                                                                                                      position_variance,
                                                                                                                      1.0,
                                                                                                                      1.0]).astype(np.float64), last_update=float(observation.timestamp), last_seen=float(observation.timestamp), z=float(observation.z), face_name=observation.face_name.strip() if named else '', face_distance=observation.face_distance if named else None, last_source=observation.source, confirmed=named or self.settings.anonymous_observations <= 1, )
        self.tracks[track.track_id] = track
        self.next_track_id += 1
        return track

    def _track_by_name(self, normalized_name: str) -> PersonTrack | None:
        matches = [track for track in self.tracks.values() if normalize_name(track.face_name) == normalized_name]
        return max(matches, key=lambda track: track.last_seen) if matches else None

    def _bound_track(self, observation: Observation, now: float) -> PersonTrack | None:
        binding = self.source_bindings.get(observation.source_key)
        if binding is None:
            return None
        track_id, bound_at = binding
        if now - bound_at > self.settings.source_binding_seconds:
            self.source_bindings.pop(observation.source_key, None)
            return None
        return self.tracks.get(track_id)

    def _bind(self, observation: Observation, track_id: int) -> None:
        self.source_bindings[observation.source_key] = (int(track_id), float(observation.timestamp))

    def _remove_track(self, track_id: int) -> None:
        self.tracks.pop(int(track_id), None)
        stale_bindings = [key for key, (bound_track_id, _) in self.source_bindings.items() if
            bound_track_id == int(track_id)]
        for key in stale_bindings:
            del self.source_bindings[key]

    def expire(self, now: float) -> list[int]:
        expired = [track_id for track_id, track in self.tracks.items() if float(now) - track.last_seen > (
            self.settings.track_expiry_seconds if normalize_name(track.face_name) else self.settings.anonymous_track_expiry_seconds)]
        for track_id in expired:
            self._remove_track(track_id)
        stale_bindings = [key for key, (track_id, bound_at) in self.source_bindings.items() if
            track_id not in self.tracks or float(now) - bound_at > self.settings.source_binding_seconds]
        for key in stale_bindings:
            del self.source_bindings[key]
        return expired

    def snapshots(self, now: float) -> list[TrackSnapshot]:
        self.expire(now)
        snapshots = []
        for track in sorted(self.tracks.values(), key=lambda item: item.track_id):
            if not track.confirmed and track.observations < self.settings.anonymous_observations:
                continue
            track.confirmed = True
            age = max(0.0, float(now) - track.last_seen)
            if not normalize_name(track.face_name) and age > self.settings.visible_seconds:
                continue
            measured_stddev = math.sqrt(max(0.0, float(np.max(np.diag(track.covariance)[:2]))))
            position_stddev = measured_stddev + min(0.6, age * 0.02)
            if track.ambiguous:
                state = 'ambiguous'
            elif age <= self.settings.visible_seconds:
                state = 'visible'
            else:
                state = 'last_known'
            navigation_eligible = bool(track.face_name and not track.ambiguous and age <= self.settings.navigation_max_age_seconds and position_stddev <= self.settings.navigation_max_stddev_m)
            snapshots.append(TrackSnapshot(track_id=track.track_id, face_name=track.face_name, x=float(
                track.state[0]), y=float(track.state[1]), z=float(track.z), velocity_x=float(
                track.state[2]), velocity_y=float(track.state[
                                                      3]), last_seen=float(track.last_seen), age_seconds=age, state=state, source=track.last_source, face_distance=track.face_distance, position_stddev=position_stddev, navigation_eligible=navigation_eligible, ))
        return snapshots
