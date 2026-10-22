"""CloudShell administration client for the IAM-authenticated read-only API.

Uses temporary credentials from the default botocore credential chain.
Never prints credentials, signed headers or unfiltered error responses.
"""
import argparse
import json
import re
import urllib.error
import urllib.parse
import urllib.request
import uuid


def endpoint_url(endpoint):
    parsed = urllib.parse.urlsplit(endpoint)
    if (
        parsed.scheme != "https"
        or not re.fullmatch(r"[a-z0-9]+\.execute-api\.us-east-1\.amazonaws\.com", parsed.hostname or "")
        or parsed.username is not None or parsed.password is not None
        or parsed.port is not None or parsed.query or parsed.fragment
        or parsed.path not in ("", "/")
    ):
        raise ValueError("Use the default-stage us-east-1 API Gateway endpoint")
    return "https://" + parsed.hostname


def canonical_uuid(value):
    if not isinstance(value, str) or str(uuid.UUID(value)) != value:
        raise ValueError("Use a canonical UUID")
    return value


def request_url(endpoint, run_id):
    canonical_uuid(run_id)
    return endpoint_url(endpoint) + "/runs/" + run_id


def artifact_request_url(endpoint, resource, evaluation_id=None):
    if resource not in ("datasets", "prompts", "evaluation"):
        raise ValueError("Choose an allowlisted metadata resource")
    if resource == "evaluation":
        path = "/evaluations/" + canonical_uuid(evaluation_id)
    else:
        if evaluation_id is not None:
            raise ValueError("Evaluation ID is valid only for an evaluation lookup")
        path = "/" + resource
    return endpoint_url(endpoint) + path


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--endpoint", required=True)
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--run-id")
    target.add_argument("--resource", choices=("datasets", "prompts", "evaluation"))
    parser.add_argument("--evaluation-id")
    parser.add_argument("--unsigned", action="store_true", help="Test IAM rejection without loading credentials")
    args = parser.parse_args()
    if args.run_id is not None:
        if args.evaluation_id is not None:
            parser.error("--evaluation-id requires --resource evaluation")
        url = request_url(args.endpoint, args.run_id)
    else:
        url = artifact_request_url(args.endpoint, args.resource, args.evaluation_id)
    headers = {"Accept": "application/json"}
    if not args.unsigned:
        try:
            from botocore.auth import SigV4Auth
            from botocore.awsrequest import AWSRequest
            from botocore.session import Session
        except ImportError as exc:
            raise SystemExit("This CloudShell administration client requires botocore.") from exc
        credentials = Session().get_credentials()
        if credentials is None:
            raise SystemExit("Use the signed-in CloudShell temporary credentials.")
        request = AWSRequest(method="GET", url=url, headers=headers)
        SigV4Auth(credentials.get_frozen_credentials(), "execute-api", "us-east-1").add_auth(request)
        headers = dict(request.headers)
    request = urllib.request.Request(url, method="GET", headers=headers)
    opener = urllib.request.build_opener(NoRedirect())
    try:
        with opener.open(request, timeout=20) as response:
            status, payload = response.status, response.read(64 * 1024)
    except urllib.error.HTTPError as exc:
        status, payload = exc.code, b""
        exc.close()
    if not 200 <= status < 300:
        print(json.dumps({"status_code": status, "error": "API request rejected or unavailable"}))
        return 1
    try:
        body = json.loads(payload)
    except (ValueError, UnicodeError) as exc:
        raise SystemExit("API returned an invalid JSON response.") from exc
    print(json.dumps({"status_code": status, "response": body}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
