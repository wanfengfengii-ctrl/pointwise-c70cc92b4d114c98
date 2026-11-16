import copy
from fractions import Fraction as F
import random
import sqlite3
import tempfile
import unittest
from pathlib import Path

from catalog import Catalog
from classifier import Classifier, ZeroEvidence
from exact import ModelError, UnknownSymbol
import sdk1
import sdk2
from samples import (build_legacy, cycle_model, explicit_model, plain_model,
                     populate, retry_model)


def independent_solve(matrix, rhs):
    """Test-only global equations; no production loader, solve or executor."""
    a = [list(row) for row in matrix]
    b = list(rhs)
    n = len(b)
    for col in range(n):
        pivot = next(r for r in range(col, n) if a[r][col])
        a[col], a[pivot] = a[pivot], a[col]
        b[col], b[pivot] = b[pivot], b[col]
        for row in range(col + 1, n):
            multiplier = a[row][col] / a[col][col]
            for j in range(col, n):
                a[row][j] -= multiplier * a[col][j]
            b[row] -= multiplier * b[col]
    result = [F(0)] * n
    for row in range(n - 1, -1, -1):
        result[row] = (b[row] - sum(
            (a[row][j] * result[j] for j in range(row + 1, n)), F(0)
        )) / a[row][row]
    return result


def reference(payload, symbols):
    states = payload['states']
    n, length = len(states), len(symbols)
    size = n * (length + 1)
    system = [[F(i == j) for j in range(size)] for i in range(size)]
    for pos in range(length + 1):
        for edge in payload['edges']:
            src, dst = states.index(edge['src']), states.index(edge['dst'])
            if edge['symbol'] is None:
                system[pos * n + src][pos * n + dst] -= F(edge['weight'])
            elif pos < length and edge['symbol'] == symbols[pos]:
                system[pos * n + src][(pos + 1) * n + dst] -= F(edge['weight'])
    terminal = [F(0)] * (size - n) + [F(v) for v in payload['final']]
    suffix = independent_solve(system, terminal)
    initial = [F(v) for v in payload['initial']]
    evidence = sum((initial[i] * suffix[i] for i in range(n)), F(0))
    if not evidence:
        return evidence, None
    visits = {}
    for state, label in enumerate(states):
        marked = [suffix[i] if i % n == state else F(0) for i in range(size)]
        numerator = independent_solve(system, marked)
        visits[label] = sum((initial[i] * numerator[i] for i in range(n)), F(0)) / evidence
    return evidence, visits


def enumerate_explicit(payload, symbols):
    states = payload['states']
    totals = {edge['id']: F(0) for edge in payload['edges']}
    evidence = F(0)

    def walk(state, pos, weight, history):
        nonlocal evidence
        if pos == len(symbols):
            weight *= F(payload['final'][states.index(state)])
            evidence += weight
            for edge_id in history:
                totals[edge_id] += weight
            return
        for edge in payload['edges']:
            if edge['src'] == state and edge['symbol'] == symbols[pos]:
                walk(edge['dst'], pos + 1, weight * F(edge['weight']),
                     history + (edge['id'],))

    for state, weight in zip(states, payload['initial']):
        walk(state, 0, F(weight), ())
    return evidence, None if not evidence else {k: v / evidence for k, v in totals.items()}


