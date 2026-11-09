#!/bin/bash
# Fully fresh, isolated Hermes DASHBOARD end-to-end run (also what Hermes Desktop's backend mounts: the same plugin_api).
# Scratch Hermes home, scratch `hermes dashboard` on 127.0.0.1:9199. Touches nothing real.
#   tests/e2e/fresh_dashboard_e2e.sh            (commit first: plugins install clones git)
set -u
unset DISPLAY XAUTHORITY DBUS_SESSION_BUS_ADDRESS
REPO="$(cd "$(dirname "$0")/../.." && pwd)"
V="${E2E_PYTHON:-$(head -1 "$(command -v hermes)" | sed 's/^#!//')}"
BASE="${TMPDIR:-/tmp}/takeover-e2e-dash"
export S="$BASE/home"
own() { tr '\0' '\n' < "/proc/$1/environ" 2>/dev/null | grep -q "$2"; }
killmine() {
  for p in $(pgrep -f "hermes.*dashboard|web_server"); do own "$p" "HERMES_HOME=$S" && kill "$p"; done
  for p in $(pgrep -f Xtigervnc); do own "$p" "HERMES_HOME=$S" && kill "$p"; done
}
killmine; sleep 2; rm -rf "$BASE"; mkdir -p "$S"
cd "$REPO"
TAKEOVER_NO_BOOTSTRAP=1 HERMES_HOME="$S" hermes plugins install "file://$REPO" --enable --yes-deps 2>&1 | grep -E "Block|rror"
(HERMES_HOME="$S" HERMES_DASHBOARD_SESSION_TOKEN=e2e-session-token TAKEOVER_NO_BOOTSTRAP=1 nohup hermes dashboard --port 9199 --no-open --skip-build > "$BASE/dashboard.log" 2>&1 &)
for i in $(seq 1 40); do curl -s -m 2 -o /dev/null http://127.0.0.1:9199/ && break; sleep 1; done
cd tests/e2e
E2E_HOME="$S" E2E_SHOTS="$BASE/shots" timeout 420 "$V" -W ignore run_e2e.py > "$BASE/run.log" 2>&1
rc=$?
grep -E "^FAIL" "$BASE/run.log"
tail -1 "$BASE/run.log"
killmine
exit $rc
