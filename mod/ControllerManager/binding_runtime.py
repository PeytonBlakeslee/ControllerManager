"""Own only handler arrays in the local player's verified live button table.

Physical button names/state/map pointers, native configuration and shared input
definitions remain untouched. Changes wait for two released-input boundaries.
"""
from __future__ import annotations
import copy
import math
import sys
from collections.abc import Mapping

import unrealsdk
from unrealsdk.hooks import Type
from unrealsdk.unreal import WeakPointer, WrappedArray, WrappedStruct, UObject

from .layout import KEYS, SHORT, canonical, resolve_keys, solve, validate


from .object_utils import same_native as same


_schema_cache={}


def clear_schema_cache():
    _schema_cache.clear()


def schema_names(struct):
    address=struct._get_address()
    cached=_schema_cache.get(address)
    # The dictionary key already matches the live type address. A valid weak
    # serial proves it is the same object, including protection from address reuse.
    if cached is not None and cached[0]() is not None:return cached[1]
    names=tuple(str(p.Name) for p in struct._properties())
    if len(names)>128:raise ValueError('Handler schema exceeds supported bounds')
    if len(_schema_cache)>=64:_schema_cache.clear()
    _schema_cache[address]=(WeakPointer(struct),names)
    return names


def fingerprint(value,depth=0):
    """Compare owned struct contents without comparing mutable button state."""
    if depth>10: raise ValueError('Handler nesting is unsupported')
    if value is None or isinstance(value,(str,bool,int)): return value
    if isinstance(value,float): return value if math.isfinite(value) else repr(value)
    # WrappedStruct._type is a UStruct; WrappedArray._type is its inner
    # property (e.g. StructProperty), which has no _properties method.
    # Lists written to Unreal are read back as WrappedArray: both must
    # fingerprint identically for readback and ownership restoration.
    if isinstance(value,(WrappedArray,list,tuple)):
        if len(value)>128: raise ValueError('Handler array exceeds supported bounds')
        return tuple(fingerprint(v,depth+1) for v in value)
    if isinstance(value,WrappedStruct):
        return tuple((name,fingerprint(getattr(value,name),depth+1)) for name in schema_names(value._type))
    if isinstance(value,UObject):return ('object',value._get_address())
    raise ValueError(f'Unsupported handler value: {type(value).__name__}')


def require(struct,name,kind):
    prop=struct._find_prop(name)
    if str(prop.Class.Name)!=kind or int(prop.ArrayDim)!=1:
        raise ValueError(f'{name}: expected scalar {kind}')
    return prop


