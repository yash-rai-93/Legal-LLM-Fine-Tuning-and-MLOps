# Dataset and prompt catalog: source prepared, AWS verification pending

This manual CPU-only stage recovers the official held-out split and publishes
an S3 catalog for train, validation, test and a canonical prompt/schema.
No new AWS resources or permissions are required. Tests and processing run
in AWS CodeBuild. CloudShell is used only for administration.

## Dataset source and reproducibility

The official [ContractNLI site](https://stanfordnlp.github.io/contract-nli/)
describes the JSON format, fixed hypotheses, annotation spans and CC BY 4.0
license. Its download is a ZIP hosted in the official Stanford repository.

The reviewed source is pinned in `config/contractnli-source.json`:

- Repository: `stanfordnlp/contract-nli`.
- Commit: `eced6528dd3c1d14d73f9a87df8f7bdbc03126f9`.
- File: `resources/contract-nli.zip`; 65,362,913 bytes.
- Git blob SHA-1: `757fd1dafd29a997fba00c60c6d40b2930a36159`.
- Expected documents: train 423, validation 61, test 123; 17 hypotheses each.
- Official `dev.json` is named validation here. No split is resampled.

The build fetches the immutable raw GitHub URL, enforces the exact archive size
and verifies the Git blob identity (including its Git blob header). It also
records a SHA-256 of the downloaded archive and each selected source JSON.
The archive contains PDFs; none are extracted or processed. Only the unique
train/dev/test JSON members are opened, each limited to 32 MiB. Network reads
use a 30-second socket timeout and the build has an independent 15-minute limit.

Preparation rejects duplicate JSON keys, missing/unknown hypotheses, incorrect
definitions, duplicate document IDs, unsupported annotation sets, invalid labels,
out-of-bounds offsets/indices and inconsistent evidence. Original document text
is preserved, labels are normalized, evidence is sliced using official offsets,
and all 17 hypotheses remain present.

All three splits must be disjoint by document ID and exact text hash. This does
not establish absence of near-duplicate contracts. The recovered train and
validation semantic hashes must match both the tracked Parquet files and the
previously approved CPU baseline. Any difference fails the build before output
publication; do not silently weaken this check to make a build pass.

The ignored `test.*` Git pattern is not bypassed. Recovery happens in the
ephemeral AWS build and outputs are retained in private S3, rather than committing
large generated data files. Source JSON and attribution are included for audit.

## Prompt artifact

`src/cpu/prompt.py` builds a deterministic JSON prompt artifact from the same
canonical hypothesis definitions used by the CPU validators. It explicitly asks
for a JSON object with one `Hypotheses` array, matching the response schema.
The old notebook's array/object mismatch is not carried into this artifact.

The prompt has no model binding, tokenizer dependency or embedded contract.
The user-message slot is `{contract}`. Its SHA-256 identifies its exact content.
The schema restricts fields, labels, IDs and array length. ID uniqueness,
ID/text correspondence and verbatim evidence need post-generation checks;
JSON Schema alone does not prove them. This job registers an artifact, not a
demonstrated model prompt or any new model predictions.

## Cloud build

Use one manual build with `buildspecs/catalog-check.yml` as an override and an
immutable feature commit. Keep the existing project's default buildspec, source
pin, environment and deployed workflow unchanged.

The job installs the existing CPU requirements and runs:

1. 32 CPU tests: the existing 15 plus 17 catalog/prompt/publication tests.
2. The existing 28 registry/status-API unit tests.
3. Official-source recovery, validation and baseline comparison.
4. Versioned S3 publication with byte verification.

The new fixtures test known evidence offsets, corrupt source data, missing or
ambiguous ZIP members, pinned archive size/blob identity, all-split preparation,
split leakage, baseline mismatches, deterministic prompt/schema, modified local
outputs, wrong destinations, failed S3 read-back and repeated writes. Fake AWS
clients are unit-test fixtures; real publication is checked separately by the
same AWS build. These new tests have not yet run in AWS.

## Publication and completion rules

Outputs use only the existing bucket's `runs/<build-UUID>/catalog/` prefix:

- `source-train.json`, `source-validation.json`, `source-test.json`.
- `train.jsonl`, `validation.jsonl`, `test.jsonl`.
- `prompt.json`, `attribution.json`, `preparation.json`.
- `catalog.json`, published last after the other nine objects are verified.

Each write requests AES256 encryption and uses If-None-Match `*` to refuse
existing keys. Each read-back must match the returned upload VersionId,
encryption and SHA-256. The build role already has current-object GetObject;
it does not need GetObjectVersion, ListBucket or wider S3 access for this job.

The final catalog contains object keys, versions, sizes, hashes, source/code
provenance, split counts, validation markers and the prompt digest.
Its status is ARTIFACTS_VERIFIED and it explicitly requires the exact CodeBuild
job to have SUCCEEDED. An object from a failed/incomplete build is not an
accepted registration, even if a partial prefix or catalog exists.

This stage's registration scope is `versioned_s3_manifest`. It does not insert
a DynamoDB run or change the deployed GET API, which remains pinned to the
original two-split processor. Dataset/prompt lookup endpoints are a future stage.

## Acceptance after the build

Require the build to report SUCCEEDED / COMPLETED before retrieving its
`runs/<build-UUID>/catalog/catalog.json`. Check:

- Train 423 / 7191 rows, validation 61 / 1037, test 123 / 2091.
- All preparation checks are true.
- Immutable application/upstream commits and archive hashes are present.
- All nine artifact references have non-null versions, AES256 and SHA-256.
- Prompt digest equals the prompt artifact's digest.
- The existing workflow and status API were not updated by the job.

If download, official/tracked equivalence or any test fails, inspect that error
before starting another build. No latest-pointer promotion, workflow update or
new Lambda deployment is part of this stage. Partial S3 versions may remain
after failure; cleanup is a separate, explicitly scoped action.

## Attribution

Dataset: Yuta Koreeda and Christopher D. Manning (2021),
[ContractNLI: A Dataset for Document-level Natural Language Inference for Contracts](https://aclanthology.org/2021.findings-emnlp.164/).
[CC BY 4.0](https://creativecommons.org/licenses/by/4.0/).
The generated attribution artifact records normalization/format changes.
