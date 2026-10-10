"""
This starts a sagemaker training job with sagemaker.huggingface.HugginFace that copies the directory in /src/scripts/evaluate
 - Checkpoints: https://huggingface.co/docs/sagemaker/en/train#spot-instances
 - Baseclass: https://sagemaker.readthedocs.io/en/v2.86.1/api/training/estimators.html#sagemaker.estimator.Framework
 - How to use: https://docs.aws.amazon.com/sagemaker/latest/dg/model-checkpoints-resume.html
This saves a model adapter on hugging face, and its location + commit hash in the job output
This saves the training graph in the job run data on S3

Uses environment variables

Remember that AWS_ENDPOINT_URL_S3 is change from http://seaweedfs-s3.mlrun.svc.cluster.local:8333 to https://s3.amazonaws.com
https://docs.mlrun.org/en/stable/store/datastore.html#s3
"""

# Imports
# import src.utils as utils
import os
import sys
import torch
from datetime import datetime
from datasets import Dataset
from transformers import AutoTokenizer
import pyarrow as pa
import pyarrow.dataset as ds
import json
import boto3
from dotenv import load_dotenv

# Load environment variables into python variables
load_dotenv()  # will not throw an error if .env not present
ENV = os.environ["ENV"]
ACCOUNT_ID = os.environ["ACCOUNT_ID"]
MLRUN_AWS_ROLE_ARN = os.environ["MLRUN_AWS_ROLE_ARN"]
HF_TOKEN = os.environ["HF_TOKEN"]

# MLRun setup =================================================
import mlrun

"""
Set the environment for execution:
Running inside the cluster - MLRun already knows the right address from environment variable
https://docs.mlrun.org/en/1.11.x/setup-guide.html

Running locally, use the mlrun-api service NodePort
kubectl --namespace mlrun get svc | grep -i api
"""

# print("Debug. variables for confirmation")
# print("cwd:", os.getcwd())
# print("__file__ dir:", os.path.dirname(os.path.abspath(__file__)))
# print("contents:", os.listdir("."))
if os.environ.get("MLRUN_DBPATH"):
    print("Detected K8s environment")
    project = mlrun.load_project(
        name="legalcontractextractor", context="/home/mlrun_code/"
    )
else:
    print("Detected Local environment")
    # ====== If run from notebooks, the working directory is /notebooks =====
    parent_dir = os.path.abspath("..")
    sys.path.append(parent_dir)
    # ====== This is necessary for importing other files from src when running locally =====
    mlrun.set_environment(api_path="http://localhost:30070")
    # Context must be where project.yaml is, if running from notebook use ../
    project = mlrun.load_project(name="legalcontractextractor", context="../")

# import other utils files

# =======================================================


def evaluate_model_train(
    context,
    train_dataset,
    train_dataset_tag,
    val_dataset,
    val_dataset_tag,
    test_dataset,
    test_dataset_tag,
    prompt,
    prompt_tag,
    ####
    epochs,
    batch_grad_accumulation,
    learning_rate,
    lora_r,
    lora_alpha,
    early_stopping_threshold,
):

    # Define key
    key = datetime.now().strftime("%Y%m%d_%H%M")

    # Preprocess train/eval data, process test data
    prepare_train_datasets(
        project,
        train_dataset,
        train_dataset_tag,
        val_dataset,
        val_dataset_tag,
        test_dataset,
        test_dataset_tag,
        prompt,
        prompt_tag,
        key,
    )

    # Train model and create loss graph
    commit_oid, log_data, s3_output_path = train_model_get_outputs(
        key=key,
        model_repo="JerroldK/Hermes-4-14B-contract-extractor",
        model_revision="75875f970c359f89ad9e7d4dc86bf3c075c73c31",
        epochs=epochs,
        batch_grad_accumulation=batch_grad_accumulation,
        learning_rate=learning_rate,
        lora_r=lora_r,
        lora_alpha=lora_alpha,
        early_stopping_threshold=early_stopping_threshold,
    )
    print("Trainer log data:")
    print(log_data)

    # key, commit_oid = "20260603_1558", "39c89f599964a53e6dc2e11c273a6d2d6ad52a2e"
    # Evaluate model with HF commit oid
    dataset_metrics, s3_output_path_metric = evaluate_model_lora(
        key,
        project,
        "JerroldK/Hermes-4-14B-contract-extractor",  # model_repo
        "75875f970c359f89ad9e7d4dc86bf3c075c73c31",  # model_revision
        prompt,
        prompt_tag,
        "JerroldK/H4-14b-contract-extractor-adapter",  # adapter_repo
        commit_oid,  # adapter_revision
    )

    # Register results in the job run to MLRun
    keys = [
        "count",
        "average_accuracy",
        "average_fmeasure",
        "t_average_fmeasure",
        "t_average_perc_above_75fmeasure",
        "f_average_fmeasure",
        "f_average_perc_above_75fmeasure",
        "min_accuracy",
        "min_t_average_fmeasure",
        "min_t_perc_above_75fmeasure",
        "min_f_average_fmeasure",
        "min_f_perc_above_75fmeasure",
    ]

    for k in keys:
        context.log_result(k, dataset_metrics[k])

    print("Experiment logged")

    # anything you return will be accessible under RunObject.outputs()['return']
    return {
        "commit_oid:": commit_oid,
        "adapter_repo": "JerroldK/H4-14b-contract-extractor-adapter",
        "s3_output_path": s3_output_path,
    }


