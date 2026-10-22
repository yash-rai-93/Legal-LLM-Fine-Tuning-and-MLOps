"""Recover pinned official ContractNLI splits and prepare a versioned CPU catalog.

Run only in AWS CodeBuild. Does not invoke models or update deployed resources.
"""

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import tempfile
import urllib.request
import zipfile

from .hypotheses import HYPOTHESES
from .prompt import build_prompt, json_bytes
from .transform import semantic_sha256, transform_rows, validate_split_disjointness
from .validate import document_id

SOURCE_PATH = Path(__file__).resolve().parents[2] / "config/contractnli-source.json"
MAX_JSON_BYTES = 32 * 1024 * 1024
ARTIFACT_NAMES = (
    "source-train.json", "source-validation.json", "source-test.json",
    "train.jsonl", "validation.jsonl", "test.jsonl",
    "prompt.json", "attribution.json", "preparation.json",
)


def require(condition, message):
    if not condition:
        raise ValueError(message)


def unique_pairs(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, "Duplicate JSON key")
        result[key] = value
    return result


def invalid_constant(_value):
    raise ValueError("Non-finite JSON value")


def parse_json(payload):
    return json.loads(
        payload, object_pairs_hook=unique_pairs, parse_constant=invalid_constant
    )


def archive_identity(path, source):
    expected_size = source["archive_bytes"]
    require(path.stat().st_size == expected_size, "Release archive size mismatch")
    blob = hashlib.sha1()
    blob.update(("blob " + str(expected_size) + "\0").encode("ascii"))
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            blob.update(chunk)
            digest.update(chunk)
    require(
        blob.hexdigest() == source["archive_git_blob_sha1"],
        "Pinned upstream Git blob identity mismatch",
    )
    return digest.hexdigest()


def download_release(path, source, opener=None):
    # URL is from the reviewed immutable configuration, never an API request.
    opener = opener or urllib.request.urlopen
    limit = source["archive_bytes"]
    length = 0
    with opener(source["archive_url"], timeout=30) as response, path.open("xb") as target:
        for chunk in iter(lambda: response.read(1024 * 1024), b""):
            length += len(chunk)
            require(length <= limit, "Release download exceeds pinned size")
            target.write(chunk)
    return archive_identity(path, source)


def official_rows(payload):
    data = parse_json(payload)
    require(isinstance(data, dict), "Official split must be an object")
    labels, documents = data.get("labels"), data.get("documents")
    require(isinstance(labels, dict) and set(labels) == set(HYPOTHESES),
            "Official hypothesis IDs differ")
    for hid, hypothesis in HYPOTHESES.items():
        require(isinstance(labels[hid], dict)
                and labels[hid].get("hypothesis") == hypothesis,
                "Official hypothesis definition differs: " + hid)
    require(isinstance(documents, list) and documents, "Official split is empty")
    rows, seen = [], set()
    choices = {
        "Entailment": "entailment", "Contradiction": "contradiction",
        "NotMentioned": "not_mentioned",
    }
    for doc in documents:
        require(isinstance(doc, dict), "Official document must be an object")
        key = document_id(doc.get("id"))
        require(key not in seen, "Duplicate official document ID")
        seen.add(key)
        text, spans, sets = doc.get("text"), doc.get("spans"), doc.get("annotation_sets")
        require(isinstance(text, str) and text.strip(), "Invalid official document text")
        require(isinstance(spans, list), "Invalid official evidence spans")
        for span in spans:
            require(
                isinstance(span, list) and len(span) == 2
                and all(type(index) is int for index in span)
                and 0 <= span[0] < span[1] <= len(text),
                "Official span offsets outside document",
            )
        require(isinstance(sets, list) and len(sets) == 1
                and isinstance(sets[0], dict), "Expected one official annotation set")
        annotations = sets[0].get("annotations")
        require(isinstance(annotations, dict) and set(annotations) == set(HYPOTHESES),
                "Missing or unknown official annotations")
        for hid, hypothesis in HYPOTHESES.items():
            annotation = annotations[hid]
            require(isinstance(annotation, dict), "Invalid official annotation")
            choice, indices = annotation.get("choice"), annotation.get("spans")
            require(isinstance(choice, str) and choice in choices, "Invalid official label")
            require(
                isinstance(indices, list)
                and all(type(index) is int and 0 <= index < len(spans) for index in indices),
                "Official evidence index outside span table",
            )
            require(len(set(indices)) == len(indices), "Duplicate official evidence index")
            require(choice != "NotMentioned" or not indices,
                    "NotMentioned has unexpected evidence")
            rows.append({
                "document_id": key, "text": text, "hypothesis_id": hid,
                "hypothesis": hypothesis, "label": choices[choice],
                "evidence_texts": [text[spans[index][0]:spans[index][1]] for index in indices],
            })
    return rows


def read_release(path, source, max_json_bytes=MAX_JSON_BYTES):
    payloads = {}
    with zipfile.ZipFile(path) as archive:
        for split, basename in source["split_files"].items():
            matches = [
                entry for entry in archive.infolist()
                if not entry.is_dir() and entry.filename.rsplit("/", 1)[-1] == basename
            ]
            require(len(matches) == 1, "Missing or ambiguous official split: " + split)
            entry = matches[0]
            require(0 < entry.file_size <= max_json_bytes, "Official split size outside limits")
            # Never extract archive paths or open PDFs. Read only three bounded JSON members.
            with archive.open(entry) as stream:
                payload = stream.read(max_json_bytes + 1)
            require(len(payload) == entry.file_size, "Official split is truncated or oversized")
            payloads[split] = payload
    return payloads


