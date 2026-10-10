print('========================================')
print('Non quantised lora')
import os
from transformers import (
    AutoTokenizer,
    AutoModelForCausalLM,
    BitsAndBytesConfig,
    EarlyStoppingCallback,
    TrainingArguments,
    Trainer,
    #DataCollatorWithPadding, # It will pad input_ids and attention_mask to the longest sequence in the batch, but it does not know how to pad the labels column
    #DataCollatorForSeq2Seq  # this is unused because it doesn't work well, created a custom padder
)
from peft import (
    LoraConfig, 
    get_peft_model, 
    prepare_model_for_kbit_training
)

from datasets import load_from_disk
os.environ["PYTORCH_CUDA_ALLOC_CONF"] = "expandable_segments:True"

import torch
import boto3
import time
from huggingface_hub import snapshot_download
from requests.exceptions import RequestException
import pandas as pd
import json

print('✅ Import successful')

def download_model_safely(repo_id:str, local_dir:str, max_retries:int=5):
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
                local_dir=local_dir,
                ignore_patterns=["*.bin", "*.pt", "*.pth", "*.h5"],
                max_workers=4,
                token=hftoken
            )
            
            print(f"✅ Model downloaded successfully to: {local_path}")
            return local_path
            
        except (RequestException, ConnectionError, TimeoutError) as e:
            print(f"⚠️ Network error on attempt {attempt}: {e}")
            if attempt == max_retries:
                raise RuntimeError(f"Failed to download {repo_id} after {max_retries} attempts.")
            
            # Exponential backoff before retrying (e.g., 5s, 10s, 20s...)
            sleep_time = 5 * (2 ** (attempt - 1))
            print(f"Retrying in {sleep_time} seconds...")
            time.sleep(sleep_time)

MODEL_REPO = "JerroldK/Hermes-4-14B-contract-extractor" # Or "Qwen/Qwen3-14B-Instruct"
LOCAL_SAVE_PATH = '/opt/ml/code/local_hermes4_14b' #"./local_hermes4_14b"
hftoken = ""

download_model_safely(MODEL_REPO, LOCAL_SAVE_PATH)

tokenizer = AutoTokenizer.from_pretrained(LOCAL_SAVE_PATH,
                                          max_length=10000) # We ought not to do this. Checking if causes OOM 

bnb_config = BitsAndBytesConfig(
    load_in_4bit=True,
    bnb_4bit_quant_type="nf4",           # Best quantization type for QLoRA
    bnb_4bit_compute_dtype=torch.bfloat16, # Computation happens in bf16, supported by L40S
    bnb_4bit_use_double_quant=True        # Quantizes the quantization constants to save memory
)

# Flash attention can't be enabled in the sagemaker training environment because it requires --no-build-install
# attn_implementation="flash_attention_2"
model = AutoModelForCausalLM.from_pretrained(
    pretrained_model_name_or_path=LOCAL_SAVE_PATH,
    quantization_config=bnb_config,
    attn_implementation="flash_attention_2", # try changing to sdpa? for optimal conf?
    dtype=torch.bfloat16, # For any layer that is not quantized, load them in bfloat16 from the start
)

# disable KV cache
model.config.use_cache = False

print('✅ Model, flasth attention, and tokenizer load successful')

# Prepare the model for QLoRA
model = prepare_model_for_kbit_training(model,
                                        use_gradient_checkpointing=True)

target_modules=[
        "q_proj",
        "v_proj",
        "o_proj",
        "k_proj",
        "up_proj",
        "down_proj",
        "gate_proj",
    ]

lora_config = LoraConfig(
    r=16, # Rank of the adapter (higher = more capacity, more memory) Reduced from 16
    lora_alpha=32,
    target_modules="all-linear", # identical to all-linear, should be 64M trainable don't load lora twice
    lora_dropout=0.05,
    bias="none",
    task_type="CAUSAL_LM"
)
# Since we have very little training data (~420 samples), the strategy is to bump up the epochs and rely on the validation dataset and early stopping to stop training.
# Arguments for notebook run on 1x48GB
training_args = TrainingArguments(
    output_dir="./sft_checkpoints",

    per_device_train_batch_size=1,  # This cannot go any higher even with all the optimisations
    gradient_accumulation_steps=16,
    gradient_checkpointing=True,                                # https://huggingface.co/docs/transformers/v4.42.0/perf_train_gpu_one#gradient-checkpointing
    gradient_checkpointing_kwargs={"use_reentrant": False},     # Required for modern PyTorch versions

    fp16=False,   # higher precision. Better for Inference
    bf16=True,    # wider dynamic range. Better for training, supported by L40S
    optim="paged_adamw_8bit",         # Instead of aggregating optimizer states like Adafactor, 8-bit Adam keeps the full state and quantizes it. expect to get about a 3x memory improvement and even slightly higher throughput as using Adafactor.

    learning_rate=2e-4,               # QLoRA requires slightly higher LRs than full fine-tuning.
    lr_scheduler_type="cosine",       # Cosine decay yields better final performance than linear
    warmup_ratio=0.05,                # Warm up the LR over the first 5% of training steps
    num_train_epochs=1,

    dataloader_num_workers=1,         # Number of subprocesses to use for data loading (PyTorch only). 0 means that the data will be loaded in the main process.
    max_grad_norm=0.3,                # Clips gradients to prevent exploding gradients (standard for QLoRA). Recommended by HF https://huggingface.co/papers/2305.14314
    
    # --- EVALUATION ARGUMENTS ---
    # Evalutation dataset has 58 samples (<6000 tokens)
    logging_strategy="steps",
    logging_steps=10,
    eval_strategy="steps",            # Evaluate every N steps (can also use "epoch")
    eval_steps=25,                    # Evaluate every x steps (If 10, evaluates ~3 times per epoch for a batch size of 16 on 422 samples)
    eval_accumulation_steps=1,        # Number of predictions steps to accumulate the output tensors for, before moving the results to the CPU. If left unset, the whole predictions are accumulated on the device accelerator before being moved to the CPU (faster but requires more memory).
    per_device_eval_batch_size=1,
    prediction_loss_only=True,
    label_names=["labels"],    

    # --- CHECKPOINTS AND SAVING FOR EARLY STOPPING ---
    save_strategy="steps",            # Must match eval_strategy to save the model when evaluating
    save_steps=25,                    # Save every 10 steps
    save_total_limit=2,               # Only keep the 2 most recent checkpoints to save disk space
    load_best_model_at_end=True,      # Crucial: At the end of training, reload the weights with the lowest eval loss
    metric_for_best_model="eval_loss",# Determine the "best" model by validation loss
    greater_is_better=False,          # For loss, lower is better 
                 
    push_to_hub=False                   # Disabled here to grab the commit hash explicitly later            
)

