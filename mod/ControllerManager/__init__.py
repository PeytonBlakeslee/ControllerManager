"""Crash-safe per-player handler layouts and verified controller look controls.

Saved native configuration, button names/state maps and shared definitions stay untouched.
When Custom Bindings is active, the live native preset layer is temporarily isolated.
"""
from __future__ import annotations

import math
import re
import sys
import copy
import json
import hashlib
import os
import tempfile
import time
from pathlib import Path
from typing import Any
import unrealsdk
from mods_base import BoolOption, ButtonOption, EInputEvent, Game, GroupedOption, HiddenOption, Mod, SpinnerOption, build_mod, get_pc
from unrealsdk import logging
from unrealsdk.hooks import Type, Block
from unrealsdk.unreal import WeakPointer
from .layout import KEYS, SHORT, FROM_SHORT, canonical, solve
from .binding_runtime import BindingRuntime
from . import layout as preferences
from .capture import ButtonCapture
from .sdk_actions import Assignments, discover, button_name, ShortcutDispatcher
from .sdk_capture import AssignmentDialog
from .side_panel import SidePanel, PanelRow
from .native_menu import NativeBindingView
from .menu_provider import ManagerOptionsProvider, adapt_provider
from .input_gate import ParentInputGate

PREFIX='Controller Manager'
IS_TPS=Game.get_current() == Game.TPS
_active=False
_hooks=[]
_ui_hooks=[]  # UI lifecycle hooks remain installed when gameplay mod is disabled.
_running=set()
_messages=0
_warned=set()
_pc=_pi=None
_loaded=_look_ready=_look_verified=False
_vehicle_fn=None
_vehicle_name=None
_vehicle_kwargs={}
_menus={}
_opening=0
_finished=_scaled=0
from .look_state import LookAccumulatorState
_look_saved=LookAccumulatorState()
_rotation_depth=0
_look_owner_depth=0
_look_reconfigure_pending=False
_syncing=_cleaning=_failing=False
_described_generation=-1
_loading_settings=False
_preference_generation=-1
_choices_cache=None
_log_seen=set()
_context_cache=None
_active_context=None
_pawn_classes={}
_menu_cleanup_due=0.0
_menu_cleanup_interval=1.0
_blocked_cache=None
_look_values=None
_look_pairs={}
_look_context_needed={'foot':False,'vehicle':False}
_look_needed=False
_restart_rebind_armed=False
_restart_rebind_ready=0
_restart_rebind_started=0.0
_zoom_schemas={}
_deadzone_provider=None
_travel_resume_armed=False
_travel_resume_pending=False
_travel_resume_running=False
_sdk_store=Assignments()
_sdk_rows={}
_sdk_dialog=None
_sdk_reset=None
_side_panel=SidePanel()
_native_view=NativeBindingView(lambda message: _warn("panel_flash", message))
_sdk_dispatch=ShortcutDispatcher(lambda key, message: _warn(key, message))
_sdk_input_owner=None
_panel_last_repeat=0.0
_native_imported=False
_display_revision=0
_display_cache=None
_panel_value_stamp=None
_panel_values_revision=0
_panel_updating=False
_panel_unbinding=False
_panel_modal_return=None
DEFAULT_LABELS=dict(zip(KEYS,('Jump','Crouch','Reload / Use','Weapon Swap','Action Skill','Grenade',
    'Aim','Fire','Sprint','Melee','Weapon 1','Weapon 2','Weapon 3','Weapon 4')))
# These are action bundles carried by the ORIGINAL physical buttons under BL2's
# factory-default DrivingVehicle context, not a second copy of Walking labels.
# The driver actions below are corroborated by GD_Input.Contexts.
# InputContext_DrivingVehicle and the official BL2 360 controller manual.
# Y/D-pad are retained as configurable physical rows for native parity, but
# their actions are context-dependent and must not be advertised as driver moves.
DRIVING_LABELS = dict(zip(KEYS, (
    'Switch Seats', 'Exit Vehicle', 'Use / Enter Seat', 'Y (Other)',
    'Vehicle Boost', 'Vehicle Handbrake', 'Weapon Alt-Fire', 'Fire Vehicle Weapon',
    'Vehicle Horn', 'Rear View', 'D-pad Up (Other)', 'D-pad Down (Other)',
    'D-pad Left (Other)', 'D-pad Right (Other)')))
TPS_DRIVING_LABELS = dict(zip(KEYS, (
    'Switch Seats (Moon Buggy)', 'Exit Vehicle', 'Enter Vehicle Seat', 'Y (Other)',
    'Boost / Hover Jump', 'Handbrake / Stingray Slam', 'Secondary Fire', 'Primary Fire',
    'Vehicle Horn', 'Rear View', 'D-pad Up (Other)', 'D-pad Down (Other)',
    'D-pad Left (Other)', 'D-pad Right (Other)')))
# Show known vehicle actions before auxiliary physical keys. This is display-only:
# no saved action identities, native handler bundles, or default layouts change.
DRIVING_ACTION_ORDER = (*KEYS[:3], *KEYS[4:10], KEYS[3], *KEYS[10:])


def _context_action_label(context, key):
    return (TPS_DRIVING_LABELS if IS_TPS else DRIVING_LABELS)[key] if context == 'vehicle' else DEFAULT_LABELS[key]


def _visible_native_binding(context, key):
    """Display saved intent; activation status belongs in the description."""
    return _display_snapshot()['values'][context][key]


def _invalidate_display():
    global _display_revision,_display_cache
    _display_revision+=1
    _display_cache=None


def _display_stamp():
    return (_display_revision,_bindings.generation,_bindings.ready,
            _bindings.override_active,_bindings.refresh)


def _display_snapshot():
    global _display_cache
    stamp=_display_stamp()
    if _display_cache is not None and _display_cache[0]==stamp:return _display_cache[1]
    try:
        metadata=preferences.state(runtime_state.value)
        baseline=_configuration_baseline(read_only=True)
        # The factory-button identities are a DISPLAY preview only. Runtime
        # edits still require a reflected/cached verified baseline in _edit.
        preview_baseline=baseline or dict(zip(KEYS,KEYS))
        choices,_=preferences.finalize(_choices(),metadata,preview_baseline)
        values={context:{key:'Unbound' if physical is None else SHORT[physical]
                        for key,physical in solve(preview_baseline,choices[context]).items()}
                for context in BINDINGS}
    except (ValueError,TypeError,KeyError):
        metadata={'pending':[]};baseline=None
        values={context:{key:str(option.value) for key,option in group.items()}
                for context,group in BINDINGS.items()}
    snapshot={'values':values,'metadata':metadata,'baseline':baseline}
    _display_cache=(stamp,snapshot)
    return snapshot



def _log(message: str) -> bool:
    global _messages
    if _messages>=8 or message in _log_seen:return False
    _log_seen.add(message)
    _messages+=1
    logging.info(f'{PREFIX}: {message}')
    return True


def _warn(key: str, message: str) -> None:
    if key not in _warned:
        _warned.add(key)
        logging.warning(f'{PREFIX}: {message}')


from .object_utils import same_native as _same

def _prop(obj: Any,name: str,kind: str) -> Any:
    prop = obj.Class._find_prop(name)
    if str(prop.Class.Name)!=kind or int(prop.ArrayDim)!=1:
        raise ValueError(f'{obj.Class.Name}.{name}: expected scalar {kind}')
    return prop

def _signature(fn: Any) -> list[Any]:
    return [p for p in fn._properties() if int(p.PropertyFlags)&0x80 and not int(p.PropertyFlags)&0x400]

def _function(obj: Any,name: str) -> Any:
    fn = obj.Class._find(name)
    if fn is None or str(fn.Class.Name)!='Function':
        raise ValueError(f'{obj.Class.Name}.{name} is not a reflected function')
    return fn

def _restore_look() -> None:
    global _look_owner_depth
    _look_saved.restore()
    _look_owner_depth=0

def _clear_context():
    global _context_cache,_blocked_cache,_active_context
    _context_cache=None
    _blocked_cache=None
    _active_context=None


def _context_key(pc):
    pawn=pc.Pawn
    return (pc._get_address(),pawn._get_address() if pawn is not None else None)


def _context(pc: Any,force: bool=False) -> str:
    """Return cached walking/vehicle context, refreshing on lifecycle events."""
    global _context_cache,_active_context
    key=_context_key(pc)
    if not force and _context_cache is not None and _context_cache[0]==key:
        _active_context=_context_cache[1]
        return _active_context
    value=_context_raw(pc)
    _context_cache=(key,value)
    _active_context=value
    return value


def _context_raw(pc: Any) -> str:
    pawn=pc.Pawn
    if pawn is None:
        return 'foot'
    cls=pawn.Class;address=cls._get_address()
    cached=_pawn_classes.get(address)
    if cached is None or cached[0]() is None:
        vehicle=any('Vehicle' in str(parent.Name) for parent in cls._superfields())
        try:_prop(pawn,'DrivenVehicle','ObjectProperty');driven=True
        except (AttributeError,ValueError):driven=False
        if len(_pawn_classes)>=32:_pawn_classes.clear()
        cached=(WeakPointer(cls),vehicle,driven);_pawn_classes[address]=cached
    if cached[1]:
        return 'vehicle'
    if cached[2] and pawn.DrivenVehicle is not None:return 'vehicle'
    if _vehicle_fn is not None:
        with unrealsdk.hooks.prevent_hooking_direct_calls():
            if bool(getattr(pc,_vehicle_name)(**_vehicle_kwargs)): return 'vehicle'
    return 'foot'

def _capturing(movie: Any) -> bool:
    name=str(movie.Class.Name).lower()
    if any(s in name for s in ('frontend','pause','option','bank','stash','storage',
            'customization','vending','vehiclespawn','dialog','inventory','statusmenu','training')):
        return True
    try:
        _prop(movie,'bCaptureInput','BoolProperty')
        return bool(movie.bCaptureInput)
    except (AttributeError,ValueError):
        # Conservative fallback for known menu classes, never modify a movie.
        return False

def _ui_blocked() -> bool:
    """Whether a live GFx/UI movie is currently capturing controller input."""
    global _menu_cleanup_due
    if _opening:return True
    if _menus:
        now=time.monotonic()
        if now>=_menu_cleanup_due:
            for address,pointer in tuple(_menus.items()):
                if pointer() is None:_menus.pop(address,None)
            _menu_cleanup_due=now+_menu_cleanup_interval
        if _menus:return True
    return False


def _bindings_blocked() -> bool:
    """Only input-capturing UI should temporarily yield custom button handlers."""
    # Cinematic/ignored-look/loading state is intentionally excluded here. BL2
    # can clear those flags natively after spawn/travel without an observable SDK
    # event. The custom layout can safely remain pending/applied through that
    # handoff because gameplay button handlers are not consumed by the loading
    # block itself.
    return _ui_blocked()


def _blocked(pc: Any,pawn: Any=None) -> bool:
    # Sensitivity is stricter: never touch look axes while UI, cinematic, pause,
    # or IgnoreLookInput is active. The caller may pass the already-read Pawn so
    # the hot rotation path never reflects it twice in one invocation.
    if _ui_blocked():return True
    if pawn is None:return True
    if _blocked_cache is None:_refresh_blocked(pc)
    return _blocked_cache


def _refresh_blocked(pc: Any) -> bool:
    global _blocked_cache
    _blocked_cache=(bool(getattr(pc,'bCinematicMode',False))
        or bool(getattr(pc,'bIgnoreLookInput',False))
        or getattr(pc.WorldInfo,'Pauser',None) is not None)
    return _blocked_cache

def _cached_pc():
    pc=_pc() if _pc is not None else None
    return pc if pc is not None else get_pc(possibly_loading=True)

def _resolve_deadzone_provider():
    """Resolve either known install/module name without importing another mod."""
    global _deadzone_provider
    for name in ('ControllerDeadzone',):
        candidate=sys.modules.get(name)
        if candidate is not None and getattr(candidate,'mod',None) is not None:
            _deadzone_provider=candidate
            return candidate
    _deadzone_provider=None
    return None


def _notify_deadzone_input_rebuilt(pc: Any | None, pi: Any | None) -> None:
    """Bridge native input rebuilds which intentionally suppress direct-call hooks."""
    provider=_deadzone_provider or _resolve_deadzone_provider()
    if provider is None or not bool(getattr(getattr(provider,'mod',None),'is_enabled',False)):
        return
    notify=getattr(provider,'notify_input_rebuilt',None)
    if callable(notify):
        notify(pc,pi)

def _notify_deadzone_input_rebuild_failed(pc: Any | None, pi: Any | None) -> None:
    """Quarantine Deadzone on incomplete native rebuild, never call success reconciliation."""
    provider=_deadzone_provider or _resolve_deadzone_provider()
    if provider is None or not bool(getattr(getattr(provider,'mod',None),'is_enabled',False)):
        return
    suspend=getattr(provider,'notify_input_rebuild_failed',None)
    if callable(suspend):
        if suspend(pc,pi) is False:
            _warn('deadzone_rebuild_failed', 'Deadzone could not suspend a partial native rebuild')
    else:
        _warn('deadzone_rebuild_failed', 'Installed Deadzone lacks partial-rebuild suspension; restart game after failure')


def _arm_travel_resume_pulse() -> None:
    """Arm one nonvisual pause/menu lifecycle pulse after a travel layout commit.

    Pause/unpause is the one recovery path which consistently repairs the stuck
    controller dispatch after vehicle -> non-vehicle travel. Rather than opening
    a real menu, the first managed controller press after the final post-travel
    binding commit replays only the pause/menu state transitions which normally
    occur around closing that menu. Duplicate lifecycle callbacks are coalesced
    while an existing pulse is already armed or waiting for the first press.
    """
    global _travel_resume_armed
    if _travel_resume_armed or _travel_resume_pending or _travel_resume_running:return
    _travel_resume_armed=True


