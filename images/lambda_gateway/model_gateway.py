"""
What this function does:

Reads prompt template from s3
Combines prompt template with input, then formats into chatml template. Perhaps transformers library isn't necessary
Enables model rolling updates by controlling traffic to model endpoints/Inference component adapters

Sends model drift metrics to cloudwatch (requires rouge-score library)
Sends all invocations and responses to data firehose

Variables are controlled by appconfig https://docs.aws.amazon.com/appconfig/latest/userguide/appconfig-creating-deployment-strategy.html

CW putmetridata API
https://docs.aws.amazon.com/boto3/latest/reference/services/cloudwatch/client/put_metric_data.html

"""

"""
Lambda logging sent to Cloudwatch as lambda log group:
Latency
Error messages/completion status
request id

Model monitoring log fields sent to S3:
endpoint_name
ic_adapter_name
prompt_template_uri (contains version)
input
output

Model drift logging sent to Cloudwatch under a model/adapter log group:
avg_rouge
count_entail
count_contradict
count_neutral

"""

import json
from datetime import datetime
import logging
import time
import uuid
import os
import urllib.request

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError, ReadTimeoutError, EndpointConnectionError
from rouge_score import rouge_scorer

# Objects and classes for logging
# https://docs.aws.amazon.com/lambda/latest/dg/python-logging.html

# environment name
ENV = os.environ["ENV"]


class InferenceError(Exception):
    """Raised when inference fails."""


logger = (
    logging.getLogger()
)  # All log outputs are captured in plain text in cloudwatch logs by default
logger.setLevel("INFO")

logger.info("✅ Imports and layers successful")


# Hard coded invocation parameters (MlRun does not store these on S3 for some reason)
# Not necessary if we can pass such long parameters to MLRun
def get_invocation_params():
    parameters = {
        "temperature": 0.2,
        "top_p": 0.95,
        "max_new_tokens": 3000,
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": "hypothesis_list",
                "schema": {
                    "type": "object",
                    "properties": {
                        "Hypotheses": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "hypothesis": {"type": "string"},
                                    "hypothesis_id": {"type": "string"},
                                    "label": {
                                        "type": "string",
                                        "enum": [
                                            "entailment",
                                            "contradiction",
                                            "not_mentioned",
                                        ],
                                    },
                                    "source_clause": {"type": "string"},
                                },
                                "required": [
                                    "hypothesis",
                                    "hypothesis_id",
                                    "label",
                                    "source_clause",
                                ],
                            },
                        }
                    },
                    "required": ["Hypotheses"],
                },
            },
        },
    }

    return parameters


def get_quote_drift_score(
    scorer,
    quote,
    contract,
):
    """
    This ensures that the quote is from the contract and not a hallucination. Checks in the form of precision score
    """
    scores = scorer.score(target=contract, prediction=quote)
    rouge3_precision = scores["rouge3"].precision

    return rouge3_precision


def get_payload(
    sys_prompt,
    input_contract,
    parameters,
):
    chatml_prompt = (
        "<|im_start|>system\n"
        f"{sys_prompt}<|im_end|>\n"
        "<|im_start|>user\n"
        f"{input_contract}<|im_end|>\n"
        "<|im_start|>assistant\n"
    )

    return {"inputs": chatml_prompt, "parameters": parameters}


def invoke_endpoint_with_ica(
    sm_client,
    payload,
    endpoint_name,
    adapter_name,
) -> list[dict]:
    start = time.perf_counter()
    # Make a request to the endpoint
    try:
        resp = sm_client.invoke_endpoint_with_response_stream(
            EndpointName=endpoint_name,
            Body=json.dumps(payload),
            ContentType="application/json",
            Accept="application/json",
            InferenceComponentName=adapter_name,
        )
    except (
        ClientError,
        EndpointConnectionError,
        ReadTimeoutError,
    ) as e:
        logger.exception("! Failed to invoke SageMaker endpoint")
        raise InferenceError("Endpoint invocation failed during invocation") from e

    # Process streaming response
    chunks = []
    try:
        for event in resp["Body"]:
            if "PayloadPart" in event:
                chunk = event["PayloadPart"]["Bytes"].decode("utf-8")
                chunks.append(chunk)
            else:
                raise InferenceError("Error during streaming")
                # Log this error

    except Exception as e:
        logger.exception("Error while reading response stream")
        raise InferenceError("Endpoint invocation failed during streaming") from e

    elapsed = time.perf_counter() - start
    logger.info(f"Inference completed in {elapsed:.2f}s")
    # Parse outer JSON response
    full_response = "".join(chunks)
    logger.info(f"Received full response from endpoint. Length: {len(full_response)}")

    try:
        response_json = json.loads(full_response)
    except json.JSONDecodeError as e:
        logger.exception("Model returned malformed JSON")
        raise InferenceError("Model output is not valid JSON") from e

    inference = response_json.get("generated_text")
    if inference is None:
        raise InferenceError("generated_text missing from endpoint response")

    # Parse model output
    try:
        inference_dict = json.loads(inference)
        hypotheses = inference_dict["Hypotheses"]  # array of hypotheses
    except Exception as e:
        logger.exception("! Hypotheses field missing from model output")
        raise InferenceError(e)

    return hypotheses


