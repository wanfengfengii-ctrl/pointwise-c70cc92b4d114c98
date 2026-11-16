"""Offline SDK1: rational weighted paths, including convergent silent cycles."""
from dataclasses import dataclass
from fractions import Fraction
from exact import load_model, symbols_for, solve


@dataclass(frozen=True)
class Execution:
    evidence: Fraction
    visits: dict | None


def load(payload):
    return load_model(payload, silent=True, state_limit=12)


def evaluate(model, symbols):
    symbols = symbols_for(model, symbols)
    n, length = len(model.states), len(symbols)
    system = [[Fraction(i == j) for j in range(n)] for i in range(n)]
    for edge in model.edges:
        if edge.symbol is None:
            system[edge.src][edge.dst] -= edge.weight
    transpose = list(map(list, zip(*system)))
    # Backward suffixes include all silent moves at this input position.
    backward = [None] * (length + 1)
    backward[length] = solve(system, model.final)
    for pos in range(length - 1, -1, -1):
        rhs = [Fraction(0) for _ in range(n)]
        for edge in model.edges:
            if edge.symbol == symbols[pos]:
                rhs[edge.src] += edge.weight * backward[pos + 1][edge.dst]
        backward[pos] = solve(system, rhs)
    evidence = sum((a * b for a, b in zip(model.initial, backward[0])), Fraction(0))
    if not evidence:
        return Execution(evidence, None)
    arrival = model.initial
    numerators = [Fraction(0) for _ in range(n)]
    for pos in range(length + 1):
        forward = solve(transpose, arrival)
        for state in range(n):
            numerators[state] += forward[state] * backward[pos][state]
        if pos < length:
            arrival = [Fraction(0) for _ in range(n)]
            for edge in model.edges:
                if edge.symbol == symbols[pos]:
                    arrival[edge.dst] += forward[edge.src] * edge.weight
    return Execution(evidence, dict(zip(model.states, (v / evidence for v in numerators))))
