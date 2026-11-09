"""Detached first-run helper started by the plugin right after install (see takeover_core/bootstrap.py)."""
import importlib.util
import sys
from pathlib import Path

root = Path(__file__).resolve().parent / "takeover_core"
spec = importlib.util.spec_from_file_location("hermes_takeover_core", root / "__init__.py", submodule_search_locations=[str(root)])
mod = importlib.util.module_from_spec(spec)
sys.modules["hermes_takeover_core"] = mod
spec.loader.exec_module(mod)
mod.bootstrap.run(mod.compat.current_home())
