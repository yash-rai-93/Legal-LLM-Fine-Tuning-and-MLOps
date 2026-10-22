"""Synthetic bounded-submission/failure tests. Run in AWS CodeBuild only."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
import threading
import unittest

from src.cloud.submission_api import (
    LOCK, SubmissionService, SubmissionStore, execution_arn, handle_request,
)

VERSION = "arn:aws:states:us-east-1:123456789012:stateMachine:legal-llm-cpu-prepare:1"
RUN = "bc39c8af-66b7-4f1d-bc38-71870e2e52ab"
OTHER = "1d58ebca-85c9-4e99-b1f9-ae0194de35bf"
BUILD = "1605eeda-3334-4fc4-a8f3-c5c047f75994"
NOW = datetime(2026, 10, 22, 15, 0, tzinfo=timezone.utc)


class AwsError(Exception):
    def __init__(self, code):
        self.response = {"Error": {"Code": code, "Message": "PRIVATE SDK MESSAGE"}}


class Store:
    def __init__(self):
        self.items, self.lock = {}, threading.Lock()
        self.claims, self.finishes = 0, 0

    def get(self, key):
        with self.lock:
            return deepcopy(self.items.get(key))

    def claim(self, item):
        with self.lock:
            if item["run_id"] in self.items or LOCK in self.items:
                raise AwsError("TransactionCanceledException")
            self.items[item["run_id"]] = deepcopy(item)
            self.items[LOCK] = {"run_id": LOCK, "owner": item["run_id"]}
            self.claims += 1

    def finish(self, item):
        with self.lock:
            if self.items[item["run_id"]]["status"] != "STARTING":
                raise AwsError("TransactionCanceledException")
            if item["status"] == "COMPLETED" and self.items[LOCK]["owner"] != item["run_id"]:
                raise AwsError("TransactionCanceledException")
            self.items[item["run_id"]] = deepcopy(item)
            if item["status"] == "COMPLETED":
                del self.items[LOCK]
            self.finishes += 1


class Sfn:
    def __init__(self):
        self.executions, self.calls = {}, []
        self.lock, self.started = threading.Lock(), 0
        self.start_error, self.error_after_start = None, False

    def describe_execution(self, *, executionArn):
        with self.lock:
            if executionArn not in self.executions:
                raise AwsError("ExecutionDoesNotExist")
            return deepcopy(self.executions[executionArn])

    def start_execution(self, **args):
        with self.lock:
            self.calls.append(deepcopy(args))
            if self.start_error is not None:
                raise self.start_error
            arn = execution_arn(args["stateMachineArn"], args["name"].removeprefix("api-"))
            if arn in self.executions:
                if self.executions[arn]["status"] != "RUNNING":
                    raise AwsError("ExecutionAlreadyExists")
            else:
                self.executions[arn] = {
                    "executionArn": arn, "stateMachineVersionArn": args["stateMachineArn"],
                    "input": args["input"], "status": "RUNNING",
                }
                self.started += 1
            if self.error_after_start:
                raise TimeoutError("PRIVATE SDK MESSAGE")
            return {"executionArn": arn}

    def terminal(self, run_id, status="SUCCEEDED"):
        execution = self.executions[execution_arn(VERSION, run_id)]
        execution["status"] = status
        execution["stopDate"] = NOW + timedelta(minutes=2)
        execution["output"] = json.dumps({
            "status": "COMPLETED", "operation": "data_preparation", "live_inference": False,
            "run_id": BUILD, "build_id": "legal-llm-cpu-check:" + BUILD,
        })


def event(method="POST", run_id=RUN):
    return {
        "version": "2.0",
        "routeKey": "POST /runs" if method == "POST" else "GET /submissions/{run_id}",
        "requestContext": {"apiId": "fixtureapi", "http": {"method": method},
                           "authorizer": {"iam": {"userArn": "arn:aws:iam::123456789012:user/fixture"}}},
        "headers": {"content-type": "application/json", "idempotency-key": run_id},
        "pathParameters": {"run_id": run_id},
        "body": '{"operation":"data_preparation"}',
    }


class SubmissionTests(unittest.TestCase):
    def setUp(self):
        self.store, self.sfn = Store(), Sfn()
        self.clock = NOW
        self.service = SubmissionService(
            store=self.store, sfn=self.sfn, workflow_version_arn=VERSION, now=lambda: self.clock,
        )
        self.factory_calls = 0

    def handle(self, request=None, enabled=True):
        def factory():
            self.factory_calls += 1
            return self.service
        return handle_request(request or event(), expected_api_id="fixtureapi",
                              service_factory=factory, submissions_enabled=enabled)

    def test_submission_returns_202_and_starts_only_fixed_workflow_input(self):
        response = self.handle()
        self.assertEqual(response["statusCode"], 202)
        payload = json.loads(response["body"])
        self.assertEqual(payload["status"], "RUNNING")
        self.assertFalse(payload["live_inference"])
        self.assertEqual(payload["status_url"], "/submissions/" + RUN)
        self.assertEqual(self.sfn.calls, [{"stateMachineArn": VERSION, "name": "api-" + RUN, "input": "{}"}])
        self.assertEqual(self.store.get(LOCK)["owner"], RUN)

    def test_duplicate_request_reuses_one_execution(self):
        first = self.handle()
        for _ in range(3):
            self.assertEqual(self.handle()["body"], first["body"])
        self.assertEqual(self.sfn.started, 1)
        self.assertEqual(self.store.claims, 1)

    def test_second_key_cannot_queue_work(self):
        self.handle()
        self.assertEqual(self.handle(event(run_id=OTHER))["statusCode"], 409)
        self.assertIsNone(self.store.get(OTHER))
        self.assertEqual(self.sfn.started, 1)

    def test_concurrent_different_keys_admit_one(self):
        barrier = threading.Barrier(2)
        def call(run_id):
            barrier.wait()
            return self.handle(event(run_id=run_id))["statusCode"]
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(call, [RUN, OTHER]))
        self.assertEqual(sorted(results), [202, 409])
        self.assertEqual(self.sfn.started, 1)

    def test_concurrent_same_key_cannot_create_multiple_executions(self):
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda _: self.handle()["statusCode"], range(4)))
        self.assertEqual(results, [202] * 4)
        self.assertEqual(self.sfn.started, 1)
        self.assertEqual(self.store.claims, 1)

    def test_start_error_retains_reservation_and_same_key_can_recover(self):
        self.sfn.start_error = AwsError("AccessDeniedException")
        with self.assertLogs("src.cloud.submission_api", level="ERROR"):
            response = self.handle()
        self.assertEqual(response["statusCode"], 503)
        self.assertEqual(json.loads(response["body"])["run_id"], RUN)
        self.assertNotIn("PRIVATE", response["body"])
        self.assertEqual(self.store.get(LOCK)["owner"], RUN)
        self.assertEqual(self.handle(event(run_id=OTHER))["statusCode"], 409)
        self.sfn.start_error = None
        self.assertEqual(self.handle()["statusCode"], 202)
        self.assertEqual(self.sfn.started, 1)

    def test_timeout_after_actual_start_is_not_replaced(self):
        self.sfn.error_after_start = True
        with self.assertLogs("src.cloud.submission_api", level="ERROR"):
            self.assertEqual(self.handle()["statusCode"], 503)
        self.assertEqual(self.handle()["statusCode"], 202)
        self.assertEqual(len(self.sfn.calls), 1)

    def test_unknown_start_outside_window_requires_recovery_without_restart(self):
        self.sfn.start_error = TimeoutError()
        with self.assertLogs("src.cloud.submission_api", level="ERROR"):
            self.handle()
        self.clock += timedelta(seconds=301)
        response = self.handle()
        self.assertEqual(json.loads(response["body"])["status"], "RECOVERY_REQUIRED")
        self.assertEqual(len(self.sfn.calls), 1)
        self.assertIn(LOCK, self.store.items)

    def test_start_reservation_survives_transaction_response_loss(self):
        original = self.store.claim
        def ambiguous(item):
            original(item)
            raise TimeoutError()
        self.store.claim = ambiguous
        with self.assertLogs("src.cloud.submission_api", level="ERROR"):
            self.assertEqual(self.handle()["statusCode"], 503)
        self.assertEqual(self.sfn.started, 0)
        self.assertEqual(self.handle()["statusCode"], 202)
        self.assertEqual(self.sfn.started, 1)

    def test_running_get_does_not_start_work(self):
        self.handle()
        self.assertEqual(json.loads(self.handle(event("GET"))["body"])["status"], "RUNNING")
        self.assertEqual(len(self.sfn.calls), 1)

    def test_success_records_registered_build_and_releases_lock_atomically(self):
        self.handle()
        self.sfn.terminal(RUN)
        payload = json.loads(self.handle(event("GET"))["body"])
        self.assertEqual(payload["status"], "COMPLETED")
        self.assertEqual(payload["completed_at"], (NOW + timedelta(minutes=2)).isoformat())
        self.assertEqual(payload["registered_run_id"], BUILD)
        self.assertEqual(payload["artifacts_status_url"], "/runs/" + BUILD)
        self.assertNotIn(LOCK, self.store.items)
        self.assertEqual(self.handle()["statusCode"], 200)
        self.assertEqual(self.sfn.started, 1)
        self.assertEqual(self.handle(event(run_id=OTHER))["statusCode"], 202)

    def test_new_key_reconciles_success_without_needing_prior_get(self):
        self.handle()
        self.sfn.terminal(RUN)
        self.assertEqual(self.handle(event(run_id=OTHER))["statusCode"], 202)
        self.assertEqual(self.sfn.started, 2)

    def test_concurrent_completion_reads_are_idempotent(self):
        self.handle()
        self.sfn.terminal(RUN)
        with ThreadPoolExecutor(max_workers=4) as pool:
            responses = list(pool.map(lambda _: self.handle(event("GET")), range(4)))
        self.assertTrue(all(r["statusCode"] == 200 for r in responses))
        self.assertEqual(self.store.finishes, 1)

    def test_failed_aborted_timed_out_runs_keep_lock_until_child_recovery(self):
        for state in ("FAILED", "ABORTED", "TIMED_OUT"):
            with self.subTest(state=state):
                self.setUp()
                self.handle()
                self.sfn.terminal(RUN, state)
                payload = json.loads(self.handle(event("GET"))["body"])
                self.assertEqual(payload["status"], state)
                self.assertTrue(payload["recovery_required"])
                self.assertIn(LOCK, self.store.items)
                self.assertEqual(self.handle(event(run_id=OTHER))["statusCode"], 409)
                self.assertEqual(self.handle()["statusCode"], 200)
                self.assertEqual(self.sfn.started, 1)

    def test_invalid_success_output_cannot_release_lock(self):
        self.handle()
        self.sfn.terminal(RUN)
        self.sfn.executions[execution_arn(VERSION, RUN)]["output"] = '{"status":"COMPLETED"}'
        with self.assertLogs("src.cloud.submission_api", level="ERROR"):
            self.assertEqual(self.handle(event("GET"))["statusCode"], 503)
        self.assertIn(LOCK, self.store.items)

    def test_execution_identity_mismatch_cannot_release_lock(self):
        self.handle()
        self.sfn.terminal(RUN)
        self.sfn.executions[execution_arn(VERSION, RUN)]["stateMachineVersionArn"] = VERSION[:-1] + "2"
        with self.assertLogs("src.cloud.submission_api", level="ERROR"):
            self.assertEqual(self.handle(event("GET"))["statusCode"], 503)
        self.assertIn(LOCK, self.store.items)

    def test_terminal_duplicate_never_restarts_after_90_days(self):
        self.handle()
        self.sfn.terminal(RUN)
        self.handle(event("GET"))
        self.sfn.executions.clear()
        self.clock += timedelta(days=91)
        self.assertEqual(self.handle()["statusCode"], 200)
        self.assertEqual(self.sfn.started, 1)

    def test_unobserved_execution_expired_from_history_never_restarts(self):
        self.handle()
        self.sfn.executions.clear()
        self.clock += timedelta(days=91)
        payload = json.loads(self.handle()["body"])
        self.assertEqual(payload["status"], "RECOVERY_REQUIRED")
        self.assertEqual(self.sfn.started, 1)
        self.assertIn(LOCK, self.store.items)

    def test_missing_or_spoofed_auth_cannot_reach_services(self):
        request = event()
        request["requestContext"]["authorizer"] = {}
        request["headers"]["userArn"] = "arn:aws:iam::123456789012:user/spoofed"
        self.assertEqual(self.handle(request)["statusCode"], 403)
        request = event()
        request["requestContext"]["apiId"] = "another"
        self.assertEqual(self.handle(request)["statusCode"], 403)
        self.assertEqual(self.factory_calls, 0)

    def test_disabled_submissions_fail_before_loading_clients(self):
        self.assertEqual(self.handle(enabled=False)["statusCode"], 503)
        self.assertEqual(self.factory_calls, 0)

    def test_request_cannot_override_source_bucket_commands_or_workflow(self):
        for extra in ("bucket", "source_commit", "command", "workflow_arn", "image", "role"):
            request = event()
            request["body"] = json.dumps({"operation": "data_preparation", extra: "untrusted"})
            self.assertEqual(self.handle(request)["statusCode"], 400)
        self.assertEqual(self.factory_calls, 0)

    def test_invalid_keys_and_control_keys_are_rejected_before_storage(self):
        for key in (None, "", RUN.upper(), LOCK, "../x", "00000000-0000-5000-8000-000000000001", RUN + "," + OTHER):
            request = event()
            request["headers"]["idempotency-key"] = key
            self.assertEqual(self.handle(request)["statusCode"], 400)
        self.assertEqual(self.factory_calls, 0)

    def test_invalid_encoded_oversize_duplicate_and_non_json_bodies(self):
        for body in ("", "null", "[]", "{", " " * 1025,
                     '{"operation":"train","operation":"data_preparation"}'):
            request = event()
            request["body"] = body
            self.assertEqual(self.handle(request)["statusCode"], 400)
        request = event()
        request["isBase64Encoded"] = True
        self.assertEqual(self.handle(request)["statusCode"], 400)
        request = event()
        request["headers"]["content-type"] = "text/plain"
        self.assertEqual(self.handle(request)["statusCode"], 415)
        self.assertEqual(self.factory_calls, 0)

    def test_unknown_submission_and_invalid_get_keys_do_not_start(self):
        self.assertEqual(self.handle(event("GET"))["statusCode"], 404)
        request = event("GET")
        request["pathParameters"]["run_id"] = LOCK
        self.assertEqual(self.handle(request)["statusCode"], 400)
        self.assertEqual(self.sfn.calls, [])

    def test_query_route_method_and_payload_restrictions(self):
        for field, value, code in (("rawQueryString", "operation=train", 400),
                                   ("routeKey", "POST /train", 404), ("version", "1.0", 400)):
            request = event()
            request[field] = value
            self.assertEqual(self.handle(request)["statusCode"], code)
        request = event()
        request["requestContext"]["http"]["method"] = "PUT"
        self.assertEqual(self.handle(request)["statusCode"], 405)
        self.assertEqual(self.factory_calls, 0)

    def test_unqualified_alias_and_foreign_workflows_are_rejected(self):
        for arn in (VERSION.rsplit(":", 1)[0], VERSION[:-1] + "prod",
                    VERSION.replace("us-east-1", "us-west-2"), VERSION.replace("cpu-prepare", "gpu-train")):
            with self.assertRaises(ValueError):
                execution_arn(arn, RUN)

    def test_response_and_logs_exclude_sdk_details_and_workflow_output(self):
        self.sfn.start_error = RuntimeError("PRIVATE SDK MESSAGE")
        with self.assertLogs("src.cloud.submission_api", level="ERROR") as logs:
            response = self.handle()
        self.assertNotIn("PRIVATE", response["body"] + "".join(logs.output))
        self.assertNotIn("userArn", response["body"])


class LowLevelStorageTests(unittest.TestCase):
    def test_claim_emits_two_conditional_puts_in_one_transaction(self):
        class Client:
            def transact_write_items(self, **args):
                self.args = args
        client = Client()
        store = SubmissionStore(client, "fixture-table")
        store.claim({"run_id": RUN, "status": "STARTING"})
        actions = client.args["TransactItems"]
        self.assertEqual(len(actions), 2)
        for action in actions:
            self.assertEqual(action["Put"]["ConditionExpression"], "attribute_not_exists(run_id)")
        self.assertEqual(actions[1]["Put"]["Item"]["owner"], {"S": RUN})

    def test_success_releases_only_matching_lock_in_same_transaction(self):
        class Client:
            def transact_write_items(self, **args):
                self.args = args
        client = Client()
        store = SubmissionStore(client, "fixture-table")
        store.finish({"run_id": RUN, "status": "COMPLETED", "execution_arn": execution_arn(VERSION, RUN)})
        actions = client.args["TransactItems"]
        self.assertEqual(len(actions), 2)
        self.assertEqual(actions[1]["Delete"]["ExpressionAttributeValues"][":owner"], {"S": RUN})
        store.finish({"run_id": RUN, "status": "FAILED", "execution_arn": execution_arn(VERSION, RUN)})
        self.assertEqual(len(client.args["TransactItems"]), 1)


if __name__ == "__main__":
    unittest.main()
