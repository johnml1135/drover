"""Request bodies of the notification HTTP adapter: one strict, size-bounded model per route."""
import json
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, model_validator

from hub.api import TaskIn

Identity = Annotated[str, Field(min_length=1, max_length=256)]


class Input(BaseModel):
    model_config = ConfigDict(extra='forbid')

    @model_validator(mode='after')
    def bounded(self):
        try:
            size = len(json.dumps(self.model_dump(), allow_nan=False).encode())
        except ValueError:
            raise ValueError('finite JSON required')
        if size > 65536:
            raise ValueError('request exceeds 65536 bytes')
        return self


class Preferences(Input):
    preset: Literal['quiet', 'normal', 'verbose'] = 'normal'
    mode: Literal['profile', 'immediate', 'digest', 'muted'] = 'profile'
    class_modes: dict[str, Literal['immediate', 'digest', 'muted']] = Field(default_factory=dict)
    digest_interval: float = Field(default=900, ge=1, le=604800, strict=True)
    digest_wake: StrictBool = False
    detail_limit: int = Field(default=50, ge=1, le=100, strict=True)
    minimum_severity: Literal['info', 'warning', 'critical'] = 'info'
    enabled: StrictBool = True
    expires_at: float | None = Field(default=None, strict=True)
    stop_on_completion: StrictBool = False


class FollowPreferences(Preferences):
    replay_after: int | None = Field(default=None, ge=0, strict=True)


class TunePreferences(Preferences):
    selectors: dict[str, Any] | None = None


class Policy(Input):
    notifications: Preferences = Field(default_factory=Preferences)
    max_pending_routine: int = Field(default=1000, ge=1, le=100000, strict=True)
    max_pending_actionable: int = Field(default=10000, ge=1, le=100000, strict=True)
    broadcast_summary: StrictBool = False
    action_allowlist: list[Literal['task.retry', 'task.comment', 'issue.resolve']] = Field(default_factory=list)
    allowed_models: list[Annotated[str, Field(min_length=1, max_length=128)]] = Field(default_factory=list)
    recovery_attempt_limit: int = Field(default=2, ge=0, le=2, strict=True)
    replacement_limit: int = Field(default=1, ge=0, le=1, strict=True)
    wake_limit_per_hour: int = Field(default=6, ge=0, le=1000, strict=True)
    wake_window_s: int = Field(default=600, ge=1, le=86400, strict=True)
    urgent_window_s: int = Field(default=60, ge=1, le=86400, strict=True)
    token_budget: int = Field(default=0, ge=0, le=10**12, strict=True)


class Role(Input):
    role_id: Identity
    policy: Policy = Field(default_factory=Policy)


class Session(Input):
    session_id: Identity
    ttl: float = Field(default=3600, gt=0, le=86400)
    capabilities: list[Literal['notifications.read', 'notifications.process', 'notifications.tune']] | None = None


class Follow(Input):
    selectors: dict[str, Any]
    preferences: FollowPreferences = Field(default_factory=FollowPreferences)


class Tune(Input):
    expected_revision: int = Field(ge=1, strict=True)
    preferences: TunePreferences


class Lease(Input):
    ttl: float = Field(default=60, gt=0, le=86400)


class Commit(Input):
    lease_token: Identity
    checkpoint: dict[str, Any]
    delivery_ids: list[Identity] = Field(default_factory=list, max_length=100)
    delivery_versions: dict[str, Annotated[int, Field(gt=0, strict=True)]] = Field(default_factory=dict)
    actions: list[dict[str, Any]] = Field(default_factory=list, max_length=32)
    request_key: Identity
    release: StrictBool = False

    @model_validator(mode='after')
    def versions(self):
        if set(self.delivery_ids) != set(self.delivery_versions):
            raise ValueError('echo message_version for every delivery_id')
        return self


class Transfer(Input):
    task_id: Identity
    role_id: Identity
    expected_revision: int | None = Field(default=None, ge=0, strict=True)
    event_class: str = '*'


class Group(Input):
    group_id: Identity
    parent_id: Identity | None = None
    mode: Literal['synthesis'] = 'synthesis'


class OwnerGroup(Group):
    role_id: Identity


class Revision(Input):
    expected_revision: int = Field(ge=1, strict=True)
    request_key: Identity


class Enroll(Revision):
    child_id: Identity


class Notice(Input):
    targets: Literal['all'] | list[Identity]
    body: str | dict[str, Any]
    urgency: Literal['summary', 'urgent'] = 'summary'
    request_key: Identity


class Control(Input):
    request_key: Identity


class Resume(Control):
    expected_epoch: int = Field(ge=0, strict=True)


class Skill(Control):
    skill: Identity
    digest: Identity
    targets: Literal['all'] | list[Identity]


class Apply(Input):
    lease_token: Identity
    digest: Identity | None = None


class Issue(Lease):
    lease_token: Identity
    max_attempts: int = Field(default=2, ge=1, le=2, strict=True)


class Hold(Input):
    lease_token: Identity
    reason: str = Field(min_length=1, max_length=2048)


class Submit(TaskIn, Input):
    title: str = Field(min_length=1, max_length=4096)
    parent_id: Identity | None = None
    group_id: Identity | None = None
