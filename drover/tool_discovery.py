"""Fixed WSL tool probe shared by the hub and setup wizard."""
import re

from . import placement


TOOLS = ("claude", "codex", "pwsh")
INVALID_PATH_REASON = "resolver returned an invalid absolute path"
ABS_POSIX = re.compile(r"^/[A-Za-z0-9._+@:=,/-]+$")

# Capture the non-login worker PATH before looking in the user's login PATH.
DISCOVERY_SCRIPT = r'''if [ -z "${DROVER_WORKER_PATH+x}" ]; then
    export DROVER_WORKER_PATH="$PATH"
    exec /bin/bash -lc "$BASH_EXECUTION_STRING"
fi
set +e
for tool in claude codex pwsh; do
    path="$(command -v "$tool" 2>/dev/null || true)"
    [ -f "$path" ] && [ -x "$path" ] || continue
    IFS= read -r -N 2 magic < "$path"
    if [ "$magic" = '#!' ]; then
        IFS= read -r line < "$path"
        read -r interpreter argument <<< "${line#\#!}"
        [ -f "$interpreter" ] && [ -x "$interpreter" ] || continue
        if [ "$interpreter" = /usr/bin/env ] || [ "$interpreter" = /bin/env ]; then
            # Accept a single interpreter name; complex env expressions need an explicit machine path.
            case "$argument" in ''|-*|*[[:space:]]*) continue ;; esac
            resolved="$(PATH="$DROVER_WORKER_PATH" command -v -- "$argument")"
            [ -f "$resolved" ] && [ -x "$resolved" ] || continue
        fi
    else
        IFS= read -r -N 4 magic < "$path"
        [ "$magic" = $'\177ELF' ] || continue
    fi
    printf 'DROVER_TOOL|%s|%s\n' "$tool" "$path"
done
'''


def valid_tool_path(line: str, tool: str) -> str | None:
    """An absolute POSIX path named `tool`, with no traversal or odd characters; else None."""
    line = line.strip()
    if not ABS_POSIX.match(line) or "//" in line:
        return None
    if not placement.is_linux_native(line):
        return None
    parts = line.split("/")[1:]
    if any(p in ("", ".", "..") for p in parts) or parts[-1] != tool:
        return None
    return line


def parse_discovery_result(output: str) -> tuple[dict, dict]:
    """Return validated tool paths and resolver failure reasons; ignore shell startup noise."""
    found, reasons = {}, {}
    for line in output.splitlines():
        fields = line.strip().split("|", 2)
        if len(fields) != 3 or fields[1] not in TOOLS:
            continue
        marker, tool, value = fields
        if marker == "DROVER_TOOL":
            path = valid_tool_path(value, tool)
            if path and tool not in found:
                found[tool] = path
            elif not path:
                reasons.setdefault(tool, INVALID_PATH_REASON)
    return found, reasons


def parse_discovery(output: str) -> dict:
    """Return only validated tool paths from the shared probe's output."""
    return parse_discovery_result(output)[0]
