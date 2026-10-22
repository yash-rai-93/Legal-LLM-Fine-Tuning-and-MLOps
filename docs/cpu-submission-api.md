# Bounded CPU workflow submission: source prepared, deployment pending

This stage adds a separate thin Lambda and submission table to the existing
IAM HTTP API. The datasets, prompts, historical evaluation and completed-build
status endpoints keep their existing integrations. No deployed resource is
changed by this source commit. Tests and packaging must run in CodeBuild,
followed by real CloudShell acceptance checks before this stage is called live.
Live inference remains disabled.

## Request and status contract

| Request | Behavior |
| --- | --- |
| `POST /runs` | Requires `AWS_IAM`, a canonical UUID4 `Idempotency-Key`, and exactly `{"operation":"data_preparation"}`; returns 202 with the same UUID as submission ID while starting/running |
| `GET /submissions/{run_id}` | Reads/reconciles STARTING, RUNNING, COMPLETED, FAILED, ABORTED or TIMED_OUT; never starts a workflow |
| Repeated POST with the same key | Returns the existing submission; a bounded retry can recover an uncertain initial start using the same execution name and input |
| Different key while a run holds admission | 409, no request queued and no new workflow started |
| Fourth distinct admission in this deployment | 429; duplicate retries do not consume another admission |
| Missing signature | API Gateway rejects the request with 403 |
| Malformed or unapproved input | 400; wrong content type 415; no backend clients loaded |
| Unknown submission | 404 |
| Disabled submissions or unavailable backend | 503; preserve the key and check status rather than generating a new key |

GET remains available when `SUBMISSIONS_ENABLED=false`. The default is disabled.
Headers/body cannot choose a source, project, image, URL, command, bucket or role.
Bodies are limited to 1024 bytes. Responses contain allowlisted metadata only;
the Lambda logs only exception class names, never event bodies or SDK messages.

The submission UUID and CodeBuild run UUID are distinct. A completed submission
contains `registered_run_id` and `artifacts_status_url=/runs/<build UUID>` for
the existing verified metadata endpoint. A workflow is COMPLETED only after
the reviewed workflow returns its successful registry result. No predictions
or model accuracy are produced.

## Admission, idempotency and failure boundaries

Use a new on-demand STANDARD DynamoDB table `legal-llm-cpu-submissions`, string
partition key `run_id`, no sort key/index/TTL. It stores small metadata records
and the `control#active` and `control#quota` rows. One transaction creates a
submission, takes its global admission lock and conditionally increments a
string-encoded admission counter. The demo permits three total distinct
admissions, including failed or uncertain starts; duplicate retries do not
consume another allowance. Completion releases the active lock but retains the
counter. There is no automatic counter reset or TTL. Do not expire or
delete request records: their retention prevents old keys from starting again
after Step Functions' 90-day execution-name retention window.

The Lambda calls only a numeric published version of `legal-llm-cpu-prepare`,
with execution name `api-<submission UUID>` and exact input `{}`. The pinned
definition retains the already verified processor commit, small build compute,
15-minute build/5-minute queue bounds, source/buildspec and registry task.
CodeBuild concurrency remains one and automatic retries remain zero. The runtime
checks the actual project before each start attempt: fixed source/default
buildspec, small Linux compute, approved managed image, no privileged mode,
concurrency one and retries zero. A failed guard retains the reservation.

A start timeout or access error retains the reservation. Within five minutes
of reservation creation the SAME key can retry that SAME execution name/input.
If execution history is absent after that window, report RECOVERY_REQUIRED and
retain admission. Do not start a replacement. The reservation also survives
loss of a successful transaction or StartExecution response.

GET or a later POST observes Standard execution status. Successful completion
atomically saves the registered build ID and deletes only the matching active
lock. Conditional updates protect against simultaneous status readers. Failed,
aborted and timed-out outcomes are recorded but retain the lock and set
`recovery_required=true`: a terminal coordinator does not prove its child build
has stopped. Unknown status, invalid success output, backend uncertainty or
workflow/version mismatch also cannot free admission.

