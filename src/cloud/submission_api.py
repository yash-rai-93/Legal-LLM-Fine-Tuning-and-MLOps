"""Bounded IAM CPU submissions. No S3, build-start, model or deployment access.

A separate table atomically reserves a durable request and one global lock.
Only a successful Standard execution releases that lock automatically. Failed,
aborted, timed-out or uncertain executions require operator recovery.
"""
from datetime import datetime, timezone
import json
import logging
import os
import re
import uuid

SCHEMA = "contractnli-submission-v1"
LOCK = "control#active"
QUOTA = "control#quota"
MAX_ADMISSIONS = 3
PROCESSOR_COMMIT = "8a1df10ea83cb9f5c476e849491cd427651c1c39"
REGISTRY_COMMIT = "39d979d9fc0e40457b3ae81e9a9a3f445099fa61"
REPOSITORY = "https://github.com/yash-rai-93/Legal-LLM-Fine-Tuning-and-MLOps"
TERMINAL = {"COMPLETED", "FAILED", "ABORTED", "TIMED_OUT"}
VERSION_ARN = re.compile(
    r"arn:aws:states:us-east-1:[0-9]{12}:stateMachine:legal-llm-cpu-prepare:[1-9][0-9]*"
)
START_WINDOW_SECONDS = 300


class RequestError(Exception):
    def __init__(self, status, message):
        self.status, self.message = status, message


class Busy(Exception):
    pass


def _code(exc):
    return getattr(exc, "response", {}).get("Error", {}).get("Code")


def _mapping(value):
    return value if isinstance(value, dict) else {}


def _uuid(value):
    if not isinstance(value, str) or str(uuid.UUID(value)) != value:
        raise ValueError("Invalid UUID")
    return value


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate key")
        result[key] = value
    return result


def _request(event, expected_api_id):
    event = _mapping(event)
    context = _mapping(event.get("requestContext"))
    iam = _mapping(_mapping(context.get("authorizer")).get("iam"))
    arn = iam.get("userArn")
    if (not expected_api_id or context.get("apiId") != expected_api_id
            or not isinstance(arn, str) or not arn.startswith("arn:aws:")):
        raise RequestError(403, "Forbidden")
    if event.get("version") != "2.0":
        raise RequestError(400, "Unsupported request format")
    if event.get("queryStringParameters") or event.get("rawQueryString"):
        raise RequestError(400, "Query parameters are not supported")
    route = event.get("routeKey")
    method = _mapping(context.get("http")).get("method")
    if route == "GET /submissions/{run_id}":
        if method != "GET":
            raise RequestError(405, "Only GET is supported")
        if event.get("body") not in (None, ""):
            raise RequestError(400, "GET bodies are not supported")
        try:
            return "GET", _uuid(_mapping(event.get("pathParameters")).get("run_id"))
        except (ValueError, AttributeError):
            raise RequestError(400, "Invalid submission ID") from None
    if route != "POST /runs":
        raise RequestError(404, "Route not found")
    if method != "POST":
        raise RequestError(405, "Only POST is supported")
    headers = _mapping(event.get("headers"))
    # API Gateway payload 2.0 normalizes names and joins duplicate headers.
    try:
        run_id = _uuid(headers.get("idempotency-key"))
        if uuid.UUID(run_id).version != 4:
            raise ValueError("Expected UUID4")
    except (ValueError, AttributeError):
        raise RequestError(400, "Idempotency-Key must be a canonical UUID4") from None
    body = event.get("body")
    try:
        size = len(body.encode("utf-8")) if isinstance(body, str) else 1025
    except UnicodeEncodeError:
        size = 1025
    if (event.get("isBase64Encoded") not in (None, False) or size > 1024):
        raise RequestError(400, "Use an unencoded JSON body of at most 1024 bytes")
    if headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json":
        raise RequestError(415, "Use application/json")
    try:
        payload = json.loads(body, object_pairs_hook=_pairs)
    except (ValueError, UnicodeError):
        raise RequestError(400, "Invalid JSON body") from None
    if payload != {"operation": "data_preparation"}:
        raise RequestError(400, "The only request body is operation=data_preparation")
    return "POST", run_id


