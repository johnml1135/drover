"""Worker action-record parsing and scanning."""
import json
import re

SCAN_BYTES = 8192
MAX_SCAN_TRIES = 20
NOTE_MAX = 300
BLOCKER_KEYS = ("kind", "detail")
SHA_RE = re.compile(r"[0-9a-f]{7,40}")
ARTIFACT_URI_RE = re.compile(r"hub:tasks/[A-Za-z0-9][A-Za-z0-9._-]*/result\.md")
SHA256_RE = re.compile(r"[0-9a-f]{64}")
KIND_MAX, DETAIL_MAX = 40, 2000


# ---- action record ----------------------------------------------------------------------------------------
def _record_from(obj):
    """Normalize a worker claim; artifact persistence and delivery authority are checked by the dispatcher."""
    if not isinstance(obj, dict) or obj.get("status") not in ("done", "blocked", "ready_to_commit"):
        return None
    note, blocker = str(obj.get("note") or "")[:NOTE_MAX], obj.get("blocker")
    sha, status = str(obj.get("sha") or ""), obj["status"]
    sha = sha if SHA_RE.fullmatch(sha) else ""
    if status == "ready_to_commit":
        message = obj.get("commit_message")
        if isinstance(message, str) and message.strip() and "\0" not in message:
            return {"status": status, "sha": "", "commit_message": message.strip(), "blocker": None, "note": note}
        status, blocker = "blocked", {"kind": "no-commit-message", "detail": "ready_to_commit requires a commit message"}
    if status == "done" and "artifact" in obj:
        artifact = obj["artifact"]
        uri = artifact.get("uri") if isinstance(artifact, dict) else None
        digest = artifact.get("sha256") if isinstance(artifact, dict) else None
        if (obj.get("sha") in (None, "") and isinstance(uri, str) and ARTIFACT_URI_RE.fullmatch(uri)
                and isinstance(digest, str) and SHA256_RE.fullmatch(digest)):
            return {"status": status, "sha": "", "artifact": {"uri": uri, "sha256": digest},
                    "blocker": None, "note": note}
        status, blocker = "blocked", {"kind": "invalid-artifact",
                                      "detail": "done requires a normalized hub task-result URI and sha256, without a commit sha"}
    if status == "done" and not sha:
        status, blocker = "blocked", {"kind": "no-sha", "detail": "worker reported done without a valid commit sha"}
    if status == "blocked":
        if not isinstance(blocker, dict) or not all(blocker.get(k) for k in BLOCKER_KEYS):
            return None
        blocker = {"kind": str(blocker["kind"])[:KIND_MAX], "detail": str(blocker["detail"])[:DETAIL_MAX]}
    else:
        blocker = None
    return {"status": status, "sha": sha, "blocker": blocker, "note": note}


def parse_action_record(text):
    """The last valid action record in the complete final message, or None."""
    text = text or ""
    dec, tries, end = json.JSONDecoder(), 0, len(text)
    while tries < MAX_SCAN_TRIES:
        pos = text.rfind('"status"', 0, end)
        if pos < 0:
            return None
        end, start = pos, pos
        for _ in range(4):               # the record's own brace may sit behind a nested one
            start = text.rfind("{", 0, start)
            if start < 0:
                break
            tries += 1
            try:
                obj, _ = dec.raw_decode(text[start:])
            except ValueError:
                continue
            rec = _record_from(obj)
            if rec:
                return rec
    return None


def _message_text(ev):
    """The text of a final-message event (claude result or assistant, codex agent_message), else None."""
    if not isinstance(ev, dict):
        return None
    t = ev.get("type")
    if t == "result" and isinstance(ev.get("result"), str):
        return ev["result"]
    if t == "assistant" and isinstance(ev.get("message"), dict):
        content = ev["message"].get("content")
        texts = [b["text"] for b in content if isinstance(b, dict) and b.get("type") == "text"
                 and isinstance(b.get("text"), str)] \
            if isinstance(content, list) else []
        return "\n".join(texts) if texts else None
    item = ev.get("item")
    if t == "item.completed" and isinstance(item, dict) and item.get("type") == "agent_message" \
            and isinstance(item.get("text"), str):
        return item["text"]
    return None


def _reverse_lines(path):
    """Read complete lines backwards, including arbitrarily long stream JSON events."""
    with open(path, 'rb') as stream:
        stream.seek(0, 2)
        end, fragments = stream.tell(), []
        while end:
            start = max(0, end - SCAN_BYTES)
            stream.seek(start)
            parts = stream.read(end - start).split(b'\n')
            if len(parts) == 1:
                fragments.append(parts[0])
            else:
                yield parts[-1] + b''.join(reversed(fragments))
                yield from reversed(parts[1:-1])
                fragments = [parts[0]]
            end = start
        if fragments:
            yield b''.join(reversed(fragments))


def scan_record(out_path):
    """Trust only the final assistant/result message, even beyond the last 8 KB.

    Unframed output may report a blocker, but never authorize delivery.
    """
    try:
        tail = []
        size = 0
        for raw in _reverse_lines(out_path):
            if size < SCAN_BYTES:
                tail.append(raw[-SCAN_BYTES:])
                size += len(raw)
            try:
                text = _message_text(json.loads(raw))
            except (ValueError, UnicodeDecodeError):
                continue
            if text is not None:
                return parse_action_record(text)
    except OSError:
        return None
    text = b'\n'.join(reversed(tail)).decode('utf-8', errors='replace')
    rec = parse_action_record(text) or parse_action_record(text.replace('\\"', '"'))
    return rec if rec and rec["status"] == "blocked" else None
