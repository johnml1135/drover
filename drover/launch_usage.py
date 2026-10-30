"""Worker stream usage and summaries."""
import re

HANDOFF_MAX = 1500


# Provider phrasing only (Anthropic API error types, Claude Code's "API Error: <status>" line, Codex's capacity and
# limit messages). Bare words such as "authentication" or "500" are not here: a failing test mentions them too.
PROVIDER_ERROR = re.compile(
    r"overloaded_error|rate_limit_error|api_error\b|authentication_error|permission_error|"
    r"^\s*API Error:?\s*(429|401|5\d\d)\b|Selected model is at capacity|model is at capacity|"
    r"Credit balance is too low|OAuth token has expired|usage limit (reached|has been reached)|"
    r"you(?:'|\u2019)ve hit your (usage )?limit|rate limit (exceeded|reached)|\b429 Too Many Requests|"
    r"(?:^|\W)503 Service Unavailable|Please run /login|Invalid API key", re.I)
PROVIDER_STATUS = (401, 429, 500, 502, 503, 504, 529)
PROGRESS_EVENTS = ("assistant", "user", "item.started", "item.completed", "turn.started", "turn.completed")


# Restrict launch classification to known setup diagnostics before any model output.
SETUP_ERROR = re.compile(r"(?:CODEX_HOME|CLAUDE_CONFIG_DIR).*?(?:does not exist|not found|not a directory)|"
                         r"(?:provided|config|home) path .*?(?:does not exist|not found)|"
                         r"error:.*?path does not exist|failed to (?:initialize|create) sandbox|"
                         r"bwrap:.*?(?:Operation not permitted|Permission denied)|"
                         r"spawn-failed|cannot resolve WSL seat home", re.I | re.S)


def infrastructure_error_of(status, model_started, output):
    if model_started or status.get('exit_code') == 0:
        return None
    try:
        from datetime import datetime
        elapsed = (datetime.fromisoformat(status['ended']) - datetime.fromisoformat(status['started'])).total_seconds()
    except (KeyError, TypeError, ValueError):
        return None
    if not 0 <= elapsed <= 15:
        return None
    diagnostic = (status.get('reason') or '') + '\n' + output
    match = SETUP_ERROR.search(diagnostic)
    return match.group(0)[:300] if match else None


def provider_error_of(ev):
    """The message of a provider-side error that ended a turn, else None. Qualifies only: an `error` or
    `turn.failed` stream event (codex) or an error `result` (claude) whose text uses provider phrasing, or an
    error `result` carrying a structured api_error_status. Free text a worker printed never qualifies."""
    t = ev.get("type")
    msg = None
    if t == "error":
        msg = ev.get("message")
    elif t == "turn.failed":
        err = ev.get("error")
        msg = err.get("message") if isinstance(err, dict) else err
    elif t == "result" and ev.get("is_error"):
        status = ev.get("api_error_status")
        if isinstance(status, int) and not isinstance(status, bool) and status in PROVIDER_STATUS:
            return f"API Error: {status} {str(ev.get('result') or '')}"[:300].strip()
        msg = ev.get("result")
    if isinstance(msg, str) and PROVIDER_ERROR.search(msg):
        return msg[:300]
    return None


