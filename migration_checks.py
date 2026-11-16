"""Public migration checks; legacy mode runs without a migration implementation."""
import importlib
import importlib.util
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch
from fractions import Fraction as F

from catalog import Catalog, result_wire
from classifier import Classification, Classifier, ZeroEvidence
from exact import UnknownSymbol
from samples import build_legacy, plain_model, populate, retry_model
import sdk1

ROOT = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("original_business", ROOT / "tests" / "test_business.py")
business = importlib.util.module_from_spec(spec)
spec.loader.exec_module(business)
OLD_TABLES = ("model_revisions", "model_heads", "tasks", "task_classes", "classification_runs")


def upgrade(catalog, before_commit=None):
    return importlib.import_module("migration").upgrade(catalog, before_commit=before_commit)


def old_rows(catalog):
    return {table: tuple(catalog.db.execute("SELECT * FROM " + table + " ORDER BY rowid"))
            for table in OLD_TABLES}


def version(catalog):
    return catalog.db.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0]


def source_forbidden(action, table, column, database, source):
    if action == sqlite3.SQLITE_READ and table in OLD_TABLES:
        return sqlite3.SQLITE_DENY
    return sqlite3.SQLITE_OK


def expected(catalog, task_id, symbols):
    evidence, visits, weighted = {}, {}, {}
    for category, model_id, revision, prior in catalog.task(task_id):
        evidence[category], visits[category] = business.reference(
            catalog.get_model(model_id, revision), symbols)
        weighted[category] = prior * evidence[category]
    total = sum(weighted.values(), F(0))
    if not total:
        return None
    return Classification(evidence, {k: v / total for k, v in weighted.items()}, visits)


def prepare(catalog):
    populate(catalog)
    catalog.create_task("cycle-check", {"cycle": ("cycle", 1, 1)})
    dead = plain_model()
    dead["final"] = ["0"]
    catalog.save_model("dead", dead)
    catalog.create_task("mixed", {
        "dead": ("dead", 1, 1), "live": ("plain", 1, 1),
        "zero-prior": ("retrying", 1, 0),
    })
    return catalog


