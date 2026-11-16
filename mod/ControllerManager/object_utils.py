"""Native identity helpers local to Controller Manager."""
from __future__ import annotations


def object_address(obj):
    if obj is None:
        return None
    try:
        address = int(obj._get_address())
        return address if address else None
    except (AttributeError, RuntimeError, TypeError, ValueError, OverflowError):
        return None


def same_native(a, b):
    address = object_address(a)
    return address is not None and address == object_address(b)
