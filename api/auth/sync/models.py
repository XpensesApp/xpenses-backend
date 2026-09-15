# models.py
import os
from datetime import datetime
from typing import Optional

import boto3
import msgspec


class User(msgspec.Struct):
    email: str  # unique identifier
    name: Optional[str] = None
    createdAt: datetime = msgspec.field(default_factory=datetime.utcnow)


# Optional: convenience functions for (de)serialization
def decode_user(data: bytes | str) -> User:
    """Decode a User from JSON bytes or string."""
    return msgspec.json.decode(data, type=User)


def encode_user(user: User) -> bytes:
    """Encode a User to JSON bytes."""
    return msgspec.json.encode(user)


# ---------- DynamoDB ----------
def get_users_table():
    """Return the DynamoDB Users table resource."""
    table_name = os.environ.get("USERS_TABLE")
    if not table_name:
        raise RuntimeError("USERS_TABLE environment variable is not set")

    dynamodb = boto3.resource("dynamodb")
    return dynamodb.Table(table_name)


def put_user(table, user: User) -> None:
    """Write a user to DynamoDB."""
    item = msgspec.structs.asdict(user)
    # DynamoDB can't store datetime objects; serialize to ISO-8601 string
    item["createdAt"] = user.createdAt.isoformat()
    table.put_item(Item=item)


if __name__ == "__main__":
    # Example usage
    user = User(email="jane@example.com", name="Jane Doe")
    print(user)

    raw = encode_user(user)
    print("JSON:", raw.decode())

    parsed = decode_user(raw)
    print("Parsed:", parsed)
    assert parsed == user