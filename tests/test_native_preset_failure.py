"""Fault injection for the actual ControllerManager native-preset transition methods.

Only selected production method ASTs are executed against a controlled fake
PlayerInput. No game API or BL2 runtime is claimed by these regressions.
"""
import ast
import contextlib
import copy
from pathlib import Path
from types import SimpleNamespace as NS
import unittest

SOURCE=Path(__file__).resolve().parents[1]/"mod"/"ControllerManager"/"binding_runtime.py"
METHODS={"remember_user_native", "_force_factory_default", "_capture_after_native_reset",
         "activate_native_override", "restore_user_native", "configuration",
         "needs_service", "sync", "attach", "disable", "detach", "rebuild_native_override",
         "adopt_external_native_rebuild"}


def binding_class():
    source=ast.parse(SOURCE.read_text(encoding="utf-8-sig"))
    cls=next(n for n in source.body if isinstance(n,ast.ClassDef) and n.name=="BindingRuntime")
    methods=[n for n in cls.body if isinstance(n,ast.FunctionDef) and n.name in METHODS]
    assert len(methods)==len(METHODS), "Production methods changed; revalidate test harness"
    selected=ast.ClassDef(name="TestBindingRuntime",bases=[],keywords=[],body=methods,
                          decorator_list=[])
    scope={"copy":copy,"fingerprint":lambda value:tuple(value),
           "same":lambda x,y:x is y and x is not None,
           "clear_schema_cache":lambda:None,
           "unrealsdk":NS(hooks=NS(prevent_hooking_direct_calls=contextlib.nullcontext))}
    exec(compile(ast.fix_missing_locations(ast.Module(body=[selected],type_ignores=[])),
                 str(SOURCE),"exec"),scope)
    return scope["TestBindingRuntime"]


class SimulatedPI:
    def __init__(self,fault):
        self.fault=fault
        self._preset_index=2
        self._rebindings=[NS(DefaultKeyName="Sprint",RemappedKeyName="Y")]
        self.LatentButtons=["Sprint"]
        self.Buttons=[NS(InputDeviceHandlers=["user"])]
        self.setups=0

    @property
    def ControllerPresetIndex(self):
        return self._preset_index

    @ControllerPresetIndex.setter
    def ControllerPresetIndex(self,value):
        self._preset_index=value
        if self.fault=="preset_index_restore":
            raise RuntimeError("preset index restoration failed")

    @property
    def ControllerRebindings(self):
        return self._rebindings

    @ControllerRebindings.setter
    def ControllerRebindings(self,value):
        self._rebindings=value
        if self.fault=="clear_after_write" and not value:
            raise RuntimeError("clear after write")
        if self.fault=="rebindings_restore" and value:
            raise RuntimeError("metadata restoration failed")

    def SetupInputDevices(self):
        self.setups+=1
        self.Buttons[0].InputDeviceHandlers=["default"]
        if self.fault=="setup_after_write":
            raise RuntimeError("SetupInputDevices partial failure")


class SimulatedPC:
    def __init__(self,pi):
        self.PlayerInput=pi
        self.calls=0

    def ApplyControllerPreset(self,**kwargs):
        self.calls+=1
        if self.PlayerInput.fault=="apply_before_write":
            raise RuntimeError("preset failed before write")
        if self.PlayerInput.fault=="apply_after_write":
            self.PlayerInput.Buttons[0].InputDeviceHandlers=["partial"]
            raise RuntimeError("preset failed after write")
        if self.PlayerInput.fault=="apply_false":
            return False
        self.PlayerInput.Buttons[0].InputDeviceHandlers=["default"]
        return True


def runtime(fault=None,capture_fails=False):
    c=binding_class().__new__(binding_class())
    pi=SimulatedPI(fault)
    pc=SimulatedPC(pi)
    c.pi=lambda:pi
    c.pc=lambda:pc
    c.warn_messages=[]
    c.warn=lambda *args:c.warn_messages.append(args)
    c.log=lambda *args:None
    c.input_rebuilt=None
    c.input_rebuild_failed=None
    c.user_preset_index=None
    c.user_rebindings=[]
    c._native_transition_pending=False
    c.override_active=False
    c._forcing_native=False
    c.paused=False
    c.ready=True
    c.applied={}
    c.latent_applied=None
    c.native_change_pending=False
    c.refresh=False
    c.retry=False
    c._sync_pending=False
    c.last_enabled=False
    c.issue=""
    c.restore_compat=lambda:None
    c.cancel_drain=lambda:None
    c.invalidate_rows=lambda:None
    c.rows=lambda:{"A":pi.Buttons[0]}
    c.current=lambda rows:{k:tuple(v.InputDeviceHandlers) for k,v in rows.items()}
    c.capture=lambda:(_ for _ in ()).throw(RuntimeError("capture failed")) if capture_fails else None
    return c,pc,pi