def prepare_catalog(archive_path, tracked_dir, output_dir, source_commit, source):
    import pyarrow.parquet as pq

    require(isinstance(source_commit, str) and re.fullmatch(r"[0-9a-f]{40}", source_commit),
            "An immutable catalog source commit is required")
    require(set(source["split_files"]) == {"train", "validation", "test"},
            "All three official splits are required")
    require(set(source["expected_documents"]) == {"train", "validation", "test"},
            "Expected counts for all splits are required")
    require(set(source["tracked_semantic_sha256"]) == {"train", "validation"},
            "Both existing split identities are required")
    output_dir = Path(output_dir)
    require(not output_dir.exists(), "Catalog output directory must not exist")
    archive_sha256 = archive_identity(Path(archive_path), source)
    payloads = read_release(archive_path, source)
    processed, splits = {}, {}
    for split, payload in payloads.items():
        documents, counts = transform_rows(official_rows(payload))
        require(counts["documents"] == source["expected_documents"][split],
                "Official document count differs: " + split)
        processed[split] = documents
        splits[split] = {
            **counts,
            "source_file": source["split_files"][split],
            "source_sha256": hashlib.sha256(payload).hexdigest(),
            "semantic_sha256": semantic_sha256(documents),
        }
    validate_split_disjointness(processed)
    for split, expected in source["tracked_semantic_sha256"].items():
        tracked, _counts = transform_rows(
            pq.read_table(Path(tracked_dir) / (split + ".parquet")).to_pylist()
        )
        require(semantic_sha256(tracked) == expected,
                "Tracked dataset differs from approved baseline: " + split)
        require(splits[split]["semantic_sha256"] == expected,
                "Official and tracked dataset content differ: " + split)
    artifacts = {}
    for split, documents in processed.items():
        artifacts["source-" + split + ".json"] = payloads[split]
        body = "".join(
            json.dumps(row, ensure_ascii=False, allow_nan=False) + "\n" for row in documents
        ).encode("utf-8")
        artifacts[split + ".jsonl"] = body
        splits[split]["output_sha256"] = hashlib.sha256(body).hexdigest()
    prompt = json_bytes(build_prompt())
    artifacts["prompt.json"] = prompt
    artifacts["attribution.json"] = json_bytes({
        "title": "ContractNLI: A Dataset for Document-level Natural Language Inference for Contracts",
        "authors": ["Yuta Koreeda", "Christopher D. Manning"],
        "year": 2021,
        "paper": "https://aclanthology.org/2021.findings-emnlp.164/",
        "source": source["dataset_source"],
        "license": source["dataset_license"],
        "license_url": "https://creativecommons.org/licenses/by/4.0/",
        "upstream_commit": source["upstream_commit"],
        "changes": [
            "dev split named validation; original official split memberships preserved",
            "labels normalized; evidence spans sliced from original text and joined with '. '",
            "documents ordered by normalized ID; text and all 17 hypotheses preserved",
            "new prompt/schema assembled from the canonical hypotheses; no model executed",
        ],
    })
    manifest = {
        "schema_version": "contractnli-catalog-preparation-v1",
        "operation": "dataset_prompt_preparation",
        "live_inference": False,
        "source_commit": source_commit,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "upstream": {
            **{key: source[key] for key in (
                "dataset_source", "dataset_license", "upstream_repository",
                "upstream_commit", "archive_url", "archive_bytes", "archive_git_blob_sha1"
            )},
            "archive_sha256": archive_sha256,
        },
        "splits": splits,
        "prompt": {
            "name": "contract_extractor_cpu_v1", "file": "prompt.json",
            "sha256": hashlib.sha256(prompt).hexdigest(),
            "model_binding": None,
        },
        "checks": {
            "expected_counts": True, "canonical_hypotheses": True,
            "split_id_and_exact_text_disjoint": True,
            "tracked_train_validation_semantic_match": True,
        },
        "artifact_sha256": {
            name: hashlib.sha256(body).hexdigest() for name, body in artifacts.items()
        },
    }
    artifacts["preparation.json"] = json_bytes(manifest)
    require(set(artifacts) == set(ARTIFACT_NAMES), "Unexpected catalog artifacts")
    # All split/provenance checks succeed before any prepared output is written.
    output_dir.mkdir(parents=True, exist_ok=False)
    for name, body in artifacts.items():
        (output_dir / name).write_bytes(body)
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tracked-dir", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--source-commit", required=True)
    args = parser.parse_args()
    source = parse_json(SOURCE_PATH.read_bytes())
    with tempfile.TemporaryDirectory(prefix="legal-cpu-release-") as workspace:
        path = Path(workspace) / "release.zip"
        download_release(path, source)
        manifest = prepare_catalog(
            path, args.tracked_dir, args.output_dir, args.source_commit, source
        )
    print(json.dumps(manifest, indent=2))  # Metadata only, never contract content.


if __name__ == "__main__":
    main()
