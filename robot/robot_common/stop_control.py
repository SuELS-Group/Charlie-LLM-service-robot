"""Issue timestamps used to reject pre-stop commands at robot admission."""
import json
import math
import time


def stop_timestamp(message):
    stamp = message.stamp.sec + message.stamp.nanosec / 1e9
    return stamp if stamp > 0 else time.time()


def action_issue_time(parameters):
    try:
        params = json.loads(parameters) if parameters else {}
        value = params.get('_issued_at', 0.0)
        return float(value) if isinstance(value, (int, float)) and math.isfinite(value) else -1.0
    except (ValueError, TypeError, AttributeError):
        return -1.0


def issued_before_stop(issued_at, last_stop):
    return not math.isfinite(issued_at) or issued_at < 0 or (issued_at > 0 and issued_at <= last_stop)
