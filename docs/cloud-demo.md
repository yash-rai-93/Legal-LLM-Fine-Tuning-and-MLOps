# AWS CPU deployment: current stage

This branch adds CPU preparation and supplied-prediction evaluation. It does
not provision AWS resources, train a model, invoke SageMaker, or run MLRun.
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
| Managed environment | Ubuntu 22.04, `aws/codebuild/standard:7.0`, Linux x86_64 |
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
The official test split is currently ignored by Git. Its recovery and
versioned registration are still pending. This check must not reuse
validation data as the held-out test set.

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

- AWS validation and first execution of the [Step Functions wrapper](cpu-workflow.md).
- Reproducible held-out test acquisition, pinned dependency transitive lock,
  S3 inputs/outputs and object version IDs.
- Isolated Terraform bootstrap/runtime resources, Step Functions and run index.
- API authorization, throttling, bounded execution and cleanup verification.
- Integration of the new pure transformation into the existing MLRun wrapper.
- Full legacy CI/CD repairs and model/deployment path reconfiguration.
- GPU training/live serving and MLRun hosting, which remain outside this release.

Use the AWS CPU plan in `docs/aws-credit-deployment-plan.md` for the staged
target. Do not run the original setup/deployment scripts as a substitute for
this check. This branch is not yet a deployed application.

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

The next incremental milestone is the [manual Step Functions workflow](cpu-workflow.md).
Its definition, scoped execution-role templates and read-only CloudShell
preparation/validation script are ready. AWS definition validation, resource
creation and first workflow execution remain pending. Test-split recovery,
manifest-validator Lambda, DynamoDB/API and Terraform ownership remain pending.
No laptop execution is required.
