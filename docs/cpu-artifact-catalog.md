# Dataset and prompt catalog: verified in AWS

This manual CPU-only stage recovers the official held-out split and publishes
an S3 catalog for train, validation, test and a canonical prompt/schema.
No new AWS resources or permissions are required. Tests and processing run
in AWS CodeBuild. CloudShell is used only for administration.

## Verified cloud evidence

Verified from account-owner-supplied AWS build status and catalog output on
22 October 2026 (IST). No dataset processing or tests ran on the laptop.

- Application source: `056f8a4269d12d1bf321660ab1e76466aa6fecd9`.
- Build: `legal-llm-cpu-check:51eed2ff-576e-4bf4-b1d1-47203f7092a8`,
  SUCCEEDED / COMPLETED, with the catalog-check override.
- The build ran 32 CPU and 28 cloud tests before recovery/publication.
- Catalog: `runs/51eed2ff-576e-4bf4-b1d1-47203f7092a8/catalog/catalog.json`
  in the existing private bucket.
- Created at (machine UTC): `2026-10-22T13:22:56.187571+00:00`.
- Status: ARTIFACTS_VERIFIED; required build status: SUCCEEDED, confirmed separately.
- Registration scope: versioned_s3_manifest; live_inference=false.
- Upstream commit: `eced6528dd3c1d14d73f9a87df8f7bdbc03126f9`.
- Archive SHA-256: `e03fc77bbf8b53e2976a250e81d8a294bc3d5e5fb014521e477dee9340d6287b`.

| Split | Documents | Hypothesis rows |
| --- | ---: | ---: |
| Train | 423 | 7191 |
| Validation | 61 | 1037 |
| Held-out test | 123 | 2091 |

All four preparation markers are true: expected counts, canonical hypotheses,
split ID/exact-text disjointness, and tracked train/validation semantic match.
Train/validation output hashes also match the earlier deployed processor.
All nine catalog artifact references carry non-null S3 versions, AES256,
positive byte sizes and SHA-256 checksums verified by the publication build.

- Test output SHA-256: `ea4d22100296f8eb44025ddff99eb423cdff700954911aeef64d692f490def83`.
- Test object version: `17oHva47lK5vvPGNkIjvCe8wbhV8LAfk`.
- Prompt SHA-256: `3e62924801de6745550dc07360c8f90f9991af9e0a2d20cbc9a05bedec9459f1`.
- Prompt object version: `Qxpiveh9kjnM86wr8lb3olk3yewFzz9E`.
- The prompt identity digest equals the prompt artifact's digest; model_binding=null.

This verifies source recovery, CPU checks and S3 artifact registration.
It does not demonstrate model evaluation, prompt quality or new predictions.
The catalog is not yet exposed through DynamoDB or the deployed status API.
Full dependency locking, infrastructure ownership, operational recovery and
catalog/API integration remain future work. Billing review was skipped at the
account owner's request; no new consumption measurement is claimed.

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
same AWS build. The successful AWS build below ran these tests before real recovery/publication.

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
