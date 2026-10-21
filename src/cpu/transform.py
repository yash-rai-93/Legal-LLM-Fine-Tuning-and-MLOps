"""Deterministic ContractNLI processing without MLRun or an AWS connection."""

import hashlib
import json

from .hypotheses import HYPOTHESES, LABELS
from .validate import document_id, validate_hypotheses

REQUIRED_COLUMNS = {
    "document_id", "text", "hypothesis_id", "hypothesis", "label", "evidence_texts"
}


def semantic_sha256(value):
    encoded = json.dumps(
        value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def transform_rows(rows):
    """Group row-per-hypothesis input into validated document records.

    Original text is preserved. Output order and IDs are stable; timestamps and
    per-run UUIDs belong in the run manifest, not the semantic content hash.
    """
    documents = {}
    row_count = 0
    for row in rows:
        row_count += 1
        if not isinstance(row, dict) or not REQUIRED_COLUMNS.issubset(row):
            raise ValueError(f"Required columns missing at row {row_count}")
        key = document_id(row["document_id"])
        text = row["text"]
        if not isinstance(text, str) or not text.strip():
            raise ValueError(f"Invalid contract text for document {key}")
        hid = row["hypothesis_id"]
        if not isinstance(hid, str) or hid not in HYPOTHESES:
            raise ValueError(f"Unknown hypothesis ID in document {key}")
        if row["hypothesis"] != HYPOTHESES[hid]:
            raise ValueError(f"Hypothesis definition differs for {hid} in document {key}")
        raw_label = row["label"]
        if not isinstance(raw_label, str):
            raise ValueError(f"Invalid label for {hid} in document {key}")
        label = raw_label.strip().lower()
        if label == "notmentioned":
            label = "not_mentioned"
        if label not in LABELS:
            raise ValueError(f"Invalid label for {hid} in document {key}")
        evidence = row["evidence_texts"]
        if hasattr(evidence, "tolist"):
            evidence = evidence.tolist()
        if not isinstance(evidence, (list, tuple)) or any(
            not isinstance(part, str) for part in evidence
        ):
            raise ValueError(f"Invalid evidence array for {hid} in document {key}")
        source_clause = ". ".join(evidence)
        doc = documents.setdefault(key, {"text": text, "hypotheses": {}})
        if doc["text"] != text:
            raise ValueError(f"Conflicting contract text for document {key}")
        if hid in doc["hypotheses"]:
            raise ValueError(f"Duplicate {hid} in document {key}")
        doc["hypotheses"][hid] = {
            "hypothesis_id": hid,
            "hypothesis": HYPOTHESES[hid],
            "label": label,
            "source_clause": source_clause,
        }
    if not documents:
        raise ValueError("Input dataset is empty")
    output = []
    for key in sorted(documents):
        doc = documents[key]
        if set(doc["hypotheses"]) != set(HYPOTHESES):
            raise ValueError(f"Document {key} does not contain all 17 hypotheses")
        hypotheses = [doc["hypotheses"][hid] for hid in HYPOTHESES]
        validate_hypotheses(hypotheses)
        output.append({
            "document_id": key,
            "text": doc["text"],
            "text_sha256": hashlib.sha256(doc["text"].encode("utf-8")).hexdigest(),
            "inference": hypotheses,
            "origin": "contractnli",
        })
    return output, {"input_rows": row_count, "documents": len(output)}


def validate_split_disjointness(splits):
    """Reject overlap in document IDs or exact contract text between splits."""
    id_owner = {}
    text_owner = {}
    for split, documents in splits.items():
        for doc in documents:
            key = document_id(doc["document_id"])
            digest = hashlib.sha256(doc["text"].encode("utf-8")).hexdigest()
            for token, owners, kind in (
                (key, id_owner, "document_id"), (digest, text_owner, "contract text")
            ):
                if token in owners and owners[token] != split:
                    raise ValueError(f"Overlapping {kind} between {owners[token]} and {split}")
                owners[token] = split