# Apply the LoRA adapters to the model
model = get_peft_model(model, lora_config)
model.print_trainable_parameters()
print('✅ Peft adapters inserted into model')

# Enable gradient checkpointing to drastically reduce memory usage. No need to call this as already called in prepare_model_for_kbit_training()
#model.gradient_checkpointing_enable()

train_data = load_from_disk('s3://legal-llama-data/training/20260508_1434/train')
validation_data = load_from_disk('s3://legal-llama-data/training/20260508_1434/validation')

# Custom padding class because DataCollator has hidden behaviors causing bottlebecks at numpy conversion 

class InstructionTuningCollator:
    def __init__(self, tokenizer):
        self.tokenizer = tokenizer
        # Ensure tokenizer has a pad token
        if self.tokenizer.pad_token_id is None:
            self.tokenizer.pad_token_id = self.tokenizer.eos_token_id

    def __call__(self, batch):
        # 1. Convert lists/numpy arrays directly to individual PyTorch tensors
        input_ids = [torch.tensor(feature["input_ids"], dtype=torch.int64) for feature in batch]
        attention_mask = [torch.tensor(feature["attention_mask"], dtype=torch.int64) for feature in batch]
        labels = [torch.tensor(feature["labels"], dtype=torch.int64) for feature in batch]

        # 2. Pad them efficiently using native PyTorch
        input_ids_padded = torch.nn.utils.rnn.pad_sequence(
            input_ids, batch_first=True, padding_value=self.tokenizer.pad_token_id
        )
        attention_mask_padded = torch.nn.utils.rnn.pad_sequence(
            attention_mask, batch_first=True, padding_value=0
        )
        labels_padded = torch.nn.utils.rnn.pad_sequence(
            labels, batch_first=True, padding_value=-100
        )

        return {
            "input_ids": input_ids_padded,
            "attention_mask": attention_mask_padded,
            "labels": labels_padded
        }

data_collator = InstructionTuningCollator(tokenizer=tokenizer)

trainer = Trainer(
    model=model,
    args=training_args,
    train_dataset=train_data,
    eval_dataset=validation_data,
    data_collator=data_collator,
    callbacks=[EarlyStoppingCallback(early_stopping_patience=2)] # Stop if eval_loss doesn't improve for 2 evals in a row
)
print('✅ Data and trainer loaded')

print("Starting training...")
trainer.train()
print("Completed training ✅ ...")
# print("Exiting program")
# exit(0)

# 1. Save locally first
local_save_dir = "./final_lora_adapters"
trainer.save_model(local_save_dir)
print(f"Adapters saved locally to {local_save_dir}")

# 2. Save adapters and logs to S3
s3_save_path = "s3://legal-llama-data/training/20260508_1434/"
s3_bucket = "legal-llama-data"
s3_prefix = "training/20260508_1434/train_artifacts"

# This saves the adapter_config.json and the unquantized adapter_model.safetensors
s3_client = boto3.client('s3')
for root, dirs, files in os.walk(local_save_dir):
    for file in files:
        local_file_path = os.path.join(root, file)
        
        # Calculate the S3 key (path) relative to the local save dir
        relative_path = os.path.relpath(local_file_path, local_save_dir)
        s3_key = os.path.join(s3_prefix, relative_path)
        
        # Upload
        print(f"  -> Uploading {file} to s3://{s3_bucket}/{s3_key}")
        s3_client.upload_file(local_file_path, s3_bucket, s3_key)

# This saves the logs
log_history = trainer.state.log_history
log_data_json = json.dumps(log_history, indent=4)
s3_client = boto3.client('s3')

bucket_name = 'legal-llama-data'
s3_file_path = 'training/20260508_1434/model_logs/training_history.json'

s3_client.put_object(
    Bucket=bucket_name,
    Key=s3_file_path,
    Body=log_data_json,
    ContentType='application/json'
)
print(f"Successfully uploaded logs to s3://{bucket_name}/{s3_file_path}")

#print(f"Successfully pushed! Commit Hash: {commit_info.oid}")

loss_data = [log for log in log_history if "loss" in log]
df = pd.DataFrame(loss_data)
print(loss_data)
print(df)

print("Exiting program")
exit(0)