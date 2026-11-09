"""Task-level possession assumptions; deliberately not a sensor measurement."""
import json
import os
from pathlib import Path
import tempfile
import threading
import uuid

OBJECT_ACTIONS = frozenset({'take_from_hand', 'give_to_person', 'grab_item', 'release_item', 'place_item'})


class ObjectState:
    def __init__(self, state_path=None, *, boot_id=None, reset_on_start=False):
        self.lock = threading.RLock()
        self.state = 'empty'
        self.source = 'startup_assumed_empty'
        self.object_id = None
        self._human_revision = 0
        self._action_human_revision = None
        if state_path is not None and '{boot_id}' in str(state_path):
            # A distinct journal per Linux boot gives every power-on its empty
            # starting assumption without forgetting a pickup on a node restart.
            boot_id = str(uuid.UUID(boot_id or Path('/proc/sys/kernel/random/boot_id').read_text().strip()))
            state_path = str(state_path).replace('{boot_id}', boot_id)
        self.state_path = Path(state_path).expanduser() if state_path is not None else None
        if reset_on_start:
            self._persist()
            return
        if self.state_path is not None:
            try:
                record = json.loads(self.state_path.read_text())
                if (not isinstance(record, dict) or record.get('version') != 1 or
                        record.get('possession') not in {'empty', 'holding_assumed', 'unknown'} or
                        not isinstance(record.get('possession_source'), str) or not record['possession_source'].strip() or
                        (record.get('assumed_object_id') is not None and not isinstance(record['assumed_object_id'], str)) or
                        (record['possession'] != 'holding_assumed' and record.get('assumed_object_id') is not None)):
                    raise ValueError('Invalid object-state record')
                self.state = record['possession']
                self.source = record['possession_source']
                self.object_id = record.get('assumed_object_id')
            except FileNotFoundError:
                pass  # A fresh setup starts with the explicitly documented empty assumption.
            except (OSError, ValueError, TypeError):
                self.state = 'unknown'
                self.source = 'state_file_unavailable_requires_operator_check'

    def _persist(self, record=None):
        if self.state_path is None:
            return
        record = record if record is not None else self.snapshot()
        temporary = None
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(mode='w', dir=self.state_path.parent,
                                             prefix=self.state_path.name + '.', delete=False) as stream:
                temporary = Path(stream.name)
                json.dump({'version': 1, **record}, stream)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.state_path)
            if hasattr(os, 'O_DIRECTORY'):
                directory = os.open(self.state_path.parent, os.O_DIRECTORY)
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
        except (OSError, ValueError, TypeError):
            self.state, self.source, self.object_id = 'unknown', 'state_store_error_requires_operator_check', None
            raise
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def begin(self, action_name):
        if action_name in OBJECT_ACTIONS:
            with self.lock:
                self._action_human_revision = self._human_revision
                # Keep the last completed assumption in memory, but leave a
                # durable unknown record before any workflow can move a gripper.
                self._persist({'possession': 'unknown',
                    'possession_source': 'unfinished_' + action_name,
                    'possession_sensor_verified': False, 'assumed_object_id': None})

    def snapshot(self):
        with self.lock:
            return {'possession': self.state, 'possession_source': self.source,
                    'possession_sensor_verified': False, 'assumed_object_id': self.object_id,
                    'holding_item': None if self.state == 'unknown' else self.state == 'holding_assumed'}

    def set(self, state, source):
        if state not in {'empty', 'holding_assumed', 'unknown'}:
            raise ValueError('Invalid object state')
        with self.lock:
            if source in {'explicit_user_empty_report', 'explicit_user_holding_report'}:
                self._human_revision += 1
                self.object_id = None  # A human reports possession, not the old workflow's object identity.
            self.state, self.source = state, source
            if state != 'holding_assumed':
                self.object_id = None
            self._persist()

    def label_assumed_object(self, identifier):
        with self.lock:
            if self.state == 'holding_assumed':
                self.object_id = identifier or None
                self._persist()

    def admission_error(self, action):
        with self.lock:
            if action in {'take_from_hand', 'grab_item'} and self.state != 'empty':
                return 'Gripper must be confirmed empty before pickup'
            if action in {'give_to_person', 'release_item', 'place_item'} and self.state != 'holding_assumed':
                return 'No held item is recorded; provide a current gripper-state correction'
        return None

    def invalidate(self, action_name, source, action=None):
        """Record workflow uncertainty without erasing a newer human correction."""
        if action_name not in OBJECT_ACTIONS:
            return False
        with self.lock:
            overridden = (self._action_human_revision is not None and
                self._action_human_revision != self._human_revision)
            if overridden:
                return False
            if action_name in {'give_to_person', 'release_item', 'place_item'} and getattr(action, '_gripper_opened', False):
                self.set('empty', 'completed_release_workflow')
            elif action_name == 'take_from_hand' and getattr(action, '_object_secured', False):
                self.set('holding_assumed', 'completed_hand_pickup_command')
            else:
                self.set('unknown', source)
            return True

    def complete(self, action_name, *, success, action=None, object_id=None):
        if action_name not in OBJECT_ACTIONS:
            return False
        with self.lock:
            overridden = (self._action_human_revision is not None and
                self._action_human_revision != self._human_revision)
            if overridden:
                return False
            if action_name == 'take_from_hand':
                if getattr(action, '_object_secured', False):
                    self.set('holding_assumed', 'completed_hand_pickup_command')
                elif getattr(action, '_grasp_attempted', False):
                    self.set('unknown', 'interrupted_gripper_close')
            elif action_name == 'give_to_person':
                if getattr(action, '_gripper_opened', False):
                    self.set('empty', 'completed_handover_open')
                elif getattr(action, '_release_attempted', False):
                    self.set('unknown', 'interrupted_gripper_open')
            elif action_name == 'grab_item':
                if success or getattr(action, '_grasp_completed', False):
                    self.set('holding_assumed', 'completed_grab_workflow')
                elif getattr(action, '_grasp_attempted', True):
                    self.set('unknown', 'incomplete_grab_workflow')
            elif action_name in {'release_item', 'place_item'}:
                if success or getattr(action, '_gripper_opened', False):
                    self.set('empty', 'completed_release_workflow')
                elif getattr(action, '_release_attempted', True):
                    self.set('unknown', 'incomplete_release_workflow')
            if action_name in {'take_from_hand', 'grab_item'} and self.state == 'holding_assumed':
                # State and identity belong to the same revision. Assigning an
                # action's label later could relabel a newer human correction.
                self.object_id = object_id or None
            # Also clear an unfinished journal when a workflow ended before
            # attempting a close/open, e.g. a normal missing-gesture timeout.
            self._persist()
            return True
