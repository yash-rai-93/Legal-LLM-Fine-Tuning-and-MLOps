# Legal LLM Fine-Tuning & MLOps

**Legal contract analysis with Hermes-4-14B, QLoRA and an AWS model lifecycle.**

![Python 3.11](https://img.shields.io/badge/Python-3.11-3776AB?logo=python&logoColor=white)
![PyTorch](https://img.shields.io/badge/Training-PyTorch%20%2B%20PEFT-EE4C2C?logo=pytorch&logoColor=white)
![AWS](https://img.shields.io/badge/Cloud-AWS%20SageMaker-FF9900)
![MLRun](https://img.shields.io/badge/Orchestration-MLRun-00B4D8)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)

A pipeline for analyzing non-disclosure agreements: read a contract, classify **17 ContractNLI hypotheses**, and return a label with supporting clauses as structured JSON. The project connects dataset and prompt registration, parameter-efficient fine-tuning, evaluation, model registration, GPU serving and traffic configuration.

The engineering focus is the full model lifecycle: fitting a 14B model and long contract inputs into GPU memory, retaining experiment artifacts, deploying base models and adapters separately, and measuring serving behavior under concurrent requests.

**Explore:** [Results](#recorded-evaluation-results) · [Benchmarks](#gpu-serving-benchmarks) · [Architecture](#architecture) · [Training](#fine-tuning-and-memory-management) · [Setup](#setup-and-run) · [Documentation](#documentation)

## Project at a glance

| Area | Implementation |
| --- | --- |
| Task | Three-way NLI classification and supporting-clause extraction for NDAs |
| Dataset | Public [ContractNLI](https://stanfordnlp.github.io/contract-nli/), with 17 hypotheses per contract |
| Model family | [Hermes-4-14B](https://huggingface.co/NousResearch/Hermes-4-14B) |
| Fine-tuning | QLoRA/PEFT, 4-bit NF4 base weights, BF16 computation and PyTorch DDP |
| Training configuration | One SageMaker `ml.g6e.12xlarge` instance; four 48 GB L40S GPUs |
| Serving | SageMaker LMI/vLLM, FP8 configuration and LoRA inference components |
| MLOps | MLRun artifacts/jobs, S3, Hugging Face adapter revisions and model status labels |
| Traffic and monitoring | Lambda gateway, AppConfig strategies and CloudWatch metrics/alarms |
| Evidence retained | Training curves, recorded evaluation metrics, raw benchmark responses and load-test summaries |

This `main` branch contains the original GPU/MLRun implementation and historical experiment artifacts. Reproduction requires an AWS/MLRun environment; the repository is not a currently hosted public inference demo. [Implementation status](#implementation-status) distinguishes available code, recorded results and integrations still to complete.

## What the model returns

For each hypothesis, the output identifies whether the contract supports it, contradicts it or does not mention it. Supporting clauses make a classification easier to inspect.

From the [recorded sample input/output](<sample input output.txt>):

> 8. The undertakings in clauses 2 and 3 will continue in force indefinitely.

```json
{
  "Hypotheses": [
    {
      "hypothesis": "Some obligations of Agreement may survive termination of Agreement.",
      "hypothesis_id": "nda-19",
      "label": "entailment",
      "source_clause": "8. The undertakings in clauses 2 and 3 will continue in force indefinitely."
    }
  ]
}
```

This excerpt shows one of the 17 expected entries. The allowed labels are `entailment`, `contradiction` and `not_mentioned`; the prompt requests an empty `source_clause` for `not_mentioned`.

The intended use is structured contract review: turn a long NDA into a consistent set of decisions and evidence that a reviewer can examine.

## Architecture

### Platform responsibilities

![MLRun and AWS platform architecture](diagram/platformarch2.png)

*Platform design: MLRun manages jobs and metadata, while AWS supplies training, serving, storage and traffic configuration. Diagrammed integrations are subject to the implementation status below.*

| Layer | Responsibility | Code and configuration |
| --- | --- | --- |
| Data and prompts | Group contract annotations, register datasets and retain prompt/invocation versions | [Data handler](src/register_raw_dataset.py), [prompt notebook](notebooks/mlr_register_prompt.ipynb) |
| Training and evaluation | Launch SageMaker jobs, train adapters, generate predictions and calculate metrics | [Training workflow](src/evaluate_train.py), [base evaluation](src/evaluate_no_train.py), [GPU scripts](src/scripts/) |
| Model registry | Associate experiment metadata and adapter revisions with model artifacts | [Registration](src/register_model.py), [registry helpers](src/utils_model_registry.py) |
| Deployment | Deploy a base model and adapter, or update an adapter on an existing endpoint | [Model deployment](src/deploy_model.py), [adapter deployment](src/deploy_adapter.py) |
| Request handling | Load the prompt, select a destination and invoke the configured inference component | [Lambda gateway](images/lambda_gateway/model_gateway.py) |
| Infrastructure | Define S3, gateway Lambda, IAM, AppConfig and rollout monitoring | [Terraform](Terraform/) |

The base model and LoRA adapter have separate deployment references, so an adapter update can use an existing serving endpoint. AppConfig carries the gateway's destination and prompt settings. MLRun model labels describe deployment roles and lineage.

### Lifecycle design

![Dataset, training, evaluation, model registration, serving and feedback lifecycle](diagram/overview1.png)

*Lifecycle design, including future feedback/export and long-term rollback paths. Firehose/Langfuse export, API Gateway integration and EventBridge/Nuclio rollback are not complete in this branch.*

The implemented workflow entry points cover:

1. Process and register contract datasets.
2. Register a prompt template and invocation settings.
3. Evaluate the base model or fine-tune and evaluate an adapter.
4. Register the selected experiment as a model artifact.
5. Deploy a model/adapter or update an existing endpoint's adapter.
6. Validate outputs, update traffic configuration and emit monitoring signals.

## Dataset and prompt engineering

The [data preparation work](dataset_engineering/) and [registration handler](src/register_raw_dataset.py) transform hypothesis-level ContractNLI annotations into document-level training records:

- Normalize the label spelling to `not_mentioned`.
- Group each document's hypothesis IDs, labels and evidence into an `inference` list.
- Retain the contract text and join its annotated evidence into supporting-clause strings.
- Write Parquet datasets to S3 and register dataset artifacts with MLRun keys/tags.
- Register the system prompt and generation settings as a separate versioned artifact.

Training and evaluation retrieve dataset/prompt artifacts by key and tag. Adapter uploads retain a Hugging Face commit reference, while model registration links experiment parameters, metrics and outputs.

ContractNLI is a public dataset. The legacy handler's `origin="in_house"` field is a metadata issue to correct; it does not describe the dataset's actual provenance. Historical prompt/evaluator copies also need a single validated hypothesis mapping before a fresh comparison.

## Fine-tuning and memory management

![Training job inputs and resulting adapter, loss and evaluation artifacts](diagram/train1.png)

The [training workflow](src/evaluate_train.py) launches [train_multi.py](src/scripts/training/train_multi.py) through a SageMaker Hugging Face estimator. The configured job uses **one instance with four GPUs and DistributedDataParallel**: each process binds to its local GPU and loads a quantized model replica. Tensor/pipeline parallel training is not configured.

Contract prompts are reported at approximately **2,000–11,000 input tokens**. The training recipe combines several memory controls:

| Technique | Purpose |
| --- | --- |
| 4-bit NF4 and double quantization | Reduce frozen base-weight memory |
| QLoRA with PEFT | Train adapter parameters while retaining the quantized base |
| BF16 computation | Use reduced-precision computation supported by the configured GPUs |
| Gradient checkpointing | Reduce stored activations by recomputing them during backpropagation |
| Microbatch 1 and gradient accumulation | Limit memory per step while maintaining an effective batch |
| Paged 8-bit AdamW | Reduce optimizer-state memory |
| SDPA, dynamic padding and disabled training KV cache | Control attention, padding and activation overhead |
| Local-rank GPU mapping | Distribute replicas across the configured GPUs |

### Recorded recipe

| Parameter | Script default / configured value |
| --- | --- |
| Epoch limit | 5, subject to early stopping |
| Per-device batch size | 1 |
| Gradient accumulation | 16; overridden in the second notebook experiment |
| Effective batch at four GPUs and accumulation 16 | `1 × 16 × 4 = 64` |
| Learning rate | `2e-4`; workflow-overridable |
| LoRA rank / alpha / dropout | 16 / 32 / 0.05 |
| LoRA targets | `all-linear` |
| Schedule | Cosine; warmup ratio 0.05 |
| Validation and checkpoint interval | Every 6 optimizer steps |
| Checkpoint selection | Lowest validation loss; early-stopping patience 2 |

The training notebook also records an experiment with accumulation **8** and learning rate **1.25e-4**. These are experiment settings, not a guarantee that a particular historical metric belongs to that configuration. Spot capacity and S3 checkpoint paths are configured; interruption/resume behavior still needs separate validation.

### Training curves

| Recorded experiment 1 | Recorded experiment 2 |
| --- | --- |
| ![Training and validation loss for experiment 1](<diagram/scs/training_curve(1).png>) | ![Training and validation loss for experiment 2](<diagram/scs/training_curve(2).png>) |

Both figures show recorded training and validation loss over optimizer steps. They do not establish a controlled accuracy comparison or fully identify the dataset, prompt and adapter revisions behind each curve.

[Training settings, original metrics and serving configuration →](docs/training-and-evaluation.md)

## Recorded evaluation results

The repository preserves two evaluation summaries:

| Recorded evaluation | Scored documents | Mean label accuracy | Mean extraction score |
| --- | ---: | ---: | ---: |
| Hermes-4-14B baseline | 121 | **77.43%** | **0.6653** |
| Hermes-4-14B Fine-Tuned | 121 | **87.63%** | **0.8237** |


How to interpret the numbers:

- Label accuracy is averaged across scored documents. The legacy evaluator uses each response's generated hypothesis count, so completeness and uniqueness need validation.
- The extraction score uses **ROUGE-3 evidence overlap**, with a special score of `1.0` for correct `not_mentioned` cases. It is not classification F1 or an evidence-only average.
- Malformed JSON and empty predictions are omitted by the historical GPU evaluation scripts. Scored counts should therefore be read alongside attempted and excluded counts.
- ROUGE overlap does not establish that evidence is an exact contiguous quote or that a prediction is legally correct.

The [evaluation report](docs/training-and-evaluation.md#reported-evaluation-results) retains the original values. [Metric definitions](<docs/model metrics.md>) explain the denominators and special cases.

## GPU serving benchmarks

The [benchmark directory](benchmark/) contains historical SageMaker serving load tests, prompt payloads and raw model responses. The runner uses `awscurl` to invoke an endpoint with an adapter inference component.

| Concurrent clients | Requests | Failed | Error rate | Mean latency | p99 latency | Reported token throughput |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| [4 clients](benchmark/benchmark_summary_4_client_test.json) | 40 | 2 | 5.00% | 46.59 s | 61.54 s | 43.96 tokens/s |
| [7 clients](benchmark/benchmark_summary_7_client_test.json) | 35 | 4 | 11.43% | 161.09 s | 300.24 s | 22.25 tokens/s |

An additional [hello-world summary](benchmark/benchmark_summary_helloworld.json) is retained separately and is not included in the contract-load table.

Latencies are converted from the summaries' milliseconds. Throughput is the runner's `tokenThroughput` field; the separate `tps` field must not be interpreted as tokens per second.

The seven-client run recorded higher latency, more failures and lower throughput. These exploratory runs have different request counts and do not isolate concurrency as the only changed variable. They do not establish an SLA or validate autoscaling, and the summaries do not fully pin the GPU instance and model/adapter revisions.

| Benchmark artifact | Contents |
| --- | --- |
| [4-client summary](benchmark/benchmark_summary_4_client_test.json) | Request/failure counts, latency percentiles and token metrics |
| [7-client summary](benchmark/benchmark_summary_7_client_test.json) | Equivalent measurements at seven clients |
| [4-client raw outputs](benchmark/4_client_test_output/) | Saved request inputs, responses and errors |
| [7-client raw outputs](benchmark/7_client_test_output/) | Saved request inputs, responses and errors |
| [Prompt payloads](benchmark/prompts/) | Inputs used by the runner |
| [Runner](benchmark/benchmark.sh) | Historical endpoint invocation and load-test arguments |

<details>
<summary>Read a saved benchmark summary without running a model</summary>

The summary files contain a leading log line before the JSON:

```python
import json
from pathlib import Path

path = Path("benchmark/benchmark_summary_4_client_test.json")
text = path.read_text()
summary = json.loads(text[text.index("{"):])
print(json.dumps(summary, indent=2))
```

</details>

## Serving, deployment and monitoring

![SageMaker model serving with Lambda, AppConfig, S3 and CloudWatch](diagram/serving1.png)

*Serving design. The gateway and SageMaker helpers are implemented; public API Gateway provisioning/event mapping and the depicted trace-export integrations require additional work.*

The [serving helpers](src/utils.py) configure SageMaker LMI with an asynchronous vLLM backend, FP8 weight/KV-cache settings, LoRA support and inference-component references. Autoscaling configuration is present, although the retained benchmarks do not verify its behavior.

The [gateway](images/lambda_gateway/model_gateway.py):

1. Accepts a top-level `contract` field.
2. Reads destination/prompt configuration through the AppConfig Lambda extension.
3. Loads the S3 prompt and constructs the model request.
4. Invokes the chosen SageMaker inference component.
5. Parses the hypotheses and emits logs, label counts and quote-overlap metrics.

The handler expects a Lambda event shaped like the following; an HTTP API needs an appropriate event mapping:

```json
{
  "contract": "Full NDA text..."
}
```

### Model and adapter releases

[Model deployment](src/deploy_model.py) creates a model/adapter serving path; [adapter deployment](src/deploy_adapter.py) targets an existing endpoint. The helpers include pre-deployment evaluation and model registry roles:

| Registry label | Meaning |
| --- | --- |
| `standby` | Registered model outside the active champion/challenger roles |
| `challenger` | Candidate for promotion |
| `champion` | Model designated as active |
| `replaced` | Lineage label referencing the replacement version |

AppConfig provides direct and gradual deployment strategies and alarm-based rollout monitoring. End-to-end rollout completion, rollback and registry reconciliation still require validation; the presence of these helpers is not a claim of a production-tested release system.

<details>
<summary>Deployment diagrams and historical registry screenshot</summary>

![AppConfig, Lambda and CloudWatch rollout configuration](diagram/modeldep1.png)

*Configuration relationship between traffic routing and rollout monitoring.*

![New model and adapter deployment](diagram/deploy1.png)

*Model-plus-adapter deployment design.*

![Adapter update on an existing endpoint](diagram/deploy2.png)

*Adapter-only deployment design.*

![Recorded MLRun model registry UI](diagram/scs/ui3.png)

*Historical repository screenshot of model artifact versions and metadata.*

</details>

### Monitoring boundaries

The gateway emits per-hypothesis label counts and a `CARP_3` score: ROUGE-3 precision comparing generated evidence with the input contract. These are distribution/quote-overlap signals, not ground-truth production accuracy. Input mix can change them.

The long-term alarm helper's `CARF_3` naming differs from the gateway's `CARP_3` and needs alignment. The intended Firehose/Langfuse feedback path and CloudWatch → EventBridge → Lambda → Nuclio rollback chain remain incomplete.

[Architecture, observability and implementation boundaries →](docs/architecture.md)

## Setup and run

This section describes the **GPU/MLRun path on `main`**. Browsing its reports and figures needs no AWS resources. Reproduction requires your own configured AWS resources, compatible images and a reachable MLRun target.

Training, GPU evaluation and serving deployment can incur GPU charges. A notebook's `local=True` runs the orchestration handler on its host; that handler can still launch a SageMaker GPU job.

### 1. Prepare the orchestration environment

Use Python **3.11**, MLRun **1.11.0**, AWS CLI, Docker and Terraform, plus access to the selected AWS region and SageMaker GPU quota. Execute the setup in a configured development environment; an AWS-hosted environment can be used when laptop execution is unsuitable.

```bash
git clone https://github.com/yash-rai-93/Legal-LLM-Fine-Tuning-and-MLOps.git
cd Legal-LLM-Fine-Tuning-and-MLOps
git switch main

python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

On Windows PowerShell, activate with `.venv\Scripts\Activate.ps1`; Bash scripts require a compatible shell. The host dependencies and GPU container dependencies are separate. Dependency installation does not create an MLRun control plane or AWS infrastructure.

### 2. Adapt configuration to your environment

| Setting | Used for |
| --- | --- |
| `ENV`, `ACCOUNT_ID`, `IMAGE_TAG` | Environment names and image/resource references |
| `AWS_REGION` | AWS SDK/CLI region; much of the checked-in code assumes `us-east-1` |
| `MLRUN_AWS_ROLE_ARN` | SageMaker execution role |
| `HF_TOKEN` | Model access and adapter-repository writes |
| `FEATURE_BRANCH` | Source branch selected by the function builder |
| `MLRUN_DBPATH` | MLRun connectivity/environment selection |
| `AWS_ENDPOINT_URL_S3` | S3 endpoint used by selected jobs |

Use appropriate AWS authentication and keep secrets out of Git. Existing job builders also expect Kubernetes secret/image-pull configuration; review [the setup guide](docs/getting-started.md#configuration).

Update the source repository and ECR references in [project.yaml](project.yaml) and [build_functions.py](setup/build_functions.py), the Terraform backend/environment values, and the model/adapter repositories, S3 paths and image references in the workflow code. **Changing only `ENV` and `ACCOUNT_ID` does not replace all historical values.**

### 3. Prepare infrastructure and images

Connect a reachable MLRun environment and prepare its job credentials/image-pull configuration. The examples commonly use `http://localhost:30070`; replace it with the correct reachable endpoint for your environment.

Review [Terraform/](Terraform/) and create your own backend key and variable file. The backend bucket must already exist and the configured Lambda image must be available in ECR. Once those prerequisites and references are configured, the review sequence is:

```bash
# Replace these filenames with your reviewed environment-specific files.
terraform -chdir=Terraform init -backend-config=backend/YOUR_ENV.hcl
terraform -chdir=Terraform validate
terraform -chdir=Terraform plan -var-file=YOUR_ENV.tfvars -out=reviewed.tfplan
```

Terraform defines S3, IAM, the Lambda gateway, AppConfig and a CloudWatch alarm; the Python workflows create SageMaker jobs/endpoints. This Terraform stack does not itself provision all GPU resources.

Build the required images under [images/](images/) and publish them to your ECR. The Lambda image expects the AppConfig extension; [its download helper](images/lambda_gateway/download_appconfig_layer.sh) and [image builder](images/build_ecr.sh) need review before building. The SageMaker GPU-image build is separate/commented out in the builder.

Several function builds in [setup/build_functions.py](setup/build_functions.py) are disabled. Enable and configure the functions needed for your selected workflow, then register them with names consistent with `project.yaml` and the calling notebooks.

### 4. Run the selected notebooks in order

Use `notebooks/` as the working directory: estimator source paths and project loading use relative paths. Review cells individually; historical data/prompt notebooks include debugging and artifact-deletion cells.

| Step | Notebook | Expected artifact or result |
| --- | --- | --- |
| Register functions | [mlr_local_register_funcs.ipynb](notebooks/mlr_local_register_funcs.ipynb) | Registered handlers with reviewed names/images |
| Prepare/register data | [mlr_workflow_register_raw_data.ipynb](notebooks/mlr_workflow_register_raw_data.ipynb) | S3 Parquet and tagged MLRun datasets |
| Register prompt | [mlr_register_prompt.ipynb](notebooks/mlr_register_prompt.ipynb) | Prompt template and invocation configuration |
| Evaluate base model | [mlr_workflow_evalutateModel_noTrain.ipynb](notebooks/mlr_workflow_evalutateModel_noTrain.ipynb) | Generated predictions and evaluation metrics |
| Fine-tune/evaluate adapter | [mlr_workflow_evalutateModel_Train.ipynb](notebooks/mlr_workflow_evalutateModel_Train.ipynb) | Adapter revision, loss history and evaluation outputs |
| Register model | [mlr_register_model.ipynb](notebooks/mlr_register_model.ipynb) | Model artifact linked to a selected experiment |
| Deploy model and adapter | [mlr_workflow_deployModelAdapter.ipynb](notebooks/mlr_workflow_deployModelAdapter.ipynb) | Endpoint and inference-component references |
| Update an adapter | [mlr_workflow_deployAdapter.ipynb](notebooks/mlr_workflow_deployAdapter.ipynb) | Adapter update on the selected existing endpoint |

Replace historical artifact tags, experiment UIDs and endpoint/component references with your own outputs. The function-registration notebook has naming differences from `project.yaml`; resolve them before launching a workflow.

### 5. Run a serving benchmark

After deploying and verifying your own endpoint, set `AWS_REGION`, `ENDPOINT_NAME`, `ADAPTER_COMPONENT_NAME` and `TOKENIZER` for that release. From the repository root:

```bash
cd benchmark
chmod +x ./awscurl

./awscurl -c 4 -N 10 -X POST \
  -n sagemaker \
  "https://runtime.sagemaker.${AWS_REGION}.amazonaws.com/endpoints/${ENDPOINT_NAME}/invocations" \
  --connect-timeout 900 \
  --dataset prompts \
  -H 'Content-Type: application/json' \
  -H "X-Amzn-SageMaker-Inference-Component: ${ADAPTER_COMPONENT_NAME}" \
  -P -t -o output.txt > benchmark_summary.json
```

This launches 40 requests using four concurrent clients. The checked-in [runner](benchmark/benchmark.sh) instead contains a historical seven-client/five-request configuration and old destination references.

Keep new output files separate from the historical evidence. Record the source commit, instance/container, model/adapter revisions, prompt/dataset identity and generation settings with each new run.

### 6. Validate the integration

With the tools from `requirements_ci.txt` installed, the repository's existing commands are:

```bash
flake8 src/ setup/ images/
pytest --capture=no tests/test_unit.py
```

The test requires MLRun/AWS access and a built/registered `build-check` function. Despite its filename, it submits a remote MLRun integration job; it is not an offline unit-test suite. Lint coverage follows the exclusions in [the configuration](.flake8).

**Current setup constraints:** `setup/setup_project.py` exits when the checked-in `project.yaml` exists, so `setup/setup.sh` is not a fresh-clone bootstrap. Existing notebooks contain environment-specific values and cleanup cells. The GitHub CI/CD definitions stop before deployment because their MLRun target is not configured. The [setup guide](docs/getting-started.md) documents these constraints in more detail.

## Implementation status

| Area | Available in this branch | Remaining validation or implementation |
| --- | --- | --- |
| Data and prompt registration | Transformation handler, Parquet artifacts and MLRun prompt notebooks | Centralize hypothesis definitions and correct legacy source metadata |
| QLoRA training/evaluation | SageMaker workflows, DDP/PEFT code, curves and recorded metrics | Complete run provenance and a controlled comparison on identical evaluated documents |
| GPU serving | LMI/vLLM helpers, adapter deployment and recorded load tests | Fresh environment reproduction, full token-budget checks and scaling validation |
| Traffic changes and rollout monitoring | AppConfig strategies, alarm resource and deployment helpers | Validate initial deployment, rollout completion and rollback/registry reconciliation |
| Monitoring | Gateway logs, label counts and quote-overlap metrics | Align metric names and calibrate thresholds |
| Public inference API | Gateway handler and target architecture | API Gateway resources/event mapping |
| Feedback and long-term rollback | Design diagrams and partial helpers | Trace delivery and complete EventBridge/Nuclio action chain |
| CI/CD | GitHub workflow definitions | Configure the MLRun target, repair stopping steps and enable required builds |

The separate [AWS CPU deployment branch](https://github.com/yash-rai-93/Legal-LLM-Fine-Tuning-and-MLOps/tree/feat/aws-cpu-deployment) contains the verified data-processing workflow, run/artifact APIs and monitoring documented during the CPU deployment work. It remains unmerged; it does not establish that the GPU/MLRun stack is currently deployed. CPU Terraform adoption was paused before state import/apply.

## Repository map

| Directory | Purpose |
| --- | --- |
| [dataset_engineering/](dataset_engineering/) | ContractNLI conversion, prepared data and exploratory work |
| [src/](src/) | MLRun handlers, registration, deployment and shared helpers |
| [src/scripts/training/](src/scripts/training/) | GPU fine-tuning entry point |
| [src/scripts/evaluate/](src/scripts/evaluate/) | Base/adapter GPU evaluation scripts |
| [notebooks/](notebooks/) | Registration, experiment and deployment walkthroughs |
| [images/](images/) | MLRun function, SageMaker training and Lambda gateway containers |
| [Terraform/](Terraform/) | AWS supporting infrastructure and backend examples |
| [benchmark/](benchmark/) | Load-test runner, prompts, raw responses and summaries |
| [diagram/](diagram/) | Architecture figures, loss curves and historical UI screenshots |
| [docs/](docs/) | Setup, architecture, metrics and experiment explanations |
| [setup/](setup/) | Project setup and function-image build scripts |
| [tests/](tests/) | MLRun/AWS integration smoke test |
| [k8s/](k8s/) | MLRun installation helpers and local-service notes |

## Documentation

- [Setup and workflow guide](docs/getting-started.md)
- [Architecture, deployment and observability](docs/architecture.md)
- [Training, evaluation and serving benchmarks](docs/training-and-evaluation.md)
- [Model and monitoring metric definitions](<docs/model metrics.md>)
- [Lambda gateway notes](<docs/Lambda gateway docs.md>)

<details>
<summary>More architecture figures</summary>

- [Alternative platform considered](diagram/platformarch1.png)
- [MLRun project components](diagram/mlrun1.png) and [orchestration architecture](diagram/mlrunarch1.png)
- [Data-store and feedback design](diagram/datastore1.png)
- [CI build design](diagram/cicd1.png) and [CD deployment design](diagram/cicd2.png)
- [Rollout rollback design](diagram/deploy3.png)
- [Observability/export design](diagram/observe1.png)

These figures include planned integrations. Upstream MLRun illustration sources are recorded in [diagram/official/references.txt](diagram/official/references.txt).

</details>

## Dataset, model and license

The task and annotations come from **ContractNLI** by Yuta Koreeda and Christopher D. Manning. The selected base-model family is **NousResearch/Hermes-4-14B**; configured model copies and adapter repositories must be reviewed for your own environment.

Repository code is provided under the [MIT License](LICENSE). Retain its copyright and license notice. Dataset and model use remain subject to their respective upstream terms.
