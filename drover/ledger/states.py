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
YANKED = 'yanked'
STATES = (PROPOSED, DRAFT, READY, CLAIMED, RUNNING, GATED, REVIEWED, LANDED,
          BLOCKED, FAILED, NEEDS_OWNER, CANCELLED, YANKED)
CREATION_STATES = (PROPOSED, DRAFT)
TERMINAL = (LANDED, CANCELLED)
CLOSED = (*TERMINAL, YANKED)  # no run should exist and cleanup may act; a yanked outcome is still undecided
ACTIVE = (CLAIMED, RUNNING, GATED, REVIEWED)  # unlanded changes: scope overlap is checked against these
PENDING = (PROPOSED, DRAFT, READY)  # not started: safe to hold
OWNER_ONLY = ((PROPOSED, READY), (DRAFT, READY), (NEEDS_OWNER, LANDED), (YANKED, CANCELLED),
              *((state, YANKED) for state in STATES if state not in CLOSED))

_ESC = (BLOCKED, NEEDS_OWNER, FAILED, CANCELLED, YANKED)
TRANSITIONS = {
    PROPOSED: {DRAFT, READY, BLOCKED, CANCELLED, YANKED},
    DRAFT: {PROPOSED, READY, BLOCKED, CANCELLED, YANKED},
    READY: {DRAFT, CLAIMED, BLOCKED, NEEDS_OWNER, CANCELLED, YANKED},
    CLAIMED: {READY, RUNNING, *_ESC},
    RUNNING: {READY, GATED, *_ESC},
    GATED: {READY, RUNNING, REVIEWED, *_ESC},
    REVIEWED: {READY, RUNNING, LANDED, *_ESC},
    LANDED: set(),
    BLOCKED: {PROPOSED, DRAFT, READY, NEEDS_OWNER, FAILED, CANCELLED, YANKED},
    FAILED: {READY, NEEDS_OWNER, CANCELLED, YANKED},
    NEEDS_OWNER: {DRAFT, READY, LANDED, FAILED, CANCELLED, YANKED},
    CANCELLED: set(),
    YANKED: {LANDED, CANCELLED},
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
