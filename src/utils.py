# torch has to be imported first before transformers and sagemaker, becuase they import torch internally.
# this will initialise the DLLs first
import torch
import boto3
from botocore.config import Config
from transformers import AutoTokenizer

# from datasets import Dataset
import pyarrow.dataset as ds
import json
from concurrent.futures import ThreadPoolExecutor
from dotenv import load_dotenv
import os
from src import utils_evaluate_model as evaluate_model

load_dotenv()

# import utils_evaluate_model as evaluate_model # for tests

# ========================================
# Functions for training and evaluation
# ========================================

# ========================================
# Functions for serving and testing in production
# ========================================
"""
https://docs.djl.ai/master/docs/serving/serving/docs/lmi/user_guides/vllm_user_guide.html#rolling-batch-configurations
https://docs.djl.ai/master/docs/serving/serving/docs/lmi/conceptual_guide/lmi_engine.html
https://github.com/aws-samples/sagemaker-genai-hosting-examples/blob/main/Llama3.1/Benchmarking-LMI-containers-Llama3p1-Instruct.ipynb
https://docs.djl.ai/master/docs/serving/plugins/secure-mode/index.html#security-controls
https://docs.djl.ai/master/docs/serving/serving/docs/lmi/conceptual_guide/lmi_engine.html


https://docs.djl.ai/master/docs/demos/aws/sagemaker/large-model-inference/sample-llm/multi_lora_gemma3_4b.html#invoke-base-ic
https://docs.djl.ai/master/docs/demos/aws/sagemaker/large-model-inference/sample-llm/stateful_inference_gemma3_4b_lora.html#invoke-adapter-ic

"""


