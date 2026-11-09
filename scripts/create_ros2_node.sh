#!/usr/bin/env bash

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"

echo "This script will create a new ROS2 Python node package in this workspace."

if ! read -r -p "Target workspace (robot/workstation) [robot]: " target; then
  target=""
fi
if [[ -z "$target" ]]; then
  target="robot"
fi

target_lower=$(echo "$target" | tr '[:upper:]' '[:lower:]')
if [[ "$target_lower" != "robot" && "$target_lower" != "workstation" ]]; then
  echo "Invalid target: $target. Choose 'robot' or 'workstation'."
  exit 1
fi

if ! read -r -p "New package (node) name (snake_case, e.g. my_node): " pkgname; then
  pkgname=""
fi
if [[ -z "$pkgname" ]]; then
  echo "Package name cannot be empty"
  exit 1
fi

# sanitize pkgname: keep lowercase letters, digits and underscores
sanitized=$(echo "$pkgname" | tr '[:upper:]' '[:lower:]' | sed -E 's/[^a-z0-9_]/_/g' | sed -E 's/^([^a-z0-9]+)//')
if [[ -z "$sanitized" ]]; then
  echo "Package name became empty after sanitization. Choose a different name."
  exit 1
fi
pkgname="$sanitized"

if [[ "$target_lower" == "robot" ]]; then
  dst_root="$ROOT_DIR/robot/ros_ws/src"
else
  dst_root="$ROOT_DIR/workstation/ament_ws/src"
fi

pkg_dir="$dst_root/$pkgname"
if [[ -e "$pkg_dir" ]]; then
  echo "Package directory already exists: $pkg_dir"
  if ! read -r -p "Overwrite existing directory? (y/N): " overwrite; then
    overwrite=""
  fi
  if [[ "$overwrite" != "y" && "$overwrite" != "Y" ]]; then
    echo "Aborting."
    exit 1
  fi
  rm -rf "$pkg_dir"
fi

mkdir -p "$pkg_dir"
mkdir -p "$pkg_dir/$pkgname"
mkdir -p "$pkg_dir/resource"

version="0.0.0"
maintainer_name="pg-llm-service-robot"
maintainer_email="pg-llm-service-robot@uni-oldenburg.de"
license_id="AGPL-3.0-only"

if ! read -r -p "Short description for the package [A ROS2 node package named $pkgname]: " desc; then
  desc=""
fi
if [[ -z "$desc" ]]; then
  desc="A ROS2 Python node package named $pkgname"
fi

cat > "$pkg_dir/package.xml" <<EOF
<?xml version="1.0"?>
<?xml-model href="http://download.ros.org/schema/package_format3.xsd" schematypens="http://www.w3.org/2001/XMLSchema"?>
<package format="3">
    <name>$pkgname</name>
    <version>$version</version>
    <description>$desc</description>
    <maintainer email="$maintainer_email">$maintainer_name</maintainer>
    <license>$license_id</license>

    <exec_depend>rclpy</exec_depend>

    <export>
        <build_type>ament_python</build_type>
    </export>
</package>
EOF

cat > "$pkg_dir/setup.py" <<'PYSETUP'
from setuptools import setup

package_name = "__PKG__"

setup(
    name=package_name,
    version="__VERSION__",
    packages=[package_name],
    data_files=[
        ("share/ament_index/resource_index/packages", ["resource/" + package_name]),
        ("share/" + package_name, ["package.xml"]),
    ],
    install_requires=["setuptools"],
    zip_safe=True,
    maintainer="__MAINT__",
    maintainer_email="__MAINT_EMAIL__",
    description="__DESC__",
    license="__LICENSE__",
    entry_points={
        "console_scripts": [
            "__PKG__ = __PKG__.node:main",
        ],
    },
)
PYSETUP

sed -i "s|__PKG__|$pkgname|g" "$pkg_dir/setup.py"
sed -i "s|__VERSION__|$version|g" "$pkg_dir/setup.py"
sed -i "s|__MAINT__|$maintainer_name|g" "$pkg_dir/setup.py"
sed -i "s|__MAINT_EMAIL__|$maintainer_email|g" "$pkg_dir/setup.py"
sed -i "s|__DESC__|$desc|g" "$pkg_dir/setup.py"
sed -i "s|__LICENSE__|$license_id|g" "$pkg_dir/setup.py"

cat > "$pkg_dir/setup.cfg" <<EOF
[metadata]
name = $pkgname
version = $version

[options]
zip_safe = True

[options.entry_points]
console_scripts =
    $pkgname = $pkgname.node:main
EOF

touch "$pkg_dir/resource/$pkgname"

cat > "$pkg_dir/$pkgname/__init__.py" <<EOF
"""
$pkgname package
"""

__all__ = ["node"]
EOF

cat > "$pkg_dir/$pkgname/node.py" <<'PYNODE'
#!/usr/bin/env python3
"""Simple ROS2 node template."""
import rclpy
from rclpy.node import Node


def main(args=None):
    rclpy.init(args=args)
    node = Node("__NODE_NAME__")

    node.get_logger().info("Hello from __NODE_NAME__")

    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        rclpy.shutdown()


if __name__ == "__main__":
    main()
PYNODE

sed -i "s|__NODE_NAME__|$pkgname|g" "$pkg_dir/$pkgname/node.py"
chmod +x "$pkg_dir/$pkgname/node.py"

echo "Created package at: $pkg_dir"
echo "Finished."

exit 0

