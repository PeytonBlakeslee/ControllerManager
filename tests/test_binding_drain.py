"""BUG-16: real ControllerManager deferred-binding methods under input faults.

UnrealSDK PlayerInput and hook calls are fakes, not a BL2 gameplay assertion.
The production methods are compiled from binding_runtime.py, including the
nested POST_UNCONDITIONAL drain callback.
"""
from __future__ import annotations

import ast
import copy
from pathlib import Path
from types import SimpleNamespace as NS
import unittest

ROOT = Path(__file__).resolve().parents[1] / "mod" / "ControllerManager"
SOURCE = ROOT / "binding_runtime.py"
MANAGER_SOURCE = ROOT / "__init__.py"
KEYS = ("A", "B")


class HookRegistry:
    def __init__(self):
        self.callbacks = {}
        self.Type = NS(POST_UNCONDITIONAL="post-unconditional")
        self.removes = 0
        self.adds = 0
        self.remove_fails = 0

    def has_hook(self, *spec):
        return tuple(spec) in self.callbacks

    def add_hook(self, path, kind, ident, callback):
        spec = (path, kind, ident)
        if spec in self.callbacks:
            return False
        self.callbacks[spec] = callback
        self.adds += 1
        return True

    def remove_hook(self, *spec):
        self.removes += 1
        if self.remove_fails:
            self.remove_fails -= 1
            raise RuntimeError("SDK removal temporarily unavailable")
        self.callbacks.pop(tuple(spec), None)
        return True

    def boundary(self, pi):
        for callback in tuple(self.callbacks.values()):
            callback(pi, None, None, None)


class Row:
    def __init__(self, handlers, state=0):
        self._handlers = list(handlers)
        self.State = state
        self.fail_before = 0
        self.fail_after = 0
        self.fail_read = 0
        self.assignments = 0

    @property
    def InputDeviceHandlers(self):
        if self.fail_read:
            self.fail_read -= 1
            raise RuntimeError("transient reflected handler read")
        return self._handlers

    @InputDeviceHandlers.setter
    def InputDeviceHandlers(self, value):
        self.assignments += 1
        if self.fail_before:
            self.fail_before -= 1
            raise RuntimeError("transient setter before write")
        self._handlers = list(value)
        if self.fail_after:
            self.fail_after -= 1
            raise RuntimeError("transient setter after write")


class PlayerInput:
    def __init__(self, rows, address=100):
        self.rows = rows
        self.address = address
        self.LatentButtons = []
        self.Class = NS(_find=lambda *_: NS(_path_name=lambda: "WillowGame.WillowPlayerInput:PlayerInput"))

    def _get_address(self):
        return self.address


class Controller:
    def __init__(self, pi):
        self.PlayerInput = pi
        self.Pawn = object()


def compiled_runtime(hooks):
    tree = ast.parse(SOURCE.read_text(encoding="utf-8-sig"))
    source = next(node for node in tree.body
                  if isinstance(node, ast.ClassDef) and node.name == "BindingRuntime")
    names = {"__init__", "disable", "cancel_drain", "restore_owned", "idle"}
    methods = [node for node in source.body
               if isinstance(node, ast.FunctionDef) and node.name in names]
    assert len(methods) == len(names)
    cls = ast.ClassDef(name="TestBindingRuntime", bases=[], keywords=[],
                       body=methods, decorator_list=[])
    ns = {
        "unrealsdk": NS(hooks=hooks),
        "Type": hooks.Type,
        "KEYS": KEYS,
        "SHORT": {"A": "A", "B": "B"},
        "same": lambda left, right: left is right and left is not None,
        "fingerprint": lambda value: tuple(value),
        "copy": copy,
    }
    exec(compile(ast.fix_missing_locations(ast.Module(body=[cls], type_ignores=[])),
                 str(SOURCE), "exec"), ns)
    return ns["TestBindingRuntime"]


def scenario():
    hooks = HookRegistry()
    rows = {"A": Row(["custom"]), "B": Row(["base-b"])}
    pi = PlayerInput(rows)
    pc = Controller(pi)
    current = {"pc": pc}
    warnings, events = [], []
    Runtime = compiled_runtime(hooks)
    r = Runtime(lambda **kwargs: current["pc"], events.append,
                lambda *args: warnings.append(args))
    r.pi = lambda: pi
    r.pc = lambda: pc
    r.ready = True
    r.released = 0
    r.applied = {"A": ("custom",)}
    r.native = {"A": "A", "B": "B"}
    r.baseline = {"A": ["baseline-a"], "B": ["base-b"]}
    r.rows = lambda: rows
    r.restore_compat = lambda: events.append("restore-compat")
    return NS(runtime=r, hooks=hooks, rows=rows, pi=pi, pc=pc,
              current=current, warnings=warnings, events=events)


