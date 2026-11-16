"""Object-scoped native button dialogs; no gameplay input rebuilds or priority edits."""
from __future__ import annotations
import time
import unrealsdk
from mods_base import EInputEvent, get_pc
from unrealsdk.hooks import Type, Block
from unrealsdk.unreal import WeakPointer
from .layout import KEYS, canonical
from .input_gate import ParentInputGate


from .object_utils import same_native as same


def function_path(struct,name):
    """Use the actual function owner, including inherited implementations."""
    for parent in struct._superfields():
        try:fn=parent._find(name)
        except (AttributeError,ValueError):continue
        if str(fn.Class.Name)=='Function' and str(fn.Name).casefold()==name.casefold():
            return fn._path_name()
    raise ValueError(f'{struct.Name}.{name}: reflected function unavailable')


def menu_context():
    from willow2_mod_menu import options_menu
    return options_menu.latest_list(), options_menu.data_provider_stack[-1] if options_menu.data_provider_stack else None


class ButtonCapture:
    def __init__(self,commit,refresh,warn,log=lambda message:None):
        self.commit=commit;self.refresh=refresh;self.warn=warn
        self.log=log;self.reported=False
        self.dialog=self.parent=self.manager=self.owner=None
        self.pawn=None
        self.hooks=[];self.context=self.action=None;self.mode=''
        self.pressed=set();self.running=set();self.opening=False
        self.action_back=False;self.started=0.0
        self.gate=ParentInputGate()
        self.opening_keys=set()
        self.last_key=None
        self.last_event=None

    def hook(self,path,kind,tag,callback):
        fn=unrealsdk.find_object('Function',path)
        if str(fn.Class.Name)!='Function': raise ValueError(f'Not a function: {path}')
        if tag=='input':
            params={str(p.Name).casefold():str(p.Class.Name) for p in fn._properties() if int(p.PropertyFlags)&0x80}
            if params.get('ukey') not in ('StrProperty','NameProperty') or params.get('uevent')!='ByteProperty':
                raise ValueError('Dialog key/event signature unverified')
        ident=f'ControllerManager:capture:{tag}'
        if unrealsdk.hooks.has_hook(path,kind,ident):raise RuntimeError('Capture hook already active')
        def guarded(obj,args,ret,func):
            if tag in self.running:return None
            self.running.add(tag)
            try:return callback(obj,args,ret,func)
            except Exception as exc:
                self.warn('capture_failure',f'Binding capture cancelled: {type(exc).__name__}: {exc}')
                self.cancel(close=tag not in ('accepted','cancelled','closed'))
                return (Block,True) if tag=='input' else None
            finally:self.running.discard(tag)
        try:unrealsdk.hooks.add_hook(path,kind,ident,guarded)
        finally:
            if unrealsdk.hooks.has_hook(path,kind,ident):self.hooks.append((path,kind,ident))
        if not unrealsdk.hooks.has_hook(path,kind,ident):raise RuntimeError('Capture hook registration failed')

    def start(self,context,action,label,opening_keys=()):
        self.cancel()
        pc=get_pc(possibly_loading=True)
        if pc is None:raise ValueError('Local menu player controller unavailable')
        self.owner=WeakPointer(pc);self.manager=WeakPointer(pc.GFxUIManager)
        self.context=context;self.action=action;self.pressed.clear()
        pawn=getattr(pc,'Pawn',None)
        self.pawn=WeakPointer(pawn) if pawn is not None else None
        self.mode='capture';self.started=time.monotonic();self.opening=True
        self.opening_keys=set(opening_keys)
        try:
            the_list,_=menu_context()
            self.parent=WeakPointer(the_list.MyOwnerMovie) if the_list is not None else None
            self.gate.begin(self.parent,opening_keys)
            loading=function_path(pc.Class,'WillowClientDisableLoadingMovie')
            try:
                pawn_class=pawn.Class if pawn is not None else unrealsdk.find_class('WillowPlayerPawn')
                lifecycle=function_path(pawn_class,'PostBeginPlay')
                lifecycle_tag='spawned';lifecycle_callback=self.world
            except (AttributeError,ValueError):
                # Some front-end states cannot reflect a pawn class yet. Watch
                # only the menu player's native input boundary for ownership/
                # pawn changes; this is temporary, silent and never a tick hook.
                lifecycle=function_path(pc.PlayerInput.Class,'PlayerInput')
                lifecycle_tag='owner-input';lifecycle_callback=self.owner_input
            # Hook before construction so registration failure never leaves a
            # working-looking capture dialog with no input handler.
            for path,kind,tag,cb in (
                ('WillowGame.WillowGFxDialogBox:HandleInputKey',Type.PRE,'input',self.input),
                ('WillowGame.WillowGFxDialogBox:Accepted',Type.PRE,'accepted',self.accepted),
                ('WillowGame.WillowGFxDialogBox:Cancelled',Type.PRE,'cancelled',self.cancelled),
                ('WillowGame.WillowGFxUIManager:Movie_OnClosed',Type.POST_UNCONDITIONAL,'closed',self.closed),
                (loading,Type.POST_UNCONDITIONAL,'loaded',self.world),
                (lifecycle,Type.POST_UNCONDITIONAL,lifecycle_tag,lifecycle_callback)):
                self.hook(path,kind,tag,cb)
            if not self.reported:
                self.log(f'Capture cleanup reflected: loading={loading}; {lifecycle_tag}={lifecycle}')
                self.reported=True
            dialog=pc.GFxUIManager.ShowDialog()
            self.dialog=WeakPointer(dialog)
            dialog.SetText('Bind '+label,f'Press a controller button for {label}. Release to save.')
            dialog.SetTooltips('Back: Actions | Escape: Cancel')
            dialog.ApplyLayout()
        except Exception:
            self.cancel();raise
        finally:self.opening=False

    def correct(self,obj):
        dialog=self.dialog() if self.dialog is not None else None
        if dialog is None:
            self.cancel(close=False);return False
        pc=get_pc(possibly_loading=True)
        if not same(pc,self.owner()) or time.monotonic()-self.started>600:
            self.cancel();return False
        return same(obj,dialog)

    def input(self,obj,args,ret,func):
        if not self.correct(obj):return None
        # Native dispatch can still be unwinding the event that opened us.
        # Consume the opening button through its release, even if the engine
        # forwards it to the newly created movie. No timer/tick hook.
        raw=str(args.ukey).casefold()
        if raw in self.opening_keys:
            if args.uevent==EInputEvent.IE_Released:
                self.opening_keys.discard(raw)
                self.gate.released(raw)
            return (Block,True)
        if self.opening:return (Block,True)
        self.last_key=raw
        self.last_event=args.uevent
        key=canonical(args.ukey);event=args.uevent
        if str(args.ukey).casefold() in ('escape','xboxtypes_start'):
            self.gate.finish(raw,released=event==EInputEvent.IE_Released)
            self.cancel(retain_gate=True)
            self.refresh()
            return (Block,True)
        back=str(args.ukey).casefold()=='xboxtypes_back'
        if self.mode=='actions':
            if self.action_back:
                if back and event==EInputEvent.IE_Released:self.action_back=False
                return (Block,True)
            if event==EInputEvent.IE_Pressed:self.gate.track(raw)
            elif event==EInputEvent.IE_Released:self.gate.released(raw)
            return None  # A/B and navigation are native in the actions menu.
        if back:
            if event==EInputEvent.IE_Pressed:self.actions(obj)
            return (Block,True)
        if key not in KEYS:return (Block,True)
        if event==EInputEvent.IE_Pressed:
            self.pressed.add(key)
            self.gate.track(raw)
        elif event==EInputEvent.IE_Released and key in self.pressed:
            self.pressed.discard(key)
            self.finish(key)
        # The opening click's release has no preceding press in this dialog.
        # Repeat/Held events never arm capture or finish a binding.
        return (Block,True)

    def actions(self,dialog):
        self.mode='actions';self.pressed.clear();self.action_back=True
        dialog.SetText('Binding Actions','Select Unbind, Restore Default or Cancel.')
        dialog.SetTooltips('<StringAliasMap:GFx_Accept> Select     <StringAliasMap:GFx_Cancel> Cancel')
        for tag,label in (('unbind','Unbind'),('default','Restore Default'),('cancel','Cancel')):
            dialog.AppendButton('ControllerManager:'+tag,label,'')
        dialog.SetDefaultButton('ControllerManager:unbind',True)
        dialog.ApplyLayout()

    def accepted(self,obj,args,ret,func):
        if not self.correct(obj) or self.mode!='actions':return None
        value=('Unbound','Default',None)[int(obj.CurrentSelection)]
        if value is None:
            self.gate.finish(self.last_key,released=self.last_event==EInputEvent.IE_Released)
            self.cancel(close=False,retain_gate=True)
            self.refresh()
        else:self.finish(value,close=False)
        return None

    def cancelled(self,obj,args,ret,func):
        if self.dialog is not None and same(obj,self.dialog()):
            self.gate.finish(self.last_key,released=self.last_event==EInputEvent.IE_Released)
            self.cancel(close=False,retain_gate=True)
            self.refresh()

    def closed(self,obj,args,ret,func):
        if self.manager is None or not same(obj,self.manager()):return
        movie=getattr(args,'Movie',None)
        if movie is None:return
        if (self.dialog is not None and same(movie,self.dialog()) or
                self.parent is not None and same(movie,self.parent())):
            self.cancel(close=self.dialog is not None and not same(movie,self.dialog()))

    def world(self,obj,args,ret,func):
        pc=get_pc(possibly_loading=True)
        if pc is None or not same(pc,self.owner()) or same(obj,pc) or same(obj,getattr(pc,'Pawn',None)):
            self.cancel()

    def owner_input(self,obj,args,ret,func):
        pc=get_pc(possibly_loading=True)
        if self.owner is None:return
        if pc is None or not same(pc,self.owner()):self.cancel();return
        if not same(obj,pc.PlayerInput):return
        pawn=getattr(pc,'Pawn',None)
        old=self.pawn() if self.pawn is not None else None
        if (pawn is None)!=(old is None) or pawn is not None and not same(pawn,old):self.cancel()

    def finish(self,value,close=True):
        from .layout import SHORT
        selection=SHORT.get(value,value)
        context,action=self.context,self.action
        # Commit atomically before closing; on save failure the coordinator rolls
        # back settings and this capture's guarded callback cancels the dialog.
        self.commit(context,action,selection)
        self.gate.finish(self.last_key,released=self.last_event==EInputEvent.IE_Released)
        self.cancel(close=close,retain_gate=True)
        self.refresh()

    def cancel(self,close=True,retain_gate=False):
        if not retain_gate:self.gate.reset()
        dialog=self.dialog() if self.dialog is not None else None
        self.dialog=None;self.parent=self.manager=self.owner=self.pawn=None
        self.mode='';self.pressed.clear();self.action_back=False
        self.opening_keys.clear()
        self.last_key=None
        self.last_event=None
        for path,kind,ident in tuple(self.hooks):
            try:
                unrealsdk.hooks.remove_hook(path,kind,ident)
                if unrealsdk.hooks.has_hook(path,kind,ident):raise RuntimeError('Capture hook still registered')
                self.hooks.remove((path,kind,ident))
            except Exception as exc:self.warn('capture_cleanup',f'Capture cleanup will retry: {exc}')
        if close and dialog is not None:
            try:dialog.Close()
            except Exception as exc:self.warn('capture_close',f'Capture dialog close unavailable: {exc}')
