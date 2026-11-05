"""Entry point the plugin uses to start its WebUI sidecar as a detached process (``python sidecar_run.py``)."""
import importlib.util
import sys
from pathlib import Path

root = Path(__file__).resolve().parent / "handoff_core"
spec = importlib.util.spec_from_file_location("hermes_handoff_core", root / "__init__.py", submodule_search_locations=[str(root)])
mod = importlib.util.module_from_spec(spec)
sys.modules["hermes_handoff_core"] = mod
spec.loader.exec_module(mod)
sys.exit(mod.sidecar.main())
