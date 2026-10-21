"""CLI for a finite cloud CPU job; no SageMaker/MLRun/model invocation.

    python -m src.cpu.prepare --input-dir ... --output-dir ... --source-commit ...
"""

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import tempfile

from .hypotheses import SCHEMA_VERSION
from .transform import semantic_sha256, transform_rows, validate_split_disjointness


def prepare(input_dir, output_dir, source_commit, splits=("train", "validation")):
    # Import Arrow only in the batch job, never in thin API handlers.
    import pyarrow.parquet as pq

    input_dir, output_dir = Path(input_dir), Path(output_dir)
    expected = tuple(splits)
    if not expected or len(set(expected)) != len(expected) or any(
        split not in {"train", "validation", "test"} for split in expected
    ):
        raise ValueError("Choose unique split names from train, validation, test")
    if not source_commit or not source_commit.strip():
        raise ValueError("A source commit must be recorded")
    if output_dir.exists() and any(output_dir.iterdir()):
        raise ValueError("Output directory must be empty; do not overwrite an earlier run")
    inputs, processed = {}, {}
    for split in expected:
        path = input_dir / f"{split}.parquet"
        raw_bytes = path.read_bytes()
        records, counts = transform_rows(pq.read_table(path).to_pylist())
        processed[split] = records
        inputs[split] = {
            **counts,
            "input_sha256": hashlib.sha256(raw_bytes).hexdigest(),
            "semantic_sha256": semantic_sha256(records),
        }
    validate_split_disjointness(processed)
    # Validate every split before publishing any completed run output.
    output_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="contractnli-cpu-") as staging:
        staged = Path(staging)
        for split, records in processed.items():
            payload = "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in records)
            (staged / f"{split}.jsonl").write_text(payload, encoding="utf-8")
            inputs[split]["output_sha256"] = hashlib.sha256(payload.encode("utf-8")).hexdigest()
        manifest = {
            "schema_version": SCHEMA_VERSION,
            "operation": "data_preparation",
            "live_inference": False,
            "source_commit": source_commit,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "dataset_source": "https://stanfordnlp.github.io/contract-nli/",
            "dataset_license": "CC BY 4.0",
            "splits": inputs,
        }
        for path in staged.iterdir():
            (output_dir / path.name).write_bytes(path.read_bytes())
        # Manifest is the completion marker; consumers require it before registering a run.
        (output_dir / "manifest.json").write_text(
            json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
        )
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-dir", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--source-commit", required=True)
    parser.add_argument("--splits", nargs="+", default=["train", "validation"])
    args = parser.parse_args()
    manifest = prepare(args.input_dir, args.output_dir, args.source_commit, args.splits)
    print(json.dumps(manifest, indent=2))  # Metadata only; never contracts or credentials.


if __name__ == "__main__":
    main()
