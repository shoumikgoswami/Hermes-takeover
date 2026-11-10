"""Detached helper (see takeover_core/bootstrap.py). Arg: ``webui`` (default; started when the plugin loads: Screen page +
service only) or ``setup`` (first-run screen setup, started only by a deliberate use: Screen page, request_takeover)."""
import importlib.util
import sys
from pathlib import Path

root = Path(__file__).resolve().parent / "takeover_core"
spec = importlib.util.spec_from_file_location("hermes_takeover_core", root / "__init__.py", submodule_search_locations=[str(root)])
mod = importlib.util.module_from_spec(spec)
sys.modules["hermes_takeover_core"] = mod
spec.loader.exec_module(mod)
mod.bootstrap.run(mod.compat.current_home(), sys.argv[1] if len(sys.argv) > 1 else "webui")
