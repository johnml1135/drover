"""The one table of legal task state transitions."""

STATES = ("proposed", "draft", "ready", "claimed", "running", "gated", "reviewed", "landed",
          "blocked", "failed", "needs-owner", "cancelled")
CREATION_STATES = ("proposed", "draft")
TERMINAL = ("landed", "cancelled")
ACTIVE = ("claimed", "running", "gated", "reviewed")  # unlanded changes: scope overlap is checked against these
PENDING = ("proposed", "draft", "ready")  # not started: safe to hold
OWNER_ONLY = (("proposed", "ready"), ("draft", "ready"), ("needs-owner", "landed"))

_ESC = ("blocked", "needs-owner", "failed", "cancelled")
TRANSITIONS = {
    "proposed": {"draft", "ready", "blocked", "cancelled"},
    "draft": {"proposed", "ready", "blocked", "cancelled"},
    "ready": {"draft", "claimed", "blocked", "needs-owner", "cancelled"},
    "claimed": {"ready", "running", *_ESC},
    "running": {"ready", "gated", *_ESC},
    "gated": {"ready", "running", "reviewed", *_ESC},
    "reviewed": {"ready", "running", "landed", *_ESC},
    "landed": set(),
    "blocked": {"proposed", "draft", "ready", "needs-owner", "failed", "cancelled"},
    "failed": {"ready", "needs-owner", "cancelled"},
    "needs-owner": {"draft", "ready", "landed", "failed", "cancelled"},
    "cancelled": set(),
}


class IllegalTransition(ValueError):
    pass


def check(old, new, actor):
    if old not in TRANSITIONS:
        raise IllegalTransition(f"unknown state {old!r}")
    if new not in TRANSITIONS[old]:
        raise IllegalTransition(f"illegal transition {old} -> {new}")
    if (old, new) in OWNER_ONLY and actor != "owner":
        raise PermissionError(f"only the owner may move {old} -> {new} (actor {actor!r})")
