"""SDK1-to-SDK2 catalog migration: v2 schema, silent elimination, plan IO.

Each legacy revision is compiled once (at upgrade or save_model time) into an
explicit SDK2-loadable model plus exact derived statistics that attribute
original-state visits to compiled-edge traversals. Classification then runs
only sdk2.load/sdk2.evaluate and combines edge expectations with the stored
statistics; the silent system is never re-solved at classify time. See
MIGRATION.md for the representation and the invariants.
"""
from fractions import Fraction
import json

from exact import load_model, solve


V2_SCHEMA = (
    'CREATE TABLE v2_model_revisions(model_id TEXT NOT NULL, revision INTEGER NOT NULL CHECK(revision>0), body TEXT NOT NULL, states TEXT NOT NULL, compiled TEXT NOT NULL, empty_visits TEXT, PRIMARY KEY(model_id,revision))',
    'CREATE TABLE v2_model_heads(model_id TEXT PRIMARY KEY, revision INTEGER NOT NULL, FOREIGN KEY(model_id,revision) REFERENCES v2_model_revisions(model_id,revision))',
    'CREATE TABLE v2_edge_visits(model_id TEXT NOT NULL, revision INTEGER NOT NULL, edge_id TEXT NOT NULL, state TEXT NOT NULL, value TEXT NOT NULL, PRIMARY KEY(model_id,revision,edge_id,state), FOREIGN KEY(model_id,revision) REFERENCES v2_model_revisions(model_id,revision))',
    'CREATE TABLE v2_tasks(task_id TEXT PRIMARY KEY)',
    'CREATE TABLE v2_task_classes(task_id TEXT NOT NULL REFERENCES v2_tasks(task_id), ordinal INTEGER NOT NULL, category TEXT NOT NULL, model_id TEXT NOT NULL, revision INTEGER NOT NULL, prior TEXT NOT NULL, PRIMARY KEY(task_id,category), UNIQUE(task_id,ordinal), FOREIGN KEY(model_id,revision) REFERENCES v2_model_revisions(model_id,revision))',
    'CREATE TABLE v2_classification_runs(run_id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL REFERENCES v2_tasks(task_id), symbols TEXT NOT NULL, bindings TEXT NOT NULL, result TEXT NOT NULL)',
)

# Internal derived-state names live in their own namespace and are never
# confused with business original-state names (see MIGRATION.md).
ENTRY = 'q#entry'


def _matrices(model):
    """Silent matrix S and per-symbol explicit matrices A_a (parallel edges summed)."""
    n = len(model.states)
    silent = [[Fraction(0) for _ in range(n)] for _ in range(n)]
    explicit = {symbol: [[Fraction(0) for _ in range(n)] for _ in range(n)]
                for symbol in model.alphabet}
    for edge in model.edges:
        if edge.symbol is None:
            silent[edge.src][edge.dst] += edge.weight
        else:
            explicit[edge.symbol][edge.src][edge.dst] += edge.weight
    return silent, explicit


def _closure(silent):
    """B = (I - S)^-1 exactly; silent row sums <= 1/2 keep it nonsingular."""
    n = len(silent)
    system = [[Fraction(i == j) - silent[i][j] for j in range(n)] for i in range(n)]
    columns = [solve(system, [Fraction(i == j) for i in range(n)]) for j in range(n)]
    return [[columns[j][i] for j in range(n)] for i in range(n)]


def _mmul(a, b):
    n = len(a)
    return [[sum((a[i][k] * b[k][j] for k in range(n)), Fraction(0))
             for j in range(n)] for i in range(n)]


def _vmul(vector, matrix):
    n = len(matrix)
    return [sum((vector[k] * matrix[k][j] for k in range(n)), Fraction(0))
            for j in range(n)]


def _mdotv(matrix, vector):
    return [sum((row[k] * vector[k] for k in range(len(vector))), Fraction(0))
            for row in matrix]


