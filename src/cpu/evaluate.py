"""Evaluate supplied predictions; this module never generates predictions."""

from rouge_score import rouge_scorer

from .hypotheses import HYPOTHESES, SCHEMA_VERSION
from .validate import index_documents, validate_hypotheses


def evaluate_predictions(references, predictions, provenance):
    """Score complete outputs and explicitly account for missing/invalid ones.

    Each record has document_id and inference (17 objects). Provenance must
    identify the source and whether this is a fixture or a previous model run.
    Invalid predictions get zero label accuracy in the all-attempts aggregate;
    evidence overlap is reported only for scored documents, with coverage.
    """
    if not isinstance(provenance, dict) or provenance.get("kind") not in {
        "fixture", "historical_predictions", "model_run"
    }:
        raise ValueError("Explicit prediction provenance is required")
    if not isinstance(provenance.get("source"), str) or not provenance["source"].strip():
        raise ValueError("Prediction provenance needs a nonempty source")
    if provenance["kind"] == "model_run":
        for field in ("model_id", "model_revision", "prompt_version"):
            if not isinstance(provenance.get(field), str) or not provenance[field].strip():
                raise ValueError(f"model_run provenance needs {field}")
    ref_index = index_documents(references)
    pred_index = index_documents(predictions)
    if not ref_index:
        raise ValueError("Reference dataset is empty")
    if set(pred_index) - set(ref_index):
        raise ValueError("Predictions contain document IDs outside the reference set")
    scorer = rouge_scorer.RougeScorer(["rouge3"], use_stemmer=False)
    results, correct, scored, missing, invalid = [], 0, 0, 0, 0
    evidence_scores = []
    for key, reference in ref_index.items():
        validate_hypotheses(reference.get("inference"))
        expected = {h["hypothesis_id"]: h for h in reference["inference"]}
        if key not in pred_index:
            missing += 1
            results.append({"document_id": key, "status": "missing"})
            continue
        try:
            items = validate_hypotheses(pred_index[key].get("inference"))
        except ValueError as exc:
            invalid += 1
            results.append({"document_id": key, "status": "invalid", "reason": str(exc)})
            continue
        document_correct, overlaps = 0, []
        for prediction in items:
            truth = expected[prediction["hypothesis_id"]]
            matches = truth["label"] == prediction["label"]
            document_correct += int(matches)
            if matches and truth["label"] == "not_mentioned":
                overlap = 1.0  # Historical project convention, not exact-span accuracy.
            else:
                overlap = scorer.score(
                    truth["source_clause"], prediction["source_clause"]
                )["rouge3"].fmeasure
            overlaps.append(overlap)
        correct += document_correct
        scored += 1
        evidence_scores.extend(overlaps)
        results.append({
            "document_id": key,
            "status": "scored",
            "label_accuracy": document_correct / len(HYPOTHESES),
            "evidence_rouge3_fmeasure": sum(overlaps) / len(overlaps),
        })
    attempted = len(ref_index)
    return {
        "schema_version": SCHEMA_VERSION,
        "operation": "evaluate_supplied_predictions",
        "live_inference": False,
        "provenance": dict(provenance),
        "attempted_documents": attempted,
        "scored_documents": scored,
        "missing_documents": missing,
        "invalid_documents": invalid,
        "coverage": scored / attempted,
        "label_accuracy_all_attempts": correct / (attempted * len(HYPOTHESES)),
        "label_accuracy_scored_only": correct / (scored * len(HYPOTHESES)) if scored else None,
        "evidence_rouge3_fmeasure_scored_only": (
            sum(evidence_scores) / len(evidence_scores) if evidence_scores else None
        ),
        "metric_notes": (
            "Invalid/missing outputs count as incorrect in all-attempts label accuracy. "
            "Evidence overlap excludes invalid/missing documents. Correct not_mentioned "
            "receives evidence score 1.0. ROUGE is not legal or exact-span correctness."
        ),
        "documents": results,
    }
