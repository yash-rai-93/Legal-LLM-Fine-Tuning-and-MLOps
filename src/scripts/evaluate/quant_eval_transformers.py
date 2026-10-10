import torch
from transformers import (
    AutoTokenizer,
    AutoModelForCausalLM,
    #FbgemmFp8Config, need H series gpu
    BitsAndBytesConfig
)
from peft import PeftModel
from datasets import load_from_disk
from requests.exceptions import RequestException
from huggingface_hub import snapshot_download, HfApi

import argparse
import os
import time
import json
import boto3
import utils_evaluate_model as evaluate_model # custom eval metrics

os.environ["PYTORCH_CUDA_ALLOC_CONF"] = "expandable_segments:True"
print('✅ Import successful')

base_model_id = "JerroldK/Hermes-4-14B-contract-extractor"
base_revision = "75875f970c359f89ad9e7d4dc86bf3c075c73c31"
base_model_dir = "./tmp/hermes4_base"
adapter_id = "JerroldK/H4-14b-contract-extractor-adaptor"
adapter_revision = "04458c54eb2f0387991b17a08d458aa1d196813b"
adapter_dir = "./tmp/hermes4_adapter"
hftoken=os.environ['HF_TOKEN']

key = "20260527_1501"

def download_model_safely(repo_id:str, local_dir:str, repo_revision, max_retries:int=5):
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
                    force_download=True
                )
                
                print(f"✅ Model downloaded successfully to: {local_path} via snapshot_download")
                return local_path
                
            except (RequestException, ConnectionError, TimeoutError) as e:
                print(f"⚠️ Network error on attempt {attempt}: {e}")
                if attempt == max_retries:
                    raise RuntimeError(f"Failed to download {repo_id} after {max_retries} attempts.")
                
                # Exponential backoff before retrying (e.g., 5s, 10s, 20s...)
                sleep_time = 5 * (2 ** (attempt - 1))
                print(f"Retrying in {sleep_time} seconds...")
                time.sleep(sleep_time)
            
download_model_safely(base_model_id, base_model_dir, base_revision)
download_model_safely(adapter_id, adapter_dir, adapter_revision)

int8_config = BitsAndBytesConfig(
    load_in_8bit=True
)
model = AutoModelForCausalLM.from_pretrained(
    pretrained_model_name_or_path=base_model_dir,
    device_map="auto",
    quantization_config=int8_config,
    dtype=torch.float16,
    attn_implementation='sdpa', #"flash_attention_2",
)
devices = {p.device for p in model.parameters()}
print(devices)
print("✅ Base model loaded in INT8")

print("Loading LoRA adapter...")
#model = model.load_adapter(adapter_dir)
model = PeftModel.from_pretrained(
     model,
     adapter_dir
)
model.eval()
model.config.use_cache = True


# merged_model = peft_model.merge_and_unload() # pointer that points to base_model, no duplicate models in memory
print("✅ LoRA adapters loaded")

tokenizer = AutoTokenizer.from_pretrained(base_model_dir)

# Import dataset
test_data = load_from_disk(f's3://legal-llama-data/training/20260526_1929/test')
model_inputs = []
model_outputs = []
reference_outputs = []
for row in test_data:
     model_inputs.append(row['text']) # string
     reference_outputs.append(row['inference']) # list of dict
print('-----------------------------')
print(f"Len inputs: {len(model_inputs)} \nLen references: {len(reference_outputs)}")
print('-----------------------------')
# Generate inferences
"""
Using left-padding with default pad-token, for batch inferences (subject to GPU's memory capacity).
Using inference_mode which has better optimisation than no_grad.
Decoder-only autoregressive models generate from the rightmost tokens, Left padding keeps the most recent tokens aligned.
KV cache alignment, batched generation, attention masking, efficient inference
"""
tokenizer.padding_side = "left"
if tokenizer.pad_token is None:
    tokenizer.pad_token = tokenizer.eos_token

model.config.pad_token_id = tokenizer.pad_token_id

invoc_config = {
    'do_sample': True,
    'temperature': 0.2,
    'top_p': 0.95,
    'max_new_tokens': 1500,
    'eos_token_id': tokenizer.eos_token_id
 #'stop': ['<|im_end|>', '<|im_start|>'],
 #'output_format': "json" # 'response_format': {'type': 'json_object'}
}

batch_size = 3
totalsize = len(model_inputs)

with torch.inference_mode():
    for i in range(0, totalsize, batch_size): # len(list_of_messages)
        formatted_batch_messages = model_inputs[i : i + batch_size]
        
        # Tokenize the batch simultaneously with padding=True
        inputs = tokenizer(formatted_batch_messages, padding=True, return_tensors="pt").to("cuda")

        # print(inputs["input_ids"].shape) # torch.Size([batch_size, maximum_size])
        # print(inputs["attention_mask"].shape)
        # print(inputs["input_ids"].dtype)
        
        # Generate for the whole batch. The output should be a list of json? Need to check.
        outputs = model.generate(
            **inputs,
            **invoc_config
        )
        
        # 3. Extract ONLY the new tokens
        # Because we used left-padding, the prompt tokens are pushed right. 
        # The new text starts exactly at the end of the input_ids matrix width.
        prompt_length = inputs["input_ids"].shape[-1]
        
        for out in outputs:
            generated_tokens = out[prompt_length:]
            response_text = tokenizer.decode(generated_tokens, skip_special_tokens=True)
            model_outputs.append(response_text)
        print(f"✅ {i + batch_size} inferences completed")

print(f"⚠️ Successfully generated {len(model_outputs)} responses!")
# If cannot be dumped to json drop that output
_model_outputs = []
_reference_outputs = []

for i, s in enumerate(model_outputs):
    try:
        dic_obj = json.loads(s)
        _model_outputs.append(dic_obj)
        _reference_outputs.append(reference_outputs[i])
    except json.JSONDecodeError:
        print(f"Output at index {i} is invalid")
print(f"✅ {len(_model_outputs)}/{len(model_outputs)} outputs are successful responses!")

# Calculate metrics
doc_metrics = [evaluate_model.document_level_metrics(
    _reference_outputs[i],
    _model_outputs[i]
    ) for i in range(len(_model_outputs))
]
datas_metrics = evaluate_model.dataset_level_metrics(doc_metrics)
print(datas_metrics)
print(f"✅ Data metrics successfully caclulated")

datas_json = json.dumps(datas_metrics, indent=4, default=str)
# Upload results to S3
bucket_name = 'legal-llama-data'
s3_met_file_path = f'training/{key}/evaluation/metrics.json'
s3_client = boto3.client('s3')

s3_client.put_object(
    Bucket=bucket_name,
    Key=s3_met_file_path,
    Body = datas_json,
    ContentType='application/json'
)
print(f"✅ Successfully uploaded logs to s3://{bucket_name}/{s3_met_file_path}")
print("⚠️Exiting program")
exit(0)