def deploy_sm_lora_model(
    batch_size: str = "4",
    max_model_len: str = "7150",
    batch_tokens: str = "28600",
    instance: str = "ml.g6e.2xlarge",
):

    # from sagemaker.djl_inference.model import DJLModel
    from sagemaker import Model
    from sagemaker.compute_resource_requirements.resource_requirements import (
        ResourceRequirements,
    )
    from sagemaker.utils import name_from_base  # appends datetime
    from sagemaker.session import Session
    import sagemaker

    """
    Sagemaker model endpoints LMI is unstable and highly prone to memory leaks as performance tests have shown. Theoretically a batch size of up to 19 is possible, but tests show that a mediocre size of 4 is stable in production workloads - even with Flashinfer backend. Using g6e.2xlarge

    Based on the math at (6000+3000) and size of KV cache, 48GB VRAM, maximum batch size under memory limit can be 19; exluding activations/attention artifacts/cache updates. However throughput flattens as batch size increases and we reach a compute bottleneck during prefill (or some other hardware limitation).

    """

    """
    Model required an invocataion component adapter, unlike a base standalone model

    Autoscaling:
    Scaling policies can be attach to a model after the endpoint has been deployed. 
    https://docs.aws.amazon.com/sagemaker/latest/dg/endpoint-auto-scaling-add-code-apply.html
    https://aws.amazon.com/blogs/machine-learning/amazon-sagemaker-inference-launches-faster-auto-scaling-for-generative-ai-models/

    Realtime endpoint adapters:
    https://docs.djl.ai/master/docs/demos/aws/sagemaker/large-model-inference/sample-llm/multi_lora_gemma3_4b.html#clean-up-resources
    https://docs.aws.amazon.com/sagemaker/latest/dg/realtime-endpoints-adapt.html
    """

    region = "us-east-1"
    boto_session = boto3.Session(region_name=region)
    sagemaker_session = Session(boto_session=boto_session)

    # DJL images. Should look at entrypoint files in this image.
    # https://aws.github.io/deep-learning-containers/reference/available_images/#djl-inference
    image = f"763104351884.dkr.ecr.{region}.amazonaws.com/djl-inference:0.36.0-lmi24.0.0-cu129"  # this works with async and roll batch
    role = os.environ["MLRUN_AWS_ROLE_ARN"]
    HF_MODEL_ID = "JerroldK/Hermes-4-14B-contract-extractor"
    HF_REVISION = "75875f970c359f89ad9e7d4dc86bf3c075c73c31"
    INSTANCE_TYPE = instance
    print(INSTANCE_TYPE)

    BATCH_SIZE = batch_size
    MAX_MODEL_LEN = max_model_len
    BATCH_TOKENS = batch_tokens

    lmi_batch_config = {
        "HF_MODEL_ID": HF_MODEL_ID,
        "HF_TOKEN": os.environ["HF_TOKEN"],
        "HF_REVISION": HF_REVISION,
        "SERVING_ENGINE": "Python",  # https://docs.djl.ai/master/docs/serving/serving/docs/lmi/conceptual_guide/lmi_engine.html
        "OPTION_ROLLING_BATCH": "disable",  # vllm disable
        "OPTION_ASYNC_MODE": "true",
        # "max" if enable tensor parallelism
        # 1 enables data parallelism since 1 model per gpu
        "TENSOR_PARALLEL_DEGREE": "1",
        "OPTION_ENTRYPOINT": "djl_python.lmi_vllm.vllm_async_service",  # this is from article
        "OPTION_QUANTIZE": "fp8",
        "OPTION_KV_CACHE_DTYPE": "fp8",
        "OPTION_GPU_MEMORY_UTILIZATION": "0.95",
        # "OPTION_ENABLE_CHUNKED_PREFILL":"false",
        # "OPTION_ENABLE_PREFIX_CACHING":"false",
        # "OPTION_ENFORCE_EAGER":"true",
        "OPTION_MAX_MODEL_LEN": MAX_MODEL_LEN,  # Max input + output = 6000 + 3000, this is a little buggy because requests close to but under 9000 tokens exceed this hard limit
        "MAX_BATCH_SIZE": BATCH_SIZE,  # https://docs.djl.ai/master/docs/serving/serving/docs/lmi/user_guides/starting-guide.html
        "MAX_CONCURRENT_REQUESTS": "200",
        "OPTION_MAX_ROLLING_BATCH_SIZE": BATCH_SIZE,  # this is in the amazon articles for async serving
        # --- ADVANCED SETTINGS -----
        # https://docs.djl.ai/master/docs/serving/serving/docs/lmi/user_guides/vllm_user_guide.html#advanced-vllm-configurations
        "OPTION_MAX_NUM_BATCHED_TOKENS": BATCH_TOKENS,  # Limits the number of tokens that can be processed in a single step during prefill
        # https://docs.vllm.ai/en/v0.8.3/serving/env_vars.html
        "VLLM_ATTENTION_BACKEND": "FLASHINFER",  # TORCH_SDPA  FLASH_ATTN
        # The maximum time it will wait to receive a chunk of data from the Python backend. This is when waiting for previous batch to complete.
        "OPTION_PREDICT_TIMEOUT": str(60 * 15),
        "OPTION_MODEL_LOADING_TIMEOUT": str(60 * 20),
        "OPTION_TRUST_REMOTE_CODE": "true",
        "SERVING_FAIL_FAST": "true",
        "OPTION_ENABLE_LORA": "true",  # Enable for dynamic Lora adapters, reserves chunk of KV cache VRAM
        "OPTION_MAX_LORA_RANK": "16",
        "OPTION_PARALLEL_LOADING": "true",  # parallel model loading when loading multiple model workers, inc temp memory footprint
        "SERVING_JOB_QUEUE_SIZE": "500",  # Default is 1000
    }
    # THIS IS OUTDATED AND REQUIRES A DIFFERENT IMAGE. DO NOT USE
    # lmi_old_batch_config = {
    #     "HF_MODEL_ID": HF_MODEL_ID,
    #     "HF_TOKEN": os.environ['HF_TOKEN'],
    #     "HF_REVISION": HF_REVISION,

    #     "OPTION_ROLLING_BATCH": "vllm", #vllm disable
    #     "OPTION_ENFORCE_EAGER":"true",
    #     "TENSOR_PARALLEL_DEGREE": "1", # or "max" if enable tensor parallelism
    #     "OPTION_QUANTIZE":"fp8",
    #     "OPTION_KV_CACHE_DTYPE":"fp8",

    #     "OPTION_MAX_MODEL_LEN":MAX_MODEL_LEN, # Max input + output = 6000 + 3000, this is a little buggy because requests close to but under 9000 tokens exceed this hard limit
    #     "MAX_CONCURRENT_REQUESTS": "200",
    #     "OPTION_MAX_ROLLING_BATCH_SIZE":BATCH_SIZE, # this is in the amazon articles for async serving
    #     # --- ADVANCED SETTINGS -----
    #     # https://docs.djl.ai/master/docs/serving/serving/docs/lmi/user_guides/vllm_user_guide.html#advanced-vllm-configurations
    #     "OPTION_MAX_NUM_BATCHED_TOKENS": BATCH_TOKENS,# Limits the number of tokens that can be processed in a single step during prefill

    #     # https://docs.vllm.ai/en/v0.8.3/serving/env_vars.html
    #     "VLLM_ATTENTION_BACKEND":"FLASH_ATTN", # TORCH_SDPA  FLASH_ATTN FLASHINFER
    #     "OPTION_ENABLE_CHUNKED_PREFILL":"false",
    #     "OPTION_ENABLE_PREFIX_CACHING":"false",
    #     "OPTION_ASYNC_MODE":"false",

    #     # The maximum time it will wait to receive a chunk of data from the Python backend. This is when waiting for previous batch to complete.
    #     "OPTION_PREDICT_TIMEOUT": str(60*10),    # 10 mins
    #     "OPTION_MODEL_LOADING_TIMEOUT": str(60*20), # 20 mins
    #     "SERVING_FAIL_FAST":"true",

    #     "OPTION_ENABLE_LORA": "true", # Enable for dynamic Lora adapters, reserves chunk of KV cache VRAM
    #     "OPTION_MAX_LORA_RANK": "16",
    #     "OPTION_PARALLEL_LOADING": "true", # parallel model loading when loading multiple model workers, inc temp memory footprint
    #     "SERVING_JOB_QUEUE_SIZE": '500', # Default is 1000
    # }

    for k, v in lmi_batch_config.items():
        print(k, v)

    # About 10-15 mins if successful
    model = Model(
        env=lmi_batch_config,
        role=role,
        image_uri=image,
        sagemaker_session=sagemaker_session,
    )

    endpoint_name = name_from_base("lmi-Hermes-FP8")
    base_ic_name = "base-" + endpoint_name

    print("Deploying model endpoint...")
    predictor = model.deploy(
        initial_instance_count=1,
        instance_type=INSTANCE_TYPE,
        endpoint_name=endpoint_name,
        endpoint_type=sagemaker.enums.EndpointType.INFERENCE_COMPONENT_BASED,
        inference_component_name=base_ic_name,
        resources=ResourceRequirements(
            requests={"num_accelerators": 1, "memory": 256, "copies": 1}
        ),  # Resource requirements for inference component object
    )

    # endpoint_name = predictor.endpoint_name # unavailable for lora endpoint
    print(f"✅ Deployed model (LoRA): {endpoint_name}, {base_ic_name}")

    # Attach autoscaling policy
    print("Attaching autoscaling policy...")

    aas_client = boto3.client("application-autoscaling")
    ic_resource_id = f"inference-component/{base_ic_name}"
    ic_dimension = "sagemaker:inference-component:DesiredCopyCount"

    aas_client.register_scalable_target(
        ServiceNamespace="sagemaker",
        ResourceId=ic_resource_id,
        ScalableDimension=ic_dimension,
        MinCapacity=1,
        MaxCapacity=4,
    )

    # Apply the scaling policy to the Base Inference Component
    invocation_count = 5.0
    aas_client.put_scaling_policy(
        PolicyName=f"scale-copies-{base_ic_name}",
        ServiceNamespace="sagemaker",
        ResourceId=ic_resource_id,
        ScalableDimension=ic_dimension,
        PolicyType="TargetTrackingScaling",
        TargetTrackingScalingPolicyConfiguration={
            "TargetValue": invocation_count,
            "PredefinedMetricSpecification": {
                "PredefinedMetricType": "SageMakerInferenceComponentInvocationsPerCopy"
            },
            "ScaleOutCooldown": 60,
            "ScaleInCooldown": 300,
        },
    )

    print("✅ Autoscaling policy successfully added")

    # Return the endpoint name and base ic name to attach IC adapter
    return endpoint_name, base_ic_name


