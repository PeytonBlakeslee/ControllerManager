"""SEC-06: execute ControllerManager's production shortcut schema and edit caps.

No UnrealSDK/game import. Run: python -B -m unittest tests/test_sec06_shortcut_schema.py -v
In a repo checkout: python -B -m unittest discover -s tests -p 'test_sec06_shortcut_schema.py' -v
"""
import importlib.util
import ast
import os
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import patch

SOURCE=Path(os.environ.get('CM_SEC06_SOURCE',Path(__file__).resolve().parents[1]
                            / 'mod/ControllerManager/sdk_actions.py'))
INIT_SOURCE=Path(os.environ.get('CM_SEC06_INIT_SOURCE',Path(__file__).resolve().parents[1]
                            / 'mod/ControllerManager/__init__.py'))


def production():
    fake=types.ModuleType('mods_base')
    fake.EInputEvent=types.SimpleNamespace(IE_Pressed=0,IE_Released=1,IE_Repeat=2)
    spec=importlib.util.spec_from_file_location('sec06_actions',SOURCE)
    module=importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules,{'mods_base':fake,spec.name:module}):
        spec.loader.exec_module(module)
    return module


class SecurityShortcutSchemaTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.api=production()

    def key(self,n):return 'settings/test.json#action'+str(n)
    def record(self,button='A',modifier=None):return {'button':button,'modifier':modifier}

    def test_valid_shortcuts_still_exact(self):
        for button in sorted(self.api.BUTTON_NAMES):
            for mod in [None,'Start','Back']:
                with self.subTest(button=button,modifier=mod):
                    value=self.api.Shortcut.from_json(self.record(button,mod))
                    self.assertEqual(value.to_json(),self.record(button,mod))
                    self.assertEqual(self.api.Shortcut.from_json(value.to_json()),value)

    def test_bad_button_types_raise_value_error_not_type_error(self):
        for value in ([],{},1,1.2,None,True,['A'],{'name':'A'},'A'*100000):
            with self.subTest(type=type(value).__name__):
                with self.assertRaises(ValueError):self.api.Shortcut.from_json(self.record(value))

    def test_bad_modifiers_raise_value_error_not_type_error(self):
        for value in ([],{},1,0,True,['Start'],{'button':'Back'},'Back'*100000):
            with self.subTest(type=type(value).__name__):
                with self.assertRaises(ValueError):self.api.Shortcut.from_json(self.record('A',value))

    def test_invalid_format_fails_cleanly(self):
        for value in (None,[],{},1,{'button':'A'},{'button':'A','modifier':None,'other':1}):
            with self.subTest(value=str(value)[:30]):
                with self.assertRaises(ValueError):self.api.Shortcut.from_json(value)

    def test_valid_records_survive_malformed_neighbor(self):
        valid1=self.key('first');valid2=self.key('last')
        records={valid1:self.record('A'),self.key('bad'):self.record([]),valid2:self.record('B','Start')}
        parsed=self.api.Assignments({'schema':1,'bindings':records})
        self.assertEqual(parsed.rejected,1)
        self.assertEqual(parsed.for_key(valid1),self.api.Shortcut('A'))
        self.assertEqual(parsed.for_key(valid2),self.api.Shortcut('B','Start'))
        self.assertIsNone(parsed.for_key(self.key('bad')))
        self.assertEqual(set(parsed.json()['bindings']),{valid1,valid2})
        # Constructor must not modify the original source mapping.
        self.assertEqual(records[self.key('bad')],self.record([]))

    def test_bad_keys_do_not_drop_neighbor_records(self):
        valid=self.key(0)
        bindings={valid:self.record('X'),'bad':self.record('A'),42:self.record('Y'),
                  '#':self.record('B'),'settings/huge.json#'+('x'*501):self.record('A')}
        parsed=self.api.Assignments({'schema':1,'bindings':bindings})
        self.assertEqual(parsed.rejected,3) # 'bad', int key, very long key
        self.assertEqual(len(parsed.saved),2)
        self.assertEqual(parsed.for_key(valid),self.api.Shortcut('X'))

    def test_bad_modifier_or_extra_key_only_skips_that_binding(self):
        data={'schema':1,'bindings':{self.key(0):self.record('A'),
            self.key(1):self.record('B',{}),self.key(2):{'button':'X','modifier':None,'extra':'x'}}}
        parsed=self.api.Assignments(data)
        self.assertEqual(parsed.rejected,2)
        self.assertEqual(set(parsed.saved),{self.key(0)})

    def test_top_level_bad_schema_is_rejected(self):
        for data in ([],{}, {'schema':True}, {'schema':'1'}, {'schema':2},
                     {'bindings':[]}, {'bindings':None}, {'bindings':[1]}):
            if data=={}:continue # empty configuration is supported
            with self.subTest(data=str(data)[:60]):
                with self.assertRaises(ValueError):self.api.Assignments(data)

    def test_exactly_1000_loads(self):
        bindings={self.key(i):self.record('A') for i in range(1000)}
        parsed=self.api.Assignments({'schema':1,'bindings':bindings})
        self.assertEqual(len(parsed.saved),1000)
        self.assertEqual(parsed.rejected,0)

    def test_1001_is_rejected(self):
        bindings={self.key(i):self.record('A') for i in range(1001)}
        with self.assertRaises(ValueError):self.api.Assignments({'schema':1,'bindings':bindings})

    def test_new_assignment_refused_when_full(self):
        parsed=self.api.Assignments({'bindings':{self.key(i):self.record('A') for i in range(1000)}})
        before=parsed.json()
        with self.assertRaises(ValueError):parsed.change(self.key(1001),self.api.Shortcut('B'))
        self.assertEqual(parsed.json(),before)

    def test_existing_assignment_can_change_when_full(self):
        parsed=self.api.Assignments({'bindings':{self.key(i):self.record('A') for i in range(1000)}})
        # New shortcut is unique, avoids collision with other A bindings.
        parsed.change(self.key(0),self.api.Shortcut('B'))
        self.assertEqual(len(parsed.saved),1000)
        self.assertEqual(parsed.for_key(self.key(0)),self.api.Shortcut('B'))

    def test_collision_replacement_does_not_break_cap(self):
        parsed=self.api.Assignments({'bindings':{self.key(i):self.record('A') for i in range(1000)}})
        # A collision with one existing assignment reduces size, permitting replacement.
        self.assertEqual(parsed.change(self.key(1001),self.api.Shortcut('A'),replace=True),())
        self.assertEqual(len(parsed.saved),1)
        self.assertEqual(parsed.for_key(self.key(1001)),self.api.Shortcut('A'))

    def test_conflict_without_replace_keeps_state(self):
        parsed=self.api.Assignments({'bindings':{self.key(0):self.record('A')}})
        self.assertEqual(parsed.change(self.key(1),self.api.Shortcut('A')),(self.key(0),))
        self.assertNotIn(self.key(1),parsed.saved)

    def test_edit_rejects_unsaveable_oversized_action_identity(self):
        parsed=self.api.Assignments()
        with self.assertRaises(ValueError):
            parsed.change('settings/example.json#'+('x'*501),self.api.Shortcut('A'))
        self.assertEqual(parsed.json()['bindings'],{})

    def test_delete_and_json_round_trip(self):
        parsed=self.api.Assignments({'bindings':{self.key(0):self.record('A'),self.key(1):self.record('B')}})
        parsed.change(self.key(1),None)
        self.assertEqual(self.api.Assignments(parsed.json()).json(),parsed.json())
        self.assertEqual(set(parsed.saved),{self.key(0)})

    def load_saved_store_block(self,raw,previous):
        """Execute the actual load_settings assignment try/except/else node."""
        tree=ast.parse(INIT_SOURCE.read_text(encoding='utf-8-sig'))
        # Match the inner assignment block, not its outer load_settings()
        # exception wrapper, which contains this text transitively.
        candidates=[n for n in ast.walk(tree) if isinstance(n,ast.Try)
                    and any(isinstance(stmt,ast.Assign)
                            and any(isinstance(target,ast.Name) and target.id=='loaded'
                                    for target in stmt.targets)
                            and isinstance(stmt.value,ast.Call)
                            and isinstance(stmt.value.func,ast.Name)
                            and stmt.value.func.id=='Assignments'
                            for stmt in n.body)]
        self.assertEqual(len(candidates),1)
        notifications=[]
        ns={'Assignments':self.api.Assignments,'sdk_assignments':types.SimpleNamespace(value=raw),
            '_sdk_store':previous,'_warn':lambda *args:notifications.append(args)}
        snippet=ast.fix_missing_locations(ast.Module(body=[candidates[0]],type_ignores=[]))
        exec(compile(snippet,str(INIT_SOURCE),'exec'),ns)
        return ns['_sdk_store'], notifications

    def test_loader_keeps_previous_valid_store_if_top_level_is_invalid(self):
        old=self.api.Assignments({'bindings':{self.key(1):self.record('X')}})
        for invalid in ([],{'schema':4}, {'bindings':[]},
                        {'bindings':{self.key(i):self.record('A') for i in range(1001)}}):
            current,warnings=self.load_saved_store_block(invalid,old)
            self.assertIs(current,old)
            self.assertEqual(len(warnings),1)
            self.assertIn('retaining prior valid bindings',warnings[0][1])

    def test_loader_warns_and_keeps_valid_neighbors(self):
        old=self.api.Assignments({'bindings':{self.key(3):self.record('X')}})
        raw={'bindings':{self.key(1):self.record('A'),self.key(2):self.record([])}}
        current,warnings=self.load_saved_store_block(raw,old)
        self.assertIsNot(current,old)
        self.assertEqual(current.for_key(self.key(1)),self.api.Shortcut('A'))
        self.assertIsNone(current.for_key(self.key(2)))
        self.assertEqual(current.rejected,1)
        self.assertEqual(raw['bindings'][self.key(2)],self.record([]))
        self.assertEqual(len(warnings),1)


if __name__=='__main__':unittest.main()
