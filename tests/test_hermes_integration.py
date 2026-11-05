"""Load Takeover through Hermes's OWN plugin machinery, the way the docs' compatibility contract tests plugins:
an isolated HERMES_HOME, `hermes plugins install file://<repo>`, then the real PluginManager discovery, `register(ctx)`,
hook dispatch (`pre_tool_call`, `transform_tool_result`) and system-prompt-section rendering. No mocks of Hermes.

Run with the Hermes venv python. Needs `hermes` on PATH and the repo committed (install clones git).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
AGENT = os.path.expanduser(os.environ.get("TAKEOVER_AGENT_DIR", "~/.hermes/hermes-agent"))
HOME = tempfile.mkdtemp(prefix="takeover-hermes-")
os.environ["HERMES_HOME"] = HOME
os.environ["HERMES_WEBUI_STATE_DIR"] = os.path.join(HOME, "no-webui")       # never touch a real hermes-webui
sys.path.insert(0, AGENT)


def setUpModule():
    p = subprocess.run(["hermes", "plugins", "install", f"file://{ROOT}", "--enable", "--yes-deps"],
                       capture_output=True, text=True, timeout=180, env=dict(os.environ))
    assert p.returncode == 0, p.stdout + p.stderr
    from hermes_cli import plugins as P
    P.discover_plugins()


class TestThroughHermes(unittest.TestCase):
    def test_doctor_and_validate_pass(self):
        for argv in (["hermes", "plugins", "doctor", str(ROOT), "--ci"], ["hermes", "plugins", "validate", str(ROOT)]):
            p = subprocess.run(argv, capture_output=True, text=True, timeout=180, env=dict(os.environ))
            self.assertEqual(p.returncode, 0, " ".join(argv[:3]) + "\n" + p.stdout[-3000:] + p.stderr[-1500:])
            self.assertNotIn("WARN: registration adds hook", p.stdout)

    def test_plugin_is_loaded_with_its_tool_hooks_and_prompt_section(self):
        from hermes_cli import plugins as P
        mgr = P.get_plugin_manager()
        loaded = {k: v for k, v in mgr._plugins.items() if "takeover" in k}
        self.assertTrue(loaded, list(mgr._plugins))
        rendered = P.render_system_prompt_sections({"session_id": "s", "model": "m", "provider": "p", "platform": "cli",
                                                     "profile_name": "default", "cwd": HOME})
        mine = [r for r in rendered if getattr(r, "id", "") == "takeover.hand-over-to-user"]
        self.assertEqual(len(mine), 1, rendered)
        self.assertIn("request_takeover", mine[0].content)

    def test_pre_tool_call_fence_through_hermes_dispatch(self):
        from hermes_cli.plugins import _get_pre_tool_call_directive_details as directive
        from tools.bot_desktop import lease as L
        L.release()
        self.assertIsNone(getattr(directive("browser_cdp", {}, task_id="t"), "action", None))
        L.acquire("viewer-x", reason="test")
        try:
            for name in ("browser_cdp", "browser_navigate", "computer_use"):
                self.assertEqual(getattr(directive(name, {}, task_id="t"), "action", None), "block", name)
            for name in ("terminal", "request_takeover"):
                self.assertIsNone(getattr(directive(name, {}, task_id="t"), "action", None), name)
        finally:
            L.release()

    def test_captcha_hint_through_hermes_hook_dispatch(self):
        from hermes_cli.plugins import invoke_hook
        page = json.dumps({"success": True, "snapshot": '- heading "Verify you are human" - iframe "reCAPTCHA" [ref=e4]'})
        out = [r for r in invoke_hook("transform_tool_result", tool_name="browser_snapshot", args={}, result=page,
                                      task_id="t", session_id="hermes-it") if isinstance(r, str)]
        self.assertEqual(len(out), 1, out)
        self.assertIn("request_takeover", json.loads(out[0])["takeover_hint"])
        plain = [r for r in invoke_hook("transform_tool_result", tool_name="browser_snapshot", args={},
                                        result='{"snapshot": "r/AskReddit"}', task_id="t", session_id="hermes-it2") if r is not None]
        self.assertEqual(plain, [])

    def test_tool_is_registered_in_hermes_registry(self):
        from tools.registry import registry
        names = set(getattr(registry, "_tools", {}) or {}) or {getattr(t, "name", "") for t in registry.get_all_tools()} \
            if hasattr(registry, "get_all_tools") else set(getattr(registry, "_tools", {}))
        self.assertIn("request_takeover", names)


if __name__ == "__main__":
    unittest.main()
