"""CPU catalog fixtures only; run in CodeBuild, never generate model predictions."""

from copy import deepcopy
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
import zipfile

from src.cpu.catalog_publish import BUCKET, destination, publish_catalog
from src.cpu.hypotheses import HYPOTHESES
from src.cpu.prompt import build_prompt, json_bytes
from src.cpu.source_catalog import (
    ARTIFACT_NAMES, SOURCE_PATH, archive_identity, download_release,
    official_rows, parse_json, prepare_catalog, read_release,
)
from src.cpu.transform import semantic_sha256, transform_rows

COMMIT = "a" * 40
BUILD = "legal-llm-cpu-check:00000000-0000-4000-8000-000000000001"
ENVIRON = {
    "AWS_DEFAULT_REGION": "us-east-1", "OUTPUT_BUCKET": BUCKET,
    "CODEBUILD_BUILD_ID": BUILD,
}


def official_fixture(key, text):
    return {
        "labels": {hid: {"hypothesis": text} for hid, text in HYPOTHESES.items()},
        "documents": [{
            "id": key, "text": text, "spans": [[0, len(text)]],
            "annotation_sets": [{"annotations": {
                hid: {
                    "choice": "Entailment" if hid == "nda-1" else "NotMentioned",
                    "spans": [0] if hid == "nda-1" else [],
                } for hid in HYPOTHESES
            }}],
        }],
    }


def release_fixture(root, *, leak=False):
    import pyarrow as pa
    import pyarrow.parquet as pq

    source = parse_json(SOURCE_PATH.read_bytes())
    source["expected_documents"] = {"train": 1, "validation": 1, "test": 1}
    source["tracked_semantic_sha256"] = {}
    payloads = {
        "train": official_fixture(1, "Synthetic training contract."),
        "validation": official_fixture(2, "Synthetic validation contract."),
        "test": official_fixture(3, "Synthetic held-out contract."),
    }
    if leak:
        payloads["test"]["documents"][0]["text"] = payloads["train"]["documents"][0]["text"]
        payloads["test"]["documents"][0]["spans"] = deepcopy(payloads["train"]["documents"][0]["spans"])
    tracked = root / "tracked"
    tracked.mkdir()
    for split in ("train", "validation"):
        rows = official_rows(json_bytes(payloads[split]))
        pq.write_table(pa.Table.from_pylist(rows), tracked / (split + ".parquet"))
        source["tracked_semantic_sha256"][split] = semantic_sha256(transform_rows(rows)[0])
    archive_path = root / "release.zip"
    with zipfile.ZipFile(archive_path, "w") as archive:
        for split, name in source["split_files"].items():
            archive.writestr("contract-nli/" + name, json_bytes(payloads[split]))
        archive.writestr("../ignored.pdf", b"not a PDF; never extracted")
    raw = archive_path.read_bytes()
    source["archive_bytes"] = len(raw)
    source["archive_git_blob_sha1"] = hashlib.sha1(
        ("blob " + str(len(raw)) + "\0").encode("ascii") + raw
    ).hexdigest()
    return archive_path, tracked, source