class BindingRuntime:
    def __init__(self,get_pc,log,warn,input_rebuilt=None,layout_committed=None,
                 input_rebuild_failed=None):
        self.get_pc=get_pc; self.log=log; self.warn=warn
        self.input_rebuilt=input_rebuilt
        self.input_rebuild_failed=input_rebuild_failed
        self.layout_committed=layout_committed
        self.pc=self.pi=None; self.ready=False; self.issue='waiting for a loaded player'
        self.native={}; self.baseline={}; self.base_fp={}; self.applied={}
        self.released=None; self.quiet=0; self.refresh=False
        self.paused=False; self.last_enabled=False; self.last_layout=None
        self.compat=None; self.compat_original=self.compat_applied=None
        self.compat_keys={}; self.drain_hook=None; self.draining=False
        self.drain_failures=0; self.drain_stalled=False; self.drain_issue=''
        self.compat_lost=False;self.compat_refresh=False;self.retry=False
        self.base_latent=[];self.latent_fp=();self.latent_applied=None
        self.config=None
        self.generation=0
        self.layout_cache={}
        self.layout_inputs={}
        self.row_indices={}
        self._sync_request=None
        self._sync_choices=None
        self._sync_pending=False
        self._pending_keys=tuple(KEYS)
        # While Custom Bindings is enabled, BL2's live preset/rebinding layer is
        # neutralized to the factory Default preset. The user's real native
        # preset index/rebinding metadata is kept here and rebuilt on restoration.
        self.override_active=False
        self._forcing_native=False
        self.user_preset_index=None
        self.user_rebindings=[]
        self.native_change_pending=False
        # A failed native preset rebuild may have mutated the live input table.
        # Do not treat it as clean or unowned without a verified rebuild.
        self._native_transition_pending=False

    def invalidate_layout(self):
        self.layout_cache.clear();self.layout_inputs.clear()
        self._sync_request=None

    def invalidate_rows(self):
        self.row_indices.clear()

    def notify_layout_committed(self,context,mapping,enabled=True):
        """Notify optional consumers only after the final live mapping is stable.

        Never call this for the temporary factory-Default table used while rebasing.
        Compatibility listeners may cache physical keys, so the notification belongs
        after the two-boundary commit (or after completed native restoration).
        """
        if self.layout_committed is None:return
        try:
            snapshot=None if mapping is None else dict(mapping)
            self.layout_committed(context,snapshot,bool(enabled))
        except Exception as exc:
            self.warn('layout_commit_notify',f'Binding-consumer refresh failed safely: {type(exc).__name__}: {exc}')

    def stage_rebase(self):
        """Finish adopting a native baseline before solving a custom layout against it."""
        self.quiet=0
        self._sync_request=None
        self._sync_choices=None
        self._sync_pending=True
        self._pending_keys=tuple(KEYS)

    def request_fresh_rebind(self):
        """Force one safe restore/capture/reapply cycle on the current live table."""
        if self._native_transition_pending or not self.ready:return False
        self.retry=True
        self.quiet=0
        self.last_layout=None
        self._sync_request=None
        self._sync_choices=None
        self._sync_pending=True
        self._pending_keys=tuple(KEYS)
        return True

    def rebuild_native_override(self):
        """Rebuild fresh factory-Default native input devices, then stage reapply.

        Used only after a settled restart/travel while Custom Bindings owns the
        live controller layer. Unlike request_fresh_rebind(), this deliberately
        calls SetupInputDevices so native RemappedButton/action-state storage is
        recreated instead of copying the existing handler structs back and forth.
        Saved preset/rebinding metadata remains untouched.
        """
        if self._native_transition_pending:return False
        if not self.ready or not self.override_active:return False
        # SetupInputDevices will replace the live table, so the old per-row
        # ownership fingerprints are no longer meaningful. Controller Binds is a
        # separate Python lookup and must still be relinquished normally first.
        self.applied={}
        self.latent_applied=None
        self.restore_compat()
        self.invalidate_rows()
        clear_schema_cache()
        self._force_factory_default()
        self._capture_after_native_reset()
        self.compat_lost=False
        self.compat_refresh=True
        self.retry=False
        self.last_layout=None
        self.stage_rebase()
        return True

    def needs_service(self,enabled):
        """Whether a temporary PlayerInput boundary hook is currently needed."""
        if self._native_transition_pending:return False
        # At startup there may be no PlayerInput yet. Keeping the one temporary
        # global PlayerInput hook alive until discovery gives the runtime a first
        # boundary without turning the coordinator back into a permanent hook.
        return bool(self.refresh or self.retry or self._sync_pending
            or enabled and (not self.ready or not self.last_enabled or not self.override_active))

    def resolved_layout(self,context,choices):
        # Callers invalidate and replace choices on configuration changes.
        # Complete ownership is rechecked immediately before every write.
        signature=(self.generation,tuple(choices[k] for k in KEYS))
        if self.layout_inputs.get(context)!=signature:
            self.layout_cache[context]=solve(self.native,choices)
            self.layout_inputs[context]=signature
        return self.layout_cache[context]

    def rows(self):
        pi=self.pi() if self.pi is not None else None
        if pi is None: return {}
        if self.row_indices:
            try:
                # No callbacks occur while checking cached rows. Read the live
                # array once for this call, retaining every button-name check.
                buttons=pi.Buttons
                cached={key:buttons[index] for key,index in self.row_indices.items()}
                if len(cached)==len(KEYS) and all(canonical(row.ButtonName)==key for key,row in cached.items()):
                    return cached
            except (IndexError,KeyError,TypeError,ValueError,AttributeError):
                pass
            self.row_indices.clear()
        rows={};indices={}
        for index,row in enumerate(pi.Buttons):
            key=canonical(row.ButtonName)
            if key not in KEYS: continue
            if key in rows: raise ValueError(f'Duplicate physical button: {SHORT[key]}')
            rows[key]=row
            indices[key]=index
        missing=[SHORT[k] for k in KEYS if k not in rows]
        if missing: raise ValueError('Missing live buttons: '+', '.join(missing))
        self.row_indices=indices
        return rows

    def current(self,rows):
        return {key:fingerprint(row.InputDeviceHandlers) for key,row in rows.items()}

    def recover_stale_press(self,physical):
        """Reset only an impossible pre-press row state; return old state if repaired.

        Called from WillowGameViewportClient.InputKey PRE, before the fresh physical
        press can reach PlayerInput. A non-Released state here therefore belongs
        to an earlier input edge which failed to unwind. Handler ownership and
        every other button remain untouched.
        """
        if not self.ready or self.released is None or physical not in KEYS:return None
        row=self.rows()[physical]
        old=int(row.State)
        if old==self.released:return None
        row.State=self.released
        row.LastPressedTime=-1.0
        if int(row.State)!=self.released:
            raise ValueError(f'Stale button state repair failed readback: {SHORT[physical]}')
        return old

    def attach(self,pc,override=False):
        if (self._native_transition_pending and self.pi is not None
                and same(self.pi(),pc.PlayerInput)):
            self.ready=False;self.paused=True
            self.issue='Native preset transition incomplete; wait for a fresh PlayerInput'
            return
        if self.pi is not None and same(self.pi(),pc.PlayerInput) and self.ready:
            if override and not self.override_active:self.activate_native_override()
            elif not override and self.override_active:self.restore_user_native(recapture=True)
            self.cancel_drain(); return
        try: self.detach()
        except Exception as exc:
            self.warn('binding_retired',f'Retired input cleanup unavailable; no unknown fields overwritten: {exc}')
            self.restore_compat();self.applied={}
        self.pc=WeakPointer(pc); self.pi=WeakPointer(pc.PlayerInput)
        self.ready=False; self.paused=False; self.applied={}; self.refresh=False
        self.native={};self.baseline={};self.base_fp={}
        self._native_transition_pending=False  # New PlayerInput lifetime.
        self.invalidate_rows()
        try:
            pi=pc.PlayerInput
            buttons=require(pi.Class,'Buttons','ArrayProperty').Inner.Struct
            require(buttons,'ButtonName','NameProperty')
            state=require(buttons,'State','ByteProperty')
            require(buttons,'LastPressedTime','FloatProperty')
            enum=state.Enum._as_py()
            released=[v for n,v in enum.__members__.items() if n.lower().endswith('_released')]
            if len(released)!=1: raise ValueError('Released button state unverified')
            self.released=int(released[0])
            handlers=require(buttons,'InputDeviceHandlers','ArrayProperty').Inner.Struct
            require(handlers,'SourceDevice','ObjectProperty')
            require(handlers,'SourceIndex','IntProperty')
            require(handlers,'RemappedButton','StructProperty')
            # This unreadable native map is deliberately not reassigned. Row
            # locations, order and physical key names never change.
            require(pi.Class,'NameToButtonStatePtr','MapProperty')
            latent=require(pi.Class,'LatentButtons','ArrayProperty')
            if str(latent.Inner.Class.Name)!='NameProperty':
                raise ValueError('Latent button registry must contain native names')
            if override:
                self.activate_native_override()
            else:
                self.capture()
            self.ready=True; self.issue=''
            self.log('Verified local Buttons[].InputDeviceHandlers; physical names, native state map and shared definitions remain untouched')
        except Exception as exc:
            self.issue=f'{type(exc).__name__}: {exc}'
            self.warn('binding_target',f'Binding target unavailable: {self.issue}')

    def remember_user_native(self):
        """Snapshot native preset metadata before replacing the live input table."""
        pi=self.pi() if self.pi is not None else None
        if pi is None: raise ValueError('PlayerInput unavailable for native-state snapshot')
        self.user_preset_index=int(pi.ControllerPresetIndex)
        self.user_rebindings=[copy.copy(r) for r in pi.ControllerRebindings]

    def note_external_native_change(self):
        if not self._forcing_native:self.native_change_pending=True

    def _force_factory_default(self):
        """Build a preset-neutral live table without changing the saved profile setting."""
        pc=self.pc() if self.pc is not None else None
        pi=self.pi() if self.pi is not None else None
        if pc is None or pi is None: raise ValueError('Player input unavailable for native override')
        # Fingerprint before the first engine mutation. We cannot safely call
        # SetupInputDevices to roll back a partially modified/foreign-owned table.
        if self._native_transition_pending:
            raise RuntimeError('Native preset transition still unresolved')
        before=(self.current(self.rows()),fingerprint(pi.LatentButtons),self.configuration())
        rebuilt=False
        self._native_transition_pending=True
        self._forcing_native=True
        try:
            # ApplyControllerPreset does not write profile setting 115; only the
            # exec SetControllerPreset wrapper does. Likewise assigning the live
            # rebinding array here does not write profile setting 158.
            if self.user_preset_index is None:
                raise ValueError('Saved native controller preset is unavailable')
            saved_index=self.user_preset_index
            saved_rebindings=[copy.copy(r) for r in self.user_rebindings]
            with unrealsdk.hooks.prevent_hooking_direct_calls():
                result=pc.ApplyControllerPreset(PresetName='Default')
                if result is False:
                    raise ValueError('BL2 rejected the Default controller preset')
                pi.ControllerRebindings=[]
                pi.SetupInputDevices()
                rebuilt=True
            # Keep BL2's selected preset/rebinding metadata truthful for its UI and
            # profile logic, but do not rebuild. The live Buttons table remains the
            # factory-Default table which Customizer owns.
            pi.ControllerPresetIndex=saved_index
            pi.ControllerRebindings=[copy.copy(r) for r in saved_rebindings]
        except Exception:
            # If no observable state changed, retry is safe. Otherwise retain
            # the owner snapshot and forbid a blind replacement of live input.
            try:
                unchanged=(self.current(self.rows())==before[0]
                           and fingerprint(pi.LatentButtons)==before[1]
                           and self.configuration()==before[2])
            except Exception:
                unchanged=False
            if unchanged:
                self._native_transition_pending=False
            else:
                self.paused=True
                self.warn('native_override_pending',
                          'Native controller preset rebuild stopped after a possible '
                          'partial change; preserving live input until a new PlayerInput')
                if self.input_rebuild_failed is not None:
                    try:
                        self.input_rebuild_failed(pc, pi)
                    except Exception as notify_exc:
                        self.warn('native_rebuild_failed_notify',
                                  f'Deadzone suspension unavailable: {notify_exc}')
            raise
        else:
            self._native_transition_pending=False
        finally:
            self._forcing_native=False
            if rebuilt and self.input_rebuilt is not None:
                try:self.input_rebuilt(pc,pi)
                except Exception as exc:self.warn('deadzone_rebuild_notify',f'Deadzone rebuild notification failed safely: {exc}')
        self.invalidate_rows()
        clear_schema_cache()

    def _capture_after_native_reset(self):
        try:
            self.capture()
        except Exception:
            # The reset succeeded, but we lack a validated baseline for
            # compare-before-restore. Preserve the owner and stop writes.
            self._native_transition_pending=True
            self.paused=True
            self.warn('native_override_capture',
                      'Native preset reset completed but input capture failed; '
                      'waiting for a fresh PlayerInput')
            raise

    def activate_native_override(self):
        """Adopt the user's native state, then replace only the live table with Default."""
        if self._native_transition_pending:
            raise RuntimeError('Native preset transition still unresolved')
        if self.override_active:return
        self.remember_user_native()
        self._force_factory_default()
        self.override_active=True
        self._capture_after_native_reset()
        self.log(f'Native controller preset isolated while Custom Bindings is active; saved preset index={self.user_preset_index}')

    def adopt_external_native_rebuild(self,user_change=None):
        """A native rebuild finished: reassert our clean layer without losing user intent."""
        if self._native_transition_pending:return False
        if self._forcing_native:return False
        if user_change is None:
            user_change=self.native_change_pending or self.configuration()!=self.config
        self.native_change_pending=False
        # SetupInputDevices has already replaced our live handler arrays. They are
        # no longer ours, so do not attempt ownership restoration against them.
        self.applied={};self.latent_applied=None
        self.restore_compat()
        self.invalidate_rows();clear_schema_cache()
        if user_change:
            # A real preset/rebinding change must become the state restored when
            # Custom Bindings turns off, even when the user selected Default and
            # the resulting config happens to equal our neutral config.
            self.override_active=False
            self.remember_user_native()
        self._force_factory_default()
        self.override_active=True
        self._capture_after_native_reset()
        self.compat_lost=False;self.compat_refresh=True
        self.stage_rebase()
        return True

    def restore_user_native(self,recapture=False):
        """Rebuild the user's selected native preset/rebindings and relinquish the live layer."""
        if self._native_transition_pending:
            self.warn('native_restore_pending',
                      'Native input restoration deferred: preset transition was incomplete')
            return False
        if not self.override_active:return True
        pi=self.pi() if self.pi is not None else None
        if pi is None:
            self.override_active=False
            self.user_preset_index=None;self.user_rebindings=[];self.native_change_pending=False
            return False
        # Restore only fields we own first. If another mod touched the live table,
        # do not call SetupInputDevices because that would overwrite its changes.
        self.restore_owned()
        rows=self.rows()
        if self.current(rows)!=self.base_fp or fingerprint(pi.LatentButtons)!=self.latent_fp:
            self.warn('native_restore_external','Native preset restore skipped because another mod edited the live input table')
            self.override_active=False
            return False
        if self.user_preset_index is None:
            raise ValueError('Saved native controller preset is unavailable')
        pi.ControllerPresetIndex=self.user_preset_index
        pi.ControllerRebindings=[copy.copy(r) for r in self.user_rebindings]
        rebuilt=False
        self._forcing_native=True
        try:
            # Rebuild fresh RemappedButton pointers instead of restoring copied
            # handler structs across SetupInputDevices reallocations.
            with unrealsdk.hooks.prevent_hooking_direct_calls():
                pi.SetupInputDevices()
                rebuilt=True
        finally:
            self._forcing_native=False
            if rebuilt and self.input_rebuilt is not None:
                try:self.input_rebuilt(self.pc() if self.pc is not None else None,pi)
                except Exception as exc:self.warn('deadzone_rebuild_notify',f'Deadzone rebuild notification failed safely: {exc}')
        self.override_active=False
        self.applied={};self.latent_applied=None;self.last_layout=None
        self.invalidate_rows();clear_schema_cache()
        self.user_preset_index=None;self.user_rebindings=[]
        self.native_change_pending=False
        # The native user mapping is now final. Consumers which cache physical
        # action keys must not retain a Customizer-era lookup across disable.
        self.notify_layout_committed(None,None,False)
        if recapture:
            self.capture()
        return True

    def capture(self):
        clear_schema_cache()
        rows=self.rows()
        device=unrealsdk.find_object('InputDeviceDefinition','GD_Input.Devices.InputDevice_Controller')
        native={}; baseline={}
        for physical,row in rows.items():
            sources=[]
            for handler in row.InputDeviceHandlers:
                if same(handler.SourceDevice,device):
                    index=int(handler.SourceIndex)
                    if not 0<=index<len(device.Buttons): raise ValueError('Invalid native source index')
                    logical=canonical(device.Buttons[index].KeyName)
                    if logical not in KEYS: raise ValueError('Live gameplay row includes a reserved native bundle')
                    sources.append(logical)
            if len(sources)!=1: raise ValueError(f'{SHORT[physical]} lacks exactly one native controller bundle')
            logical=sources[0]
            if logical in native: raise ValueError(f'Duplicate native bundle: {SHORT[logical]}')
            native[logical]=physical
            # Includes supplemental character/device handlers and remapped
            # press/tap/hold definitions, copied as complete native structs.
            baseline[logical]=[copy.copy(h) for h in row.InputDeviceHandlers]
        if set(native)!=set(KEYS): raise ValueError('Incomplete native action layout')
        self.restore_compat()
        self.native=native; self.baseline=baseline; self.base_fp=self.current(rows)
        self.base_latent=list(self.pi().LatentButtons)
        self.latent_fp=fingerprint(self.base_latent);self.latent_applied=None
        self.config=self.configuration()
        self.applied={}; self.quiet=0; self.paused=False; self.last_layout=None
        self._sync_pending=False;self._pending_keys=tuple(KEYS)
        self.generation+=1
        self.invalidate_layout()
        self.log(f'Live native layout captured: {[(SHORT[k],SHORT[native[k]]) for k in KEYS]}')

    def configuration(self):
        pi=self.pi()
        return (int(pi.ControllerPresetIndex),tuple((str(r.DefaultKeyName),str(r.RemappedKeyName)) for r in pi.ControllerRebindings))

    def idle(self,rows,keys=None):
        # Reserved Start/Back are outside our changed rows and remain native.
        selected=KEYS if keys is None else keys
        return all(int(rows[key].State)==self.released for key in selected)

    def changed_rows(self,rows,mapping,current=None):
        """Return only physical rows whose live handler bundle must change."""
        validate(mapping)
        if current is None:current=self.current(rows)
        wanted={physical:() for physical in KEYS}
        for logical in KEYS:
            physical=mapping[logical]
            if physical is not None:wanted[physical]=fingerprint(self.baseline[logical])
        return tuple(key for key in KEYS if current[key]!=wanted[key])

    def compatible(self,native_refresh=False):
        if native_refresh: self.compat_lost=False
        root=sys.modules.get('ControllerBinds')
        candidate=sys.modules.get('ControllerBinds.game_commands')
        states=sys.modules.get('ControllerBinds.game_states')
        hook=getattr(states,'ui_interaction_input_key',None)
        if hook is not None and hasattr(hook,'get_active_count'):
            active=hook.get_active_count()>0
        elif getattr(root,'mod',None) is not None:
            active=bool(root.mod.is_enabled)
        else:
            if root is not None or candidate is not None: return False,'Controller Binds is initializing'
            active=False
        if not active:
            self.restore_compat();self.compat=None;self.compat_keys={}
            return True,''
        table=getattr(candidate,'controller_commands',None)
        if not isinstance(table,Mapping): return False,'Controller Binds lookup is initializing'
        matched,missing=resolve_keys(table)
        if missing: return False,'Controller Binds missing: '+', '.join(SHORT[k] for k in missing)
        if any(not isinstance(table[matched[k]],Mapping) for k in KEYS): return False,'Invalid Controller Binds action lookup'
        if self.compat is not None and candidate is not self.compat:
            self.restore_compat()
        owned=tuple(dict.fromkeys(matched[k] for k in KEYS))
        if self.compat_applied is not None and any(table.get(key)!=self.compat_applied.get(key) for key in owned):
            # Controller Binds legitimately recreates its lookup during map/input
            # lifecycle rebuilds. If every row we owned is back at the exact
            # snapshot we captured before adapting it, ownership has simply ended
            # cleanly. Any third state remains a real conflict and is preserved.
            if (self.compat_original is not None
                    and all(table.get(key)==self.compat_original.get(key) for key in owned)):
                self.compat_original=self.compat_applied=None
                self.compat_lost=False
            else:
                self.compat_lost=True
                return False,'Controller Binds lookup edited externally; layouts paused'
        if self.compat_lost: return False,'Controller Binds lookup edited externally; layouts paused'
        self.compat=candidate;self.compat_keys=matched
        return True,''

    def adapt_compat(self,mapping):
        if self.compat is None: return
        table=self.compat.controller_commands
        owned=tuple(dict.fromkeys(self.compat_keys[k] for k in KEYS))
        if self.compat_original is None:
            self.compat_original={key:copy.deepcopy(table[key]) for key in owned}
        # Preserve every unrelated Controller Binds entry exactly as it is now.
        updated=dict(table)
        # An explicitly unused slot suppresses no gameplay commands. Every
        # native lookup key was verified before reaching this assignment.
        for physical in KEYS: updated[self.compat_keys[physical]]={}
        for logical in KEYS:
            if mapping[logical] is not None:
                updated[self.compat_keys[mapping[logical]]]=copy.deepcopy(self.compat_original[self.compat_keys[self.native[logical]]])
        self.compat_applied={key:copy.deepcopy(updated[key]) for key in owned}
        self.compat.controller_commands=updated

    def restore_compat(self):
        if self.compat is not None and self.compat_applied is not None:
            table=self.compat.controller_commands
            if all(table.get(key)==value for key,value in self.compat_applied.items()):
                updated=dict(table)
                for key,value in self.compat_original.items():updated[key]=copy.deepcopy(value)
                self.compat.controller_commands=updated
            elif (self.compat_original is not None
                    and all(table.get(key)==self.compat_original.get(key) for key in self.compat_applied)):
                # Expected Controller Binds/map rebuild: its own baseline is already
                # restored, so there is nothing for Customizer to write back.
                self.compat_lost=False
            else:
                self.compat_lost=True
                self.warn('compat_external','Controller Binds external edit preserved')
        self.compat_original=self.compat_applied=None

    def restore_owned(self):
        rows=self.rows()
        for physical,expected in tuple(self.applied.items()):
            row=rows[physical]
            if fingerprint(row.InputDeviceHandlers)==expected:
                logical=next(k for k in KEYS if self.native[k]==physical)
                row.InputDeviceHandlers=[copy.copy(h) for h in self.baseline[logical]]
            else: self.warn('binding_external','External live input handler edit preserved')
            self.applied.pop(physical,None)
        self.restore_compat();self.last_layout=None
        pi=self.pi() if self.pi is not None else None
        if pi is not None and self.latent_applied is not None:
            if fingerprint(pi.LatentButtons)==self.latent_applied:
                pi.LatentButtons=list(self.base_latent)
            else: self.warn('latent_external','External tap/hold registry edit preserved')
        self.latent_applied=None

    def apply(self,rows,mapping):
        validate(mapping)
        wanted={physical:[] for physical in KEYS}
        for logical in KEYS:
            if mapping[logical] is not None:
                wanted[mapping[logical]]=[copy.copy(h) for h in self.baseline[logical]]
        for physical,row in rows.items():
            desired=fingerprint(wanted[physical])
            current=fingerprint(row.InputDeviceHandlers)
            if current==desired: continue
            # Establish partial ownership before a field setter can fail.
            self.applied[physical]=desired
            row.InputDeviceHandlers=wanted[physical]
            if fingerprint(row.InputDeviceHandlers)!=desired:
                raise ValueError('Live handler assignment failed readback')
        # Changed rows which have returned to baseline no longer need ownership.
        self.applied={key:fp for key,fp in self.applied.items() if fp!=self.base_fp[key]}
        # Native SetupInputDevices builds this list of physical keys requiring
        # latent (tap/hold) matching. Move its gameplay names with their bundles;
        # retain every keyboard, mouse, reserved and duplicate entry unchanged.
        inverse={physical:logical for logical,physical in self.native.items()}
        latent=[]
        for name in self.base_latent:
            if canonical(name) not in KEYS: latent.append(name)
            else:
                target=mapping[inverse[canonical(name)]]
                if target is not None: latent.append(str(rows[target].ButtonName))
        desired_latent=fingerprint(latent)
        if fingerprint(self.pi().LatentButtons)!=desired_latent:
            self.latent_applied=desired_latent
            self.pi().LatentButtons=latent
            if fingerprint(self.pi().LatentButtons)!=desired_latent:
                raise ValueError('Tap/hold registry assignment failed readback')
        if desired_latent==self.latent_fp:self.latent_applied=None
        self.adapt_compat(mapping)

    def sync(self,enabled,context,choices,blocked,boundary=False):
        if self._native_transition_pending or not self.ready:return
        # This method is now event driven. A new request performs one complete
        # ownership pass, then a temporary PlayerInput POST hook supplies only
        # natural release boundaries until the transaction is safe to commit.
        request=(enabled,context,blocked,self.generation)
        activating=enabled and not self.last_enabled
        changed_request=request!=self._sync_request or choices is not self._sync_choices
        if changed_request or self.refresh:
            # Every apply/restore transaction gets its own two local boundaries.
            # Never inherit quiet state from a previous request.
            self.quiet=0
        if (not changed_request and not self.refresh and not self.retry):
            if not self._sync_pending:
                return
            if not boundary:
                return
            # While waiting, inspect only state for rows actually affected by the
            # transaction. Full fingerprints are repeated immediately before write.
            if not self.idle(self.rows(),self._pending_keys):
                self.quiet=0
                return
            self.quiet+=1
            if self.quiet<2:
                return
        rows=self.rows();current=self.current(rows)
        latent_current=fingerprint(self.pi().LatentButtons)
        self._sync_request=request
        self._sync_choices=choices
        if activating and self.paused:self.retry=True
        # Custom Bindings deliberately owns the *live* controller preset layer.
        # Capture the user's native state, neutralize the live table to BL2's
        # factory Default, and only then solve Customizer choices. This makes
        # explicit mod bindings independent of the game's selected preset.
        if enabled and not self.override_active and not self.paused and not self.retry:
            self.activate_native_override()
            request=(enabled,context,blocked,self.generation)
            self._sync_request=request
            self._sync_choices=choices
            self.last_enabled=enabled
            self.stage_rebase()
            return True
        if self.retry:
            keys=tuple(key for key in KEYS if key in self.applied) or tuple(KEYS)
            if self._pending_keys!=keys:self.quiet=0
            self._sync_pending=True;self._pending_keys=keys
            if not boundary:return
            self.quiet=self.quiet+1 if self.idle(rows,keys) else 0
            if self.quiet<2:return
            self.restore_owned();self.capture();self.compat_lost=False
            self.retry=False
            self.stage_rebase()
            return True
        if self.refresh:
            # Native rebuilds and map travel are two-stage operations. First adopt
            # the completely rebuilt native table. Only a later sync may solve and
            # apply the user's layout, after preference metadata has rebased to this
            # new generation. Never continue with choices prepared for the old base.
            expected={k:self.applied.get(k,self.base_fp[k]) for k in KEYS}
            latent_expected=self.latent_applied if self.latent_applied is not None else self.latent_fp
            config_changed=self.configuration()!=self.config
            rebased=(current!=expected or latent_current!=latent_expected or config_changed)
            if rebased:
                if self.override_active:
                    # Any rebuild of the derived button table must be neutralized
                    # again. If metadata changed too, adopt it as the user's new
                    # selected native state before reasserting the clean table.
                    self.applied={};self.latent_applied=None;self.restore_compat()
                    if config_changed:self.remember_user_native()
                    self._force_factory_default();self.capture();self.stage_rebase()
                else:
                    self.applied={};self.restore_compat();self.capture();self.stage_rebase()
                self.refresh=False
                self.compat_refresh=False
                return True
            self.refresh=False
            compatible,issue=self.compatible(native_refresh=self.compat_refresh)
            self.compat_refresh=False
        else: compatible,issue=self.compatible()
        self.last_enabled=enabled
        expected={k:self.applied.get(k,self.base_fp[k]) for k in KEYS}
        if (current!=expected or latent_current!=self.latent_fp) and not enabled and not self.applied and self.latent_applied is None:
            # With overrides Off, an independently changed complete native
            # layout can safely become the new baseline.
            self.capture();rows=self.rows();current=self.current(rows)
            expected={k:self.base_fp[k] for k in KEYS}
            latent_current=fingerprint(self.pi().LatentButtons)
        latent_expected=self.latent_applied if self.latent_applied is not None else self.latent_fp
        if (current!=expected or latent_current!=latent_expected) and not self.paused:
            self.paused=True;self.issue='Live input handlers edited externally; layouts paused until native rebuild or Off/On'
            self.warn('binding_external',self.issue)
        if not compatible:
            self.issue=issue
            if 'externally' in issue: self.paused=True
        elif not self.paused: self.issue=''
        if not enabled or blocked or self.paused or not compatible:
            pending=bool(self.applied or self.latent_applied is not None or self.compat_applied is not None
                or not enabled and self.override_active)
            keys=tuple(key for key in KEYS if key in self.applied) or tuple(KEYS)
            if pending and (not self._sync_pending or self._pending_keys!=keys):
                self.quiet=0
            self._sync_pending=pending;self._pending_keys=keys
            if self._sync_pending and self.quiet>=2:
                self.restore_owned()
                if not enabled and self.override_active:
                    self.restore_user_native(recapture=True)
                self._sync_pending=False
            return
        mapping=self.resolved_layout(context,choices[context])
        signature=(context,tuple(mapping[k] for k in KEYS))
        if mapping==self.native:
            pending=bool(self.applied or self.latent_applied is not None or self.compat_applied is not None)
            keys=tuple(key for key in KEYS if key in self.applied) or tuple(KEYS)
            if pending and (not self._sync_pending or self._pending_keys!=keys):self.quiet=0
            self._sync_pending=pending;self._pending_keys=keys
            if self._sync_pending and self.quiet>=2:
                self.restore_owned();self._sync_pending=False
                self.last_layout=signature
                self.notify_layout_committed(context,mapping,True)
            elif not pending and self.last_layout!=signature:
                # A generation/context can settle directly on native Default with
                # no rows to write. It is still a real effective-layout commit.
                self.last_layout=signature
                self.notify_layout_committed(context,mapping,True)
            return
        pending=signature!=self.last_layout or self.compat is not None and self.compat_applied is None
        keys=self.changed_rows(rows,mapping,current) or tuple(KEYS)
        if pending and (not self._sync_pending or self._pending_keys!=keys):self.quiet=0
        self._sync_pending=pending;self._pending_keys=keys
        if self._sync_pending and self.quiet>=2:
            # Re-read all owned fields after the two boundaries and immediately
            # before mutation. No periodic blind audit is needed in steady state.
            rows=self.rows();current=self.current(rows)
            expected={k:self.applied.get(k,self.base_fp[k]) for k in KEYS}
            latent_expected=self.latent_applied if self.latent_applied is not None else self.latent_fp
            if current!=expected or fingerprint(self.pi().LatentButtons)!=latent_expected:
                self.paused=True;self.issue='Live input handlers edited externally; layouts paused until native rebuild or Off/On'
                self.warn('binding_external',self.issue);self._sync_pending=False
                return
            self.apply(rows,mapping)
            self.last_layout=signature
            self._sync_pending=False
            # Notify only after handler + latent + Controller Binds adaptation has
            # committed successfully. Some mods cache the physical key returned
            # for an action and must discard that cache when our effective mapping
            # generation changes.
            self.notify_layout_committed(context,mapping,True)
            self.log(f'Applied live {context} layout: {[(SHORT[k],SHORT.get(mapping[k],"Unbound")) for k in KEYS if mapping[k]!=self.native[k]]}')
        return False

    def cancel_drain(self):
        if self.drain_hook is not None:
            path,kind,ident=self.drain_hook
            unrealsdk.hooks.remove_hook(path,kind,ident)
            # Keep the hook ledger if SDK removal reports success but retains
            # the ID. A manual retry can still remove our known registration.
            if unrealsdk.hooks.has_hook(path,kind,ident):
                raise RuntimeError('Binding cleanup hook removal not confirmed')
            self.drain_hook=None
        self.draining=False

    def detach(self):
        self.cancel_drain()
        if self._native_transition_pending:
            # Do not rebuild/restore a partially modified live table on unload.
            # A fresh PlayerInput is the only verified lifetime boundary.
            self.warn('native_detach_pending',
                      'Retiring incomplete native input transition without overwriting fields')
            self.restore_compat()
        elif self.pi is not None and self.pi() is not None:
            if self.override_active:self.restore_user_native(recapture=False)
            elif self.applied or self.latent_applied is not None:self.restore_owned()
            else:self.restore_compat()
        else:
            self.restore_compat();self.override_active=False
            self.user_preset_index=None;self.user_rebindings=[];self.native_change_pending=False
        self.ready=False;self.pc=self.pi=None;self.applied={}
        self._native_transition_pending=False
        self.override_active=False
        self.user_preset_index=None;self.user_rebindings=[];self.native_change_pending=False
        self.invalidate_rows()

    def disable(self):
        if self._native_transition_pending:
            self.restore_compat();self.cancel_drain();return
        if not self.applied and self.latent_applied is None and not self.override_active:
            self.restore_compat();self.cancel_drain()
            self.drain_failures=0;self.drain_stalled=False;self.drain_issue=''
            return
        if self.drain_hook is not None:return
        pi=self.pi() if self.pi is not None else None
        pc=self.get_pc(possibly_loading=True)
        if pi is None:
            self.applied={};self.latent_applied=None;self.restore_compat();self.override_active=False
            self.user_preset_index=None;self.user_rebindings=[];self.native_change_pending=False
            self.cancel_drain();return
        if pc is None or not same(pc.PlayerInput,pi):
            if self.override_active:self.restore_user_native(recapture=False)
            else:self.restore_owned()
            self.cancel_drain()
            self.drain_failures=0;self.drain_stalled=False;self.drain_issue=''
            return
        # Disabling while an input is held must not change its eventual release
        # action. This is a new restore transaction, so never reuse quiet state
        # from a previous apply. Keep one local cleanup hook for two boundaries.
        fn=pi.Class._find('PlayerInput');path=fn._path_name()
        ident='ControllerCustomizer:binding-drain';kind=Type.POST_UNCONDITIONAL
        if unrealsdk.hooks.has_hook(path,kind,ident): raise RuntimeError('Existing binding cleanup hook')
        self.quiet=0
        self.drain_failures=0
        self.drain_stalled=False
        self.drain_issue=('Binding restore pending: release held gameplay buttons '
                          'for two input boundaries. If no callbacks arrive, '
                          'use Retry Pending Restore or restart the game.')
        def drain(obj,args,ret,func):
            if self.draining or self.drain_stalled:return None
            self.draining=True
            try:
                old=self.pi() if self.pi is not None else None
                current=self.get_pc(possibly_loading=True)
                if old is None:
                    self.applied={};self.latent_applied=None;self.restore_compat()
                elif current is None or not same(current.PlayerInput,old):
                    if self.override_active:
                        if self.restore_user_native(recapture=False) is False and self.override_active:
                            raise RuntimeError('Native preset restore still pending')
                    else:self.restore_owned()
                else:
                    if not same(obj,old):return None
                    self.quiet=self.quiet+1 if self.idle(self.rows()) else 0
                    if self.quiet<2:return None
                    if self.override_active:
                        if self.restore_user_native(recapture=False) is False and self.override_active:
                            raise RuntimeError('Native preset restore still pending')
                    else:self.restore_owned()
                # A verified release or safe owner retirement is final.
                self.cancel_drain()
                self.drain_failures=0;self.drain_issue=''
                if self.issue.startswith('Binding cleanup'):self.issue=''
            except Exception as exc:
                # One reflected setter/getter can fail transiently. Keep all
                # outstanding compare-before-restore records and demand TWO
                # NEW quiet boundaries before retrying; never force held input.
                self.quiet=0
                self.drain_failures+=1
                if self.drain_failures>=3:
                    self.drain_stalled=True
                    self.drain_issue=('Binding cleanup paused after three failed attempts; '
                                      'ownership retained. Use Retry Pending Restore '
                                      'when buttons are released, or restart the game.')
                    try:self.cancel_drain()
                    except Exception as removal_exc:
                        self.warn('binding_drain_remove',
                                  f'Cleanup hook removal pending: {removal_exc}')
                else:
                    self.drain_issue=('Binding cleanup retry pending after error; '
                                      'release gameplay buttons for two new boundaries.')
                self.issue=self.drain_issue
                self.warn('binding_drain',
                          f'Binding cleanup attempt {self.drain_failures}/3: '
                          f'{type(exc).__name__}: {exc}. {self.drain_issue}')
            finally:self.draining=False
            return None
        try: unrealsdk.hooks.add_hook(path,kind,ident,drain)
        except Exception:
            if unrealsdk.hooks.has_hook(path,kind,ident): self.drain_hook=(path,kind,ident)
            raise
        if not unrealsdk.hooks.has_hook(path,kind,ident): raise RuntimeError('Binding cleanup hook failed')
        self.drain_hook=(path,kind,ident)
        self.log('Layout restoration waits for held gameplay buttons to release; Start/Back remain unchanged')
