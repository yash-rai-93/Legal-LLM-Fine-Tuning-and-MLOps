"""Known-answer and failure tests; synthetic contracts are fixtures only.

Execute in AWS CodeBuild using buildspecs/cpu-check.yml.
"""

from copy import deepcopy
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

from src.cpu.evaluate import evaluate_predictions
from src.cpu.hypotheses import HYPOTHESES
from src.cpu.prepare import prepare
from src.cpu.transform import semantic_sha256, transform_rows, validate_split_disjointness
from src.cpu.validate import validate_hypotheses


def fixture_rows(doc_id=1, text="The recipient shall protect all confidential information."):
    return [
        {
            "document_id": doc_id, "text": text, "hypothesis_id": hid,
            "hypothesis": hypothesis, "label": "notmentioned", "evidence_texts": [],
        }
        for hid, hypothesis in HYPOTHESES.items()
    ]


def fixture_document(doc_id=1):
    return transform_rows(fixture_rows(doc_id))[0][0]


class TransformTests(unittest.TestCase):
    def test_real_shape_normalizes_labels_and_preserves_contract(self):
        rows = fixture_rows()
        rows[0]["label"] = "entailment"
        rows[0]["evidence_texts"] = ["The recipient shall protect", "all confidential information."]
        docs, counts = transform_rows(rows)
        self.assertEqual(counts, {"input_rows": 17, "documents": 1})
        self.assertEqual(docs[0]["origin"], "contractnli")
        self.assertEqual(docs[0]["text"], rows[0]["text"])
        self.assertEqual(docs[0]["inference"][0]["source_clause"], ". ".join(rows[0]["evidence_texts"]))
        self.assertEqual(docs[0]["inference"][1]["label"], "not_mentioned")

    def test_reordering_rows_does_not_change_semantic_content(self):
        rows = fixture_rows(2) + fixture_rows(1, "A distinct contract.")
        first, _ = transform_rows(rows)
        second, _ = transform_rows(list(reversed(rows)))
        self.assertEqual(semantic_sha256(first), semantic_sha256(second))

    def test_rejects_missing_duplicate_unknown_or_corrupted_rows(self):
        cases = [[], fixture_rows()[:-1], fixture_rows() + [fixture_rows()[0]]]
        for field, value in (
            ("hypothesis_id", "nda-999"), ("label", "maybe"),
            ("text", None), ("document_id", None), ("evidence_texts", "not a list"),
            ("hypothesis", "A different classification task"),
        ):
            rows = fixture_rows()
            rows[0][field] = value
            cases.append(rows)
        for rows in cases:
            with self.subTest(rows=len(rows)), self.assertRaises(ValueError):
                transform_rows(rows)

    def test_conflicting_text_for_same_document_is_not_silently_merged(self):
        rows = fixture_rows()
        rows[1]["text"] = "Another contract under the same document ID."
        with self.assertRaisesRegex(ValueError, "Conflicting"):
            transform_rows(rows)

    def test_rejects_split_leakage_by_id_and_by_exact_text(self):
        train = transform_rows(fixture_rows())[0]
        for other in (train, transform_rows(fixture_rows(2))[0]):
            with self.assertRaisesRegex(ValueError, "Overlapping"):
                validate_split_disjointness({"train": train, "validation": other})

    def test_not_mentioned_cannot_have_nonempty_evidence(self):
        rows = fixture_rows()
        rows[0]["evidence_texts"] = ["Fabricated support"]
        with self.assertRaisesRegex(ValueError, "empty evidence"):
            transform_rows(rows)


