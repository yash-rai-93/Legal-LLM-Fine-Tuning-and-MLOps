"""Manual CloudShell client for bounded CPU submissions; never auto-retries."""
import argparse
import json
import urllib.error
import urllib.request
import uuid

from call_cpu_status_api import NoRedirect, canonical_uuid, endpoint_url


def submission_request(endpoint, *, idempotency_key=None, submission_id=None):
    base = endpoint_url(endpoint)
    if (idempotency_key is None) == (submission_id is None):
        raise ValueError("Choose submission or status lookup")
    if submission_id is not None:
        return "GET", base + "/submissions/" + canonical_uuid(submission_id), None, {"Accept": "application/json"}
    key = canonical_uuid(idempotency_key)
    if uuid.UUID(key).version != 4:
        raise ValueError("Use a canonical UUID4 idempotency key")
    return "POST", base + "/runs", b'{"operation":"data_preparation"}', {
        "Accept": "application/json", "Content-Type": "application/json", "Idempotency-Key": key,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--endpoint", required=True)
    choice = parser.add_mutually_exclusive_group(required=True)
    choice.add_argument("--idempotency-key", help="Starts one CPU workflow; retain and reuse this UUID4")
    choice.add_argument("--submission-id", help="Read/reconcile a submission; starts no workflow")
    parser.add_argument("--unsigned", action="store_true")
    args = parser.parse_args()
    method, url, body, headers = submission_request(
        args.endpoint, idempotency_key=args.idempotency_key, submission_id=args.submission_id,
    )
    if not args.unsigned:
        from botocore.auth import SigV4Auth
        from botocore.awsrequest import AWSRequest
        from botocore.session import Session

        credentials = Session().get_credentials()
        if credentials is None:
            raise SystemExit("Use signed-in CloudShell temporary credentials.")
        request = AWSRequest(method=method, url=url, data=body, headers=headers)
        SigV4Auth(credentials.get_frozen_credentials(), "execute-api", "us-east-1").add_auth(request)
        headers = dict(request.headers)
    request = urllib.request.Request(url, method=method, data=body, headers=headers)
    try:
        with urllib.request.build_opener(NoRedirect()).open(request, timeout=35) as response:
            status, payload = response.status, response.read(64 * 1024)
    except urllib.error.HTTPError as exc:
        status = exc.code
        exc.close()
        message = {"status_code": status, "error": "API rejected request or status is unavailable"}
        if args.idempotency_key:
            message.update(run_id=args.idempotency_key, retry="Reuse this SAME key; do not generate a replacement")
        print(json.dumps(message, indent=2))
        return 1
    except urllib.error.URLError:
        message = {"error": "Request outcome unknown; read status and reuse the SAME key"}
        if args.idempotency_key:
            message["run_id"] = args.idempotency_key
        print(json.dumps(message, indent=2))
        return 1
    result = json.loads(payload)
    print(json.dumps({"status_code": status, "response": result}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
