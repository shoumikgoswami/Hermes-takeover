"""Run every test file in its own process (each pins its own HERMES_HOME and module state).
    <hermes venv python> tests/run_all.py"""
import subprocess, sys
from pathlib import Path

here = Path(__file__).resolve().parent
bad = 0
for f in sorted(here.glob("test_*.py")):
    print(f"== {f.name}", flush=True)
    bad |= subprocess.call([sys.executable, "-W", "ignore", "-m", "unittest", "discover", "-s", str(here), "-p", f.name])
sys.exit(1 if bad else 0)
