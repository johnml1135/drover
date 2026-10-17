#!/usr/bin/env bash
# Claude Code statusline wrapper: tee rate_limits to drover's allowance dir, then
# pass the original JSON to an optional wrapped command ($1) or print a short line.
# Install later as the statusLine command; it never fails the statusline.
input=$(cat)
# Without a working python3 (missing, or the Windows Store stub) just pass the statusline through.
if ! python3 -c 'import sys; sys.exit(0)' >/dev/null 2>&1; then
  if [ -n "$1" ]; then printf '%s' "$input" | sh -c "$1"; else echo "allowance n/a"; fi
  exit 0
fi
dir="${DROVER_STATE:-$HOME/.local/state/drover}/allowance"
line=$(printf '%s' "$input" | DIR="$dir" python3 -c '
import json, os, re, sys, tempfile, time
try:
    d = json.load(sys.stdin)
    if not isinstance(d, dict):
        raise ValueError
except Exception:
    sys.exit(0)
rl = d.get("rate_limits") if isinstance(d.get("rate_limits"), dict) else {}
sid = re.sub(r"[^A-Za-z0-9_-]", "_", str(d.get("session_id") or ""))
if sid:
    try:
        os.makedirs(os.environ["DIR"], exist_ok=True)
        snap = {"rate_limits": rl, "model": d.get("model"), "session_id": d.get("session_id"),
                "cwd": d.get("cwd"), "ts": time.time()}
        fd, tmp = tempfile.mkstemp(dir=os.environ["DIR"], prefix=".tmp-")
        with os.fdopen(fd, "w") as f:
            json.dump(snap, f)
        os.replace(tmp, os.path.join(os.environ["DIR"], "claude-%s.json" % sid))
    except Exception:
        pass
parts = []
for key, label in (("five_hour", "5h"), ("seven_day", "7d")):
    w = rl.get(key)
    if isinstance(w, dict) and isinstance(w.get("used_percentage"), (int, float)):
        parts.append("%s %.0f%%" % (label, w["used_percentage"]))
print(" ".join(parts) or "allowance n/a")
' 2>/dev/null)
if [ -n "$1" ]; then
  printf '%s' "$input" | sh -c "$1"
else
  printf '%s\n' "${line:-allowance n/a}"
fi
exit 0
