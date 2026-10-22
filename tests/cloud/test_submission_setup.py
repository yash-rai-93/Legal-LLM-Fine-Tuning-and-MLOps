"""Cloud-only checks for submission preflight and administration boundaries."""
from copy import deepcopy
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

ROOT = Path(__file__).resolve().parents[2]


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, ROOT / "setup" / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class SubmissionSetupTests(unittest.TestCase):
    def setUp(self):
        self.prepare = load("submission_prepare", "prepare_cpu_submission.py")
        with patch.object(sys, "path", [str(ROOT / "setup"), *sys.path]):
            self.client = load("submission_client", "call_cpu_submission_api.py")
        self.account = "123456789012"
        self.definition = json.loads((ROOT / "infra/serverless-demo/stepfunctions/cpu-prepare.asl.json").read_text().replace(
            "__AWS_ACCOUNT_ID__", self.account,
        ))
        self.description = {
            "type": "STANDARD", "definition": json.dumps(self.definition),
            "roleArn": "arn:aws:iam::123456789012:role/legal-llm-cpu-workflow-role",
        }
        self.project = {
            "name": "legal-llm-cpu-check", "concurrentBuildLimit": 1, "autoRetryLimit": 0,
            "timeoutInMinutes": 15, "queuedTimeoutInMinutes": 5,
            "environment": {"computeType": "BUILD_GENERAL1_SMALL", "privilegedMode": False},
            "source": {"type": "GITHUB", "location": self.prepare.REPOSITORY,
                       "buildspec": "buildspecs/cpu-publish.yml"},
        }

    def test_reviewed_definition_and_small_project_are_accepted(self):
        self.prepare.validate_workflow(self.description, self.definition, self.account)
        self.prepare.validate_project(self.project, self.definition)

    def test_workflow_drift_express_or_role_change_is_rejected(self):
        for field, value in (("type", "EXPRESS"), ("roleArn", "untrusted"), ("definition", "{}")):
            description = {**self.description, field: value}
            with self.assertRaises(ValueError):
                self.prepare.validate_workflow(description, self.definition, self.account)

    def test_unsafe_build_limits_or_source_are_rejected(self):
        for field, value in (("concurrentBuildLimit", 2), ("autoRetryLimit", 1),
                             ("timeoutInMinutes", 60), ("queuedTimeoutInMinutes", 60)):
            project = {**self.project, field: value}
            with self.assertRaises(ValueError):
                self.prepare.validate_project(project, self.definition)
        for section, field, value in (("environment", "privilegedMode", True),
                                      ("environment", "computeType", "BUILD_GENERAL1_LARGE"),
                                      ("source", "location", "https://example.com"),
                                      ("source", "buildspec", "arbitrary.yml")):
            project = deepcopy(self.project)
            project[section][field] = value
            with self.assertRaises(ValueError):
                self.prepare.validate_project(project, self.definition)

    def test_submission_client_restricts_endpoint_and_constructs_only_fixed_body(self):
        run_id = "bc39c8af-66b7-4f1d-bc38-71870e2e52ab"
        endpoint = "https://fixtureapi.execute-api.us-east-1.amazonaws.com"
        method, url, body, headers = self.client.submission_request(endpoint, idempotency_key=run_id)
        self.assertEqual((method, url), ("POST", endpoint + "/runs"))
        self.assertEqual(json.loads(body), {"operation": "data_preparation"})
        self.assertEqual(headers["Idempotency-Key"], run_id)
        for value in ("https://example.com", endpoint + "/evil", "http://fixtureapi.execute-api.us-east-1.amazonaws.com"):
            with self.assertRaises(ValueError):
                self.client.submission_request(value, idempotency_key=run_id)
        with self.assertRaises(ValueError):
            self.client.submission_request(endpoint, idempotency_key="control#active")

    def test_submission_package_is_a_thin_module_with_no_private_config(self):
        packager = load("submission_packager", "package_registry.py")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "package.zip"
            packager.create_package("submission-api", path)
            with zipfile.ZipFile(path) as archive:
                self.assertEqual(archive.namelist(), ["submission_api.py"])
                self.assertIn(b"def lambda_handler", archive.read("submission_api.py"))
            with self.assertRaises(ValueError):
                packager.create_package("submission-api", path, approved_path="private.json")


if __name__ == "__main__":
    unittest.main()
