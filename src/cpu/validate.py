"""Validation for complete document-level predictions and references."""

from .hypotheses import HYPOTHESES, LABELS


def validate_hypotheses(items):
    """Require all 17 unique IDs, valid labels and string evidence.

    Prediction wording need not exactly match the canonical text; IDs identify
    the classification task. Reference/data ingestion validates the wording.
    """
    if not isinstance(items, list) or len(items) != len(HYPOTHESES):
        raise ValueError("Expected a list of exactly 17 hypotheses")
    seen = set()
    for item in items:
        if not isinstance(item, dict):
            raise ValueError("Every hypothesis must be an object")
        hid = item.get("hypothesis_id")
        if not isinstance(hid, str) or hid not in HYPOTHESES:
            raise ValueError("Unknown hypothesis ID")
        if hid in seen:
            raise ValueError(f"Duplicate hypothesis ID: {hid}")
        seen.add(hid)
        label = item.get("label")
        if not isinstance(label, str) or label not in LABELS:
            raise ValueError(f"Invalid label for {hid}")
        if not isinstance(item.get("hypothesis"), str) or not item["hypothesis"].strip():
            raise ValueError(f"Missing hypothesis text for {hid}")
        evidence = item.get("source_clause")
        if not isinstance(evidence, str):
            raise ValueError(f"Evidence must be a string for {hid}")
        if label == "not_mentioned" and evidence.strip():
            raise ValueError(f"not_mentioned must have empty evidence: {hid}")
        if label != "not_mentioned" and not evidence.strip():
            raise ValueError(f"A supported label needs evidence: {hid}")
    return items


def document_id(value):
    """Normalize numeric/string IDs while rejecting missing IDs and booleans."""
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise ValueError("document_id must be a nonempty string or integer")
    normalized = str(value).strip()
    if not normalized:
        raise ValueError("document_id is empty")
    return normalized


def index_documents(records):
    """Return an ID index; reject duplicate documents rather than overwrite."""
    result = {}
    for record in records:
        if not isinstance(record, dict):
            raise ValueError("Every document must be an object")
        key = document_id(record.get("document_id"))
        if key in result:
            raise ValueError(f"Duplicate document_id: {key}")
        result[key] = record
    return result
