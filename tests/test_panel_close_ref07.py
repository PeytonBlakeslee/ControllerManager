"""REF-07: freeze current close/popped guards, order and exception behavior.

For integration: place in ControllerManager/tests/. Source functions are loaded
from current __init__.py without importing the game or SDK.
"""
from __future__ import annotations
import ast
import os
from pathlib import Path
from types import SimpleNamespace as NS
import unittest

DEFAULT_SOURCE = Path(__file__).resolve().parents[1] / 'mod' / 'ControllerManager' / '__init__.py'
SOURCE = Path(os.environ.get('CM_REF07_SOURCE', str(DEFAULT_SOURCE)))


def load_close_functions(env):
    tree = ast.parse(SOURCE.read_text(encoding='utf-8-sig'), str(SOURCE))
    names = {'_pane_closed','_pane_popped'}
    nodes = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in names]
    if {node.name for node in nodes} != names:
        raise AssertionError('Required production panel close functions missing')
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(SOURCE), 'exec'), env)
    return env


class Session:
    def __init__(self, movie_ok=True, provider_ok=True, fail=None):
        self.events=[]
        self.movie_ok=movie_ok
        self.provider_ok=provider_ok
        self.fail=fail
        self.reenter=None
        self.env=None
        self.movie=object()
        self.provider=object()
        self.foreign=object()
        self.view=NS(belongs_to=self.belongs_to,
                     belongs_to_provider=self.belongs_to_provider,
                     close=self.close)
        self.env=dict(_native_view=self.view,
                      _side_panel=NS(reset_focus=lambda:self.step('reset_focus')),
                      _sdk_dialog=NS(stop=lambda:self.step('sdk_stop')),
                      _capture=NS(cancel=lambda:self.step('capture_cancel')),
                      _sdk_reset=NS(stop=lambda:self.step('reset_stop')),
                      _panel_modal_return=('modal',self.movie))
        load_close_functions(self.env)

    def step(self, name):
        self.events.append(name)
        if self.fail==name:
            raise RuntimeError('injected '+name)

    def belongs_to(self,obj):
        self.events.append('movie_guard')
        if self.fail=='movie_guard':
            raise RuntimeError('injected movie_guard')
        return obj is self.movie and self.movie_ok

    def belongs_to_provider(self,obj):
        self.events.append('provider_guard')
        if self.fail=='provider_guard':
            raise RuntimeError('injected provider_guard')
        return obj is self.provider and self.provider_ok

    def close(self):
        self.events.append('native_close')
        if self.fail=='native_close':
            raise RuntimeError('injected native_close')
        if self.reenter is not None:
            callback=self.reenter
            self.reenter=None
            callback()
        # Models NativeBindingView.close() clearing both ownership references.
        self.movie_ok=self.provider_ok=False

    def call_movie(self,obj=None):
        return self.env['_pane_closed'](self.movie if obj is None else obj,None,None,None)

    def call_provider(self,obj=None):
        return self.env['_pane_popped'](self.provider if obj is None else obj,None,None,None)


class PanelCloseContract(unittest.TestCase):
    TAIL=['native_close','reset_focus','sdk_stop','capture_cancel','reset_stop']

    def test_movie_close_success_order_and_modal_clear(self):
        s=Session()
        self.assertIsNone(s.call_movie())
        self.assertEqual(s.events, ['movie_guard'] + self.TAIL)
        self.assertIsNone(s.env['_panel_modal_return'])
        s.call_movie()
        self.assertEqual(s.events, ['movie_guard'] + self.TAIL + ['movie_guard'])

    def test_provider_pop_success_order_and_modal_clear(self):
        s=Session()
        s.call_provider()
        self.assertEqual(s.events, ['provider_guard'] + self.TAIL)
        self.assertIsNone(s.env['_panel_modal_return'])
        s.call_provider()
        self.assertEqual(s.events, ['provider_guard'] + self.TAIL + ['provider_guard'])

    def test_foreign_movie_does_not_clear_anything(self):
        s=Session()
        s.call_movie(s.foreign)
        self.assertEqual(s.events, ['movie_guard'])
        self.assertEqual(s.env['_panel_modal_return'][0], 'modal')

    def test_foreign_provider_does_not_clear_anything(self):
        s=Session()
        s.call_provider(s.foreign)
        self.assertEqual(s.events, ['provider_guard'])
        self.assertEqual(s.env['_panel_modal_return'][0], 'modal')

    def test_wrong_callback_identity_never_substitutes_other_guard(self):
        s=Session(movie_ok=False, provider_ok=True)
        s.call_movie()
        self.assertEqual(s.events, ['movie_guard'])
        s.call_provider()
        self.assertEqual(s.events, ['movie_guard','provider_guard']+self.TAIL)

    def test_cleanup_step_exception_stops_tail_with_first_error(self):
        for which in ('movie','provider'):
            for name in self.TAIL:
                with self.subTest(which=which,fail=name):
                    s=Session(fail=name)
                    with self.assertRaisesRegex(RuntimeError,'injected '+name):
                        (s.call_movie if which=='movie' else s.call_provider)()
                    guard=['movie_guard' if which=='movie' else 'provider_guard']
                    self.assertEqual(s.events, guard+self.TAIL[:self.TAIL.index(name)+1])
                    self.assertIsNone(s.env['_panel_modal_return'])

    def test_guard_exception_leaves_modal_and_skips_cleanup(self):
        for which,kind in (('movie','movie_guard'),('provider','provider_guard')):
            with self.subTest(which=which):
                s=Session(fail=kind)
                with self.assertRaisesRegex(RuntimeError,kind):
                    (s.call_movie if which=='movie' else s.call_provider)()
                self.assertEqual(s.events,[kind])
                self.assertIsNotNone(s.env['_panel_modal_return'])

    def test_reentrant_provider_pop_during_movie_close_runs_two_tails(self):
        s=Session()
        s.reenter=s.call_provider
        s.call_movie()
        self.assertEqual(s.events,['movie_guard','native_close','provider_guard']+self.TAIL+self.TAIL[1:])
        self.assertIsNone(s.env['_panel_modal_return'])

    def test_reentrant_movie_close_during_provider_pop_runs_two_tails(self):
        s=Session()
        s.reenter=s.call_movie
        s.call_provider()
        self.assertEqual(s.events,['provider_guard','native_close','movie_guard']+self.TAIL+self.TAIL[1:])
        self.assertIsNone(s.env['_panel_modal_return'])

if __name__=='__main__':
    unittest.main()