def _notify_binding_consumers(context: str | None,mapping: dict[str,str | None] | None,enabled: bool) -> None:
    """Convert an armed travel repair into a first-input recovery pulse."""
    global _travel_resume_armed,_travel_resume_pending
    if not enabled:
        _travel_resume_armed=False
        _travel_resume_pending=False
        return
    if not _travel_resume_armed:return
    _travel_resume_armed=False
    _travel_resume_pending=True


def _apply_travel_resume_pulse(pc: Any) -> None:
    """Replay pause/menu close state without opening or rendering a menu.

    This is deliberately a bounded compatibility workaround. A real pause/unpause
    reliably repairs the broken controller route, while ReleasePressedButtons,
    forced contexts, logical ActionSkill resets, handler recapture and a native
    SetupInputDevices rebuild did not. The pulse therefore reproduces the small
    engine/mod lifecycle which is unique to pause recovery, synchronously, before
    the user's first post-travel button edge is processed.
    """
    global _travel_resume_pending,_travel_resume_running
    if not _travel_resume_pending or _travel_resume_running:return
    _travel_resume_pending=False
    _travel_resume_running=True
    failures=[]
    try:
        # Recreate the native pause-menu state transition without spawning GFx.
        # Always force the closing half in finally so an exception cannot strand
        # the controller in a synthetic pause-open state.
        try:
            pc.SetGFxPauseMenuOpenState(True)
        except Exception as exc:
            failures.append(f'pause-open:{type(exc).__name__}')
        finally:
            try:
                pc.SetGFxPauseMenuOpenState(False)
            except Exception as exc:
                failures.append(f'pause-close:{type(exc).__name__}')

        # Reproduce the generic input-menu lifecycle without spawning GFx.
        # WillowPlayerController.GFxMenuOpened increments the replicated
        # bGFxMenuOpen counter; GFxMenuClosed decrements it. Never emit an
        # unmatched Close if an SDK hook raises before Open takes effect.
        def observed_menu_count():
            try:
                return int(pc.PlayerReplicationInfo.bGFxMenuOpen)
            except Exception:
                return None

        count_before=observed_menu_count()
        open_completed=False
        try:
            pc.GFxMenuOpened()
            open_completed=True
        except Exception as exc:
            failures.append(f'gfx-open:{type(exc).__name__}')
        finally:
            # A post-Open hook may raise after native/script state changed.
            # Readback is useful in that case, but is not assumed to be
            # synchronous on every co-op client.
            count_after=observed_menu_count() if not open_completed else None
            increment_seen=(count_before is not None and count_after is not None
                            and count_after>count_before)
            if open_completed or increment_seen:
                try:
                    pc.GFxMenuClosed()
                except Exception as exc:
                    # One attempted close only: the method can decrement the
                    # counter before raising; a retry could decrement twice.
                    failures.append(f'gfx-close:{type(exc).__name__}')
            else:
                # An unobserved Open exception is ambiguous. Never guess and
                # risk decrementing an unrelated real menu's counter.
                failures.append('gfx-open-unverified; no synthetic close issued')

        # Finish the same focus/pause reconciliation performed by
        # WillowGFxUIManager.Movie_OnClosed. These are best-effort because private
        # script exposure can vary between SDK builds.
        ui=getattr(pc,'GFxUIManager',None)
        if ui is not None:
            try:ui.UpdateFocus()
            except Exception as exc:failures.append(f'focus:{type(exc).__name__}')
            try:ui.UpdatePause()
            except Exception as exc:failures.append(f'pause-update:{type(exc).__name__}')

        if failures:
            _warn('travel_resume_partial','Post-travel controller resume pulse was partial: '+', '.join(failures))
    finally:
        _travel_resume_running=False

def _look_overrides():
    global _look_values,_look_pairs,_look_context_needed,_look_needed
    if _look_values is None:
        values=tuple(option.value for option in SENSITIVITY.values())
        factors={name:_factor(option.value) for name,option in SENSITIVITY.items()}
        if any(not math.isfinite(factor) or not 0.0<=factor<=5.0 for factor in factors.values()):
            _warn('invalid_multiplier','Invalid sensitivity selection; invalid values will be treated as 1.00x')
            factors={name:(factor if math.isfinite(factor) and 0.0<=factor<=5.0 else 1.0) for name,factor in factors.items()}
        _look_pairs={prefix:(factors[f'{prefix}_x'],factors[f'{prefix}_y']) for prefix in ('hip','ads','vehicle')}
        _look_context_needed={
            'foot':_look_pairs['hip']!=(1.0,1.0) or _look_pairs['ads']!=(1.0,1.0),
            'vehicle':_look_pairs['vehicle']!=(1.0,1.0),
        }
        _look_needed=any(_look_context_needed.values())
        _look_values=values
    return _look_needed

def _context_needs_look(context: str) -> bool:
    _look_overrides()
    return bool(_look_context_needed.get(context,False))

def _ads_zoomed(pawn: Any) -> bool | None:
    """Return settled ADS state, or None when the weapon schema is unverified."""
    try:
        weapon=getattr(pawn,'Weapon',None)
        if weapon is None:return False
        cls=weapon.Class;address=cls._get_address()
        schema=_zoom_schemas.get(address)
        if schema is None or schema[0]() is None:
            enum=cls._find_prop('ZoomState').Enum._as_py()
            names=[name for name in enum.__members__ if re.sub(r'[^a-z]','',name.lower()).endswith(('zoomed','zoomedin'))
                   and not re.search(r'unzoom|not.?zoom|zoom.*out',name,re.I)]
            if len(names)!=1:raise ValueError(f'unknown settled ADS enum {list(enum.__members__)}')
            if len(_zoom_schemas)>=32:_zoom_schemas.clear()
            schema=(WeakPointer(cls),names[0],int(enum.__members__[names[0]]))
            _zoom_schemas[address]=schema
        return int(weapon.ZoomState)==schema[2]
    except (AttributeError,ValueError):
        _warn('zoom_unknown','ADS state unverified; ADS look controls omitted')
        return None

def _look(pc: Any,pi: Any,pawn: Any,blocked: bool) -> None:
    global _scaled,_look_verified,_look_owner_depth
    if _finished==0 or _scaled==_finished:return
    if not _look_verified:
        _look_verified=True
    _scaled=_finished
    device='controller' if bool(getattr(pi,'bUsingGamepad',False)) else 'kbm'
    deadzone=_deadzone_provider
    if deadzone is not None and bool(getattr(getattr(deadzone,'mod',None),'is_enabled',False)):
        provider_device=getattr(deadzone,'active_device',None)
        if provider_device not in ('controller','kbm'):
            provider_device=getattr(deadzone,'_active_device',None)
        if provider_device in ('controller','kbm'):
            device=provider_device
    if device!='controller' or blocked:return
    context=_active_context if _active_context in ('foot','vehicle') else _context(pc)
    if context=='vehicle':
        factors=_look_pairs['vehicle']
    else:
        hip=_look_pairs['hip'];ads=_look_pairs['ads']
        if hip==ads:
            factors=hip
        else:
            zoom=_ads_zoomed(pawn)
            if zoom is None:
                # Preserve the previous fail-safe: when ADS state cannot be proven,
                # never guess an ADS override. Hip-only settings remain usable.
                if ads!=(1.0,1.0):return
                zoom=False
            factors=ads if zoom else hip
    if factors==(1.0,1.0):return
    applied=False
    for factor,field in ((factors[0],'aTurn'),(factors[1],'aLookUp')):
        if factor!=1.0:
            value=float(getattr(pi,field))
            if math.isfinite(value):
                _look_saved.save(field,_pi,value,value*factor)
                setattr(pi,field,value*factor)
                applied=True
    if applied:_look_owner_depth=_rotation_depth

SENSITIVITY_CHOICES=[f'{step/20.0:.2f}' for step in range(101)]


def _factor(value: Any) -> float:
    return float(value)


class SensitivityOption(SpinnerOption):
    """Decimal sensitivity selector which persists the historical float format."""
    def _to_json(self) -> float:
        return round(float(self.value),2)

    def _from_json(self,value: Any) -> None:
        # Existing builds persist real multipliers (for example 1.25), so the
        # decimal UI needs no settings-format migration. Quantize only to the
        # supported 0.05 steps and display a consistent two-decimal value.
        try:
            parsed=float(str(value).removesuffix('x'))
        except (ValueError,TypeError):
            return
        if not math.isfinite(parsed):
            return
        parsed=min(5.0,max(0.0,parsed))
        self.value=f'{round(parsed*20.0)/20.0:.2f}'


class BindingOption(ButtonOption):
    def __init__(self,*args,value='Default',**kwargs):
        super().__init__(*args,**kwargs)
        self.value=value

    def _to_json(self): return self.value

    def _from_json(self,value):
        if value in preferences.VALUES: self.value=value
        else: _warn('invalid_binding','Unsupported saved binding retained as Default; no unsupported input will be applied')


class ControllerManagerMod(Mod):
    def load_settings(self):
        global mod,_loading_settings,_choices_cache,_preference_generation,_look_values
        global _sdk_store,_native_imported
        mod=self;_loading_settings=True;_look_values=None
        _choices_cache=None;_preference_generation=-1
        _invalidate_display()
        imported=False
        try:
            # Import ONCE to a separate file without modifying/deleting the known-good original.
            original=None
            if self.settings_file is not None and not Path(self.settings_file).exists():
                source=Path(self.settings_file).parent/'2ControllerCustomizer.json'
                if source.is_file():
                    try:
                        with source.open(encoding='utf-8') as file: original=json.load(file)
                        native=original.get('options',{})
                        if not all(group in native for group in ('foot','vehicle','capabilities')):
                            raise ValueError('Native settings are incomplete')
                    except (OSError, ValueError, TypeError, AttributeError) as exc:
                        original=None
                        _warn('native_import',f'Existing native settings not imported: {exc}')
            super().load_settings()
            if original is not None:
                from mods_base.settings import load_options_dict
                load_options_dict(self.options,original['options'])
                imported=True
            try:
                loaded=Assignments(sdk_assignments.value)
            except (ValueError,TypeError) as exc:
                # A malformed top-level document must not erase an active valid
                # configuration. The original file remains untouched until a
                # subsequent explicit save/edit.
                _warn('sdk_settings',f'Invalid shortcut settings; retaining prior valid bindings: {exc}')
            else:
                _sdk_store=loaded
                if loaded.rejected:
                    _warn('sdk_settings',f'Ignored {loaded.rejected} invalid SDK shortcuts; other bindings retained')
        finally:_loading_settings=False
        _binding_descriptions()
        # SDK auto-enable runs before the saved Assignments store is read.
        if _active:_sdk_refresh()
        if imported:
            _native_imported=True
            self.save_settings()
            _log('Imported original Customizer settings into SEPARATE manager file; original left intact')

    def iter_display_options(self):
        """Keep the controller menus available without gameplay input hooks."""
        _binding_descriptions()
        yield layouts_enabled
        yield auto_swap
        sections={}
        for identifier,title in (('foot','Walking'),('vehicle','Driving')):
            native_rows=[PanelRow(
                identifier='reset',kind='reset',
                label='Reset '+title+' Settings',
                read=lambda:'',
                invoke=lambda context=identifier:_confirm_reset(context),
                help='Reset this layout and its sensitivity settings. Confirmation required.')]
            # Native Flash rows are all selectable. Never add spacer rows: a
            # blank row would become an invisible controller focus target.
            keys=('hip_x','hip_y','ads_x','ads_y') if identifier=='foot' else ('vehicle_x','vehicle_y')
            for sens_key in keys:
                option=SENSITIVITY[sens_key]
                native_rows.append(PanelRow(
                    identifier=sens_key,kind='sensitivity',
                    label=option.display_name,
                    read=lambda opt=option:str(opt.value),
                    adjust=lambda direction,opt=option:_panel_adjust_sensitivity(opt,direction),
                    help='D-pad Left/Right: Change sensitivity immediately  |  B: Categories'))
            order=DRIVING_ACTION_ORDER if identifier=='vehicle' else KEYS
            for action_key in order:
                option=BINDINGS[identifier][action_key]
                native_rows.append(PanelRow(
                    identifier=action_key,kind='binding',
                    label=_context_action_label(identifier, action_key),
                    read=lambda context=identifier,key=action_key:_visible_native_binding(context,key),
                    invoke=lambda context=identifier,key=action_key:_start_capture(context,key),
                    clear=lambda context=identifier,key=action_key:_panel_change_native(context,key,'Unbound'),
                    restore=lambda context=identifier,key=action_key:_panel_change_native(context,key,'Default'),
                    help='A: Change  |  X: Unbind  |  Y: Default'))
            sections[identifier]=(title,native_rows)
            yield _panel_category(identifier,title,'Edit '+title+' controls')

        _sdk_rows.clear()
        try:
            from mods_base import get_ordered_mod_list
            actions,duplicates=discover(get_ordered_mod_list(),self)
        except Exception as exc:
            _warn('sdk_discovery',f'SDK action discovery unavailable: {exc}')
            actions=();duplicates=()
        if duplicates:
            _warn('sdk_duplicates','Duplicate SDK action identities skipped: '+', '.join(duplicates[:3]))
        grouped={}
        global _sdk_actions_cache
        _sdk_actions_cache=actions
        _sdk_dispatch.configure(actions,_sdk_store)
        for action in actions:
            grouped.setdefault(action.key.split('#',1)[0],[]).append(action)
        sdk_options=[]
        for group_id,group in grouped.items():
            label=group[0].mod_label
            rows=[PanelRow(
                identifier='reset',kind='reset',
                label='Reset '+label+' Bindings',
                read=lambda:'',
                invoke=lambda group_id=group_id:_confirm_reset('sdk:'+group_id),
                help="Clear only this mod's controller shortcuts. Confirmation required."),
                ]
            for action in group:
                rows.append(PanelRow(
                    identifier=action.key,kind='shortcut',
                    label=action.label,
                    read=lambda key=action.key:(_sdk_store.for_key(key).describe() if _sdk_store.for_key(key) else 'None'),
                    invoke=lambda item=action:_start_sdk_capture(item),
                    clear=lambda key=action.key:_commit_sdk_binding(key,None),
                    help='A: Change  |  X: Unbind'+(' | '+action.description[:95] if action.description else '')))
            section_id='sdk_'+hashlib.sha1(group_id.encode('utf-8')).hexdigest()[:16]
            sections[section_id]=(label,rows)
            sdk_options.append(_panel_category(section_id,_short_mod_label(label),
                'Select SDK action bindings for '+label))
        _side_panel.set_sections(sections)
        if sdk_options:
            yield GroupedOption(identifier='sdk_action_sections',display_name='Mod Shortcuts',
                description='Mod controller shortcuts, separate from native layouts.',children=sdk_options)
        yield ButtonOption(identifier='master_reset',display_name='Reset All Controller Settings',
            description='Reset both Walking and Driving layouts, sensitivity, and all SDK controller shortcuts. Requires confirmation. Keyboard bindings are never changed.',
            on_press=lambda option:_confirm_reset('all'))

    def save_settings(self):
        """One atomic SDK JSON replacement for native and SDK action preferences."""
        from mods_base.settings import create_options_dict
        if self.settings_file is None or _loading_settings:return
        path=Path(self.settings_file)
        data={'options':create_options_dict(self.options)}
        if self.auto_enable:data['enabled']=bool(self.is_enabled)
        temporary=None
        try:
            with tempfile.NamedTemporaryFile(mode='w',encoding='utf-8',dir=path.parent,
                    prefix='.'+path.name+'.',suffix='.tmp',delete=False) as handle:
                temporary=Path(handle.name)
                json.dump(data,handle,indent=4);handle.write('\n');handle.flush();os.fsync(handle.fileno())
            os.replace(temporary,path);temporary=None
        finally:
            if temporary is not None:temporary.unlink(missing_ok=True)


