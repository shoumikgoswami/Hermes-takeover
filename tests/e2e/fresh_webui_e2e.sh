#!/bin/bash
# Fully fresh, isolated hermes-webui end-to-end run. Touches nothing real: scratch Hermes home (+ a second profile "p2"),
# scratch WebUI on 127.0.0.1:8798 (password e2e-pass-123), Takeover service on 127.0.0.1:17951.
#   tests/e2e/fresh_webui_e2e.sh            (commit first: plugins install clones git)
# Needs: hermes on PATH, ~/hermes-webui, a headless-capable Chromium (tests/e2e/cdp.py), TigerVNC + Xfce.
set -u
unset DISPLAY XAUTHORITY DBUS_SESSION_BUS_ADDRESS
REPO="$(cd "$(dirname "$0")/../.." && pwd)"
V="${E2E_PYTHON:-$(head -1 "$(command -v hermes)" | sed 's/^#!//')}"
WEBUI="${E2E_WEBUI_DIR:-$HOME/hermes-webui}"
BASE="${TMPDIR:-/tmp}/takeover-e2e"
export S="$BASE/home" W="$BASE/webui-state" TAKEOVER_PORT=17951
own() { tr '\0' '\n' < "/proc/$1/environ" 2>/dev/null | grep -q "$2"; }
for p in $(pgrep -f "hermes-webui/server.py"); do own "$p" "HERMES_WEBUI_PORT=8798" && kill "$p"; done
for p in $(pgrep -f sidecar_run.py); do own "$p" "TAKEOVER_PORT=17951" && kill "$p"; done
for p in $(pgrep -f Xtigervnc); do own "$p" "HERMES_HOME=$S" && kill "$p"; done
sleep 2; rm -rf "$BASE"; mkdir -p "$S/profiles/p2" "$W"
cd "$REPO"
TAKEOVER_NO_BOOTSTRAP=1 HERMES_HOME="$S" hermes plugins install "file://$REPO" --enable --yes-deps 2>&1 | grep -E "Block|rror"
TAKEOVER_NO_BOOTSTRAP=1 HERMES_HOME="$S/profiles/p2" hermes plugins install "file://$REPO" --enable --yes-deps 2>&1 | grep -E "Block|rror"
(cd "$WEBUI" && HERMES_HOME="$S" HERMES_WEBUI_STATE_DIR="$W" HERMES_WEBUI_PLUGINS_DIR="$S/plugins" HERMES_WEBUI_PASSWORD=e2e-pass-123 \
  HERMES_WEBUI_HOST=127.0.0.1 HERMES_WEBUI_PORT=8798 nohup "$V" server.py > "$BASE/webui.log" 2>&1 &)
for i in $(seq 1 60); do curl -s -m 2 -o /dev/null http://127.0.0.1:8798/ && break; sleep 1; done   # wait until WebUI answers
TAKEOVER_NO_BOOTSTRAP=1 HERMES_HOME="$S" HERMES_WEBUI_STATE_DIR="$W" hermes takeover webui > /dev/null 2>&1
if [ -n "${E2E_SETUP_ONLY:-}" ]; then echo "scratch WebUI on http://127.0.0.1:8798 (left running)"; exit 0; fi
cd tests/e2e
E2E_STATE="$W" E2E_HOME="$S" E2E_SHOTS="$BASE/shots" timeout 360 "$V" -W ignore run_webui_e2e.py > "$BASE/run.log" 2>&1
rc=$?
grep -E "^FAIL" "$BASE/run.log"
grep -nE "run_webui_e2e.py\", line|^[A-Za-z]*Error: timed out waiting" "$BASE/run.log" | tail -3
tail -1 "$BASE/run.log"
# leave nothing running
for p in $(pgrep -f "hermes-webui/server.py"); do own "$p" "HERMES_WEBUI_PORT=8798" && kill "$p"; done
for p in $(pgrep -f sidecar_run.py); do own "$p" "TAKEOVER_PORT=17951" && kill "$p"; done
for p in $(pgrep -f Xtigervnc); do own "$p" "HERMES_HOME=$S" && kill "$p"; done
exit $rc