class DrainTests(unittest.TestCase):
    def test_two_quiet_boundaries_restore_and_remove_one_hook(self):
        t = scenario()
        t.runtime.disable()
        self.assertEqual(t.hooks.adds, 1)
        t.hooks.boundary(t.pi)
        self.assertEqual(t.rows["A"]._handlers, ["custom"])
        t.hooks.boundary(t.pi)
        self.assertEqual(t.rows["A"]._handlers, ["baseline-a"])
        self.assertEqual(t.hooks.callbacks, {})
        self.assertEqual(t.runtime.applied, {})
        self.assertEqual(t.runtime.drain_issue, "")
        self.assertEqual(t.hooks.removes, 1)

    def test_single_restore_exception_retries_only_after_two_new_quiet_boundaries(self):
        t = scenario()
        t.rows["A"].fail_before = 1
        t.runtime.disable()
        t.hooks.boundary(t.pi)
        t.hooks.boundary(t.pi)
        self.assertEqual(t.runtime.drain_failures, 1)
        self.assertEqual(t.runtime.quiet, 0)
        self.assertEqual(t.rows["A"]._handlers, ["custom"])
        self.assertEqual(len(t.hooks.callbacks), 1)
        t.hooks.boundary(t.pi)
        self.assertEqual(t.rows["A"]._handlers, ["custom"])
        t.hooks.boundary(t.pi)
        self.assertEqual(t.rows["A"]._handlers, ["baseline-a"])
        self.assertFalse(t.hooks.callbacks)
        self.assertEqual(t.runtime.drain_failures, 0)

    def test_no_input_callbacks_retains_pending_ownership_and_reports_status(self):
        t = scenario()
        t.runtime.disable()
        self.assertEqual(t.rows["A"].assignments, 0)
        self.assertIn("two input boundaries", t.runtime.drain_issue)
        self.assertEqual(t.runtime.applied, {"A": ("custom",)})
        self.assertFalse(t.runtime.drain_stalled)
        self.assertEqual(len(t.hooks.callbacks), 1)
        t.runtime.disable()
        self.assertEqual(t.hooks.adds, 1)

    def test_long_held_button_cannot_be_restored_by_callback_count(self):
        t = scenario()
        t.rows["A"].State = 1
        t.runtime.disable()
        for _ in range(300):
            t.hooks.boundary(t.pi)
        self.assertEqual(t.runtime.drain_failures, 0)
        self.assertEqual(t.runtime.quiet, 0)
        self.assertEqual(t.rows["A"].assignments, 0)
        t.rows["A"].State = 0
        t.hooks.boundary(t.pi)
        self.assertEqual(t.rows["A"]._handlers, ["custom"])
        t.hooks.boundary(t.pi)
        self.assertEqual(t.rows["A"]._handlers, ["baseline-a"])

    def test_third_failure_stops_auto_attempts_but_preserves_owner(self):
        t = scenario()
        t.rows["A"].fail_before = 4
        t.runtime.disable()
        for _ in range(6):
            t.hooks.boundary(t.pi)
        self.assertEqual(t.runtime.drain_failures, 3)
        self.assertTrue(t.runtime.drain_stalled)
        self.assertFalse(t.hooks.callbacks)
        self.assertEqual(t.runtime.applied, {"A": ("custom",)})
        self.assertIn("Retry Pending Restore", t.runtime.drain_issue)
        self.assertEqual(t.rows["A"].assignments, 3)
        t.runtime.disable()  # Explicit re-arm, still requires two boundaries.
        self.assertFalse(t.runtime.drain_stalled)
        self.assertEqual(t.rows["A"].assignments, 3)
        t.hooks.boundary(t.pi)
        self.assertEqual(t.rows["A"].assignments, 3)
        t.rows["A"].fail_before = 0
        t.hooks.boundary(t.pi)
        self.assertEqual(t.rows["A"]._handlers, ["baseline-a"])
        self.assertEqual(t.runtime.applied, {})

    def test_other_mod_edit_is_not_overwritten_on_release(self):
        t = scenario()
        t.runtime.disable()
        t.rows["A"]._handlers = ["foreign"]
        t.hooks.boundary(t.pi)
        t.hooks.boundary(t.pi)
        self.assertEqual(t.rows["A"]._handlers, ["foreign"])
        self.assertEqual(t.rows["A"].assignments, 0)
        self.assertEqual(t.runtime.applied, {})
        self.assertFalse(t.hooks.callbacks)
        self.assertTrue(any(w[0] == "binding_external" for w in t.warnings))

    def test_replacement_player_input_does_not_touch_new_player(self):
        t = scenario()
        t.runtime.disable()
        newer = PlayerInput({"A": Row(["new-input"]), "B": Row(["new-b"])}, 200)
        t.current["pc"] = Controller(newer)
        t.hooks.boundary(newer)
        self.assertEqual(newer.rows["A"]._handlers, ["new-input"])
        self.assertEqual(newer.rows["A"].assignments, 0)
        self.assertEqual(t.rows["A"]._handlers, ["baseline-a"])
        self.assertEqual(t.runtime.applied, {})
        self.assertEqual(t.hooks.callbacks, {})

    def test_foreign_controller_callback_never_advances_quiet(self):
        t = scenario()
        t.runtime.disable()
        other = PlayerInput({"A": Row(["foreign"]), "B": Row(["foreign-b"])}, 404)
        for _ in range(7):
            t.hooks.boundary(other)
        self.assertEqual(t.runtime.quiet, 0)
        self.assertEqual(t.rows["A"]._handlers, ["custom"])
        self.assertEqual(len(t.hooks.callbacks), 1)

    def test_after_write_exception_reconciles_without_repeating_write(self):
        t = scenario()
        t.rows["A"].fail_after = 1
        t.runtime.disable()
        t.hooks.boundary(t.pi)
        t.hooks.boundary(t.pi)
        self.assertEqual(t.rows["A"]._handlers, ["baseline-a"])
        self.assertEqual(t.runtime.applied, {"A": ("custom",)})
        self.assertEqual(t.runtime.drain_failures, 1)
        t.hooks.boundary(t.pi)
        t.hooks.boundary(t.pi)
        self.assertEqual(t.rows["A"].assignments, 1)
        self.assertEqual(t.runtime.applied, {})
        self.assertEqual(t.hooks.callbacks, {})

    def test_failed_hook_removal_keeps_ledger_and_retry_hook(self):
        t = scenario()
        t.hooks.remove_fails = 1
        t.runtime.disable()
        t.hooks.boundary(t.pi)
        t.hooks.boundary(t.pi)
        self.assertEqual(t.rows["A"]._handlers, ["baseline-a"])
        self.assertTrue(t.hooks.callbacks)
        self.assertIsNotNone(t.runtime.drain_hook)
        self.assertEqual(t.runtime.drain_failures, 1)
        t.hooks.boundary(t.pi)
        t.hooks.boundary(t.pi)
        self.assertFalse(t.hooks.callbacks)
        self.assertIsNone(t.runtime.drain_hook)

    def test_manual_ui_retry_rearms_but_never_restores_synchronously(self):
        t = scenario()
        t.rows["A"].fail_before = 4
        t.runtime.disable()
        for _ in range(6):
            t.hooks.boundary(t.pi)
        self.assertTrue(t.runtime.drain_stalled)
        tree = ast.parse(MANAGER_SOURCE.read_text(encoding="utf-8-sig"))
        fn = next(node for node in tree.body if isinstance(node, ast.FunctionDef)
                  and node.name == "_retry_pending_binding_restore")
        logs, notices = [], []
        env = {"_active": False, "_bindings": t.runtime,
               "_log": logs.append, "_warn": lambda *args: notices.append(args),
               "_binding_descriptions": lambda: None, "_pane_redraw_current": lambda: None}
        exec(compile(ast.fix_missing_locations(ast.Module(body=[fn], type_ignores=[])),
                     str(MANAGER_SOURCE), "exec"), env)
        env["_retry_pending_binding_restore"](None)
        self.assertEqual(t.rows["A"].assignments, 3)
        self.assertEqual(len(t.hooks.callbacks), 1)
        t.rows["A"].fail_before = 0
        t.hooks.boundary(t.pi)
        self.assertEqual(t.rows["A"].assignments, 3)
        t.hooks.boundary(t.pi)
        self.assertEqual(t.rows["A"]._handlers, ["baseline-a"])
        self.assertEqual(notices, [])


if __name__ == "__main__":
    unittest.main()