def _save_settings():
    if not _loading_settings and 'mod' in globals() and hasattr(mod,'save_settings'):mod.save_settings()


def _sensitivity_changed(option,value):
    global _look_values,_panel_values_revision
    _look_values=None
    if _loading_settings:return
    before=option.value
    parsed=min(5.0,max(0.0,float(value)))
    value=f'{round(parsed*20.0)/20.0:.2f}'
    object.__setattr__(option,'value',value)
    try:
        _save_settings()
        _panel_values_revision+=1
        if _active:_configure_look_hooks(_cached_pc())
    except Exception:
        object.__setattr__(option,'value',before)
        _look_values=None
        if _active:
            try:_configure_look_hooks(_cached_pc())
            except Exception:pass
        raise


def _layouts_changed(option,value):
    global _restart_rebind_armed,_restart_rebind_ready,_restart_rebind_started
    global _travel_resume_armed,_travel_resume_pending,_travel_resume_running
    if _loading_settings:return
    before=option.value
    object.__setattr__(option,'value',bool(value))
    try:
        _save_settings()
        if _active:
            if not bool(value):
                _restart_rebind_armed=False;_restart_rebind_ready=0;_restart_rebind_started=0.0
                _travel_resume_armed=False;_travel_resume_pending=False;_travel_resume_running=False
                _configure_restart_recovery()
            _configure_physical_input_hook()
            _request_binding_sync()
    except Exception:
        object.__setattr__(option,'value',before)
        if _active:
            try:_request_binding_sync()
            except Exception:pass
        raise


def _runtime_choices():
    global _choices_cache
    # _store, load_settings and enable invalidate every supported edit path.
    if _choices_cache is None:
        _choices_cache=_choices()
        _bindings.invalidate_layout()
    return _choices_cache


def _install(path: str,kind: Any,tag: str,callback: Any) -> None:
    identifier=f'ControllerManager:{tag}'
    entry=(path,kind,identifier)
    if entry in _hooks:
        if unrealsdk.hooks.has_hook(*entry): return
        _hooks.remove(entry)  # A stale ledger is not a live callback.
    if unrealsdk.hooks.has_hook(path,kind,identifier):
        raise RuntimeError(f'hook identifier already exists: {path} ({tag})')
    def guarded(obj,args,ret,func):
        if not _active or tag in _running: return None
        _running.add(tag)
        try: return callback(obj,args,ret,func)
        except Exception as exc: _failure(exc)
        finally:
            _running.discard(tag)
    try: added=unrealsdk.hooks.add_hook(path,kind,identifier,
        guarded)
    except Exception:
        if unrealsdk.hooks.has_hook(path,kind,identifier): _hooks.append((path,kind,identifier))
        raise
    # UnrealSDK 3.2.0 can return False for a newly appended callback on a
    # nonempty function/stage hook list. Preflight absence plus postflight
    # presence is the usable registration receipt on that SDK revision.
    try:
        live=unrealsdk.hooks.has_hook(path,kind,identifier)
    except Exception:
        # A successful add with an unavailable verification still owns cleanup.
        _hooks.append((path,kind,identifier))
        raise
    if not live:
        raise RuntimeError(f'hook registration failed: {path} ({tag})')
    _hooks.append((path,kind,identifier))


def _has_tag(tag: str) -> bool:
    identifier=f'ControllerManager:{tag}'
    return any(existing==identifier for _,_,existing in _hooks)


def _remove_tags(*tags: str) -> None:
    identifiers={f'ControllerManager:{tag}' for tag in tags}
    for path,kind,identifier in tuple(_hooks):
        if identifier not in identifiers:continue
        unrealsdk.hooks.remove_hook(path,kind,identifier)
        if unrealsdk.hooks.has_hook(path,kind,identifier):
            raise RuntimeError(f'hook removal unverified: {identifier}')
        _hooks.remove((path,kind,identifier))
        _running.discard(identifier.removeprefix('ControllerManager:'))


def _configure_physical_input_hook() -> None:
    """One physical-input boundary for SDK shortcuts and native edge recovery."""
    if not _active:
        _remove_tags('physical-input')
        return
    fn=unrealsdk.find_object('Function','WillowGame.WillowGameViewportClient:InputKey')
    params={str(p.Name).casefold():str(p.Class.Name) for p in _signature(fn)}
    required={'controllerid':'IntProperty','key':'NameProperty','eventtype':'ByteProperty'}
    if any(params.get(name)!=kind for name,kind in required.items()):
        raise ValueError(f'WillowGameViewportClient.InputKey signature changed: {params}')
    _install(fn._path_name(),Type.PRE,'physical-input',_physical_input)


def _physical_input(obj: Any,args: Any,ret: Any,func: Any) -> Any:
    consumed=_sdk_input(obj,args,ret,func)
    if consumed is not None:return consumed
    if bool(layouts_enabled.value):_button_input_sanity(obj,args,ret,func)
    return None


def _button_input_sanity(obj: Any,args: Any,ret: Any,func: Any) -> None:
    """Repair an impossible stale native state immediately before a fresh press.

    InputKey PRE sees the physical edge before WillowPlayerInput processes it. If
    a supported gamepad button is already Pressed/Held/Cleared at that instant,
    the previous release was lost somewhere in the native/mod input pipeline.
    Reset only that row, never unrelated held inputs, then let BL2 process the
    real press normally.
    """
    if getattr(args,'EventType',None)!=EInputEvent.IE_Pressed:return
    key=canonical(getattr(args,'Key',''))
    if key not in KEYS:return
    pc=_cached_pc()
    if pc is None or getattr(pc,'PlayerInput',None) is None:return
    player=getattr(pc,'Player',None)
    local_id=getattr(player,'ControllerId',None)
    event_id=getattr(args,'ControllerId',None)
    if local_id is not None and event_id is not None and int(event_id)!=int(local_id):return
    # Never alter gameplay state for an input-capturing menu/bind dialog.
    dialog=_capture.dialog() if _capture.dialog is not None else None
    if dialog is not None or _ui_blocked():return
    _apply_travel_resume_pulse(pc)
    if not _bindings.ready or _bindings.pi is None or not _same(_bindings.pi(),pc.PlayerInput):return
    if not _bindings.override_active:return
    old_state=_bindings.recover_stale_press(key)
    if old_state is not None:
        _log(f'Recovered stale native button state: {SHORT[key]} ({old_state} -> Released)')

    # InputButtonData.State is only the public physical-row state. Gearbox also
    # maintains a native logical-action state map (NameToButtonStatePtr), and the
    # action-skill toggle can remain stale there even when the physical row reads
    # Released. Reset only the logical ActionSkill edge immediately before a
    # fresh press of whichever physical button currently owns the factory-LB
    # action bundle. The incoming press then establishes the real state normally.
    try:
        context=_context(pc)
        mapping=_bindings.resolved_layout(context,_runtime_choices()[context])
        if mapping.get(FROM_SHORT['LB'])==key:
            pc.PlayerInput.ResetButtonState('ActionSkill')
    except Exception as exc:
        _warn('action_skill_edge_reset',
              f'Action-skill input edge reset unavailable: {type(exc).__name__}: {exc}')


def _configure_binding_service() -> None:
    """Keep PlayerInput hooked only while a binding transaction needs boundaries."""
    if not _active or not _bindings.needs_service(bool(layouts_enabled.value)):
        _remove_tags('binding-service')
        return
    # Resolve the class function globally, even before the first local PlayerInput
    # exists. This one-shot bootstrap fixes auto-enabled layouts at initial load.
    fn=unrealsdk.find_object('Function','WillowGame.WillowPlayerInput:PlayerInput')
    _install(fn._path_name(),Type.POST_UNCONDITIONAL,'binding-service',_binding_boundary)


def _request_binding_sync(pc=None,defer=False) -> None:
    """Wake binding work from a rare state/settings event, never from every frame."""
    if not _active:return
    enabled=bool(layouts_enabled.value)
    dormant=(not enabled and not _bindings.last_enabled and not _bindings.applied
        and _bindings.latent_applied is None and _bindings.compat_applied is None
        and not _bindings.refresh and not _bindings.retry and not _bindings._sync_pending)
    if dormant:
        _configure_binding_service()
        return
    if not defer:_sync(pc,boundary=False)
    _configure_binding_service()


def _binding_boundary(obj: Any,args: Any,ret: Any,func: Any) -> None:
    # Never trust the cached PlayerInput across travel. The hook is on the class
    # function, so use the engine's current local object as the authority.
    pc=get_pc(possibly_loading=True)
    if pc is None or getattr(pc,'PlayerInput',None) is None or not _same(obj,pc.PlayerInput):return
    _sync(pc,obj,boundary=True)
    _configure_binding_service()


def _configure_look_hooks(pc=None) -> None:
    """Keep the input epoch marker, but only hook rotation in contexts that scale."""
    global _look_ready,_look_verified,_look_reconfigure_pending,_finished,_scaled
    needed=_look_overrides()
    if not needed:
        if _rotation_depth:
            _look_reconfigure_pending=True
            return
        _restore_look()
        _remove_tags('look-consume','look-restore','input-end')
        _look_ready=_look_verified=False
        _look_reconfigure_pending=False
        _finished=_scaled=0
        return
    if pc is None or getattr(pc,'PlayerInput',None) is None:return
    try:
        if not _look_ready:
            _resolve_deadzone_provider()
            for name in ('aTurn','aLookUp'):_prop(pc.PlayerInput,name,'FloatProperty')
            _prop(pc.PlayerInput,'bUsingGamepad','BoolProperty')
            player_input=unrealsdk.find_object('Function','WillowGame.WillowPlayerInput:PlayerInput')
            _install(player_input._path_name(),Type.POST_UNCONDITIONAL,'input-end',_input_end)
            _look_ready=True
        elif not _has_tag('input-end'):
            player_input=unrealsdk.find_object('Function','WillowGame.WillowPlayerInput:PlayerInput')
            _install(player_input._path_name(),Type.POST_UNCONDITIONAL,'input-end',_input_end)
        context=_active_context if _active_context in ('foot','vehicle') else _context(pc)
        if _context_needs_look(context):
            if not _has_tag('look-consume') or not _has_tag('look-restore'):
                rotation=_function(pc,'UpdateRotation')
                _install(rotation._path_name(),Type.PRE,'look-consume',_rotation_pre)
                _install(rotation._path_name(),Type.POST_UNCONDITIONAL,'look-restore',_rotation_post)
            _look_reconfigure_pending=False
        elif _rotation_depth:
            _look_reconfigure_pending=True
        else:
            _restore_look()
            _remove_tags('look-consume','look-restore')
            _look_reconfigure_pending=False
    except Exception:
        _restore_look()
        _remove_tags('look-consume','look-restore','input-end')
        _look_ready=False
        _look_reconfigure_pending=False
        raise


def _labels(pc: Any) -> None:
    # Read labels only. No interpretation or application of native rebindings.
    try:
        device=unrealsdk.find_object('InputDeviceDefinition','GD_Input.Devices.InputDevice_Controller')
        for row in device.Buttons:
            key=canonical(row.KeyName)
            if key not in KEYS: continue
            names=[]
            for field in ('PressActions','TapActions','HoldActions'):
                for action in getattr(row,field):
                    name=str(action.Name).removeprefix('InputAction_')
                    name=re.sub(r'(?<=[a-z])(?=[A-Z])',' ',name).replace('_',' ')
                    if name not in names: names.append(name)
            if names:
                label=' / '.join(names)
                for context,group in BINDINGS.items():
                    # Preserve the separate controller contexts even after
                    # native input metadata is discovered on character load.
                    group[key].display_name=_context_action_label(context,key)
                    group[key].description=(
                        f"Factory physical key {SHORT[key]}: {label}. "
                        f"{context.title()} context: {_context_action_label(context,key)}. "
                        f"Default means the {'TPS' if IS_TPS else 'BL2'} factory input for that bundle.")
    except Exception as exc:
        _warn('labels',f'Native labels unavailable; provisional labels retained: {exc}')


