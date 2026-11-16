"""Controller Manager original SDK action catalog and persistent bindings.

Contains no keyboard emulation, no gameplay hooks, and no third-party bind source.
All IDs are stable across row reordering; absent actions are retained as settings.
"""
from __future__ import annotations
from dataclasses import dataclass
from pathlib import Path
from mods_base import EInputEvent

SCHEMA = 1
BUTTON_NAMES = frozenset(('A','B','X','Y','LB','RB','LS','RS','LT','RT',
                          'D-pad Up','D-pad Down','D-pad Left','D-pad Right'))
MODIFIERS = frozenset(('Start','Back'))

@dataclass(frozen=True)
class Action:
    key: str
    mod_label: str
    label: str
    description: str
    mod: object
    binding: object

@dataclass(frozen=True)
class Shortcut:
    button: str
    modifier: str | None = None

    def __post_init__(self):
        # JSON arrays/dicts must never reach the set membership check.
        if (not isinstance(self.button, str)
                or (self.modifier is not None and not isinstance(self.modifier, str))
                or self.button not in BUTTON_NAMES
                or self.modifier not in (None, *MODIFIERS)):
            raise ValueError('A controller shortcut is one button, optionally held with Start/Back')

    def describe(self):
        return ((self.modifier+' + ') if self.modifier else '')+self.button

    def to_json(self):
        return {'button':self.button,'modifier':self.modifier}

    @classmethod
    def from_json(cls, raw):
        if not isinstance(raw,dict) or set(raw)!={'button','modifier'}:
            raise ValueError('Invalid shortcut format')
        return cls(raw['button'],raw['modifier'])


def identity(mod):
    """Stable SDK settings basename first; author+name fallback when unavailable."""
    settings = getattr(mod,'settings_file',None)
    if settings:
        return 'settings/'+Path(str(settings)).name.casefold()
    author = str(getattr(mod,'author','')).strip().casefold()
    name = str(getattr(mod,'name','')).strip().casefold()
    return 'name/'+author+'/'+name


def discover(mods, our_mod):
    # A settings basename and binding identifier are not necessarily unique to
    # one installed mod. Keep the saved key stable, but never choose a callback
    # based on whichever colliding mod happens to be discovered first.
    found=[]; seen=set(); collisions=set()
    for mod in mods:
        if mod is our_mod:continue
        ident=identity(mod)
        for kb in getattr(mod,'keybinds',()) or ():
            if bool(getattr(kb,'is_hidden',False)) or not bool(getattr(kb,'is_rebindable',True)):
                continue
            action_id=str(getattr(kb,'identifier','')).strip()
            if not action_id:continue
            key=ident+'#'+action_id.casefold()
            if key in seen:
                collisions.add(key)
                continue
            seen.add(key)
            found.append(Action(key,str(getattr(mod,'name',ident)),
                                str(getattr(kb,'display_name',action_id)),
                                str(getattr(kb,'description','')),mod,kb))
    # Suppress ALL owners of each ambiguous key, not only later duplicates.
    # The serialized assignment is separate and resumes on catalog refresh
    # once precisely one visible, rebindable owner remains.
    return (tuple(action for action in found if action.key not in collisions),
            tuple(sorted(collisions)))


class Assignments:
    def __init__(self, serialized=None):
        serialized = {} if serialized is None else serialized
        if not isinstance(serialized,dict): raise ValueError('Invalid saved shortcuts')
        ver=serialized.get('schema',SCHEMA)
        if type(ver) is not int or ver != SCHEMA:
            raise ValueError(f'Unsupported shortcut schema: {ver}')
        data=serialized.get('bindings',{})
        if not isinstance(data,dict) or len(data)>1000:raise ValueError('Invalid shortcut entries')
        self.saved={}
        self.rejected=0
        for key,record in data.items():
            if not isinstance(key,str) or '#' not in key or len(key)>500:
                self.rejected+=1
                continue
            try:
                self.saved[key]=Shortcut.from_json(record)
            except (TypeError, ValueError):
                # Reject only the invalid entry. Keep all other saved actions.
                self.rejected+=1

    def for_key(self,key):return self.saved.get(key)

    def collisions(self,key,shortcut):
        return [other for other,binding in self.saved.items() if other!=key and binding==shortcut]

    def change(self,key,shortcut,*,replace=False):
        if not isinstance(key,str) or '#' not in key or len(key)>500:
            raise ValueError('Invalid SDK action identity')
        if shortcut is not None and not isinstance(shortcut,Shortcut):raise TypeError('Invalid shortcut')
        conflict=self.collisions(key,shortcut) if shortcut else []
        if conflict and not replace:return tuple(conflict)
        # The read-side 1000-entry cap also applies to new UI assignments.
        # When replacing collisions, those entries are removed first.
        after_removal=len(self.saved)-(len(conflict) if replace else 0)
        if shortcut is not None and key not in self.saved and after_removal>=1000:
            raise ValueError('Too many SDK action shortcuts (maximum 1000)')
        if replace:
            for previous in conflict:self.saved.pop(previous,None)
        if shortcut is None:self.saved.pop(key,None)
        else:self.saved[key]=shortcut
        return ()

    def clear(self):self.saved.clear()

    def json(self):
        return {'schema':SCHEMA,'bindings':{key:binding.to_json() for key,binding in sorted(self.saved.items())}}


