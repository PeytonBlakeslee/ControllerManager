"""REF-10/11: ControllerManager's confirmed reset transaction, isolated from SDK.

Compiles the real production function AST and uses the repo's pure layout.py
policy. No UnrealSDK import, input hooks or real settings files.
"""
from __future__ import annotations

import ast
import copy
import importlib.util
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

ROOT=Path(__file__).resolve().parents[1]
SOURCE=ROOT/'mod'/'ControllerManager'/'__init__.py'
LAYOUT=ROOT/'mod'/'ControllerManager'/'layout.py'

class Option:
    def __init__(self,value): self.value=value

class Assignments:
    def __init__(self,serialized=None):
        self.saved=copy.deepcopy((serialized or {}).get('bindings',{}))
    def json(self): return {'schema':1,'bindings':copy.deepcopy(self.saved)}
    def clear(self):self.saved.clear()


def load_preferences():
    spec=importlib.util.spec_from_file_location('phase04e_controller_layout',LAYOUT)
    module=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def make_harness(*,baseline_present=True):
    prefs=load_preferences()
    ast_tree=ast.parse(SOURCE.read_text(encoding='utf-8-sig'),filename=str(SOURCE))
    functions=[n for n in ast_tree.body if isinstance(n,ast.FunctionDef) and n.name=='_commit_confirmed_reset']
    if len(functions)!=1:raise AssertionError('Production confirmed-reset function missing/duplicate')
    baseline=dict(zip(prefs.KEYS,prefs.KEYS))
    bindings={c:{k:Option('Default') for k in prefs.KEYS} for c in ('foot','vehicle')}
    bindings['foot'][prefs.KEYS[0]].value='Unbound'
    bindings['vehicle'][prefs.KEYS[1]].value='Unbound'
    sensitive={key:Option('1.75') for key in ('hip_x','hip_y','ads_x','ads_y','vehicle_x','vehicle_y')}
    store=Assignments({'schema':1,'bindings':{
        'settings/first.json#jump':{'button':'A','modifier':None},
        'settings/first.json#fire':{'button':'B','modifier':None},
        'settings/second.json#dash':{'button':'X','modifier':None}}})
    metadata={'version':3,'baseline':baseline,'pending':[]} if baseline_present else {'pending':[]}
    actions=[]; disk=[]
    env={
        'Assignments':Assignments,'copy':copy,'preferences':prefs,
        'BINDINGS':bindings,'SENSITIVITY':sensitive,
        '_sdk_store':store,'sdk_assignments':Option(store.json()),
        'runtime_state':Option(copy.deepcopy(metadata)),
        'layouts_enabled':Option(True),'auto_swap':Option(False),
        '_configuration_baseline':lambda: baseline if baseline_present else None,
        '_panel_values_revision':0,'_choices_cache':None,'_preference_generation':0,
        '_sdk_actions_cache':(), '_sdk_dispatch':SimpleNamespace(configure=Mock()),
        '_active':False,'_syncing':False,'_look_values':'old',
        '_invalidate_display':Mock(),'_clear_context':Mock(),
        '_bindings':SimpleNamespace(invalidate_layout=Mock()),
        '_request_binding_sync':Mock(),'_cached_pc':Mock(return_value=None),
        '_configure_look_hooks':Mock(),'_configure_restart_recovery':Mock(),
        '_configure_physical_input_hook':Mock(), '_refresh_binding_rows':Mock(),
        '_warn':Mock(), '_restart_rebind_armed':True, '_restart_rebind_ready':2,
        '_restart_rebind_started':10.0, '_travel_resume_armed':True,
        '_travel_resume_pending':True,'_travel_resume_running':True,
    }
    def choices():return {c:{k:opt.value for k,opt in group.items()} for c,group in env['BINDINGS'].items()}
    env['_choices']=choices
    def save():
        actions.append('save')
        disk.append(copy.deepcopy({
            'foot':choices()['foot'], 'vehicle':choices()['vehicle'],
            'sensitivity':{k:o.value for k,o in sensitive.items()},
            'runtime_state':env['runtime_state'].value,
            'sdk_assignments':env['sdk_assignments'].value,
            'layouts_enabled':env['layouts_enabled'].value,
            'auto_swap':env['auto_swap'].value,
        }))
    env['_save_settings']=save
    exec(compile(ast.Module(body=functions,type_ignores=[]),str(SOURCE),'exec'),env)
    return SimpleNamespace(env=env,prefs=prefs,bindings=bindings,sensitive=sensitive,
                           actions=actions,disk=disk,store=store,commit=env['_commit_confirmed_reset'])


