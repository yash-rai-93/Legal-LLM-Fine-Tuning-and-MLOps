"""
This is a QLoRA training script for very long data sequences in chatml format (up to 11k tokens) for a qwen3 model designed to SAVE AS MUCH MEMORY AS POSSIBLE to run on 4 cheap L40S GPUs (no fancy A100/H100). It uses Transformers/Pytorch and runs on a sagemaker training job with PyTorch distributed as the backend instead of SMP (which is highly outdated and not compatible for this version of transformers).

References:
https://github.com/huggingface/notebooks/blob/main/sagemaker/01_getting_started_pytorch/sagemaker-notebook.ipynb
https://github.com/huggingface/notebooks/blob/main/sagemaker/05_spot_instances/sagemaker-notebook.ipynb
https://github.com/huggingface/notebooks/blob/main/sagemaker/24_train_bloom_peft_lora/scripts/run_clm.py

https://huggingface.co/docs/sagemaker/tutorials/sagemaker-sdk/training-sagemaker-sdk
https://huggingface.co/docs/transformers/main_classes/trainer
https://huggingface.co/docs/transformers/v4.42.0/perf_train_gpu_one#methods-and-tools-for-efficient-training-on-a-single-gpu
https://huggingface.co/docs/transformers/v4.24.0/en/perf_train_gpu_one#efficient-training-on-a-single-gpu


Additional for hyperparameter tuning:
https://huggingface.co/docs/transformers/v5.7.0/en/hpo_train?backends=Optuna#define-the-search-space

"""

print("========================================")

from transformers import (
    AutoTokenizer,
    AutoModelForCausalLM,
    BitsAndBytesConfig,
    EarlyStoppingCallback,
    TrainingArguments,
    Trainer,
    # DataCollatorWithPadding, # It will pad input_ids and attention_mask to the longest sequence in the batch, but it does not know how to pad the labels column which we use for instruction masking
    # DataCollatorForSeq2Seq  # this is unused because it doesn't work well/bugged in this version of transformers, created a custom padder
)
from peft import (
    LoraConfig,
    get_peft_model,
    prepare_model_for_kbit_training,
)

# from accelerate import unwrap_model
from datasets import load_from_disk
import torch
import torch.nn.functional as F
import boto3
from huggingface_hub import snapshot_download, HfApi
from requests.exceptions import RequestException
import pandas as pd
import json
import time
import os
import argparse

os.environ["PYTORCH_CUDA_ALLOC_CONF"] = "expandable_segments:True"
print("✅ Import successful")

# Parse arguments
parser = argparse.ArgumentParser()
parser.add_argument("--model_repo", type=str)
parser.add_argument("--model_revision", type=str)
parser.add_argument("--hftoken", type=str)
parser.add_argument("--epochs", type=int, default=5)
parser.add_argument("--batch_grad_accumulation", type=int, default=16)
parser.add_argument("--learning_rate", type=float, default=2e-4)
parser.add_argument("--lora_r", type=int, default=16)
parser.add_argument("--lora_alpha", type=int, default=32)
parser.add_argument("--early_stopping_threshold", type=float, default=1e-3)
parser.add_argument("--key", type=str)
parser.add_argument("--environment", type=str)

args, _ = parser.parse_known_args()

MODEL_REPO = args.model_repo
REVISION = args.model_revision
hftoken = args.hftoken
epochs = args.epochs
batch_grad_accumulation = args.batch_grad_accumulation
learning_rate = args.learning_rate
lora_r = args.lora_r
lora_alpha = args.lora_alpha
early_stopping_threshold = args.early_stopping_threshold
key = args.key
environment = args.environment

ADAPTER_REPO = "JerroldK/H4-14b-contract-extractor-adapter"
LOCAL_SAVE_PATH = "/opt/ml/code/local_hermes4_14b"  # "./local_hermes4_14b"
LOCAL_LORA_SAVE_DIR = "./final_lora_adapters"

# Distributed variables
local_rank = int(os.environ.get("LOCAL_RANK", "0"))
global_rank = int(os.environ.get("RANK", "0"))
is_main_process = global_rank == 0

# Bind the process to the specific GPU. For pausing
torch.cuda.set_device(local_rank)

##########################################################################################
# TRAINING

# Download dataset and tokenised data into memory (all processes do this)
# Truncation should be done in preprocessing
tokenizer = AutoTokenizer.from_pretrained(MODEL_REPO, token=hftoken)

