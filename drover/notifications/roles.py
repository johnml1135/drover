"""Director sessions, checkpointing, issue handling, and wake admission."""
import hashlib
import json
import re
import secrets

from ..ledger import ops
from ..ledger.db import tx
from .core_scope import Conflict, _role, recipient_owner
from .core_evidence import coalesce_issue, issue_key
from .roles_base import CAPABILITIES, canonical, hashed, _ttl, _runtime, _artifact, _delivery_identity
from .roles_auth import issue_session, authenticate, admission, acquire, validate_lease
from .roles_commit import (role_state, get_checkpoint, _acknowledge, commit, _issue, claim_issue,
                           validate_issue, _hold_issue, hold_issue)
from .roles_wake import (signal, refresh_wake, validate_policy, _hold_role, _clear_role_hold,
                         reserve_wake, replace)
