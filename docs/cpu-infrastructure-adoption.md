# Adopting the existing CPU deployment

The CPU workflow, IAM APIs and monitoring are manually deployed and verified.
The directory `infra/serverless-demo/` currently contains supporting policies
and configuration templates; it does not yet provide Terraform resource
ownership. Keep this distinction until imports and a saved plan are verified.

This stage begins with a private read-only snapshot. It does not create a
backend, import state, apply Terraform or perform cleanup. It does not establish
that an existing resource is absent from another Terraform state. Check existing
state ownership before assigning any resource to a new address.

## Capture current configuration

Run from the exact feature commit in AWS CloudShell:

```bash
python3 setup/capture_cpu_infrastructure.py --api-id ACTUAL_API_ID
```

The script uses temporary CloudShell credentials and read-only SDK operations.
It requires the submission API to remain disabled and its API ID and numeric
CPU workflow version to match. It reads only known CPU resources and the roles,
bucket and subscriptions referenced by them. It writes a timestamped directory
under `~/legal-llm-deploy/infrastructure/`, with private permissions:

- `snapshot.json`: live configurations, referenced policies, IDs and package
  manifest candidates, plus any read failures and redactions.
- `summary.json`: counts, status and snapshot SHA-256 suitable for sharing.

The snapshot checkpoints after each resource group. AWS timestamps are encoded
as ISO text. Unknown Lambda/project environment values, webhook secrets and
non-ARN source-auth resources are redacted; redactions must be resolved through
appropriate private configuration before claiming complete reproduction.
No session credentials, Lambda presigned download URLs, contract objects,
prediction files or DynamoDB application rows are copied. SNS endpoint and
subscription details remain private in the snapshot.

| Captured group | Scope |
| --- | --- |
| Lambda | Four CPU function configurations, concurrency, tags and resource policies |
| Workflow | Current CPU Standard definition, configured immutable numeric version and tags |
| CodeBuild | CPU project source/buildspec, role, environment, image, limits and settings |
| HTTP API | Existing API, routes, integrations, stages and authorizers |
| Storage | Two table descriptions/TTL/backup settings/tags and configured output bucket settings |
| IAM | Referenced roles, inline policies and managed-policy attachment references |
| Logs and alerts | Five known log groups, three alarm configurations, dedicated topic and subscriptions |
| Local package manifests | Saved private package manifests matched to deployed Lambda code hashes |

The script records absent optional bucket settings/resource policies separately
from failed reads. `PARTIAL` means one or more reads failed; share only the summary
and resolve the named service/operation errors. It never expands an IAM role or
changes a resource to make a read succeed. `CAPTURED` means the requested reads
completed; it does not mean the snapshot is a complete backup or a verified
recreation plan. Package-match counts and redactions are separate readiness
signals. A Lambda hash match identifies a candidate saved ZIP manifest; it does
not re-download or re-verify that S3 object version.

Keep the full snapshot outside the repository. Do not paste it into chat or
commit it: it contains private deployment configuration, IAM documents and email
subscription endpoints. Share only the final summary JSON.

## First live capture and package recovery

The account owner supplied a successful first CloudShell capture: four Lambdas,
two tables, six referenced roles, six HTTP routes, five log groups and three
metric alarms. ReadErrors was empty, no environment variables were redacted,
and submissions remained disabled. No AWS resource or Terraform state changed.
Only one Lambda matched a previously saved local package manifest, so full
package-pin recovery remains a prerequisite for the Terraform baseline.

From the feature commit containing the recovery script, run:

```bash
python3 setup/recover_cpu_package_pins.py --snapshot-dir ACTUAL_PRIVATE_SNAPSHOT_DIRECTORY
```

The script verifies the snapshot hash/account and current Lambda code hashes.
It lists only the configured bucket's `runs/` metadata, bounded to 5,000 listed
objects and 100 package-manifest candidates. It reads only matching package
manifest keys and selected thin ZIPs, never contract/prediction contents. It
uses manifests whose hashes match deployed code rather than selecting the newest
build. Each chosen candidate must have the correct component/schema, handler,
runtime, source commit, bucket, build-scoped key and non-null object version.

