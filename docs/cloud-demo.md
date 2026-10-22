# AWS CPU deployment: current stage

This branch deploys CPU preparation, supplied-prediction evaluation, verified
S3 artifacts, run registration and an IAM submit/status/metadata API. The
[submission and recovery checkpoint](cpu-submission-api.md#verified-cloud-evidence)
records two completed API-submitted workflows and one controlled abort with
child termination and conditional lock recovery. POST is currently disabled
after the three-admission demo allowance was used; GET routes remain available.
The CPU release does not train a model, invoke SageMaker, or run MLRun.
The original GPU implementation and its historical results remain separate.

## Account constraints

Verify account plan, live credit balance, individual credit expiry and
applicable services before creating resources. A normal AWS Budget is an
alert, not a hard cap; disabling bank autopay does not prevent AWS charges.
Keep the initial jobs small and explicitly monitor actual usage. No account
balance or budget configuration has been independently verified by this code.

## First cloud check

Verified from supplied AWS logs on 21 October 2026 (UTC): commit
`8b2606dc9efed0dfbbb73fede194dfb5d0cce1d8` passed all 15 tests and prepared
423 training / 61 validation documents (7191 / 1037 hypothesis rows).
The original check did not retain external artifacts. This evidence verifies
CPU processing and fixture-based metric tests, not a trained model's accuracy.

Run the check in AWS CodeBuild, not on the laptop:

| Setting | Value |
| --- | --- |
| Source | This GitHub repository, branch `feat/aws-cpu-deployment` |
| Buildspec | `buildspecs/cpu-check.yml` |
| Managed environment | Verified current image `aws/codebuild/amazonlinux-x86_64-standard:6.0`, Linux x86_64 |
| Compute | On-demand `BUILD_GENERAL1_SMALL`, if permitted |
| Privileged mode | Off |
| Timeout | 15 minutes initially; investigate before increasing |
| Concurrent build limit | 1 |
| Trigger | Manual; no schedule/webhook |
| Build artifacts | No external artifacts for this first check |
| Logs | CloudWatch; set retention to 7 days |
| GPU/Hugging Face credentials | None |

The build role needs only the source-connection permissions required by the
chosen source integration and scoped CloudWatch log permissions for this
check. It does not need SageMaker, EC2, IAM administration or access to
unrelated S3 buckets. The console can create a dedicated service role; inspect
its proposed permissions. Do not add AdministratorAccess.

The build creates an isolated Python 3.11 environment, installs only the CPU
requirements, runs known-answer/failure tests, and processes the repository's
tracked train/validation Parquet files. It prints a metadata-only manifest.
It neither calls AWS resource-creation APIs nor generates predictions.
The temporary JSONL output stays in the ephemeral build environment at this
stage; S3 publication is a later deployment milestone.

Expected counts if the supplied data is unchanged: 423 train documents and
61 validation documents, with 17 hypotheses per document. These expectations
must be checked in the real AWS build, not assumed from earlier analysis.
The official test split is ignored by Git. Recovery and versioned S3
registration have since passed the separate catalog build recorded below.
The initial CPU check did not include test data and must not reuse validation
data as the held-out test set.

## Output formats

Processed JSONL records have `document_id` (normalized string), original
`text`, `text_sha256`, `origin: contractnli` and `inference` (17 objects).
The manifest records source commit, schema, timestamps, counts and hashes.
Random run IDs/timestamps do not enter semantic content hashes.

The Python evaluation function accepts supplied reference/prediction records
in this schema. It requires provenance (`fixture`, `historical_predictions`,
or an identified `model_run`). Fixtures verify metric logic; they are not
predictions from Hermes. Missing/invalid documents count as wrong in
`label_accuracy_all_attempts`; scored-only metrics explicitly report coverage.
ROUGE-3 uses the historical project convention of score 1 for a correct
`not_mentioned`. It is not legal correctness or exact evidence-span accuracy.

The older JSONL files under `dataset_engineering/processed/` use a different
historical representation in some rows. This new path deliberately reads the
row-per-hypothesis Parquet files rather than silently accepting that format.

## Still pending

- Additional failure modes, post-recovery workload execution and full resource cleanup/recreation.
- Real in-flight HTTP race/load checks and HTTP 429 after quota exhaustion.
- Full dependency locking; a deployed alarm/notification acceptance check.
- Isolated Terraform ownership/import of the manually deployed CPU resources.
- Live throttle/load tests and measured billing/credit consumption.
- Integration of the pure transformation into the existing MLRun wrapper.
- Full legacy CI/CD repairs and model/deployment path reconfiguration.
- GPU training/live serving and MLRun hosting, outside this release.

Use the AWS CPU plan in `docs/aws-credit-deployment-plan.md` for the staged
target. Do not run the original setup/deployment scripts as a substitute.
The CPU workflow, automatic run registration and IAM status/artifact APIs are
operational. Bounded submission has passed its real submission, completed-key
replay, controlled-abort and conditional-lock-recovery checks; it is currently
disabled with its three-admission counter preserved.

## Next milestone: retain outputs in private S3

The chosen destination is `yash-legal-llm-cpu-20261022` in `us-east-1`.
Keep Block Public Access enabled, ACLs disabled, versioning enabled and
SSE-S3 encryption. Bucket creation was reported by the account owner; its
configuration is checked where needed by the publication build.

1. In CodeBuild's environment settings, open the actual service-role link.
   Add the inline policy in
   `infra/serverless-demo/policies/codebuild-artifact-access.json` to that role.
   It allows bucket configuration reads plus read/write/multipart cleanup
   only under this bucket's `runs/` prefix. It does not allow object deletion,
   public ACL changes, bucket policy changes or access to other buckets.
   Existing source/log permissions remain necessary; inspect them separately
   if auditing the role's effective permissions.
2. Set the CodeBuild buildspec to `buildspecs/cpu-publish.yml` and add a
   plaintext environment variable named `OUTPUT_BUCKET` with the bucket name.
   This is configuration, not a secret. Keep native Artifacts at No artifacts:
   the build script performs the S3 publication itself.
3. Retain the same small compute type, 15-minute timeout, concurrency one,
   disabled privileged mode and manual trigger. Use the feature branch.
4. Run one build. It reruns CPU checks, generates the outputs, checks bucket
   region/versioning, uploads with SSE-S3, downloads each object in the AWS
   build and compares its bytes. It prints object size, encryption and version
   ID, without printing contract contents.
5. After success, browse `runs/<build-run-UUID>/` in S3. It must contain
   `train.jsonl`, `validation.jsonl` and `manifest.json`. A different prefix is
   used for every build; old runs are not overwritten by normal publication.

The script uploads the manifest last. Consumers must require CodeBuild
success and valid output checksums before registering a run, because a
failed/terminated upload can leave partial objects. There are a few S3
upload/read/metadata requests per run, plus storage and build charges;
credits and budget alerts are not an unlimited free-use guarantee.

On 21 October 2026 (UTC), the account owner confirmed all three files in S3
and supplied the manifest created at `2026-10-21T22:39:44.484677+00:00` from
commit `8a1df10ea83cb9f5c476e849491cd427651c1c39`. Its train/validation counts,
input hashes, semantic hashes and output hashes match the first verified
CPU check. The S3 confirmation is user-reported; the second build's final
logs and object version IDs have not been independently inspected.

## First successful orchestrated cloud run

Verified from account-owner-supplied AWS output on 21 October 2026 (UTC):

- AWS definition validation: OK, no diagnostics; project autoRetryLimit: 0.
- Workflow: legal-llm-cpu-prepare (Standard, us-east-1).
- Execution ID: 417ac134-d43a-45d9-b441-9f786ead2182.
- Execution status: SUCCEEDED; Error and Cause: null.
- Build: legal-llm-cpu-check:2ec431fc-340c-45f8-bb16-d93ab1c3aa89.
- Build status: SUCCEEDED.
- CPU source commit: 8a1df10ea83cb9f5c476e849491cd427651c1c39.
- S3 prefix: runs/2ec431fc-340c-45f8-bb16-d93ab1c3aa89/ in the chosen bucket.
- Operation: data_preparation; live_inference: false.

This verifies the Standard workflow's synchronous CodeBuild integration and
the build's tested preparation/publication path. The three new objects and
their version IDs have not been separately inspected after this execution.
The previously supplied manifest contains the expected 423/61 document counts.
At that checkpoint, the API, held-out test recovery, failure/cancellation
checks, billing measurement and Terraform ownership remained pending. Automated registration has since
passed the integrated verification recorded below. Independent registry verification is recorded below. See [workflow operations](cpu-workflow.md).

## Log retention and next stage

The account owner supplied a CloudWatch result confirming retention of seven
days on `/aws/codebuild/legal-llm-cpu-check`. The existing successful workflow
is unchanged. The next [run-registry milestone](cpu-run-registry.md) adds
manifest/output integrity checks and conditional DynamoDB registration.
The registry test/package build succeeded. The account owner confirmed an
ACTIVE DynamoDB table, an Active/Successful Python 3.12 Lambda, a matching ZIP
checksum and successful real registration of the existing CPU run. Repeating
the invocation returned created=false and preserved the original timestamp.
See [registry evidence](cpu-run-registry.md#verified-cloud-evidence).
The [workflow integration update](cpu-workflow.md#update-the-existing-workflow-with-registration)
passed AWS validation, deployment and an integrated execution after these
independent Lambda checks. Those independent checks reused the existing build;
the subsequent integrated smoke test created one new finite CPU build.

## Successful workflow with automatic registration

Verified from supplied AWS output on 22 October 2026 (IST):

- Definition validation: OK, no diagnostics; autoRetryLimit: 0.
- Applied workflow revision: `530c5353-7eae-4548-bf9f-135d58f59f68`.
- Execution: `56cbb5cc-6db7-4389-a908-58009740e135`, SUCCEEDED.
- Final registry status: COMPLETED, created=true, live_inference=false.
- Build/run UUID: `1605eeda-3334-4fc4-a8f3-c5c047f75994`.
- Processor source: `8a1df10ea83cb9f5c476e849491cd427651c1c39`.
- Registry source: `39d979d9fc0e40457b3ae81e9a9a3f445099fa61`.
- Artifacts: `runs/1605eeda-3334-4fc4-a8f3-c5c047f75994/` in the chosen private bucket.
- Counts: train 423 documents / 7191 rows; validation 61 documents / 1037 rows.
- Manifest version: `efsfjDwxyPcvmTvJJ3UtRcJeKl88doXh`.
- Registration time (machine UTC): `2026-10-21T23:57:13.838299+00:00`.
- Execution Error and Cause: null.

This verifies the real chain: Step Functions -> CodeBuild -> S3 -> Lambda ->
DynamoDB. Lambda streaming checksum/provenance checks and the conditional
registration were exercised against actual AWS resources. It demonstrates a
CPU data workflow, not new model training/inference or model accuracy.
Additional negative/failure/recovery checks, billing measurement, held-out
split recovery, submission API and repeatable infrastructure remained pending
at that checkpoint. The read-only API and split/prompt catalog have since
passed the verification recorded below.

## Provenance rejection and status API verification

A direct real invocation with an unapproved processor commit returned the
expected FunctionError/ValueError, `Unapproved processor commit`, before any
artifact reads or run registration. This complements successful registration
and idempotency checks. Full cancellation/orphan-job recovery remains pending.

The account owner again reported $100.39 estimated credits remaining; the
summary updates approximately every 24 hours, so this is not proof that recent
builds/invocations incurred no charges.

The [read-only IAM status API](cpu-status-api.md#verified-deployment-evidence)
is now deployed. Its test/package build
`legal-llm-cpu-check:0ff4be99-7647-4a6d-a6b7-97a6d2dad3da` succeeded from
source `6113880be73a3caa8bb483473336e016c7d3fd3b`. The deployed ZIP checksum
matches the versioned package manifest. The account owner supplied HTTP 200
for a signed request for the integrated run (expected counts and artifact
versions), 403 for an unsigned request and 404 for a valid unregistered UUID.
The GET route uses AWS_IAM; the default stage reports a rate of 1 request/second
and burst 2. These reads do not start builds or perform inference.

## Successful dataset/prompt catalog

The [catalog stage](cpu-artifact-catalog.md#verified-cloud-evidence) passed in
AWS build `legal-llm-cpu-check:51eed2ff-576e-4bf4-b1d1-47203f7092a8`
from source `056f8a4269d12d1bf321660ab1e76466aa6fecd9`.
It ran 32 CPU and 28 cloud tests, recovered the pinned official release,
validated split overlap and baseline equivalence, then published and
read-back-verified nine artifacts and the final S3 catalog.

Supplied catalog output confirms train 423/7191 rows, validation 61/1037 and
held-out test 123/2091, all four checks true, encrypted versioned artifacts and
matching prompt identity/artifact digests. Registration is an S3 manifest,
without a DynamoDB/API integration yet. The existing deployed workflow,
registry and status API were not updated. No model was run. Billing review
was skipped at the account owner's request; no consumption measurement is claimed.

## Historical evaluation replay

The [CPU historical replay](cpu-historical-evaluation.md) uses a manual
evaluation-check build with pinned checked-in request/response logs
and the exact accepted S3 test artifact. It reports request-level and
deterministic first-attempt-per-document metrics, including missing/invalid
outputs and the actual held-out subset covered. Model/adapter revision gaps
and the distinction from the new CPU prompt remain explicit.

The initial evaluation build `84a774e3-3a70-477d-9c6f-651236af5d01`
passed 52 CPU and 28 cloud tests, then stopped before publication because a
global reference-text uniqueness guard rejected duplicate held-out texts.
A targeted fix now preserves equivalent reference aliases, rejects conflicting
requested annotations and reports input-contract versus matched-record counts.
The follow-up build `7a14af2a-39a3-42e7-8720-4b4005269d1a` succeeded
from source `01e110a8999e6a333d8089635f22f0d3d3dc6ab9`, after 56 CPU and
28 cloud tests. Its versioned evaluation manifest confirms 40 requests
(26 scored, 12 invalid, 2 missing) and 35 requests (22 scored, 9 invalid,
4 missing). Each group matches 10 of the 123 held-out records.

Request-level all-attempts label accuracy is 51.47% and 50.76%;
deterministic first-attempt-per-contract accuracy is 48.24% and 57.65%.
These historical subset scores retain missing/invalid responses in their
denominators. They are not full-held-out or newly generated model results.
The encrypted report/attempt artifact versions and provenance limitations
are recorded in [verified replay evidence](cpu-historical-evaluation.md#verified-aws-replay).
The existing workflow, registry and status API were not updated.

## Verified submission and abort recovery

The final submission package at
`e533237a61f7c49e09d69d1e0a7f3f7629378f79` passed its AWS cloud check and
exact S3 ZIP/code-checksum verification. Both new routes use AWS_IAM:
POST /runs and GET /submissions/{run_id}. The runtime checks the actual CPU
project's source, image and limits and invokes only the reviewed published
workflow version.

Supplied CloudShell responses confirm two completed submissions and a third
controlled ABORTED submission. Replaying the first completed key returned the
existing result; a distinct key was rejected with 409 while the aborted
reservation remained held. The aborted run's exact CodeBuild child was STOPPED
and complete. An operator disabled POST, drained invocations and conditionally
released only that submission's lock, preserving both its ABORTED record and
the admission counter at three. The recovery read-back and private audit were
confirmed by supplied output.

See [deployment facts, real check results and remaining limits](cpu-submission-api.md#verified-cloud-evidence).
These results establish guarded manual lock recovery; no new processing run
was started afterward because the allowance was exhausted. Reads remain
available, and model training/inference remain disabled.
