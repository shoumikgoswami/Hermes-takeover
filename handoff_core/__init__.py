"""Handoff core: everything the tool, the CLI and the dashboard routes share.

Loaded by path under the module name ``hermes_handoff_core`` (see the ``_load_core`` helper in
``__init__.py`` and ``dashboard/plugin_api.py``), so all entry points in one process share one copy
of the in-memory state (setup progress, stream tickets).
"""
from __future__ import annotations

import importlib

__version__ = "0.1.0"
PLUGIN_ID = "handoff"

_SUBMODULES = ("compat", "state", "setup", "tickets", "control", "bridge", "tool", "cli")


def __getattr__(name):          # lazy: the CLI never needs the bridge, the bridge never needs argparse
    if name in _SUBMODULES:
        return importlib.import_module(f"{__name__}.{name}")
    raise AttributeError(name)
