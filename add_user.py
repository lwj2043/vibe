"""CLI helper to add or reset a user in users.json.

Usage:
    python add_user.py <username> <password>
    python add_user.py admin newpassword   # 비밀번호 변경
"""

from __future__ import annotations

import hashlib
import json
import secrets
import sys
from pathlib import Path

USERS_PATH = Path(__file__).resolve().parent / "users.json"
PBKDF2_ITERS = 200_000


def hash_password(password: str, salt: str) -> str:
    dk = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), bytes.fromhex(salt), PBKDF2_ITERS
    )
    return dk.hex()


def load_users() -> list[dict]:
    if not USERS_PATH.exists():
        return []
    try:
        data = json.loads(USERS_PATH.read_text(encoding="utf-8"))
        return data.get("users", []) if isinstance(data, dict) else []
    except (json.JSONDecodeError, OSError):
        return []


def save_users(users: list[dict]) -> None:
    USERS_PATH.write_text(
        json.dumps({"users": users}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def main() -> None:
    if len(sys.argv) != 3:
        print("사용법: python add_user.py <아이디> <비밀번호>")
        sys.exit(1)

    username, password = sys.argv[1], sys.argv[2]
    users = load_users()

    salt = secrets.token_hex(16)
    pw_hash = hash_password(password, salt)

    existing = next((u for u in users if u["username"] == username), None)
    if existing:
        existing["salt"] = salt
        existing["password_hash"] = pw_hash
        print(f"✔ '{username}' 비밀번호가 변경되었습니다.")
    else:
        users.append({"username": username, "salt": salt, "password_hash": pw_hash})
        print(f"✔ '{username}' 계정이 추가되었습니다.")

    save_users(users)


if __name__ == "__main__":
    main()
