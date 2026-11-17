#!/usr/bin/env python3
from __future__ import annotations

import json
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Set, Tuple

import rclpy
from rclpy.node import Node

DISCOVERY_WAIT_SECONDS = 2.0
INCLUDE_SERVICES = False
HIDE_PATTERNS: List[str] = [r"^/rosout$", r"^/parameter_events$", ]
EXCLUDED_TOPIC_PREFIXES: List[str] = ["/navigate_through_poses",
                                      "/follow_path",
                                      "/odom",
                                      "/speed_limit",
                                      "/goal_pose",
                                      "/tf",
                                      "/tf_static",
                                      "/stretch_controller",
                                      "/vim",
                                      "/joy",
                                      "/pose",
                                      "/scan_filtered",
                                      "/laser_filter",
                                      "/map_metadata",
                                      "/slam_toolbox",
                                      "/initialpose",
                                      "/clicked_point"
                                      "/assisted_teleop",
                                      "/backup",
                                      "/behavior_server",
                                      "/cmd_vel",
                                      "/drive_on_heading",
                                      "/spin",
                                      "/local_costmap",
                                      "/wait",
                                      "/bt_navigator",
                                      "/map_saver",
                                      "/bond",
                                      "/diagnostics",
                                      "/global_costmap",
                                      "/transformed_global_plan",
                                      "/compute_path_to_pose",
                                      "/local_plan",
                                      "/stretch_gamepad_state",
                                      "/imu_mobile_base",
                                      "/imu_wrist",
                                      "/is_gamepad_dongle"]
MIN_SUBSCRIBERS = 1


@dataclass
class TopicInfo:
    name: str
    types: Set[str] = field(default_factory=set)


@dataclass
class ServiceInfo:
    name: str
    types: Set[str] = field(default_factory=set)


def full_node_name(name: str, namespace: str) -> str:
    if not namespace or namespace == "/":
        return f"/{name}" if not name.startswith("/") else name
    if namespace.endswith("/"):
        return f"{namespace}{name}"
    return f"{namespace}/{name}"


def is_excluded_topic(topic_name: str, excluded_prefixes: List[str]) -> bool:
    return any(topic_name.startswith(prefix) for prefix in excluded_prefixes)


class GraphExporter(Node):

    def __init__(self) -> None:
        super().__init__("ros2_graph_exporter_temp_node")

    def collect_graph(self, include_services: bool = False, hidden_name_patterns: List[
                                                                                      str] | None = None, excluded_topic_prefixes:
    List[str] | None = None, ) -> Tuple[
        Dict[str, TopicInfo], Dict[str, ServiceInfo], Set[Tuple[str, str, str]], Set[Tuple[str, str, str]], Set[
            Tuple[str, str, str]],]:
        hidden_name_patterns = hidden_name_patterns or []
        excluded_topic_prefixes = excluded_topic_prefixes or []
        hidden_res = [re.compile(pattern) for pattern in hidden_name_patterns]

        def is_hidden(name: str) -> bool:
            return any(rx.search(name) for rx in hidden_res)

        topics: Dict[str, TopicInfo] = {}
        services: Dict[str, ServiceInfo] = {}
        pub_edges: Set[Tuple[str, str, str]] = set()
        sub_edges: Set[Tuple[str, str, str]] = set()
        srv_edges: Set[Tuple[str, str, str]] = set()
        nodes = self.get_node_names_and_namespaces()
        for node_name, node_ns in nodes:
            node_full = full_node_name(node_name, node_ns)
            try:
                pubs = self.get_publisher_names_and_types_by_node(node_name, node_ns)
            except Exception:
                pubs = []
            for topic_name, type_list in pubs:
                if is_hidden(topic_name):
                    continue
                if is_excluded_topic(topic_name, excluded_topic_prefixes):
                    continue
                if topic_name not in topics:
                    topics[topic_name] = TopicInfo(name=topic_name)
                for msg_type in type_list:
                    topics[topic_name].types.add(msg_type)
                    pub_edges.add((node_full, topic_name, msg_type))
            try:
                subs = self.get_subscriber_names_and_types_by_node(node_name, node_ns)
            except Exception:
                subs = []
            for topic_name, type_list in subs:
                if is_hidden(topic_name):
                    continue
                if is_excluded_topic(topic_name, excluded_topic_prefixes):
                    continue
                if topic_name not in topics:
                    topics[topic_name] = TopicInfo(name=topic_name)
                for msg_type in type_list:
                    topics[topic_name].types.add(msg_type)
                    sub_edges.add((topic_name, node_full, msg_type))
            if include_services:
                try:
                    srvs = self.get_service_names_and_types_by_node(node_name, node_ns)
                except Exception:
                    srvs = []
                for service_name, type_list in srvs:
                    if is_hidden(service_name):
                        continue
                    if service_name not in services:
                        services[service_name] = ServiceInfo(name=service_name)
                    for srv_type in type_list:
                        services[service_name].types.add(srv_type)
                        srv_edges.add((node_full, service_name, srv_type))
        return topics, services, pub_edges, sub_edges, srv_edges


