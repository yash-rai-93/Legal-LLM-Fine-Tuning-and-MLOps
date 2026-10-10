"""
This starts a sagemaker training job that copies the directory in /src/scripts/evaluate

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
import pyarrow.fs as pafs
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


def evaluate_model(context, test_dataset, test_dataset_tag, prompt, prompt_tag):

    # Define key
    key = datetime.now().strftime("%Y%m%d_%H%M")

    # Prepare data
    prepare_notrain_datasets(
        project, test_dataset, test_dataset_tag, prompt, prompt_tag, key
    )

    # Run eval script as a training job
    dataset_metrics, s3_output_path = evaluate_model_base(
        key,
        project,
        "JerroldK/Hermes-4-14B-contract-extractor",  # model_repo
        "75875f970c359f89ad9e7d4dc86bf3c075c73c31",  # model_revision
        prompt,
        prompt_tag,
    )
    print("Inferences complete for no-train")
    print(dataset_metrics)

    # Save experiment metrics to MLRun
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
    return {"s3_output_path": s3_output_path}


def prepare_notrain_datasets(
    project,
    test_dataset,
    test_dataset_tag,
    prompt,
    prompt_tag,
    key,
):
    # key = datetime.now().strftime("%Y%m%d_%H%M")

    test_uri = project.get_artifact(key=test_dataset, tag=test_dataset_tag).target_path

    ####################################################### HELPER FUNCTIONS
    def get_dataset(data_uri):
        """
        Gets dataset pyarrow.FileSystemDataset and validates itself and its schema
        """
        data_pointer = mlrun.get_dataitem(data_uri)
        s3_path = data_pointer.url
        # ignore AWS_ENDPOINT_URL_S3
        s3_path = s3_path.replace("s3://", "")
        s3_fs = pafs.S3FileSystem(
            region="us-east-1",
            endpoint_override="https://s3.amazonaws.com",
        )
        raw_dataset = ds.dataset(
            s3_path,
            format="parquet",
            filesystem=s3_fs,
        )  # pyarrow FileSystemDataset

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

    def get_sys_prompt(
        project, prompt_key="contract_extractor_prompt", prompt_tag="latest"
    ):

        prompt_artifact = project.get_artifact(key=prompt_key, tag=prompt_tag)
        prompt_template = prompt_artifact.read_prompt()
        system_prompt = prompt_template[0]["content"]

        return system_prompt

    def simple_process(raw_dataset, system_prompt, tokenizer, max_length=11000):
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
                    add_generation_prompt=True,
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
    s3_input_path = f"s3://{ENV}-mlops-bucket-haviv/notrain/{key}/"
    s3_test_input_path = s3_input_path + "test_data"
    print(s3_test_input_path)

    # Upload raw train data
    test_dd = simple_process(test_pa, system_prompt, tokenizer)
    # print((type(test_dd)))
    # print("-----------")
    # test_dd.save_to_disk(
    #     r"C:\temp\hf_test"
    # )
    # raise SystemError(":)")
    test_dd.save_to_disk(s3_test_input_path)
    print("Test data processed and uploaded")

    return s3_test_input_path


def evaluate_model_base(
    key,
    project,
    model_repo,
    model_revision,
    prompt,
    prompt_tag,
):

    from sagemaker.pytorch import PyTorch

    # import sagemaker

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
        "hftoken": HF_TOKEN,
        "key": key,
        "prompt_temp": _temp,
        "prompt_topp": _topp,
        "prompt_max_tok": _max_tok,
        "environment": ENV,
    }
    print(hyperparameters)
    # Sagemaker packages and uploads the entire directory source_dir, hence it is able to import the other scripts
    estimator = PyTorch(
        entry_point="quant_eval_vllm_base.py",
        base_job_name="sm-hf-basemodel-eval",
        source_dir="../src/scripts/evaluate",
        instance_type="ml.g6e.2xlarge",
        instance_count=1,
        ###### max_wait should be equal to or greater than max_run in seconds
        use_spot_instances=True,
        max_wait=60 * 60,  # maximum time allowed for wait + run
        max_run=60 * 45,  # maximum time allowed to run
        ######
        role=MLRUN_AWS_ROLE_ARN,
        py_version="py311",  # why is this required if the image states the version already
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
    s3_met_file_path = f"notrain/{key}/metrics.json"

    dictionary = s3_client.get_object(Bucket=bucket_name, Key=s3_met_file_path)
    metric_data = json.loads(dictionary["Body"].read().decode("utf-8"))

    return metric_data, s3_met_file_path