def _reply(status, payload):
    return {
        "statusCode": status,
        "headers": {"content-type": "application/json", "cache-control": "no-store",
                    "x-content-type-options": "nosniff"},
        "isBase64Encoded": False,
        "body": json.dumps(payload, allow_nan=False, separators=(",", ":")),
    }


def execution_arn(version_arn, run_id):
    if not isinstance(version_arn, str) or not VERSION_ARN.fullmatch(version_arn):
        raise ValueError("A numeric immutable CPU workflow version ARN is required")
    _uuid(run_id)
    base = version_arn.rsplit(":", 1)[0]
    return base.replace(":stateMachine:", ":execution:") + ":api-" + run_id


def validate_runtime_project(project):
    environment, source = project.get("environment", {}), project.get("source", {})
    if (project.get("name") != "legal-llm-cpu-check"
            or type(project.get("concurrentBuildLimit")) is not int or project["concurrentBuildLimit"] != 1
            or type(project.get("autoRetryLimit")) is not int or project["autoRetryLimit"] != 0
            or environment.get("computeType") != "BUILD_GENERAL1_SMALL"
            or environment.get("type") != "LINUX_CONTAINER"
            or environment.get("image") != "aws/codebuild/standard:7.0"
            or environment.get("privilegedMode") is not False
            or source.get("type") != "GITHUB"
            or source.get("buildspec") != "buildspecs/cpu-publish.yml"
            or source.get("location", "").rstrip("/").removesuffix(".git") != REPOSITORY):
        raise ValueError("Runtime CPU project settings differ from the approved limits/source")


def _strings(item):
    if not all(isinstance(value, str) for value in item.values()):
        raise ValueError("Submission storage accepts string metadata only")
    return {key: {"S": value} for key, value in item.items()}


class SubmissionStore:
    """Low-level DynamoDB client avoids mixing resource/client serialization."""
    def __init__(self, client, table_name):
        self.client, self.table_name = client, table_name

    def get(self, key):
        item = self.client.get_item(
            TableName=self.table_name, Key={"run_id": {"S": key}}, ConsistentRead=True
        ).get("Item")
        if item is None:
            return None
        if not all(isinstance(value, dict) and set(value) == {"S"} for value in item.values()):
            raise ValueError("Invalid submission storage")
        return {key: value["S"] for key, value in item.items()}

    def claim(self, item):
        quota = self.get(QUOTA)
        count = int(quota["admissions"]) if quota is not None else 0
        if not 0 <= count <= MAX_ADMISSIONS:
            raise ValueError("Invalid demo admission counter")
        if count >= MAX_ADMISSIONS:
            raise RequestError(429, "Demo submission allowance exhausted")
        quota_put = {
            "TableName": self.table_name,
            "Item": _strings({"run_id": QUOTA, "admissions": str(count + 1)}),
            "ConditionExpression": "attribute_not_exists(run_id)",
        }
        if quota is not None:
            quota_put.update(
                ConditionExpression="admissions = :expected",
                ExpressionAttributeValues={":expected": {"S": str(count)}},
            )
        self.client.transact_write_items(TransactItems=[
            {"Put": {"TableName": self.table_name, "Item": _strings(item),
                     "ConditionExpression": "attribute_not_exists(run_id)"}},
            {"Put": {"TableName": self.table_name,
                     "Item": _strings({"run_id": LOCK, "owner": item["run_id"]}),
                     "ConditionExpression": "attribute_not_exists(run_id)"}},
            {"Put": quota_put},
        ])

    def finish(self, item):
        put = {"TableName": self.table_name, "Item": _strings(item),
               "ConditionExpression": "#state = :starting AND execution_arn = :arn",
               "ExpressionAttributeNames": {"#state": "status"},
               "ExpressionAttributeValues": {":starting": {"S": "STARTING"},
                                             ":arn": {"S": item["execution_arn"]}}}
        actions = [{"Put": put}]
        if item["status"] == "COMPLETED":
            actions.append({"Delete": {
                "TableName": self.table_name, "Key": {"run_id": {"S": LOCK}},
                "ConditionExpression": "#owner = :owner",
                "ExpressionAttributeNames": {"#owner": "owner"},
                "ExpressionAttributeValues": {":owner": {"S": item["run_id"]}},
            }})
        self.client.transact_write_items(TransactItems=actions)