def prepare_train_datasets(
    project,
    train_dataset,
    train_dataset_tag,
    val_dataset,
    val_dataset_tag,
    test_dataset,
    test_dataset_tag,
    prompt,
    prompt_tag,
    key,
):
    # train_uri = "store://datasets/finetune-legal-extractor/raw-proc-process-raw_train_data:latest"
    # validation_uri = "store://datasets/finetune-legal-extractor/raw-proc-process-raw_validation_data:latest"
    # key = datetime.now().strftime("%Y%m%d_%H%M")
    train_uri = project.get_artifact(
        key=train_dataset,
        tag=train_dataset_tag,
    ).target_path
    validation_uri = project.get_artifact(
        key=val_dataset,
        tag=val_dataset_tag,
    ).target_path
    test_uri = project.get_artifact(
        key=test_dataset,
        tag=test_dataset_tag,
    ).target_path

    ####################################################### HELPER FUNCTIONS
    def get_dataset(data_uri):
        """
        Gets dataset pyarrow.FileSystemDataset and validates itself and its schema
        """
        data_pointer = mlrun.get_dataitem(data_uri)
        s3_path = data_pointer.url
        raw_dataset = ds.dataset(s3_path, format="parquet")  # pyarrow FileSystemDataset

        # Validate data and its schema
        print(f"Validating data at {s3_path}:")
        required_columns = ["text", "inference"]

        for column in required_columns:
            if column not in raw_dataset.schema.names:
                print(f"! Missing column: {column}")
                raise ValueError(f"Missing required column: {column}")

        table = raw_dataset.to_table()
        for column in required_columns:
            valid_count = table[column].drop_null().length()
            print(f"{column}: {valid_count} rows present")

        return raw_dataset

    def get_sys_prompt(project, prompt_key=prompt, prompt_tag=prompt_tag):

        prompt_artifact = project.get_artifact(key=prompt_key, tag=prompt_tag)
        prompt_template = prompt_artifact.read_prompt()
        system_prompt = prompt_template[0]["content"]

        return system_prompt

    def preprocess(
        batch,
        system_prompt,
        tokenizer,
        max_length=11000,
    ):  # system + user <= max_length. This should be changed to 8000

        system = system_prompt
        user = batch["text"]
        assistant = json.dumps(batch["inference"])

        # 1. Full conversation text (system + user + assistant response)
        # Chatml template
        full_messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
            {"role": "assistant", "content": assistant},
        ]
        full_text = tokenizer.apply_chat_template(
            full_messages,
            tokenize=False,
            add_generation_prompt=False,  # False: assistant response already in messages
            max_length=max_length,
        )

        # 2. Prompt text: everything the model is allowed to *see*, not generate
        prompt_messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]

        prompt_text = tokenizer.apply_chat_template(
            prompt_messages,
            tokenize=False,
            add_generation_prompt=True,  # this is important to append the assistant tag <|assistant|>
        )

        # 3. Tokenize both prompt_ids and full_ids
        prompt_ids = tokenizer(prompt_text, add_special_tokens=False)["input_ids"]
        full_ids = tokenizer(full_text, add_special_tokens=False)["input_ids"]

        # Sanity check: full_ids must start with prompt_ids
        if full_ids[: len(prompt_ids)] != prompt_ids:
            raise ValueError("Tokenization mismatch! Adjust your prompt split.")

        # 4. Build labels array: mask prompt tokens with -100, keep response
        labels = [-100] * len(prompt_ids) + full_ids[len(prompt_ids):]

        # 5. Truncate to max_length
        def pad_trim(ids):
            if len(ids) >= max_length:
                return ids[:max_length]
            return ids

        input_ids = pad_trim(full_ids)
        labels = pad_trim(labels)
        # Replace pad positions in labels with -100 so padding doesn't contribute loss
        labels = [lab if lab != tokenizer.pad_token_id else -100 for lab in labels]
        attention_mask = [
            1 if tok != tokenizer.pad_token_id else 0 for tok in input_ids
        ]

        # Format is lost during coversion from dict to datasetDict, do don't bother
        # return (torch.tensor(input_ids),
        #         torch.tensor(attention_mask),
        #         torch.tensor(labels))
        return (input_ids, attention_mask, labels, len(prompt_ids))

    def preprocess_and_format_to_tensor(
        raw_dataset,
        system_prompt,
        tokenizer,
    ):
        """
        Converts pyarrow datasets into datasets.arrow_dataset.Dataset
        """

        processed_data = {"input_ids": [], "attention_mask": [], "labels": []}
        count = 0
        for batch in raw_dataset.to_batches():
            # Process each pyarrow.RecordBatch
            print(f"Processing batch with {batch.num_rows} rows")
            for row in batch.to_pylist():  # 'row' is a standard Python dictionary
                input_id, attention_mask, label, token_length = preprocess(
                    row,
                    system_prompt,
                    tokenizer,
                )
                # this is for testing on limited hardware because some samples go up to 12k tokens, causing OOM during training
                # most samples are less than 9000
                if token_length > 9000:
                    print(count, f"Token skipped. Length: {token_length}")
                    continue
                else:
                    processed_data["input_ids"].append(input_id)
                    processed_data["attention_mask"].append(attention_mask)
                    processed_data["labels"].append(label)

                    count += 1
                    print(count, f"Token length: {token_length}")

        processed_data = Dataset.from_dict(processed_data)
        # processed_data.set_format('torch', columns=['input_ids', 'attention_mask', 'labels']) # convert to pytorch tensors
        print(type(processed_data))
        return processed_data

    def simple_process(
        raw_dataset,
        system_prompt,
        tokenizer,
        max_length=11000,
    ):
        """
        Converts pyarrow datasets into datasets.arrow_dataset.Dataset
        """
        processed_data = {"text": [], "inference": []}
        count = 0
        for batch in raw_dataset.to_batches():
            # Process each pyarrow.RecordBatch
            print(f"Processing batch with {batch.num_rows} rows")
            for row in batch.to_pylist():
                full_messages = [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": row["text"]},
                ]
                full_text = tokenizer.apply_chat_template(
                    full_messages,
                    tokenize=False,
                    add_generation_prompt=True,  # this is important to append the assistant tag
                    max_length=max_length,
                )

                processed_data["text"].append(full_text)
                processed_data["inference"].append(row["inference"])

                count += 1
                print(count)

        processed_data = Dataset.from_dict(processed_data)
        return processed_data

    #######################################################

    # Load datasets
    pointer_train = mlrun.get_dataitem(train_uri)
    train_pa = get_dataset(pointer_train.url)

    pointer_validation = mlrun.get_dataitem(validation_uri)
    validation_pa = get_dataset(pointer_validation.url)

    pointer_test = mlrun.get_dataitem(test_uri)
    test_pa = get_dataset(pointer_test.url)

    # Get system prompt
    system_prompt = get_sys_prompt(project, prompt, prompt_tag)

    # Get tokenizer
    tokenizer = AutoTokenizer.from_pretrained(
        "JerroldK/Hermes-4-14B-contract-extractor"
    )
    if tokenizer.pad_token is None:
        tokenizer.pad_token = tokenizer.eos_token

    # Prepare paths
    print("s3_eval_path:")
    s3_input_path = f"s3://{ENV}-mlops-bucket-haviv/training/{key}/"
    s3_train_input_path = s3_input_path + "train"
    s3_validation_input_path = s3_input_path + "validation"
    s3_test_input_path = s3_input_path + "test"
    print(s3_train_input_path, s3_validation_input_path, s3_test_input_path)

    # Upload raw train data
    test_dd = simple_process(test_pa, system_prompt, tokenizer)
    test_dd.save_to_disk(s3_test_input_path)
    print("Test data processed and uploaded")

    # Preprocess and upload data
    val_dd = preprocess_and_format_to_tensor(validation_pa, system_prompt, tokenizer)
    val_dd.save_to_disk(s3_validation_input_path)
    print("Validation data preprocessed and uploaded")

    train_dd = preprocess_and_format_to_tensor(train_pa, system_prompt, tokenizer)
    train_dd.save_to_disk(s3_train_input_path)
    print("Train data preprocessed and uploaded")

    return s3_validation_input_path, s3_train_input_path, s3_test_input_path