def filter_by_min_subscribers(node_pairs: List[Tuple[str, str]], pub_edges: Set[Tuple[str, str, str]], sub_edges: Set[
    Tuple[str, str, str]], srv_edges: Set[Tuple[str, str, str]], min_subscribers: int, ) -> Tuple[
    List[Tuple[str, str]], Set[Tuple[str, str, str]], Set[Tuple[str, str, str]], Set[Tuple[str, str, str]],]:
    if min_subscribers <= 0:
        return node_pairs, pub_edges, sub_edges, srv_edges
    node_to_subscribed_topics: Dict[str, Set[str]] = {}
    for topic_name, node_full, _msg_type in sub_edges:
        node_to_subscribed_topics.setdefault(node_full, set()).add(topic_name)
    kept_nodes: Set[str] = set()
    filtered_node_pairs: List[Tuple[str, str]] = []
    for node_name, node_ns in node_pairs:
        node_full = full_node_name(node_name, node_ns)
        sub_count = len(node_to_subscribed_topics.get(node_full, set()))
        if sub_count >= min_subscribers:
            kept_nodes.add(node_full)
            filtered_node_pairs.append((node_name, node_ns))
    filtered_pub_edges = {(node_full, topic_name, msg_type) for node_full, topic_name, msg_type in pub_edges if
                          node_full in kept_nodes}
    filtered_sub_edges = {(topic_name, node_full, msg_type) for topic_name, node_full, msg_type in sub_edges if
                          node_full in kept_nodes}
    filtered_srv_edges = {(node_full, service_name, srv_type) for node_full, service_name, srv_type in srv_edges if
                          node_full in kept_nodes}
    return filtered_node_pairs, filtered_pub_edges, filtered_sub_edges, filtered_srv_edges


def filter_unused_entities(topics: Dict[str, TopicInfo], services: Dict[str, ServiceInfo], pub_edges: Set[
    Tuple[str, str, str]], sub_edges: Set[Tuple[str, str, str]], srv_edges: Set[Tuple[str, str, str]], ) -> Tuple[
    Dict[str, TopicInfo], Dict[str, ServiceInfo]]:
    used_topics = {topic_name for _, topic_name, _ in pub_edges}
    used_topics.update(topic_name for topic_name, _, _ in sub_edges)
    used_services = {service_name for _, service_name, _ in srv_edges}
    filtered_topics = {name: info for name, info in topics.items() if name in used_topics}
    filtered_services = {name: info for name, info in services.items() if name in used_services}
    return filtered_topics, filtered_services