class NativeTransitionTests(unittest.TestCase):
    def test_successful_override_unaffected(self):
        r,pc,pi=runtime()
        r.activate_native_override()
        self.assertEqual(pc.calls,1)
        self.assertEqual(pi.setups,1)
        self.assertEqual(pi.ControllerPresetIndex,2)
        self.assertEqual(len(pi.ControllerRebindings),1)
        self.assertTrue(r.override_active)
        self.assertFalse(r._native_transition_pending)

    def test_side_effecting_failures_preserve_recoverable_owner(self):
        for fault in ("apply_after_write","clear_after_write",
                      "setup_after_write","preset_index_restore",
                      "rebindings_restore"):
            with self.subTest(fault=fault):
                r,pc,pi=runtime(fault)
                with self.assertRaises(RuntimeError):
                    r.activate_native_override()
                self.assertTrue(r._native_transition_pending)
                self.assertTrue(r.paused)
                self.assertEqual(r.user_preset_index,2)
                self.assertEqual(len(r.user_rebindings),1)
                original=pc.calls
                pi.Buttons[0].InputDeviceHandlers=["foreign"]
                self.assertFalse(r.restore_user_native())
                r.disable()
                self.assertEqual(pi.Buttons[0].InputDeviceHandlers,["foreign"])
                with self.assertRaises(RuntimeError):
                    r.activate_native_override()
                self.assertEqual(pc.calls,original)

    def test_prewrite_failure_allows_retry_without_unknown_ownership(self):
        r,pc,pi=runtime("apply_before_write")
        with self.assertRaises(RuntimeError):
            r.activate_native_override()
        self.assertFalse(r._native_transition_pending)
        self.assertFalse(r.override_active)
        pi.fault=None
        r.activate_native_override()
        self.assertTrue(r.override_active)

    def test_explicit_native_false_is_safe_when_no_mutation(self):
        r,_,pi=runtime("apply_false")
        with self.assertRaises(ValueError):
            r.activate_native_override()
        self.assertFalse(r._native_transition_pending)
        self.assertEqual(pi.Buttons[0].InputDeviceHandlers,["user"])

    def test_capture_failure_after_reset_is_retained(self):
        r,_,_=runtime(capture_fails=True)
        with self.assertRaisesRegex(RuntimeError,"capture failed"):
            r.activate_native_override()
        self.assertTrue(r._native_transition_pending)
        self.assertTrue(r.override_active)
        self.assertTrue(r.paused)

    def test_pending_same_input_cannot_be_reactivated_by_attach(self):
        r,pc,pi=runtime("apply_after_write")
        with self.assertRaises(RuntimeError):
            r.activate_native_override()
        before=pc.calls
        r.attach(pc,override=True)
        self.assertEqual(pc.calls,before)
        self.assertTrue(r._native_transition_pending)
        self.assertFalse(r.ready)

    def test_retire_pending_input_does_not_touch_foreign_table(self):
        r,pc,pi=runtime("apply_after_write")
        with self.assertRaises(RuntimeError):
            r.activate_native_override()
        pi.Buttons[0].InputDeviceHandlers=["foreign"]
        r.detach()
        self.assertEqual(pi.Buttons[0].InputDeviceHandlers,["foreign"])
        self.assertFalse(r._native_transition_pending)
        self.assertFalse(r.override_active)
        self.assertIsNone(r.user_preset_index)
        self.assertIsNone(r.pi)

    def test_partial_failure_notifies_other_input_consumer_once(self):
        for fault in ("apply_after_write", "setup_after_write",
                      "preset_index_restore"):
            with self.subTest(fault=fault):
                r,pc,pi=runtime(fault)
                notices=[]
                r.input_rebuild_failed=lambda controller,player_input: notices.append(
                    (controller,player_input))
                with self.assertRaises(RuntimeError):
                    r.activate_native_override()
                self.assertEqual(notices,[(pc,pi)])
                self.assertTrue(r._native_transition_pending)
        r,pc,pi=runtime("apply_before_write")
        notices=[]
        r.input_rebuild_failed=lambda *args:notices.append(args)
        with self.assertRaises(RuntimeError):
            r.activate_native_override()
        self.assertEqual(notices,[])

    def test_failure_notification_exception_preserves_original_error(self):
        r,pc,pi=runtime("apply_after_write")
        r.input_rebuild_failed=lambda *args:(_ for _ in ()).throw(
            RuntimeError("deadzone notification unavailable"))
        with self.assertRaisesRegex(RuntimeError,"preset failed after write"):
            r.activate_native_override()
        self.assertTrue(r._native_transition_pending)
        self.assertTrue(any(w[0]=="native_rebuild_failed_notify"
                            for w in r.warn_messages))

    def test_pending_service_does_not_spin_or_rebuild(self):
        r,_,_=runtime("setup_after_write")
        with self.assertRaises(RuntimeError):
            r.activate_native_override()
        self.assertFalse(r.needs_service(True))
        self.assertIsNone(r.sync(True,"foot",{},False))
        self.assertFalse(r.rebuild_native_override())
        self.assertFalse(r.adopt_external_native_rebuild())


if __name__=="__main__":
    unittest.main()
