#!/usr/bin/env python3
"""CPU regression for the installed manager's actual grammar_bitmask method.

Targets the v0.30.0 redesign: reasoning-boundary logic lives in
`_get_constraint_start` (extracted here as well) and draft tokens go through
`accept_tokens` directly, with a log + bitmask shutdown on rejection.

AST extraction avoids loading vLLM/CUDA. The matcher and tensor are test
doubles; live MTP tests separately exercise the real model and XGrammar
backend.
"""
import ast
import os
from pathlib import Path
from types import SimpleNamespace
import unittest

SOURCE_ROOT = Path(os.environ.get(
    'VLLM_SOURCE_ROOT', '/usr/local/lib/python3.12/dist-packages'))
SOURCE = SOURCE_ROOT / 'vllm' / 'v1' / 'structured_output' / '__init__.py'
UTILS = SOURCE_ROOT / 'vllm' / 'v1' / 'structured_output' / 'utils.py'


class Tensor:
    shape = (32,)

    def __getitem__(self, key):
        return self

    def numpy(self):
        return 'bitmask'


class Grammar:
    def __init__(self):
        self.tokens = []
        self.errors = []
        self.advances = []
        self.validations = []
        self.rollbacks = []

    def is_terminated(self):
        return False

    def accept_tokens(self, request_id, tokens):
        if any(token not in (1, 2) for token in tokens):
            self.errors.append(tokens)
            return False
        self.tokens.extend(tokens)
        self.advances.append(tokens)
        return True

    def validate_tokens(self, tokens):
        self.validations.append(tokens)
        return tokens if all(token in (1, 2) for token in tokens) else []

    def rollback(self, count):
        self.rollbacks.append(count)
        del self.tokens[-count:]


def _extract(path, class_name, names):
    tree = ast.parse(path.read_text())
    found = []
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            for member in node.body:
                if (isinstance(member, ast.FunctionDef)
                        and member.name in names):
                    found.append(member)
        elif isinstance(node, ast.FunctionDef) and node.name in names:
            found.append(node)
    missing = set(names) - {node.name for node in found}
    assert not missing, f'missing definitions in {path}: {missing}'
    return found


def manager(reasoner=True):
    methods = _extract(SOURCE, 'StructuredOutputManager',
                       ('_get_constraint_start', 'grammar_bitmask'))
    helpers = _extract(UTILS, None, ('strip_speculative_padding',))
    module = ast.Module(body=methods + helpers, type_ignores=[])
    logs = []
    namespace = {
        'TYPE_CHECKING': False,
        'logger': SimpleNamespace(error=lambda *a, **k: logs.append(a[1])),
        'ParserEngineReasoningAdapter': type('ParserEngineReasoningAdapter',
                                             (), {}),
    }
    import __future__
    exec(compile(ast.fix_missing_locations(module), str(SOURCE), 'exec',
                 flags=__future__.annotations.compiler_flag), namespace)
    instance = SimpleNamespace(
        vllm_config=SimpleNamespace(num_speculative_tokens=5,
                                    model_config=SimpleNamespace(is_diffusion=False)),
        _grammar_bitmask=Tensor(), fill_bitmask_parallel_threshold=128,
        enable_in_reasoning=False, masks=[], logs=logs,
    )
    instance._get_constraint_start = lambda *args: namespace[
        '_get_constraint_start'](instance, *args)
    if reasoner:
        instance._get_reasoner = lambda request: SimpleNamespace(
            is_reasoning_end=lambda history: False,
            is_reasoning_end_streaming=lambda history, delta: 42 in delta)
    else:
        instance._get_reasoner = lambda request: None
    instance._fill_bitmasks = lambda batch: instance.masks.extend(
        (index, enabled) for grammar, index, enabled in batch)
    instance.run = lambda requests, ids, drafts: namespace['grammar_bitmask'](
        instance, requests, ids, drafts)
    return instance


class ReasoningTests(unittest.TestCase):
    def run_tokens(self, tokens, reasoner=True):
        mgr, grammar = manager(reasoner), Grammar()
        request = SimpleNamespace(
            all_token_ids=[10, 11], prompt_token_ids=[10, 11],
            structured_output_request=SimpleNamespace(
                grammar=grammar, reasoning_ended=None))
        result = mgr.run({'r': request}, ['r'], {'r': tokens})
        self.assertEqual(result, 'bitmask')
        self.assertEqual(grammar.tokens, [])
        return mgr, grammar

    def test_invalid_post_reasoning_draft_stops_grammar(self):
        mgr, grammar = self.run_tokens([42, 99])
        self.assertEqual(grammar.errors, [[99]])
        self.assertEqual(grammar.validations, [])
        self.assertEqual(grammar.advances, [])
        self.assertEqual(grammar.rollbacks, [])
        self.assertEqual(mgr.logs, [99])
        # 42 unconstrained, rejected draft masked out, bonus disabled.
        self.assertEqual(mgr.masks, [(0, False), (1, True), (2, False)])

    def test_valid_post_reasoning_drafts_advance_and_rollback(self):
        mgr, grammar = self.run_tokens([42, 1, 2])
        self.assertEqual(grammar.validations, [])
        self.assertEqual(grammar.advances, [[1], [2]])
        self.assertEqual(grammar.rollbacks, [2])
        self.assertEqual(mgr.masks,
                         [(0, False), (1, True), (2, True), (3, True)])

    def test_reasoning_tokens_are_not_sent_to_grammar(self):
        mgr, grammar = self.run_tokens([99, 98, 42])
        self.assertEqual(grammar.errors, [])
        self.assertEqual(grammar.advances, [])
        self.assertFalse(mgr.masks[-2][1])
        self.assertTrue(mgr.masks[-1][1])

    def test_invalid_constrained_draft_logs_and_disables_bonus(self):
        mgr, grammar = self.run_tokens([99], reasoner=False)
        self.assertEqual(grammar.errors, [[99]])
        self.assertEqual(grammar.advances, [])
        self.assertEqual(mgr.logs, [99])
        self.assertEqual(mgr.masks, [(0, True), (1, False)])

    def test_valid_constrained_tokens_keep_existing_path(self):
        mgr, grammar = self.run_tokens([1, 2], reasoner=False)
        self.assertEqual(grammar.validations, [])
        self.assertEqual(grammar.advances, [[1], [2]])
        self.assertEqual(grammar.rollbacks, [2])
        self.assertEqual(mgr.masks, [(0, True), (1, True), (2, True)])

    def test_padding_does_not_advance(self):
        mgr, grammar = self.run_tokens([42, -1])
        self.assertEqual(grammar.advances, [])
        self.assertEqual(grammar.errors, [])
        self.assertFalse(mgr.masks[-1][1])

    def test_no_structured_requests(self):
        self.assertIsNone(manager().run({}, [], {}))


if __name__ == '__main__':
    unittest.main()
