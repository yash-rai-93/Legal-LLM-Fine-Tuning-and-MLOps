"""CodeBuild-only real S3 manifest check; injected API events are not live IAM tests."""

import argparse
import json
from pathlib import Path
import tempfile

from .artifact_api import handle_request, require
from .artifact_api_config import SnapshotS3, download_current, load_build_config


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-config", required=True)
    args = parser.parse_args()
    approved = load_build_config(Path(args.output_config))
    snapshots = {}
    with tempfile.TemporaryDirectory(prefix="legal-artifact-api-check-") as workspace:
        for name in ("catalog", "evaluation"):
            identity = approved[name]
            path = Path(workspace) / (name + ".json")
            snapshots[identity["key"]] = download_current(
                approved["bucket"], identity["key"], identity["version_id"], identity["sha256"], path
            )

    responses = {}
    for route in ("GET /datasets", "GET /prompts", "GET /evaluations/{evaluation_id}"):
        event = {
            "version": "2.0", "routeKey": route,
            "requestContext": {
                "apiId": "codebuild-metadata-check", "http": {"method": "GET"},
                "authorizer": {"iam": {"userArn": "arn:aws:iam::000000000000:user/test-fixture"}},
            },
        }
        if "evaluation_id" in route:
            event["pathParameters"] = {"evaluation_id": approved["evaluation"]["evaluation_id"]}
        response = handle_request(
            event, expected_api_id="codebuild-metadata-check", approved=approved,
            s3_factory=lambda: SnapshotS3(approved["bucket"], snapshots),
        )
        require(response["statusCode"] == 200, "Real manifest projection failed: " + route)
        body = json.loads(response["body"])
        responses[route] = {
            "status_code": 200, "operation": body["operation"], "live_inference": False,
        }
    print(json.dumps({
        "check": "real_version_matched_manifest_projection",
        "injected_api_event": True, "live_api_deployed_by_this_build": False,
        "responses": responses,
    }, indent=2))


if __name__ == "__main__":
    main()