class EvaluationTests(unittest.TestCase):
    provenance = {"kind": "fixture", "source": "synthetic test contract; no model executed"}

    def test_known_wrong_label_changes_accuracy_by_one_seventeenth(self):
        reference = fixture_document()
        prediction = deepcopy(reference)
        prediction["inference"][0].update(label="entailment", source_clause="Incorrect fixture evidence.")
        report = evaluate_predictions([reference], [prediction], self.provenance)
        self.assertAlmostEqual(report["label_accuracy_all_attempts"], 16 / 17)
        self.assertEqual(report["coverage"], 1)
        self.assertFalse(report["live_inference"])
        self.assertEqual(report["provenance"]["kind"], "fixture")

    def test_missing_and_malformed_predictions_are_counted(self):
        references = [fixture_document(i) for i in (1, 2, 3)]
        malformed = deepcopy(references[1])
        malformed["inference"] = []
        report = evaluate_predictions(references, [references[0], malformed], self.provenance)
        self.assertEqual(report["attempted_documents"], 3)
        self.assertEqual(report["scored_documents"], 1)
        self.assertEqual(report["invalid_documents"], 1)
        self.assertEqual(report["missing_documents"], 1)
        self.assertAlmostEqual(report["label_accuracy_all_attempts"], 1 / 3)
        self.assertEqual(report["label_accuracy_scored_only"], 1)

    def test_all_missing_outputs_do_not_report_perfect_accuracy(self):
        report = evaluate_predictions([fixture_document()], [], self.provenance)
        self.assertEqual(report["label_accuracy_all_attempts"], 0)
        self.assertIsNone(report["label_accuracy_scored_only"])
        self.assertIsNone(report["evidence_rouge3_fmeasure_scored_only"])

    def test_duplicate_ids_and_unrelated_documents_are_errors(self):
        doc = fixture_document()
        for predictions in ([doc, doc], [fixture_document(99)]):
            with self.assertRaises(ValueError):
                evaluate_predictions([doc], predictions, self.provenance)

    def test_provenance_is_required(self):
        doc = fixture_document()
        for provenance in ({}, {"kind": "model_run", "source": "unknown"}):
            with self.assertRaises(ValueError):
                evaluate_predictions([doc], [doc], provenance)

    def test_truncated_duplicate_and_invalid_label_outputs_are_rejected(self):
        hypotheses = fixture_document()["inference"]
        malformed = deepcopy(hypotheses)
        malformed[-1] = deepcopy(malformed[0])
        invalid_label = deepcopy(hypotheses)
        invalid_label[0]["label"] = "maybe"
        for items in (hypotheses[:-1], malformed, invalid_label, None):
            with self.assertRaises(ValueError):
                validate_hypotheses(items)

    def test_evidence_overlap_matches_a_known_three_word_quote(self):
        reference = fixture_document()
        reference["inference"][0].update(label="entailment", source_clause="strict confidential treatment")
        report = evaluate_predictions([reference], [deepcopy(reference)], self.provenance)
        self.assertEqual(report["evidence_rouge3_fmeasure_scored_only"], 1)


class CloudBatchTests(unittest.TestCase):
    def test_parquet_roundtrip_writes_completion_manifest_and_never_overwrites(self):
        import pyarrow as pa
        import pyarrow.parquet as pq

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for split, rows in (
                ("train", fixture_rows()),
                ("validation", fixture_rows(2, "A different validation contract.")),
            ):
                pq.write_table(pa.Table.from_pylist(rows), root / f"{split}.parquet")
            report = prepare(root, root / "out", "fixture-commit")
            self.assertEqual(report["splits"]["train"]["documents"], 1)
            self.assertFalse(report["live_inference"])
            manifest = json.loads((root / "out/manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["source_commit"], "fixture-commit")
            with self.assertRaisesRegex(ValueError, "empty"):
                prepare(root, root / "out", "fixture-commit")

    def test_legacy_helpers_have_correct_nda12_and_nda20_definitions(self):
        for index, path in enumerate((
            "src/utils_evaluate_model.py", "src/scripts/evaluate/utils_evaluate_model.py"
        )):
            spec = importlib.util.spec_from_file_location(f"legacy_metrics_{index}", path)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            self.assertTrue(module.ensure_id_matches_hypothesis([
                {"hypothesis_id": hid, "hypothesis": HYPOTHESES[hid]}
                for hid in ("nda-12", "nda-20")
            ]))


if __name__ == "__main__":
    unittest.main()
