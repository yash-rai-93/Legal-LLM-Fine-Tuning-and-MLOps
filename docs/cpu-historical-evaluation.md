# Historical CPU evaluation replay: verified in AWS

This stage rescores existing model responses saved in the original repository.
It generates no predictions and calls no model endpoint. It is a historical
benchmark replay with incomplete model provenance, not a new model run or a
controlled fine-tuning comparison.

## Verified AWS replay

Verified from account-owner-supplied AWS status and evaluation manifest on
22 October 2026 (IST):

- Build: `legal-llm-cpu-check:7a14af2a-39a3-42e7-8720-4b4005269d1a`,
  SUCCEEDED / COMPLETED.
- Evaluation source: `01e110a8999e6a333d8089635f22f0d3d3dc6ab9`.
- Tests before replay: 56 CPU and 28 cloud tests (84 total).
- Historical source: `de13969e5624cfb3862762986c8197ca348924e2`.
- Manifest: `runs/7a14af2a-39a3-42e7-8720-4b4005269d1a/evaluation/evaluation.json`.
- Created at (machine UTC): `2026-10-22T14:03:59.437374+00:00`.
- Status: ARTIFACTS_VERIFIED; required build status: SUCCEEDED, confirmed separately.
- Registration scope: versioned_s3_manifest; live_inference=false.

| Historical group | Requests | Scored | Invalid | Missing | Unique input contracts | Matched test records |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| 4-client | 40 | 26 | 12 | 2 | 10 | 10 |
| 7-client | 35 | 22 | 9 | 4 | 10 | 10 |

Each group matches 10/123 reference records (8.13%); their counts do not imply
20 distinct contracts across groups. Request accounting balances: scored +
invalid + missing equals 40 and 35. Recorded transport failures are 2 and 4,
separate from the additional schema/response-invalid outputs.

| Historical group | Request-level all-attempts label accuracy | First-attempt-per-contract all-attempts label accuracy |
| --- | ---: | ---: |
| 4-client | 51.47% | 48.24% |
| 7-client | 50.76% | 57.65% |

These percentages include missing/invalid outputs as zero correct labels.
They describe this strict replay of a small historical benchmark subset.
They are not newly generated model results, full-held-out scores, a controlled
comparison, or a reproduction of the earlier 77.43%/87.63% aggregates.
Unknown model/adapter revisions and historical prompt provenance remain
explicit in the manifest. Exact causes of invalid predictions and additional
scored-only/evidence statistics require inspecting the full report/attempt
artifacts; they are not inferred from this compact manifest.

Both published artifacts have AES256 encryption and byte-verified versions:

| Artifact | Bytes | S3 version |
| --- | ---: | --- |
| report.json | 34962 | `sTR5TQgWp6jRuuWPHjg.jEEoj0xDXd.K` |
| attempts.jsonl | 357020 | `LmMeJZcgkgh7ocfanyY6uikHYn_Xl4ZB` |

- Report SHA-256: `4cb4806b66bfa7f7487264a349375d1241c699753e9e3fde54c44ad843adb3da`.
- Attempts SHA-256: `b97419e7524685e2c3bad46753edce46474f87fabb22f619138978999481636f`.
- Reference test version/hash match the accepted catalog described below.

This verifies the finite CPU evaluation/reporting path and duplicate-reference
fix against real AWS artifacts. No model endpoint, deployed workflow, run
registry or API was changed. Evaluation lookup through the API remains pending.

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
text hashes and unique document IDs are checked again. Exact-text duplicates
inside the held-out split are retained and resolved by the policy below.

The previously accepted catalog build status is evidence recorded in the
reviewed configuration. This job does not add CodeBuild inspection permissions
to the execution role or query a producer build dynamically.

## Matching and response handling

Each saved input must use the reviewed chat-template boundaries. Its contract
must match the held-out reference text exactly, with no stripping, fuzzy match
or document-order guessing. Multiple IDs may carry the same official test text.
They form one scoring target only when all 17 hypothesis IDs, canonical texts,
labels and evidence strings agree. The lexicographically smallest ID names
that equivalent target; all original alias IDs are retained per attempt.
A requested exact-text group with conflicting reference annotations is rejected,
with candidate IDs in the diagnostic. An unrequested duplicate group does not
block otherwise identifiable requests.

Reports distinguish unique input contracts from matched reference records,
and list duplicate text groups and their annotation equivalence. The matched
record fraction includes equivalent aliases; it does not imply independent
requests for every alias. Duplicate document IDs remain invalid. Unknown inputs,
changed source bytes, malformed logs or request-count disagreement fail the
job before publication.

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
| First attempt per input contract | Earliest timestamp for each exact-text target within that benchmark group; equivalent reference aliases count once; ties use source path then record ordinal |

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

## First AWS run and reference-duplicate fix

Build `legal-llm-cpu-check:84a774e3-3a70-477d-9c6f-651236af5d01`
from source `5e0217a0de7375b68bca593720847211a65bd47d` passed
52 CPU and 28 cloud tests, then failed during real reference validation with
`ValueError: Reference exact text is ambiguous`. Supplied logs show the
exception occurred before report preparation/publication. The deployed CPU
workflow, run registry, status API and accepted S3 test data were unchanged.

That guard incorrectly required globally unique text among held-out records.
The fix preserves pinned bytes, versions, hashes, all source IDs and strict
gold-reference validation. Ambiguity is assessed only for requested exact-text
groups, and equivalent copies do not inflate scoring units. Conflicting
requested gold labels or evidence still fail. Four new tests cover equivalent
aliases, conflicting labels/evidence, unrequested conflicts and duplicate IDs.
The subsequent AWS check ran all 84 tests and completed the real replay,
as recorded above.

## AWS build and outputs

Run one manual override build with `buildspecs/evaluation-check.yml` and an
immutable feature commit. It installs only the existing CPU requirements,
runs 56 CPU tests (32 existing plus 24 replay tests) and the existing 28 cloud
tests, then evaluates the historical logs and publishes verified S3 artifacts.

The fixtures test source pins, request/response parsing, exact contract
mapping, reference identity, known accuracy denominators, deterministic first
attempt selection, malformed/missing output accounting, evidence audit,
unknown model/prompt provenance, S3 races/read-back failures, wrong destinations
and refusal to overwrite existing keys. All 84 tests, including the four
duplicate-reference regression tests, passed before the successful real replay.

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
