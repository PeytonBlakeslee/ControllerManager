"""Session selection for native binding lists; Flash owns navigation and scrolling."""
from __future__ import annotations
from dataclasses import dataclass
from typing import Callable

@dataclass
class PanelRow:
    label: str
    read: Callable[[], str]
    identifier: str = ''
    kind: str = 'binding'
    invoke: Callable[[], None] | None = None
    adjust: Callable[[int], None] | None = None
    help: str = ''
    clear: Callable[[], None] | None = None
    restore: Callable[[], None] | None = None

class SidePanel:
    def __init__(self):
        self.sections: dict[str, tuple[str, list[PanelRow]]] = {}
        self.indexes: dict[str, int] = {}
        self.section: str | None = None
        self.focus = 'left'
        self.releases: set[str] = set()

    def set_sections(self, sections):
        self.sections = dict(sections)
        if self.section not in self.sections:
            self.section = None
            self.focus = 'left'
        self.indexes = {
            key: max(0, min(self.indexes.get(key, 0), len(rows)-1))
            for key, (_, rows) in self.sections.items()
        }

    def reset_focus(self):
        self.focus = 'left'
        self.section = None
        self.indexes.clear()
        self.releases.clear()

    def choose(self, key, enter=False):
        if key not in self.sections:
            self.focus = 'left'
            self.section = None
            return False
        self.section = key
        self.indexes.setdefault(key, 0)
        if enter:
            self.focus = 'right' if self.sections[key][1] else 'left'
        return True

    def selected(self):
        if self.section not in self.sections:
            return None
        rows = self.sections[self.section][1]
        if not rows:
            return None
        return rows[self.indexes.get(self.section, 0)]

    def activate(self):
        row = self.selected()
        if row is None:
            return False
        if row.adjust is None and row.invoke is not None:
            row.invoke()
        return True

    def adjust(self, direction):
        row = self.selected()
        if self.focus != 'right' or row is None or row.adjust is None:
            return False
        row.adjust(direction)
        return True