class Usage:
    """Token totals plus the context size of the latest request, from claude stream-json or codex --json events."""

    def __init__(self):
        self.tin = self.tout = self.cached = self.context = 0
        self.text, self._claude_final = "", False
        self.sessions, self.compactions, self.models = set(), 0, {}
        self._messages = set()
        self.provider_error = None
        self.model_started = False

    def feed(self, ev):
        t = ev.get("type")
        if t in ('assistant', 'item.started', 'item.completed', 'turn.completed'):
            self.model_started = True
        perr = provider_error_of(ev)
        if perr:
            self.provider_error = perr
        elif t in PROGRESS_EVENTS:
            self.provider_error = None       # the run went on after the error: it is not what ended it
        if ev.get("session_id"):
            self.sessions.add(ev["session_id"])
        if t == "thread.started" and ev.get("thread_id"):
            self.sessions.add(ev["thread_id"])
        if t == "system" and ev.get("subtype") == "compact_boundary":
            self.compactions += 1
        if t == "assistant":
            message = ev.get("message") or {}
            key = message.get("id")
            if key and key in self._messages:
                return
            if key:
                self._messages.add(key)
            u = (ev.get("message") or {}).get("usage") or {}
            ctx = (u.get("input_tokens") or 0) + (u.get("cache_read_input_tokens") or 0) \
                + (u.get("cache_creation_input_tokens") or 0)
            self.context = ctx or self.context
            if not self._claude_final:
                self.tin += u.get("input_tokens") or 0
                self.tout += u.get("output_tokens") or 0
                self.cached += u.get("cache_read_input_tokens") or 0
            model = (ev.get("message") or {}).get("model")
            if model:
                dest = self.models.setdefault(model, {"tokens_in": 0, "tokens_out": 0, "tokens_cached": 0,
                                                      "tokens_cache_write": 0})
                for field, source in (("tokens_in", "input_tokens"), ("tokens_out", "output_tokens"),
                                      ("tokens_cached", "cache_read_input_tokens"),
                                      ("tokens_cache_write", "cache_creation_input_tokens")):
                    dest[field] += u.get(source) or 0
            for b in (ev.get("message") or {}).get("content") or []:
                if b.get("type") == "text":
                    self.text = b.get("text", "")
        elif t == "result":
            self.text = ev.get("result") or self.text
            if isinstance(ev.get("modelUsage"), dict):
                self.models = {model: {"tokens_in": values.get("inputTokens", 0),
                                       "tokens_out": values.get("outputTokens", 0),
                                       "tokens_cached": values.get("cacheReadInputTokens", 0),
                                       "tokens_cache_write": values.get("cacheCreationInputTokens", 0)}
                               for model, values in ev["modelUsage"].items()}
            u = ev.get("usage")
            if u:
                self._claude_final = True
                self.tin, self.tout = u.get("input_tokens") or 0, u.get("output_tokens") or 0
                self.cached = u.get("cache_read_input_tokens") or 0
        elif t == "turn.completed":
            u = ev.get("usage") or {}
            self.context = u.get("input_tokens") or self.context
            self.tin += u.get("input_tokens") or 0
            self.tout += u.get("output_tokens") or 0
            self.cached += u.get("cached_input_tokens") or 0
        elif t == "item.completed" and (ev.get("item") or {}).get("type") == "agent_message":
            self.text = ev["item"].get("text", "")

    def as_dict(self):
        return {"tokens_in": self.tin, "tokens_out": self.tout, "tokens_cached": self.cached,
                "context": self.context, "sessions": len(self.sessions) or int(bool(
                    self.tin or self.tout or self.cached or self.text or self.models)), "compactions": self.compactions,
                "tokens_by_model": self.models}


def _one_line(text, limit=160):
    """One tail line per message. A message holding an action record keeps its end (the record) whole."""
    text = " ".join(str(text).split())
    return text[-1000:] if '"status"' in text else text[:limit]


def summarise(ev, raw):
    t = ev.get("type")
    if t == "assistant":
        parts = []
        for b in (ev.get("message") or {}).get("content") or []:
            parts.append("tool: " + b.get("name", "?") if b.get("type") == "tool_use" else _one_line(b.get("text", "")))
        return "assistant: " + " | ".join(p for p in parts if p)
    if t == "item.completed":
        it = ev.get("item") or {}
        return f"{it.get('type')}: {_one_line(it.get('text') or it.get('command') or '')}"
    return raw[:200]


def rotation_handoff(task, usage, ceiling):
    text = (f"Rotated: context reached ~{usage.context} tokens (ceiling {ceiling}); never compact.\n"
            f"Goal: {task.get('goal') or task['title']}\n"
            f"Done (last worker message): {usage.text.strip()[-700:] or 'nothing reported'}\n"
            "Next: continue from the worktree state; read `git log` and `git status` for what exists, "
            "then finish the acceptance list.\nOpen questions: none recorded.")
    return text[:HANDOFF_MAX]
