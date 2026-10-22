"""Synthetic fixtures for historical replay; execute only in AWS CodeBuild."""

from copy import deepcopy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from src.cpu.catalog_publish import BUCKET
from src.cpu.historical_replay import (
    ASSISTANT_SUFFIX, USER_MARKER, build_text_index, checked_source, decode_prediction,
    evaluate_view, parse_log, prepare_replay, reference_documents,
    request_identity, sha256, summary_record,
)
from src.cpu.hypotheses import HYPOTHESES
from src.cpu.prompt import json_bytes
from src.cpu.replay_cloud import download_reference, run_replay
from src.cpu.transform import semantic_sha256, transform_rows

COMMIT = "a" * 40
BUILD = "legal-llm-cpu-check:00000000-0000-4000-8000-000000000002"
ENVIRON = {
    "AWS_DEFAULT_REGION": "us-east-1", "OUTPUT_BUCKET": BUCKET,
    "CODEBUILD_BUILD_ID": BUILD,
}


def reference_fixture(key):
    text = "strict confidential treatment\nSynthetic fixture contract " + str(key) + "."
    rows = [{
        "document_id": key, "text": text, "hypothesis_id": hid,
        "hypothesis": definition,
        "label": "entailment" if hid == "nda-1" else "not_mentioned",
        "evidence_texts": ["strict confidential treatment"] if hid == "nda-1" else [],
    } for hid, definition in HYPOTHESES.items()]
    return transform_rows(rows)[0][0]


def request_text(contract):
    return json.dumps({
        "inputs": "<|im_start|>system\nFixture prompt\n<|im_end|>\n" + USER_MARKER
                  + contract + ASSISTANT_SUFFIX,
        "parameters": {"temperature": 0.2, "top_p": 0.95, "max_new_tokens": 2000},
    })


def output_text(items):
    return json.dumps({"generated_text": json.dumps({"Hypotheses": items})})


def log_record(timestamp, doc, output, *, marker=True):
    result = "timestamp: " + str(timestamp) + "\ninput: " + request_text(doc["text"]) + "\n"
    if marker:
        result += "output: " + output + "\n"
    return result + "\n"


def source_entry(path, body):
    return {
        "path": path, "bytes": len(body),
        "git_blob_sha1": hashlib.sha1(
            ("blob " + str(len(body)) + "\0").encode("ascii") + body
        ).hexdigest(),
    }