class MigrationTests(unittest.TestCase):
    def test_joint_conversion_and_consumer(self):
        with prepare(Catalog()) as catalog:
            classifier = Classifier(catalog)
            cases = [
                ("frozen", ()), ("frozen", ("x",)), ("current", ("x",)),
                ("cycle-check", ()), ("cycle-check", ("x", "y")),
                ("cycle-check", ("y",)), ("mixed", ()),
            ]
            answers = [(task, symbols, expected(catalog, task, symbols))
                       for task, symbols in cases]
            models = {(mid, rev): catalog.get_model(mid, rev)
                      for mid, rev in catalog.db.execute(
                          "SELECT model_id,revision FROM model_revisions")}
            tasks = {task: catalog.task(task) for task in
                     ("frozen", "current", "cycle-check", "mixed")}
            audits = {task: catalog.audits(task) for task in tasks}
            source = old_rows(catalog)
            calls = []
            self.assertIsNone(upgrade(catalog, lambda: calls.append("commit")))
            self.assertEqual(calls, ["commit"])
            self.assertEqual(version(catalog), "2")
            self.assertEqual(old_rows(catalog), source)
            published = tuple(catalog.db.iterdump())
            self.assertIsNone(upgrade(catalog, lambda: self.fail("repeat called callback")))
            self.assertEqual(tuple(catalog.db.iterdump()), published)
            catalog.db.set_authorizer(source_forbidden)
            try:
                with patch.object(sdk1, "load", side_effect=AssertionError("SDK1 load")), \
                     patch.object(sdk1, "evaluate", side_effect=AssertionError("SDK1 evaluate")):
                    for key, payload in models.items():
                        self.assertEqual(catalog.get_model(*key), payload)
                    for task, bindings in tasks.items():
                        self.assertEqual(catalog.task(task), bindings)
                        self.assertEqual(catalog.audits(task), audits[task])
                    for task, symbols, answer in answers:
                        before = len(catalog.audits(task))
                        if answer is None:
                            with self.assertRaises(ZeroEvidence):
                                classifier.classify(task, symbols)
                            self.assertEqual(len(catalog.audits(task)), before)
                        else:
                            actual = classifier.classify(task, (s for s in symbols))
                            self.assertEqual(actual, answer)
                            self.assertEqual(catalog.audits(task)[-1]["result"], {
                                "evidence": answer.evidence,
                                "probabilities": answer.probabilities,
                                "visits": answer.visits,
                            })
                    count = len(catalog.audits("frozen"))
                    with self.assertRaises(UnknownSymbol):
                        classifier.classify("frozen", ["unknown"])
                    self.assertEqual(len(catalog.audits("frozen")), count)
                    self.assertEqual(catalog.save_model("retrying", retry_model("0")), 3)
                    catalog.create_task("continued", {
                        "retry": ("retrying", None, 1), "plain": ("plain", None, 2)})
                    continued = classifier.classify("continued", ["x"])
                    self.assertEqual(continued.probabilities, {"retry": F(1, 3), "plain": F(2, 3)})
                    self.assertEqual(continued.visits, {
                        "retry": {"collector": F(2)}, "plain": {"collector": F(2)}})
                    for task, symbols, answer in answers[:3]:
                        self.assertEqual(classifier.classify(task, symbols), answer)
                    for task, previous in audits.items():
                        self.assertEqual(catalog.audits(task)[:len(previous)], previous)
            finally:
                catalog.db.set_authorizer(None)
            self.assertEqual(old_rows(catalog), source)

    def test_file_failure_retry_and_reopen(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "catalog.sqlite"
            build_legacy(path)
            with Catalog(path) as catalog:
                before = tuple(catalog.db.iterdump())
                def fail():
                    raise RuntimeError("migration commit barrier")
                with self.assertRaisesRegex(RuntimeError, "migration commit barrier"):
                    upgrade(catalog, fail)
                self.assertEqual(tuple(catalog.db.iterdump()), before)
                self.assertEqual(version(catalog), "1")
            with Catalog(path) as catalog:
                old = Classifier(catalog).classify("frozen", ["x"])
                audits = catalog.audits("frozen")
                upgrade(catalog)
            with Catalog(path) as catalog:
                self.assertEqual(version(catalog), "2")
                catalog.db.set_authorizer(source_forbidden)
                with patch.object(sdk1, "load", side_effect=AssertionError("SDK1 load")), \
                     patch.object(sdk1, "evaluate", side_effect=AssertionError("SDK1 evaluate")):
                    self.assertEqual(catalog.audits("frozen"), audits)
                    self.assertEqual(Classifier(catalog).classify("frozen", ["x"]), old)
                    payload = catalog.get_model("retrying", 1)
                    payload["edges"].clear()
                    self.assertEqual(len(catalog.get_model("retrying", 1)["edges"]), 2)


def demo():
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "demo.sqlite"
        build_legacy(path)
        with Catalog(path) as catalog:
            classifier = Classifier(catalog)
            before = classifier.classify("frozen", ["x"])
            def fail():
                raise RuntimeError("demonstrated rollback")
            try:
                upgrade(catalog, fail)
            except RuntimeError as exc:
                if str(exc) != "demonstrated rollback":
                    raise
                assert version(catalog) == "1"
            else:
                raise AssertionError("callback failure did not propagate")
            upgrade(catalog)
            with patch.object(sdk1, "load", side_effect=AssertionError("SDK1 load")), \
                 patch.object(sdk1, "evaluate", side_effect=AssertionError("SDK1 evaluate")):
                assert classifier.classify("frozen", ["x"]) == before
                assert catalog.save_model("retrying", retry_model("0")) == 3
                catalog.create_task("continued", {
                    "retry": ("retrying", None, 1), "plain": ("plain", None, 2)})
                after = classifier.classify("continued", ["x"])
        with Catalog(path) as catalog:
            catalog.db.set_authorizer(source_forbidden)
            with patch.object(sdk1, "load", side_effect=AssertionError("SDK1 load")), \
                 patch.object(sdk1, "evaluate", side_effect=AssertionError("SDK1 evaluate")):
                assert Classifier(catalog).classify("frozen", ["x"]) == before
                assert Classifier(catalog).classify("continued", ["x"]) == after
                print(json.dumps({
                    "schema": version(catalog), "frozen": result_wire(before),
                    "continued": result_wire(after),
                    "bindings": catalog.audits("frozen")[-1]["bindings"],
                    "reopened_frozen_runs": len(catalog.audits("frozen")),
                }, ensure_ascii=False, indent=2))


def suite(mode):
    loader = unittest.defaultTestLoader
    if mode == "legacy":
        return unittest.TestSuite(loader.loadTestsFromTestCase(getattr(business, name))
                                  for name in ("SDK1Tests", "SDK2Tests", "CatalogTests"))
    return unittest.TestSuite((loader.loadTestsFromModule(business),
                              loader.loadTestsFromTestCase(MigrationTests)))


def main():
    mode = sys.argv[1] if len(sys.argv) == 2 else ""
    if mode == "demo":
        demo()
    elif mode in ("legacy", "verify"):
        result = unittest.TextTestRunner(verbosity=2).run(suite(mode))
        if not result.wasSuccessful():
            raise SystemExit(1)
    else:
        raise SystemExit("usage: python3.11 -B migration_checks.py legacy|verify|demo")


if __name__ == "__main__":
    main()