class SubmissionService:
    def __init__(self, *, store, sfn, workflow_version_arn, now=None, project_checker=None):
        execution_arn(workflow_version_arn, "00000000-0000-4000-8000-000000000001")
        self.store, self.sfn, self.workflow_version_arn = store, sfn, workflow_version_arn
        self.now = now or (lambda: datetime.now(timezone.utc))
        self.project_checker = project_checker

    def _validate(self, item, run_id):
        if (item.get("schema_version") != SCHEMA or item.get("run_id") != run_id
                or item.get("status") not in TERMINAL | {"STARTING"}
                or item.get("execution_arn") != execution_arn(item.get("workflow_version_arn"), run_id)):
            raise ValueError("Invalid submission record")
        created = datetime.fromisoformat(item["created_at"])
        if created.utcoffset() is None:
            raise ValueError("Missing timestamp timezone")
        return (self.now() - created).total_seconds()

    def _public(self, item, status, *, recovery=False):
        result = {
            "api_schema_version": SCHEMA, "run_id": item["run_id"],
            "status": status, "operation": "data_preparation", "live_inference": False,
            "created_at": item["created_at"],
            "status_url": "/submissions/" + item["run_id"],
            "recovery_required": recovery,
        }
        for field in ("completed_at", "registered_run_id"):
            if field in item:
                result[field] = item[field]
        if "registered_run_id" in result:
            result["artifacts_status_url"] = "/runs/" + result["registered_run_id"]
        return result

    def observe(self, item):
        age = self._validate(item, item["run_id"])
        if item["status"] in TERMINAL:
            return self._public(item, item["status"], recovery=item["status"] != "COMPLETED")
        try:
            execution = self.sfn.describe_execution(executionArn=item["execution_arn"])
        except Exception as exc:
            if _code(exc) != "ExecutionDoesNotExist":
                raise
            recovery = not 0 <= age < START_WINDOW_SECONDS
            return self._public(item, "RECOVERY_REQUIRED" if recovery else "STARTING", recovery=recovery)
        if (execution.get("executionArn") != item["execution_arn"]
                or execution.get("stateMachineVersionArn") != item["workflow_version_arn"]
                or execution.get("input") != "{}" or execution.get("redriveCount", 0) != 0):
            raise ValueError("Execution identity mismatch")
        status = execution.get("status")
        if status == "RUNNING":
            return self._public(item, "RUNNING")
        if status not in {"SUCCEEDED", "FAILED", "ABORTED", "TIMED_OUT"}:
            raise ValueError("Unexpected execution state; recovery required")
        stopped = execution.get("stopDate")
        if not isinstance(stopped, datetime) or stopped.utcoffset() is None:
            raise ValueError("Missing authoritative workflow completion time")
        finished = {**item, "status": "COMPLETED" if status == "SUCCEEDED" else status,
                    "completed_at": stopped.isoformat()}
        if status == "SUCCEEDED":
            output = json.loads(execution.get("output", ""), object_pairs_hook=_pairs)
            if (not isinstance(output, dict) or output.get("status") != "COMPLETED"
                    or output.get("operation") != "data_preparation"
                    or output.get("live_inference") is not False
                    or output.get("source_commit") != PROCESSOR_COMMIT
                    or output.get("registry_commit") != REGISTRY_COMMIT):
                raise ValueError("Missing verified registration result")
            registered_id = _uuid(output.get("run_id"))
            if output.get("build_id") != "legal-llm-cpu-check:" + registered_id:
                raise ValueError("Registration build identity mismatch")
            for split, documents in (("train", 423), ("validation", 61)):
                counts = output["counts"][split]
                if (type(counts["documents"]) is not int or counts["documents"] != documents
                        or type(counts["input_rows"]) is not int or counts["input_rows"] != 17 * documents):
                    raise ValueError("Registration document counts differ")
            finished["registered_run_id"] = registered_id
        try:
            self.store.finish(finished)
        except Exception as exc:
            if _code(exc) != "TransactionCanceledException":
                raise
            current = self.store.get(item["run_id"])
            self._validate(current, item["run_id"])
            if current["status"] != finished["status"]:
                raise ValueError("Concurrent status conflict") from None
            finished = current
        return self._public(finished, finished["status"], recovery=status != "SUCCEEDED")

    def get(self, run_id):
        item = self.store.get(run_id)
        if item is None:
            raise RequestError(404, "Submission not found")
        self._validate(item, run_id)
        return self.observe(item)

    def _start(self, item):
        age = self._validate(item, item["run_id"])
        if (item["workflow_version_arn"] != self.workflow_version_arn
                or not 0 <= age < START_WINDOW_SECONDS):
            raise RequestError(503, "Start requires operator recovery; reservation retained")
        # Same execution name AND exact input on every retry, including ambiguous
        # network failures. Never generate a new name or drop the reservation.
        if self.project_checker is not None:
            self.project_checker()
        try:
            result = self.sfn.start_execution(
                stateMachineArn=self.workflow_version_arn,
                name="api-" + item["run_id"], input="{}",
            )
        except Exception as exc:
            if _code(exc) != "ExecutionAlreadyExists":
                raise
            return self.observe(item)
        if result.get("executionArn") != item["execution_arn"]:
            raise ValueError("StartExecution returned a different execution")
        return self._public(item, "RUNNING")

    def submit(self, run_id):
        item = self.store.get(run_id)
        if item is not None:
            self._validate(item, run_id)
            observed = self.observe(item)
            return self._start(item) if observed["status"] == "STARTING" else observed
        active = self.store.get(LOCK)
        if active is not None:
            active_id = _uuid(active["owner"])
            active_item = self.store.get(active_id)
            self._validate(active_item, active_id)
            if self.observe(active_item)["status"] != "COMPLETED":
                raise Busy()
        item = {
            "schema_version": SCHEMA, "run_id": run_id, "status": "STARTING",
            "execution_arn": execution_arn(self.workflow_version_arn, run_id),
            "workflow_version_arn": self.workflow_version_arn,
            "created_at": self.now().isoformat(),
        }
        try:
            self.store.claim(item)
        except Exception as exc:
            if _code(exc) != "TransactionCanceledException":
                raise
            # One bounded read handles a simultaneous request for this key.
            existing = self.store.get(run_id)
            if existing is None:
                raise Busy() from None
            self._validate(existing, run_id)
            observed = self.observe(existing)
            return self._start(existing) if observed["status"] == "STARTING" else observed
        return self._start(item)


