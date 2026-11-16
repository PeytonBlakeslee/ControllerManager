"""BUG-18: synthetic GFx Open/Close balance under partial SDK failure.

Executes the production travel-resume pulse with controlled PC/menu observers.
No live game, native replication, or network timing is asserted.
"""
from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace as NS
import unittest

SOURCE = Path(__file__).resolve().parents[1] / "mod" / "ControllerManager" / "__init__.py"


def pulse_harness(*, open_failure=None, close_failure=None, counter=0,
                  readable=True, after_open_counter=True):
    tree = ast.parse(SOURCE.read_text(encoding="utf-8-sig"), filename=str(SOURCE))
    fn = next(node for node in tree.body if isinstance(node, ast.FunctionDef)
              and node.name == "_apply_travel_resume_pulse")
    events, warnings = [], []

    class Replication:
        def __init__(self):
            self.value = counter

        @property
        def bGFxMenuOpen(self):
            if not readable:
                raise RuntimeError("not replicated yet")
            return self.value

    replication = Replication()
    ui = NS(UpdateFocus=lambda: events.append("focus"),
            UpdatePause=lambda: events.append("pause-update"))

    class PC:
        PlayerReplicationInfo = replication
        GFxUIManager = ui
        on_open = None

        def SetGFxPauseMenuOpenState(self, enabled):
            events.append(("pause-state", bool(enabled)))

        def GFxMenuOpened(self):
            events.append("open-attempt")
            if open_failure == "before":
                raise RuntimeError("injected before the menu observer")
            # The exported game script increments this counter on the server.
            # Test observer calls model SDK hook delivery to another mod.
            events.append("observer-open")
            if after_open_counter:
                replication.value += 1
            if self.on_open:
                self.on_open()
            if open_failure == "after":
                raise RuntimeError("injected after an observable open")

        def GFxMenuClosed(self):
            events.append("close-attempt")
            if close_failure == "before":
                raise RuntimeError("injected before closing")
            replication.value -= 1
            events.append("observer-close")
            if close_failure == "after":
                raise RuntimeError("injected after closing")

    scope = {
        "Any": object,
        "_travel_resume_pending": True,
        "_travel_resume_running": False,
        "_warn": lambda *args: warnings.append(args),
    }
    exec(compile(ast.fix_missing_locations(ast.Module(body=[fn], type_ignores=[])),
                 str(SOURCE), "exec"), scope)
    return NS(pc=PC(), counter=replication, scope=scope, events=events,
              warnings=warnings)