def deploy_sm_lora_adapter(key, endpoint_name, base_ic_name, adapter_revision):
    from huggingface_hub import HfFileSystem
    import tarfile
    import io
    import sagemaker
    from sagemaker.session import Session
    from sagemaker.utils import name_from_base  # appends datetime

    print("Deploying IC adapter.....")
    print(key)

    ENV = os.environ["ENV"]
    BUCKET = f"{ENV}-mlops-bucket-haviv"

    ADAPTER_ID = "JerroldK/H4-14b-contract-extractor-adapter"
    ADAPTER_FILENAME = "adapter.tar.gz"
    S3_KEY = f"Adapters/{key}/{ADAPTER_FILENAME}"
    S3_URI = f"s3://{BUCKET}/{S3_KEY}"

    ic_adapter_name = f'adapter-{name_from_base("lmi-Hermes-FP8")}'

    s3_client = boto3.client(
        "s3",
        region_name="us-east-1",
        endpoint_url="https://s3.amazonaws.com",
    )

    # Compress and upload adapter to S3 ================================
    fs = HfFileSystem(token=os.environ["HF_TOKEN"])

    # Create an in-memory buffer for the tar archive
    tar_buffer = io.BytesIO()
    print(
        f"Fetching files from '{ADAPTER_ID}' {adapter_revision} and building {ADAPTER_FILENAME} in memory..."
    )

    # Open the tar buffer for writing in gzip mode
    with tarfile.open(fileobj=tar_buffer, mode="w:gz") as tar:

        # fs.find() recursively gets all files in the Hugging Face repo
        for file_path in fs.find(path=ADAPTER_ID, revision=adapter_revision):

            # Read the file's bytes directly from Hugging Face
            with fs.open(file_path, "rb") as f:
                file_data = f.read()

            file_obj = io.BytesIO(file_data)

            # Strip the repo name from the path so it extracts cleanly without user/repo_name@hash/
            # relative_path = file_path.replace(f"{ADAPTER_ID}/", "")
            relative_path = "/".join(
                file_path.split("/")[2:]
            )  # "/".join() is for nested folders
            print(f"  -> Packaging: {file_path} as {relative_path}")
            # Create tar file metadata
            tarinfo = tarfile.TarInfo(name=relative_path)
            tarinfo.size = len(file_data)
            # Add the file to the in-memory tar archive
            tar.addfile(tarinfo, fileobj=file_obj)

    # Rewind the buffer to the beginning before uploading
    tar_buffer.seek(0)

    print(f"⚠️ Uploading directly to {S3_URI}...")

    # Stream the buffer directly to S3
    s3_client.upload_fileobj(tar_buffer, BUCKET, S3_KEY)

    print("✅ Success! Lora module Transfer complete")

    # Create inference component for model endpoint ==========================
    sm_client = boto3.client(
        service_name="sagemaker", region_name="us-east-1"
    )  # not sagemaker-runtime
    sess = sagemaker.session.Session()

    adapter_inference = sm_client.create_inference_component(
        InferenceComponentName=ic_adapter_name,  # extension of endpoint_name
        EndpointName=endpoint_name,
        Specification={
            "BaseInferenceComponentName": base_ic_name,
            "Container": {"ArtifactUrl": S3_URI},
        },
    )
    sess.wait_for_inference_component(ic_adapter_name)

    print(
        f"✅ Created Adapter inference component {ic_adapter_name} for endpoint {endpoint_name} ARN: {adapter_inference['InferenceComponentArn']}"
    )

    # # Apply the scaling policy to the Inference component
    # print(f"Attaching autoscaling policy....")

    # aas_client = boto3.client("application-autoscaling")
    # ic_resource_id = f"inference-component/{ic_adapter_name}"
    # ic_dimension = "sagemaker:inference-component:DesiredCopyCount"

    # aas_client.register_scalable_target(
    #     ServiceNamespace="sagemaker",
    #     ResourceId=ic_resource_id,
    #     ScalableDimension=ic_dimension,
    #     MinCapacity=1,
    #     MaxCapacity=4,  # Desired maximum instances
    # )

    # aas_client.put_scaling_policy(
    #     PolicyName=f"scale-copies-{ic_adapter_name}",
    #     ServiceNamespace="sagemaker",
    #     ResourceId=ic_resource_id,
    #     ScalableDimension=ic_dimension,
    #     PolicyType="TargetTrackingScaling",
    #     # Faster: SageMakerInferenceComponentConcurrentRequestsPerCopyHighResolution
    #     TargetTrackingScalingPolicyConfiguration={
    #         "TargetValue": 4.0,  # 4 invocations per adapter copy
    #         "PredefinedMetricSpecification": {
    #             "PredefinedMetricType": "SageMakerInferenceComponentInvocationsPerCopy"
    #         },
    #         "ScaleOutCooldown": 60,
    #         "ScaleInCooldown": 300,
    #     },
    # )

    # print(f"✅ Autoscaling policy added")

    return adapter_inference, ic_adapter_name


