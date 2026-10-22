"""Rescore pinned historical benchmark responses, without invoking any model."""

from datetime import datetime, timezone
import hashlib
import json
from importlib.metadata import version
from pathlib import Path
import re

from .evaluate import evaluate_predictions
from .hypotheses import HYPOTHESES
from .prompt import json_bytes
from .source_catalog import parse_json, require
from .transform import semantic_sha256
from .validate import index_documents, validate_hypotheses

CONFIG_PATH = Path(__file__).resolve().parents[2] / "config/historical-replay.json"
USER_MARKER = "<|im_start|>user\n"
ASSISTANT_SUFFIX = "<|im_end|>\n<|im_start|>assistant\n"
MAX_FILE_BYTES = 2 * 1024 * 1024
OUTPUT_FILES = ("report.json", "attempts.jsonl")


def sha256(body):
    return hashlib.sha256(body).hexdigest()


def checked_source(root, entry):
    relative = Path(entry["path"])
    require(not relative.is_absolute() and ".." not in relative.parts
            and relative.parts[0] == "benchmark", "Unexpected historical source path")
    path = Path(root) / relative
    require(path.is_file() and not path.is_symlink(), "Historical source is not a regular file")
    size = path.stat().st_size
    require(0 < size == entry["bytes"] <= MAX_FILE_BYTES, "Historical source size mismatch")
    body = path.read_bytes()
    digest = hashlib.sha1(("blob " + str(size) + "\0").encode("ascii") + body).hexdigest()
    require(digest == entry["git_blob_sha1"], "Pinned historical source blob mismatch")
    return body


def reference_documents(body, identity):
    require(len(body) == identity["bytes"] and sha256(body) == identity["sha256"],
            "Reference bytes differ from the accepted catalog")
    documents = [parse_json(line) for line in body.splitlines() if line.strip()]
    index = index_documents(documents)
    require(len(index) == identity["documents"], "Reference document count differs")
    require(identity["hypothesis_rows"] == 17 * len(index), "Reference hypothesis count differs")
    for doc in documents:
        require(isinstance(doc.get("text"), str) and doc["text"].strip(),
                "Reference text is absent")
        require(doc.get("origin") == "contractnli"
                and doc.get("text_sha256") == sha256(doc["text"].encode("utf-8")),
                "Reference text identity differs")
        validate_hypotheses(doc.get("inference"))
        require(all(item["hypothesis"] == HYPOTHESES[item["hypothesis_id"]]
                    for item in doc["inference"]), "Reference hypothesis wording differs")
    require(semantic_sha256(documents) == identity["semantic_sha256"],
            "Reference semantic identity differs")
    return index


def build_text_index(references):
    """Group exact-text aliases without choosing between different gold annotations."""
    grouped = {}
    for key, doc in references.items():
        by_id = {item["hypothesis_id"]: item for item in doc["inference"]}
        annotation = [
            {field: by_id[hid][field] for field in (
                "hypothesis_id", "hypothesis", "label", "source_clause"
            )}
            for hid in HYPOTHESES
        ]
        group = grouped.setdefault(doc["text"], {"ids": [], "signatures": set()})
        group["ids"].append(key)
        group["signatures"].add(semantic_sha256(annotation))
    return {
        text: {
            "reference_document_id": sorted(group["ids"])[0],
            "reference_document_ids": sorted(group["ids"]),
            "reference_text_sha256": sha256(text.encode("utf-8")),
            "equivalent_annotations": len(group["signatures"]) == 1,
        }
        for text, group in grouped.items()
    }


