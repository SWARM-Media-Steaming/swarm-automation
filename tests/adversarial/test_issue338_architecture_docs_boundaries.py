"""Issue #338: boundary and lifecycle invariants for architecture docs."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "issue_worker"))

import architecture_docs as docs  # noqa: E402

SHA = "c" * 40


def op(**over):
    entity = {"id": "svc", "section": "components", "kind": "component", "name": "Svc",
              "provenance": "observed", "confidence": 0.8,
              "evidence": [{"type": "path", "ref": "src/svc.py"}]}
    entity.update(over)
    return {"impact": "update", "reason": "r", "confidence": 0.8,
            "operations": [{"op": "upsert", "entity": entity}]}


class Boundaries(unittest.TestCase):
    def test_bad_confidences_rejected(self):
        for value in (float("nan"), -0.1, 1.01, "bogus", None):
            with self.assertRaises(docs.PatchError, msg=repr(value)):
                docs.validate_review(op(confidence=value))

    def test_ai_cannot_claim_human_or_unknown_top_level_fields(self):
        with self.assertRaises(docs.PatchError):
            docs.validate_review(op(provenance="human"))
        payload = op()
        payload["html"] = "<b>x</b>"
        with self.assertRaises(docs.PatchError):
            docs.validate_review(payload)

    def test_sensitive_evidence_paths_never_stored(self):
        review = docs.validate_review(op(evidence=[
            {"type": "path", "ref": "src/.ENV"}, {"type": "path", "ref": "a/../b"},
            {"type": "path", "ref": "src/svc.py"}]))
        refs = [e["ref"] for e in review["operations"][0]["entity"]["evidence"]]
        self.assertEqual(refs, ["src/svc.py"])

    def test_unmerged_review_never_advances_documented_through(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = docs.ArchitectureStore(Path(tmp), "o/r")
            store.record_review(issue_number=1, issue_title="t", commit=SHA,
                                review=docs.validate_review(op()), signals=["api"], merged=False)
            snap = store.load()
            self.assertEqual(snap["documentedThrough"], "")
            self.assertEqual(snap["entities"], {})
            self.assertEqual(store.view()["freshness"], "empty")

    def test_invalid_commit_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = docs.ArchitectureStore(Path(tmp), "o/r")
            with self.assertRaises(docs.PatchError):
                store.record_review(issue_number=1, issue_title="t", commit="nothex",
                                    review=docs.validate_review(op()), signals=[], merged=True)


if __name__ == "__main__":
    unittest.main()
