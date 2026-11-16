"""Offline SDK2: explicit-symbol paths and conditional edge multiplicities only."""
from dataclasses import dataclass
from fractions import Fraction
from exact import load_model, symbols_for


@dataclass(frozen=True)
class Execution:
    evidence: Fraction
    edge_counts: dict | None


def load(payload):
    return load_model(payload, silent=False)


def evaluate(model, symbols):
    if any(edge.symbol is None for edge in model.edges):
        raise ValueError('SDK2 cannot execute silent edges')
    symbols = symbols_for(model, symbols)
    n, length = len(model.states), len(symbols)
    forward = [model.initial]
    for symbol in symbols:
        row = [Fraction(0) for _ in range(n)]
        for edge in model.edges:
            if edge.symbol == symbol:
                row[edge.dst] += forward[-1][edge.src] * edge.weight
        forward.append(tuple(row))
    evidence = sum((a * b for a, b in zip(forward[-1], model.final)), Fraction(0))
    if not evidence:
        return Execution(evidence, None)
    counts = {edge.id: Fraction(0) for edge in model.edges}
    suffix = model.final
    for pos in range(length - 1, -1, -1):
        row = [Fraction(0) for _ in range(n)]
        for edge in model.edges:
            if edge.symbol == symbols[pos]:
                counts[edge.id] += forward[pos][edge.src] * edge.weight * suffix[edge.dst]
                row[edge.src] += edge.weight * suffix[edge.dst]
        suffix = tuple(row)
    return Execution(evidence, {eid: value / evidence for eid, value in counts.items()})
