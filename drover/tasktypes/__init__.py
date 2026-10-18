"""Format-independent task type contracts; importing this package needs no codec."""

from .schema import (
    ActionReceipt,
    ActionRequest,
    FormDescriptor,
    FrameworkBinding,
    Principal,
    TaskRecord,
    TypeSnapshot,
    ValidationError,
    canonical_hash,
    decode_fields_json,
    encode_fields_json,
    normalize_type,
    validate_task,
)

__all__ = [
    'ActionReceipt', 'ActionRequest', 'FormDescriptor', 'FrameworkBinding',
    'Principal', 'TaskRecord', 'TypeSnapshot', 'ValidationError', 'canonical_hash',
    'decode_fields_json', 'encode_fields_json', 'normalize_type', 'validate_task',
]
