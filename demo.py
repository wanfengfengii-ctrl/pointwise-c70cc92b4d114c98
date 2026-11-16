"""Real legacy database, fixed-revision classification, reopen, and SDK2 port."""
from fractions import Fraction
import json
from pathlib import Path
import tempfile

from catalog import Catalog, result_wire
from classifier import Classifier
from samples import build_legacy, explicit_model, retry_model
import sdk2


def main():
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / 'records.sqlite'
        build_legacy(path)
        with Catalog(path) as catalog:
            classifier = Classifier(catalog)
            frozen = classifier.classify('frozen', ['x'])
            catalog.save_model('retrying', retry_model('0'))
            catalog.create_task('continued', {
                'retry': ('retrying', None, 1), 'plain': ('plain', None, 2),
            })
            continued = classifier.classify('continued', ['x'])
            assert frozen.visits['retry']['collector'] == Fraction(4)
            assert continued.visits['retry']['collector'] == Fraction(2)
        with Catalog(path) as catalog:
            assert Classifier(catalog).classify('frozen', ['x']) == frozen
            audit = catalog.audits('frozen')[-1]
            print(json.dumps({
                'frozen': result_wire(frozen), 'continued': result_wire(continued),
                'reopened_bindings': audit['bindings'],
                'durable_frozen_runs': len(catalog.audits('frozen')),
            }, ensure_ascii=False, indent=2))
        explicit = sdk2.evaluate(sdk2.load(explicit_model()), ['x', 'x'])
        assert explicit.evidence == Fraction(1)
        assert sum(explicit.edge_counts.values()) == Fraction(2)
        print(json.dumps({
            'sdk2_evidence': str(explicit.evidence),
            'sdk2_edge_counts': {k: str(v) for k, v in explicit.edge_counts.items()},
        }, indent=2))


if __name__ == '__main__':
    main()