def request_handler(input_contract):
    # Request configuration profile and get values
    start = time.perf_counter()
    url = os.getenv("configurationProfileURL")
    # cw_log_group = os.getenv("CLOUDWATCH_LOG_GROUP")

    if url:
        print(f"configurationProfileURL found: {url}")
    else:
        print("configurationProfileURL is not set.")
    config_bytes = urllib.request.urlopen(url).read()
    # decode the bytes since it was uploaded in bytes with utf8 format
    config = json.loads(config_bytes.decode("utf-8"))

    logger.info(f"✅ Configuration profile -> {config}")
    elapsed = time.perf_counter() - start
    logger.info(f"✅ Configuration profile retrieved in {elapsed:.2f}s")

    target_endpoint, target_adapter = config["model_endpoint"], config["model_adapter"]
    prompt_template_uri = config["template_uri"]

    print(config)

    logger.info(f"✅{target_endpoint}\n{target_adapter}\n{prompt_template_uri}")

    # Declare boto3 clients
    cw_client = boto3.client(
        "cloudwatch",
        region_name="us-east-1",
    )
    s3_client = boto3.client(
        "s3",
        region_name="us-east-1",
    )
    custom_config = Config(
        read_timeout=900,  # 15 mins
        connect_timeout=900,
        retries={"max_attempts": 2},
        max_pool_connections=1,
    )
    sm_client = boto3.client(
        "sagemaker-runtime", config=custom_config, region_name="us-east-1"
    )

    # Get prompt template
    start = time.perf_counter()

    key = "/".join(prompt_template_uri.split("/")[3:])
    response = s3_client.get_object(
        Bucket=f"{ENV}-mlops-bucket-haviv",
        Key=key,
    )
    json_data = json.load(response["Body"])
    sys_prompt = json_data[0]["content"]
    elapsed = time.perf_counter() - start
    logger.info(f"✅ Prompt template downloaded in {elapsed:.2f}s")

    # Combine with contract and parameters. Form payload
    parameters = get_invocation_params()
    payload = get_payload(sys_prompt, input_contract, parameters)

    # error handling is inside invoke_endpoint_with_ica()
    inference_json = invoke_endpoint_with_ica(
        sm_client, payload, target_endpoint, target_adapter
    )
    # Validate that inference_json is a list of dictionaries
    if not isinstance(inference_json, list):
        logger.info(f"❌ inference_json is not a list:\n{inference_json}")
        raise TypeError(f"❌ inference_json is not a list:\n{inference_json}")
    # print('@==================================================================')
    # print(inference_json)

    # Log model drift metrics to cloudwatch
    # Can this bulky section be moved to after the response is returned to the user?
    start = time.perf_counter()

    t_s = datetime.now().replace(microsecond=0)
    shortmetricdata = []
    longmetricdata = []
    carp_values = []  # Contract average rouge3_precision

    scorer = rouge_scorer.RougeScorer(["rouge3"], use_stemmer=False)

    # Log label data to CW
    for h in inference_json:
        hypothesis_id = h.get("hypothesis_id")
        label = h.get("label")
        sc = h.get("source_clause")

        if label != "not_mentioned":
            rouge3_precision = get_quote_drift_score(scorer, sc, input_contract)
            # logger.info(f"⚠️{random_uuid}:{rouge3_precision}")
            # logger.info(f"⚠️{random_uuid}:\n{sc}")

            if isinstance(rouge3_precision, (float, int)):
                carp_values.append(rouge3_precision)
            else:
                print(f"carp_value is invalid for {hypothesis_id}")
            print("CARP VALUE ADDED")
        else:
            print("CARP VALUE NOT ADDED")

        short_data = {
            "MetricName": f"Hlabel_{hypothesis_id}",
            "Dimensions": [
                {
                    "Name": "label",
                    "Value": label,
                }
            ],
            "Timestamp": t_s,
            "Value": 1,
            "Unit": "Count",
        }
        shortmetricdata.append(short_data)

        long_data = {
            "MetricName": f"Hlabel_{hypothesis_id}",
            "Dimensions": [
                {"Name": "model_endpoint", "Value": target_endpoint},
                {"Name": "model_adapter", "Value": target_adapter},
                {
                    "Name": "label",
                    "Value": label,
                },
            ],
            "Timestamp": t_s,
            "Value": 1,
            "Unit": "Count",
        }

        longmetricdata.append(long_data)

    # Log CARP data to CW
    # WATCH OUT FOR DIVIDE BY 0 errors
    print(f"CARP VALUES: {carp_values}")
    if len(carp_values) == 0:
        avg_carp = 1.0
    else:
        avg_carp = sum(carp_values) / len(carp_values)
    logger.info(f"⚠️Average CARP: {avg_carp}")
    short_carp_data = {
        "MetricName": "CARP_3",
        "Timestamp": t_s,
        "Value": avg_carp,
    }
    shortmetricdata.append(short_carp_data)

    long_carp_data = {
        "MetricName": "CARP_3",
        "Timestamp": t_s,
        "Value": avg_carp,
        "Dimensions": [
            {
                "Name": "model_endpoint",
                "Value": target_endpoint,
            },
            {
                "Name": "model_adapter",
                "Value": target_adapter,
            },
        ],
    }
    longmetricdata.append(long_carp_data)

    # Short term metrics for deployment rollback
    cw_client.put_metric_data(
        Namespace=f"{ENV}-Short_contract_llm_drift_metrics",
        MetricData=shortmetricdata,
    )

    cw_client.put_metric_data(
        Namespace=f"{ENV}-Long_contract_llm_drift_metrics",
        MetricData=longmetricdata,
    )

    # Long term metrics for model drift
    elapsed = time.perf_counter() - start
    logger.info(f"✅ Drift metrics calculated and sent in {elapsed:.2f}s")
    print(shortmetricdata)

    # Log metrics to data firehose + Arize/Langfuse

    return inference_json


def lambda_handler(
    event,
    context,
):
    random_uuid = str(uuid.uuid4())
    print("=========================================================")
    print("0.3.1")
    print(random_uuid)
    print("=========================================================")
    print(event)

    # response_data = {}
    input_contract = event["contract"]
    response_data = request_handler(input_contract)

    # print(f"✅{random_uuid}:\n{event}")

    return response_data
