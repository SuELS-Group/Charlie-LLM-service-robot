#!/usr/bin/env python3
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Dict, List, Set, Tuple

SCRIPT_DIR = Path(__file__).resolve().parent
JSON_PATH = SCRIPT_DIR / "graph.json"
DOT_PATH = SCRIPT_DIR / "graph_pretty.dot"
PDF_PATH = SCRIPT_DIR / "graph.pdf"
NODE_COLOR_PALETTE: List[Tuple[str, str, str]] = [("#DCEEFF", "#4A90E2", "#18324A"),
                                                  ("#EAF7E6", "#57A05A", "#234425"),
                                                  ("#FFE8E8", "#C65A5A", "#5A1F1F"),
                                                  ("#F3E8FF", "#8A5AC2", "#3F2461"),
                                                  ("#FFF2CC", "#C9A227", "#5F4B00"),
                                                  ("#FFEFD8", "#D98E04", "#6E4700"),
                                                  ("#E6F7F5", "#1FA39A", "#114B47"),
                                                  ("#FDECEF", "#D14D72", "#5B1E2E"),
                                                  ("#ECECFF", "#6C7AE0", "#28306B"),
                                                  ("#F2F7D9", "#8AAA2A", "#44520F"),
                                                  ("#E9F5FF", "#2E86C1", "#16384E"),
                                                  ("#FBE9E7", "#D35400", "#5A2500"), ]


def sanitize_id(prefix: str, value: str) -> str:
    safe = []
    for ch in value:
        if ch.isalnum() or ch == "_":
            safe.append(ch)
        else:
            safe.append("_")
    return f"{prefix}_{''.join(safe)}"


def shorten_type_name(type_name: str) -> str:
    parts = type_name.split("/")
    if len(parts) >= 3:
        return f"{parts[-2]}/{parts[-1]}"
    return type_name


def prettify_topic_color(topic_name: str) -> Tuple[str, str, str]:
    low = topic_name.lower()
    if topic_name in ("/tf", "/tf_static"):
        return "#FFF2CC", "#C9A227", "#5F4B00"
    if "image" in low or "camera" in low:
        return "#F3E8FF", "#8A5AC2", "#3F2461"
    if "scan" in low or "laser" in low or "lidar" in low:
        return "#FFE8E8", "#C65A5A", "#5A1F1F"
    if "cmd_vel" in low or "twist" in low or "drive" in low:
        return "#E8F1FF", "#4A90E2", "#18324A"
    if "odom" in low or "pose" in low or "imu" in low:
        return "#EAF7E6", "#57A05A", "#234425"
    return "#EEF3F7", "#8AA1B1", "#25313B"


def get_node_color_map(nodes: List[dict]) -> Dict[str, Tuple[str, str, str]]:
    color_map: Dict[str, Tuple[str, str, str]] = {}
    sorted_node_names = sorted(node["name"] for node in nodes)
    for index, node_name in enumerate(sorted_node_names):
        color_map[node_name] = NODE_COLOR_PALETTE[index % len(NODE_COLOR_PALETTE)]
    return color_map


def load_graph_json(path: Path) -> dict:
    if not path.exists():
        raise FileNotFoundError(f"Missing JSON input: {path}")
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def run_exporter() -> None:
    # ROS is needed only when generating a graph, not to start the web server.
    from . import ros_node_topic_finder as ros_exporter

    result = ros_exporter.main()
    if result != 0:
        raise RuntimeError(f"export_ros2_graph.main() failed with exit code {result}")


def collect_used_topics(data: dict) -> Dict[str, Set[str]]:
    topic_types: Dict[str, Set[str]] = {}
    for topic in data.get("topics", []):
        topic_name = topic["name"]
        topic_types.setdefault(topic_name, set()).update(topic.get("types", []))
    for edge in data.get("edges", {}).get("publishers", []):
        topic_types.setdefault(edge["topic"], set()).add(edge["type"])
    for edge in data.get("edges", {}).get("subscribers", []):
        topic_types.setdefault(edge["topic"], set()).add(edge["type"])
    return topic_types


def group_nodes_by_namespace(nodes: List[dict]) -> Dict[str, List[dict]]:
    groups: Dict[str, List[dict]] = {}
    for node in nodes:
        namespace = node.get("namespace", "/") or "/"
        groups.setdefault(namespace, []).append(node)
    return dict(sorted(groups.items(), key=lambda item: item[0]))