def _discover(pc: Any) -> None:
    global _pc,_pi,_loaded,_look_ready,_look_verified,_vehicle_fn,_vehicle_kwargs
    global _finished,_scaled,_vehicle_name,_rotation_depth,_look_owner_depth,_look_reconfigure_pending
    _pc,_pi=WeakPointer(pc),WeakPointer(pc.PlayerInput)
    _loaded=True;_look_ready=_look_verified=False
    _clear_context();_pawn_classes.clear()
    _finished=_scaled=0
    _rotation_depth=_look_owner_depth=0;_look_reconfigure_pending=False
    _bindings.attach(pc,override=bool(layouts_enabled.value))
    _context(pc,force=True)
    _refresh_blocked(pc)
    provider=_resolve_deadzone_provider()
    if provider is not None:_log(f'Deadzone Control integration: {provider.__name__}')
    _labels(pc)
    _binding_descriptions()
    _vehicle_fn=None;_vehicle_name=None;_vehicle_kwargs={}
    try:
        fn=_function(pc,'IsUsingVehicle')
        result=next(p for p in fn._properties() if int(p.PropertyFlags)&0x400)
        required=[p for p in _signature(fn) if not int(p.PropertyFlags)&0x10]
        if str(result.Class.Name)=='BoolProperty' and all(str(p.Name)=='IncludeAttachedRiders' and str(p.Class.Name)=='BoolProperty' for p in required):
            _vehicle_fn=fn
            _vehicle_name=str(fn.Name)
            _vehicle_kwargs={str(p.Name):True for p in required}
    except (AttributeError,ValueError,StopIteration):pass
    try:_configure_look_hooks(pc)
    except Exception as exc:_warn('look_unavailable',f'Look controls unavailable: {exc}')


def _activate(pc: Any) -> None:
    for name,kind,tag,callback in (
            ('PlayMovie',Type.PRE,'menu-open-pre',_movie_pre),
            ('PlayMovie',Type.POST_UNCONDITIONAL,'menu-open-post',_movie_post),
            ('Movie_OnClosed',Type.POST_UNCONDITIONAL,'menu-close',_movie_closed)):
        fn=_function(pc.GFxUIManager,name)
        _install(fn._path_name(),kind,tag,callback)
    _discover(pc)


def _sync(pc=None,pi=None,boundary=False) -> None:
    global _syncing,_preference_generation,_choices_cache
    if _syncing:return
    _syncing=True
    try:
        if pc is None:pc=_cached_pc()
        if pc is None or pc.Pawn is None:return
        if pi is None:pi=pc.PlayerInput
        if pi is None:return
        if not _loaded or _pc is None or not _same(_pc(),pc) or not _same(_pi(),pi):
            _restore_look()
            _menus.clear()
            _activate(pc)
        try:
            if _bindings.ready and _preference_generation!=_bindings.generation:
                _prepare_preferences(_bindings.native)
                _preference_generation=_bindings.generation
            enabled=bool(layouts_enabled.value)
            context=_context(pc) if enabled else 'foot'
            _bindings.sync(enabled,context,_runtime_choices(),_bindings_blocked() if enabled else False,boundary=boundary)
            if _described_generation!=_bindings.generation:
                _prepare_preferences(_bindings.native)
                _binding_descriptions()
        except Exception as exc:
            _bindings.issue=f'Layout changes stopped: {type(exc).__name__}: {exc}'
            _bindings.paused=True
            _warn('binding_stopped',_bindings.issue)
            try:_bindings.disable()
            except Exception as cleanup:_warn('binding_cleanup',f'Layout cleanup unavailable: {cleanup}')
    finally:_syncing=False


def _rotation_pre(obj,args,ret,func):
    global _rotation_depth
    if not _active or not _look_ready:return
    pc=_pc() if _pc is not None else None
    pi=_pi() if _pi is not None else None
    if pc is None or pi is None or not _same(obj,pc):return
    _rotation_depth+=1
    pawn=getattr(pc,'Pawn',None)
    _look(pc,pi,pawn,_blocked(pc,pawn))


def _rotation_post(obj,args,ret,func):
    global _rotation_depth,_look_owner_depth
    pc=_pc() if _pc is not None else None
    if pc is None or not _same(obj,pc):return
    try:
        # Nested UpdateRotation calls share the same input epoch. Only the call
        # which actually applied the multiplier owns restoration.
        if _look_saved and _look_owner_depth==_rotation_depth:
            _restore_look()
    finally:
        if _rotation_depth>0:_rotation_depth-=1
        if _rotation_depth==0 and not _look_saved:_look_owner_depth=0


def _input_end(obj: Any,args: Any,ret: Any,func: Any) -> None:
    global _finished,_rotation_depth,_look_owner_depth
    if _active and _look_ready and _pi is not None and _same(obj,_pi()):
        # This is both the input-epoch marker and a fail-safe. If a previous
        # rotation path failed to restore or unwind, never carry scaled axes or
        # stale nesting state into a new native PlayerInput epoch.
        if _look_saved:_restore_look()
        if _rotation_depth:
            _rotation_depth=0;_look_owner_depth=0
        _finished+=1
        if _look_reconfigure_pending and _look_overrides():
            _configure_look_hooks(_pc())


def _movie_pre(obj: Any,args: Any,ret: Any,func: Any) -> None:
    global _opening
    pc=get_pc(possibly_loading=True)
    if _active and pc is not None and _same(obj,pc.GFxUIManager):
        _opening+=1
        _request_binding_sync(pc)


def _movie_post(obj: Any,args: Any,ret: Any,func: Any) -> None:
    global _opening
    pc=get_pc(possibly_loading=True)
    if not _active or pc is None or not _same(obj,pc.GFxUIManager): return
    _opening=max(0,_opening-1)
    if ret is not None and hasattr(ret,'_get_address') and _capturing(ret):
        _sdk_dispatch.cancel()
        _menus[ret._get_address()]=WeakPointer(ret)
    _request_binding_sync(pc)


def _movie_closed(obj: Any,args: Any,ret: Any,func: Any) -> None:
    pc=get_pc(possibly_loading=True)
    if not _active or pc is None or not _same(obj,pc.GFxUIManager): return
    movie=getattr(args,'Movie',None)
    if movie is not None: _menus.pop(movie._get_address(),None)
    if movie is not None and _capturing(movie):_sdk_refresh()
    _request_binding_sync(pc)


def _input_context_changed(obj: Any,args: Any,ret: Any,func: Any) -> None:
    pc=_cached_pc()
    if pc is None or pc.PlayerInput is None or not _same(obj,pc.PlayerInput):return
    _context(pc,force=True)
    _configure_look_hooks(pc)
    # A real context change is strong evidence that ClientRestart has settled,
    # so let the temporary restart watcher consume the repair on its next input
    # boundary. Do not depend on this event exclusively: SetInputContext does not
    # emit PostChangeInputContext when the resolved context name is unchanged.
    if _restart_rebind_armed:_restart_rebind_note_ready(pc)
    _request_binding_sync(pc)


def _controller_context_changed(obj: Any,args: Any,ret: Any,func: Any) -> None:
    pc=_cached_pc()
    if pc is None or not _same(obj,pc):return
    _context(pc,force=True)
    _configure_look_hooks(pc)
    _request_binding_sync(pc)


def _controller_state_changed(obj: Any,args: Any,ret: Any,func: Any) -> None:
    pc=_cached_pc()
    if pc is None or not _same(obj,pc):return
    # Bindings no longer depend on cinematic/pause/look-block state. Keep this
    # event path sensitivity-only so those rare engine changes do not wake the
    # binding transaction runtime.
    _refresh_blocked(pc)


def _possession_changed(obj: Any,args: Any,ret: Any,func: Any) -> None:
    pc=_cached_pc()
    if pc is None or not _same(obj,pc):return
    _sdk_dispatch.cancel()
    # Do not globally release inputs on ordinary possession changes. The release
    # barrier must continue to observe genuinely held buttons across vehicle/foot
    # transitions; stale edge recovery is handled per button at raw InputKey PRE.
    _clear_context();_pawn_classes.clear()
    if pc.Pawn is not None and pc.PlayerInput is not None:_context(pc,force=True)
    _configure_look_hooks(pc)
    _request_binding_sync(pc)


def _mark_native_rebuild(pc: Any,*,defer: bool) -> None:
    """Invalidate native input caches and adopt the finished table before reapplying."""
    if pc is None or getattr(pc,'PlayerInput',None) is None:return
    _bindings.refresh=True
    _bindings.compat_refresh=True
    _bindings.invalidate_rows()
    _clear_context();_pawn_classes.clear()
    from .binding_runtime import clear_schema_cache
    clear_schema_cache()
    # Map travel may replace PlayerInput. Immediate sync discovers/re-hooks the
    # current object; native SetupInputDevices stays deferred until its call stack
    # has completely returned.
    _request_binding_sync(pc,defer=defer)


def _post_context_input_cleanup(pc: Any) -> None:
    """Clear native press/hold/latent state after a restarted context settles.

    This runs after ClientRestart has led to the new PostChangeInputContext, not
    at an arbitrary loading-movie boundary. It is paired with a fresh handler
    restore/capture/reapply transaction because ReleasePressedButtons alone is
    not sufficient for every stale copied-handler state.
    """
    if not bool(layouts_enabled.value):return
    pi=getattr(pc,'PlayerInput',None)
    if pi is None:return
    try:
        pi.ReleasePressedButtons()

        # A same-name vehicle -> walking transition can skip SetInputContext's
        # PreChange/PostChange pair entirely. That leaves native hidden action
        # maps/remapped-button pointers from the prior context alive even though
        # Buttons[].State and our copied handler arrays look correct. Pause works
        # because it forces an input-context transition. Reproduce that repair
        # directly without opening UI: force-refresh the already selected context.
        current_context=getattr(pi,'CurrentInputContextName',None)
        if current_context is not None and str(current_context).casefold() not in ('','none'):
            pi.SetInputContext(current_context,True)

        # ActionSkill has an additional logical state entry. Clear it after the
        # forced context rebuild so the next controller press is always a fresh
        # toggle edge. This does not deactivate an already-running skill.
        pi.ResetButtonState('ActionSkill')
        pi.ReleasePressedButtons()

        # Native cleanup is authoritative, but verify the public managed rows
        # when this is the runtime we currently own. Repair only as a fallback.
        repaired=[]
        if (_bindings.ready and _bindings.pi is not None
                and _same(_bindings.pi(),pi) and _bindings.released is not None):
            for key,row in _bindings.rows().items():
                if int(row.State)!=_bindings.released:
                    row.State=_bindings.released
                    row.LastPressedTime=-1.0
                    repaired.append(SHORT[key])
        if repaired:
            _log('Restart-context input cleanup repaired managed rows: '+', '.join(repaired))
        else:
            _log('Restart-context native pressed/latent input state cleared')
    except Exception as exc:
        # Do not take down an otherwise working layout if Gearbox changes this
        # native cleanup path. The fresh-press repair remains as a fallback.
        _warn('post_context_input_cleanup',f'Post-context input cleanup unavailable: {type(exc).__name__}: {exc}')


def _restart_rebind_note_ready(pc: Any) -> None:
    """Accelerate an armed restart repair after a confirmed input-context update."""
    global _restart_rebind_ready
    if not _restart_rebind_armed:return
    # One more natural PlayerInput boundary is still required. This avoids doing
    # a restore/rebuild from inside SetInputContext/PostChangeInputContext itself.
    _restart_rebind_ready=max(_restart_rebind_ready,1)


def _configure_restart_recovery() -> None:
    """Keep a PlayerInput watcher only while a ClientRestart repair is armed."""
    if not _active or not _restart_rebind_armed:
        _remove_tags('restart-recovery')
        return
    fn=unrealsdk.find_object('Function','WillowGame.WillowPlayerInput:PlayerInput')
    _install(fn._path_name(),Type.POST_UNCONDITIONAL,'restart-recovery',_restart_recovery_boundary)


def _restart_recovery_boundary(obj: Any,args: Any,ret: Any,func: Any) -> None:
    """Wait for restarted possession/input state to settle, then refresh handlers once."""
    global _restart_rebind_armed,_restart_rebind_ready,_restart_rebind_started
    if not _restart_rebind_armed:
        _configure_restart_recovery();return
    if not bool(layouts_enabled.value):
        _restart_rebind_armed=False;_restart_rebind_ready=0;_restart_rebind_started=0.0
        _configure_restart_recovery();return
    pc=get_pc(possibly_loading=True)
    if pc is None or getattr(pc,'PlayerInput',None) is None or not _same(obj,pc.PlayerInput):return
    pawn=getattr(pc,'Pawn',None)
    if pawn is None:
        _restart_rebind_ready=0;return

    # The important broken case is ClientRestart while an old vehicle reference
    # is still draining. PlayerWalking itself waits for DrivenVehicle to clear.
    # Mirror that readiness rule, but cap the wait so legitimate vehicle restarts
    # cannot leave a permanent per-frame watcher installed.
    driven=None
    try:
        _prop(pawn,'DrivenVehicle','ObjectProperty')
        driven=getattr(pawn,'DrivenVehicle',None)
    except (AttributeError,ValueError):
        pass
    timed_out=(time.monotonic()-_restart_rebind_started)>=1.0
    if driven is not None and not timed_out:
        _restart_rebind_ready=0;return

    _restart_rebind_ready+=1
    if _restart_rebind_ready<2:return

    # Rediscover first in case travel replaced PlayerInput. A plain handler
    # restore/capture is not enough for the vehicle -> non-vehicle travel bug:
    # disabling Customizer fixes it because disable performs a real native
    # SetupInputDevices rebuild. Recreate that native state here while preserving
    # the user's selected preset metadata, then refresh the settled context while
    # the clean factory-Default table is active. The existing two-boundary
    # transaction reapplies the custom layout afterwards.
    _sync(pc,boundary=False)
    if (not _bindings.ready or _bindings.pi is None
            or not _same(_bindings.pi(),pc.PlayerInput)):
        return
    _arm_travel_resume_pulse()
    _restart_rebind_armed=False
    _restart_rebind_ready=0
    _restart_rebind_started=0.0
    rebuilt=_bindings.rebuild_native_override()
    if rebuilt:
        _post_context_input_cleanup(pc)
    else:
        # Defensive fallback for an unexpected lifecycle state. Keep the older
        # safe refresh available rather than leaving the controller unserviced.
        _post_context_input_cleanup(pc)
        _bindings.request_fresh_rebind()
    _request_binding_sync(pc)
    _configure_restart_recovery()