def train_model_get_outputs(
    key,
    model_repo,
    model_revision,
    epochs,
    batch_grad_accumulation,
    learning_rate,
    lora_r,
    lora_alpha,
    early_stopping_threshold,
):
    from sagemaker.huggingface import HuggingFace
    import matplotlib.pyplot as plt
    import io

    # print(hftoken); exit(0)
    hyperparameters = {
        "model_repo": model_repo,
        "model_revision": model_revision,
        "hftoken": HF_TOKEN,
        # This is only a small fraction of the parameters, but this is all I would change for my training strategy. This already produces very good training loss results
        "epochs": epochs,
        "batch_grad_accumulation": batch_grad_accumulation,
        "learning_rate": learning_rate,
        "lora_r": lora_r,
        "lora_alpha": lora_alpha,
        "early_stopping_threshold": early_stopping_threshold,
        "key": key,
        "environment": ENV,
    }
    """
    mpi_options = {
        "enabled" : True,
        "processes_per_host" : 4,   # 4 processes for 4 gpus in the instance
    }
    smp_options = {
        "enabled": True,
        "parameters": {
            "ddp": True, # Dist data parallelilsm
            # Sharded data parallelsim
            #"sharded_data_parallel_degree": 2, # This parameter specifies the number of GPUs over which the training state is sharded. Start small
            #"bf16": True
        }
    }

    distribution={
        "smdistributed": {"modelparallel": smp_options},
    }
    """

    # Parallelism config. Currently only data parallelism
    # Check help(HuggingFace) for documentation on the distribution parameter
    distribution = {"torch_distributed": {"enabled": True}}

    bucket_name = f"{ENV}-mlops-bucket-haviv"

    huggingface_estimator = HuggingFace(
        entry_point="train_multi.py",
        base_job_name="sm-hf-train",
        source_dir="../src/scripts/training",  # working dir is /notebooks if called from a notebook
        instance_type="ml.g6e.12xlarge",
        instance_count=1,
        ###### max_wait should be equal to or greater than max_run in seconds
        use_spot_instances=True,
        max_wait=60 * 120,  # maximum time allowed for wait + run
        max_run=60 * 90,  # maximum time allowed to run
        checkpoint_s3_uri=f"s3://{ENV}-mlops-bucket-haviv/training/{key}/checkpoints",
        ######
        role=MLRUN_AWS_ROLE_ARN,
        py_version="py313",  # why is this required if the image states the version already
        image_uri=f"{ACCOUNT_ID}.dkr.ecr.us-east-1.amazonaws.com/smhf-torch2.5.1-flash-trans5.3.0-gpul4-py311-cu124:latest",
        hyperparameters=hyperparameters,
        distribution=distribution,
    )

    # Add a dummy isatty method so SageMaker doesn't crash

    if not hasattr(sys.stdout, "isatty"):
        sys.stdout.isatty = lambda: False
    print("🔔Hugging Face estimator training job starting...")
    huggingface_estimator.fit()

    print("🔔Getting loss data and commit id")
    # Get artifacts from training, and save loss curve as png on S3
    s3_client = boto3.client(
        "s3",
        region_name="us-east-1",
        endpoint_url="https://s3.amazonaws.com",
    )
    s3_output_path = f"training/{key}"
    s3_lh_file_path = f"training/{key}/model_logs/training_history.json"
    s3_hfid_file_path = f"training/{key}/hfh_commit/commit_oid.txt"

    commit_oid = s3_client.get_object(
        Bucket=f"{ENV}-mlops-bucket-haviv",
        Key=s3_hfid_file_path,
    )

    commit_oid = commit_oid["Body"].read().decode("utf-8").strip()

    dictionary = s3_client.get_object(
        Bucket=f"{ENV}-mlops-bucket-haviv",
        Key=s3_lh_file_path,
    )

    log_data = json.loads(dictionary["Body"].read().decode("utf-8"))

    train_loss = []
    train_steps = []
    eval_loss = []
    eval_steps = []

    for entry in log_data:
        if "loss" in entry:
            train_loss.append(entry["loss"])
            train_steps.append(entry["step"])

        # Evaluation loss is typically logged under 'eval_loss'
        elif "eval_loss" in entry:
            eval_loss.append(entry["eval_loss"])
            eval_steps.append(entry["step"])

    # --------- Plot loss graph and upload to S3 ----------
    print("🔔Plotting loss graph")
    plt.figure(figsize=(10, 6))
    plt.plot(train_steps, train_loss, marker="x", label="Training Loss", color="blue")

    # Only plot eval loss if it exists
    if eval_loss:
        plt.plot(
            eval_steps, eval_loss, marker="x", label="Validation Loss", color="orange"
        )

    plt.title("Training and Validation Loss Curves")
    plt.xlabel("Training Steps")
    plt.ylabel("Loss")
    plt.legend()
    plt.grid(True)
    # plt.show()

    # 1. Save plot to an in-memory buffer
    img_buffer = io.BytesIO()
    plt.savefig(img_buffer, format="png", bbox_inches="tight")

    # Reset the buffer's file pointer to the beginning so boto3 can read it
    img_buffer.seek(0)

    s3_graph_file_path = f"training/{key}/model_logs/training_curve.png"
    # We should log the loss graph as well, but I skipped it this time

    try:
        s3_client.upload_fileobj(img_buffer, bucket_name, s3_graph_file_path)
        print(f"Successfully saved plot to s3://{bucket_name}/{s3_graph_file_path}")
    except Exception as e:
        print(f"Failed to upload to S3: {e}")
    finally:
        # Clean up memory
        img_buffer.close()
        plt.close()

    return commit_oid, log_data, s3_output_path


