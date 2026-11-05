"""auth.py 비밀번호 해싱 — 신규 형식 + 레거시 폴백 + 자동 재해시."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from routes import auth  # noqa: E402


class PasswordHashFormatTests(unittest.TestCase):
    def test_new_hash_uses_dollar_format(self):
        h = auth._make_password_hash("hunter2")
        parts = h.split("$")
        self.assertEqual(len(parts), 3)
        self.assertEqual(int(parts[0]), auth.PBKDF2_ITERS)

    def test_parse_new_format(self):
        h = auth._make_password_hash("hunter2")
        parsed = auth._parse_password_hash(h)
        self.assertIsNotNone(parsed)
        iters, salt, expected = parsed
        self.assertEqual(iters, auth.PBKDF2_ITERS)
        self.assertTrue(salt and expected)

    def test_parse_legacy_format(self):
        # 레거시: salt:hash → 200_000 iters 로 가정
        legacy = "ab" * 16 + ":" + "cd" * 32
        parsed = auth._parse_password_hash(legacy)
        self.assertIsNotNone(parsed)
        iters, _, _ = parsed
        self.assertEqual(iters, auth.PBKDF2_LEGACY_ITERS[0])

    def test_parse_invalid(self):
        self.assertIsNone(auth._parse_password_hash(""))
        self.assertIsNone(auth._parse_password_hash("garbage"))


class VerifyAndAutoMigrateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.users_path = Path(self.tmp.name) / "users.json"
        self._patcher = mock.patch.object(auth, "USERS_PATH", self.users_path)
        self._patcher.start()

    def tearDown(self):
        self._patcher.stop()
        self.tmp.cleanup()

    def test_verify_with_new_hash(self):
        auth._save_users([{
            "username": "alice",
            "password_hash": auth._make_password_hash("pw1234"),
        }])
        self.assertTrue(auth._verify_credentials("alice", "pw1234"))
        self.assertFalse(auth._verify_credentials("alice", "wrong"))

    def test_legacy_hash_is_rehashed_on_login(self):
        # 직접 레거시 형식으로 사용자 생성 (200_000 iters, salt:hash)
        salt = "ab" * 16
        legacy_hash_hex = auth._hash_password("pw1234", salt, auth.PBKDF2_LEGACY_ITERS[0])
        auth._save_users([{
            "username": "bob",
            "password_hash": f"{salt}:{legacy_hash_hex}",
        }])
        self.assertTrue(auth._verify_credentials("bob", "pw1234"))
        # 로그인 후 자동 재해시 — 신규 $ 형식으로 바뀌어야 함
        users = auth._load_users()
        new_hash = users[0]["password_hash"]
        self.assertIn("$", new_hash)
        self.assertTrue(new_hash.startswith(f"{auth.PBKDF2_ITERS}$"))
        # 새 해시로도 여전히 검증 통과
        self.assertTrue(auth._verify_credentials("bob", "pw1234"))

    def test_unknown_user_returns_false(self):
        self.assertFalse(auth._verify_credentials("ghost", "anything"))


if __name__ == "__main__":
    unittest.main()
