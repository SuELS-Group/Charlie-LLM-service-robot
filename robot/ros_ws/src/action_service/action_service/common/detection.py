import json
import math

import rclpy
from geometry_msgs.msg import PointStamped
from rclpy.duration import Duration
from tf2_geometry_msgs import do_transform_point


def fresh_detection_payload(node, msg, max_age=1.2):
    try:
        payload = json.loads(msg.data)
        stamp = float(payload['header']['stamp'])
        now = node.get_clock().now().nanoseconds / 1e9
        if math.isfinite(stamp) and 0 <= now - stamp <= max_age:
            return payload
    except (KeyError, TypeError, ValueError, AttributeError):
        pass
    return None


def parse_detection_message(msg):
    try:
        data = json.loads(msg.data)
    except (json.JSONDecodeError, AttributeError):
        return None, []
    if not isinstance(data, dict):
        return None, []
    header = data.get('header', {})
    detections = data.get('detections', [])
    if not isinstance(header, dict) or not isinstance(detections, list):
        return None, []
    return header.get('frame_id', ''), detections


def canonical_object_label(label):
    raw = str(label or '').strip()
    lowered = raw.lower()
    if lowered.startswith('person (') and lowered.endswith(')'):
        return 'person'
    return raw


def person_name_from_label(label):
    raw = str(label or '').strip()
    if raw.lower().startswith('person (') and raw.endswith(')'):
        return raw[len('person ('):-1].strip()
    return ''


def iter_object_detections(detections, min_score=0.0):
    threshold = float(min_score)
    for det in detections:
        if not isinstance(det, dict):
            continue
        raw_label = (det.get('label') or '').strip()
        label = canonical_object_label(raw_label)
        try:
            score = float(det.get('confidence', 0.0))
        except (TypeError, ValueError):
            continue
        if not label or label.isdigit():
            continue
        if label.lower().startswith('aruco'):
            continue
        if score < threshold:
            continue
        yield det, label, score


def deduplicate_objects(detections, distance_threshold, min_observations=1):
    if not detections:
        return []
    by_class = {}
    for det in detections:
        by_class.setdefault(det['class_id'], []).append(det)
    deduplicated = []
    obj_id = 0
    threshold = float(distance_threshold)
    min_count = int(min_observations)
    for cls, dets in by_class.items():
        clusters = []
        for det in dets:
            merged = False
            for cluster in clusters:
                cx = sum(d['x'] for d in cluster) / len(cluster)
                cy = sum(d['y'] for d in cluster) / len(cluster)
                cz = sum(d['z'] for d in cluster) / len(cluster)
                dist = math.sqrt((det['x'] - cx) ** 2 + (det['y'] - cy) ** 2 + (det['z'] - cz) ** 2)
                if dist < threshold:
                    cluster.append(det)
                    merged = True
                    break
            if not merged:
                clusters.append([det])
        for cluster in clusters:
            if len(cluster) < min_count:
                continue
            marker_ids = [d.get('marker_id') for d in cluster if d.get('marker_id') is not None]
            best_score = max(float(d['score']) for d in cluster)
            best_desc = max((d.get('description', '') or '' for d in cluster), key=len)
            face_names = [d.get('face_name', '') for d in cluster if d.get('face_name', '')]
            best_face_name = max(set(face_names), key=face_names.count) if face_names else ''
            wsum = sum(max(1e-6, float(d['score'])) for d in cluster)
            avg_x = sum(float(d['x']) * max(1e-6, float(d['score'])) for d in cluster) / wsum
            avg_y = sum(float(d['y']) * max(1e-6, float(d['score'])) for d in cluster) / wsum
            avg_z = sum(float(d['z']) * max(1e-6, float(d['score'])) for d in cluster) / wsum
            avg_dist = sum(float(d['distance']) * max(1e-6, float(d['score'])) for d in cluster) / wsum
            class_id = cls if marker_ids else f'{cls}_{obj_id}'
            deduplicated.append({
                'class_id': class_id,
                'label': cls,
                'x': avg_x,
                'y': avg_y,
                'z': avg_z,
                'distance': avg_dist,
                'score': best_score,
                'description': best_desc,
                'face_name': best_face_name,
                'observations': len(cluster), })
            if not marker_ids:
                obj_id += 1
    return deduplicated


def build_detection_record(position, label, score, description='', **extra):
    if not isinstance(position, dict):
        return None
    try:
        x = float(position['x'])
        y = float(position['y'])
        z = float(position['z'])
    except (KeyError, TypeError, ValueError):
        return None
    if not all(math.isfinite(value) for value in (x, y, z)):
        return None
    record = {
        'x': x,
        'y': y,
        'z': z,
        'distance': math.sqrt(x * x + y * y + z * z),
        'class_id': label,
        'score': float(score),
        'description': description or '', }
    record.update(extra)
    return record


def collect_transformed_detection_records(node, msg, target_frame: str, *, default_frame='base_link', timeout_s=0.3, min_score=0.0, now=None):
    frame_id, detections = parse_detection_message(msg)
    if frame_id is None:
        return []
    if not frame_id:
        frame_id = default_frame
    try:
        transform = node.tf_buffer.lookup_transform(target_frame, frame_id, rclpy.time.Time(), timeout=Duration(seconds=float(timeout_s)), )
    except Exception:
        return []
    records = []
    for det, label, score in iter_object_detections(detections, min_score=min_score):
        pos = det.get('position_3d', {})
        source_record = build_detection_record(pos, label, score)
        if source_record is None:
            continue
        pt = PointStamped()
        pt.header.frame_id = frame_id
        pt.point.x = float(pos.get('x', 0.0))
        pt.point.y = float(pos.get('y', 0.0))
        pt.point.z = float(pos.get('z', 0.0))
        try:
            pt_out = do_transform_point(pt, transform)
        except Exception:
            continue
        extra = {
            'description': det.get('description', ''),
            'face_name': det.get('face_name', '') or person_name_from_label(det.get('label', '')),
            'marker_id': det.get('marker_id'),
            'source': det.get('source', ''), }
        if now is not None:
            extra['t'] = now
        record = build_detection_record({
            'x': pt_out.point.x,
            'y': pt_out.point.y,
            'z': pt_out.point.z}, label, score, **extra, )
        if record is not None:
            record['distance'] = source_record['distance']
            records.append(record)
    return records


def build_items_result(detections):
    return json.dumps([{
        'item_name': det['class_id'],
        'x': round(det['x'], 2),
        'y': round(det['y'], 2),
        'z': round(det['z'], 2),
        'distance': round(det['distance'], 2),
        'description': det.get('description', ''),
        'face_name': det.get('face_name', ''),
        **({'track_id':det['track_id'], 'confidence':det['score']} if
            isinstance(det.get('track_id'),int) and not isinstance(det['track_id'],bool) and det['track_id'] >= 0 else {})
        } for det in detections])
