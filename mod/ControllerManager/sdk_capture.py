"""Object-scoped two-stage SDK shortcut dialog; never handles gameplay input."""
from __future__ import annotations
import time
import unrealsdk
from mods_base import get_pc, EInputEvent
from unrealsdk.hooks import Type, Block
from unrealsdk.unreal import WeakPointer
from .sdk_actions import CaptureState, button_name
from .input_gate import ParentInputGate

from .object_utils import same_native as same

class AssignmentDialog:
    def __init__(self, save, log):
        self.save=save;self.log=log;self.dialog=None;self.owner=None;self.parent=None
        self.key=None;self.label='';self.state=None;self.started=0;self.hooks=[]
        self.pressed=set();self._handling=False
        self.gate=ParentInputGate()
        self.opening_keys=set()

    def install(self,path,kind,tag,callback):
        function=unrealsdk.find_object('Function',path)
        path=function._path_name();tag='ControllerManager:SDKCapture:'+tag
        if unrealsdk.hooks.has_hook(path,kind,tag):raise RuntimeError('Hook already present')
        try:unrealsdk.hooks.add_hook(path,kind,tag,callback)
        finally:
            # Registration can raise after installing the hook. Retain ownership
            # so start()'s failure cleanup can remove it and retry if needed.
            if unrealsdk.hooks.has_hook(path,kind,tag):self.hooks.append((path,kind,tag))
        if not unrealsdk.hooks.has_hook(path,kind,tag):raise RuntimeError('Capture hook unavailable')

    def start(self,key,label,opening_keys=()):
        self.stop()
        pc=get_pc(possibly_loading=True)
        if pc is None:raise RuntimeError('No local player to own dialog')
        self.owner=WeakPointer(pc)
        self.key=key;self.label=label;self.state=CaptureState()
        self.started=time.monotonic();self.pressed.clear()
        self.opening_keys=set(opening_keys)
        try:
            from willow2_mod_menu import options_menu
            list_obj=options_menu.latest_list()
            if list_obj is None or list_obj.MyOwnerMovie is None:
                raise RuntimeError('SDK capture requires the manager options page')
            self.parent=WeakPointer(list_obj.MyOwnerMovie)
            self.gate.begin(self.parent,opening_keys)
            # Register input and close before showing the dialog.
            self.install('WillowGame.WillowGFxDialogBox:HandleInputKey',Type.PRE,'input',self.input)
            self.install('WillowGame.WillowGFxUIManager:Movie_OnClosed',Type.POST_UNCONDITIONAL,'closed',self.closed)
            self.install('WillowGame.WillowPlayerController:WillowClientDisableLoadingMovie',
                         Type.POST_UNCONDITIONAL,'travel',self.travel)
            self.dialog=WeakPointer(pc.GFxUIManager.ShowDialog())
            self.prompt()
        except Exception:
            self.stop();raise

    def prompt(self):
        dialog=self.dialog() if self.dialog else None
        if dialog is None:return
        if self.state.stage=='modifier':
            dialog.SetText('Bind '+self.label,
                'Choose a modifier:\n\n'
                '  • START: Press Start\n'
                '  • BACK: Press Back\n'
                '  • X: No modifier\n'
                '  • Y: Clear binding\n\n'
                'Other buttons: Cancel\n\n'
                'During gameplay, HOLD Start or Back and press your bound button.')
        else:
            dialog.SetText('Bind '+self.label,
                           'Press one controller button for '+self.label+'.\n\n'
                           '  • A, B, X, Y\n'
                           '  • LB, RB, LS, RS\n'
                           '  • Triggers or D-pad\n\n'
                           'Escape: Cancel')
        dialog.SetTooltips('Escape: Cancel')
        dialog.ApplyLayout()

    def current(self,obj):
        dialog=self.dialog() if self.dialog else None
        pc=get_pc(possibly_loading=True)
        if dialog is None or pc is None or not same(self.owner(),pc):
            self.stop();return False
        return same(dialog,obj)

    def input(self,obj,args,ret,func):
        if not self.current(obj):return None
        if self._handling:return (Block,True)
        self._handling=True
        try:
            raw=str(args.ukey)
            if raw.casefold() in self.opening_keys:
                if args.uevent==EInputEvent.IE_Released:
                    self.opening_keys.discard(raw.casefold())
                    self.gate.released(raw)
                return (Block,True)
            physical = 'Escape' if raw.casefold()=='escape' else button_name(raw)
            event=args.uevent
            if physical is None:return (Block,True)
            if event == EInputEvent.IE_Pressed:
                if physical in self.pressed:return (Block,True)
                self.pressed.add(physical)
                result=self.state.press(physical)
                if result=='next':
                    self.gate.track(raw)
                    self.prompt()
                elif result in ('clear','save'):
                    value=self.state.value if result=='save' else None
                    key=self.key
                    # The save callback can refuse a conflict without altering settings.
                    self.gate.finish(raw)
                    self.stop(retain_gate=True);self.save(key,value)
                elif result=='cancel':
                    self.gate.finish(raw)
                    self.stop(retain_gate=True)
            elif event==EInputEvent.IE_Released:
                self.pressed.discard(physical)
                self.gate.released(raw)
            return (Block,True)
        finally:self._handling=False

    def closed(self,obj,args,ret,func):
        if self.dialog is None:return None
        movie=getattr(args,'Movie',None)
        if movie is not None and (same(movie,self.dialog()) or (self.parent and same(movie,self.parent()))):
            self.stop(close=not same(movie,self.dialog()))
        return None

    def travel(self,obj,args,ret,func):
        if self.owner is not None and same(obj,self.owner()):self.stop()
        return None

    def stop(self,close=True,retain_gate=False):
        if not retain_gate:self.gate.reset()
        dialog=self.dialog() if self.dialog is not None else None
        self.dialog=self.owner=self.parent=None;self.key=None;self.state=None
        self.pressed.clear();self.started=0
        self.opening_keys.clear()
        for path,kind,tag in tuple(self.hooks):
            try:
                unrealsdk.hooks.remove_hook(path,kind,tag)
                if unrealsdk.hooks.has_hook(path,kind,tag):raise RuntimeError('SDK capture hook still registered')
            except Exception as exc:
                self.log('SDK capture cleanup hook failed: '+str(exc))
            else:self.hooks.remove((path,kind,tag))
        if close and dialog is not None:
            try:dialog.Close()
            except Exception as exc:self.log('SDK capture dialog close failed: '+str(exc))