class SDK1Tests(unittest.TestCase):
    def test_hand_computed_silent_self_loop(self):
        model = sdk1.load(retry_model())
        for length in range(5):
            result = sdk1.evaluate(model, ('x',) * length)
            self.assertEqual(result.evidence, F(2) * F(2, 3) ** length)
            self.assertEqual(result.visits, {'collector': F(2 * (length + 1))})
        empty = sdk1.evaluate(sdk1.load(plain_model()), ())
        self.assertEqual(empty.evidence, F(1))
        self.assertEqual(empty.visits, {'collector': F(1)})

    def test_cycle_before_after_and_unreachable(self):
        payload = cycle_model()
        model = sdk1.load(payload)
        empty = sdk1.evaluate(model, ())
        self.assertEqual(empty.evidence, F(3, 10))
        self.assertEqual(empty.visits, {
            'capture': F(6, 5), 'retry': F(6, 5), 'accepted': F(0), 'unused': F(0),
        })
        for symbols in ((), ('x',), ('x', 'y'), ('y',), ('x', 'x')):
            expected = reference(payload, symbols)
            actual = sdk1.evaluate(model, symbols)
            self.assertEqual((actual.evidence, actual.visits), expected)

    def test_fixed_seed_global_equation_reference(self):
        rng = random.Random(92641)
        for case in range(16):
            states = ['a', 'b', 'c']
            payload = {
                'states': states, 'alphabet': ['x', 'y'],
                'initial': [str(rng.randrange(3)) for _ in states],
                'final': [str(rng.randrange(3)) for _ in states],
                'edges': [],
            }
            for i, src in enumerate(states):
                for j, dst in enumerate(states):
                    for symbol in (None, 'x', 'y'):
                        # Each silent row sum is at most 3/6.
                        weight = F(rng.randrange(2), 6) if symbol is None else F(rng.randrange(3), 4)
                        payload['edges'].append({
                            'id': str((i, j, symbol)), 'src': src, 'dst': dst,
                            'symbol': symbol, 'weight': str(weight),
                        })
            for symbols in ((), ('x',), ('y', 'x')):
                with self.subTest(case=case, symbols=symbols):
                    actual = sdk1.evaluate(sdk1.load(payload), symbols)
                    self.assertEqual((actual.evidence, actual.visits), reference(payload, symbols))

    def test_zero_evidence_and_input_validation(self):
        model = sdk1.load(cycle_model())
        result = sdk1.evaluate(model, ('y',))
        self.assertEqual(result.evidence, F(0))
        self.assertIsNone(result.visits)
        for symbols in ('x', ['unknown'], [None], [1]):
            with self.assertRaises(UnknownSymbol):
                sdk1.evaluate(model, symbols)

    def test_model_validation(self):
        variants = []
        payload = retry_model('2/3')
        variants.append(payload)
        payload = retry_model()
        payload['initial'] = [0.5]
        variants.append(payload)
        payload = retry_model()
        payload['edges'][1]['weight'] = '-1'
        variants.append(payload)
        payload = retry_model()
        payload['edges'][1]['id'] = 'retry'
        variants.append(payload)
        payload = retry_model()
        payload['edges'][1]['dst'] = 'missing'
        variants.append(payload)
        payload = retry_model()
        payload['states'] *= 2
        variants.append(payload)
        for payload in variants:
            with self.subTest(payload=payload), self.assertRaises(ModelError):
                sdk1.load(payload)


class SDK2Tests(unittest.TestCase):
    def test_public_explicit_port(self):
        model = sdk2.load(explicit_model())
        result = sdk2.evaluate(model, ('x', 'x'))
        self.assertEqual(result.evidence, F(1))
        self.assertEqual(result.edge_counts, {'stay': F(3, 4), 'finish': F(3, 4), 'tail': F(1, 2)})
        empty = sdk2.evaluate(model, ())
        self.assertEqual(empty.evidence, F(1))
        self.assertEqual(empty.edge_counts, {'stay': F(0), 'finish': F(0), 'tail': F(0)})
        with self.assertRaises(ModelError):
            sdk2.load(retry_model())
        with self.assertRaises(ModelError):
            sdk2.load(retry_model('0'))

    def test_hand_compatible_symbol_only_model(self):
        payload = plain_model()
        for length in range(4):
            symbols = ('x',) * length
            old = sdk1.evaluate(sdk1.load(payload), symbols)
            new = sdk2.evaluate(sdk2.load(payload), symbols)
            self.assertEqual(old.evidence, F(1, 3) ** length)
            self.assertEqual(new.evidence, old.evidence)
            self.assertEqual(old.visits, {'collector': F(length + 1)})
            self.assertEqual(new.edge_counts, {'read': F(length)})

    def test_explicit_enumeration_parallel_edges_and_zero(self):
        payload = explicit_model()
        extra = copy.deepcopy(payload['edges'][0])
        extra['id'], extra['weight'] = 'parallel', '1/7'
        payload['edges'].append(extra)
        zero = copy.deepcopy(extra)
        zero['id'], zero['weight'] = 'zero', '0'
        payload['edges'].append(zero)
        for length in range(5):
            actual = sdk2.evaluate(sdk2.load(payload), ('x',) * length)
            self.assertEqual((actual.evidence, actual.edge_counts),
                             enumerate_explicit(payload, ('x',) * length))
        payload['final'] = ['0', '0']
        result = sdk2.evaluate(sdk2.load(payload), ())
        self.assertEqual(result.evidence, F(0))
        self.assertIsNone(result.edge_counts)
        with self.assertRaises(UnknownSymbol):
            sdk2.evaluate(sdk2.load(explicit_model()), ['y'])