train_data = load_from_disk(
    f"s3://{environment}-mlops-bucket-haviv/training/{key}/train"
)
validation_data = load_from_disk(
    f"s3://{environment}-mlops-bucket-haviv/training/{key}/validation"
)

print("✅ Tokenizer and data load successful")

# Since we have very little training data (~420 samples), the strategy is to bump up the epochs and rely on the validation dataset and early stopping to stop training. Keep batch size low 32-64 since there isn't much data
# Arguments for notebook run on 1x48GB

# normal is 3 for 5 epochs with large data. 1 for 1 epoch and small data
# Change this in accordance with batch size
EVAL_STEPS = 6
print(f"EVAL_STEPS: {EVAL_STEPS}")

training_args = TrainingArguments(
    output_dir="./sft_checkpoints",
    per_device_train_batch_size=1,  # This is multiplied by 4 for 4 GPUs. This cannot go any higher than 1 even with all the memory optimisations, tt to process batchsize 1 = tt to process batchsize 2.
    gradient_accumulation_steps=batch_grad_accumulation,
    gradient_checkpointing=True,  # https://huggingface.co/docs/transformers/v4.42.0/perf_train_gpu_one#gradient-checkpointing
    gradient_checkpointing_kwargs={
        "use_reentrant": False
    },  # Required for modern PyTorch versions
    fp16=False,  # higher precision. Better for Inference
    bf16=True,  # wider dynamic range. Better for training, supported by L40S
    optim="paged_adamw_8bit",  # Instead of aggregating optimizer states like Adafactor, 8-bit Adam keeps the full state and quantizes it. expect to get about a 3x memory improvement and even slightly higher throughput as using Adafactor.
    learning_rate=learning_rate,  # QLoRA requires slightly higher LRs than full fine-tuning.
    lr_scheduler_type="cosine",  # Cosine decay yields better final performance than linear
    warmup_ratio=0.05,  # Warm up the LR over the first 5% of training steps
    num_train_epochs=epochs,
    dataloader_num_workers=1,  # Number of subprocesses to use for data loading (PyTorch only). 0 means that the data will be loaded in the main process.
    max_grad_norm=0.3,  # Clips gradients to prevent exploding gradients (standard for QLoRA). Recommended by HF https://huggingface.co/papers/2305.14314
    # --- EVALUATION ARGUMENTS ---
    # No. steps = 422 * 3 / (1*16*4) = 19
    # Evalutation dataset has 58 samples (<6000 tokens)
    logging_strategy="steps",
    logging_steps=EVAL_STEPS,  # This tracks the loss of the training data and appears in the log history
    eval_strategy="steps",  # Evaluate every N steps (can also use "epoch")
    eval_steps=EVAL_STEPS,  # Evaluate every x steps (divide data_size*epochs by eff_batch_size)
    eval_accumulation_steps=1,  # Number of predictions steps to accumulate the output tensors for, before moving the results to the CPU. If left unset, the whole predictions are accumulated on the device accelerator before being moved to the CPU (faster but requires more memory).
    per_device_eval_batch_size=1,
    prediction_loss_only=True,
    label_names=["labels"],
    # --- CHECKPOINTS AND SAVING FOR EARLY STOPPING ---
    save_strategy="steps",  # Must match eval_strategy to save the model when evaluating
    save_steps=EVAL_STEPS,  # Save every x steps
    save_total_limit=2,  # Only keep the 2 most recent checkpoints to save disk space
    load_best_model_at_end=True,  # Crucial: At the end of training, reload the weights with the lowest eval loss
    metric_for_best_model="eval_loss",  # Determine the "best" model by validation loss
    greater_is_better=False,  # For loss, lower is better
    push_to_hub=False,  # Disabled here to grab the commit hash explicitly later
    ddp_find_unused_parameters=False,
)

# Download the model into disk ONCE, pauses the execution here on all processes before moving on
with training_args.main_process_first(desc="Model Download"):
    if is_main_process:

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
                    )

                    print(
                        f"✅ Model downloaded successfully to: {local_path} via snapshot_download"
                    )
                    return local_path

                except (RequestException, ConnectionError, TimeoutError) as e:
                    print(f"⚠️ Network error on attempt {attempt}: {e}")
                    if attempt == max_retries:
                        raise RuntimeError(
                            f"Failed to download {repo_id} after {max_retries} attempts."
                        ) from e

                    # Exponential backoff before retrying (e.g., 5s, 10s, 20s...)
                    sleep_time = 5 * (2 ** (attempt - 1))
                    print(f"Retrying in {sleep_time} seconds...")
                    time.sleep(sleep_time)

        download_model_safely(MODEL_REPO, LOCAL_SAVE_PATH, REVISION)