def parse_log(body):
    require(0 < len(body) <= MAX_FILE_BYTES, "Historical log exceeds limits")
    attempts, current = [], None
    for line in body.decode("utf-8").splitlines():
        if not line.strip():
            continue
        if line.startswith("timestamp: "):
            if current is not None:
                require("input_json" in current, "Historical request has no input")
                attempts.append(current)
            value = line[len("timestamp: "):]
            require(re.fullmatch(r"[0-9]{13}", value), "Invalid historical timestamp")
            current = {"timestamp_ms": int(value), "output_json": None}
        elif line.startswith("input: "):
            require(current is not None and "input_json" not in current,
                    "Unexpected or duplicate historical input")
            current["input_json"] = line[len("input: "):]
        elif line.startswith("output:"):
            require(current is not None and "input_json" in current
                    and not current.get("output_marker"), "Unexpected or duplicate historical output")
            current["output_json"] = line[len("output:"):].lstrip(" ")
            current["output_marker"] = True
        else:
            raise ValueError("Unsupported historical log line")
    require(current is not None and "input_json" in current, "Historical log has no request")
    attempts.append(current)
    require(len(attempts) <= 100, "Historical log has too many requests")
    return attempts


def request_identity(raw_input, text_index):
    request = parse_json(raw_input)
    require(isinstance(request, dict), "Historical request must be JSON")
    rendered, parameters = request.get("inputs"), request.get("parameters")
    require(isinstance(rendered, str) and isinstance(parameters, dict),
            "Missing historical prompt or parameters")
    require(rendered.count(USER_MARKER) == 1 and rendered.endswith(ASSISTANT_SUFFIX),
            "Unsupported historical chat-template boundary")
    prefix, user = rendered.split(USER_MARKER)
    contract = user[:-len(ASSISTANT_SUFFIX)]
    require(contract in text_index, "Historical contract has no exact held-out reference match")
    match = text_index[contract]
    require(
        match["equivalent_annotations"] is True,
        "Conflicting reference annotations for document IDs: "
        + ", ".join(match["reference_document_ids"]),
    )
    return {
        "reference_document_id": match["reference_document_id"],
        "reference_document_ids": list(match["reference_document_ids"]),
        "reference_text_sha256": match["reference_text_sha256"],
        "equivalent_reference_aliases": len(match["reference_document_ids"]) > 1,
        "prompt_prefix_sha256": sha256(prefix.encode("utf-8")),
        "parameters_sha256": sha256(json_bytes(parameters)),
        "generation_settings": {
            name: parameters[name] for name in ("temperature", "top_p", "max_new_tokens")
            if name in parameters
        },
    }


def decode_prediction(raw):
    if raw is None or not raw.strip():
        return None, "missing_output"
    try:
        envelope = parse_json(raw)
        if not isinstance(envelope, dict) or not isinstance(envelope.get("generated_text"), str):
            return None, "invalid_output_envelope"
        generated = parse_json(envelope["generated_text"])
        if not isinstance(generated, dict) or set(generated) != {"Hypotheses"}:
            return None, "invalid_generated_envelope"
    except (ValueError, TypeError):
        return None, "invalid_output_json"
    try:
        items = validate_hypotheses(generated["Hypotheses"])
    except ValueError:
        return None, "invalid_hypothesis_schema"
    return items, "valid_output"


def summary_record(body, expected_requests):
    text = body.decode("utf-8")
    prefix = "Loading dataset from directory: prompts\n"
    require(text.startswith(prefix), "Unsupported benchmark summary format")
    summary = parse_json(text[len(prefix):])
    require(isinstance(summary, dict)
            and type(summary.get("totalRequests")) is int
            and summary["totalRequests"] == expected_requests,
            "Benchmark summary request count differs")
    failed = summary.get("failedRequests")
    require(type(failed) is int and 0 <= failed <= expected_requests,
            "Invalid benchmark summary failure count")
    return summary


def evaluate_view(attempts, references, provenance, *, first_per_document=False):
    selected, seen = [], set()
    for attempt in sorted(attempts, key=lambda row: (
        row["timestamp_ms"], row["source_file"], row["source_record"]
    )):
        canonical = attempt["reference_document_id"]
        if first_per_document and canonical in seen:
            continue
        selected.append(attempt)
        seen.add(canonical)
    refs, predictions = [], []
    for attempt in selected:
        key = attempt["reference_document_id"] if first_per_document else attempt["attempt_id"]
        refs.append({**references[attempt["reference_document_id"]], "document_id": key})
        if attempt["prediction_status"] != "missing_output":
            predictions.append({"document_id": key, "inference": attempt["inference"]})
    return evaluate_predictions(refs, predictions, provenance)


