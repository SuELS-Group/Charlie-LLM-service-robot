"""Fresh recipient evidence for tracked pickup and current-person delivery."""
import math
import time
import threading


def recipient_error_code(reason):
    return {
        'Handover requires exactly one visible person': 'RECIPIENT_AMBIGUOUS',
        'No person is currently visible': 'NO_PERSON_FOUND',
        'Multiple visible tracks match the requested recipient': 'RECIPIENT_AMBIGUOUS',
        'The requested recipient is not currently confirmed': 'TARGET_PERSON_NOT_FOUND',
        'Recipient camera evidence is stale or unavailable': 'RECIPIENT_VIEW_UNAVAILABLE',
        'Recipient position is not in the base frame': 'RECIPIENT_POSITION_INVALID',
        'Recipient tracking is unavailable': 'RECIPIENT_TRACKING_UNAVAILABLE',
        'The visible recipient changed during handover': 'RECIPIENT_CHANGED',
        'Recipient position/confidence is invalid': 'RECIPIENT_POSITION_INVALID',
        'Recipient is outside the checked handover range': 'RECIPIENT_OUT_OF_RANGE',
        'Recipient camera evidence is incomplete': 'RECIPIENT_VIEW_UNAVAILABLE',
    }.get(reason, 'RECIPIENT_NOT_CONFIRMED')


class RecipientEvidence:
    def __init__(self):
        self.frame = None
        self.received = 0.
        self._lock = threading.Lock()

    def update(self, payload):
        if not isinstance(payload, dict) or payload.get('source', 'obj') != 'obj':
            return
        try:
            stamp = float(payload['header']['stamp'])
            if not math.isfinite(stamp) or stamp <= 0:
                return
        except (KeyError, TypeError, ValueError):
            return
        with self._lock:
            if self.frame is not None and stamp <= float(self.frame['header']['stamp']):
                return  # Old/replayed frames cannot restore a departed recipient.
            self.frame, self.received = payload, time.monotonic()

    def check(self, *, now, person=None, track_id=None, max_age=1.2, current_visible=False):
        if current_visible:
            # Delivery deliberately accepts the current person rather than
            # proving continuity with a name/track selected before navigation.
            person, track_id = None, None
        with self._lock:
            frame, received = self.frame or {}, self.received
        try:
            stamp = float(frame['header']['stamp'])
            if (not math.isfinite(stamp) or not 0 <= now - stamp <= max_age or
                    time.monotonic() - received > max_age):
                return None, 'Recipient camera evidence is stale or unavailable'
            if frame['header']['frame_id'] != 'base_link':
                return None, 'Recipient position is not in the base frame'
            people = [p for p in frame['detections'] if isinstance(p, dict) and
                      str(p.get('label', '')).casefold().startswith('person')]
            if not people:
                return None, 'No person is currently visible'
            wanted = ' '.join(str(person or '').split()).casefold()
            candidates = [p for p in people if not wanted or
                ' '.join(str(p.get('face_name') or '').split()).casefold() == wanted]
            if not candidates:
                return None, 'The requested recipient is not currently confirmed'
            if track_id is not None:
                candidates = [p for p in candidates if p.get('track_id') == track_id]
                if not candidates:
                    return None, 'The visible recipient changed during handover'
                if len(candidates) != 1:
                    return None, 'Recipient tracking is unavailable'
            elif wanted and len(candidates) != 1:
                return None, 'Multiple visible tracks match the requested recipient'
            if not wanted and track_id is None:
                valid = []
                for index, candidate in enumerate(candidates):
                    try:
                        position = candidate['position_3d']
                        x, y = float(position['x']), float(position['y'])
                        confidence = float(candidate['confidence'])
                        identifier = candidate.get('track_id')
                        valid_track = isinstance(identifier,int) and not isinstance(identifier,bool) and identifier >= 0
                        if (all(math.isfinite(v) for v in (x,y,confidence)) and .5 <= confidence <= 1. and
                                (current_visible or valid_track)):
                            valid.append((math.hypot(x,y), index if current_visible else identifier, candidate))
                    except (KeyError,TypeError,ValueError,OverflowError):
                        continue
                if not valid:
                    return None, 'Recipient position/confidence is invalid'
                # Select before checking reach. A too-near nearest person must
                # not silently turn the request into a handover to someone else.
                _, identifier, target = min(valid, key=lambda entry: entry[:2])
                if not current_visible and sum(p.get('track_id') == identifier for p in people) != 1:
                    return None, 'Recipient tracking is unavailable'
            else:
                target = candidates[0]
            current_id = target.get('track_id')
            if not current_visible and (not isinstance(current_id, int) or isinstance(current_id, bool) or current_id < 0):
                return None, 'Recipient tracking is unavailable'
            if track_id is not None and current_id != track_id:
                return None, 'The visible recipient changed during handover'
            p = target['position_3d']
            x, y = float(p['x']), float(p['y'])
            distance = math.hypot(x, y)
            confidence = float(target['confidence'])
            if not all(math.isfinite(v) for v in (x, y, confidence)) or not .5 <= confidence <= 1.:
                return None, 'Recipient position/confidence is invalid'
            if not .8 <= distance <= 2.0:
                return None, 'Recipient is outside the checked handover range'
            return {**target, 'visible_person_count':len(people)}, None
        except (KeyError, TypeError, ValueError, OverflowError):
            return None, 'Recipient camera evidence is incomplete'