bnb_config = BitsAndBytesConfig(
    load_in_4bit=True,
    bnb_4bit_quant_type="nf4",  # Best quantization type for QLoRA
    bnb_4bit_compute_dtype=torch.bfloat16,  # Computation happens in bf16, supported by L40S
    bnb_4bit_use_double_quant=True,  # Quantizes the quantization constants to save memory
)

# Loading model from disk
model = AutoModelForCausalLM.from_pretrained(
    pretrained_model_name_or_path=LOCAL_SAVE_PATH,
    # revision=REVISION,
    quantization_config=bnb_config,
    attn_implementation="sdpa",  # "sdpa" "flash_attention_2"   SDPA uses best available attention mech, including Flash2
    dtype=torch.bfloat16,  # For any layer that is not quantized, load them in bfloat16 from the start. Qlora is mixed precision
    device_map={
        "": local_rank
    },  # Tells accelerate to map entire model to gpu by local_rank. Prevents GPU 0 OOM thundering herd
)

model.config.use_cache = False  # disable KV cache

print("✅ Model and flash attention load successful")

# Prepare the model for QLoRA
model = prepare_model_for_kbit_training(model, use_gradient_checkpointing=True)

# target_modules=[
#         "q_proj",
#         "v_proj",
#         "o_proj",
#         "k_proj",
#         "up_proj",
#         "down_proj",
#         "gate_proj",
#     ]

# small=[
#         "q_proj",
#         "v_proj",
#         "o_proj",
#         "k_proj"
#     ]

lora_config = LoraConfig(
    r=lora_r,  # Rank of the adapter (higher = more capacity, more memory)
    lora_alpha=lora_alpha,  # Reported that training improves as alpha increases relative to r
    target_modules="all-linear",  # identical to target_modules, should be 64M trainable don't load lora twice
    lora_dropout=0.05,
    bias="none",
    task_type="CAUSAL_LM",
)

# Apply the LoRA adapters to the model
model = get_peft_model(model, lora_config)

model.print_trainable_parameters()
print("✅ Peft adapters inserted into model")

# Enable gradient checkpointing to drastically reduce memory usage. No need to call this as already called in prepare_model_for_kbit_training()
# Instead of saving all intermediate layer outputs (activations) during the forward pass, it saves only a few select checkpoints and recalculates the missing values on the fly during the backward pass.
# model.gradient_checkpointing_enable()

# Custom padding class because DataCollator has hidden behaviors causing bottlebecks at numpy conversion


class InstructionTuningCollator:
    def __init__(self, tokenizer, pad_to_multiple_of=8):
        self.tokenizer = tokenizer
        self.pad_to_multiple_of = pad_to_multiple_of
        # Ensure tokenizer has a pad token
        if self.tokenizer.pad_token_id is None:
            self.tokenizer.pad_token_id = self.tokenizer.eos_token_id

    def __call__(self, batch):
        # Convert lists/numpy arrays directly to individual PyTorch tensors
        input_ids = [
            torch.tensor(feature["input_ids"], dtype=torch.int64) for feature in batch
        ]
        attention_mask = [
            torch.tensor(feature["attention_mask"], dtype=torch.int64)
            for feature in batch
        ]
        labels = [
            torch.tensor(feature["labels"], dtype=torch.int64) for feature in batch
        ]

        # Pad them efficiently using native PyTorch to the batch's max length
        # This is right padding by default, which is correct
        input_ids_padded = torch.nn.utils.rnn.pad_sequence(
            input_ids,
            batch_first=True,
            padding_value=self.tokenizer.pad_token_id,
        )
        attention_mask_padded = torch.nn.utils.rnn.pad_sequence(
            attention_mask,
            batch_first=True,
            padding_value=0,
        )
        labels_padded = torch.nn.utils.rnn.pad_sequence(
            labels,
            batch_first=True,
            padding_value=-100,
        )

        # Pad the resulting tensors to a multiple of 8. This Utilises Nvidia tensorcore optimisation
        # https://developer.nvidia.com/blog/optimizing-gpu-performance-tensor-cores/#h.9yili3t5wcy5
        if self.pad_to_multiple_of is not None:
            max_len = input_ids_padded.size(1)
            remainder = max_len % self.pad_to_multiple_of

            if remainder != 0:
                pad_len = self.pad_to_multiple_of - remainder
                # F.pad takes padding from last dimension backwards: (pad_left, pad_right)
                input_ids_padded = F.pad(
                    input_ids_padded,
                    (0, pad_len),
                    value=self.tokenizer.pad_token_id,
                )
                attention_mask_padded = F.pad(
                    attention_mask_padded,
                    (0, pad_len),
                    value=0,
                )
                labels_padded = F.pad(
                    labels_padded,
                    (0, pad_len),
                    value=-100,
                )

        return {
            "input_ids": input_ids_padded,
            "attention_mask": attention_mask_padded,
            "labels": labels_padded,
        }


