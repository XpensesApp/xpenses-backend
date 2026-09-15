# test_user_model.py
from models import User, get_users_table, put_user

# Load .env locally; silently skip if python-dotenv isn't installed (e.g. in Lambda)
try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass


# ---------- Test users ----------
TEST_USERS: list[User] = [
    User(email="jane@test", name="Jane Doe"),
    User(email="john@test", name="John Doe"),
    User(email="alice@test", name="Alice Smith"),
    User(email="bob@test", name="Bob Johnson"),
    User(email="charlie@test", name="Charlie Brown"),
    User(email="diana@test", name="Diana Prince"),
    User(email="eve@test", name=None),  # no name
]


# ---------- CLI ----------
if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Load test users into DynamoDB.")
    parser.add_argument(
        "--email",
        default=None,
        help="Load a single user by email (must match a TEST_USERS entry)",
    )
    parser.add_argument("--name", default=None, help="Optional user name (with --email)")
    args = parser.parse_args()
    table = get_users_table()

    if args.email:
        # Load just one user — if it's in TEST_USERS, reuse that entry
        match = next((u for u in TEST_USERS if u.email == args.email), None)
        user = match or User(email=args.email, name=args.name)
        put_user(table, user)
        print(f"Loaded user: {user}")
    else:
        # Seed all test users
        for user in TEST_USERS:
            put_user(table, user)
            print(f"Loaded user: {user}")
        print(f"\nSeeded {len(TEST_USERS)} test users.")