class CatalogTests(unittest.TestCase):
    def setUp(self):
        self.catalog = populate(Catalog())
        self.classifier = Classifier(self.catalog)

    def tearDown(self):
        self.catalog.close()

    def test_fixed_revision_prior_and_original_identity(self):
        old = self.classifier.classify('frozen', ('x',))
        new = self.classifier.classify('current', ('x',))
        self.assertEqual(old.evidence, {'retry': F(4, 3), 'plain': F(1, 3)})
        self.assertEqual(old.probabilities, {'retry': F(2, 3), 'plain': F(1, 3)})
        self.assertEqual(new.probabilities, {'retry': F(8, 17), 'plain': F(9, 17)})
        self.assertEqual(old.visits['retry'], {'collector': F(4)})
        self.assertEqual(new.visits['retry'], {'collector': F(8, 3)})
        self.assertEqual(self.catalog.task('frozen')[0], ('retry', 'retrying', 1, F(1)))
        self.assertEqual(self.catalog.task('current')[0], ('retry', 'retrying', 2, F(1)))

    def test_audit_is_durable_value_snapshot(self):
        result = self.classifier.classify('frozen', (s for s in ['x']))
        audit = self.catalog.audits('frozen')[-1]
        self.assertEqual(audit['symbols'], ('x',))
        self.assertEqual(audit['bindings'], {'retry': ('retrying', 1), 'plain': ('plain', 1)})
        self.assertEqual(audit['result']['visits'], result.visits)
        result.visits['retry']['collector'] = F(999)
        audit['result']['visits']['retry']['collector'] = F(888)
        self.assertEqual(self.catalog.audits('frozen')[-1]['result']['visits']['retry'],
                         {'collector': F(4)})
        body = self.catalog.get_model('retrying', 1)
        body['edges'].clear()
        self.assertEqual(len(self.catalog.get_model('retrying', 1)['edges']), 2)

    def test_zero_category_zero_prior_and_failure_no_audit(self):
        dead = plain_model()
        dead['final'] = ['0']
        self.catalog.save_model('dead', dead)
        self.catalog.create_task('mixed', {
            'dead': ('dead', None, 1), 'live': ('plain', None, 1),
            'ignored': ('retrying', 1, 0),
        })
        result = self.classifier.classify('mixed', ())
        self.assertEqual(result.probabilities, {'dead': F(0), 'live': F(1), 'ignored': F(0)})
        self.assertIsNone(result.visits['dead'])
        self.assertEqual(result.visits['ignored'], {'collector': F(2)})
        self.catalog.create_task('impossible', {'dead': ('dead', None, 1)})
        with self.assertRaises(ZeroEvidence):
            self.classifier.classify('impossible', ())
        self.assertEqual(self.catalog.audits('impossible'), ())
        count = len(self.catalog.audits('frozen'))
        with self.assertRaises(UnknownSymbol):
            self.classifier.classify('frozen', ['bad'])
        self.assertEqual(len(self.catalog.audits('frozen')), count)

    def test_task_validation_is_atomic(self):
        invalid = [
            {}, {'bad': ('plain', None, -1)}, {'bad': ('plain', None, 0)},
            {'bad': ('missing', None, 1)}, {'bad': ('plain', 99, 1)},
            {'ok': ('plain', 1, 1), 'other': ('cycle', 1, 1)},
        ]
        for i, choices in enumerate(invalid):
            task_id = 'invalid-' + str(i)
            with self.assertRaises((ValueError, KeyError)):
                self.catalog.create_task(task_id, choices)
            with self.assertRaises(KeyError):
                self.catalog.task(task_id)
        with self.assertRaises(ValueError):
            self.catalog.create_task('frozen', {'x': ('plain', None, 1)})

    def test_outer_failure_and_nested_savepoint(self):
        before = tuple(self.catalog.db.iterdump())

        def fail():
            raise RuntimeError('commit barrier')

        with self.assertRaisesRegex(RuntimeError, 'commit barrier'):
            with self.catalog.atomic(before_commit=fail):
                self.catalog.save_model('retrying', retry_model('0'))
                self.catalog.create_task('rolled-back', {'r': ('retrying', None, 1)})
                self.classifier.classify('rolled-back', ())
        self.assertEqual(tuple(self.catalog.db.iterdump()), before)
        with self.catalog.atomic():
            try:
                with self.catalog.atomic():
                    self.catalog.save_model('aborted', plain_model())
                    raise ValueError('inner')
            except ValueError:
                pass
            self.catalog.save_model('survivor', plain_model())
        with self.assertRaises(KeyError):
            self.catalog.get_model('aborted')
        self.assertEqual(self.catalog.get_model('survivor')['initial'], ['1'])

    def test_real_sqlite_audit_failure_rolls_back(self):
        before = len(self.catalog.audits('frozen'))
        self.catalog.db.execute(
            "CREATE TRIGGER refuse_audit BEFORE INSERT ON classification_runs BEGIN SELECT RAISE(ABORT,'audit offline'); END"
        )
        with self.assertRaisesRegex(sqlite3.IntegrityError, 'audit offline'):
            self.classifier.classify('frozen', ())
        self.assertEqual(len(self.catalog.audits('frozen')), before)
        self.assertFalse(self.catalog.db.in_transaction)
        self.catalog.db.execute('DROP TRIGGER refuse_audit')
        self.classifier.classify('frozen', ())
        self.assertEqual(len(self.catalog.audits('frozen')), before + 1)


