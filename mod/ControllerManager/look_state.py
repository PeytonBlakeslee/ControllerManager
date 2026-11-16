"""Two reusable ownership slots, independent of SDK registration and settings."""
from __future__ import annotations
import math


class AxisSlot:
    __slots__ = ('field', 'pointer', 'before', 'applied', 'active')

    def __init__(self, field):
        self.field = field
        self.pointer = None
        self.before = self.applied = 0.0
        self.active = False

    def clear(self):
        self.pointer = None
        self.active = False


class LookAccumulatorState:
    __slots__ = ('slots',)

    def __init__(self):
        self.slots = (AxisSlot('aTurn'), AxisSlot('aLookUp'))

    def __bool__(self):
        return self.slots[0].active or self.slots[1].active

    def save(self, field, pointer, before, applied):
        slot = self.slots[0] if field == 'aTurn' else self.slots[1]
        if slot.field != field:
            raise ValueError('Unknown look accumulator: ' + field)
        slot.pointer, slot.before, slot.applied = pointer, before, applied
        slot.active = True

    def restore(self):
        first_error = None
        for slot in self.slots:
            if not slot.active:
                continue
            try:
                owner = slot.pointer()
                if owner is not None and math.isclose(
                    float(getattr(owner, slot.field)), slot.applied,
                    rel_tol=1e-6, abs_tol=1e-6,
                ):
                    setattr(owner, slot.field, slot.before)
                slot.clear()
            except Exception as exc:
                # Keep unresolved ownership for lifecycle cleanup to retry.
                # The independent axis can still be restored in this call.
                if first_error is None:
                    first_error = exc
        if first_error is not None:
            raise first_error
