"""Record classification and original-state audit consumer.

v1 catalogs execute stored legacy payloads through SDK1. Upgraded (v2)
catalogs execute only the stored explicit compiled models through SDK2 and
restore original-state visits from the saved per-edge statistics; the silent
system is never re-solved at classify time.
"""
from dataclasses import dataclass
from fractions import Fraction
import sdk1
import sdk2
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
            upgraded = self.catalog.schema_version == '2'
            evidence, visits, weighted, bindings = {}, {}, {}, {}
            for category, model_id, revision, prior in choices:
                if upgraded:
                    execution_evidence, execution_visits = self._execute_compiled(
                        model_id, revision, symbols)
                else:
                    model = sdk1.load(self.catalog.get_model(model_id, revision))
                    execution = sdk1.evaluate(model, symbols)
                    execution_evidence, execution_visits = execution.evidence, execution.visits
                evidence[category] = execution_evidence
                visits[category] = execution_visits
                weighted[category] = prior * execution_evidence
                bindings[category] = (model_id, revision)
            total = sum(weighted.values(), Fraction(0))
            if not total:
                raise ZeroEvidence('task has zero prior-weighted evidence')
            result = Classification(evidence, {c: v / total for c, v in weighted.items()}, visits)
            self.catalog.record(task_id, symbols, bindings, result)
        return result

    def _execute_compiled(self, model_id, revision, symbols):
        """Run the stored explicit plan through SDK2; restore original visits."""
        plan = self.catalog.compiled_plan(model_id, revision)
        execution = sdk2.evaluate(sdk2.load(plan['compiled']), symbols)
        counts = execution.edge_counts
        if counts is None:
            return execution.evidence, None
        if not symbols:
            return execution.evidence, dict(plan['empty_visits'])
        totals = {state: Fraction(0) for state in plan['states']}
        for edge_id, count in counts.items():
            if not count:
                continue
            for state, value in plan['edge_visits'].get(edge_id, {}).items():
                totals[state] += count * value
        return execution.evidence, totals
