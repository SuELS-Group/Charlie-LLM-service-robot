"""Spoken battery notices and the Web Bridge's persistent mode setting."""
import json
import logging
import os
from pathlib import Path
import tempfile
import time


ANNOUNCEMENT_MODES = {'reduced', 'full', 'off'}
ANNOUNCEMENT_TOPIC = '/battery_node/announcement_mode'
DEFAULT_SETTINGS_FILE = '~/.local/state/charlie/battery-announcements.json'


class BatteryAnnouncements:
    def __init__(self, clock=time.monotonic):
        self.mode = 'reduced'
        self.clock = clock
        self.warned_levels = set()
        self.last_notice = -float('inf')

    def set_mode(self, mode):
        if not isinstance(mode, str) or mode not in ANNOUNCEMENT_MODES:
            raise ValueError('Invalid battery announcement mode')
        self.mode = mode

    def choose(self, level, *, connected=False):
        if connected:
            self.warned_levels.clear()
            return None
        if self.mode == 'off' or level not in {'green', 'yellow', 'orange', 'red'}:
            return None
        if self.mode == 'reduced':
            if level == 'green' or level in self.warned_levels:
                return None
        elif self.clock() - self.last_notice < 30.:
            return None
        self.warned_levels.add(level)
        self.last_notice = self.clock()
        return level


class BatteryAnnouncementSettings:
    def __init__(self, path=DEFAULT_SETTINGS_FILE):
        self.path = Path(path).expanduser()
        self.mode = 'reduced'
        try:
            saved = json.loads(self.path.read_text())
            mode = saved['mode']
            if not isinstance(mode, str) or mode not in ANNOUNCEMENT_MODES:
                raise ValueError('Invalid battery announcement mode')
            self.mode = mode
        except FileNotFoundError:
            pass
        except (OSError, ValueError, TypeError, KeyError):
            logging.getLogger(__name__).warning('Cannot read battery announcement setting; using reduced')

    def save(self, mode):
        if not isinstance(mode, str) or mode not in ANNOUNCEMENT_MODES:
            raise ValueError('Invalid battery announcement mode')
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode='w', dir=self.path.parent, delete=False) as stream:
                temporary = stream.name
                json.dump({'mode': mode}, stream)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
            self.mode = mode
        finally:
            if temporary and os.path.exists(temporary):
                os.unlink(temporary)