Reconciliation is on demand, without schedules, callbacks or continuous polling.
An unqueried record may retain STARTING in storage while its execution is running
or terminal; GET obtains the authoritative current status and persists terminal
outcomes. If history expires before reconciliation, operator recovery is needed.
Do not redrive API executions. Manual console/build starts bypass API admission;
keep manual builds and packaging separate from submissions.

## Cloud validation and packaging

From the existing CloudShell checkout, fetch and detach at the exact feature
commit for this stage. Start ONE manual build against that immutable commit,
with `buildspecs/submission-api-check.yml` as a one-build override. Do not change
the project's default cpu-publish buildspec, source or OUTPUT_BUCKET setting.
Do not run application tests or packaging on the laptop.

The build runs the complete cloud unittest suite, including concurrent same-key
and competing-key admission, response-loss recovery, failed/aborted/time-out
retention, late-key replay, authorization, request restrictions, low-level
transaction encoding, source/limit preflight and thin-package checks.
These tests use synthetic injected clients; they do not prove actual AWS IAM
permissions or a deployed workflow. The build publishes `cpu-submission-api.zip`
and package.json under its own `runs/<package-build UUID>/packages/` prefix.
Both uploads are byte-verified with recorded hashes and S3 versions. No pip,
model, GPU, workflow execution or resource deployment occurs in this check.

Require build SUCCEEDED. Download the exact ZIP version in CloudShell, verify
AES256, size, SHA-256 and ZIP contents (`submission_api.py` only), and retain
the manifest privately before creating runtime resources.

## Read-only preflight and runtime deployment order

Run `python3 setup/prepare_cpu_submission.py --api-id ACTUAL_API_ID` in
CloudShell. It reads the account identity, workflow, project, API and stage;
requires the exact reviewed Standard graph/role, small compute, concurrency
one, retry zero and low API throttles. It renders private files under
`~/legal-llm-deploy/submission-api`, surviving CloudShell /tmp resets. The script
does not publish, deploy, modify resources or start a build.

Review the saved definition and `publish-version.json`, then use
`aws stepfunctions publish-state-machine-version --region us-east-1
--cli-input-json file://$HOME/legal-llm-deploy/submission-api/publish-version.json`
to publish the checked revision. Publication starts no execution. Run the
preflight again with `--workflow-version-arn ACTUAL_NUMERIC_VERSION_ARN` to
verify that version and render the exact scoped policies/disabled environment.
The registry task still invokes the current unqualified registry Lambda;
preserve its verified code/configuration and require separate verification for
later changes. Drain submissions before changing the configured workflow version.

After cloud checks and package verification:

1. Create the new submission table and wait for ACTIVE. Do not alter the existing
   `legal-llm-cpu-runs` registry table. Retain submission records without TTL.
2. Create `/aws/lambda/legal-llm-cpu-submission-api`, seven-day retention, and
   `legal-llm-cpu-submission-api-role` using the supplied trust/scoped policies.
   DynamoDB transaction permissions use PutItem/DeleteItem with
   `dynamodb:EnclosingOperation=TransactWriteItems`, rather than a nonexistent
   broad transactional action. Runtime may read only the CPU project's settings
   with BatchGetProjects; it cannot start builds directly. StartExecution uses
   the base state-machine resource with a required StateMachineQualifier condition
   for the exact published numeric version. Runtime has no S3, registry-write,
   StopExecution, RedriveExecution, IAM or model-service permissions.
3. Create the Lambda from the verified ZIP object version: Python 3.12,
   `submission_api.lambda_handler`, x86_64, 128 MiB, timeout 30 seconds; set
   reserved concurrency 1. Use rendered `environment.disabled.json` initially.
   No Function URL, VPC, schedules or provisioned concurrency.
4. Create a separate AWS_PROXY integration, POST invocation, payload 2.0,
   timeout 30000 ms. Create only `POST /runs` and `GET /submissions/{run_id}`
   with AWS_IAM from creation. Preserve existing routes/integrations/throttles.
5. Add exact-source-account Lambda invoke permissions for this API's default
   stage and these two method/paths. Caller policy permits only these routes.
6. Check unsigned rejection, signed unknown submission 404 and disabled POST
   503 before enabling. Verify the numeric workflow version and IAM policies.
   Set SUBMISSIONS_ENABLED=true only for deliberate acceptance runs; update
   the complete environment map so other variables are retained.

