"""Offline tests for Controller Manager layout and shortcut consolidation."""
import importlib.util
import pathlib
import sys
import types
import unittest
from unittest.mock import patch

MOD_ROOT = pathlib.Path(__file__).resolve().parents[1] / 'mod' / 'ControllerManager'

def load(name):
    spec=importlib.util.spec_from_file_location('cm_consolidation_'+name, MOD_ROOT / (name+'.py'))
    mod=importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, {spec.name: mod}):
        spec.loader.exec_module(mod)
    return mod

class LayoutTests(unittest.TestCase):
    def test_default_roundtrip_and_unbound(self):
        l=load('layout')
        baseline={key:key for key in l.KEYS}
        self.assertEqual(l.solve(baseline,{k:'Default' for k in l.KEYS}), baseline)
        self.assertEqual(l.encode(baseline,baseline), {k:'Default' for k in l.KEYS})
        swapped=l.assign(baseline,l.KEYS[0],l.KEYS[1],True)
        self.assertEqual(swapped[l.KEYS[0]],l.KEYS[1])
        self.assertEqual(swapped[l.KEYS[1]],l.KEYS[0])
        self.assertIsNone(l.assign(baseline,l.KEYS[0],None)[l.KEYS[0]])
        with self.assertRaises(ValueError):l.validate({l.KEYS[0]:l.KEYS[0]})

    def test_queued_edit_and_finalize(self):
        l=load('layout')
        baseline={key:key for key in l.KEYS}
        choices={c:{key:'Default' for key in l.KEYS} for c in l.CONTEXTS}
        old={'pending':[]}
        pending=l.queue(old,'vehicle',l.KEYS[0],'Unbound',False)
        self.assertEqual(old['pending'],[])
        result,meta=l.finalize(choices,pending,baseline)
        self.assertEqual(result['vehicle'][l.KEYS[0]],'Unbound')
        self.assertEqual(result['foot'][l.KEYS[0]],'Default')
        self.assertEqual(meta['pending'],[])
        self.assertEqual(meta['version'],3)
        self.assertEqual(meta['baseline'],baseline)

class ShortcutTests(unittest.TestCase):
    def test_serialization_collision_and_capture(self):
        fake_mods=types.ModuleType('mods_base')
        fake_mods.EInputEvent=types.SimpleNamespace(IE_Pressed=1,IE_Released=2,IE_Repeat=3)
        with patch.dict(sys.modules,{'mods_base':fake_mods}):
            a=load('sdk_actions')
        shortcut=a.Shortcut('A','Start')
        self.assertEqual(a.Shortcut.from_json(shortcut.to_json()),shortcut)
        bindings=a.Assignments()
        self.assertFalse(bindings.change('owner#first',shortcut))
        self.assertEqual(bindings.collisions('owner#second',shortcut),['owner#first'])
        capture=a.CaptureState()
        self.assertEqual(capture.press('Start'),'next')
        self.assertEqual(capture.press('A'),'save')
        self.assertEqual(capture.value,shortcut)

    def test_hold_dispatch_and_cancel_release(self):
        fake_mods=types.ModuleType('mods_base')
        events=types.SimpleNamespace(IE_Pressed=1,IE_Released=2,IE_Repeat=3)
        fake_mods.EInputEvent=events
        with patch.dict(sys.modules,{'mods_base':fake_mods}):
            a=load('sdk_actions')
        calls=[]
        binding=types.SimpleNamespace(identifier='test',is_hidden=False,is_rebindable=True,
                        is_enabled=True,callback=lambda event:calls.append(event),event_filter=None)
        mod=types.SimpleNamespace(keybinds=[binding])
        action=a.Action('mod#test','test','Test','',mod,binding)
        assignments=a.Assignments(); assignments.change('mod#test',a.Shortcut('A'))
        dispatch=a.ShortcutDispatcher(lambda *_a:calls.append('warn'))
        dispatch.configure([action],assignments)
        self.assertTrue(dispatch.input('A',events.IE_Pressed,True,lambda _:None))
        dispatch.cancel()
        self.assertEqual(calls,[events.IE_Pressed,events.IE_Released])
        self.assertTrue(dispatch.input('A',events.IE_Released,True,lambda _:None))
        self.assertEqual(calls,[events.IE_Pressed,events.IE_Released])

if __name__=='__main__':unittest.main()