# ========================================
# model pre-deployment validation of model endpoint
# ========================================


def invoke_model(
    smr_client,
    sys_prompt,
    parameters,
    contract_data,
    endpoint_name,
    adapter_name,
    tokenizer,
    id="",
    max_prompt_l=5000,
):
    """
    change max_token_length parameter outside this function before passing parameters
    id is an optional identifier to track to inhouse data
    """
    # print(parameters)

    prompt = [
        {"role": "system", "content": sys_prompt},
        {"role": "user", "content": contract_data},
    ]

    chatml_prompt = tokenizer.apply_chat_template(
        prompt,
        tokenize=False,
        add_generation_prompt=True,  # this is important to append the assistant tag
    )

    # ignore inference if contract_data is too long
    tokens = tokenizer.encode(chatml_prompt)
    print(f"Document {id} prompt length is {len(tokens)} tokens")
    if len(tokens) >= max_prompt_l:
        print(f"Document {id} prompt length is over {max_prompt_l} tokens and ignored")
        return -1

    chatml = {"inputs": chatml_prompt, "parameters": parameters}

    # invoke endpoint with response stream
    try:
        resp = smr_client.invoke_endpoint_with_response_stream(
            EndpointName=endpoint_name,
            Body=json.dumps(chatml),
            ContentType="application/json",
            Accept="application/json",
            InferenceComponentName=adapter_name,  # remove this line for base model endpoint invocation
        )
        # Process streaming response
        full_response = ""
        for event in resp["Body"]:
            if "PayloadPart" in event:
                payload = event["PayloadPart"]["Bytes"].decode("utf-8")
                full_response += payload

        tokens = tokenizer.encode(full_response)
        l_t = len(tokens)
        print(f"Doc {id} response length is {l_t} tokens ✅")
        # if l_t > 4900:
        #     print(f"Warning: Document {id} response is over 4900 tokens")
    except Exception as e:
        print(f"! Error with doc {id} part A", e)

    try:
        full_response_dict = json.loads(full_response)
    except Exception as e:
        print(f"! Error Document {id} during json.load() Part B:", e)
        # print(full_response) # this throws errors
        return -1

    # At this point we have not reached LLM generated tokens yet
    # print("---->", full_response_dict)
    inference = full_response_dict["generated_text"]

    try:
        # start, end = soft_search_json(inference)
        # inference = inference[start:end+1]
        inference_dict = json.loads(inference)["Hypotheses"]
    except Exception as e:
        print(f"! Error Document {id} during json.load() part C:", e)
        print(inference)
        return -1

    return inference_dict