def build_dot(data: dict) -> str:
    nodes = data.get("nodes", [])
    edges = data.get("edges", {})
    publisher_edges = edges.get("publishers", [])
    subscriber_edges = edges.get("subscribers", [])
    topic_types = collect_used_topics(data)
    namespace_groups = group_nodes_by_namespace(nodes)
    node_color_map = get_node_color_map(nodes)
    lines: List[str] = []
    lines.append("digraph ROS2Graph {")
    lines.append('  graph [')
    lines.append('    rankdir="LR",')
    lines.append('    bgcolor="white",')
    lines.append('    splines="spline",')
    lines.append('    overlap="false",')
    lines.append('    concentrate="true",')
    lines.append('    outputorder="edgesfirst",')
    lines.append('    pad="0.4",')
    lines.append('    nodesep="0.4",')
    lines.append('    ranksep="0.9",')
    lines.append('    fontname="Helvetica",')
    lines.append('    labelloc="t",')
    lines.append('    label="ROS 2 Network Graph"')
    lines.append('  ];')
    lines.append('  node [fontname="Helvetica", fontsize="11"];')
    lines.append('  edge [fontname="Helvetica", fontsize="9", color="#8A8A8A", arrowsize="0.7"];')
    lines.append("")
    cluster_index = 0
    for namespace, group_nodes in namespace_groups.items():
        cluster_name = f"cluster_{cluster_index}"
        cluster_index += 1
        lines.append(f'  subgraph "{cluster_name}" {{')
        lines.append('    style="rounded,filled";')
        lines.append('    color="#D7E3EC";')
        lines.append('    fillcolor="#F7FAFC";')
        lines.append(f'    label="{namespace}";')
        lines.append('    fontsize="12";')
        lines.append('    fontname="Helvetica-Bold";')
        for node in sorted(group_nodes, key=lambda n: n["name"]):
            node_name = node["name"]
            node_id = sanitize_id("node", node_name)
            short_name = node.get("short_name", node_name)
            subscriber_count = node.get("subscriber_count", 0)
            fill_color, border_color, font_color = node_color_map[node_name]
            label = (f"{short_name}"
                     f"\\n{subscriber_count} subs")
            lines.append(f'    "{node_id}" [label="{label}", shape="box", '
                         f'style="rounded,filled", fillcolor="{fill_color}", color="{border_color}", '
                         f'fontcolor="{font_color}", penwidth="1.5", margin="0.12,0.08"];')
        lines.append("  }")
        lines.append("")
    for topic_name in sorted(topic_types.keys()):
        topic_id = sanitize_id("topic", topic_name)
        types_sorted = sorted(t for t in topic_types[topic_name] if t)
        pretty_types = ", ".join(shorten_type_name(t) for t in types_sorted[:2])
        if len(types_sorted) > 2:
            pretty_types += ", ..."
        fill, border, font = prettify_topic_color(topic_name)
        label = topic_name
        if pretty_types:
            label += f"\\n[{pretty_types}]"
        lines.append(f'  "{topic_id}" [label="{label}", shape="ellipse", style="filled", '
                     f'fillcolor="{fill}", color="{border}", fontcolor="{font}", penwidth="1.3"];')
    lines.append("")
    for edge in sorted(publisher_edges, key=lambda e: (e["node"], e["topic"], e["type"])):
        node_name = edge["node"]
        node_id = sanitize_id("node", node_name)
        topic_id = sanitize_id("topic", edge["topic"])
        msg_type = shorten_type_name(edge["type"])
        _fill_color, border_color, _font_color = node_color_map.get(node_name, ("#DCEEFF", "#4A90E2", "#18324A"))
        lines.append(f'  "{node_id}" -> "{topic_id}" [label="{msg_type}", color="{border_color}", fontcolor="{border_color}", penwidth="1.2"];')
    for edge in sorted(subscriber_edges, key=lambda e: (e["topic"], e["node"], e["type"])):
        node_name = edge["node"]
        topic_id = sanitize_id("topic", edge["topic"])
        node_id = sanitize_id("node", node_name)
        msg_type = shorten_type_name(edge["type"])
        _fill_color, border_color, _font_color = node_color_map.get(node_name, ("#DCEEFF", "#4A90E2", "#18324A"))
        lines.append(f'  "{topic_id}" -> "{node_id}" [label="{msg_type}", color="{border_color}", fontcolor="{border_color}", penwidth="1.4"];')
    lines.append("}")
    return "\n".join(lines)


def write_dot(dot_text: str, path: Path) -> None:
    with path.open("w", encoding="utf-8") as f:
        f.write(dot_text)


def render_pdf(dot_path: Path, pdf_path: Path) -> None:
    try:
        subprocess.run(["dot", "-Tpdf", str(dot_path), "-o", str(pdf_path)], check=True, )
    except FileNotFoundError as exc:
        raise RuntimeError("Graphviz 'dot' was not found. Install it with: sudo apt install graphviz") from exc
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(f"Graphviz rendering failed with exit code {exc.returncode}") from exc


def execute() -> int:
    try:
        run_exporter()
        data = load_graph_json(JSON_PATH)
        dot_text = build_dot(data)
        write_dot(dot_text, DOT_PATH)
        render_pdf(DOT_PATH, PDF_PATH)
        print(f"[OK] wrote {JSON_PATH}")
        print(f"[OK] wrote {DOT_PATH}")
        print(f"[OK] wrote {PDF_PATH}")
        return 0
    except Exception as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 1