For each selected package, the script requires a completed successful CPU
CodeBuild producer at that exact source commit, repository, component buildspec
and output bucket. It reads the exact ZIP version, checks SSE-S3, byte length,
SHA-256 against the manifest and deployed Lambda, source metadata and bounded
ZIP contents/CRC. The artifact API's private approval JSON stays within the
verified ZIP. Legacy registry manifests without a component field are supported
only with the corresponding schema/operation/handler/key and all other checks.

Verified ZIPs, exact manifests and a recovery audit remain under a timestamped
private `package-recovery/` subdirectory. Only after all four packages verify
does the script write `package-pins.json` beside the original snapshot. The
snapshot itself is preserved. It rechecks live code hashes and disabled
submissions before completing. It never invokes or updates a Lambda, publishes
an object, starts a build or changes Terraform state.

Share only the final recovery summary. If INCOMPLETE/STOPPED, resolve the reported
gap; do not rebuild or substitute a different package merely to fill the pin.
The first real recovery run and all four verified package pins are pending until
AWS evidence is supplied. A successful snapshot alone does not close this gap.

## Build the isolated Terraform baseline after capture

1. Record the ownership boundary and the new state location. Check any existing
   state before adopting resources. Referenced/shared roles or managed policies
   may remain external dependencies rather than become managed resources.
2. Recover every exact Lambda ZIP object/version and verify its hash against
   the deployed code. Preserve the artifact API's packaged approval manifest.
   A function configuration/hash alone cannot recreate its code.
3. Define resource blocks from captured settings in a new isolated CPU stack.
   Pin Terraform/provider versions. Keep generated private variables, state and
   saved plans outside source; use encrypted/versioned state with locking.
4. Map each existing remote object to one Terraform address and prepare reviewed
   imports. Preserve route/integration IDs, numeric workflow pins, current role
   scopes, log retention, throttles, table data and disabled submissions. Treat
   unsupported immutable objects and externally managed policies explicitly.
5. Run Terraform formatting/validation and planning in AWS. The first adoption
   plan must contain imports with zero resource creates, updates, replacements
   or destroys. Resolve configuration drift before an adoption apply.
6. After state adoption, require a no-change plan and repeat the verified API/
   monitoring checks. Import readiness or a generated configuration file alone
   does not establish ownership or successful recreation.

No Terraform import/apply is included in the capture command. A saved plan and
exact state-ownership boundary are the reviewable next deliverables.

## Recreation and cleanup are separate acceptance work

The snapshot is configuration metadata, not a data backup. Restoring this
service requires its versioned S3 artifacts/packages, dataset and prompt approval
pins, registry/submission records and operator audits. In particular, preserve
the admission counter and idempotency records rather than resetting them through
recreation. Their recovery/export procedure has not yet been verified.

Recreation should first use deliberately isolated resources and a reviewed plan.
Existing production/demo names, the shared artifact bucket, table contents and
immutable workflow pins must not be replaced merely to test Terraform. The
cleanup inventory must identify shared dependencies and retained data before
any deletion. No cleanup, resource recreation, table export/restore, extra
processing admission or billing measurement is established by this snapshot.

## Primary references

- [Import existing Terraform resources](https://developer.hashicorp.com/terraform/cli/import/usage)
- [Configuration-driven imports](https://developer.hashicorp.com/terraform/language/import)
- [First import operation should preserve existing configuration](https://developer.hashicorp.com/terraform/tutorials/state/state-import)
- [Lambda configuration API](https://docs.aws.amazon.com/lambda/latest/api/API_GetFunctionConfiguration.html)
- [CodeBuild project configuration](https://docs.aws.amazon.com/codebuild/latest/APIReference/API_BatchGetProjects.html)
