"""spec_storage 회귀 테스트 — 일별 파일 회전, 보존 기간, 라운드트립."""

from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from pipelines import spec_storage  # noqa: E402

VALID_SPEC = {
    "project": {"name": "demo", "type": "web", "scope": "frontend"},
    "files": [{"path": "index.html"}],
    "components": [],
    "data_model": {},
    "api": [],
    "logic": [],
    "styling": "",
    "constraints": [],
    "notes": "",
}


class SpecStorageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.tmp.name)
        # SPECS_ROOT 를 임시 디렉터리로 패치
        self._patcher = mock.patch.object(spec_storage, "SPECS_ROOT", self.tmp_path)
        self._patcher.start()

    def tearDown(self):
        self._patcher.stop()
        self.tmp.cleanup()

    def test_save_and_load_roundtrip(self):
        spec_storage.save_spec("alice", "chat-1", VALID_SPEC, "make me a site")
        loaded = spec_storage.load_spec("alice", "chat-1")
        self.assertIsInstance(loaded, dict)
        self.assertEqual(loaded["project"]["name"], "demo")

    def test_load_returns_none_for_unknown_user(self):
        self.assertIsNone(spec_storage.load_spec("ghost", "chat-x"))

    def test_load_falls_back_to_latest(self):
        # chat_id 매칭 실패 → latest.json 폴백
        spec_storage.save_spec("bob", "chat-1", VALID_SPEC)
        loaded = spec_storage.load_spec("bob", "chat-unknown")
        self.assertIsNotNone(loaded)

    def test_save_creates_dayfile(self):
        path = spec_storage.save_spec("carol", "chat-1", VALID_SPEC)
        self.assertTrue(path.exists())
        self.assertTrue(spec_storage._SPEC_DAYFILE_RE.match(path.name))

    def test_prune_removes_old_dayfile(self):
        d = spec_storage.spec_dir("dave")
        # 보존 기간보다 오래된 파일을 위조
        old_date = datetime.now() - timedelta(days=spec_storage.SPEC_RETENTION_DAYS + 5)
        old_file = d / (old_date.strftime("%Y%m%d") + ".jsonl")
        old_file.write_text('{"saved_at":"x","spec":{}}\n', encoding="utf-8")
        # save_spec 호출 시 자동 정리
        spec_storage.save_spec("dave", "chat-1", VALID_SPEC)
        self.assertFalse(old_file.exists(), "오래된 dayfile 이 정리되어야 함")

    def test_prune_keeps_recent_dayfile(self):
        d = spec_storage.spec_dir("erin")
        recent_date = datetime.now() - timedelta(days=1)
        recent_file = d / (recent_date.strftime("%Y%m%d") + ".jsonl")
        recent_file.write_text('{"saved_at":"x","spec":{}}\n', encoding="utf-8")
        spec_storage.save_spec("erin", "chat-1", VALID_SPEC)
        self.assertTrue(recent_file.exists(), "최근 파일은 유지되어야 함")

    def test_username_with_path_separators_is_sanitized(self):
        # ../ 같은 경로 조작 시도가 차단되는지
        path = spec_storage.save_spec("../evil", "chat-1", VALID_SPEC)
        # SPECS_ROOT 하위에만 생성되어야 함
        self.assertTrue(str(path).startswith(str(self.tmp_path)))

    def test_duplicate_save_does_not_append(self):
        # 같은 내용을 두 번 저장해도 dayfile 레코드는 한 줄만 유지
        spec_storage.save_spec("frank", "chat-1", VALID_SPEC, "make me a site")
        spec_storage.save_spec("frank", "chat-1", VALID_SPEC, "make me a site")
        path = spec_storage.spec_dayfile("frank")
        records = spec_storage._read_jsonl(path)
        self.assertEqual(len(records), 1, "동일한 spec 은 dayfile 에 한 번만 기록되어야 함")

    def test_different_spec_appends(self):
        spec_storage.save_spec("grace", "chat-1", VALID_SPEC, "v1")
        modified = dict(VALID_SPEC)
        modified["notes"] = "changed"
        spec_storage.save_spec("grace", "chat-1", modified, "v1")
        path = spec_storage.spec_dayfile("grace")
        records = spec_storage._read_jsonl(path)
        self.assertEqual(len(records), 2)


class SanitizationTests(unittest.TestCase):
    """spec_dir 의 username 정규화 동작 검증."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.tmp.name)
        self._patcher = mock.patch.object(spec_storage, "SPECS_ROOT", self.tmp_path)
        self._patcher.start()

    def tearDown(self):
        self._patcher.stop()
        self.tmp.cleanup()

    def test_anonymous_for_empty_username(self):
        d = spec_storage.spec_dir("")
        self.assertEqual(d.name, "anonymous")

    def test_special_chars_replaced(self):
        d = spec_storage.spec_dir("user@host.com")
        self.assertNotIn("@", d.name)
        self.assertNotIn(".", d.name.strip("_"))


if __name__ == "__main__":
    unittest.main()