def _arm_restart_recovery() -> None:
    """Arm one bounded post-restart/travel handler refresh."""
    global _restart_rebind_armed,_restart_rebind_ready,_restart_rebind_started
    if not _active or not bool(layouts_enabled.value) or not _loaded:return
    if _restart_rebind_armed:return
    _restart_rebind_armed=True
    _restart_rebind_ready=0
    _restart_rebind_started=time.monotonic()
    _configure_restart_recovery()


def _client_restart_pre(obj: Any,args: Any,ret: Any,func: Any) -> None:
    """Arm one fresh-handler rebuild for an already-running local player restart."""
    if not _active or not bool(layouts_enabled.value) or not _loaded:return
    pc=get_pc(possibly_loading=True)
    if pc is None or not _same(obj,pc):return
    # Arm before ClientRestart runs. PostChangeInputContext is only an accelerator:
    # if the resolved context name stays the same, SetInputContext emits no event,
    # so the temporary PlayerInput watcher is the reliable fallback.
    _arm_restart_recovery()


def _world(obj: Any,args: Any,ret: Any,func: Any) -> None:
    if not _active: return
    pc=get_pc(possibly_loading=True)
    if pc is None: return
    path=func.func._path_name()
    if 'DisableLoadingMovie' in path and not _same(obj,pc): return
    if 'PostBeginPlay' in path and not _same(obj,pc.Pawn): return
    _capture.cancel()
    _sdk_dispatch.cancel()
    _sdk_refresh()
    _sdk_dialog.stop()
    _native_view.close()
    _side_panel.reset_focus()
    _menus.clear()
    # Travel can rebuild the live button table or replace PlayerInput entirely.
    # Treat the loaded world as a native input-generation boundary.
    _mark_native_rebuild(pc,defer=False)
    # Arm the bounded repair from the local player's pawn spawn as a fallback.
    # Loading-movie completion can fire repeatedly for streaming/sublevels and
    # must not trigger repeated controller rebuilds. ClientRestart is primary.
    if 'PostBeginPlay' in path:
        _arm_restart_recovery()


def _failure(exc: Exception) -> None:
    global _failing,_active
    if _failing: return
    _failing=True; _active=False
    if 'mod' in globals():mod.is_enabled=False
    try:
        _warn('stopped',f'Changes stopped: {type(exc).__name__}: {exc}')
        _disable()
    finally: _failing=False


def _enable() -> None:
    # Two native binding owners cannot run safely at the same time.
    from mods_base import get_ordered_mod_list
    for another in get_ordered_mod_list():
        if another is not mod and getattr(another,'is_enabled',False):
            filename=getattr(another,'settings_file',None)
            if filename is not None and Path(filename).name.casefold()=='2controllercustomizer.json':
                _warn('native_conflict',
                    'Controller Customizer is still enabled. Disable it, then toggle Controller Manager Off/On.')
                mod.is_enabled=False
                return
    global _active,_messages,_finished,_scaled,_rotation_depth,_look_owner_depth,_look_reconfigure_pending
    global _preference_generation,_choices_cache
    global _look_values,_deadzone_provider,_restart_rebind_armed,_restart_rebind_ready,_restart_rebind_started
    global _travel_resume_armed,_travel_resume_pending,_travel_resume_running
    # Re-enable must not cancel the only release-boundary cleanup callback.
    # The user can complete/retry the old restore while this mod remains Off.
    if _bindings.drain_hook is not None or _bindings.drain_stalled:
        _warn('binding_drain_pending',
              'Controller Manager cannot enable until pending input restoration '
              'completes. Release held buttons or use Retry Pending Restore; '
              'restart the game if input boundaries are unavailable.')
        mod.is_enabled=False
        return
    _look_values=None
    _disable()
    _messages=0;_log_seen.clear();_warned.clear();_deadzone_provider=None
    _preference_generation=-1;_choices_cache=None
    _clear_context();_pawn_classes.clear()
    _capture.reported=False
    _finished=_scaled=0
    _rotation_depth=_look_owner_depth=0;_look_reconfigure_pending=False
    _restart_rebind_armed=False;_restart_rebind_ready=0;_restart_rebind_started=0.0
    _travel_resume_armed=False;_travel_resume_pending=False;_travel_resume_running=False
    try:
        for path in ('WillowGame.WillowPlayerController:WillowClientDisableLoadingMovie',
                     'WillowGame.WillowPlayerPawn:PostBeginPlay'):
            try:fn=unrealsdk.find_object('Function',path)
            except ValueError:continue
            _install(fn._path_name(),Type.POST_UNCONDITIONAL,f'world:{path}',_world)
        try:
            fn=unrealsdk.find_object('Function','WillowGame.WillowPlayerInput:PostChangeInputContext')
            _install(fn._path_name(),Type.POST_UNCONDITIONAL,'input-context',_input_context_changed)
        except ValueError:
            fn=unrealsdk.find_object('Function','WillowGame.WillowPlayerController:UpdateInputContext')
            _install(fn._path_name(),Type.POST_UNCONDITIONAL,'input-context-fallback',_controller_context_changed)
            _warn('context_fallback','PostChangeInputContext unavailable; using UpdateInputContext fallback')
        try:
            target=unrealsdk.find_object('Function','Engine.PlayerController:ClientRestart')
            _install(target._path_name(),Type.PRE,'client-restart-pre',_client_restart_pre)
        except ValueError:
            _warn('restart_rebind_unavailable','ClientRestart unavailable; post-travel handler refresh will use normal lifecycle recovery')
        for path in ('WillowGame.WillowPlayerController:Possess','WillowGame.WillowPlayerController:UnPossess'):
            try:target=unrealsdk.find_object('Function',path)
            except ValueError:continue
            _install(target._path_name(),Type.POST_UNCONDITIONAL,f'lifecycle:{path}',_possession_changed)
        for path in ('WillowGame.WillowPlayerController:SetPause',
                     'WillowGame.WillowPlayerController:SetCinematicMode',
                     'WillowGame.WillowPlayerController:ClientSetCinematicMode',
                     'Engine.PlayerController:IgnoreLookInput',
                     'Engine.PlayerController:ResetPlayerMovementInput'):
            try:target=unrealsdk.find_object('Function',path)
            except ValueError:continue
            _install(target._path_name(),Type.POST_UNCONDITIONAL,f'blocked:{path}',_controller_state_changed)
        # Distinguish deliberate preset/rebinding changes from routine native
        # input rebuilds. Our own direct Default application is guarded/suppressed.
        for path in ('WillowGame.WillowPlayerController:ApplyControllerPreset',
                     'WillowGame.WillowPlayerInput:SetControllerRebindings'):
            try:target=unrealsdk.find_object('Function',path)
            except ValueError:continue
            _install(target._path_name(),Type.POST_UNCONDITIONAL,f'native-config:{path}',_native_configuration_changed)
        # Observe the controller wrapper only. It fires after the nested
        # PlayerInput rebuild is complete, so we never capture a half-built table.
        path='WillowGame.WillowPlayerController:SetupInputDevices'
        try:target=unrealsdk.find_object('Function',path)
        except ValueError:target=None
        if target is not None:
            _install(target._path_name(),Type.POST_UNCONDITIONAL,f'native-rebuild:{path}',_native_rebuild)
        _active=True
        _sdk_refresh()
        from .capture import function_path
        travel_path=function_path(unrealsdk.find_class('WillowPlayerController'),'PreClientTravel')
        _install(travel_path,Type.PRE,'sdk-travel',_sdk_travel)
        # Native connection notifications are event-driven, including front end.
        try:
            change_path=function_path(unrealsdk.find_class('WillowPlayerController'),'OnControllerChanged')
            _install(change_path,Type.PRE,'sdk-controller-change',_sdk_controller_changed)
        except ValueError:
            _warn('sdk_connection_hook','Controller-change event unavailable; menu/travel and fresh edges still clear shortcuts')
        # UI hooks are installed once at module import, independently of Enabled.
        # No gameplay hooks or native remapping are required to show or edit the panel.
        _install_ui_hooks()
        _configure_physical_input_hook()
        pc=get_pc(possibly_loading=True)
        if pc is not None and pc.Pawn is not None and pc.PlayerInput is not None:_activate(pc)
        else:_configure_look_hooks(None)
        _request_binding_sync(pc)
        _log(f'enabled {mod.version}; SDK dispatch active; custom bindings={bool(layouts_enabled.value)}')
    except Exception as exc:_failure(exc)


def _install_ui_hooks():
    _UI_HOOKS = (
        ('WillowGame.WillowScrollingList:PushDataProvider',Type.PRE,'provider-adapter',_pane_provider_push),
        ('WillowGame.WillowScrollingListDataProviderKeyboardMouseOptions:OnPop',Type.PRE,'provider-pop',_pane_popped),
        ('WillowGame.WillowScrollingListDataProviderKeyboardMouseOptions:extOnKeyClicked',Type.PRE,'flash-click',_pane_flash_click),
        ('WillowGame.WillowScrollingListDataProviderKeyboardMouseOptions:DoBind',Type.PRE,'prevent-native-bind',_pane_native_do_bind),
        ('WillowGame.WillowScrollingListDataProviderKeyboardMouseOptions:extOnKeySelectionChange',Type.POST_UNCONDITIONAL,'flash-select',_pane_flash_selection),
        ('WillowGame.WillowScrollingListDataProviderKeyboardMouseOptions:HandleSelectionChange',Type.PRE,'category-select',_pane_category_selection),
        ('WillowGame.WillowScrollingListDataProviderKeyboardMouseOptions:HandleSelectionRollover',Type.PRE,'category-hover',_pane_category_selection),
        ('WillowGame.OptionsGFxMovie:HandleInputKey',Type.PRE,'panel-input',_pane_input),
        ('WillowGame.WillowScrollingList:OnClikEvent',Type.PRE,'panel-mouse',_pane_mouse_left),
        ('WillowGame.OptionsGFxMovie:OnScollingListSelectedIndexChanged',Type.POST_UNCONDITIONAL,'panel-selection',_pane_selection),
        ('WillowGame.OptionsGFxMovie:OnClose',Type.POST_UNCONDITIONAL,'panel-close',_pane_closed),
        ('WillowGame.WillowGFxUIManager:Movie_OnClosed',Type.POST_UNCONDITIONAL,'modal-close',_pane_modal_closed),
    )
    # Resolve the complete TPS adapter before adding hooks: a partially wired
    # provider could intercept native menu input without the matching cleanup.
    resolved={}
    if IS_TPS:
        for path,kind,tag,callback in _UI_HOOKS:
            try:
                fn=unrealsdk.find_object('Function',path)
            except ValueError:
                fn=None
            if fn is None:
                if _ui_hooks:
                    raise RuntimeError('TPS UI function unavailable after hook registration: '+tag)
                _warn('tps_ui_missing:'+tag,
                      f'TPS controller panel unavailable: missing UI function {tag}')
                return
            resolved[path]=fn._path_name()
    # Remain available while Disabled. Every callback first verifies our exact
    # menu movie and current SDK mod provider, so other options aren't affected.
    for path,kind,tag,callback in _UI_HOOKS:
        ident='ControllerManager:UI:'+tag
        existing=next((entry for entry in _ui_hooks if entry[2]==ident),None)
        if existing is not None:
            if unrealsdk.hooks.has_hook(*existing):
                continue
            _ui_hooks.remove(existing)  # SDK no longer has this UI callback.
        if IS_TPS:
            path=resolved[path]
        else:
            fn=unrealsdk.find_object('Function',path)
            path=fn._path_name()
        if unrealsdk.hooks.has_hook(path,kind,ident):
            raise RuntimeError('Stale Controller Manager UI hook: '+ident)
        def guarded(obj,args,ret,func,callback=callback):
            try:return callback(obj,args,ret,func)
            except Exception as exc:
                _warn('ui_hook_error',f'UI hook error: {type(exc).__name__}: {exc}')
                return None
        try:
            added=unrealsdk.hooks.add_hook(path,kind,ident,
                guarded)
        except Exception:
            # Retain a registration installed before the SDK raised.
            if unrealsdk.hooks.has_hook(path,kind,ident):
                _ui_hooks.append((path,kind,ident))
            raise
        # A False return can still mean the callback was appended successfully.
        # This ID was absent at preflight; verify it exists after the call.
        try:
            live=unrealsdk.hooks.has_hook(path,kind,ident)
        except Exception:
            _ui_hooks.append((path,kind,ident))
            raise
        if not live:
            raise RuntimeError('UI hook registration failed: '+ident)
        _ui_hooks.append((path,kind,ident))