# Conservative physical pad button mapping. Unknown names never get guessed.
PHYSICAL = {
    'xboxtypes_a':'A', 'xboxtypes_b':'B','xboxtypes_x':'X','xboxtypes_y':'Y',
    'xboxtypes_leftshoulder':'LB','xboxtypes_rightshoulder':'RB',
    'xboxtypes_leftthumbstick':'LS','xboxtypes_rightthumbstick':'RS',
    'xboxtypes_lefttrigger':'LT','xboxtypes_righttrigger':'RT',
    'xboxtypes_leftthumb':'LS','xboxtypes_rightthumb':'RS',
    'xboxtypes_dpad_up':'D-pad Up','xboxtypes_dpad_down':'D-pad Down',
    'xboxtypes_dpad_left':'D-pad Left','xboxtypes_dpad_right':'D-pad Right',
    'xboxtypes_start':'Start','xboxtypes_back':'Back',
}

def button_name(key):return PHYSICAL.get(str(key).casefold())


class CaptureState:
    """Independent, event-driven, two-stage controller shortcut capture."""
    def __init__(self):
        self.stage='modifier'
        self.modifier=None
        self.done=False
        self.value=None

    def press(self,physical):
        if self.done:return 'finished'
        if physical=='Escape':self.done=True;return 'cancel'
        if self.stage=='modifier':
            if physical=='Y':self.done=True;return 'clear'
            if physical in MODIFIERS:self.modifier=physical
            elif physical=='X':self.modifier=None
            else:self.done=True;return 'cancel'
            self.stage='button';return 'next'
        if physical not in BUTTON_NAMES:self.done=True;return 'cancel'
        self.value=Shortcut(physical,self.modifier);self.done=True
        return 'save'


# Runtime dispatch belongs to the SDK action catalog and saved shortcut schema.
class ShortcutDispatcher:
    def __init__(self, warn):
        self.warn = warn
        self.lookup = {}
        self.modifier_actions = {'Start': (), 'Back': ()}
        self.modifiers = {}
        self.active = {}
        self.draining = set()

    @staticmethod
    def current_binding(action):
        # A mod may replace its published bindings after settings load/enable.
        # Resolve only the matched action, never rediscover the catalog per frame.
        identifier = str(action.binding.identifier).strip().casefold()
        for binding in getattr(action.mod, 'keybinds', ()) or ():
            if str(getattr(binding, 'identifier', '')).strip().casefold() != identifier:
                continue
            if (getattr(binding, 'is_hidden', False)
                    or not getattr(binding, 'is_rebindable', True)
                    or not callable(getattr(binding, 'callback', None))):
                return None
            return binding
        return None

    @classmethod
    def eligible(cls, action):
        binding = cls.current_binding(action)
        # A disabled mod opts into recovery by explicitly keeping its public
        # binding enabled (AutoPickup does this in its disable callback).
        return binding is not None and bool(binding.is_enabled)

    def configure(self, actions, assignments):
        self.cancel()
        self.lookup = {}
        self.modifier_actions = {'Start': (), 'Back': ()}
        for action in actions:
            shortcut = assignments.for_key(action.key)
            if shortcut is not None:
                pair = (shortcut.button, shortcut.modifier)
                previous = self.lookup.setdefault(pair, action)
                if previous is not action:
                    self.warn('sdk_saved_conflict:' + action.key,
                              shortcut.describe() + ' is also assigned to '
                              + previous.mod_label + ' / ' + previous.label)

        self.modifier_actions = {
            modifier: tuple(action for (_, assigned), action in self.lookup.items()
                            if assigned == modifier)
            for modifier in ('Start', 'Back')
        }

    def fire(self, action, event):
        binding = self.current_binding(action)
        if binding is None:
            return
        if not binding.is_enabled:
            return  # Only explicitly enabled recovery binds may run while disabled.
        try:
            if binding.event_filter is None:
                binding.callback(event)
            elif binding.event_filter == event:
                binding.callback()
        except Exception as exc:
            self.warn('sdk_callback:' + action.key,
                      action.mod_label + ' / ' + action.label + ': ' + str(exc))

    def cancel(self):
        self.draining.update(self.modifiers)
        self.draining.update(self.active)
        active = tuple(self.active.values())
        self.active.clear()
        self.modifiers.clear()
        # Release event-based hold actions; cancelled release-only commands
        # must not execute an action just because a menu/travel interrupted us.
        for action in active:
            binding = self.current_binding(action)
            if binding is not None and binding.event_filter is None:
                self.fire(action, EInputEvent.IE_Released)

    def input(self, button, event, ready, tap):
        if button in self.draining:
            if event == EInputEvent.IE_Pressed:
                self.draining.discard(button)  # Fresh edge after a lost release.
            else:
                if event == EInputEvent.IE_Released:
                    self.draining.discard(button)
                return True
        if not ready:
            if self.active or self.modifiers:
                self.cancel()
                return self.input(button, event, False, tap)
            return False
        if button in ('Start', 'Back'):
            if event == EInputEvent.IE_Pressed:
                if button in self.modifiers:
                    return True
                used = any(self.eligible(action)
                           for action in self.modifier_actions[button])
                if not used:
                    return False
                self.modifiers[button] = False
                return True
            if button in self.modifiers:
                if event == EInputEvent.IE_Released:
                    consumed = self.modifiers.pop(button)
                    if not consumed:
                        tap(button)
                return True
            return False
        if button in self.active:
            action = self.active[button]
            if event == EInputEvent.IE_Released:
                self.active.pop(button)
                self.fire(action, event)
            elif event == EInputEvent.IE_Repeat:
                self.fire(action, event)
            return True
        if event != EInputEvent.IE_Pressed:
            return False
        modifier = next(iter(self.modifiers)) if len(self.modifiers) == 1 else None
        action = self.lookup.get((button, modifier)) if modifier else None
        if action is None or not self.eligible(action):
            modifier = None
            action = self.lookup.get((button, None))
        if action is None or not self.eligible(action):
            return False
        self.active[button] = action
        if modifier is not None:
            self.modifiers[modifier] = True
        self.fire(action, event)
        return True
