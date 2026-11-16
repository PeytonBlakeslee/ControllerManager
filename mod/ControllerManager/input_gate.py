"""Scoped protection against capture ending events leaking to a parent menu."""
from __future__ import annotations

class ParentInputGate:
    def __init__(self):
        self.owner=None
        self.held=set()
        self.tail=set()
        self.parent_releases=None
        self.capturing=False

    def begin(self,parent,parent_releases=None):
        self.owner=parent
        self.held.clear()
        self.tail.clear()
        self.parent_releases=parent_releases if hasattr(parent_releases,'discard') else None
        self.capturing=True

    def track(self,raw):
        self.held.add(str(raw).casefold())

    def released(self,raw):
        key=str(raw).casefold()
        self.held.discard(key)
        if self.parent_releases is not None:self.parent_releases.discard(key)

    def finish(self,raw=None,*,released=False):
        self.capturing=False
        if raw is not None:
            key=str(raw).casefold()
            if released:
                self.released(key)
                self.tail.add(key)
            else:self.held.add(key)

    def reset(self):
        self.owner=None
        self.held.clear()
        self.tail.clear()
        self.parent_releases=None
        self.capturing=False

    def intercept(self,movie,raw,event):
        target=self.owner() if self.owner is not None else None
        if target is None or movie is None or target._get_address()!=movie._get_address():
            return False
        key=str(raw).casefold()
        if self.capturing:
            if int(event)==1:self.released(key)
            return True
        if key in self.held:
            if int(event)==1:
                self.released(key)
                self.tail.add(key)
            return True
        if key in self.tail:
            if int(event)==0:
                self.tail.discard(key)
                return False
            return True
        return False