def _disable() -> None:
    global _active,_cleaning,_opening,_pc,_pi,_loaded,_look_ready,_look_verified,_menu_cleanup_due,_deadzone_provider
    global _restart_rebind_armed,_restart_rebind_ready,_restart_rebind_started
    global _travel_resume_armed,_travel_resume_pending,_travel_resume_running
    global _rotation_depth,_look_owner_depth,_look_reconfigure_pending,_finished,_scaled
    global _sdk_input_owner
    if _cleaning: return
    _cleaning=True; _active=False
    errors=[]
    try:
        _sdk_dispatch.cancel()
        _sdk_input_owner=None
        # Keep the settings panel visible; Enabled controls gameplay, not the UI.
        try: _capture.cancel()
        except Exception as exc: errors.append(f'capture cleanup: {exc}')
        try:
            if _sdk_dialog is not None:_sdk_dialog.stop()
            if _sdk_reset is not None:_sdk_reset.stop()
        except Exception as exc: errors.append(f'SDK dialog cleanup: {exc}')
        try: _bindings.disable()
        except Exception as exc: errors.append(f'binding restore: {exc}')
        try: _restore_look()
        except Exception as exc: errors.append(f'look restore: {exc}')
        for path,kind,identifier in tuple(_hooks):
            try:
                unrealsdk.hooks.remove_hook(path,kind,identifier)
                if unrealsdk.hooks.has_hook(path,kind,identifier):
                    raise RuntimeError(f'hook removal unverified: {identifier}')
                _hooks.remove((path,kind,identifier))
            except Exception as exc: errors.append(f'hook: {exc}')
        _menus.clear();_opening=0;_menu_cleanup_due=0.0
        _loaded=_look_ready=_look_verified=False
        _rotation_depth=_look_owner_depth=0;_look_reconfigure_pending=False
        _finished=_scaled=0
        _pc=_pi=None
        _deadzone_provider=None;_zoom_schemas.clear()
        _restart_rebind_armed=False;_restart_rebind_ready=0;_restart_rebind_started=0.0
        _travel_resume_armed=False;_travel_resume_pending=False;_travel_resume_running=False
        _clear_context();_pawn_classes.clear()
    finally: _cleaning=False
    _pane_redraw_current()
    if errors: logging.warning(f'{PREFIX}: cleanup incomplete: {errors}')


def _native_configuration_changed(obj: Any,args: Any,ret: Any,func: Any) -> None:
    """Mark a real user/profile preset change before SetupInputDevices rebuilds."""
    if not _active or not bool(layouts_enabled.value) or not _bindings.override_active:return
    pc=get_pc(possibly_loading=True)
    if pc is None or getattr(pc,'PlayerInput',None) is None:return
    path=func.func._path_name()
    if 'ApplyControllerPreset' in path:
        if not _same(obj,pc) or ret is False:return
    elif 'SetControllerRebindings' in path:
        if not _same(obj,pc.PlayerInput):return
    else:return
    _bindings.note_external_native_change()


def _native_rebuild(obj: Any,args: Any,ret: Any,func: Any) -> None:
    pc=get_pc(possibly_loading=True)
    if pc is None or not _same(obj,pc):return
    try:
        # If BL2 just rebuilt a user-selected preset while Custom Bindings owns
        # the live layer, save that finished native state for later restoration,
        # then immediately rebuild our preset-neutral Default layer. The game's
        # SetControllerPreset wrapper can still save the user's chosen preset.
        if (bool(layouts_enabled.value) and _bindings.ready and _bindings.pi is not None
                and _same(_bindings.pi(),pc.PlayerInput) and _bindings.override_active):
            if _bindings.adopt_external_native_rebuild():
                _clear_context();_pawn_classes.clear()
                _request_binding_sync(pc)
                return
        _mark_native_rebuild(pc,defer=True)
    except Exception as exc:
        _warn('native_rebuild',f'Native controller rebuild recovery failed: {exc}')
        _mark_native_rebuild(pc,defer=True)


def _binding_descriptions() -> None:
    global _described_generation
    try:
        snapshot=_display_snapshot()
        saved=snapshot['metadata']
        live_baseline=snapshot['baseline']
        cached_baseline=saved.get('baseline',{}) if saved.get('version')==3 else {}
        baseline=live_baseline or cached_baseline
        preview=snapshot['values']
    except Exception as exc:
        _warn('preferences_display',f'Saved layout needs review: {exc}')
        saved={'pending':[]};baseline={};preview=None;live_baseline=None
    pc=get_pc(possibly_loading=True)
    cached=live_baseline is None
    status=''
    if _bindings.drain_issue:
        status=' Status: '+_bindings.drain_issue
    elif _bindings.issue:
        status=' Status: '+_bindings.issue
    elif _bindings._sync_pending:
        names=', '.join(SHORT[k] for k in _bindings._pending_keys)
        status=' Pending: waiting for two released input boundaries'+(f' on {names}.' if names else '.')
    for context,group in BINDINGS.items():
        for key,option in group.items():
            label=_context_action_label(context,key)
            option.action_label=label
            selection=option.value
            pending=[v for v in saved['pending'] if v['context']==context]
            if preview is not None:
                selection=preview[context][key]
            else:
                for v in pending:
                    if v['action']=='reset':selection='Default'
                    elif v['action']==key:selection=v['value']
            option.display_name=label+': '+selection
            option.description_title=label
            hint=' Last known button; updates after loading.' if cached and baseline else ''
            option.description='Press A / Enter to bind. Back opens Unbind / Restore Default / Cancel.'+hint+(' Changes apply after loading.' if pending else '')+status
    _described_generation=_bindings.generation


def _choices():
    return {c:{k:o.value for k,o in group.items()} for c,group in BINDINGS.items()}


def _configuration_baseline(read_only=False):
    """Return only a verified factory-Default baseline.

    When Custom Bindings is off, the live table may reflect any BL2 preset. Edits
    stay queued rather than being interpreted against that preset.
    """
    pc=get_pc(possibly_loading=True)
    # Front-end menus have no Pawn. Saved, validated generation-3 factory
    # baselines remain usable for editing without a live world.
    if pc is not None and pc.Pawn is not None and pc.PlayerInput is not None and (_bindings.ready and _bindings.override_active and _bindings.pi is not None
            and _same(_bindings.pi(),pc.PlayerInput) and not _bindings.refresh
            and _bindings.configuration()==_bindings.config):
        return _bindings.native
    # Once generation-3 metadata has captured the factory baseline, it is safe
    # to edit against that cached baseline even while Custom Bindings is off.
    try:
        saved=preferences.state(runtime_state.value)
        if saved.get('version')==3 and saved.get('baseline'):
            return saved['baseline']
    except Exception:
        pass
    return None


def _store(choices,saved):
    global _choices_cache,_preference_generation
    before=_choices();old=copy.deepcopy(runtime_state.value)
    try:
        for c,group in BINDINGS.items():
            for key,option in group.items():option.value=choices[c][key]
        runtime_state.value=saved
        _save_settings()
        _invalidate_display()
        _clear_context()
        _choices_cache=None;_preference_generation=-1
        _bindings.invalidate_layout()
        if _active and not _syncing:_request_binding_sync()
    except Exception:
        for c,group in BINDINGS.items():
            for key,option in group.items():option.value=before[c][key]
        runtime_state.value=old
        raise


def _prepare_preferences(baseline):
    if _loading_settings:return
    saved=preferences.state(runtime_state.value)
    if saved.get('version')==3 and saved.get('baseline')==baseline and not saved['pending']:return
    choices,updated=preferences.finalize(_choices(),saved,baseline)
    _store(choices,updated)


def _edit(context,action,value):
    baseline=_configuration_baseline()
    saved=preferences.state(runtime_state.value)
    if baseline is None:
        saved=preferences.queue(saved,context,action,value,bool(auto_swap.value))
        _store(_choices(),saved)
    else:
        choices,saved=preferences.finalize(_choices(),saved,baseline)
        choices=preferences.apply_edit(choices,baseline,context,action,value,bool(auto_swap.value))
        saved.update(version=3,baseline=dict(baseline),pending=[])
        _store(choices,saved)
    _binding_descriptions()


def _start_capture(context,key):
    try:
        _capture.start(context,key,BINDINGS[context][key].action_label,_side_panel.releases)
        _remember_panel_modal(_capture.dialog)
    except Exception as exc:_warn('capture_unavailable',f'Capture unavailable; preferences unchanged: {exc}')


def _refresh_binding_rows():
    _binding_descriptions()
    # These mappings are displayed through our separate binding Flash clip,
    # not the stock CLiK options list. Update THAT clip after modal capture.
    _pane_redraw_current()


def _reset(context: str) -> None:
    _edit(context,'reset','Default')
    _refresh_binding_rows()


def _panel_change_native(context, key, value):
    # Use the same transactional edit/restore path as the original Customizer.
    _edit(context, key, value)
    _refresh_binding_rows()


def _short_mod_label(name):
    # Preserve the actual name. The old 21-character ellipsis was artificial.
    return ' '.join(str(name).split())


def _panel_category(identifier,title,description):
    option=ButtonOption(identifier='panel_'+identifier,display_name=title,
                        description=description,
                        on_press=lambda opt,key=identifier:_pane_enter(key))
    option._cm_category=identifier
    option.mod=mod
    return option


def _panel_adjust_sensitivity(option,direction):
    choices=list(option.choices)
    try:index=choices.index(option.value)
    except ValueError:return
    new_value=choices[max(0,min(index+direction,len(choices)-1))]
    if new_value!=option.value:
        # The proven sensitivity handler owns save/rollback and live look hooks.
        _sensitivity_changed(option,new_value)
        _pane_redraw_current()


def _manager_provider(movie=None):
    from willow2_mod_menu import options_menu
    if not options_menu.data_provider_stack:
        return None
    provider=options_menu.data_provider_stack[-1]
    if not isinstance(provider,ManagerOptionsProvider) or provider.mod is not mod:
        return None
    owner=provider.movie()
    if owner is None or movie is not None and not _same(owner,movie):
        return None
    pc=get_pc(possibly_loading=True)
    return provider if pc is not None and _same(pc,owner.WPCOwner) else None


def _our_panel_movie(movie):
    return movie is not None and _manager_provider(movie) is not None


def _pane_provider_push(obj,args,ret,func):
    from willow2_mod_menu import options_menu
    from willow2_mod_menu.data_providers.mod_options import ModOptionsDataProvider
    if not options_menu.data_provider_stack:
        return None
    provider=options_menu.data_provider_stack[-1]
    if not isinstance(provider,ModOptionsDataProvider) or provider.mod is not mod:
        return None
    native=args.DataProvider
    if native is None or str(native.Class.Name)!='WillowScrollingListDataProviderKeyboardMouseOptions':
        return None
    adapt_provider(provider,native,obj,_pane_preview,_pane_populate_keys)
    _native_view.close()
    _side_panel.reset_focus()
    return None


def _pane_selected_category(movie):
    provider=_manager_provider(movie)
    if provider is None:return None
    index=int(movie.TheList.GetSelectedIndex())
    if 0<=index<len(provider.drawn_options):
        return getattr(provider.drawn_options[index],'_cm_category',None)
    return None


def _pane_description(movie):
    if movie.OptionsObj is None:return
    _native_view.update_tip(_side_panel)
    if _side_panel.focus=='left':
        if _side_panel.section in _side_panel.sections:
            title=_side_panel.sections[_side_panel.section][0]
            _native_view.describe(movie,('Edit '+title+' controls.' if _side_panel.section in ('foot','vehicle')
                                        else 'Controller shortcuts for '+title+'.'))
        return
    row=_side_panel.selected()
    if row is None:return
    message=row.help or row.label
    if row.kind=='binding':
        if not _active:
            message+=' | Saved settings activate when Controller Manager is enabled.'
            if _bindings.drain_issue:
                message+=' | '+_bindings.drain_issue
        elif not layouts_enabled.value:
            message+=' | Turn Custom Bindings on to apply this layout.'
        elif _display_snapshot()['baseline'] is None:
            message+=' | Saved; activates after loading a character.'
        elif _bindings._sync_pending:
            message+=' | Saved; release held buttons to activate.'
    _native_view.describe(movie,message)


def _pane_draw(movie,preview=None):
    global _panel_updating,_panel_value_stamp
    if _panel_updating:return
    provider=_manager_provider(movie)
    if provider is None:return
    if _side_panel.focus=='left':
        _side_panel.choose(preview if preview is not None else _pane_selected_category(movie))
    _panel_updating=True
    try:
        stamp=(_display_stamp(),_panel_values_revision)
        if _side_panel.section != _native_view._section or stamp != _panel_value_stamp:
            _native_view.render(movie,_side_panel,provider)
            _panel_value_stamp=stamp
        _pane_description(movie)
    except Exception as exc:
        _warn('panel_draw',f'Binding panel: {type(exc).__name__}: {exc}')
        if movie.OptionsObj is not None:
            movie.OptionsObj.SetDescription('Binding panel: '+str(exc))
    finally:
        _panel_updating=False


def _pane_preview(movie,category):
    # A provider click also fires from Flash focus/selection callbacks. Only
    # the actual left-list mouse handler or B/Left may transfer focus left.
    if (_side_panel.focus!='left' or _panel_unbinding or _panel_updating
            or _native_view._drawing or _panel_modal()
            or _native_view._restore_focus is not None):return
    _native_view.focus_left(movie)
    _pane_draw(movie,category)


def _pane_anchor_category(movie):
    provider=_manager_provider(movie)
    if provider is None:return
    for index,option in enumerate(provider.drawn_options):
        if getattr(option,'_cm_category',None)==_side_panel.section:
            if int(movie.TheList.GetSelectedIndex())!=index:
                movie.TheList.SetSelectedIndex(index)
            break


def _pane_enter(section):
    provider=_manager_provider()
    if provider is None:return
    movie=provider.movie()
    _side_panel.focus='left'
    _side_panel.choose(section)
    _pane_draw(movie,section)
    _pane_anchor_category(movie)
    try:
        _native_view.focus_right(movie,_side_panel.indexes.get(section,0))
        _side_panel.choose(section,enter=True)
        _pane_description(movie)
    except Exception as exc:
        _side_panel.focus='left'
        _native_view.focus_left(movie)
        _warn('panel_focus',str(exc))
        movie.OptionsObj.SetDescription(str(exc))


def _pane_flash_owned(native):
    provider=_manager_provider()
    if provider is None or not _same(native,provider.native()):return None
    return provider.movie()