class DiskTests(unittest.TestCase):
    def test_complete_legacy_build_reopen_and_continue(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'legacy.sqlite'
            build_legacy(path)
            with Catalog(path) as catalog:
                self.assertEqual(catalog.db.execute(
                    "SELECT value FROM meta WHERE key='schema_version'"
                ).fetchone(), ('1',))
                self.assertEqual(catalog.db.execute('SELECT count(*) FROM model_revisions').fetchone(), (4,))
                self.assertEqual(len(catalog.audits('frozen')), 2)
                self.assertEqual(len(catalog.audits('current')), 1)
                classifier = Classifier(catalog)
                old = classifier.classify('frozen', ('x',))
                self.assertEqual(catalog.save_model('retrying', retry_model('0')), 3)
                catalog.create_task('continued', {
                    'retry': ('retrying', None, 1), 'plain': ('plain', None, 2),
                })
                self.assertEqual(classifier.classify('frozen', ('x',)), old)
                current = classifier.classify('continued', ('x',))
                self.assertEqual(current.probabilities, {'retry': F(1, 3), 'plain': F(2, 3)})
            with Catalog(path) as reopened:
                self.assertEqual(Classifier(reopened).classify('frozen', ('x',)), old)
                self.assertEqual(Classifier(reopened).classify('continued', ('x',)), current)
                self.assertEqual(reopened.audits('frozen')[0]['result']['visits']['retry'],
                                 {'collector': F(2)})
                self.assertEqual(reopened.db.execute('PRAGMA foreign_key_check').fetchall(), [])

    def test_failed_publication_reopen_and_retry(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'catalog.sqlite'
            build_legacy(path)

            def fail():
                raise RuntimeError('before commit')

            with Catalog(path) as catalog:
                with self.assertRaises(RuntimeError):
                    with catalog.atomic(before_commit=fail):
                        catalog.save_model('retrying', retry_model('0'))
                        catalog.create_task('next', {'r': ('retrying', None, 1)})
            with Catalog(path) as catalog:
                self.assertEqual(catalog.task('current')[0][2], 2)
                with self.assertRaises(KeyError):
                    catalog.task('next')
                self.assertEqual(catalog.save_model('retrying', retry_model('0')), 3)
                catalog.create_task('next', {'r': ('retrying', None, 1)})
                self.assertEqual(Classifier(catalog).classify('next', ()).visits,
                                 {'r': {'collector': F(1)}})


if __name__ == '__main__':
    unittest.main()
