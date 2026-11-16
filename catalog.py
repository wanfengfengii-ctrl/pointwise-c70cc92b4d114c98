"""SQLite v1/v2 model revisions, fixed tasks and durable classification audit."""
from contextlib import contextmanager
from fractions import Fraction
import json
import sqlite3
import sdk1
import migration
from exact import dump_model, load_model, name, rational


SCHEMA = (
    'CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL)',
    "INSERT INTO meta VALUES ('schema_version','1')",
    'CREATE TABLE model_revisions(model_id TEXT NOT NULL, revision INTEGER NOT NULL CHECK(revision>0), body TEXT NOT NULL, PRIMARY KEY(model_id,revision))',
    'CREATE TABLE model_heads(model_id TEXT PRIMARY KEY, revision INTEGER NOT NULL, FOREIGN KEY(model_id,revision) REFERENCES model_revisions(model_id,revision))',
    'CREATE TABLE tasks(task_id TEXT PRIMARY KEY)',
    'CREATE TABLE task_classes(task_id TEXT NOT NULL REFERENCES tasks(task_id), ordinal INTEGER NOT NULL, category TEXT NOT NULL, model_id TEXT NOT NULL, revision INTEGER NOT NULL, prior TEXT NOT NULL, PRIMARY KEY(task_id,category), UNIQUE(task_id,ordinal), FOREIGN KEY(model_id,revision) REFERENCES model_revisions(model_id,revision))',
    'CREATE TABLE classification_runs(run_id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL REFERENCES tasks(task_id), symbols TEXT NOT NULL, bindings TEXT NOT NULL, result TEXT NOT NULL)',
)


def result_wire(result):
    return {
        'evidence': {k: str(v) for k, v in result.evidence.items()},
        'probabilities': {k: str(v) for k, v in result.probabilities.items()},
        'visits': {k: None if v is None else {s: str(x) for s, x in v.items()}
                   for k, v in result.visits.items()},
    }


def result_values(wire):
    return {
        'evidence': {k: Fraction(v) for k, v in wire['evidence'].items()},
        'probabilities': {k: Fraction(v) for k, v in wire['probabilities'].items()},
        'visits': {k: None if v is None else {s: Fraction(x) for s, x in v.items()}
                   for k, v in wire['visits'].items()},
    }