def compile_model(payload):
    """Compile one legacy (possibly silent) model into its v2 execution plan.

    Returns a dict with the original state names, an SDK2-loadable explicit
    payload, empty-input visits (None when empty evidence is zero) and, for
    every compiled edge id, the exact expected entries into each original
    state per single traversal of that edge.
    """
    model = load_model(payload, silent=True, state_limit=12)
    states = list(model.states)
    n = len(states)
    silent, explicit = _matrices(model)
    closure = _closure(silent)
    alpha = list(model.initial)
    final = list(model.final)
    alpha_b = _vmul(alpha, closure)
    b_final = _mdotv(closure, final)
    empty_evidence = sum((a * f for a, f in zip(alpha, b_final)), Fraction(0))
    empty_visits = None
    if empty_evidence:
        empty_visits = {state: alpha_b[i] * b_final[i] / empty_evidence
                        for i, state in enumerate(states)}
    compiled = {
        'states': [ENTRY] + ['q#' + str(i) for i in range(n)],
        'alphabet': list(model.alphabet),
        'initial': ['1'] + ['0'] * n,
        'final': [str(empty_evidence)] + [str(v) for v in final],
        'edges': [],
    }
    edge_visits = {}
    for symbol in model.alphabet:
        a_b = _mmul(explicit[symbol], closure)
        b_a_b = _mmul(closure, a_b)
        alpha_a_b = _vmul(alpha_b, a_b)
        for j in range(n):
            weight = alpha_a_b[j]
            if not weight:
                continue
            edge_id = json.dumps(['e', symbol, 'entry', j], ensure_ascii=False)
            visits = {}
            for i, state in enumerate(states):
                value = (alpha_b[i] * b_a_b[i][j] + alpha_a_b[i] * closure[i][j]) / weight
                if value:
                    visits[state] = value
            compiled['edges'].append({
                'id': edge_id, 'src': ENTRY, 'dst': 'q#' + str(j),
                'symbol': symbol, 'weight': str(weight)})
            edge_visits[edge_id] = visits
        for i in range(n):
            for j in range(n):
                weight = a_b[i][j]
                if not weight:
                    continue
                edge_id = json.dumps(['e', symbol, i, j], ensure_ascii=False)
                visits = {}
                for k, state in enumerate(states):
                    value = a_b[i][k] * closure[k][j] / weight
                    if value:
                        visits[state] = value
                compiled['edges'].append({
                    'id': edge_id, 'src': 'q#' + str(i), 'dst': 'q#' + str(j),
                    'symbol': symbol, 'weight': str(weight)})
                edge_visits[edge_id] = visits
    return {'states': states, 'compiled': compiled,
            'empty_visits': empty_visits, 'edge_visits': edge_visits}


def store_plan(db, model_id, revision, body, plan):
    """Persist one revision row and its per-edge visit statistics, atomically
    with the caller's surrounding transaction."""
    empty = None if plan['empty_visits'] is None else json.dumps(
        {state: str(value) for state, value in plan['empty_visits'].items()},
        ensure_ascii=False, sort_keys=True)
    db.execute('INSERT INTO v2_model_revisions VALUES (?,?,?,?,?,?)',
               (model_id, revision, body,
                json.dumps(plan['states'], ensure_ascii=False),
                json.dumps(plan['compiled'], ensure_ascii=False, sort_keys=True),
                empty))
    db.executemany(
        'INSERT INTO v2_edge_visits VALUES (?,?,?,?,?)',
        [(model_id, revision, edge_id, state, str(value))
         for edge_id, visits in plan['edge_visits'].items()
         for state, value in visits.items()])


def load_plan(db, model_id, revision):
    """Read back a stored plan as independent values (no shared mutables)."""
    row = db.execute(
        'SELECT states,compiled,empty_visits FROM v2_model_revisions '
        'WHERE model_id=? AND revision=?', (model_id, revision)).fetchone()
    if row is None:
        raise KeyError((model_id, revision))
    empty_visits = None if row[2] is None else {
        state: Fraction(value) for state, value in json.loads(row[2]).items()}
    edge_visits = {}
    for edge_id, state, value in db.execute(
            'SELECT edge_id,state,value FROM v2_edge_visits '
            'WHERE model_id=? AND revision=?', (model_id, revision)):
        edge_visits.setdefault(edge_id, {})[state] = Fraction(value)
    return {'states': json.loads(row[0]), 'compiled': json.loads(row[1]),
            'empty_visits': empty_visits, 'edge_visits': edge_visits}


def upgrade(catalog, before_commit=None):
    """Atomically switch an open v1 catalog to the v2 SDK2-backed schema.

    No-op when the catalog is already v2 (the callback is not invoked). Any
    failure, including one raised by before_commit, rolls back the target
    DDL, data and version flag, leaving the legacy catalog fully usable.
    """
    if catalog.schema_version == '2':
        return None
    with catalog.atomic(before_commit=before_commit):
        db = catalog.db
        for statement in V2_SCHEMA:
            db.execute(statement)
        revisions = db.execute(
            'SELECT model_id,revision,body FROM model_revisions '
            'ORDER BY model_id,revision').fetchall()
        for model_id, revision, body in revisions:
            store_plan(db, model_id, revision, body, compile_model(json.loads(body)))
        db.execute('INSERT INTO v2_model_heads SELECT model_id,revision FROM model_heads')
        db.execute('INSERT INTO v2_tasks SELECT task_id FROM tasks')
        db.execute('INSERT INTO v2_task_classes '
                   'SELECT task_id,ordinal,category,model_id,revision,prior FROM task_classes')
        db.execute('INSERT INTO v2_classification_runs(run_id,task_id,symbols,bindings,result) '
                   'SELECT run_id,task_id,symbols,bindings,result FROM classification_runs')
        db.execute("UPDATE meta SET value='2' WHERE key='schema_version'")
    return None
