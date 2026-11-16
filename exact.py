"""Shared wire validation and rational linear algebra; no migration logic."""
from dataclasses import dataclass
from fractions import Fraction


class ModelError(ValueError):
    pass


class UnknownSymbol(ValueError):
    pass


def rational(value):
    if isinstance(value, bool) or not isinstance(value, (str, int, Fraction)):
        raise ValueError('expected integer, rational string or Fraction')
    try:
        return Fraction(value)
    except (ValueError, ZeroDivisionError) as exc:
        raise ValueError('invalid rational') from exc


def name(value):
    if not isinstance(value, str) or not value:
        raise ValueError('names must be nonempty strings')
    return value


@dataclass(frozen=True)
class Edge:
    id: str
    src: int
    dst: int
    symbol: str | None
    weight: Fraction


@dataclass(frozen=True)
class Model:
    states: tuple
    alphabet: tuple
    initial: tuple
    final: tuple
    edges: tuple


def load_model(payload, *, silent, state_limit=None):
    try:
        if set(payload) != {'states', 'alphabet', 'initial', 'final', 'edges'}:
            raise ValueError('unexpected model fields')
        states = tuple(name(s) for s in payload['states'])
        alphabet = tuple(name(s) for s in payload['alphabet'])
        n = len(states)
        if not n or len(set(states)) != n or (state_limit and n > state_limit):
            raise ValueError('invalid state set')
        if len(alphabet) > 4 or len(set(alphabet)) != len(alphabet):
            raise ValueError('invalid alphabet')
        initial = tuple(rational(v) for v in payload['initial'])
        final = tuple(rational(v) for v in payload['final'])
        if len(initial) != n or len(final) != n or any(v < 0 for v in initial + final):
            raise ValueError('invalid endpoint weights')
        indices = {s: i for i, s in enumerate(states)}
        edges, ids, epsilon = [], set(), [Fraction(0) for _ in states]
        for raw in payload['edges']:
            if set(raw) != {'id', 'src', 'dst', 'symbol', 'weight'}:
                raise ValueError('unexpected edge fields')
            eid = name(raw['id'])
            if eid in ids:
                raise ValueError('duplicate edge id')
            ids.add(eid)
            src, dst = indices[raw['src']], indices[raw['dst']]
            symbol, weight = raw['symbol'], rational(raw['weight'])
            if weight < 0 or (symbol is None and not silent):
                raise ValueError('invalid edge')
            if symbol is not None and symbol not in alphabet:
                raise ValueError('symbol outside alphabet')
            if symbol is None:
                epsilon[src] += weight
            edges.append(Edge(eid, src, dst, symbol, weight))
        if silent and any(v > Fraction(1, 2) for v in epsilon):
            raise ValueError('silent outgoing weight exceeds 1/2')
        return Model(states, alphabet, initial, final, tuple(edges))
    except (KeyError, TypeError, ValueError) as exc:
        raise ModelError(str(exc)) from exc


def dump_model(model):
    return {
        'states': list(model.states), 'alphabet': list(model.alphabet),
        'initial': [str(v) for v in model.initial],
        'final': [str(v) for v in model.final],
        'edges': [
            {'id': e.id, 'src': model.states[e.src], 'dst': model.states[e.dst],
             'symbol': e.symbol, 'weight': str(e.weight)} for e in model.edges
        ],
    }


def symbols_for(model, symbols):
    if isinstance(symbols, (str, bytes)):
        raise UnknownSymbol('pass an iterable of symbols, not a string')
    result = tuple(symbols)
    if any(not isinstance(s, str) or s not in model.alphabet for s in result):
        raise UnknownSymbol('symbol outside alphabet')
    return result


def solve(matrix, rhs):
    """Exact square linear solve, with pivoting and no floating tolerance."""
    n = len(rhs)
    if len(matrix) != n or any(len(row) != n for row in matrix):
        raise ValueError('matrix shape')
    a = [[Fraction(v) for v in row] + [Fraction(rhs[i])]
         for i, row in enumerate(matrix)]
    for col in range(n):
        pivot = next((r for r in range(col, n) if a[r][col]), None)
        if pivot is None:
            raise ValueError('singular system')
        a[col], a[pivot] = a[pivot], a[col]
        scale = a[col][col]
        a[col] = [v / scale for v in a[col]]
        for row in range(n):
            if row != col:
                scale = a[row][col]
                a[row] = [x - scale * y for x, y in zip(a[row], a[col])]
    return tuple(row[-1] for row in a)