Keep actual account/API/version ARNs and generated environment/policy files
in the private inventory. Resource creation, IAM propagation and real permission
validation remain separate from unit tests. The source does not apply Terraform
or take ownership of existing manually provisioned resources.

## Acceptance and failure recovery

Generate and SAVE one UUID4 idempotency key before the first submission. Use
`setup/call_cpu_submission_api.py --endpoint ACTUAL_ENDPOINT --idempotency-key KEY`
from CloudShell. It signs the exact fixed request using temporary credentials,
restricts destinations, blocks redirects and has no auto-retry loop. Reuse the
same key for duplicate checks. Use `--submission-id KEY` for status reads,
spaced to respect the existing 1/s API throttle. GET starts no work.

Require one 202 and exactly one Standard execution/CodeBuild job for repeated
same-key requests; a different key during the active run must return 409 with
no second execution. On completion require COMPLETED, expected registered build
ID, existing GET /runs/<build UUID> 200, and matching counts/versions. Repeat
the completed key and require no new execution. Recheck existing metadata
endpoints and unsigned access. These are pending real AWS checks.

For failure/recovery acceptance, deliberately stop one uniquely identified
API execution during a bounded test run; do not redrive it. Require ABORTED and
recovery_required=true from GET; a different key must remain blocked. Inspect
its CodeBuild child, stop it explicitly if active, and verify terminal build
status. Cancellation by the coordinator is best effort. Invalid dataset,
dependency-install, processing, missing-output and timeout cases are not all
established by this one abort check; document each actually exercised case.

Before releasing any failed/uncertain reservation, an operator must:

1. Disable POST submissions and wait at least the full Lambda timeout after
   the configuration update is Successful, so no previous invocation can start
   work late. Keep status reads available.
2. Inspect the stored exact execution ARN and its history. Confirm the
   execution is terminal or was never started. Verify all associated queued/
   running CodeBuild jobs have ended; stop active children and recheck. If
   status is uncertain, retain admission.
3. Retain the submission record as terminal FAILED/ABORTED/TIMED_OUT; for an
   abandoned STARTING record, record ABORTED with a private operator audit note.
   Never delete that record or reuse its key.
4. Delete only `control#active` with an `owner = :this_submission_id` condition.
   Do not clear a different owner's reservation. Enable submission only after
   resource/status checks pass, then deliberately submit one NEW key to verify
   recovery. Record the real results before claiming recovery is verified.

Do not delete `control#quota` during recovery. If the three-admission allowance
has been exhausted, remain disabled until an operator deliberately reviews and
authorizes additional work. Do not reset it to make an acceptance check pass.

The API cannot stop jobs or clear failed reservations itself. This favors
retaining admission during uncertainty over launching overlapping paid jobs.
There is no periodic recovery service or automatic failure retry.

## Retirement and remaining limits

Disable submission first, drain/stop executions and confirm child builds end.
Remove only the two new routes/integration/invoke permissions and submission
Lambda/dedicated role if retiring this stage. Preserve the existing metadata
API, CPU workflow, registry and artifact bucket. Preserve/export submission
records while their keys might be replayed; restore them before re-enabling a
replacement API. Published workflow versions and table/log retention have
separate cleanup decisions. Do not delete a shared workflow merely to remove
this API stage.

Low throttles, single-run admission, finite deadlines and manual invocation
bound accidental work; they are not an account-wide billing cap. Alarm
delivery, complete failure coverage, billing measurement and repeatable
infrastructure ownership/recreation remain unverified.

## Primary references

- [Standard StartExecution idempotency and numeric version ARNs](https://docs.aws.amazon.com/step-functions/latest/apireference/API_StartExecution.html)
- [Version authorization uses the base ARN and a qualifier condition](https://docs.aws.amazon.com/step-functions/latest/dg/auth-version-alias.html)
- [Atomic DynamoDB transactions](https://docs.aws.amazon.com/amazondynamodb/latest/APIReference/API_TransactWriteItems.html)
- [IAM permissions for transactional operations](https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/transaction-apis-iam.html)
- [Best-effort cancellation of synchronous jobs](https://docs.aws.amazon.com/step-functions/latest/dg/connect-to-resource.html)