def _pane_populate_keys(native):
    movie=_pane_flash_owned(native)
    if movie is not None:
        _native_view.repopulate(movie,_side_panel,_manager_provider(movie))


def _panel_modal():
    return (_capture.dialog is not None or _sdk_dialog.dialog is not None
            or _sdk_reset.dialog is not None)


def _pane_flash_click(obj,args,ret,func):
    movie=_pane_flash_owned(obj)
    if movie is None:return None
    if _panel_modal():return Block
    index=_native_view.index_for_tag(args.Tag)
    if index is None:return Block
    _pane_anchor_category(movie)
    _side_panel.indexes[_side_panel.section]=index
    _side_panel.focus='right'
    _native_view.find_receiver(movie)
    _pane_draw(movie)
    _side_panel.activate()
    return Block


def _pane_native_do_bind(obj,args,ret,func):
    # Native row identities/indexes are valid, but keyboard capture must never
    # edit our controller settings or the game's keyboard profile.
    if _pane_flash_owned(obj) is not None:return Block
    return None


def _pane_flash_selection(obj,args,ret,func):
    movie=_pane_flash_owned(obj)
    if movie is None or _panel_updating or _native_view._drawing or _panel_modal():return
    index=_native_view.index_for_tag(args.Tag)
    if index is not None:
        if _side_panel.focus!='right':
            pc=get_pc(possibly_loading=True)
            if pc is None or pc.PlayerInput is None or pc.PlayerInput.bUsingGamepad:return
            # A real mouse rollover uses the panel it points at. Programmatic
            # row population is excluded by _drawing above.
            _pane_anchor_category(movie)
            _side_panel.focus='right'
            try:_native_view.focus_right(movie,index)
            except Exception as exc:
                _side_panel.focus='left'
                _native_view.focus_left(movie)
                _warn('panel_mouse_focus',str(exc))
                return
        _side_panel.indexes[_side_panel.section]=index
        _native_view.selection_changed(movie,index)
        _pane_description(movie)
    # POST observation: native FindBinding and selection tracking remain intact.


def _pane_category_selection(obj,args,ret,func):
    movie=_pane_flash_owned(obj)
    if movie is None:return None
    if _panel_unbinding or _panel_updating or _native_view._drawing or _panel_modal():return (Block,True)
    # Inactive left-list focus events must not hide the active right clip.
    if _side_panel.focus=='right':return (Block,True)
    provider=_manager_provider(movie)
    category=provider.category(args.EventID)
    if category is not None:
        _pane_draw(movie,category)
        return (Block,True)
    _side_panel.choose(None)
    _native_view.hide()
    return None


def _pane_unbind_row(movie,row):
    """A direct unbind retains its right-panel row through the input dispatch."""
    global _panel_unbinding
    section=_side_panel.section
    index=_side_panel.indexes.get(section,0)
    previous=_panel_unbinding
    _panel_unbinding=True
    try:
        row.clear()
    finally:
        try:
            if _our_panel_movie(movie) and _side_panel.section==section and not _panel_modal():
                _side_panel.focus='right'
                _side_panel.indexes[section]=index
                _native_view.focus_right(movie,index)
        finally:_panel_unbinding=previous


def _pane_input(obj,args,ret,func):
    if not _our_panel_movie(obj):return None
    pc=get_pc(possibly_loading=True)
    if pc is None or int(args.ControllerId)!=int(pc.GetMyControllerId()):return None
    raw=str(args.ukey).casefold()
    event=args.uevent
    if _sdk_dialog.gate.intercept(obj,raw,event):return (Block,True)
    if _capture.gate.intercept(obj,raw,event):return (Block,True)
    if _sdk_reset.gate.intercept(obj,raw,event):return (Block,True)
    if _panel_modal():return None
    if raw in _side_panel.releases:
        if event==EInputEvent.IE_Released:
            _side_panel.releases.discard(raw)
            if raw=='xboxtypes_x' and _side_panel.focus=='right':
                row=_side_panel.selected()
                if row is not None and row.clear is not None:
                    # Finish after the closing X release, not two ticks after
                    # its press while the button may still be held.
                    _native_view.restore_after_refresh(obj,
                        _side_panel.indexes.get(_side_panel.section,0),_pane_description)
        if event!=EInputEvent.IE_Repeat:return (Block,True)
    if event not in (EInputEvent.IE_Pressed,EInputEvent.IE_Repeat):return None
    button={
        'xboxtypes_dpad_up':'up','xboxtypes_dpad_down':'down',
        'xboxtypes_dpad_right':'right','xboxtypes_dpad_left':'left',
        'xboxtypes_a':'accept','xboxtypes_b':'back',
        'xboxtypes_x':'unbind','xboxtypes_y':'restore',
        'gamepad_leftstick_up':'up','gamepad_leftstick_down':'down',
        'gamepad_leftstick_right':'right','gamepad_leftstick_left':'left',
        'up':'up','down':'down','right':'right','left':'left',
        'return':'accept','enter':'accept','escape':'back',
    }.get(raw)
    if button is None:return None
    if _side_panel.focus=='left':
        section=_pane_selected_category(obj)
        if section is None or button not in ('accept','right'):return None
        if event==EInputEvent.IE_Pressed:
            _side_panel.releases.add(raw)
            _pane_enter(section)
        return (Block,True)
    if button in ('up','down'):
        return None  # Only the genuinely focused native right list receives it.
    row=_side_panel.selected()
    if event==EInputEvent.IE_Pressed:_side_panel.releases.add(raw)
    if button in ('left','right') and row is not None and row.adjust is not None:
        global _panel_last_repeat
        now=time.monotonic()
        if event!=EInputEvent.IE_Repeat or now-_panel_last_repeat>=0.13:
            _panel_last_repeat=now
            _side_panel.adjust(-1 if button=='left' else 1)
    elif event==EInputEvent.IE_Pressed:
        if button in ('back','left'):
            _side_panel.focus='left'
            _native_view.focus_left(obj)
        elif button=='accept':
            _side_panel.activate()
        elif button=='unbind' and row is not None and row.clear is not None:
            _pane_unbind_row(obj,row)
        elif button=='restore' and row is not None and row.restore is not None:
            row.restore()
    _side_panel.releases.add(raw)
    if not _panel_modal():_pane_description(obj)
    return (Block,True)


def _pane_mouse_left(obj,args,ret,func):
    movie=obj.MyOwnerMovie
    if not _our_panel_movie(movie) or not _same(obj,movie.TheList):return None
    event=str(getattr(args.Data,'Type',''))
    if event not in ('itemClick','itemRollOver'):return None
    if (_panel_unbinding or _panel_updating or _native_view._drawing or _panel_modal()
            or _native_view._restore_focus is not None
            or (_side_panel.focus=='right' and 'xboxtypes_x' in _side_panel.releases)):
        # Returning None here lets native OnClikEvent call handle_click anyway.
        return Block
    pc=get_pc(possibly_loading=True)
    if pc is not None and pc.PlayerInput is not None and not pc.PlayerInput.bUsingGamepad:
        _side_panel.focus='left'
        _native_view.focus_left(movie)
    return None


def _pane_selection(obj,args,ret,func):
    if _our_panel_movie(obj) and _side_panel.focus=='left':
        _pane_draw(obj)


def _pane_closed(obj,args,ret,func):
    global _panel_modal_return
    if not _native_view.belongs_to(obj):return
    _panel_modal_return=None
    _native_view.close()
    _side_panel.reset_focus()
    _sdk_dialog.stop()
    _capture.cancel()
    _sdk_reset.stop()


def _pane_popped(obj,args,ret,func):
    global _panel_modal_return
    # SDK OnPop can already have removed its Python provider. Verify native
    # ownership directly so a later mod page never inherits our Flash state.
    if not _native_view.belongs_to_provider(obj):return
    _panel_modal_return=None
    _native_view.close()
    _side_panel.reset_focus()
    _sdk_dialog.stop()
    _capture.cancel()
    _sdk_reset.stop()


def _pane_redraw_current():
    provider=_manager_provider()
    if provider is not None:_pane_draw(provider.movie())


def _sdk_update_rows():
    # Unlike the old NestedOption pages, the right-side action model is not a
    # CLiK data-provider row. Avoid invalidating the stock left list.
    _pane_redraw_current()


def _remember_panel_modal(dialog):
    global _panel_modal_return
    provider=_manager_provider()
    target=dialog() if dialog is not None else None
    if provider is not None and target is not None:
        _panel_modal_return=(target._get_address(),provider.movie,_side_panel.section,
                             _side_panel.indexes.get(_side_panel.section,0))


def _pane_modal_closed(obj,args,ret,func):
    global _panel_modal_return
    pending=_panel_modal_return
    closed=getattr(args,'Movie',None)
    if pending is None or closed is None or closed._get_address()!=pending[0]:return
    _panel_modal_return=None
    provider=_manager_provider()
    if provider is not None and _same(obj,provider.movie().WPCOwner.GFxUIManager) and not _panel_modal():
        _pane_redraw_current()
        if _side_panel.focus=='right' and _side_panel.section==pending[2] and _same(pending[1](),provider.movie()):
            _native_view.restore_after_modal(provider.movie(),pending[3],_pane_description)


def _sdk_refresh():
    global _sdk_actions_cache
    try:
        from mods_base import get_ordered_mod_list
        actions,duplicates=discover(get_ordered_mod_list(),mod)
        _sdk_actions_cache=actions
        _sdk_dispatch.configure(actions,_sdk_store)
        if duplicates:
            _warn('sdk_duplicates',
                  'Ambiguous SDK actions suppressed (all matching owners); '
                  'saved shortcuts retained until the collision is removed: '
                  + ', '.join(duplicates[:3])
                  + (' ...' if len(duplicates)>3 else ''))
    except Exception as exc:
        _warn('sdk_discovery',f'SDK catalog refresh: {exc}')


def _sdk_travel(obj,args,ret,func):
    pc=get_pc(possibly_loading=True)
    if pc is not None and _same(obj,pc):_sdk_dispatch.cancel()


def _sdk_controller_changed(obj,args,ret,func):
    pc=get_pc(possibly_loading=True)
    if pc is not None and _same(obj,pc) and int(args.ControllerId)==int(pc.GetMyControllerId()):
        _sdk_dispatch.cancel()
        _sdk_dialog.stop()
        _capture.cancel()
        _sdk_reset.stop()


def _sdk_input(obj,args,ret,func):
    try:
        return _sdk_physical_input(obj,args,ret,func)
    except Exception as exc:
        # A shortcut error must not stop the independent native remapping core.
        _warn('sdk_input',f'SDK shortcut input: {type(exc).__name__}: {exc}')
        _sdk_dispatch.cancel()
        return None


def _sdk_physical_input(obj,args,ret,func):
    global _sdk_input_owner
    if not (_sdk_dispatch.lookup or _sdk_dispatch.active
            or _sdk_dispatch.modifiers or _sdk_dispatch.draining):return None
    pc=get_pc(possibly_loading=True)
    if pc is None:
        _sdk_dispatch.cancel()
        _sdk_input_owner=None
        return None
    player=getattr(pc,'Player',None)
    if not _same(obj,getattr(player,'ViewportClient',None)):return None
    if int(args.ControllerId)!=int(pc.GetMyControllerId()):return None
    pi=pc.PlayerInput
    owner=(pc._get_address(),pi._get_address() if pi is not None else None)
    owner_changed=owner!=_sdk_input_owner
    if owner_changed:
        _sdk_dispatch.cancel()
        _sdk_input_owner=owner
    physical=button_name(args.Key)
    event=args.EventType
    if physical is None:
        if (event==EInputEvent.IE_Pressed and not bool(getattr(args,'bGamepad',False))
                and (_sdk_dispatch.active or _sdk_dispatch.modifiers)):
            _sdk_dispatch.cancel()  # Keyboard/mouse takes over; cancel deferred menu taps.
        return None
    if owner_changed:
        # Cancelling a held SDK action can invoke its release callback. Refresh
        # only when readiness is needed; unrelated input keeps its early exit.
        pi=pc.PlayerInput
    ready=_sdk_input_ready(pc,pi)
    def tap(modifier):
        # Exported BL2 entrypoints retain their normal native eligibility checks.
        # Replay the action once rather than recursively injecting input.
        if modifier=='Start':pc.ShowPauseMenu()
        else:pc.ShowStatusMenu()
    if _sdk_dispatch.input(physical,event,ready,tap):return (Block,True)
    return None


def _sdk_input_ready(pc,pi):
    # This is an event-local measurement boundary, never a readiness cache.
    return (_active and pc.Pawn is not None and pi is not None
            and not _ui_blocked() and not bool(getattr(pc,'bCinematicMode',False))
            and not pc.IsLoadingMoviePlaying())


def _commit_sdk_binding(key,shortcut):
    global _sdk_store,_panel_values_revision
    try:
        staged=Assignments(_sdk_store.json())
        conflict=staged.change(key,shortcut)
        if conflict:
            labels={action.key:action.mod_label+' / '+action.label for action in _sdk_actions_cache}
            message='Already assigned to '+', '.join(labels.get(item,item) for item in conflict[:3])+'. Clear that shortcut first.'
            _warn('sdk_shortcut_conflict',message)
            from ui_utils import TrainingBox
            TrainingBox(title='Shortcut conflict',message=message).show()
            return
        old=sdk_assignments.value
        sdk_assignments.value=staged.json()
        try:_save_settings()
        except Exception:
            sdk_assignments.value=old
            raise
        _sdk_store=staged
        _panel_values_revision+=1
        _sdk_dispatch.configure(_sdk_actions_cache,_sdk_store)
        _sdk_update_rows()
    except Exception as exc:_warn('sdk_commit',f'SDK binding not saved: {type(exc).__name__}: {exc}')


def _start_sdk_capture(action):
    try:
        _sdk_dialog.start(action.key,action.label,_side_panel.releases)
        _remember_panel_modal(_sdk_dialog.dialog)
    except Exception as exc:_warn('sdk_capture_start',f'SDK binding dialog unavailable: {type(exc).__name__}: {exc}')


