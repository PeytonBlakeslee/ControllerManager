"""Manager's native InputBindingsClip and its source-verified CLIK list.

UI_Options.Options: InputBindingsClip.onPopulate initializes keyBindingList;
WillowStagePlacedScrollingList registers empty public property getters. Use its
actual getter/setter methods. The native list owns selection and scrolling.
"""
import hashlib
import unrealsdk
from unrealsdk.unreal import WeakPointer


def as_value(value):
    if value is None:
        return unrealsdk.make_struct('ASValue', Type=1)
    if isinstance(value, bool):
        return unrealsdk.make_struct('ASValue', Type=4, B=value)
    if isinstance(value, (int, float)):
        return unrealsdk.make_struct('ASValue', Type=2, N=float(value))
    return unrealsdk.make_struct('ASValue', Type=3, S=str(value))


class NativeBindingView:
    def __init__(self, warning):
        self.warn = warning
        self._movie = self._clip = self._receiver = self._provider = None
        self._section = self._structure = None
        self._values = {}
        self._tags = {}
        self._drawing = False
        self._renderer_positions = {}
        self._row_kinds = ()
        self._style_hook = None
        self._style_frames = 0
        self._style_signature = None
        self._style_path = None
        self._restore_focus = None
        self._tip = self._description = None

    def belongs_to(self, movie):
        owner = self._movie() if self._movie else None
        return owner is not None and owner._get_address() == movie._get_address()

    def active_clip(self):
        native = self._provider() if self._provider else None
        return native.ControllerMappingClip if native is not None else None

    def belongs_to_provider(self, native):
        owner = self._provider() if self._provider else None
        return owner is not None and owner._get_address() == native._get_address()

    def ensure(self, movie, provider):
        if self.belongs_to(movie) and self.active_clip() is not None:
            return self.active_clip()
        self.close()
        native = provider.native()
        clip = native.ControllerMappingClip if native is not None else None
        if clip is None:
            raise RuntimeError('Native controller binding clip is not initialized')
        self._movie, self._clip = WeakPointer(movie), WeakPointer(clip)
        self._provider = WeakPointer(native)
        return clip

    def index_for_tag(self, tag):
        return self._tags.get(str(tag))

    def find_receiver(self, movie):
        clip = self.active_clip()
        component = clip.GetObject('keyBindingList') if clip is not None else None
        if component is None:
            return None
        # These methods are present in the shipped SWF. Reading the public
        # selectedIndex/dataProvider getters returns undefined on this class.
        selected = component.Invoke('__get__selectedIndex', [])
        if int(selected.Type) != 2 or component.GetObject('renderers') is None:
            return None
        self._receiver = WeakPointer(component)
        return component

    def focus_left(self, movie):
        self._restore_focus = None
        drawing = self._drawing
        self._drawing = True
        try:
            receiver = self.find_receiver(movie)
            if receiver is not None:
                receiver.Invoke('__set__focused', [as_value(0)])
                receiver.Invoke('__set__displayFocus', [as_value(False)])
            movie.TheList.SetFocused(1)
            movie.TheList.SetBool('displayFocus', True)
        finally:self._drawing = drawing

    def focus_right(self, movie, index):
        receiver = self.find_receiver(movie)
        if receiver is None:
            self.focus_left(movie)
            raise RuntimeError('Right list is not ready; select the category again')
        drawing = self._drawing
        self._drawing = True
        try:
            movie.TheList.SetFocused(0)
            movie.TheList.SetBool('displayFocus', False)
            receiver.Invoke('__set__focused', [as_value(1)])
            receiver.Invoke('__set__displayFocus', [as_value(True)])
            if int(receiver.Invoke('__get__selectedIndex', []).N) != index:
                receiver.Invoke('__set__selectedIndex', [as_value(index)])
        finally:self._drawing = drawing

    def restore_after_modal(self, movie, index, on_restored=None):
        # The UI manager finishes restoring parent focus after close callbacks.
        self.restore_after_refresh(movie, index, on_restored)

    def restore_after_refresh(self, movie, index, on_restored=None):
        # Also retain focus through deferred Flash callbacks after a direct edit.
        self._restore_focus = (WeakPointer(movie), self._section, index, on_restored)
        self._description = self._tip = None
        self._schedule_style()

    def selection_changed(self, movie, index):
        receiver = self.find_receiver(movie)
        if receiver is None:return
        start = int(receiver.Invoke('__get__scrollPosition', []).N)
        count = int(receiver.GetObject('renderers').GetFloat('length'))
        # CLIK can scroll after the callback, especially near its row margin.
        if self._style_signature is None or start != self._style_signature[0] or index <= start+1 or index >= start+count-2:
            self._schedule_style()

    def describe(self, movie, text):
        if text != self._description:
            movie.OptionsObj.SetDescription(text)
            self._description = text

    def _space_sections(self):
        """Move existing stage-placed renderers; add no data or selectable rows.

        The SWF initializes renderers from menu0, menu1, ... and retains these
        clips while scrolling. Their native index identifies the displayed row.
        """
        # GetObject creates a transient GFxObject wrapper. A weak wrapper from
        # an earlier callback may already be gone although the Flash list lives.
        clip = self.active_clip()
        receiver = clip.GetObject('keyBindingList') if clip is not None else None
        renderers = receiver.GetObject('renderers') if receiver is not None else None
        if renderers is None:
            return
        start = int(receiver.Invoke('__get__scrollPosition', []).N)
        signature = (start, self._row_kinds, int(renderers.GetFloat('length')))
        if signature == self._style_signature:return
        gaps = [i for i in range(1, len(self._row_kinds))
                if self._row_kinds[i] != self._row_kinds[i - 1]]
        for slot in range(signature[2]):
            renderer = renderers.GetObject(str(slot))
            if renderer is None:
                continue
            current = renderer.GetFloat('_y')
            base = self._renderer_positions.setdefault(slot, current)
            index = start + slot
            offset = 4 * sum(start < boundary <= index for boundary in gaps)
            if abs(current-base-offset) > 0.01:
                renderer.SetFloat('_y', base + offset)
        self._style_signature = signature

    def _schedule_style(self):
        # CLIK completes scrollToIndex/populateData after its selection callback.
        # Finish cosmetic positioning on two UI ticks, then remove the hook.
        # This neither polls selection nor writes selection/focus during ticks.
        self._style_frames = 2
        if self._style_hook is not None:
            return
        if self._style_path is None:
            self._style_path = unrealsdk.find_object('Function', 'WillowGame.WillowUIInteraction:TickImp')._path_name()
        path = self._style_path
        kind = unrealsdk.hooks.Type.POST_UNCONDITIONAL
        ident = 'ControllerManager:UI:section-spacing'
        self._style_hook = (path, kind, ident)

        def finish(obj, args, ret, func):
            try:
                self._space_sections()
            finally:
                self._style_frames -= 1
                if self._style_frames <= 0:
                    restore, self._restore_focus = self._restore_focus, None
                    self._cancel_style()
                    if restore is not None:
                        owner, section, index, on_restored = restore
                        movie = owner()
                        if movie is not None and self.belongs_to(movie) and section == self._section:
                            self.focus_right(movie, index)
                            if on_restored is not None:on_restored(movie)

        unrealsdk.hooks.add_hook(path, kind, ident, finish)

    def _cancel_style(self):
        if self._style_hook is not None:
            unrealsdk.hooks.remove_hook(*self._style_hook)
            self._style_hook = None
        self._style_frames = 0

    def hide(self):
        self._cancel_style()
        self._restore_focus = None
        self._row_kinds = ()
        self._space_sections()
        clip = self.active_clip()
        if clip is not None:
            clip.Hide()
        self._section = self._structure = None
        self._values.clear()
        self._tags.clear()
        self._tip = self._description = None

    def close(self):
        self._cancel_style()
        if self._movie is not None:
            movie = self._movie()
            if movie is not None:
                try:
                    self.focus_left(movie)
                    self.hide()
                except (ReferenceError, RuntimeError, AttributeError):
                    pass
        self._movie = self._clip = self._receiver = self._provider = None
        self._section = self._structure = None
        self._values = {}
        self._tags = {}
        self._renderer_positions = {}
        self._row_kinds = ()
        self._style_signature = None
        self._restore_focus = None
        self._tip = self._description = None

    def render(self, movie, side, provider):
        if self._drawing:
            return
        if side.section not in side.sections:
            self.hide()
            return
        title, rows = side.sections[side.section]
        clip = self.ensure(movie, provider)
        self._row_kinds = tuple(row.kind for row in rows)
        native = provider.native()
        structure = (side.section, tuple((r.identifier, r.label, r.kind) for r in rows))
        self._drawing = True
        try:
            rebuilt = structure != self._structure or len(native.KeyBinds) != len(rows)
            if rebuilt:
                native.KeyBinds = []
                native.CurrentKeyBindSelection = -1
                native.bNeedsToSaveKeyBinds = False
                clip.EmptyKeyData()
                self._tags = {}
                self._values = {}
                for index, row in enumerate(rows):
                    identity = side.section + ':' + row.identifier
                    tag = 'ControllerManager:' + hashlib.sha1(identity.encode()).hexdigest()[:20]
                    idx = int(native.AddKeyBindEntry(tag, 'DUMMY', row.label))
                    value = str(row.read())
                    native.KeyBinds[idx].Object = clip.AddKeyData(tag, row.label, value)
                    self._tags[tag] = index
                    self._values[tag] = value
                clip.GetObject('controller').SetText(title)
                self._structure = structure
                native.CurrentKeyBindSelection=side.indexes.get(side.section,0)
                clip.InvalidateKeyData()
                self._style_signature = None
                self._schedule_style()
            else:
                changed = {}
                for index, (key, row) in enumerate(zip(native.KeyBinds, rows)):
                    tag, value = str(key.Tag), str(row.read())
                    if self._values.get(tag) != value:
                        key.Object.SetString('value', value)
                        self._values[tag] = value
                        changed[index] = value
                if changed:
                    # Values alone do not require CLIK to repopulate the list.
                    # The shipped renderer's setValues updates text without
                    # touching selection, scroll position or native focus.
                    receiver = clip.GetObject('keyBindingList')
                    renderers = receiver.GetObject('renderers') if receiver is not None else None
                    if renderers is not None:
                        for slot in range(int(renderers.GetFloat('length'))):
                            renderer = renderers.GetObject(str(slot))
                            if renderer is None:continue
                            index = int(renderer.GetFloat('index'))
                            if index in changed:
                                renderer.Invoke('setValues', [as_value(rows[index].label), as_value(changed[index])])
            if not clip.bIsShown:
                clip.Show()
            self._section = side.section
            # Show may focus its first row. Resolve the real component before
            # returning focus to the left for a preview.
            if self._receiver is None:
                try:
                    self.find_receiver(movie)
                except (AttributeError, RuntimeError, ValueError):
                    # Preview does not require querying Selection. Entering
                    # the list reports an unavailable focus interface visibly.
                    pass
            if rebuilt:
                if side.focus == 'left':self.focus_left(movie)
                else:self.focus_right(movie, side.indexes.get(side.section, 0))
            self.update_tip(side)
        finally:
            self._drawing = False

    def update_tip(self, side):
        clip = self.active_clip()
        if clip is None:
            return
        row = side.selected()
        tip = ('' if side.focus == 'left' else
               'Left / Right: Adjust  |  B: Categories' if row and row.adjust else
               'A: Change  |  X: Unbind  |  Y: Default  |  B: Categories' if row and row.kind == 'binding' else
               'A: Change  |  X: Unbind  |  B: Categories' if row and row.kind == 'shortcut' else
               'A: Select  |  B: Categories')
        if tip != self._tip:
            clip.SetCustomizeTip(tip)
            self._tip = tip

    def repopulate(self, movie, side, provider):
        if self._drawing:
            return
        self._structure = None
        self._receiver = None
        self.render(movie, side, provider)