data_collator = InstructionTuningCollator(tokenizer=tokenizer)

trainer = Trainer(
    model=model,
    args=training_args,
    train_dataset=train_data,
    eval_dataset=validation_data,
    data_collator=data_collator,
    # Stop if eval_loss doesn't improve for 2 evals in a row. Evaluation dataset (supposedly)
    # https://huggingface.co/docs/transformers/en/main_classes/callback#transformers.EarlyStoppingCallback
    callbacks=[
        EarlyStoppingCallback(
            early_stopping_patience=2,
            early_stopping_threshold=early_stopping_threshold,
        )
    ],
)
print("✅ Data and trainer loaded")

# Trainer handles 4-way data parallelism behind the scenes
print("Starting training...")
trainer.train()
print("Completed training ✅ ...")

model.peft_config["default"].base_model_name_or_path = MODEL_REPO
model.config.name_or_path = MODEL_REPO
model.config._name_or_path = MODEL_REPO

##########################################################################################
# Uploads to S3 and HFH
# Wait for all GPUs to catch up before moving to uploads/exits
torch.distributed.barrier()

if is_main_process:
    # Not unwrapping the model only saves the adapter
    trainer.save_model(LOCAL_LORA_SAVE_DIR)
    print(f"✅ Adapters saved locally to {LOCAL_LORA_SAVE_DIR}")

    # Save the log history once
    log_history = trainer.state.log_history

    # Print the trainer log data
    print("----------------------------------------------")

    loss_data = [log for log in log_history if "loss" in log]
    df = pd.DataFrame(loss_data)
    print(df)

    eval_loss_data = [log for log in log_history if "eval_loss" in log]
    df = pd.DataFrame(eval_loss_data)
    print(df)
    print("----------------------------------------------")

    # Retry helper ============================
    def retry(func, retries=3, delay=5):
        for attempt in range(retries):
            try:
                return func()
            except Exception as e:
                if attempt == retries - 1:
                    raise

                print(f"⚠️ Attempt {attempt + 1}\n error: {e}")
                print(f"🔄 Retrying in {delay} seconds...")
                time.sleep(delay)

    ##============================================

    hfapi = HfApi(token=hftoken)

    commit_info = retry(
        lambda: hfapi.upload_folder(
            folder_path=LOCAL_LORA_SAVE_DIR,
            repo_id=ADAPTER_REPO,
            repo_type="model",
            token=hftoken,
        )
    )

    print(commit_info)
    if hasattr(commit_info, "oid"):
        print(f"oid: {commit_info.oid}")
    if hasattr(commit_info, "commit_url"):
        print(f"commit_info: {commit_info.commit_url}")
    print(f"✅ Adapter saved on HF model hub to {MODEL_REPO} commit {commit_info.oid}")

    # Save the trainer logs with the loss data to S3
    log_data_json = json.dumps(
        log_history,
        indent=4,
        default=str,
    )
    s3_client = boto3.client("s3")

    bucket_name = f"{environment}-mlops-bucket-haviv"
    s3_lh_file_path = f"training/{key}/model_logs/training_history.json"
    s3_hfid_file_path = f"training/{key}/hfh_commit/commit_oid.txt"

    retry(
        lambda: s3_client.put_object(
            Bucket=bucket_name,
            Key=s3_lh_file_path,
            Body=log_data_json,
            ContentType="application/json",
        )
    )
    print(f"✅ Successfully uploaded logs to s3://{bucket_name}/{s3_lh_file_path}")

    retry(
        lambda: s3_client.put_object(
            Bucket=bucket_name,
            Key=s3_hfid_file_path,
            Body=str(commit_info.oid).encode("utf-8"),
            ContentType="text/plain",
        )
    )
    print(
        f"✅ Successfully uploaded HF commit id to s3://{bucket_name}/{s3_hfid_file_path}"
    )

    print("⚠️ Exiting program")
    exit(0)