def process_single_row_testdata(
    row, sys_prompt, parameters, endpoint_name, adapter_name, tokenizer
):
    # Create a new session and client
    custom_config = Config(
        read_timeout=900,  # 15 mins
        connect_timeout=900,
        retries={"max_attempts": 1},
        max_pool_connections=1,
    )
    thread_session = boto3.Session()
    smr_client = thread_session.client(
        "sagemaker-runtime", config=custom_config, region_name="us-east-1"
    )

    # Extract data from row
    document_id = row["document_id"]
    contract_data = row["text"]
    reference_dict = row["inference"]  # list of dictionaries

    # make inference to model
    inference_dict = invoke_model(
        smr_client,
        sys_prompt,
        parameters,
        contract_data,
        endpoint_name,
        adapter_name,
        tokenizer,
        id=document_id,
    )
    # print(inference_dict)
    if inference_dict == -1:
        return -1

    try:
        document_level_metrics = evaluate_model.document_level_metrics(
            reference_dict, inference_dict
        )
    except Exception as e:
        print("! Error in calculating document_level_metrics:", e)
        print(inference_dict)
    # print(document_level_metrics)
    # return artifacts
    return {
        "contract_data": contract_data,
        "inference_dict": inference_dict,
        "reference_dict": reference_dict,
        "document_level_metrics": document_level_metrics,
    }