class Catalog:
    def __init__(self, path=':memory:'):
        self.db = sqlite3.connect(path, isolation_level=None)
        self.db.execute('PRAGMA foreign_keys=ON')
        self._serial = 0
        try:
            exists = self.db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='meta'"
            ).fetchone()
            if not exists:
                with self.atomic():
                    for statement in SCHEMA:
                        self.db.execute(statement)
            version = self.db.execute(
                "SELECT value FROM meta WHERE key='schema_version'"
            ).fetchone()
            if version not in (('1',), ('2',)):
                raise ValueError('unsupported catalog schema')
        except BaseException:
            self.db.close()
            raise

    def close(self):
        self.db.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    @property
    def schema_version(self):
        return self.db.execute(
            "SELECT value FROM meta WHERE key='schema_version'").fetchone()[0]

    def compiled_plan(self, model_id, revision):
        """Stored SDK2 execution plan for a fixed revision (v2 catalogs only)."""
        return migration.load_plan(self.db, model_id, revision)

    @contextmanager
    def atomic(self, before_commit=None):
        """Nested savepoints; callback executes immediately before outer commit/release."""
        outer = not self.db.in_transaction
        self._serial += 1
        savepoint = 'catalog_' + str(self._serial)
        self.db.execute('BEGIN IMMEDIATE' if outer else 'SAVEPOINT ' + savepoint)
        try:
            yield self.db
            if before_commit is not None:
                before_commit()
            self.db.execute('COMMIT' if outer else 'RELEASE ' + savepoint)
        except BaseException:
            if outer:
                if self.db.in_transaction:
                    self.db.execute('ROLLBACK')
            else:
                self.db.execute('ROLLBACK TO ' + savepoint)
                self.db.execute('RELEASE ' + savepoint)
            raise

    def save_model(self, model_id, payload):
        model_id = name(model_id)
        if self.schema_version == '2':
            body = json.dumps(
                dump_model(load_model(payload, silent=True, state_limit=12)),
                ensure_ascii=False, sort_keys=True)
            plan = migration.compile_model(json.loads(body))
            with self.atomic():
                row = self.db.execute(
                    'SELECT revision FROM v2_model_heads WHERE model_id=?', (model_id,)
                ).fetchone()
                revision = 1 if row is None else row[0] + 1
                migration.store_plan(self.db, model_id, revision, body, plan)
                self.db.execute(
                    'INSERT INTO v2_model_heads VALUES (?,?) ON CONFLICT(model_id) DO UPDATE SET revision=excluded.revision',
                    (model_id, revision))
            return revision
        body = json.dumps(dump_model(sdk1.load(payload)), ensure_ascii=False, sort_keys=True)
        with self.atomic():
            row = self.db.execute(
                'SELECT revision FROM model_heads WHERE model_id=?', (model_id,)
            ).fetchone()
            revision = 1 if row is None else row[0] + 1
            self.db.execute('INSERT INTO model_revisions VALUES (?,?,?)',
                            (model_id, revision, body))
            self.db.execute(
                'INSERT INTO model_heads VALUES (?,?) ON CONFLICT(model_id) DO UPDATE SET revision=excluded.revision',
                (model_id, revision))
        return revision

    def get_model(self, model_id, revision=None):
        name(model_id)
        v2 = self.schema_version == '2'
        heads = 'v2_model_heads' if v2 else 'model_heads'
        revisions = 'v2_model_revisions' if v2 else 'model_revisions'
        if revision is None:
            row = self.db.execute('SELECT revision FROM ' + heads + ' WHERE model_id=?',
                                  (model_id,)).fetchone()
            if row is None:
                raise KeyError(model_id)
            revision = row[0]
        if isinstance(revision, bool) or not isinstance(revision, int) or revision < 1:
            raise ValueError('revision must be a positive integer')
        row = self.db.execute(
            'SELECT body FROM ' + revisions + ' WHERE model_id=? AND revision=?',
            (model_id, revision)).fetchone()
        if row is None:
            raise KeyError((model_id, revision))
        return json.loads(row[0])

    def create_task(self, task_id, choices):
        """choices maps category to (model_id, revision-or-None, nonnegative prior)."""
        task_id = name(task_id)
        choices = list(choices.items())
        if not choices:
            raise ValueError('task needs categories')
        v2 = self.schema_version == '2'
        tasks = 'v2_tasks' if v2 else 'tasks'
        classes = 'v2_task_classes' if v2 else 'task_classes'
        heads = 'v2_model_heads' if v2 else 'model_heads'
        with self.atomic():
            if self.db.execute('SELECT 1 FROM ' + tasks + ' WHERE task_id=?', (task_id,)).fetchone():
                raise ValueError('task already exists')
            rows, alphabet = [], None
            for ordinal, (category, choice) in enumerate(choices):
                category = name(category)
                model_id, revision, prior = choice
                name(model_id)
                prior = rational(prior)
                if prior < 0:
                    raise ValueError('negative prior')
                if revision is None:
                    head = self.db.execute('SELECT revision FROM ' + heads + ' WHERE model_id=?',
                                           (model_id,)).fetchone()
                    if head is None:
                        raise KeyError(model_id)
                    revision = head[0]
                if v2:
                    current_alphabet = frozenset(self.get_model(model_id, revision)['alphabet'])
                else:
                    model = sdk1.load(self.get_model(model_id, revision))
                    current_alphabet = frozenset(model.alphabet)
                if alphabet is not None and alphabet != current_alphabet:
                    raise ValueError('task model alphabets differ')
                alphabet = current_alphabet
                rows.append((task_id, ordinal, category, model_id, revision, str(prior)))
            if not any(Fraction(row[-1]) for row in rows):
                raise ValueError('all priors are zero')
            self.db.execute('INSERT INTO ' + tasks + ' VALUES (?)', (task_id,))
            self.db.executemany('INSERT INTO ' + classes + ' VALUES (?,?,?,?,?,?)', rows)

    def task(self, task_id):
        v2 = self.schema_version == '2'
        tasks = 'v2_tasks' if v2 else 'tasks'
        classes = 'v2_task_classes' if v2 else 'task_classes'
        if not self.db.execute('SELECT 1 FROM ' + tasks + ' WHERE task_id=?', (task_id,)).fetchone():
            raise KeyError(task_id)
        return tuple((category, model_id, revision, Fraction(prior))
                     for category, model_id, revision, prior in self.db.execute(
                         'SELECT category,model_id,revision,prior FROM ' + classes + ' WHERE task_id=? ORDER BY ordinal',
                         (task_id,)))

    def record(self, task_id, symbols, bindings, result):
        runs = 'v2_classification_runs' if self.schema_version == '2' else 'classification_runs'
        with self.atomic():
            cursor = self.db.execute(
                'INSERT INTO ' + runs + '(task_id,symbols,bindings,result) VALUES (?,?,?,?)',
                (task_id, json.dumps(list(symbols), ensure_ascii=False),
                 json.dumps(bindings, ensure_ascii=False),
                 json.dumps(result_wire(result), ensure_ascii=False, sort_keys=True)))
            return cursor.lastrowid

    def audits(self, task_id):
        self.task(task_id)
        runs = 'v2_classification_runs' if self.schema_version == '2' else 'classification_runs'
        return tuple({
            'run_id': run_id, 'symbols': tuple(json.loads(symbols)),
            'bindings': {k: tuple(v) for k, v in json.loads(bindings).items()},
            'result': result_values(json.loads(result)),
        } for run_id, symbols, bindings, result in self.db.execute(
            'SELECT run_id,symbols,bindings,result FROM ' + runs + ' WHERE task_id=? ORDER BY run_id',
            (task_id,)))
