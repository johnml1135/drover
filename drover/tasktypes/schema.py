"""Pure JSON contracts for task types (schema version 1).

Registry inheritance precedes normalization. No registry, filesystem, YAML or hub
is consulted here. Reference existence, model/seat compatibility, graph safety,
approval, CAS and exact-SHA evidence belong to the consuming services. This
module checks their format and the graph-independent capability invariants.

Paths in ValidationError.errors use dotted keys and zero-based list indexes.
Task patches merge custom fields by key; null is a value, never deletion. Only
creation materializes defaults and appends acceptance defaults. Storage adapters
use decode_fields_json/encode_fields_json explicitly; public task payloads use
``fields`` dictionaries and never accept a serialized ``fields_json`` shortcut.
"""
from copy import deepcopy
import hashlib
import json
import math
import re
from typing import TypedDict
from urllib.parse import urlsplit

from drover.task_context import split_read


from .schema_core import (
    ActionReceipt, ActionRequest, CORE_IDS, FIELD_ID, FIELD_KINDS, FormDescriptor,
    FrameworkBinding, Principal, ROLES, TASK_ID, TASK_INPUTS, TASK_STORED,
    TASK_TEMPLATE_VALUES, TEMPLATE_VALUES, TUNABLE_PATHS, TYPE_ID, TaskRecord,
    TypeSnapshot, ValidationError, _boolean, _choice, _dumps, _fail, _id,
    _integer, _json, _keys, _mapping, _path, _read_ref, _relative, _strings,
    _task_ref, _text, _unique, _value, canonical_hash, decode_fields_json,
    encode_fields_json,
)
from .schema_type import _delivery, _field, _forms, _placeholders, _section, normalize_type
from .schema_task import _aliases, _connections, _framework, _merge, validate_task
