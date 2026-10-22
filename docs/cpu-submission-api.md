# Bounded CPU workflow submission: verified deployment and controlled recovery

This stage adds a separate thin Lambda and submission table to the existing
IAM HTTP API. The datasets, prompts, historical evaluation and completed-build
status endpoints keep their existing integrations. The final package passed
its AWS CodeBuild check, and real signed CloudShell requests exercised successful
submission, completed-key replay and a controlled abort with exact child-build
termination and conditional lock recovery. See [verified cloud evidence](#verified-cloud-evidence).

Current operating state: `SUBMISSIONS_ENABLED=false`, all three distinct demo
admissions used, and the failed run's active lock released. The failed submission
record and admission counter are retained. GET status/metadata routes remain
available. New processing requires a separate deliberate allowance decision;
merely enabling POST does not replenish the exhausted counter. Live inference
remains disabled.

## Request and status contract

| Request | Behavior |
| --- | --- |
| `POST /runs` | Requires `AWS_IAM`, a canonical UUID4 `Idempotency-Key`, and exactly `{"operation":"data_preparation"}`; returns 202 with the same UUID as submission ID while starting/running |
| `GET /submissions/{run_id}` | Reads/reconciles STARTING, RUNNING, COMPLETED, FAILED, ABORTED or TIMED_OUT; never starts a workflow |
| Repeated POST with the same key | Returns the existing submission; a bounded retry can recover an uncertain initial start using the same execution name and input |
| Different key while a run holds admission | 409, no request queued and no new workflow started |
| Fourth distinct admission while POST is enabled and no lock is held | 429; duplicate retries do not consume another admission; this real HTTP case remains unexercised |
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
buildspec, small Linux compute, the verified managed image
`aws/codebuild/amazonlinux-x86_64-standard:6.0`, no privileged mode,
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
requires the exact reviewed Standard graph/role, small Linux compute, the same
verified managed image as the runtime guard, concurrency one, retry zero and
low API throttles. It renders private files under
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
   reserved concurrency 1 where the regional quota permits it. In the verified
   account the total concurrency quota and minimum unreserved pool are both 10,
   so AWS rejected a reservation of 1. Leave concurrency unreserved in this
   account; the atomic DynamoDB admission lock and CodeBuild concurrency-one
   setting still limit this API to one active workflow. Lambda invocations can
   overlap and share regional capacity with the other functions. Retain low API
   throttles and the finite admission allowance. Use rendered
   `environment.disabled.json` initially.
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
endpoints and unsigned access. The completed-key replay was exercised in AWS.
The attempted competing-key request arrived after the prior execution completed,
so it was admitted as the second workflow. A 409 was later verified while the
aborted workflow's reservation was retained. Simultaneous running-request
rejection remains covered by cloud-run unit fixtures rather than a real racing
HTTP test. A subsequent seven-check CloudShell regression passed after the
submission routes were attached: signed completed-run metadata, datasets,
prompts and historical evaluation returned 200; signed submission lookups
returned 200 with COMPLETED and ABORTED respectively; unsigned dataset lookup
returned 403. The Lambda configuration was Successful with submissions disabled.
The private JSON report retains the individual responses. These checks establish
HTTP availability and the two expected submission states; they do not add a
fresh artifact-content or count/hash validation.

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
   Do not clear a different owner's reservation. Read back the absent lock,
   unchanged terminal record and unchanged counter, and retain a private recovery
   audit. A new post-recovery processing run is a separate acceptance check and
   requires an available, deliberately authorized admission. Record only the
   recovery steps actually exercised.

Do not delete `control#quota` during recovery. If the three-admission allowance
has been exhausted, remain disabled until an operator deliberately reviews and
authorizes additional work. Do not reset it to make an acceptance check pass.

The verified operator recovery used one DynamoDB transaction: condition-check
that the exact submission is ABORTED with the expected execution ARN, condition-
check that the admission counter is still the expected string value `3`, then
delete only the active lock whose owner matches that submission. Consistent
reads confirmed the lock absent and the original failure record/counter unchanged.
A private CloudShell JSON audit records the execution, stopped child build,
verification time and successful recovery. No quota row or request record was
deleted, and no replacement workflow was started.

The current status handler retains `recovery_required=true` on historical
ABORTED/FAILED/TIMED_OUT responses even after manual lock release. Consult the
operator audit and active-lock state for recovery completion; the failed attempt
retains its original status. There is no API-level recovery acknowledgement.

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

## Verified cloud evidence

Recorded 23 October 2026 (IST), from account-owner-supplied AWS output.
Tests and packaging ran in CodeBuild; HTTP administration and recovery ran in
CloudShell. No laptop application/test execution was used.

| Deployment fact | Verified value |
| --- | --- |
| Final package source | `e533237a61f7c49e09d69d1e0a7f3f7629378f79` |
| Final cloud check | SUCCEEDED / COMPLETED at that exact source commit |
| ZIP | `submission_api.py` only; 5736 bytes; exact version, AES256, SHA-256 and ZIP integrity checked |
| ZIP SHA-256 | `23fbc48849ec1c11efae645152438d5dc56a914ff6881f502bef3cfaf96c7016` |
| Deployed CodeSha256 | `I/vEiEnsHBHvrmRRUkONXcVqkU/2iB9QK+88+vlscBY=`; matched the ZIP digest |
| Lambda | Active / Successful, Python 3.12, 128 MiB, timeout 30 seconds |
| Workflow | Published Standard workflow version 1; fixed reviewed processor and registry path |
| Submission table | ACTIVE, PAY_PER_REQUEST, string `run_id` partition key |
| API integration | AWS_PROXY, payload 2.0, 30000 ms; both routes created with AWS_IAM |
| CodeBuild project | `aws/codebuild/amazonlinux-x86_64-standard:6.0`, small Linux compute, concurrency 1, retries 0, build 15 minutes, queue 5 minutes |
| Log retention | Dedicated submission Lambda log group set to seven days |
| Final control state | Submissions disabled, active failure lock absent, failed record preserved, admissions used 3 |

The scoped role policy uses the base state-machine ARN and a required numeric
StateMachineQualifier condition for StartExecution; it also permits only the
CPU project's read-only settings check. Lambda invocation permissions are
scoped to the exact API/account, default stage, HTTP methods and two paths.
Account IDs, API identifiers, object versions, run/execution IDs and generated
policies remain in the private deployment inventory.

AWS reported regional total/unreserved Lambda concurrency of 10/10 and rejected
reserved concurrency 1 because the minimum unreserved pool is 10. The deployment
therefore uses shared Lambda capacity with transactional single-workflow
admission, the finite counter and CodeBuild concurrency 1. No quota increase or
other function's reservation was changed.

| Real cloud check | Observed result |
| --- | --- |
| Unsigned POST | Account owner reported 403 |
| GET unknown submission | Account owner reported 404 |
| Signed POST with submissions disabled | 503 |
| First authorized submission | 202 / RUNNING; later COMPLETED with a registered build reference |
| Second distinct submission | 202 after the first had completed; later COMPLETED; exact child SUCCEEDED and complete |
| Replay of the first successful key | 200 / COMPLETED, original submission and registered-build reference |
| Third submission and automated stop | 202 / RUNNING, followed by a stop request about six seconds after creation |
| GET for the third submission | 200 / ABORTED, `recovery_required=true`, original terminal timestamp |
| Exact third child build | STOPPED, `buildComplete=true`, no missing build |
| Different key while the failed reservation was retained | 409 |
| Recovery configuration | Submissions disabled; update Successful; full 30-second invocation drain |
| Conditional operator lock recovery | LockReleased, FailureRecordPreserved and QuotaPreserved all true; admissions used 3; private audit saved |
| Final API regression after recovery | 7/7 passed: four signed metadata lookups 200, completed/aborted submission lookups 200 with expected states, unsigned dataset lookup 403; submissions remained disabled |

The stop request against the second workflow did not establish cancellation:
the later API/build responses were COMPLETED/SUCCEEDED. Only the third,
automatically stopped execution establishes the controlled abort result.

This checkpoint verifies submission, completed-status retrieval, completed-key
idempotency, failed-reservation blocking, one controlled abort, its child
termination and guarded manual lock recovery. It does not establish a new
successful workload after recovery: all three admissions were consumed and the
API was left disabled without resetting the counter. It also does not establish
real HTTP 429 after lock release, in-flight HTTP race/load behavior, dependency/
processing failure coverage, missing-output recovery, alarm delivery, measured
billing, full infrastructure recreation or Terraform ownership. No new model
training or live inference was performed.
