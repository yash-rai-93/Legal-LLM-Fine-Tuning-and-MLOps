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

- First CodeBuild execution and resolution of any cloud test failures.
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
