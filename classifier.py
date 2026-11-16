"""SDK1-backed record classification and original-state audit consumer."""
from dataclasses import dataclass
from fractions import Fraction
import sdk1
from exact import UnknownSymbol


class ZeroEvidence(ValueError):
    pass


@dataclass(frozen=True)
class Classification:
    evidence: dict
    probabilities: dict
    visits: dict


class Classifier:
    def __init__(self, catalog):
        self.catalog = catalog

    def classify(self, task_id, symbols):
        if isinstance(symbols, (str, bytes)):
            raise UnknownSymbol('pass an iterable of symbols, not a string')
        symbols = tuple(symbols)
        with self.catalog.atomic():
            choices = self.catalog.task(task_id)
            evidence, visits, weighted, bindings = {}, {}, {}, {}
            for category, model_id, revision, prior in choices:
                model = sdk1.load(self.catalog.get_model(model_id, revision))
                execution = sdk1.evaluate(model, symbols)
                evidence[category] = execution.evidence
                visits[category] = execution.visits
                weighted[category] = prior * execution.evidence
                bindings[category] = (model_id, revision)
            total = sum(weighted.values(), Fraction(0))
            if not total:
                raise ZeroEvidence('task has zero prior-weighted evidence')
            result = Classification(evidence, {c: v / total for c, v in weighted.items()}, visits)
            self.catalog.record(task_id, symbols, bindings, result)
        return result