def replay_fixture(root):
    docs = [reference_fixture(1), reference_fixture(2)]
    reference = "".join(
        json.dumps(doc, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
        for doc in docs
    ).encode("utf-8")
    correct = deepcopy(docs[0]["inference"])
    wrong = deepcopy(docs[1]["inference"])
    wrong[-1].update(label="contradiction", source_clause="Invented fixture quote")
    # A successful retry appears first in the file, but an earlier failed request
    # must remain the first-attempt result for document 1.
    log = (
        log_record(1700000001000, docs[0], output_text(correct))
        + log_record(1700000000000, docs[0], "")
        + log_record(1700000002000, docs[1], output_text(wrong))
    ).encode("utf-8")
    summary = b"Loading dataset from directory: prompts\n" + json_bytes({
        "totalRequests": 3, "failedRequests": 1, "tokenizer": "fixture-only",
    })
    folder = root / "benchmark"
    folder.mkdir()
    (folder / "fixture.log").write_bytes(log)
    (folder / "fixture-summary.json").write_bytes(summary)
    identity = {
        "bucket": BUCKET,
        "key": "runs/00000000-0000-4000-8000-000000000001/catalog/test.jsonl",
        "version_id": "accepted-test-version",
        "sha256": sha256(reference), "semantic_sha256": semantic_sha256(docs),
        "bytes": len(reference), "documents": 2, "hypothesis_rows": 34,
        "catalog_build_id": "fixture-build", "catalog_source_commit": "c" * 40,
    }
    config = {
        "schema_version": "contractnli-historical-replay-config-v1",
        "historical_repository": "https://github.com/example/fixture",
        "historical_source_commit": "b" * 40,
        "reference": identity,
        "groups": [{
            "name": "fixture", "requests": 3,
            "summary": "benchmark/fixture-summary.json",
            "files": [
                source_entry("benchmark/fixture.log", log),
                source_entry("benchmark/fixture-summary.json", summary),
            ],
        }],
        "limitations": ["Synthetic unit fixtures; no model run."],
    }
    return config, reference, docs


class ReplayParsingTests(unittest.TestCase):
    def test_source_bytes_are_pinned_and_traversal_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config, _reference, _docs = replay_fixture(root)
            entry = config["groups"][0]["files"][0]
            self.assertTrue(checked_source(root, entry))
            changed = {**entry, "git_blob_sha1": "0" * 40}
            with self.assertRaisesRegex(ValueError, "blob"):
                checked_source(root, changed)
            with self.assertRaisesRegex(ValueError, "path"):
                checked_source(root, {**entry, "path": "../elsewhere"})
            with self.assertRaisesRegex(ValueError, "size"):
                checked_source(root, {**entry, "bytes": entry["bytes"] + 1})

    def test_empty_and_absent_output_markers_preserve_request_accounting(self):
        doc = reference_fixture(1)
        raw = (log_record(1700000000000, doc, "")
               + log_record(1700000001000, doc, "", marker=False)).encode()
        records = parse_log(raw)
        self.assertEqual(len(records), 2)
        self.assertEqual(records[0]["output_json"], "")
        self.assertIsNone(records[1]["output_json"])

    def test_duplicate_unpaired_and_unrecognized_log_lines_fail(self):
        doc = reference_fixture(1)
        valid = log_record(1700000000000, doc, "")
        cases = (
            b"output: {}",
            b"timestamp: 1700000000000\noutput: {}",
            (valid + "input: {}\n").encode(),
            (valid + "output: {}\n").encode(),
            (valid + "unrecognized line\n").encode(),
            b"timestamp: invalid\ninput: {}",
        )
        for raw in cases:
            with self.subTest(size=len(raw)), self.assertRaises(ValueError):
                parse_log(raw)

    def test_contract_mapping_is_exact_and_rejects_ambiguous_template_boundaries(self):
        doc = reference_fixture(1)
        index = build_text_index({"1": doc})
        identity = request_identity(request_text(doc["text"]), index)
        self.assertEqual(identity["reference_document_id"], "1")
        for contract in (doc["text"] + " ", "Unknown contract"):
            with self.assertRaisesRegex(ValueError, "exact"):
                request_identity(request_text(contract), index)
        malformed = json.loads(request_text(doc["text"]))
        malformed["inputs"] += "trailing text"
        with self.assertRaisesRegex(ValueError, "boundary"):
            request_identity(json.dumps(malformed), index)

    def test_invalid_prediction_json_envelopes_and_schemas_are_not_repaired(self):
        valid = reference_fixture(1)["inference"]
        self.assertEqual(decode_prediction(output_text(valid))[1], "valid_output")
        for raw in (
            "{", json.dumps({"other": "data"}),
            json.dumps({"generated_text": json.dumps(valid)}),
            json.dumps({"generated_text": '{"Hypotheses": []}'}),
            output_text(valid[:-1]), output_text(valid + [valid[0]]),
        ):
            with self.subTest(raw=raw[:20]):
                self.assertTrue(decode_prediction(raw)[1].startswith("invalid"))
        for raw in ("", " ", None):
            self.assertEqual(decode_prediction(raw)[1], "missing_output")

    def test_summary_request_and_failure_counts_are_validated(self):
        prefix = b"Loading dataset from directory: prompts\n"
        body = prefix + json_bytes({"totalRequests": 3, "failedRequests": 1})
        self.assertEqual(summary_record(body, 3)["failedRequests"], 1)
        for bad in (
            prefix + json_bytes({"totalRequests": 4, "failedRequests": 1}),
            prefix + json_bytes({"totalRequests": 3, "failedRequests": True}),
            prefix + json_bytes({"totalRequests": 3, "failedRequests": 4}),
            b"unsupported prefix\n{}",
        ):
            with self.assertRaises(ValueError):
                summary_record(bad, 3)


class ReplayReferenceTests(unittest.TestCase):
    def test_reference_checksum_semantics_and_counts_are_all_required(self):
        with tempfile.TemporaryDirectory() as directory:
            config, reference, _docs = replay_fixture(Path(directory))
            identity = config["reference"]
            self.assertEqual(len(reference_documents(reference, identity)), 2)
            for key, value in (
                ("sha256", "0" * 64), ("semantic_sha256", "0" * 64),
                ("documents", 3), ("hypothesis_rows", 17),
            ):
                with self.assertRaises(ValueError):
                    reference_documents(reference, {**identity, key: value})

    def test_reference_hypothesis_and_text_identity_cannot_be_substituted(self):
        docs = [reference_fixture(1)]
        docs[0]["inference"][0]["hypothesis"] = "A changed task."
        body = (json.dumps(docs[0], ensure_ascii=False) + "\n").encode("utf-8")
        identity = {
            "bytes": len(body), "sha256": sha256(body), "documents": 1,
            "hypothesis_rows": 17, "semantic_sha256": semantic_sha256(docs),
        }
        with self.assertRaisesRegex(ValueError, "wording"):
            reference_documents(body, identity)


def replace_reference_fixture(config, docs):
    result = deepcopy(config)
    body = "".join(
        json.dumps(doc, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
        for doc in docs
    ).encode("utf-8")
    result["reference"].update({
        "bytes": len(body), "sha256": sha256(body),
        "semantic_sha256": semantic_sha256(docs), "documents": len(docs),
        "hypothesis_rows": 17 * len(docs),
    })
    return result, body


class DuplicateReferenceTests(unittest.TestCase):
    def test_equivalent_text_aliases_preserve_ids_without_inflating_metric_units(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config, _body, docs = replay_fixture(root)
            alias = deepcopy(docs[0])
            alias["document_id"] = "3"
            config, body = replace_reference_fixture(config, [*docs, alias])
            report = prepare_replay(root, body, config, COMMIT, root / "out")
            group = report["groups"]["fixture"]
            self.assertEqual(report["reference_dataset_documents"], 3)
            self.assertEqual(report["reference_unique_texts"], 2)
            self.assertEqual(group["unique_input_contracts"], 2)
            self.assertEqual(group["matched_reference_records"], 3)
            self.assertEqual(group["matched_reference_record_ids"], ["1", "2", "3"])
            self.assertEqual(group["reference_subset_fraction"], 1)
            self.assertEqual(group["request_level"]["attempted_documents"], 3)
            self.assertEqual(group["unique_documents_first_attempt"]["attempted_documents"], 2)
            self.assertAlmostEqual(group["request_level"]["label_accuracy_all_attempts"], 33 / 51)
            self.assertAlmostEqual(
                group["unique_documents_first_attempt"]["label_accuracy_all_attempts"], 16 / 34
            )
            attempts = [json.loads(line) for line in (root / "out/attempts.jsonl").read_text().splitlines()]
            aliases = [row for row in attempts if row["reference_document_id"] == "1"]
            self.assertTrue(aliases)
            self.assertTrue(all(row["reference_document_ids"] == ["1", "3"] for row in aliases))
            self.assertTrue(all(row["equivalent_reference_aliases"] for row in aliases))

    def test_conflicting_requested_labels_or_evidence_fail_without_report(self):
        for field, value in (
            ("label", "contradiction"), ("source_clause", "Synthetic fixture contract"),
        ):
            with self.subTest(field=field), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                config, _body, docs = replay_fixture(root)
                alias = deepcopy(docs[0])
                alias["document_id"] = "3"
                alias["inference"][0][field] = value
                config, body = replace_reference_fixture(config, [*docs, alias])
                with self.assertRaisesRegex(ValueError, "Conflicting reference annotations.*1, 3"):
                    prepare_replay(root, body, config, COMMIT, root / "out")
                self.assertFalse((root / "out").exists())

    def test_unrequested_conflicting_text_group_does_not_block_identifiable_inputs(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config, _body, docs = replay_fixture(root)
            other = reference_fixture(4)
            conflicting = deepcopy(other)
            conflicting["document_id"] = "5"
            conflicting["inference"][0]["label"] = "contradiction"
            config, body = replace_reference_fixture(config, [*docs, other, conflicting])
            report = prepare_replay(root, body, config, COMMIT, root / "out")
            group = report["groups"]["fixture"]
            self.assertEqual(group["matched_reference_records"], 2)
            self.assertEqual(group["reference_subset_fraction"], 0.5)
            self.assertEqual(group["request_level"]["attempted_documents"], 3)
            self.assertEqual(report["duplicate_reference_text_groups"][0]["document_ids"], ["4", "5"])
            self.assertFalse(report["duplicate_reference_text_groups"][0]["equivalent_annotations"])

    def test_duplicate_document_ids_remain_invalid_when_text_and_annotations_agree(self):
        with tempfile.TemporaryDirectory() as directory:
            config, _body, docs = replay_fixture(Path(directory))
            config, body = replace_reference_fixture(config, [*docs, deepcopy(docs[0])])
            with self.assertRaisesRegex(ValueError, "Duplicate document_id"):
                reference_documents(body, config["reference"])


class ReplayMetricTests(unittest.TestCase):
    def report(self, root):
        config, reference, docs = replay_fixture(root)
        report = prepare_replay(root, reference, config, COMMIT, root / "out")
        return report, config, reference, docs

    def test_wrong_label_and_missing_request_have_known_all_attempts_denominator(self):
        with tempfile.TemporaryDirectory() as directory:
            report, _config, _reference, _docs = self.report(Path(directory))
            metrics = report["groups"]["fixture"]["request_level"]
            self.assertEqual(metrics["attempted_documents"], 3)
            self.assertEqual(metrics["scored_documents"], 2)
            self.assertEqual(metrics["missing_documents"], 1)
            self.assertEqual(metrics["invalid_documents"], 0)
            self.assertAlmostEqual(metrics["label_accuracy_all_attempts"], 33 / 51)
            self.assertAlmostEqual(metrics["coverage"], 2 / 3)

    def test_first_attempt_is_earliest_and_does_not_select_successful_retry(self):
        with tempfile.TemporaryDirectory() as directory:
            report, _config, _reference, _docs = self.report(Path(directory))
            group = report["groups"]["fixture"]
            self.assertEqual(group["repeated_requests"], 1)
            self.assertEqual(group["unique_reference_documents"], 2)
            first = group["unique_documents_first_attempt"]
            self.assertEqual(first["attempted_documents"], 2)
            self.assertEqual(first["missing_documents"], 1)
            self.assertAlmostEqual(first["label_accuracy_all_attempts"], 16 / 34)
            self.assertEqual(group["failure_accounting"]["recorded_transport_failures"], 1)

    def test_malformed_response_counts_as_invalid_instead_of_disappearing(self):
        doc = reference_fixture(1)
        attempt = {
            "timestamp_ms": 1700000000000, "source_file": "fixture",
            "source_record": 1, "reference_document_id": "1", "attempt_id": "attempt-1",
            "prediction_status": "invalid_output_json", "inference": None,
        }
        metrics = evaluate_view([attempt], {"1": doc},
                                {"kind": "fixture", "source": "synthetic unit fixture"})
        self.assertEqual(metrics["invalid_documents"], 1)
        self.assertEqual(metrics["label_accuracy_all_attempts"], 0)
        self.assertIsNone(metrics["label_accuracy_scored_only"])

    def test_unmatched_contracts_fail_before_any_report_is_written(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config, reference, _docs = replay_fixture(root)
            path = root / "benchmark/fixture.log"
            body = path.read_bytes().replace(b"Synthetic fixture contract 1.",
                                             b"Unmatched fixture contract 1.")
            path.write_bytes(body)
            config["groups"][0]["files"][0] = source_entry("benchmark/fixture.log", body)
            with self.assertRaisesRegex(ValueError, "exact"):
                prepare_replay(root, reference, config, COMMIT, root / "out")
            self.assertFalse((root / "out").exists())

    def test_source_change_or_request_count_mismatch_fails_without_report(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config, reference, _docs = replay_fixture(root)
            bad = deepcopy(config)
            bad["groups"][0]["files"][0]["git_blob_sha1"] = "0" * 40
            with self.assertRaisesRegex(ValueError, "blob"):
                prepare_replay(root, reference, bad, COMMIT, root / "out")
            bad = deepcopy(config)
            bad["groups"][0]["requests"] = 4
            with self.assertRaisesRegex(ValueError, "count"):
                prepare_replay(root, reference, bad, COMMIT, root / "out")
            self.assertFalse((root / "out").exists())

    def test_report_distinguishes_historical_prompt_unknown_model_and_literal_evidence_audit(self):
        with tempfile.TemporaryDirectory() as directory:
            report, _config, _reference, _docs = self.report(Path(directory))
            group = report["groups"]["fixture"]
            provenance = group["request_level"]["provenance"]
            self.assertEqual(provenance["kind"], "historical_predictions")
            self.assertIsNone(provenance["model_revision"])
            self.assertIsNone(provenance["adapter_revision"])
            self.assertFalse(provenance["new_cpu_prompt_used_for_generation"])
            self.assertEqual(report["reference_dataset_documents"], 2)
            self.assertEqual(group["evidence_audit"]["nonempty_evidence_fields"], 3)
            self.assertEqual(group["evidence_audit"]["exact_contiguous_substrings"], 2)
            self.assertFalse(report["live_inference"])
            text = (Path(directory) / "out/attempts.jsonl").read_text()
            self.assertNotIn("<|im_start|>", text)
            self.assertEqual(len(text.splitlines()), 3)

    def test_existing_evaluation_output_is_not_overwritten(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _report, config, reference, _docs = self.report(root)
            before = (root / "out/report.json").read_bytes()
            with self.assertRaisesRegex(ValueError, "must not exist"):
                prepare_replay(root, reference, config, COMMIT, root / "out")
            self.assertEqual((root / "out/report.json").read_bytes(), before)


class FakeReplayAws:
    def __init__(self, reference, identity, failure=None):
        self.reference, self.identity, self.failure = reference, identity, failure
        self.calls, self.objects = [], {}

    def __call__(self, *args):
        self.calls.append(args)
        if args[0] != "s3api":
            raise AssertionError("Replay must use only scoped S3 operations")
        action = args[1]
        if action == "get-bucket-location":
            return {"LocationConstraint": None}
        if action == "get-bucket-versioning":
            return {"Status": "Enabled"}
        key = args[args.index("--key") + 1]
        if action == "head-object":
            return {
                "VersionId": "wrong" if self.failure == "reference-version" else self.identity["version_id"],
                "ContentLength": self.identity["bytes"], "ServerSideEncryption": "AES256",
            }
        if action == "get-object" and key == self.identity["key"]:
            body = self.reference + (b"x" if self.failure == "reference-bytes" else b"")
            Path(args[-1]).write_bytes(body)
            return {
                "VersionId": "wrong" if self.failure == "reference-race" else self.identity["version_id"],
                "ServerSideEncryption": "AES256",
            }
        if action == "put-object":
            if args[args.index("--if-none-match") + 1] != "*":
                raise AssertionError("Conditional writes required")
            if key in self.objects:
                raise RuntimeError("Precondition failed")
            if self.failure == "manifest-put" and key.endswith("/evaluation.json"):
                raise RuntimeError("Injected final upload failure")
            body = Path(args[args.index("--body") + 1]).read_bytes()
            version = "version-" + str(len(self.objects) + 1)
            self.objects[key] = (body, version)
            return {"VersionId": version}
        if action == "get-object":
            body, version = self.objects[key]
            if self.failure == "report-readback" and key.endswith("/report.json"):
                body += b"x"
            Path(args[-1]).write_bytes(body)
            return {"VersionId": version, "ServerSideEncryption": "AES256"}
        raise AssertionError("Unexpected replay operation")


class ReplayCloudTests(unittest.TestCase):
    def test_reference_metadata_race_and_bytes_are_checked_without_writes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config, reference, _docs = replay_fixture(root)
            for failure in ("reference-version", "reference-race", "reference-bytes"):
                aws = FakeReplayAws(reference, config["reference"], failure)
                with self.subTest(failure=failure), self.assertRaises(ValueError):
                    download_reference(config["reference"], root / "download.jsonl", aws)
                self.assertEqual(aws.objects, {})

    def test_complete_replay_publishes_encrypted_versions_and_final_manifest_last(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config, reference, _docs = replay_fixture(root)
            aws = FakeReplayAws(reference, config["reference"])
            receipt = run_replay(root, COMMIT, ENVIRON, config, aws)
            self.assertEqual(len(aws.objects), 3)
            self.assertEqual(list(aws.objects)[-1], receipt["manifest"]["key"])
            self.assertTrue(all("/evaluation/" in key for key in aws.objects))
            manifest = json.loads(aws.objects[receipt["manifest"]["key"]][0])
            self.assertEqual(manifest["evaluation_kind"], "historical_benchmark_replay")
            self.assertEqual(manifest["required_build_status"], "SUCCEEDED")
            self.assertEqual(manifest["groups"]["fixture"]["requests"], 3)
            for artifact in manifest["artifacts"].values():
                body, version = aws.objects[artifact["key"]]
                self.assertEqual(artifact["sha256"], sha256(body))
                self.assertEqual(artifact["version_id"], version)
                self.assertEqual(artifact["encryption"], "AES256")
            self.assertTrue(all("--version-id" not in call for call in aws.calls))

    def test_readback_and_final_upload_failures_do_not_publish_completion(self):
        for failure in ("report-readback", "manifest-put"):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                config, reference, _docs = replay_fixture(root)
                aws = FakeReplayAws(reference, config["reference"], failure)
                with self.assertRaises((ValueError, RuntimeError)):
                    run_replay(root, COMMIT, ENVIRON, config, aws)
                self.assertFalse(any(key.endswith("/evaluation.json") for key in aws.objects))

    def test_wrong_destination_rejected_before_any_aws_call(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config, reference, _docs = replay_fixture(root)
            aws = FakeReplayAws(reference, config["reference"])
            with self.assertRaisesRegex(ValueError, "bucket"):
                run_replay(root, COMMIT, {**ENVIRON, "OUTPUT_BUCKET": "other"}, config, aws)
            self.assertEqual(aws.calls, [])

    def test_repeat_build_prefix_cannot_overwrite_existing_evaluation_artifacts(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config, reference, _docs = replay_fixture(root)
            aws = FakeReplayAws(reference, config["reference"])
            run_replay(root, COMMIT, ENVIRON, config, aws)
            before = deepcopy(aws.objects)
            with self.assertRaisesRegex(RuntimeError, "Precondition"):
                run_replay(root, COMMIT, ENVIRON, config, aws)
            self.assertEqual(aws.objects, before)


if __name__ == "__main__":
    unittest.main()
