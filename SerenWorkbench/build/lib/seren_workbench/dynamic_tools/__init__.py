"""
seren_workbench.dynamic_tools — YAML-defined plug-and-play tool dispatchers.

Dynamic tools are loaded from YAML manifests in a tools/ directory and
dispatched via web_dispatcher (HTTP calls) or process_dispatcher (subprocess
calls). This module mirrors the original dynamic_tools/ package at the
project root, now moved inside the seren_workbench package for family
alignment.
"""
from __future__ import annotations