def build_json(node_pairs: List[Tuple[str, str]], topics: Dict[str, TopicInfo], services: Dict[
    str, ServiceInfo], pub_edges: Set[Tuple[str, str, str]], sub_edges: Set[Tuple[str, str, str]], srv_edges: Set[
    Tuple[str, str, str]], ) -> dict:
    node_map: Dict[str, dict] = {}
    for node_name, node_ns in sorted(node_pairs):
        node_full = full_node_name(node_name, node_ns)
        node_map[node_full] = {
            "name": node_full,
            "short_name": node_name,
            "namespace": node_ns,
            "publishers": [],
            "subscribers": [],
            "services": [],
            "subscriber_count": 0, }
    for node_full, topic_name, topic_type in sorted(pub_edges):
        if node_full in node_map:
            node_map[node_full]["publishers"].append({"topic": topic_name, "type": topic_type, })
    for topic_name, node_full, topic_type in sorted(sub_edges):
        if node_full in node_map:
            node_map[node_full]["subscribers"].append({"topic": topic_name, "type": topic_type, })
    for node_full, service_name, service_type in sorted(srv_edges):
        if node_full in node_map:
            node_map[node_full]["services"].append({"service": service_name, "type": service_type, })
    for node_data in node_map.values():
        unique_topics = {entry["topic"] for entry in node_data["subscribers"]}
        node_data["subscriber_count"] = len(unique_topics)
    data = {
        "nodes": list(node_map.values()),
        "topics": [{"name": topic.name, "types": sorted(topic.types), } for topic in
                   sorted(topics.values(), key=lambda t: t.name)],
        "services": [{"name": service.name, "types": sorted(service.types), } for service in
                     sorted(services.values(), key=lambda s: s.name)],
        "edges": {
            "publishers": [{"node": node, "topic": topic, "type": msg_type} for node, topic, msg_type in
                           sorted(pub_edges)],
            "subscribers": [{"topic": topic, "node": node, "type": msg_type} for topic, node, msg_type in
                            sorted(sub_edges)],
            "services": [{"node": node, "service": service, "type": srv_type} for node, service, srv_type in
                         sorted(srv_edges)], },
        "config": {
            "discovery_wait_seconds": DISCOVERY_WAIT_SECONDS,
            "include_services": INCLUDE_SERVICES,
            "hide_patterns": HIDE_PATTERNS,
            "excluded_topic_prefixes": EXCLUDED_TOPIC_PREFIXES,
            "min_subscribers": MIN_SUBSCRIBERS, }, }
    return data


def main() -> int:
    if MIN_SUBSCRIBERS < 0:
        print("[ERROR] MIN_SUBSCRIBERS must be >= 0", file=sys.stderr)
        return 1
    output_path = Path(__file__).resolve().parent / "graph.json"
    rclpy.init(args=None)
    exporter = GraphExporter()
    try:
        end_time = time.time() + max(DISCOVERY_WAIT_SECONDS, 0.0)
        while time.time() < end_time:
            rclpy.spin_once(exporter, timeout_sec=0.1)
        node_pairs = exporter.get_node_names_and_namespaces()
        topics, services, pub_edges, sub_edges, srv_edges = exporter.collect_graph(include_services=INCLUDE_SERVICES, hidden_name_patterns=HIDE_PATTERNS, excluded_topic_prefixes=EXCLUDED_TOPIC_PREFIXES, )
        node_pairs, pub_edges, sub_edges, srv_edges = filter_by_min_subscribers(node_pairs=node_pairs, pub_edges=pub_edges, sub_edges=sub_edges, srv_edges=srv_edges, min_subscribers=MIN_SUBSCRIBERS, )
        topics, services = filter_unused_entities(topics=topics, services=services, pub_edges=pub_edges, sub_edges=sub_edges, srv_edges=srv_edges, )
        data = build_json(node_pairs=node_pairs, topics=topics, services=services, pub_edges=pub_edges, sub_edges=sub_edges, srv_edges=srv_edges, )
        with output_path.open("w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        print(f"[OK] wrote {output_path}")
        print(f"[INFO] nodes: {len(data['nodes'])}")
        print(f"[INFO] topics: {len(data['topics'])}")
        if INCLUDE_SERVICES:
            print(f"[INFO] services: {len(data['services'])}")
        print(f"[INFO] publisher edges: {len(data['edges']['publishers'])}")
        print(f"[INFO] subscriber edges: {len(data['edges']['subscribers'])}")
        if INCLUDE_SERVICES:
            print(f"[INFO] service edges: {len(data['edges']['services'])}")
    finally:
        exporter.destroy_node()
        rclpy.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
