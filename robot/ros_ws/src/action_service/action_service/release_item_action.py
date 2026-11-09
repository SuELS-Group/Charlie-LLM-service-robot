import threading
from pathlib import Path

from robot_common import get_config

from action_service.common.motion import MotionHelper

config = get_config('action_service', Path(__file__).stem)


class ReleaseItemAction:

    def __init__(self, node):
        self.node = node
        self.log = node.log.sub('release')
        self._lock = threading.Lock()
        self._stop_evt = threading.Event()
        self.release_active = False
        self._release_attempted = False
        self._gripper_opened = False
        self.motion = MotionHelper(node, config=config, stop_evt=self._stop_evt, log_prefix='release')

    def stop(self):
        with self._lock:
            if not self.release_active:
                return
            self.release_active = False
        self.log.warning('ReleaseItemAction: external stop requested')
        self._stop_evt.set()

    def execute(self, request, response, parameters=None):
        self.log.info('release_item: starting')
        del request, parameters
        with self._lock:
            if self.release_active:
                return self.node.set_failure(response, 'Release already in progress')
            self.release_active = True
            self._stop_evt.clear()
            self._release_attempted = False
            self._gripper_opened = False
        if not self.motion.wait_for_traj_server(timeout_s=4.0):
            with self._lock:
                self.release_active = False
            if self._stop_evt.is_set():
                return self.node.set_stopped(response, 'Release operation was stopped')
            return self.node.set_failure(response, 'Release failed: trajectory controller not ready')
        try:
            switched = self.motion.switch_to_position_mode()
            if not switched:
                return self.node.set_failure(response, 'Release failed: position mode not confirmed')
            if self._stop_evt.is_set():
                return self.node.set_stopped(response, 'Release operation was stopped')
            self._release_attempted = True
            opened = self.motion.open_gripper(duration_s=1.2)
            self._gripper_opened = bool(opened)
            if opened:
                self.log.info('release_item: gripper opened')
            else:
                self.log.warning('release_item: gripper open command failed')
            self.motion.sleep(0.4)
            if self._stop_evt.is_set():
                return self.node.set_stopped(response, 'Release operation was stopped')
            if opened:
                if not self.motion.stow_empty_gripper():
                    if self._stop_evt.is_set():
                        return self.node.set_stopped(response, 'Release operation was stopped')
                    return self.node.set_failure(response, 'Object released, but resting posture was not confirmed')
                return self.node.set_success(response, 'Released item')
            return self.node.set_failure(response, 'Release failed: unable to open gripper')
        finally:
            try:
                self.motion.switch_to_navigation_mode()
            finally:
                with self._lock:
                    self.release_active = False