def evaluate_model_lora(
    key,
    project,
    model_repo,
    model_revision,
    prompt,
    prompt_tag,
    adapter_repo,
    adapter_revision,
):

    from sagemaker.pytorch import PyTorch

    # If you define a session, it uses this instead of the default us-east-1
    # boto_session = boto3.Session(region_name="us-east-2")
    # sm_session = sagemaker.Session(boto_session=boto_session)
    # print("🔔Us-east-2 sage session created")

    # Run evluation script as a training job, calculate metrics and upload to S3
    prompt_config = project.get_artifact(key=prompt, tag=prompt_tag).to_dict()["spec"][
        "invocation_config"
    ]

    _temp, _topp, _max_tok = (
        prompt_config["temperature"],
        prompt_config["top_p"],
        prompt_config["max_new_tokens"],
    )

    hyperparameters = {
        "model_repo": model_repo,
        "model_revision": model_revision,
        "adapter_repo": adapter_repo,
        "adapter_revision": adapter_revision,
        "hftoken": HF_TOKEN,
        "key": key,
        "prompt_temp": _temp,
        "prompt_topp": _topp,
        "prompt_max_tok": _max_tok,
        "environment": ENV,
    }
    print(hyperparameters)

    estimator = PyTorch(
        entry_point="quant_eval_vllm_lora.py",
        base_job_name="sm-hf-lora-eval",
        source_dir="../src/scripts/evaluate",
        instance_type="ml.g6e.2xlarge",
        instance_count=1,
        ###### max_wait should be equal to or greater than max_run in seconds
        use_spot_instances=True,
        max_wait=60 * 60,  # maximum time allowed for wait + run
        max_run=60 * 45,  # maximum time allowed to run
        ######
        role=MLRUN_AWS_ROLE_ARN,
        py_version="py313",  # why is this required if the image states the version already
        image_uri="763104351884.dkr.ecr.us-east-1.amazonaws.com/pytorch-training:2.10.0-gpu-py313-cu130-ubuntu22.04-sagemaker",
        hyperparameters=hyperparameters,
        # sagemaker_session=sm_session
    )
    import sys

    if not hasattr(sys.stdout, "isatty"):
        sys.stdout.isatty = lambda: False

    print("🔔Pytorch estimator evaluation job starting...")
    estimator.fit()

    # Get the evaluation metrics from its path on S3, then return them both
    print("🔔Getting loss data and commit id")
    s3_client = boto3.client(
        "s3",
        region_name="us-east-1",
        endpoint_url="https://s3.amazonaws.com",
    )
    bucket_name = f"{ENV}-mlops-bucket-haviv"
    s3_met_file_path = f"training/{key}/evaluation/metrics.json"

    dictionary = s3_client.get_object(Bucket=bucket_name, Key=s3_met_file_path)
    metric_data = json.loads(dictionary["Body"].read().decode("utf-8"))
    s3_met_uri = f"s3://{bucket_name}/{s3_met_file_path}"

    return metric_data, s3_met_uri


# if __name__ == "__main__":
#     evaluate_model_train(
#         train_dataset="raw-proc-process-raw_train_data",
#         train_dataset_tag="20260506_1224",
#         val_dataset="raw-proc-process-raw_validation_data",
#         val_dataset_tag="20260506_1224",
#         test_dataset="raw-proc-process-raw_test_data",
#         test_dataset_tag="20260506_1224",
#         prompt="contract_extractor_prompt",
#         prompt_tag="latest",
#         epochs=5,
#         batch_grad_accumulation=16,
#         learning_rate=2e-4,
#         lora_r=16,
#         lora_alpha=32,
#         early_stopping_threshold=1e-3,
#     )
