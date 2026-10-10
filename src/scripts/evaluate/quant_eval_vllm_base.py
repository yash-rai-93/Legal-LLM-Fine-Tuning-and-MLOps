import torch

from vllm import LLM, SamplingParams

# from vllm.lora.request import LoRARequest
from vllm.sampling_params import StructuredOutputsParams
from pydantic import BaseModel
from typing import List, Literal

from datasets import load_from_disk
from requests.exceptions import RequestException
from huggingface_hub import snapshot_download, HfApi

import argparse
import os
import time
import json
import boto3
import utils_evaluate_model as evaluate_model  # custom eval metrics

"""
https://docs.vllm.ai/en/stable/features/lora/

https://docs.vllm.ai/en/stable/features/structured_outputs/?h=structure#offline-inference
https://github.com/vllm-project/vllm/blob/main/examples/features/structured_outputs/structured_outputs_offline.py

"""

os.environ["PYTORCH_CUDA_ALLOC_CONF"] = "expandable_segments:True"
print("✅ Import successful")

parser = argparse.ArgumentParser()
parser.add_argument("--model_repo", type=str)
parser.add_argument("--model_revision", type=str)
parser.add_argument("--hftoken", type=str)
parser.add_argument("--key", type=str)
parser.add_argument("--prompt_temp", type=float)
parser.add_argument("--prompt_topp", type=float)
parser.add_argument("--prompt_max_tok", type=int)
parser.add_argument("--environment", type=str)

args, _ = parser.parse_known_args()

hftoken = args.hftoken
key = args.key
_temp = args.prompt_temp
_topp = args.prompt_topp
_max_tok = args.prompt_max_tok
environment = args.environment

BASE_MODEL_ID = args.model_repo
BASE_REVISION = args.model_revision  # "75875f970c359f89ad9e7d4dc86bf3c075c73c31"
BASE_MODEL_DIR = "./tmp/hermes4_base"


def download_model_safely(
    repo_id: str,
    local_dir: str,
    repo_revision,
    max_retries: int = 5,
):
    """
    Downloads a model from Hugging Face with built-in retry logic for network errors.
    """
    os.makedirs(local_dir, exist_ok=True)

    for attempt in range(1, max_retries + 1):
        try:
            print(f"Attempt {attempt}/{max_retries}: Downloading {repo_id}...")

            # Download the repository
            # We ignore deprecated .bin/.pt weights to save bandwidth,
            # prioritizing modern, safe .safetensors files.
            local_path = snapshot_download(
                repo_id=repo_id,
                revision=repo_revision,
                local_dir=local_dir,
                ignore_patterns=["*.bin", "*.pt", "*.pth", "*.h5"],
                max_workers=4,
                token=hftoken,
                force_download=True,
            )

            print(
                f"✅ Model downloaded successfully to: {local_path} via snapshot_download"
            )
            return local_path

        except (RequestException, ConnectionError, TimeoutError) as e:
            print(f"🔔 Network error on attempt {attempt}: {e}")
            if attempt == max_retries:
                raise RuntimeError(
                    f"Failed to download {repo_id} after {max_retries} attempts."
                )

            # Exponential backoff before retrying (e.g., 5s, 10s, 20s...)
            sleep_time = 5 * (2 ** (attempt - 1))
            print(f"Retrying in {sleep_time} seconds...")
            time.sleep(sleep_time)


download_model_safely(BASE_MODEL_ID, BASE_MODEL_DIR, BASE_REVISION)

print(f"Initializing vLLM Engine with base model: {BASE_MODEL_ID}...")

llm = LLM(
    model=BASE_MODEL_DIR,
    trust_remote_code=True,
    max_model_len=12000,
    # Batch size
    max_num_seqs=5,
    # --- FP8 Optimizations for L40S Architecture ---
    # KV Cache
    kv_cache_dtype="fp8",
    # weights and activations
    quantization="fp8",
    # # LoRA specific configurations
    # enable_lora=True,
    # max_loras=1,
    # max_lora_rank=64            # Maximum rank
)
print("✅ Model loaded in FP8")
# ------- JSON OUTPUT FORMAT ----------------


# This has to be hard-coded, long strings or quotes cannot be passed through args in a training job
class Hypothesis(BaseModel):
    hypothesis: str
    hypothesis_id: str
    label: Literal["entailment", "contradiction", "not_mentioned"]
    source_clause: str


class HypothesisList(BaseModel):
    Hypotheses: List[Hypothesis]


json_schema = HypothesisList.model_json_schema()
structured_outputs_params_json = StructuredOutputsParams(json=json_schema)

sampling_params = SamplingParams(
    temperature=_temp,
    top_p=_topp,
    max_tokens=_max_tok,
    structured_outputs=structured_outputs_params_json,
)

print("🔔 Sampling parameters defined")

# ----------------------------------------------------

# merged_model = peft_model.merge_and_unload() # pointer that points to base_model, no duplicate models in memory


# Import dataset using key
test_data = load_from_disk(
    f"s3://{environment}-mlops-bucket-haviv/notrain/{key}/test_data"
)
model_inputs = []
model_outputs = []
reference_outputs = []
for row in test_data:
    model_inputs.append(row["text"])  # string
    reference_outputs.append(row["inference"])  # list of dict

model_inputs = model_inputs
reference_outputs = reference_outputs
print("-----------------------------")
print(f"Len inputs: {len(model_inputs)} \nLen references: {len(reference_outputs)}")
print("-----------------------------")

# Generate inferences
model_raw_outputs = llm.generate(
    model_inputs,
    sampling_params,
    # lora_request=LoRARequest("eval_adapter", 1, adapter_dir)
)
for raw_output in model_raw_outputs:
    text = raw_output.outputs[0].text
    model_outputs.append(text)

print(f"🔔 Successfully generated {len(model_outputs)} responses!")

# If cannot be dumped to json drop that output
_model_outputs = []
_reference_outputs = []
for i, s in enumerate(model_outputs):
    try:
        print(f"Input length is: {len(model_inputs[i])}")
        print(f"Output length is : {len(s)}")
        dic_obj = json.loads(s)["Hypotheses"]
        if not dic_obj:
            print(f"Output at index {i} is empty")
        else:
            _model_outputs.append(dic_obj)
            _reference_outputs.append(reference_outputs[i])
    except json.JSONDecodeError:
        print(f"Output at index {i} is invalid")

print(
    f"✅ {len(_model_outputs)}/{len(model_outputs)} outputs are successful responses!"
)

# Calculate metrics
doc_metrics = []
for i in range(len(_model_outputs)):
    doc_metric = evaluate_model.document_level_metrics(
        _reference_outputs[i], _model_outputs[i]
    )
    doc_metrics.append(doc_metric)

datas_metrics = evaluate_model.dataset_level_metrics(doc_metrics)
print(datas_metrics)
print(f"✅ Data metrics successfully caclulated")

datas_json = json.dumps(datas_metrics, indent=4, default=str)
# Upload results to S3
bucket_name = f"{environment}-mlops-bucket-haviv"
s3_met_file_path = f"notrain/{key}/metrics.json"
s3_client = boto3.client("s3")

s3_client.put_object(
    Bucket=bucket_name,
    Key=s3_met_file_path,
    Body=datas_json,
    ContentType="application/json",
)
print(f"✅ Successfully uploaded metrics to s3://{bucket_name}/{s3_met_file_path}")

print("🔔Exiting program")
exit(0)
