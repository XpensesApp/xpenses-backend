# get_test_token.py
"""Local-only dev tool: mint a real Cognito ID token for a native test user.

Never deployed as an endpoint. Requires your own AWS credentials
(AWS_PROFILE) to call Cognito's Admin APIs, so only whoever has access to
this AWS account can mint a token this way.

The pool already supports native (non-Google) sign-in for the "COGNITO"
identity provider, so a dedicated test user logs in with a real
username+password and gets back a token indistinguishable from one a real
Google-authenticated user would receive (same issuer/audience/claims).

Usage:
    PYTHONPATH=. python get_test_token.py --password 'Some$trongPass1'
    PYTHONPATH=. python get_test_token.py --email other@test --password '...'
"""
import base64
import hashlib
import hmac
import os

import boto3

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

USER_POOL_ID = os.environ["COGNITO_USER_POOL_ID"]
APP_CLIENT_ID = os.environ["COGNITO_APP_CLIENT_ID"]
APP_CLIENT_SECRET = os.environ["COGNITO_APP_CLIENT_SECRET"]

client = boto3.client("cognito-idp")


def _secret_hash(username: str) -> str:
    """Cognito requires this whenever the app client has a secret."""
    message = (username + APP_CLIENT_ID).encode()
    digest = hmac.new(APP_CLIENT_SECRET.encode(), message, hashlib.sha256).digest()
    return base64.b64encode(digest).decode()


def ensure_test_user(email: str, password: str) -> None:
    """Create the native test user if missing, then (re)set its password.

    Idempotent — safe to call every time; a rerun just resets the password
    to the same value rather than failing.
    """
    try:
        client.admin_create_user(
            UserPoolId=USER_POOL_ID,
            Username=email,
            UserAttributes=[
                {"Name": "email", "Value": email},
                {"Name": "email_verified", "Value": "true"},
            ],
            MessageAction="SUPPRESS",  # don't send a real invite email
        )
        print(f"Created test user: {email}")
    except client.exceptions.UsernameExistsException:
        pass

    client.admin_set_user_password(
        UserPoolId=USER_POOL_ID, Username=email, Password=password, Permanent=True
    )

f
def get_id_token(email: str, password: str) -> str:
    """Log in as the test user via Cognito's choice-based USER_AUTH flow and
    return a real ID token — the same shape auth/sync's verify_token() expects.
    """
    secret_hash = _secret_hash(email)

    response = client.initiate_auth(
        ClientId=APP_CLIENT_ID,
        AuthFlow="USER_AUTH",
        AuthParameters={
            "USERNAME": email,
            "PASSWORD": password,
            "PREFERRED_CHALLENGE": "PASSWORD",
            "SECRET_HASH": secret_hash,
        },
    )

    # USER_AUTH can still come back as a challenge (e.g. NEW_PASSWORD_REQUIRED
    # for a first-time login) rather than resolving immediately.
    if "AuthenticationResult" not in response:
        raise RuntimeError(
            f"Expected immediate auth result, got challenge: {response.get('ChallengeName')}"
        )

    return response["AuthenticationResult"]["IdToken"]


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Mint a real Cognito ID token for local testing.")
    parser.add_argument("--email", default="test@xpenses.dev")
    parser.add_argument(
        "--password", default=os.environ.get("TEST_USER_PASSWORD"),
        help="Required (or set TEST_USER_PASSWORD in .env). Must satisfy the pool's password policy.",
    )
    args = parser.parse_args()

    if not args.password:
        raise SystemExit("Pass --password, or set TEST_USER_PASSWORD in .env")

    ensure_test_user(args.email, args.password)
    token = get_id_token(args.email, args.password)

    print(f"\nID token for {args.email}:\n")
    print(token)
