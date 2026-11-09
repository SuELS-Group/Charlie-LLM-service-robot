"""Latest-only battery telemetry and durable, one-attempt charging episodes."""
import json
import math
import os
from pathlib import Path
import tempfile
import threading
import time
import uuid

from .battery_announcements import BatteryAnnouncements


class BatteryReturnPolicy:
    def __init__(self, *, delay=10., max_age=4., rearm_after=30., state_path=None,
                 clock=time.monotonic, wall=time.time, boot_id=None):
        self.delay,self.max_age,self.rearm_after=float(delay),float(max_age),float(rearm_after)
        if (not all(math.isfinite(value) for value in (self.delay,self.max_age,self.rearm_after)) or
                self.delay<10. or not .5<=self.max_age<=10. or self.rearm_after<10.):
            raise ValueError('Invalid automatic charging timing configuration')
        self.clock,self.wall=clock,wall
        self.lock=threading.RLock()
        self.latest=None
        self.received=0.
        self.orange_since=self.healthy_since=self.connected_since=None
        self.attempted=False
        self.claim_id=None
        self.return_state='idle'
        self.return_reason=None
        self.notice=None
        self.announcements=BatteryAnnouncements(clock=clock)
        self.path=Path(state_path).expanduser() if state_path else None
        try:self.boot_id=boot_id or Path('/proc/sys/kernel/random/boot_id').read_text().strip()
        except OSError:self.boot_id='unknown_boot'
        if self.path and self.path.exists():
            try:
                saved=json.loads(self.path.read_text())
                if saved.get('version')!=1 or not isinstance(saved.get('attempted'),bool) or not isinstance(saved.get('boot_id'),str):
                    raise ValueError('Invalid charging return marker')
                if saved.get('boot_id')==self.boot_id:
                    self.attempted=bool(saved['attempted'])
                    self.return_state='previous_attempt' if self.attempted else 'idle'
            except (OSError,ValueError,KeyError,TypeError):
                self.attempted=True
                self.return_state='state_unavailable'

    def _persist(self):
        if self.path is None:
            return True
        temporary=None
        try:
            self.path.parent.mkdir(parents=True,exist_ok=True)
            with tempfile.NamedTemporaryFile(mode='w',dir=self.path.parent,delete=False) as stream:
                temporary=stream.name
                json.dump({'version':1,'boot_id':self.boot_id,'attempted':self.attempted},stream)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary,self.path)
            return True
        except OSError:
            self.attempted=True
            self.return_state='state_unavailable'
            self.return_reason='persistence_failed'
            return False
        finally:
            if temporary and os.path.exists(temporary):
                try:os.unlink(temporary)
                except OSError:pass

    def update(self, event):
        if not isinstance(event,dict) or event.get('type')!='battery_state' or event.get('source')!='battery_monitor':
            return False
        try:
            issued=float(event['issued_at'])
            captured=float(event['sampled_at'])
            level=event['level']
            if (not all(math.isfinite(value) for value in (issued,captured)) or
                    not -.5 <= self.wall()-issued <= self.max_age or level not in {'green','yellow','orange','red','unknown'} or
                    (event.get('charger_connected') is not None and not isinstance(event['charger_connected'],bool))):
                return False
            metrics={}
            for key in ('voltage','estimated_percentage'):
                value=event.get(key)
                if value is not None and (isinstance(value,bool) or not isinstance(value,(int,float)) or not math.isfinite(value)):
                    return False
                metrics[key]=value
        except (KeyError,TypeError,ValueError,OverflowError):
            return False
        with self.lock:
            if self.latest and issued<=self.latest['issued_at']:
                return False
            now=self.clock()
            continuous=self.latest is not None and now-self.received<=self.max_age
            previous=(self.latest or {}).get('level')
            self.latest={key:event.get(key) for key in ('level','issued_at','sampled_at','charger_connected',
                'charging','charging_state','voltage','estimated_percentage','sample_valid')}
            self.latest.update(issued_at=issued,sampled_at=captured,**metrics)
            self.latest['charging']=event.get('charging') if isinstance(event.get('charging'),bool) else None
            allowed={'unknown','charging','discharging','not charging','full'}
            charge_state=event.get('charging_state')
            self.latest['charging_state']=charge_state if isinstance(charge_state,str) and charge_state in allowed else 'unknown'
            self.latest['sample_valid']=event.get('sample_valid') is True
            self.received=now
            fresh=self._fresh()
            connected=event.get('charger_connected') is True
            if fresh and self._connected():
                self.announcements.warned_levels.clear()
            orange=fresh and level=='orange' and event.get('charger_connected') is False
            if orange:
                if self.orange_since is None or not continuous:
                    self.orange_since=now
            else:
                self.orange_since=None
            if fresh and level in {'green','yellow'}:
                if self.healthy_since is None or not continuous:self.healthy_since=now
            else:self.healthy_since=None
            if fresh and connected:
                if self.connected_since is None or not continuous:self.connected_since=now
            else:self.connected_since=None
            recovered=(self.healthy_since is not None and now-self.healthy_since>=self.rearm_after)
            unplug_cycle=(self.connected_since is not None and now-self.connected_since>=10.)
            if self.attempted and self.claim_id is None and (recovered or unplug_cycle):
                self.attempted=False
                self.return_state='idle'
                self.return_reason=None
                self._persist()
            if level!=previous:
                self.notice=level
            return True

    def _fresh(self):
        return bool(self.latest and self.latest.get('sample_valid') is True and
            self.clock()-self.received<=self.max_age and
            -.5<=self.wall()-self.latest['sampled_at']<=self.max_age)

    def eligible(self):
        with self.lock:
            return bool(not self.attempted and self.claim_id is None and self._fresh() and
                self.latest['level']=='orange' and self.latest['charger_connected'] is False and
                self.orange_since is not None and self.clock()-self.orange_since>=self.delay)

    def claim(self):
        with self.lock:
            if not self.eligible():return None
            self.attempted=True
            if not self._persist():return None
            self.notice=None
            self.claim_id=uuid.uuid4().hex
            self.return_state='checking'
            return self.claim_id

    def valid_start(self, claim_id):
        with self.lock:
            return bool(claim_id and claim_id==self.claim_id and self._fresh() and
                self.latest['level']=='orange' and self.latest['charger_connected'] is False and
                self.orange_since is not None and self.clock()-self.orange_since>=self.delay)

    def finish(self, state, reason=None):
        with self.lock:
            self.claim_id=None
            self.return_state=str(state)
            self.return_reason=reason

    def suppress(self):
        with self.lock:
            self.attempted=True
            self._persist()
            self.orange_since=None
            self.return_state='stopped'

    def set_phase(self, phase):
        with self.lock:self.return_state=phase

    def cancellation_reason(self):
        with self.lock:
            if not self.claim_id:return None
            if not self._fresh():return 'battery_data_stale'
            if self.latest['level']=='red':return 'battery_critical'
            return None

    def take_notice(self):
        with self.lock:
            if self.notice is None or not self._fresh():
                return None
            self.notice=None
            return self.announcements.choose(self.latest['level'], connected=self._connected())

    def _connected(self):
        return bool(self.latest and (self.latest.get('charger_connected') is True or
            self.latest.get('charging') is True or self.latest.get('charging_state') in {'charging','full'}))

    def set_announcement_mode(self, mode):
        with self.lock:
            if mode != self.announcements.mode:
                self.announcements.set_mode(mode)
                self.notice=None  # A mode change never replays an old notification.

    def legacy_notice(self, level):
        with self.lock:
            return self.announcements.choose(level, connected=self._connected())

    def snapshot(self):
        with self.lock:
            data=dict(self.latest or {'level':'unknown'})
            data['fresh']=self._fresh()
            data['sample_age_seconds']=round(max(0.,self.wall()-self.latest['sampled_at']),2) if self.latest else None
            return {'battery':data,'battery_announcements':{'mode':self.announcements.mode},'automatic_charging':{'state':self.return_state,
                'reason':self.return_reason,'attempted':self.attempted,'waypoint':'charging station'}}
