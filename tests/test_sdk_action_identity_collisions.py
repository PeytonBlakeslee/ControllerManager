"""BUG-17: ambiguous SDK shortcut identity must never select the first mod.

Runs the production sdk_actions discovery and ShortcutDispatcher, using fake
mod catalogs. These are offline dispatch tests, not BL2 controller acceptance.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace as NS
import unittest
from unittest.mock import patch

SOURCE = (Path(__file__).resolve().parents[1] / "mod" / "ControllerManager"
          / "sdk_actions.py")
EVENTS = NS(IE_Pressed=1, IE_Released=2, IE_Repeat=3)


def production():
    fake_mods = ModuleType("mods_base")
    fake_mods.EInputEvent = EVENTS
    spec = importlib.util.spec_from_file_location("cm_bug17_sdk_actions", SOURCE)
    module = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, {"mods_base": fake_mods, spec.name: module}):
        spec.loader.exec_module(module)
    return module


def mod(name, settings, action_id, log, *, hidden=False, rebindable=True,
        enabled=True, author=""):
    bind = NS(identifier=action_id, is_hidden=hidden,
              is_rebindable=rebindable, is_enabled=enabled,
              display_name=action_id + " action", description="testing",
              event_filter=None,
              callback=lambda event: log.append((name, event)))
    return NS(name=name, author=author, settings_file=settings, keybinds=[bind])


class DuplicateActionIdentityTests(unittest.TestCase):
    def setUp(self):
        self.sdk = production()
        self.events = []
        self.our_mod = object()
        self.a = mod("Owner A", "/mods/first/shared.json", "Heal",
                     self.events)
        self.b = mod("Owner B", "/mods/second/SHARED.JSON", "heal",
                     self.events)
        self.key = "settings/shared.json#heal"
        self.saved = self.sdk.Assignments()
        self.assertEqual(self.saved.change(self.key,
                                           self.sdk.Shortcut("A")), ())
        self.snapshot = self.saved.json()

    def dispatcher(self, catalog):
        warnings = []
        dispatcher = self.sdk.ShortcutDispatcher(
            lambda *warning: warnings.append(warning))
        dispatcher.configure(catalog, self.saved)
        return dispatcher, warnings

    def test_both_discovery_orders_quarantine_every_ambiguous_owner(self):
        for owners in ((self.a, self.b), (self.b, self.a)):
            with self.subTest(order=owners[0].name):
                catalog, collisions = self.sdk.discover(owners, self.our_mod)
                self.assertEqual(catalog, ())
                self.assertEqual(collisions, (self.key,))
                dispatcher, warnings = self.dispatcher(catalog)
                self.assertFalse(dispatcher.input(
                    "A", EVENTS.IE_Pressed, True, lambda *_: None))
                self.assertFalse(dispatcher.input(
                    "Start", EVENTS.IE_Pressed, True, lambda *_: None))
                self.assertEqual(dispatcher.lookup, {})
                self.assertEqual(self.events, [])
                self.assertEqual(self.saved.json(), self.snapshot)
                self.assertEqual(warnings, [])

    def test_removing_duplicate_restores_saved_shortcut_to_remaining_action(self):
        for survivor in (self.a, self.b):
            with self.subTest(survivor=survivor.name):
                catalog, collisions = self.sdk.discover(
                    [self.a, self.b], self.our_mod)
                dispatcher, _ = self.dispatcher(catalog)
                self.assertFalse(dispatcher.input(
                    "A", EVENTS.IE_Pressed, True, lambda *_: None))
                refreshed, collisions = self.sdk.discover(
                    [survivor], self.our_mod)
                self.assertEqual(collisions, ())
                self.assertEqual([action.key for action in refreshed],
                                 [self.key])
                dispatcher.configure(refreshed, self.saved)
                self.assertTrue(dispatcher.input(
                    "A", EVENTS.IE_Pressed, True, lambda *_: None))
                self.assertTrue(dispatcher.input(
                    "A", EVENTS.IE_Released, True, lambda *_: None))
                self.assertEqual(self.events[-2:],
                                 [(survivor.name, EVENTS.IE_Pressed),
                                  (survivor.name, EVENTS.IE_Released)])
                self.assertEqual(self.saved.json(), self.snapshot)

    def test_duplicate_added_during_hold_releases_old_owner_but_never_arms_new(self):
        catalog, _ = self.sdk.discover((self.a,), self.our_mod)
        dispatcher, _ = self.dispatcher(catalog)
        self.assertTrue(dispatcher.input(
            "A", EVENTS.IE_Pressed, True, lambda *_: None))
        ambiguous, conflicts = self.sdk.discover(
            [self.a, self.b], self.our_mod)
        self.assertEqual(conflicts, (self.key,))
        dispatcher.configure(ambiguous, self.saved)
        # Existing hold gets one terminal release, not a synthetic press.
        self.assertEqual(self.events, [
            ("Owner A", EVENTS.IE_Pressed),
            ("Owner A", EVENTS.IE_Released)])
        self.assertFalse(dispatcher.input(
            "A", EVENTS.IE_Pressed, True, lambda *_: None))
        self.assertEqual(len(self.events), 2)

    def test_same_settings_basename_with_different_actions_remains_usable(self):
        other = mod("Owner B", "/mods/second/shared.json", "OpenMenu",
                    self.events)
        unique_key = "settings/shared.json#openmenu"
        self.assertEqual(self.saved.change(unique_key,
                                           self.sdk.Shortcut("B")), ())
        catalog, collisions = self.sdk.discover((self.a, other), self.our_mod)
        self.assertEqual(collisions, ())
        self.assertEqual({action.key for action in catalog},
                         {self.key, unique_key})
        dispatcher, _ = self.dispatcher(catalog)
        self.assertTrue(dispatcher.input(
            "A", EVENTS.IE_Pressed, True, lambda *_: None))
        self.assertTrue(dispatcher.input(
            "B", EVENTS.IE_Pressed, True, lambda *_: None))
        self.assertEqual(self.events, [
            ("Owner A", EVENTS.IE_Pressed),
            ("Owner B", EVENTS.IE_Pressed)])

    def test_hidden_or_nonrebindable_duplicate_is_not_a_live_conflict(self):
        for kwargs in ({"hidden": True}, {"rebindable": False}):
            with self.subTest(kwargs=kwargs):
                other = mod("Hidden", "/other/shared.json", "HEAL",
                            self.events, **kwargs)
                catalog, collisions = self.sdk.discover(
                    (self.a, other), self.our_mod)
                self.assertEqual(collisions, ())
                self.assertEqual(len(catalog), 1)
                self.assertIs(catalog[0].mod, self.a)

    def test_duplicate_identifiers_within_one_mod_are_also_ambiguous(self):
        duplicate = mod("Duplicate", "/mods/one/shared.json", "hEaL",
                        self.events)
        self.a.keybinds.append(duplicate.keybinds[0])
        catalog, collisions = self.sdk.discover((self.a,), self.our_mod)
        self.assertEqual(catalog, ())
        self.assertEqual(collisions, (self.key,))
        self.assertEqual(self.saved.json(), self.snapshot)

    def test_author_name_fallback_duplicate_is_also_suppressed(self):
        x = mod("Sample", None, "test", self.events, author="Creator")
        y = mod("sample", None, "TEST", self.events, author="CREATOR")
        ident = "name/creator/sample#test"
        self.assertEqual(self.saved.change(ident,
                                           self.sdk.Shortcut("X")), ())
        for order in ((x, y), (y, x)):
            with self.subTest(order=order[0].name):
                catalog, collisions = self.sdk.discover(
                    order, self.our_mod)
                self.assertEqual(catalog, ())
                self.assertEqual(collisions, (ident,))
        self.assertIn(ident, self.saved.json()["bindings"])

    def test_other_unique_action_survives_when_one_key_collides(self):
        unique = mod("Independent", "/mods/other/unique.json", "Ping",
                     self.events)
        unique_key = "settings/unique.json#ping"
        self.assertEqual(self.saved.change(unique_key,
                                           self.sdk.Shortcut("B")), ())
        catalog, collisions = self.sdk.discover(
            (self.b, unique, self.a), self.our_mod)
        self.assertEqual(collisions, (self.key,))
        self.assertEqual([action.key for action in catalog], [unique_key])
        dispatcher, _ = self.dispatcher(catalog)
        self.assertFalse(dispatcher.input(
            "A", EVENTS.IE_Pressed, True, lambda *_: None))
        self.assertTrue(dispatcher.input(
            "B", EVENTS.IE_Pressed, True, lambda *_: None))
        self.assertEqual(self.events, [
            ("Independent", EVENTS.IE_Pressed)])
        self.assertIn(self.key, self.saved.json()["bindings"])

    def test_collisions_reported_once_and_in_deterministic_order(self):
        second = mod("Owner C", "/mods/third/shared.json", "Zoom",
                     self.events)
        third = mod("Owner D", "/mods/fourth/shared.json", "zOOm",
                    self.events)
        self.a.keybinds.append(self.a.keybinds[0])
        catalog, conflicts = self.sdk.discover(
            (second, self.a, third, self.b), self.our_mod)
        self.assertEqual(catalog, ())
        self.assertEqual(conflicts, (self.key, "settings/shared.json#zoom"))

    def test_our_own_action_is_not_considered_a_collision(self):
        catalog, conflicts = self.sdk.discover(
            (self.a, self.our_mod, self.a), self.our_mod)
        # This test deliberately repeats an external instance; both
        # occurrences are still ambiguous regardless of self exclusion.
        self.assertEqual(catalog, ())
        self.assertEqual(conflicts, (self.key,))
        catalog, conflicts = self.sdk.discover(
            (self.our_mod, self.a), self.our_mod)
        self.assertEqual(conflicts, ())
        self.assertEqual(len(catalog), 1)


if __name__ == "__main__":
    unittest.main()
