# app.py
"""Lambda TOKEN authorizer for API Gateway REST API routes.

Verifies a Cognito ID token (same verification `auth/sync` uses inline) and
returns an IAM policy allowing execute-api:Invoke on the requested method.
On success, the verified email is passed to the downstream Lambda via the
authorizer context (event["requestContext"]["authorizer"]["email"] there).

Per the API Gateway TOKEN-authorizer contract: raising an exception with the
exact message "Unauthorized" is what produces a 401 response; any other
exception message/type produces a 500.

The 401 body itself is customized via GatewayResponseDefault4xx in
template.yaml, NOT a dedicated GatewayResponseUnauthorized (ResponseType:
UNAUTHORIZED) — confirmed empirically that UNAUTHORIZED genuinely never
applies to this failure mode (patched it with a debug marker + zero wait,
never appeared), while DEFAULT_4XX does apply, just needs a few seconds to
propagate after a deploy before it's visible (an earlier test on DEFAULT_4XX
gave a false negative from testing too soon after patching). DEFAULT_4XX is
safe to use for an auth-specific message here since our own Lambda-level
400/404 responses never go through GatewayResponse at all — those are
ordinary successful Lambda executions that just happen to return a
4xx-flavored body, so they can't collide with it.
"""
from cognito_auth import AuthError, extract_bearer_token, verify_token


def _wildcard_resource(method_arn: str) -> str:
    """Build a policy Resource covering every method/path in this API stage,
    so a cached Allow decision (see Identity.ReauthorizeEvery in
    template.yaml) applies across routes, not just the one that triggered it.
    """
    # method_arn: "arn:aws:execute-api:{region}:{account}:{apiId}/{stage}/{method}/{resource-path}"
    arn_prefix, _, path_part = method_arn.partition("/")
    stage = path_part.split("/", 1)[0]
    return f"{arn_prefix}/{stage}/*/*"


def _allow_policy(principal_id: str, method_arn: str, context: dict) -> dict:
    return {
        "principalId": principal_id,
        "policyDocument": {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Action": "execute-api:Invoke",
                    "Effect": "Allow",
                    "Resource": _wildcard_resource(method_arn),
                }
            ],
        },
        "context": context,
    }


def lambda_handler(event, context):
    try:
        token = extract_bearer_token(event.get("authorizationToken"))
        claims = verify_token(token)
    except AuthError as exc:
        # The client only ever sees the (now-customized) generic 401 body —
        # the real reason has to go to CloudWatch instead of the response.
        print(f"Authorization failed: {exc}")
        raise Exception("Unauthorized")

    email = claims.get("email")
    if not email:
        print("Authorization failed: token has no email claim")
        raise Exception("Unauthorized")

    return _allow_policy(email, event["methodArn"], context={"email": email})


if __name__ == "__main__":
    import importlib.util
    import os

    try:
        from dotenv import load_dotenv

        load_dotenv()
    except ImportError:
        pass

    # Load api/auth/sync/get_test_token.py by path (not by module name) since
    # both directories have their own app.py and a plain `import` would collide.
    sync_dir = os.path.join(os.path.dirname(__file__), "..", "api", "auth", "sync")
    spec = importlib.util.spec_from_file_location(
        "get_test_token", os.path.join(sync_dir, "get_test_token.py")
    )
    get_test_token = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(get_test_token)

    TEST_EMAIL = "test@xpenses.dev"
    TEST_METHOD_ARN = "arn:aws:execute-api:us-east-1:000000000000:fakeapi/prod/GET/transactions"

    token = get_test_token.get_id_token(TEST_EMAIL, os.environ["TEST_USER_PASSWORD"])

    print("valid token:")
    print(lambda_handler({"authorizationToken": f"Bearer {token}", "methodArn": TEST_METHOD_ARN}, None))

    print("\ninvalid token:")
    try:
        lambda_handler({"authorizationToken": "Bearer garbage", "methodArn": TEST_METHOD_ARN}, None)
        print("FAIL: garbage token was accepted")
    except Exception as exc:
        print(f"correctly raised: {exc!r}")

    print("\nmissing header:")
    try:
        lambda_handler({"authorizationToken": None, "methodArn": TEST_METHOD_ARN}, None)
        print("FAIL: missing token was accepted")
    except Exception as exc:
        print(f"correctly raised: {exc!r}")
