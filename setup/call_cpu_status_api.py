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


def request_url(endpoint, run_id):
    if not isinstance(run_id, str) or str(uuid.UUID(run_id)) != run_id:
        raise ValueError("Use a canonical run UUID")
    parsed = urllib.parse.urlsplit(endpoint)
    if (
        parsed.scheme != "https"
        or not re.fullmatch(r"[a-z0-9]+\.execute-api\.us-east-1\.amazonaws\.com", parsed.hostname or "")
        or parsed.username is not None or parsed.password is not None
        or parsed.port is not None or parsed.query or parsed.fragment
        or parsed.path not in ("", "/")
    ):
        raise ValueError("Use the default-stage us-east-1 API Gateway endpoint")
    return "https://" + parsed.hostname + "/runs/" + run_id


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--endpoint", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--unsigned", action="store_true", help="Test IAM rejection without loading credentials")
    args = parser.parse_args()
    url = request_url(args.endpoint, args.run_id)
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
