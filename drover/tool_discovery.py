"""Fixed WSL tool probe shared by the hub and setup wizard."""
import re


TOOLS = ("claude", "codex")
NODE_SHIM_REASON = (
    "node shim found, but no valid executable @openai/codex Linux native binary was found"
)
CLAUDE_NODE_SHIM_REASON = (
    "node shim found; install a native Claude Linux binary to run without Node on PATH"
)
NODE_SHIM_REASONS = {"codex": NODE_SHIM_REASON, "claude": CLAUDE_NODE_SHIM_REASON}
INVALID_PATH_REASON = "resolver returned an invalid absolute path"
ABS_POSIX = re.compile(r"^/[A-Za-z0-9._+@:=,/-]+$")

# This script is fixed text: distro names and task data never enter its shell source.
# It reports only executable paths. The npm @openai/codex launcher is a Node shim; resolve it
# through the package's platform-specific native binary instead of handing the shim to wsl.exe.
# Claude Node shims are rejected because they also require Node on the exec PATH.
DISCOVERY_SCRIPT = r'''set +e
emit_tool() { printf 'DROVER_TOOL|%s|%s\n' "$1" "$2"; }
emit_error() { printf 'DROVER_ERROR|%s|%s\n' "$1" "$2"; }
node_shim_reason='node shim found, but no valid executable @openai/codex Linux native binary was found'
claude_node_shim_reason='node shim found; install a native Claude Linux binary to run without Node on PATH'

valid_candidate_path() {
    local path="$1"
    case "$path" in
        /mnt/*|*//*|*/./*|*/../*) return 1 ;;
    esac
    [[ "$path" =~ ^/[A-Za-z0-9._+@:=,/-]+$ ]] || return 1
    [ "${path##*/}" = "$2" ]
}

claude_reason=""
resolve_claude() {
    local path real magic first_line
    path="$1"
    valid_candidate_path "$path" claude || return 1
    [ -f "$path" ] && [ -x "$path" ] || return 1
    real="$(readlink -f -- "$path" 2>/dev/null || true)"
    [ -n "$real" ] || real="$path"
    magic="$(od -An -tx1 -N4 -- "$real" 2>/dev/null | tr -d '[:space:]')"
    if [ "$magic" != "7f454c46" ]; then
        first_line="$(head -n 1 -- "$real" 2>/dev/null || true)"
        case "$first_line" in
            *node*)
                claude_reason="$claude_node_shim_reason"
                return 2
                ;;
        esac
    fi
    emit_tool claude "$path"
    return 0
}

claude_found=0
claude_path="$(command -v claude 2>/dev/null || true)"
if [ -n "$claude_path" ] && resolve_claude "$claude_path"; then
    claude_found=1
fi
if [ "$claude_found" -eq 0 ]; then
    while IFS= read -r nvm_bin; do
        [ -d "$nvm_bin" ] && [ -x "$nvm_bin/claude" ] || continue
        if resolve_claude "$nvm_bin/claude"; then
            claude_found=1
            break
        fi
    done < <(printf '%s\n' "$HOME"/.nvm/versions/node/*/bin | sort -Vr)
fi
if [ "$claude_found" -eq 0 ] && [ -n "$claude_reason" ]; then
    emit_error claude "$claude_reason"
fi

codex_reason=""
resolve_codex() {
    local path real root vendor native magic first_line
    path="$1"
    valid_candidate_path "$path" codex || return 1
    [ -f "$path" ] && [ -x "$path" ] || return 1
    real="$(readlink -f -- "$path" 2>/dev/null || true)"
    [ -n "$real" ] || real="$path"

    case "$real" in
        */node_modules/@openai/codex/bin/codex.js)
            root="${real%/bin/codex.js}"
            vendor="$root/node_modules/@openai/codex-linux-x64/vendor"
            for native in "$vendor"/*/bin/codex; do
                valid_candidate_path "$native" codex || continue
                [ -f "$native" ] && [ -x "$native" ] || continue
                magic="$(od -An -tx1 -N4 -- "$native" 2>/dev/null | tr -d '[:space:]')"
                [ "$magic" = "7f454c46" ] || continue
                emit_tool codex "$native"
                return 0
            done
            codex_reason="$node_shim_reason"
            return 2
            ;;
    esac

    magic="$(od -An -tx1 -N4 -- "$real" 2>/dev/null | tr -d '[:space:]')"
    if [ "$magic" != "7f454c46" ]; then
        first_line="$(head -n 1 -- "$real" 2>/dev/null || true)"
        case "$first_line" in
            *node*)
                codex_reason="$node_shim_reason"
                return 2
                ;;
        esac
    fi
    emit_tool codex "$path"
    return 0
}

codex_found=0
codex_path="$(command -v codex 2>/dev/null || true)"
if [ -n "$codex_path" ] && resolve_codex "$codex_path"; then
    codex_found=1
fi

if [ "$codex_found" -eq 0 ]; then
    while IFS= read -r nvm_bin; do
        [ -d "$nvm_bin" ] && [ -x "$nvm_bin/codex" ] || continue
        if resolve_codex "$nvm_bin/codex"; then
            codex_found=1
            break
        fi
    done < <(printf '%s\n' "$HOME"/.nvm/versions/node/*/bin | sort -Vr)
fi

if [ "$codex_found" -eq 0 ] && [ -n "$codex_reason" ]; then
    emit_error codex "$codex_reason"
fi'''


def valid_tool_path(line: str, tool: str) -> str | None:
    """An absolute POSIX path named `tool`, with no traversal or odd characters; else None."""
    line = line.strip()
    if not ABS_POSIX.match(line) or "//" in line:
        return None
    if line.startswith("/mnt/"):
        return None
    parts = line.split("/")[1:]
    if any(p in ("", ".", "..") for p in parts) or parts[-1] != tool:
        return None
    return line


def parse_discovery_result(output: str) -> tuple[dict, dict]:
    """Return validated tool paths and fixed resolver failure reasons; ignore shell startup noise."""
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
        elif marker == "DROVER_ERROR" and value == NODE_SHIM_REASONS[tool]:
            reasons.setdefault(tool, value)
    return found, reasons


def parse_discovery(output: str) -> dict:
    """Return only validated tool paths from the shared probe's output."""
    return parse_discovery_result(output)[0]
