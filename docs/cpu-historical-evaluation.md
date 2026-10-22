# Historical CPU evaluation replay: source prepared, AWS check pending

This stage rescores existing model responses saved in the original repository.
It generates no predictions and calls no model endpoint. It is a historical
benchmark replay with incomplete model provenance, not a new model run or a
controlled fine-tuning comparison.

## Available source evidence

The reviewed repository contains input/output request records in:

- `benchmark/4_client_test_output/output.txt.0` through `.3`.
- `benchmark/7_client_test_output/output.txt.0` through `.6`.
- The corresponding 4-client and 7-client benchmark summary files.

The source files are present unchanged at original commit
`de13969e5624cfb3862762986c8197ca348924e2`. Their expected byte sizes and Git
blob identities are pinned in `config/historical-replay.json`. The replay
also records SHA-256 hashes of the actual checked-out bytes.

The summaries record 40 and 35 total requests. The logs include repeated
contracts and empty responses. Prompt text and generation parameters are
retained in each request. Immutable model/adapter revisions are not established
by these files. A recorded tokenizer name is not proof of the generating
model. The current benchmark shell script's endpoint is not assigned to
earlier logs as if it were their verified configuration.

The full historical 119/121-document prediction artifacts behind the README's
reported aggregate metrics are not tracked in this repository. This stage
does not claim to reconstruct those runs or their metrics.

## Reference artifact and identity

Use the accepted catalog's held-out test data:

- Producer build: `legal-llm-cpu-check:51eed2ff-576e-4bf4-b1d1-47203f7092a8`,
  previously confirmed SUCCEEDED / COMPLETED.
- Producer source: `056f8a4269d12d1bf321660ab1e76466aa6fecd9`.
- Key: `runs/51eed2ff-576e-4bf4-b1d1-47203f7092a8/catalog/test.jsonl`.
- Version: `17oHva47lK5vvPGNkIjvCe8wbhV8LAfk`.
- SHA-256: `ea4d22100296f8eb44025ddff99eb423cdff700954911aeef64d692f490def83`.
- Size: 2,391,518 bytes; 123 documents / 2091 hypotheses.

The CodeBuild role already has current-object reads, but not GetObjectVersion.
The job checks HEAD metadata, downloads the current object, then requires its
returned version, AES256 encryption, size and checksum to equal the accepted
identity. A changed current version causes failure; it does not silently fall
back to another reference. Semantic hash, document counts, canonical hypotheses,
text hashes and unique IDs/text are checked again.

The previously accepted catalog build status is evidence recorded in the
reviewed configuration. This job does not add CodeBuild inspection permissions
to the execution role or query a producer build dynamically.

## Matching and response handling

Each saved input must use the reviewed chat-template boundaries. Its contract
must match exactly one held-out reference text, with no stripping, fuzzy match
or document-order guessing. Unknown inputs, changed source bytes, malformed
log structure or request-count disagreement fail the entire job before
publication.

An absent/empty output remains a missing prediction. A nonempty malformed JSON
response, wrong envelope or invalid hypothesis schema remains an invalid
prediction. Truncated outputs are not repaired, hypotheses are not completed
from labels, and valid later responses never replace an earlier failed attempt
in the first-attempt view.

The historical system-prompt prefix and full generation parameters are hashed
per request. The newly registered CPU prompt was not used to generate these
responses; its identity must not be assigned to this historical inference.

## Metrics and denominators

Reports keep the two benchmark groups separate and provide both views:

| View | Denominator and selection |
| --- | --- |
| Request-level | Every saved benchmark request, including repeated contracts |
| First attempt per requested document | Earliest timestamp for each contract within that group; ties use source path then record ordinal |

Each view uses the existing CPU evaluator. Missing/invalid outputs contribute
zero correct labels to all-attempts accuracy. Scored-only accuracy and
ROUGE-3 evidence overlap have their own denominators and coverage. Correct
not_mentioned labels retain the existing evidence score of 1.0; evidence
overlap is not a measure of legal correctness.

Each group reports the number/fraction of the 123 held-out documents actually
requested. Neither view is a full-held-out accuracy claim. Recorded transport
failure counts are shown separately from empty/schema-invalid output counts.
Timing/throughput values are retained as historical summary metadata; no
current latency or throughput measurement is performed.

A separate audit counts nonempty evidence fields that occur as exact contiguous
substrings in the original contract, without whitespace normalization. Joined
multi-span quotes may fail that strict check. Canonical hypothesis-text
mismatches are also counted; label matching uses the fixed IDs. These audits
do not alter the original label or ROUGE metric rules.

## AWS build and outputs

Run one manual override build with `buildspecs/evaluation-check.yml` and an
immutable feature commit. It installs only the existing CPU requirements,
runs 52 CPU tests (32 existing plus 20 replay tests) and the existing 28 cloud
tests, then evaluates the historical logs and publishes verified S3 artifacts.

The new fixtures test source pins, request/response parsing, exact contract
mapping, reference identity, known accuracy denominators, deterministic first
attempt selection, malformed/missing output accounting, evidence audit,
unknown model/prompt provenance, S3 races/read-back failures, wrong destinations
and refusal to overwrite existing keys. They are synthetic unit fixtures and
have not yet run in AWS. Real historical replay metrics remain unverified.

Outputs use `runs/<evaluation-build-UUID>/evaluation/`:

- `report.json`: provenance, metrics, coverage, accounting and limitations.
- `attempts.jsonl`: normalized historical predictions and attempt/reference IDs.
- `evaluation.json`: final S3 artifact manifest, published last.

Report logs contain summary metadata only. Full contracts/input prompts are
not copied to CloudWatch. The private attempts artifact includes historical
predicted evidence quotes, rather than fabricated new predictions.

The existing conditional S3 publisher requires non-null versions, AES256 and
byte-identical read-back. The final manifest requires the exact evaluation
build to have SUCCEEDED; partial/failed builds are not completed evaluations.
Existing scoped runs/* permissions suffice. No Lambda, DynamoDB, Step Functions,
endpoint, GPU or infrastructure update is part of this build.

## Acceptance

Require SUCCEEDED / COMPLETED, then retrieve evaluation.json and report.json.
Confirm request counts against the pinned summaries, subset coverage, all
missing/invalid counts, both metric denominators, unknown-model limitations
and versioned output references. Do not prefill expected accuracy from the
historical README. If source matching/accounting fails, inspect it before
rerunning; do not skip mismatched contracts to manufacture a passing score.

Dataset/prompt/evaluation API integration, bounded submission, complete
operational recovery and repeatable infrastructure remain later stages.