class ControllerManagerResetContractTests(unittest.TestCase):
    def test_defaults_are_explicit_and_scoped(self):
        h=make_harness()
        self.assertEqual(len(h.prefs.KEYS),14)
        self.assertEqual(set(h.bindings),{'foot','vehicle'})
        self.assertEqual(len(h.bindings['foot']),14)
        self.assertEqual(len(h.bindings['vehicle']),14)
        self.assertEqual(h.env['layouts_enabled'].value,True)
        self.assertEqual(h.env['auto_swap'].value,False)

    def test_fresh_constructor_defaults_from_production_ast(self):
        tree=ast.parse(SOURCE.read_text(encoding='utf-8-sig'),filename=str(SOURCE))
        assignments={node.targets[0].id:node.value for node in tree.body
                     if isinstance(node,ast.Assign) and len(node.targets)==1
                     and isinstance(node.targets[0],ast.Name)}
        for key,expected in (('layouts_enabled',False),('auto_swap',True)):
            call=assignments[key]
            value=next(kw.value for kw in call.keywords if kw.arg=='value')
            self.assertIs(ast.literal_eval(value),expected)
        bindings=assignments['BINDINGS']
        sensitivity=assignments['SENSITIVITY']
        binding_calls=[n for n in ast.walk(bindings) if isinstance(n,ast.Call)
                       and isinstance(n.func,ast.Name) and n.func.id=='BindingOption']
        sensitivity_calls=[n for n in ast.walk(sensitivity) if isinstance(n,ast.Call)
                           and isinstance(n.func,ast.Name) and n.func.id=='SensitivityOption']
        self.assertEqual(len(binding_calls),1)
        self.assertEqual(len(sensitivity_calls),1)
        self.assertEqual(ast.literal_eval(next(k.value for k in binding_calls[0].keywords if k.arg=='value')),'Default')
        self.assertEqual(ast.literal_eval(next(k.value for k in sensitivity_calls[0].keywords if k.arg=='value')),'1.00')

    def test_walking_reset_preserves_vehicle_and_shortcuts(self):
        h=make_harness()
        before=copy.deepcopy(h.env['sdk_assignments'].value)
        h.commit('foot')
        self.assertTrue(all(o.value=='Default' for o in h.bindings['foot'].values()))
        self.assertEqual(h.bindings['vehicle'][h.prefs.KEYS[1]].value,'Unbound')
        self.assertTrue(all(h.sensitive[k].value=='1.00' for k in ('hip_x','hip_y','ads_x','ads_y')))
        self.assertEqual(h.sensitive['vehicle_x'].value,'1.75')
        self.assertEqual(h.env['sdk_assignments'].value,before)
        self.assertEqual(h.actions,['save'])
        h.env['_bindings'].invalidate_layout.assert_called_once()

    def test_vehicle_reset_preserves_walking_and_shortcuts(self):
        h=make_harness()
        h.commit('vehicle')
        self.assertTrue(all(o.value=='Default' for o in h.bindings['vehicle'].values()))
        self.assertEqual(h.bindings['foot'][h.prefs.KEYS[0]].value,'Unbound')
        self.assertEqual(h.sensitive['hip_x'].value,'1.75')
        self.assertEqual(h.sensitive['vehicle_x'].value,'1.00')
        self.assertEqual(h.sensitive['vehicle_y'].value,'1.00')
        self.assertEqual(len(h.env['_sdk_store'].saved),3)
        self.assertEqual(h.actions,['save'])

    def test_sdk_group_reset_preserves_other_mod_and_all_native_values(self):
        h=make_harness()
        old_choices=h.env['_choices']()
        old_sensitivity={k:o.value for k,o in h.sensitive.items()}
        h.commit('sdk:settings/first.json')
        self.assertEqual(set(h.env['_sdk_store'].saved),{'settings/second.json#dash'})
        self.assertEqual(h.env['_choices'](),old_choices)
        self.assertEqual({k:o.value for k,o in h.sensitive.items()},old_sensitivity)
        h.env['_bindings'].invalidate_layout.assert_not_called()
        h.env['_sdk_dispatch'].configure.assert_called_once()
        self.assertEqual(h.actions,['save'])

    def test_reset_all_resets_every_group_and_settings_with_one_save(self):
        h=make_harness()
        h.commit('all')
        for context in ('foot','vehicle'):
            self.assertTrue(all(opt.value=='Default' for opt in h.bindings[context].values()))
        self.assertTrue(all(opt.value=='1.00' for opt in h.sensitive.values()))
        self.assertFalse(h.env['layouts_enabled'].value)
        self.assertTrue(h.env['auto_swap'].value)
        self.assertEqual(h.env['_sdk_store'].saved,{})
        self.assertEqual(h.env['sdk_assignments'].value['bindings'],{})
        self.assertEqual(h.actions,['save'])
        self.assertEqual(h.env['runtime_state'].value['pending'],[])
        self.assertEqual(h.env['_panel_values_revision'],1)

    def test_active_reset_all_disarms_transition_watchers(self):
        h=make_harness()
        h.env['_active']=True
        h.commit('all')
        for key in ('_restart_rebind_armed','_travel_resume_armed','_travel_resume_pending','_travel_resume_running'):
            self.assertFalse(h.env[key])
        h.env['_configure_restart_recovery'].assert_called_once()
        h.env['_configure_physical_input_hook'].assert_called_once()
        h.env['_request_binding_sync'].assert_called_once()
        self.assertEqual(h.actions,['save'])

    def test_queued_reset_when_baseline_not_available(self):
        h=make_harness(baseline_present=False)
        h.env['runtime_state'].value={'pending':[dict(context='vehicle',action=h.prefs.KEYS[2],value='Unbound',swap=True)]}
        h.commit('foot')
        self.assertEqual([q['context'] for q in h.env['runtime_state'].value['pending']],['vehicle','foot'])
        self.assertEqual(h.env['runtime_state'].value['pending'][-1]['action'],'reset')
        self.assertEqual(h.bindings['foot'][h.prefs.KEYS[0]].value,'Unbound')
        self.assertEqual(h.actions,['save'])
        # Actual pure preferences code consumes queued edits after a verified baseline.
        baseline=dict(zip(h.prefs.KEYS,h.prefs.KEYS))
        choices,meta=h.prefs.finalize(h.env['_choices'](),h.env['runtime_state'].value,baseline)
        self.assertTrue(all(x=='Default' for x in choices['foot'].values()))
        self.assertEqual(choices['vehicle'][h.prefs.KEYS[2]],'Unbound')
        self.assertEqual(meta['pending'],[])

    def test_invalid_scope_leaves_everything_unchanged(self):
        h=make_harness()
        before=h.env['_choices'](); before_sdk=h.env['sdk_assignments'].value
        with self.assertRaisesRegex(ValueError,'Invalid reset scope'):h.commit('other')
        self.assertEqual(h.env['_choices'](),before)
        self.assertEqual(h.env['sdk_assignments'].value,before_sdk)
        self.assertEqual(h.actions,[])

    def test_save_exception_rolls_back_memory_and_does_not_publish(self):
        h=make_harness()
        before=h.env['_choices'](); before_runtime=copy.deepcopy(h.env['runtime_state'].value)
        before_sdk=copy.deepcopy(h.env['sdk_assignments'].value)
        before_sens={k:o.value for k,o in h.sensitive.items()}
        h.env['_save_settings']=Mock(side_effect=OSError('disk full'))
        with self.assertRaisesRegex(OSError,'disk full'):h.commit('all')
        self.assertEqual(h.env['_choices'](),before)
        self.assertEqual(h.env['runtime_state'].value,before_runtime)
        self.assertEqual(h.env['sdk_assignments'].value,before_sdk)
        self.assertEqual({k:o.value for k,o in h.sensitive.items()},before_sens)
        self.assertEqual(h.env['_sdk_store'].saved,h.store.saved)
        self.assertFalse(h.disk)
        h.env['_sdk_dispatch'].configure.assert_not_called()

    def test_persisted_snapshot_can_reopen_without_losing_unrelated_state(self):
        h=make_harness()
        h.commit('foot')
        snapshot=copy.deepcopy(h.disk[0])
        reopened=make_harness()
        for c in ('foot','vehicle'):
            for k,option in reopened.bindings[c].items():option.value=snapshot[c][k]
        reopened.env['runtime_state'].value=snapshot['runtime_state']
        reopened.env['sdk_assignments'].value=snapshot['sdk_assignments']
        for k,opt in reopened.sensitive.items():opt.value=snapshot['sensitivity'][k]
        self.assertTrue(all(o.value=='Default' for o in reopened.bindings['foot'].values()))
        self.assertEqual(reopened.bindings['vehicle'][reopened.prefs.KEYS[1]].value,'Unbound')
        self.assertEqual(len(reopened.env['sdk_assignments'].value['bindings']),3)
        self.assertEqual(reopened.env['runtime_state'].value['pending'],[])

    def test_menu_requires_confirmation_and_scopes_are_distinct(self):
        tree=ast.parse(SOURCE.read_text(encoding='utf-8-sig'),filename=str(SOURCE))
        source=ast.unparse(tree)
        for scope in ('foot','vehicle','all'):
            self.assertIn(f"_confirm_reset('{scope}')",source)
        reset_dialog=[x for x in tree.body if isinstance(x,ast.ClassDef) and x.name=='ResetDialog']
        self.assertEqual(len(reset_dialog),1)
        self.assertIn('_commit_confirmed_reset(scope)',ast.unparse(reset_dialog[0]))
        self.assertIn("_confirm_reset('sdk:' + group_id)",source)

if __name__=='__main__':unittest.main()