def evidence_audit(attempts, references):
    quoted, exact, wording_mismatches = 0, 0, 0
    for attempt in attempts:
        if attempt["prediction_status"] != "valid_output":
            continue
        text = references[attempt["reference_document_id"]]["text"]
        for hypothesis in attempt["inference"]:
            wording_mismatches += int(
                hypothesis["hypothesis"] != HYPOTHESES[hypothesis["hypothesis_id"]]
            )
            quote = hypothesis["source_clause"]
            if quote.strip():
                quoted += 1
                exact += int(quote in text)
    return {
        "nonempty_evidence_fields": quoted, "exact_contiguous_substrings": exact,
        "exact_contiguous_substring_rate": exact / quoted if quoted else None,
        "noncanonical_hypothesis_text_fields": wording_mismatches,
        "notes": (
            "Literal contiguous substring check, without whitespace normalization. "
            "A joined multi-span quote may fail this check. Label matching uses fixed "
            "hypothesis IDs; this audit does not alter label or ROUGE scores."
        ),
    }


def prepare_replay(root, reference_body, config, source_commit, output_dir):
    require(isinstance(source_commit, str) and re.fullmatch(r"[0-9a-f]{40}", source_commit),
            "An immutable evaluation source commit is required")
    output_dir = Path(output_dir)
    require(not output_dir.exists(), "Evaluation output directory must not exist")
    require(config.get("schema_version") == "contractnli-historical-replay-config-v1",
            "Unsupported replay configuration")
    require(isinstance(config.get("groups"), list) and config["groups"],
            "At least one benchmark group is required")
    require(isinstance(config.get("historical_source_commit"), str)
            and re.fullmatch(r"[0-9a-f]{40}", config["historical_source_commit"]),
            "An immutable historical source commit is required")
    references = reference_documents(reference_body, config["reference"])
    text_index = build_text_index(references)
    all_attempts, groups, sources = [], {}, {}
    for group in config["groups"]:
        require(type(group.get("requests")) is int and 0 < group["requests"] <= 100,
                "Invalid expected benchmark request count")
        name = group["name"]
        require(isinstance(name, str) and re.fullmatch(r"[a-z0-9_]+", name)
                and name not in groups, "Invalid or duplicate benchmark group")
        group_attempts, summary = [], None
        require(len({entry["path"] for entry in group["files"]}) == len(group["files"]),
                "Duplicate pinned historical file")
        for entry in group["files"]:
            body = checked_source(root, entry)
            path = entry["path"]
            sources[path] = {**entry, "sha256": sha256(body)}
            if path == group["summary"]:
                require(summary is None, "Duplicate historical summary")
                summary = summary_record(body, group["requests"])
                continue
            for ordinal, raw in enumerate(parse_log(body), 1):
                identity = request_identity(raw["input_json"], text_index)
                inference, state = decode_prediction(raw["output_json"])
                group_attempts.append({
                    "attempt_id": name + "/" + Path(path).name + "/" + str(ordinal),
                    "group": name, "source_file": path, "source_record": ordinal,
                    "timestamp_ms": raw["timestamp_ms"], **identity,
                    "input_sha256": sha256(raw["input_json"].encode("utf-8")),
                    "raw_output_sha256": (
                        sha256(raw["output_json"].encode("utf-8"))
                        if raw["output_json"] is not None else None
                    ),
                    "prediction_status": state, "inference": inference,
                })
        require(summary is not None and len(group_attempts) == group["requests"],
                "Historical request accounting differs from summary")
        provenance = {
            "kind": "historical_predictions",
            "source": config["historical_repository"] + "/tree/"
                      + config["historical_source_commit"] + "/benchmark (" + name + ")",
            "model_id": None, "model_revision": None, "adapter_revision": None,
            "generation_prompt": "Pinned request prompt_prefix_sha256 per attempt",
            "new_cpu_prompt_used_for_generation": False,
        }
        request_metrics = evaluate_view(group_attempts, references, provenance)
        first_metrics = evaluate_view(group_attempts, references, provenance, first_per_document=True)
        settings = {}
        for attempt in group_attempts:
            key = (attempt["prompt_prefix_sha256"], attempt["parameters_sha256"])
            if key not in settings:
                settings[key] = {
                    "prompt_prefix_sha256": key[0], "parameters_sha256": key[1],
                    "generation_settings": attempt["generation_settings"], "request_count": 0,
                }
            settings[key]["request_count"] += 1
        matched_record_ids = sorted({
            key for attempt in group_attempts for key in attempt["reference_document_ids"]
        })
        groups[name] = {
            "recorded_summary": summary,
            "observed_request_records": len(group_attempts),
            "unique_reference_documents": first_metrics["attempted_documents"],
            "unique_input_contracts": first_metrics["attempted_documents"],
            "matched_reference_records": len(matched_record_ids),
            "matched_reference_record_ids": matched_record_ids,
            "reference_subset_fraction": len(matched_record_ids) / len(references),
            "repeated_requests": len(group_attempts) - first_metrics["attempted_documents"],
            "request_level": request_metrics, "unique_documents_first_attempt": first_metrics,
            "prompt_and_settings": list(settings.values()),
            "evidence_audit": evidence_audit(group_attempts, references),
            "failure_accounting": {
                "recorded_transport_failures": summary["failedRequests"],
                "empty_or_absent_outputs": request_metrics["missing_documents"],
                "invalid_nonempty_outputs": request_metrics["invalid_documents"],
                "notes": "Recorded transport failures and schema/empty-output counts are distinct.",
            },
        }
        all_attempts.extend(group_attempts)
    report = {
        "schema_version": "contractnli-historical-replay-v1",
        "operation": "evaluate_historical_benchmark_predictions",
        "live_inference": False, "evaluation_source_commit": source_commit,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "historical_repository": config["historical_repository"],
        "historical_source_commit": config["historical_source_commit"],
        "reference": config["reference"], "reference_dataset_documents": len(references),
        "reference_unique_texts": len(text_index),
        "duplicate_reference_text_groups": [
            {
                "document_ids": match["reference_document_ids"],
                "text_sha256": match["reference_text_sha256"],
                "equivalent_annotations": match["equivalent_annotations"],
            }
            for match in sorted(text_index.values(), key=lambda item: item["reference_document_id"])
            if len(match["reference_document_ids"]) > 1
        ],
        "reference_resolution_policy": (
            "Exact-text duplicates are usable only when all 17 scoring annotations match. "
            "The lexicographically smallest ID names the equivalent target; every alias ID "
            "is retained. A requested group with conflicting annotations is rejected."
        ),
        "scope": "Historical benchmark subset; request and deterministic first-attempt metrics",
        "metric_implementation": {
            "rouge_score_version": version("rouge-score"), "ngram": 3, "use_stemmer": False,
        },
        "source_files": sources, "groups": groups,
        "limitations": config["limitations"],
        "first_attempt_policy": (
            "Earliest recorded timestamp per reference document within each benchmark "
            "group; ties use source-file path then record ordinal. No best-response selection."
        ),
        "denominator_policy": (
            "Request metrics include every logged benchmark request, including repeated "
            "contracts. First-attempt metrics include each requested contract once. Neither "
            "view treats unrequested held-out documents as attempted. Equivalent exact-text "
            "reference aliases form one input-contract target; covered reference-record "
            "counts and all alias IDs are reported separately. Missing/invalid responses "
            "count as wrong in all-attempts label accuracy."
        ),
    }
    attempt_bytes = "".join(
        json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                   allow_nan=False) + "\n" for row in all_attempts
    ).encode("utf-8")
    # Use compact JSONL records; no reference text or full input prompts are copied.
    # Quotes in valid historical predictions remain private artifact content.
    output_dir.mkdir(parents=True, exist_ok=False)
    (output_dir / "attempts.jsonl").write_bytes(attempt_bytes)
    (output_dir / "report.json").write_bytes(json_bytes(report))
    return report