def process_multiple_row_testdata(
    project,
    endpoint_name,
    adapter_name,
    eval_data_key,
    eval_data_tag,
    prompt_key,
    prompt_tag,
    key,
    max_output_l=3000,  # original value: 2000
    batchsize=5,
):

    # get input data handle data paths
    artifact = project.get_artifact(key=eval_data_key, tag=eval_data_tag)
    artifact_latest_s3_path = artifact.target_path

    # print("s3_eval_path:")
    # s3_eval_path = f"s3://legal-llama-data/evaluation/{key}/"
    # print(s3_eval_path)

    # get prompt and invoc config
    prompt_artifact = project.get_artifact(key=prompt_key, tag=prompt_tag)
    prompt_tag = prompt_artifact.to_dict()["spec"]["producer"]["tag"]
    invoc_config = prompt_artifact.to_dict()["spec"]["invocation_config"]
    print("invoc_config:")
    if invoc_config["max_new_tokens"] > max_output_l:
        invoc_config["max_new_tokens"] = max_output_l
        print(f"max_new_tokens is too high and reduced to {max_output_l}")

    # print(invoc_config)
    # print()

    tokenizer = AutoTokenizer.from_pretrained(
        "JerroldK/Hermes-4-14B-contract-extractor"
    )

    # get prompt template and prepare system prompt
    prompt_template = prompt_artifact.read_prompt()
    system_prompt = prompt_template[0][
        "content"
    ]  # unfortunately mlrun forces the openai messages format for prompt storage

    # get test data
    test_dataset = ds.dataset(source=artifact_latest_s3_path, format="parquet")
    # test the first 10 samples
    # test_dataset = test_dataset.to_table().to_pylist()[:10]

    # test 5 random samples
    import random

    testSize = 5
    test_dataset = test_dataset.to_table().to_pylist()
    start = random.randint(0, len(test_dataset) - testSize)
    test_dataset = test_dataset[start: start + testSize]

    MINI_BATCH_SIZE = batchsize  # or whatever, it goes to the queue
    all_results = {
        "contract_data": [],
        "inference_dict": [],
        "reference_dict": [],
        "document_level_metrics": [],
    }

    # Run the inferences over each mini-batch in parallel
    for i in range(0, len(test_dataset), MINI_BATCH_SIZE):

        # Prepare the minibatch
        mini_batch = test_dataset[i: i + MINI_BATCH_SIZE]
        batch_number = (i // MINI_BATCH_SIZE) + 1
        print(
            f"\n--- Starting Mini-Batch {batch_number} ({len(mini_batch)} requests) ---"
        )

        with ThreadPoolExecutor(max_workers=MINI_BATCH_SIZE) as executor:
            mbatch_results = list(
                executor.map(
                    lambda x: process_single_row_testdata(
                        x,
                        system_prompt,
                        invoc_config,
                        endpoint_name,
                        adapter_name,
                        tokenizer,
                    ),
                    mini_batch,
                )
            )
            # remove invalid entries because the contract_data is too long
            mbatch_results = [x for x in mbatch_results if x != -1]
            # all_results.extend(mbatch_results)
            # loop over mbatch_results and append to all_results
            for i in mbatch_results:
                all_results["contract_data"].append(i["contract_data"])
                all_results["inference_dict"].append(i["inference_dict"])
                all_results["reference_dict"].append(i["reference_dict"])
                all_results["document_level_metrics"].append(
                    i["document_level_metrics"]
                )
            print(f"Mini-Batch {batch_number} completed successfully.")

    dataset_metrics = evaluate_model.dataset_level_metrics(
        all_results["document_level_metrics"]
    )
    print(dataset_metrics)

    # Write all data to S3 output path
    # Ensure your AWS credentials are configured in your environment
    # table = pa.table(all_results)
    # ds.write_dataset(
    #     table,
    #     base_dir=s3_eval_path,
    #     basename_template="inference_output{i}.parquet",
    #     format="parquet"
    # )

    # pd_dataset_metrics = pd.DataFrame(dataset_metrics, index=[range(len(dataset_metrics))])
    # pd_dataset_metrics.to_json(f"{s3_eval_path}metrics.json", orient="records", lines=True)

    # print("Inference results and metrics written to S3 at:")
    # print(s3_eval_path)
    s3_eval_path = ""

    return dataset_metrics, s3_eval_path


# ========================================
# Update traffic gateway with rollback
# ========================================


def update_gateway_destination_sm(
    project,
    model_endpoint: str,
    model_adapter: str,
    template_uri: str,
    rolling: bool,
    test_dataset,
    test_dataset_tag,
):
    """
    This updates the AppConfig read by lambda with a linear or rolling deployment
    """

    # Get infra var names from terraform out
    # Get environment variables
    ENV = os.environ["ENV"]

    import subprocess

    parent_dir = os.path.abspath("../Terraform/")  # this is called from notebook

    # These variables need to be baked into the image as environment variables during build
    # Terraform -> Build MLRun function image -> Push image -> Deploy to prod
    result = subprocess.run(
        ["terraform", "output", "-json"],
        capture_output=True,
        text=True,
        check=True,
        cwd=parent_dir,
    )
    outputs = json.loads(result.stdout)
    print(outputs)
    values = {key: data["value"] for key, data in outputs.items()}
    try:
        appconfig_app_id = values["appconfig_app_id"]
        appconfig_env_id = values["appconfig_env_id"]
        appconfig_confprof_cpid = values["appconfig_confprof_cpid"]
        appconfig_deploystrat_direct_id = values["appconfig_deploystrat_direct_id"]
        appconfig_deploystrat_rolling_id = values["appconfig_deploystrat_rolling_id"]

    except KeyError as e:
        raise KeyError(f"Missing required configuration key: {e.args[0]}") from None

    ac_client = boto3.client("appconfig", region_name="us-east-1")

    ################################
    # Update the free-form configuration profile read in lambda for the deployment

    configuration = {
        "model_endpoint": model_endpoint,
        "model_adapter": model_adapter,
        "template_uri": template_uri,
    }

    host_config_response = ac_client.create_hosted_configuration_version(
        ApplicationId=appconfig_app_id,
        ConfigurationProfileId=appconfig_confprof_cpid,
        Content=json.dumps(configuration).encode("utf-8"),
        ContentType="application/json",
        Description="AppConfig configuration profile for a rolling or direct deployment to be read in Lambda to change traffic",
    )
    version_number = host_config_response["VersionNumber"]

    deployment_strategy_id = ""
    if rolling is False:
        deployment_strategy_id = appconfig_deploystrat_direct_id
    elif rolling is True:
        deployment_strategy_id = appconfig_deploystrat_rolling_id

    deploy = ac_client.start_deployment(
        ApplicationId=appconfig_app_id,
        EnvironmentId=appconfig_env_id,
        # deployment strategy
        DeploymentStrategyId=deployment_strategy_id,
        # configuration profile created on AppConfig hosted configuration profile
        ConfigurationProfileId=appconfig_confprof_cpid,
        ConfigurationVersion=str(version_number),
    )
    deployment_number = deploy["DeploymentNumber"]
    print("Deployment number:", deployment_number)
    for k, v in deploy.items():
        print(k, v)

    print("")
    if rolling:
        # Begin the 20 minute test
        import time
        from concurrent.futures import ThreadPoolExecutor

        print(
            "===> Deployment workflow finished, rolling update in progress, now monitoring new model in preparation for rollback"
        )
        print(
            "===> Now run the rolling_update_test_20mins() function manually to simulate load"
        )

        with ThreadPoolExecutor(max_workers=1) as executor:
            # This function is to simulate production workloads during rolling update
            # This will not be here in production
            future = executor.submit(
                rolling_update_test_20mins,
                project,
                test_dataset,
                test_dataset_tag,
                ENV,
            )

            # Poll GetDeployment until it reaches a terminal state
            while True:
                deployment = ac_client.get_deployment(
                    ApplicationId=appconfig_app_id,
                    EnvironmentId=appconfig_env_id,
                    DeploymentNumber=deployment_number,
                )

                state = deployment["State"]
                event_log = deployment.get("EventLog", [])

                print(f"Deployment state: {state}")

                if state == "COMPLETE":
                    print("✅Rolling update complete with no rollback")
                    print("This should not be here if baking time exists")
                    break

                elif state in ("ROLLING_BACK", "ROLLED_BACK", "REVERTED", "FAILED"):
                    raise RuntimeError(
                        f"Deployment ended in state {state}. "
                        f"Latest event: {event_log[-1] if event_log else 'None'}"
                    )
                # If the function has already finished stop polling
                if future.done():
                    print(
                        "✅rolling_update_test_20mins() finished. Now entering Baking time"
                    )
                    break

                time.sleep(10)

            future.result()
    else:
        print(
            "✅ Direct deployment finished, now monitoring new model in preparation for long-term model drift rollback (alarm must be created with real-time rollback function)"
        )


def create_drift_alarm(
    model_endpoint: str,
    model_adapter: str,
    ENV: str,
):
    """
    Creates a long-term drift alarm for a unique endpoint/adapter combination.
    """
    alarm_name = f"{ENV}-Drift-Alarm-1-{model_endpoint}-{model_adapter}"
    cw_client = boto3.client("cloudwatch")

    cw_client.put_metric_alarm(
        AlarmName=alarm_name,
        AlarmDescription=f"Long-term drift monitoring for adapter {model_adapter} on {model_endpoint}",
        Namespace=f"{ENV}-Long_contract_llm_drift_metrics",
        MetricName="CARF_3",
        Period=86400,  # 1 day
        EvaluationPeriods=1,
        DatapointsToAlarm=1,
        Statistic="Average",
        Threshold=0.7,
        ComparisonOperator="LessThanOrEqualToThreshold",
        # (Default): The alarm ignores the missing evaluation periods, and maintains its current state if there is not enough recent data.
        TreatMissingData="missing",
        # THESE DIMENSIONS MAKE IT UNIQUE
        Dimensions=[
            {
                "Name": "model_endpoint",
                "Value": model_endpoint,
            },
            {
                "Name": "model_adapter",
                "Value": model_adapter,
            },
        ],
        # AlarmActions=[] # eventbridge/sns ARN
    )
    print(f"✅ Successfully created alarm: {alarm_name} 🚨")


def test_lambda_few_rows(
    project,
    eval_data_key,
    eval_data_tag,
    l_client,
    ENV,
):

    # get input data handle data paths
    artifact = project.get_artifact(key=eval_data_key, tag=eval_data_tag)
    artifact_latest_s3_path = artifact.target_path

    # get test data
    test_dataset = ds.dataset(source=artifact_latest_s3_path, format="parquet")

    import random

    testSize = 4
    test_dataset = test_dataset.to_table().to_pylist()
    start = random.randint(0, len(test_dataset) - testSize)
    test_dataset = test_dataset[start: start + testSize]
    print(len(test_dataset))

    for doc in test_dataset:
        document_id = doc["document_id"]
        contract_data = doc["text"]
        payload = json.dumps({"contract": contract_data})

        print(f"Testing document {document_id}: {contract_data[:10]}")

        try:
            response = l_client.invoke(
                FunctionName=f"{ENV}-model_gateway",
                InvocationType="RequestResponse",
                Payload=payload,
            )
            print("✅ Invocation to lambda is sent successfully")
        except Exception as e:
            print("====> Invocation to lambda failed for some reason")
            print(e)

        if response:
            response_str = response["Payload"].read().decode("utf-8")
            hypotheses_list = json.loads(response_str)
            print("=======================================")
            print(hypotheses_list)
            print("=======================================")


def rolling_update_test_20mins(
    project,
    eval_data_key,
    eval_data_tag,
    ENV,
):

    import time

    interval = 5 * 60
    duration = 20 * 60

    start_time = time.monotonic()
    scheduled_times = [start_time + i * interval for i in range(duration // interval)]

    for scheduled_time in scheduled_times:

        # Define Lambda client with custom config
        custom_config = Config(
            region_name="us-east-1",
            read_timeout=900,
            connect_timeout=900,
            retries={"max_attempts": 1},
            max_pool_connections=10,
        )
        l_client = boto3.client("lambda", config=custom_config, region_name="us-east-1")

        now = time.monotonic()

        # Wait if we're early
        if now < scheduled_time:
            time.sleep(scheduled_time - now)
        try:
            test_lambda_few_rows(
                project,
                eval_data_key,
                eval_data_tag,
                l_client,
                ENV,
            )
        except Exception as e:
            print(e)

    print("20 minute rolling test finished")
