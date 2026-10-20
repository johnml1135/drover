"""The one table of legal task state transitions."""

PROPOSED = 'proposed'
DRAFT = 'draft'
READY = 'ready'
CLAIMED = 'claimed'
RUNNING = 'running'
GATED = 'gated'
REVIEWED = 'reviewed'
LANDED = 'landed'
BLOCKED = 'blocked'
FAILED = 'failed'
NEEDS_OWNER = 'needs-owner'
CANCELLED = 'cancelled'
STATES = (PROPOSED, DRAFT, READY, CLAIMED, RUNNING, GATED, REVIEWED, LANDED,
          BLOCKED, FAILED, NEEDS_OWNER, CANCELLED)
CREATION_STATES = (PROPOSED, DRAFT)
TERMINAL = (LANDED, CANCELLED)
ACTIVE = (CLAIMED, RUNNING, GATED, REVIEWED)  # unlanded changes: scope overlap is checked against these
PENDING = (PROPOSED, DRAFT, READY)  # not started: safe to hold
OWNER_ONLY = ((PROPOSED, READY), (DRAFT, READY), (NEEDS_OWNER, LANDED))

_ESC = (BLOCKED, NEEDS_OWNER, FAILED, CANCELLED)
TRANSITIONS = {
    PROPOSED: {DRAFT, READY, BLOCKED, CANCELLED},
    DRAFT: {PROPOSED, READY, BLOCKED, CANCELLED},
    READY: {DRAFT, CLAIMED, BLOCKED, NEEDS_OWNER, CANCELLED},
    CLAIMED: {READY, RUNNING, *_ESC},
    RUNNING: {READY, GATED, *_ESC},
    GATED: {READY, RUNNING, REVIEWED, *_ESC},
    REVIEWED: {READY, RUNNING, LANDED, *_ESC},
    LANDED: set(),
    BLOCKED: {PROPOSED, DRAFT, READY, NEEDS_OWNER, FAILED, CANCELLED},
    FAILED: {READY, NEEDS_OWNER, CANCELLED},
    NEEDS_OWNER: {DRAFT, READY, LANDED, FAILED, CANCELLED},
    CANCELLED: set(),
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