class OfficialSourceTests(unittest.TestCase):
    def test_known_evidence_offsets_preserve_text_and_all_hypotheses(self):
        fixture = official_fixture(8, "Quoted evidence. Unrelated words.")
        fixture["documents"][0]["spans"] = [[0, 6], [7, 16]]
        fixture["documents"][0]["annotation_sets"][0]["annotations"]["nda-1"]["spans"] = [0, 1]
        docs, counts = transform_rows(official_rows(json_bytes(fixture)))
        self.assertEqual(counts, {"documents": 1, "input_rows": 17})
        self.assertEqual(docs[0]["document_id"], "8")
        self.assertEqual(docs[0]["text"], "Quoted evidence. Unrelated words.")
        self.assertEqual(docs[0]["inference"][0]["source_clause"], "Quoted. evidence.")
        self.assertEqual(docs[0]["inference"][1]["label"], "not_mentioned")
        self.assertEqual(docs[0]["inference"][1]["source_clause"], "")

    def test_corrupt_offsets_indices_labels_and_annotation_sets_are_rejected(self):
        original = official_fixture(1, "Fixture evidence.")
        cases = []
        for spans in ([[0, 999]], [[-1, 2]], [[True, 2]], [[2, 2]]):
            case = deepcopy(original)
            case["documents"][0]["spans"] = spans
            cases.append(case)
        for field, value in (("spans", [99]), ("spans", [True]), ("spans", [0, 0]), ("choice", "Maybe")):
            case = deepcopy(original)
            case["documents"][0]["annotation_sets"][0]["annotations"]["nda-1"][field] = value
            cases.append(case)
        case = deepcopy(original)
        case["documents"][0]["annotation_sets"][0]["annotations"]["nda-2"]["spans"] = [0]
        cases.append(case)
        case = deepcopy(original)
        case["documents"][0]["annotation_sets"].append(deepcopy(case["documents"][0]["annotation_sets"][0]))
        cases.append(case)
        for case in cases:
            with self.subTest(case=len(cases)), self.assertRaises(ValueError):
                official_rows(json_bytes(case))

    def test_duplicate_documents_missing_annotations_and_wrong_hypotheses_fail(self):
        original = official_fixture(1, "Synthetic evidence.")
        cases = []
        case = deepcopy(original)
        case["documents"].append(deepcopy(case["documents"][0]))
        cases.append(case)
        case = deepcopy(original)
        del case["documents"][0]["annotation_sets"][0]["annotations"]["nda-20"]
        cases.append(case)
        case = deepcopy(original)
        case["labels"]["nda-12"]["hypothesis"] = "A different task"
        cases.append(case)
        for case in cases:
            with self.assertRaises(ValueError):
                official_rows(json_bytes(case))
        for payload in (b'{"documents": [], "documents": []}', b'{"value": NaN}'):
            with self.assertRaises(ValueError):
                parse_json(payload)

    def test_release_reads_only_unique_bounded_json_members_without_extraction(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path, _tracked, source = release_fixture(root)
            payloads = read_release(path, source)
            self.assertEqual(set(payloads), {"train", "validation", "test"})
            self.assertEqual({item.name for item in root.iterdir()}, {"release.zip", "tracked"})
            with self.assertRaisesRegex(ValueError, "size"):
                read_release(path, source, max_json_bytes=5)
            with zipfile.ZipFile(path, "a") as archive:
                archive.writestr("duplicate/test.json", payloads["test"])
            with self.assertRaisesRegex(ValueError, "ambiguous"):
                read_release(path, source)

    def test_missing_release_split_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "missing.zip"
            with zipfile.ZipFile(path, "w") as archive:
                archive.writestr("train.json", b"{}")
                archive.writestr("dev.json", b"{}")
            source = parse_json(SOURCE_PATH.read_bytes())
            with self.assertRaisesRegex(ValueError, "Missing"):
                read_release(path, source)

    def test_pinned_download_checks_git_blob_identity_size_and_sha256(self):
        raw = b"small synthetic release bytes"
        source = {
            "archive_url": "https://example.invalid/fixture",
            "archive_bytes": len(raw),
            "archive_git_blob_sha1": hashlib.sha1(
                ("blob " + str(len(raw)) + "\0").encode("ascii") + raw
            ).hexdigest(),
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            digest = download_release(root / "valid.zip", source,
                                      opener=lambda *_args, **_kw: io.BytesIO(raw))
            self.assertEqual(digest, hashlib.sha256(raw).hexdigest())
            for name, body in (("oversize", raw + b"x"), ("short", raw[:-1]),
                               ("corrupt", b"X" + raw[1:])):
                with self.subTest(name=name), self.assertRaises(ValueError):
                    download_release(root / name, source,
                                     opener=lambda *_args, body=body, **_kw: io.BytesIO(body))

    def test_three_split_preparation_and_prompt_are_reproducible_without_overwrite(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path, tracked, source = release_fixture(root)
            result = prepare_catalog(path, tracked, root / "first", COMMIT, source)
            second = prepare_catalog(path, tracked, root / "second", COMMIT, source)
            self.assertEqual(set(p.name for p in (root / "first").iterdir()), set(ARTIFACT_NAMES))
            self.assertEqual(result["splits"], second["splits"])
            self.assertEqual(result["prompt"], second["prompt"])
            self.assertEqual(result["upstream"]["archive_sha256"], archive_identity(path, source))
            self.assertEqual(result["splits"]["test"]["documents"], 1)
            self.assertFalse(result["live_inference"])
            with self.assertRaisesRegex(ValueError, "must not exist"):
                prepare_catalog(path, tracked, root / "first", COMMIT, source)

    def test_wrong_counts_baselines_and_commits_leave_no_output(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path, tracked, source = release_fixture(root)
            changed_count = deepcopy(source)
            changed_count["expected_documents"]["test"] = 2
            changed_baseline = deepcopy(source)
            changed_baseline["tracked_semantic_sha256"]["train"] = "0" * 64
            for index, (config, commit) in enumerate((
                (changed_count, COMMIT), (changed_baseline, COMMIT), (source, "main"),
            )):
                output = root / ("rejected-" + str(index))
                with self.assertRaises(ValueError):
                    prepare_catalog(path, tracked, output, commit, config)
                self.assertFalse(output.exists())

    def test_test_split_leakage_leaves_no_prepared_output(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path, tracked, source = release_fixture(root, leak=True)
            output = root / "rejected"
            with self.assertRaisesRegex(ValueError, "Overlapping"):
                prepare_catalog(path, tracked, output, COMMIT, source)
            self.assertFalse(output.exists())

    def test_official_content_must_match_tracked_baseline(self):
        import pyarrow as pa
        import pyarrow.parquet as pq

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path, tracked, source = release_fixture(root)
            # An approved tracked variant still cannot silently replace different official data.
            rows = official_rows(json_bytes(official_fixture(1, "Changed tracked text.")))
            pq.write_table(pa.Table.from_pylist(rows), tracked / "train.parquet")
            source["tracked_semantic_sha256"]["train"] = semantic_sha256(transform_rows(rows)[0])
            with self.assertRaisesRegex(ValueError, "Official and tracked"):
                prepare_catalog(path, tracked, root / "out", COMMIT, source)
            self.assertFalse((root / "out").exists())


class PromptTests(unittest.TestCase):
    def test_canonical_ids_and_json_object_envelope_are_consistent(self):
        prompt = build_prompt()
        schema = prompt["response_format"]["json_schema"]["schema"]
        self.assertEqual(schema["type"], "object")
        self.assertEqual(schema["required"], ["Hypotheses"])
        self.assertFalse(schema["additionalProperties"])
        array = schema["properties"]["Hypotheses"]
        self.assertEqual((array["minItems"], array["maxItems"]), (17, 17))
        self.assertEqual(array["items"]["properties"]["hypothesis_id"]["enum"], list(HYPOTHESES))
        for hid in ("nda-12", "nda-20"):
            self.assertIn(hid + ": " + HYPOTHESES[hid], prompt["messages"][0]["content"])
        self.assertIn("JSON object", prompt["messages"][0]["content"])

    def test_prompt_is_deterministic_unbound_and_has_no_contract_or_model_call(self):
        first, second = json_bytes(build_prompt()), json_bytes(build_prompt())
        self.assertEqual(first, second)
        self.assertEqual(build_prompt()["messages"][1]["content"], "{contract}")
        self.assertIsNone(build_prompt()["model_binding"])
        self.assertFalse(build_prompt()["live_inference"])


class FakeAws:
    def __init__(self, failure=None):
        self.calls, self.objects, self.failure = [], {}, failure

    def __call__(self, *args):
        self.calls.append(args)
        action = args[1]
        if action == "get-bucket-location":
            return {"LocationConstraint": None}
        if action == "get-bucket-versioning":
            return {"Status": "Suspended" if self.failure == "versioning" else "Enabled"}
        key = args[args.index("--key") + 1]
        if action == "put-object":
            if args[args.index("--if-none-match") + 1] != "*":
                raise AssertionError("Conditional writes required")
            if key in self.objects:
                raise RuntimeError("Precondition failed")
            body = Path(args[args.index("--body") + 1]).read_bytes()
            version = "version-" + str(len(self.objects) + 1)
            self.objects[key] = (body, version)
            return {"VersionId": "null" if self.failure == "missing-version" else version}
        if action == "get-object":
            body, version = self.objects[key]
            Path(args[-1]).write_bytes(body + (b"corrupt" if self.failure == "bytes" else b""))
            return {
                "VersionId": "other" if self.failure == "version" else version,
                "ServerSideEncryption": "aws:kms" if self.failure == "encryption" else "AES256",
            }
        raise AssertionError("Unexpected AWS operation")


class CatalogPublicationTests(unittest.TestCase):
    def prepared(self, root):
        path, tracked, source = release_fixture(root)
        directory = root / "prepared"
        prepare_catalog(path, tracked, directory, COMMIT, source)
        return directory

    def test_complete_catalog_is_published_last_with_real_versions_and_build_gate(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = self.prepared(Path(temporary))
            aws = FakeAws()
            receipt = publish_catalog(directory, COMMIT, ENVIRON, aws)
            writes = [call for call in aws.calls if call[1] == "put-object"]
            self.assertEqual(len(writes), len(ARTIFACT_NAMES) + 1)
            self.assertEqual(writes[-1][writes[-1].index("--key") + 1], receipt["catalog"]["key"])
            catalog = parse_json(aws.objects[receipt["catalog"]["key"]][0])
            self.assertEqual(set(catalog["artifacts"]), set(ARTIFACT_NAMES))
            self.assertEqual(catalog["required_build_status"], "SUCCEEDED")
            self.assertEqual(catalog["registration_scope"], "versioned_s3_manifest")
            self.assertEqual(catalog["status"], "ARTIFACTS_VERIFIED")
            self.assertFalse(catalog["live_inference"])
            for artifact in catalog["artifacts"].values():
                body, version = aws.objects[artifact["key"]]
                self.assertEqual(artifact["version_id"], version)
                self.assertEqual(artifact["sha256"], hashlib.sha256(body).hexdigest())
            self.assertTrue(all("--version-id" not in call for call in aws.calls))

    def test_changed_local_bytes_fail_before_any_aws_operation(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = self.prepared(Path(temporary))
            (directory / "test.jsonl").write_bytes(b"tampered\n")
            aws = FakeAws()
            with self.assertRaisesRegex(ValueError, "changed"):
                publish_catalog(directory, COMMIT, ENVIRON, aws)
            self.assertEqual(aws.calls, [])

    def test_readback_failures_never_publish_catalog(self):
        for failure in ("missing-version", "bytes", "version", "encryption", "versioning"):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as temporary:
                directory = self.prepared(Path(temporary))
                aws = FakeAws(failure)
                with self.assertRaises(ValueError):
                    publish_catalog(directory, COMMIT, ENVIRON, aws)
                self.assertFalse(any(key.endswith("/catalog.json") for key in aws.objects))

    def test_existing_keys_are_not_overwritten_on_repeat_publication(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = self.prepared(Path(temporary))
            aws = FakeAws()
            publish_catalog(directory, COMMIT, ENVIRON, aws)
            original = deepcopy(aws.objects)
            with self.assertRaisesRegex(RuntimeError, "Precondition"):
                publish_catalog(directory, COMMIT, ENVIRON, aws)
            self.assertEqual(aws.objects, original)

    def test_other_buckets_regions_projects_and_noncanonical_ids_are_rejected(self):
        for field, value in (
            ("OUTPUT_BUCKET", "another-bucket"), ("AWS_DEFAULT_REGION", "us-west-2"),
            ("CODEBUILD_BUILD_ID", "other:" + BUILD.split(":")[1]),
            ("CODEBUILD_BUILD_ID", "legal-llm-cpu-check:../../arbitrary"),
            ("CODEBUILD_BUILD_ID", "legal-llm-cpu-check:" + "A" * 36),
        ):
            environment = {**ENVIRON, field: value}
            with self.subTest(field=field), self.assertRaises(ValueError):
                destination(environment)


if __name__ == "__main__":
    unittest.main()