def handle_request(event, *, expected_api_id, service_factory, submissions_enabled=False):
    try:
        method, run_id = _request(event, expected_api_id)
        if method == "POST" and not submissions_enabled:
            raise RequestError(503, "Submissions are disabled")
        service = service_factory()
        result = service.submit(run_id) if method == "POST" else service.get(run_id)
        return _reply(202 if method == "POST" and result["status"] in {"STARTING", "RUNNING"} else 200, result)
    except RequestError as exc:
        return _reply(exc.status, {"error": exc.message})
    except Busy:
        return _reply(409, {"error": "Another submission holds the active-run reservation"})
    except Exception as exc:
        # Never log the request, IAM context, workflow output or SDK exception.
        logging.getLogger(__name__).error("Submission request failed (%s)", type(exc).__name__)
        payload = {"error": "Workflow status unavailable; retry the SAME idempotency key"}
        if "run_id" in locals():
            payload.update(run_id=run_id, status_url="/submissions/" + run_id)
        return _reply(503, payload)


def lambda_handler(event, _context):
    def service_factory():
        import boto3
        from botocore.config import Config

        # No SDK automatic retries: explicit same-key recovery bounds work.
        config = Config(connect_timeout=2, read_timeout=3, retries={"total_max_attempts": 1})
        codebuild = boto3.client("codebuild", config=config)
        def project_checker():
            response = codebuild.batch_get_projects(names=["legal-llm-cpu-check"])
            if response.get("projectsNotFound") or len(response.get("projects", [])) != 1:
                raise ValueError("CPU project unavailable")
            validate_runtime_project(response["projects"][0])
        return SubmissionService(
            store=SubmissionStore(boto3.client("dynamodb", config=config), os.environ["SUBMISSION_TABLE"]),
            sfn=boto3.client("stepfunctions", config=config),
            workflow_version_arn=os.environ["WORKFLOW_VERSION_ARN"],
            project_checker=project_checker,
        )
    return handle_request(
        event, expected_api_id=os.environ.get("HTTP_API_ID"), service_factory=service_factory,
        submissions_enabled=os.environ.get("SUBMISSIONS_ENABLED") == "true",
    )