class SyntheticMenuPulseTests(unittest.TestCase):
    def assert_finished(self, t):
        self.assertFalse(t.scope["_travel_resume_pending"])
        self.assertFalse(t.scope["_travel_resume_running"])
        self.assertEqual(t.events.count(("pause-state", True)), 1)
        self.assertEqual(t.events.count(("pause-state", False)), 1)
        self.assertIn("focus", t.events)
        self.assertIn("pause-update", t.events)

    def test_normal_open_close_order_and_replication_balance(self):
        t = pulse_harness()
        t.scope["_apply_travel_resume_pulse"](t.pc)
        self.assertEqual(t.counter.value, 0)
        self.assertEqual(t.events.count("observer-open"), 1)
        self.assertEqual(t.events.count("observer-close"), 1)
        self.assertLess(t.events.index("observer-open"),
                        t.events.index("observer-close"))
        self.assertEqual(t.warnings, [])
        self.assert_finished(t)

    def test_exception_before_open_does_not_send_unowned_close(self):
        t = pulse_harness(open_failure="before")
        t.scope["_apply_travel_resume_pulse"](t.pc)
        self.assertEqual(t.counter.value, 0)
        self.assertNotIn("observer-open", t.events)
        self.assertNotIn("close-attempt", t.events)
        self.assertEqual(len(t.warnings), 1)
        self.assertIn("gfx-open:RuntimeError", t.warnings[0][1])
        self.assertIn("gfx-open-unverified", t.warnings[0][1])
        self.assert_finished(t)

    def test_exception_after_observable_open_still_sends_one_close(self):
        t = pulse_harness(open_failure="after")
        t.scope["_apply_travel_resume_pulse"](t.pc)
        self.assertEqual(t.counter.value, 0)
        self.assertEqual(t.events.count("observer-open"), 1)
        self.assertEqual(t.events.count("observer-close"), 1)
        self.assertEqual(t.events.count("close-attempt"), 1)
        self.assertEqual(len(t.warnings), 1)
        self.assertIn("gfx-open:RuntimeError", t.warnings[0][1])
        self.assertNotIn("gfx-open-unverified", t.warnings[0][1])
        self.assert_finished(t)

    def test_successful_open_without_readback_still_matches_close(self):
        t = pulse_harness(readable=False)
        t.scope["_apply_travel_resume_pulse"](t.pc)
        self.assertEqual(t.counter.value, 0)
        self.assertEqual(t.events.count("observer-open"), 1)
        self.assertEqual(t.events.count("observer-close"), 1)
        self.assertEqual(t.warnings, [])
        self.assert_finished(t)

    def test_ambiguous_failure_with_no_counter_readback_skips_close(self):
        t = pulse_harness(open_failure="after", readable=False)
        t.scope["_apply_travel_resume_pulse"](t.pc)
        # Without trusted readback, cannot distinguish before vs after effect.
        # An unmatched decrement risks corrupting a real existing menu.
        self.assertEqual(t.counter.value, 1)
        self.assertEqual(t.events.count("observer-open"), 1)
        self.assertNotIn("close-attempt", t.events)
        self.assertIn("gfx-open-unverified", t.warnings[0][1])
        self.assert_finished(t)

    def test_post_hook_exception_without_observable_increment_cannot_guess(self):
        t = pulse_harness(open_failure="after", after_open_counter=False)
        t.scope["_apply_travel_resume_pulse"](t.pc)
        self.assertEqual(t.counter.value, 0)
        self.assertEqual(t.events.count("observer-open"), 1)
        self.assertNotIn("close-attempt", t.events)
        self.assertIn("gfx-open-unverified", t.warnings[0][1])
        self.assert_finished(t)

    def test_close_failure_after_counter_decrement_is_not_retried(self):
        t = pulse_harness(close_failure="after")
        t.scope["_apply_travel_resume_pulse"](t.pc)
        self.assertEqual(t.counter.value, 0)
        self.assertEqual(t.events.count("close-attempt"), 1)
        self.assertEqual(t.events.count("observer-close"), 1)
        self.assertIn("gfx-close:RuntimeError", t.warnings[0][1])
        self.assert_finished(t)

    def test_close_failure_before_effect_is_logged_without_second_attempt(self):
        t = pulse_harness(close_failure="before")
        t.scope["_apply_travel_resume_pulse"](t.pc)
        self.assertEqual(t.counter.value, 1)
        self.assertEqual(t.events.count("close-attempt"), 1)
        self.assertNotIn("observer-close", t.events)
        self.assertIn("gfx-close:RuntimeError", t.warnings[0][1])
        self.assert_finished(t)

    def test_existing_real_menu_counter_is_preserved_on_partial_open(self):
        t = pulse_harness(open_failure="after", counter=3)
        t.scope["_apply_travel_resume_pulse"](t.pc)
        self.assertEqual(t.counter.value, 3)
        self.assertEqual(t.events.count("observer-open"), 1)
        self.assertEqual(t.events.count("observer-close"), 1)
        self.assert_finished(t)

    def test_reentrant_pulse_does_not_duplicate_open_or_close(self):
        t = pulse_harness()
        def reenter():
            # The current pulse already consumed its pending request; a
            # nested invocation must not manufacture another request.
            t.scope["_apply_travel_resume_pulse"](t.pc)
        t.pc.on_open = reenter
        t.scope["_apply_travel_resume_pulse"](t.pc)
        self.assertEqual(t.events.count("observer-open"), 1)
        self.assertEqual(t.events.count("observer-close"), 1)
        self.assertEqual(t.counter.value, 0)
        self.assert_finished(t)

    def test_no_pending_pulse_does_not_emit_any_events(self):
        t = pulse_harness()
        t.scope["_travel_resume_pending"] = False
        t.scope["_apply_travel_resume_pulse"](t.pc)
        self.assertEqual(t.events, [])
        self.assertEqual(t.warnings, [])
        self.assertFalse(t.scope["_travel_resume_running"])

    def test_ui_update_failures_do_not_break_event_symmetry(self):
        t = pulse_harness()
        def fails_focus():
            raise RuntimeError("focus stopped")
        def fails_pause():
            raise RuntimeError("pause stopped")
        t.pc.GFxUIManager.UpdateFocus = fails_focus
        t.pc.GFxUIManager.UpdatePause = fails_pause
        t.scope["_apply_travel_resume_pulse"](t.pc)
        self.assertEqual(t.counter.value, 0)
        self.assertEqual(t.events.count("observer-open"), 1)
        self.assertEqual(t.events.count("observer-close"), 1)
        self.assertIn("focus:RuntimeError", t.warnings[0][1])
        self.assertIn("pause-update:RuntimeError", t.warnings[0][1])
        self.assertFalse(t.scope["_travel_resume_running"])


if __name__ == "__main__":
    unittest.main()
