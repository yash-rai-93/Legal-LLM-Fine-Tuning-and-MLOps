"""IAM HTTP API handler: read one completed run's metadata from DynamoDB.

This function cannot start workflows/builds or read contract objects from S3.
API Gateway must use AWS_IAM and payload format 2.0 for the sole GET route.
"""
from decimal import Decimal
import json
import logging
import os
import uuid


class RequestError(Exception):
    def __init__(self, status, message):
        self.status, self.message = status, message


def _mapping(value):
    return value if isinstance(value, dict) else {}


def _reply(status, payload):
    return {
        "statusCode": status,
        "headers": {
            "content-type": "application/json",
            "cache-control": "no-store",
            "x-content-type-options": "nosniff",
        },
        "isBase64Encoded": False,
        "body": json.dumps(payload, allow_nan=False, separators=(",", ":")),
    }


def _run_id(event, expected_api_id):
    event = _mapping(event)
    context = _mapping(event.get("requestContext"))
    iam = _mapping(_mapping(context.get("authorizer")).get("iam"))
    user_arn = iam.get("userArn")
    if (
        not expected_api_id or context.get("apiId") != expected_api_id
        or not isinstance(user_arn, str) or not user_arn.startswith("arn:aws:")
    ):
        raise RequestError(403, "Forbidden")
    if event.get("version") != "2.0":
        raise RequestError(400, "Unsupported request format")
    if event.get("routeKey") != "GET /runs/{run_id}":
        raise RequestError(404, "Route not found")
    if _mapping(context.get("http")).get("method") != "GET":
        raise RequestError(405, "Only GET is supported")
    if event.get("queryStringParameters"):
        raise RequestError(400, "Query parameters are not supported")
    run_id = _mapping(event.get("pathParameters")).get("run_id")
    try:
        if not isinstance(run_id, str) or str(uuid.UUID(run_id)) != run_id:
            raise ValueError("Invalid UUID")
    except (ValueError, AttributeError):
        raise RequestError(400, "Invalid run ID") from None
    return run_id


def _integer(value):
    if type(value) is int:
        return value
    if isinstance(value, Decimal) and value.is_finite() and value == value.to_integral_value():
        return int(value)
    raise ValueError("Expected integral metadata")


def _public_record(item, run_id):
    if not (
        item.get("run_id") == run_id
        and item.get("registry_version") == "contractnli-run-v1"
        and item.get("status") == "COMPLETED"
        and item.get("operation") == "data_preparation"
        and item.get("live_inference") is False
    ):
        raise ValueError("Unsupported run record")
    result = {
        "api_schema_version": "contractnli-status-api-v1",
        "run_id": run_id, "status": "COMPLETED",
        "operation": "data_preparation", "live_inference": False,
    }
    for name in (
        "build_id", "source_commit", "registry_commit", "bucket", "prefix",
        "registered_at", "manifest_created_at", "dataset_source", "dataset_license",
    ):
        if not isinstance(item.get(name), str) or not item[name]:
            raise ValueError("Missing metadata field")
        result[name] = item[name]
    result["counts"] = {}
    for split in ("train", "validation"):
        counts = item["counts"][split]
        documents, rows = _integer(counts["documents"]), _integer(counts["input_rows"])
        if documents <= 0 or rows != 17 * documents:
            raise ValueError("Invalid metadata counts")
        result["counts"][split] = {"documents": documents, "input_rows": rows}
    result["artifacts"] = {}
    for name in ("manifest.json", "train.jsonl", "validation.jsonl"):
        artifact = item["artifacts"][name]
        exposed = {}
        for field in ("key", "version_id", "sha256", "encryption"):
            if not isinstance(artifact.get(field), str) or not artifact[field]:
                raise ValueError("Invalid artifact metadata")
            exposed[field] = artifact[field]
        exposed["bytes"] = _integer(artifact["bytes"])
        if exposed["bytes"] <= 0:
            raise ValueError("Invalid artifact length")
        result["artifacts"][name] = exposed
    return result


def handle_request(event, *, expected_api_id, table_factory):
    try:
        run_id = _run_id(event, expected_api_id)
    except RequestError as exc:
        return _reply(exc.status, {"error": exc.message})
    try:
        result = table_factory().get_item(Key={"run_id": run_id}, ConsistentRead=True)
        item = result.get("Item")
        if item is None:
            return _reply(404, {"error": "Run not found"})
        return _reply(200, _public_record(item, run_id))
    except Exception as exc:
        # Do not log event bodies, IAM credentials, table items or SDK messages.
        logging.getLogger(__name__).error("Run metadata lookup failed (%s)", type(exc).__name__)
        return _reply(503, {"error": "Run metadata is temporarily unavailable"})


def lambda_handler(event, _context):
    def table_factory():
        import boto3
        from botocore.config import Config

        config = Config(connect_timeout=2, read_timeout=2, retries={"total_max_attempts": 2})
        return boto3.resource("dynamodb", config=config).Table(os.environ["RUN_TABLE"])

    return handle_request(
        event, expected_api_id=os.environ["HTTP_API_ID"], table_factory=table_factory
    )
