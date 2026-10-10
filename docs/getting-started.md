# Setup and Workflow Guide

[Back to the README](../README.md)

The repository contains environment-specific infrastructure and notebook examples. Adapt those references to your own AWS/MLRun environment before execution.

## Contents

- [Local environment](#local-environment)
- [Configuration](#configuration)
- [Infrastructure and MLRun](#infrastructure-and-mlrun)
- [Notebook execution order](#notebook-execution-order)
- [Validation](#validation)
- [Known constraints](#known-constraints)

## Local environment

Use Python 3.11 for local orchestration, matching CI. GPU training/evaluation use separate containers.

From the repository root:

```bash
python -m venv .venv
```

Activate the environment:

```powershell
# Windows PowerShell
.venv\Scripts\Activate.ps1
```

```bash
# Linux/macOS/WSL
source .venv/bin/activate
```

Then install the recorded host dependencies:

```bash
python -m pip install -r requirements.txt
```

`requirements_ci.txt` provides the separate CI tool environment. Linting/test commands below need its tools.

## Configuration

AWS SDK authentication must be available locally and in remote jobs. Some scripts assume explicit credential variables and Kubernetes secrets rather than a local AWS profile.

| Variable | Purpose |
| --- | --- |
| `ENV` | Resource and S3-path environment prefix |
| `ACCOUNT_ID` | AWS account in image/resource references |
| `MLRUN_AWS_ROLE_ARN` | SageMaker execution role |
| `HF_TOKEN` | Model access and adapter upload |
| `IMAGE_TAG` | Build/deployment image version |
| `FEATURE_BRANCH` | Git branch used by the function build script |
| `MLRUN_DBPATH` | MLRun connection; also affects local/cluster code paths |
| `AWS_ENDPOINT_URL_S3` | Explicit S3 endpoint used by some jobs |

CI reads secrets including `AWS_ACCOUNT_ID`, AWS credentials, `MLRUN_AWS_ROLE_ARN`, `HF_TOKEN`, and platform connection values in the [workflow](../.github/workflows/dev-pr-open.yml).

Local modules use `load_dotenv()`, so a local `.env` can supply application variables. Keep credentials out of version control. Bash scripts need exported process variables.

Review these configuration locations:

- [project.yaml](../project.yaml): project source and image references.
- [Terraform/providers.tf](../Terraform/providers.tf) and [Terraform/](../Terraform/): region, state bucket/keys, account/environment values, and resource names.
- [build_functions.py](../setup/build_functions.py): source repository, branch, images, enabled builds.
- [evaluate_train.py](../src/evaluate_train.py) and [evaluate_no_train.py](../src/evaluate_no_train.py): model revisions, GPU images, instance types, and S3 paths.
- [train_multi.py](../src/scripts/training/train_multi.py): adapter upload repository.
- Workflow notebooks: dataset/prompt tags, endpoint references, and deployment settings.

Changing only `ENV` and `ACCOUNT_ID` does not replace every hard-coded value.

## Infrastructure and MLRun

Use an accessible MLRun 1.11.0 environment and AWS resources required by your selected workflow. Installing Python dependencies does not provision infrastructure.

[k8s/install.sh](../k8s/install.sh) assumes Bash, Minikube, Helm, an ECR account, `k8s/credentials`, and exported AWS credentials. It disables pipeline, Prometheus-stack, and Spark-operator options. Adapt it to your cluster.

[Local service notes](../k8s/local_kubernetes.md) list API/UI addresses. Most current workflow code expects `http://localhost:30070` for MLRun.

Terraform includes S3, Lambda, AppConfig, IAM, and CloudWatch resources. Review the backend bucket/key and environment variable files before provisioning; the state bucket must exist and container image references must be available in your ECR.

## Notebook execution order

Use `notebooks/` as the notebook working directory. Many cells use `context="../"` and estimator source paths relative to this directory.

| Step | Notebook | Preparation |
| --- | --- | --- |
| Connect/register functions | [mlr_local_register_funcs.ipynb](../notebooks/mlr_local_register_funcs.ipynb) | Reachable MLRun project/API and suitable images |
| Register data | [mlr_workflow_register_raw_data.ipynb](../notebooks/mlr_workflow_register_raw_data.ipynb) | Prepared Parquet files in configured S3 paths |
| Register prompt | [mlr_register_prompt.ipynb](../notebooks/mlr_register_prompt.ipynb) | Selected prompt and artifact version |
| Evaluate base model | [mlr_workflow_evalutateModel_noTrain.ipynb](../notebooks/mlr_workflow_evalutateModel_noTrain.ipynb) | Dataset/prompt tags, model revision, evaluation image, AWS GPU access |
| Train/evaluate adapter | [mlr_workflow_evalutateModel_Train.ipynb](../notebooks/mlr_workflow_evalutateModel_Train.ipynb) | Training image, model access, adapter write permission, SageMaker role/quota |
| Register model | [mlr_register_model.ipynb](../notebooks/mlr_register_model.ipynb) | Selected revision, metrics, and artifacts |
| Deploy model/adapter | [mlr_workflow_deployModelAdapter.ipynb](../notebooks/mlr_workflow_deployModelAdapter.ipynb) | Infrastructure, serving image, AppConfig settings, model artifact |
| Deploy adapter update | [mlr_workflow_deployAdapter.ipynb](../notebooks/mlr_workflow_deployAdapter.ipynb) | Existing endpoint and new adapter |

Cells contain historical values: replace them before executing. `local=True` runs the MLRun handler locally, but training/evaluation handlers still launch SageMaker jobs.

The older [mlr_local_project_setup.ipynb](../notebooks/mlr_local_project_setup.ipynb) uses another project name/API port and includes a cascading project-deletion cell. Review it before execution; it is not the default first step for the checked-in `legalcontractextractor` project.

## Validation

With CI tools installed, the repository's lint command is:

```bash
flake8 src/ setup/ images/
```

It uses the exclusions in `.flake8`.

The test needs MLRun connectivity, AWS access, and a registered/built `build-check` function:

```bash
pytest --capture=no tests/test_unit.py
```

Despite its filename, this submits an MLRun job and is an integration smoke test, not an offline unit-test suite. Current CI stops before reaching it.

## Known constraints

- `setup/setup_project.py` exits when `project.yaml` exists, preventing `setup/setup.sh` from acting as a fresh-clone bootstrap.
- The build script has several function builds commented out and local paths that assume a notebook-style working directory.
- `setup/setup_data.py` contains a commented-out upload example rather than an active uploader.
- Notebook names, tags, addresses, repositories, and image references need environment-specific changes.
- Windows notebook examples import `torch` before `mlrun` to avoid a recorded runtime-loading issue.
- CI/CD stops before deployment because the intended platform target is not configured.
- API Gateway, trace export, and long-term rollback require additional work; see [architecture boundaries](architecture.md#implementation-boundaries).