class ResetDialog:
    """One reusable, confirmation-only dialog for every reset scope."""
    def __init__(self):
        self.dialog=None;self.hooks=[];self.scope=None
        self.gate=ParentInputGate()
        self.last_key=None
        self.last_event=None
        self.opening_keys=set()

    def start(self,scope):
        self.stop()
        descriptions={
            'foot':('Reset Walking Settings','Reset Walking bindings and sensitivities?'),
            'vehicle':('Reset Driving Settings','Reset Driving bindings and sensitivities?'),
            'all':('Reset All Controller Settings',
                   'Reset Walking, Driving, sensitivity and ALL mod controller shortcuts?')}
        if scope.startswith('sdk:'):
            group_id=scope[4:]
            section_id='sdk_'+hashlib.sha1(group_id.encode('utf-8')).hexdigest()[:16]
            labels=[title for section,(title,rows) in _side_panel.sections.items()
                    if section==section_id]
            title=labels[0] if labels else 'this SDK mod'
            heading='Reset '+title+' Bindings'
            message='Remove only '+title+' controller shortcuts?'
        else:
            heading,message=descriptions[scope]
        pc=get_pc(possibly_loading=True)
        if pc is None:raise RuntimeError('No local menu player')
        self.scope=scope
        provider=_manager_provider()
        self.gate.begin(provider.movie if provider is not None else None,_side_panel.releases)
        self.opening_keys=set(_side_panel.releases)
        try:
            for path,kind,tag,cb in (
                ('WillowGame.WillowGFxDialogBox:HandleInputKey',Type.PRE,'input',self.input),
                ('WillowGame.WillowGFxDialogBox:Accepted',Type.PRE,'accept',self.accept),
                ('WillowGame.WillowGFxDialogBox:Cancelled',Type.PRE,'cancel',self.cancel),
                ('WillowGame.WillowGFxUIManager:Movie_OnClosed',Type.POST_UNCONDITIONAL,'closed',self.closed)):
                fn=unrealsdk.find_object('Function',path)
                name=fn._path_name();ident='ControllerManager:reset:'+tag
                unrealsdk.hooks.add_hook(name,kind,ident,cb)
                self.hooks.append((name,kind,ident))
            self.dialog=WeakPointer(pc.GFxUIManager.ShowDialog())
            _remember_panel_modal(self.dialog)
            d=self.dialog()
            d.SetText(heading,'Are you sure?\n'+message)
            d.AppendButton('CM:confirm','Confirm','')
            d.AppendButton('CM:cancel','Cancel','')
            d.SetDefaultButton('CM:cancel',True)
            d.ApplyLayout()
        except Exception:
            self.stop();raise

    def input(self,obj,args,ret,func):
        if not self.dialog or not _same(obj,self.dialog()):return None
        raw=str(args.ukey).casefold()
        if raw in self.opening_keys:
            if args.uevent==EInputEvent.IE_Released:
                self.opening_keys.discard(raw)
                self.gate.released(raw)
            return (Block,True)
        self.last_key=raw
        self.last_event=args.uevent
        if args.uevent==EInputEvent.IE_Pressed:self.gate.track(raw)
        elif args.uevent==EInputEvent.IE_Released:self.gate.released(raw)
        return None

    def accept(self,obj,args,ret,func):
        if self.dialog and _same(obj,self.dialog()):
            scope=self.scope
            if int(obj.CurrentSelection)==0:
                try:_commit_confirmed_reset(scope)
                except Exception as exc:_warn('reset_error',f'Reset failed: {exc}')
            self.gate.finish(self.last_key,released=self.last_event==EInputEvent.IE_Released)
            self.stop(close=False,retain_gate=True)
            _pane_redraw_current()
        return None

    def cancel(self,obj,args,ret,func):
        if self.dialog and _same(obj,self.dialog()):
            self.gate.finish(self.last_key,released=self.last_event==EInputEvent.IE_Released)
            self.stop(close=False,retain_gate=True)
            _pane_redraw_current()
        return None

    def closed(self,obj,args,ret,func):
        if self.dialog and _same(getattr(args,'Movie',None),self.dialog()):self.stop(close=False)
        return None

    def stop(self,close=True,retain_gate=False):
        if not retain_gate:self.gate.reset()
        obj=self.dialog() if self.dialog else None
        self.dialog=None;self.scope=None
        self.last_key=None;self.opening_keys.clear()
        self.last_event=None
        for path,kind,ident in tuple(self.hooks):
            try:unrealsdk.hooks.remove_hook(path,kind,ident)
            except Exception as exc:_warn('reset_cleanup',f'Reset cleanup: {exc}')
            else:self.hooks.remove((path,kind,ident))
        if close and obj is not None:
            try:obj.Close()
            except Exception:pass


def _confirm_reset(scope):
    try:_sdk_reset.start(scope)
    except Exception as exc:_warn('reset_dialog',f'Unable to show reset confirmation: {exc}')


def _commit_confirmed_reset(scope):
    """Commit a single reset scope with a single settings save and rollback.

    A native baseline may not be resident at the front end. In that case only
    the relevant reset edits are queued, retaining unrelated pending changes.
    """
    global _sdk_store,_choices_cache,_preference_generation,_panel_values_revision
    if scope not in ('foot','vehicle','all') and not scope.startswith('sdk:'):
        raise ValueError('Invalid reset scope')
    all_reset=scope=='all'
    contexts=('foot','vehicle') if all_reset else ((scope,) if scope in ('foot','vehicle') else ())
    staged_sdk=Assignments(_sdk_store.json())
    if all_reset:
        staged_sdk.clear()
    elif scope.startswith('sdk:'):
        prefix=scope[4:]+'#'
        for key in tuple(staged_sdk.saved):
            if key.startswith(prefix):staged_sdk.saved.pop(key,None)

    choices=_choices()
    metadata=preferences.state(runtime_state.value)
    if contexts:
        baseline=_configuration_baseline()
        if baseline is not None:
            choices,metadata=preferences.finalize(choices,metadata,baseline)
            for context in contexts:
                choices=preferences.apply_edit(choices,baseline,context,'reset','Default',False)
            metadata.update(version=3,baseline=dict(baseline),pending=[])
        else:
            for context in contexts:
                metadata=preferences.queue(metadata,context,'reset','Default',False)

    previous_choices=_choices()
    previous_runtime=copy.deepcopy(runtime_state.value)
    previous_sdk=sdk_assignments.value
    previous_sensitivity={key:opt.value for key,opt in SENSITIVITY.items()}
    previous_controls=(layouts_enabled.value,auto_swap.value)
    try:
        for context in contexts:
            for key,option in BINDINGS[context].items():
                option.value=choices[context][key]
            sensitive=('hip_x','hip_y','ads_x','ads_y') if context=='foot' else ('vehicle_x','vehicle_y')
            for key in sensitive:object.__setattr__(SENSITIVITY[key],'value','1.00')
        runtime_state.value=metadata
        sdk_assignments.value=staged_sdk.json()
        if all_reset:
            object.__setattr__(layouts_enabled,'value',False)
            object.__setattr__(auto_swap,'value',True)
        _save_settings()
    except Exception:
        for context in ('foot','vehicle'):
            for key,option in BINDINGS[context].items():
                option.value=previous_choices[context][key]
        runtime_state.value=previous_runtime
        sdk_assignments.value=previous_sdk
        for key,value in previous_sensitivity.items():object.__setattr__(SENSITIVITY[key],'value',value)
        object.__setattr__(layouts_enabled,'value',previous_controls[0])
        object.__setattr__(auto_swap,'value',previous_controls[1])
        raise
    _sdk_store=staged_sdk
    _panel_values_revision+=1
    _invalidate_display()
    _sdk_dispatch.configure(_sdk_actions_cache,_sdk_store)
    if all_reset and _active:
        global _restart_rebind_armed,_restart_rebind_ready,_restart_rebind_started
        global _travel_resume_armed,_travel_resume_pending,_travel_resume_running
        _restart_rebind_armed=False;_restart_rebind_ready=0;_restart_rebind_started=0.0
        _travel_resume_armed=False;_travel_resume_pending=False;_travel_resume_running=False
        _configure_restart_recovery()
        _configure_physical_input_hook()
    if contexts:
        global _look_values
        _look_values=None
        _clear_context()
        _choices_cache=None;_preference_generation=-1
        _bindings.invalidate_layout()
        if _active and not _syncing:_request_binding_sync()
        if _active:
            try:_configure_look_hooks(_cached_pc())
            except Exception as exc:_warn('reset_look',f'Look settings refresh pending: {exc}')
    _refresh_binding_rows()


_bindings=BindingRuntime(get_pc,_log,_warn,_notify_deadzone_input_rebuilt,_notify_binding_consumers,
                         input_rebuild_failed=_notify_deadzone_input_rebuild_failed)


def _retry_pending_binding_restore(_option):
    """Explicit retry, still gated on two natural released-input boundaries."""
    if _active:
        _warn('binding_drain_retry',
              'Disable Controller Manager before retrying a pending binding restore.')
        return
    if _bindings.drain_hook is not None:
        _warn('binding_drain_retry',
              'Binding restoration is already waiting for released input boundaries.')
        return
    if not (_bindings.applied or _bindings.latent_applied is not None
            or _bindings.override_active or _bindings.compat_applied is not None):
        _log('No pending binding restoration remains')
        return
    try:
        _bindings.disable()
        _log('Binding restoration rearmed; release gameplay buttons for two input boundaries')
    except Exception as exc:
        _warn('binding_drain_retry',
              f'Binding cleanup retry unavailable: {type(exc).__name__}: {exc}')
    finally:
        _binding_descriptions()
        _pane_redraw_current()


_capture=ButtonCapture(_edit,_refresh_binding_rows,_warn,_log)
runtime_state=HiddenOption(identifier='layout_state',value={})
sdk_assignments=HiddenOption(identifier='sdk_assignments',value=Assignments().json())
_sdk_dialog=AssignmentDialog(_commit_sdk_binding, lambda message:_warn('sdk_dialog',message))
_sdk_reset=ResetDialog()
_sdk_actions_cache=()
auto_swap=BoolOption(identifier='auto_swap',value=True,display_name='Auto-Swap Conflicts',
    description='When two actions share a button, move the displaced action to the previous button. Otherwise leave it Unbound.')
layouts_enabled=BoolOption(identifier='layouts_enabled',value=False,display_name='Use Custom Bindings',
    description='Use separate Walking and Driving layouts. Menu controls stay unchanged; changes wait for held buttons to release.',
    on_change_anytime=_layouts_changed)
BINDINGS={context:{key:BindingOption(identifier=key,value='Default',
    display_name=_context_action_label(context,key),is_hidden=False,
    description='Press A or Enter to choose a controller button for this action.',
    on_press=lambda option,c=context,k=key:_start_capture(c,k))
    for key in KEYS} for context in ('foot','vehicle')}
_SENS_DESCRIPTIONS={
    ('hip','x'):'Adjust horizontal controller sensitivity while not aiming down sights.',
    ('hip','y'):'Adjust vertical controller sensitivity while not aiming down sights.',
    ('ads','x'):'Adjust horizontal controller sensitivity while aiming down sights.',
    ('ads','y'):'Adjust vertical controller sensitivity while aiming down sights.',
    ('vehicle','x'):'Adjust horizontal controller sensitivity while driving.',
    ('vehicle','y'):'Adjust vertical controller sensitivity while driving.',
}
SENSITIVITY={f'{prefix}_{axis}':SensitivityOption(identifier=f'{prefix}_{axis}',value='1.00',
    choices=SENSITIVITY_CHOICES,wrap_enabled=False,is_hidden=False,
    display_name=f'{label} {"Horizontal" if axis=="x" else "Vertical"} Sensitivity',
    on_change_anytime=_sensitivity_changed,description=_SENS_DESCRIPTIONS[(prefix,axis)])
    for prefix,label in (('hip','Hip'),('ads','ADS'),('vehicle','Look')) for axis in ('x','y')}
from mods_base import SETTINGS_DIR
from ._settings_migration import migrate_settings
migrate_settings(SETTINGS_DIR)
mod=build_mod(cls=ControllerManagerMod,version_info_parser=lambda version: tuple(int(part) for part in version.split("-", 1)[0].partition("rc")[0].split(".")),settings_file=SETTINGS_DIR / "ControllerManager.json",options=[
    GroupedOption(identifier='capabilities',display_name='Controls',children=[
        layouts_enabled,auto_swap,
        ButtonOption(identifier='retry_pending_restore',
            display_name='Retry Pending Binding Restore',
            description=('Only when Controller Manager is Off: retry an interrupted '
                         'binding restore after two natural released-input boundaries. '
                         'Never forces held buttons or overwrites foreign changes. '
                         'If input callbacks stop entirely, restart the game.'),
            on_press=_retry_pending_binding_restore)]),
    GroupedOption(identifier='foot',display_name='Walking',children=[
        ButtonOption(identifier='reset_layout',display_name='Reset Walking Settings',
            description='Reset Walking bindings and sensitivity after confirmation.',on_press=lambda option:_confirm_reset('foot')),
        *[SENSITIVITY[k] for k in ('hip_x','hip_y','ads_x','ads_y')],*BINDINGS['foot'].values()]),
    GroupedOption(identifier='vehicle',display_name='Driving',children=[
        ButtonOption(identifier='reset_layout',display_name='Reset Driving Settings',
            description='Reset Driving bindings and sensitivity after confirmation.',on_press=lambda option:_confirm_reset('vehicle')),
        SENSITIVITY['vehicle_x'],SENSITIVITY['vehicle_y'],*BINDINGS['vehicle'].values()]),
    runtime_state,sdk_assignments],on_enable=_enable,on_disable=_disable)
_binding_descriptions()

# Display/edit bindings even with Controller Manager's gameplay master switch OFF.
_install_ui_hooks()
