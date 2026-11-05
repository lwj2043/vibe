"""External-data helpers."""

from __future__ import annotations

import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from pipelines.external_data import current_datetime_reply, needs_external_data  # noqa: E402


class CurrentDatetimeReplyTests(unittest.TestCase):
    def test_korean_today_question_is_answered_deterministically(self):
        now = datetime(2026, 5, 4, 13, 45, tzinfo=timezone.utc)

        self.assertEqual(
            current_datetime_reply("오늘 몇일이야", now=now),
            "오늘은 2026년 5월 4일 월요일입니다.",
        )

    def test_time_question_includes_time(self):
        now = datetime(2026, 5, 4, 13, 45, tzinfo=timezone.utc)

        self.assertEqual(
            current_datetime_reply("지금 시간 알려줘", now=now),
            "현재 시각은 2026년 5월 4일 월요일 13시 45분입니다.",
        )

    def test_non_datetime_realtime_query_is_not_direct_answered(self):
        self.assertIsNone(current_datetime_reply("오늘 뉴스 알려줘"))
        self.assertTrue(needs_external_data("오늘 뉴스 알려줘"))


if __name__ == "__main__":
    unittest.main()
