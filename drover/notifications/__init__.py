"""Trusted in-process durable notifications; transports must authenticate callers."""
from .core import Conflict, Notifications

__all__ = ['Conflict', 'Notifications']
