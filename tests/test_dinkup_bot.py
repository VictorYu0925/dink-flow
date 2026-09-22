import unittest
from datetime import datetime
from threading import Barrier, Event, Lock
from unittest.mock import MagicMock, patch

import requests

import dinkup_bot as bot


def event(event_id="event-1", location="松山高中", divisions=None):
    return {
        "id": event_id,
        "title": "松山站旁歡樂場",
        "location": location,
        "divisions": divisions if divisions is not None else [
            {"id": "fun-1", "level": "fun", "courtCount": 1}
        ],
    }


def response(data=None, status=200):
    result = MagicMock(status_code=status, text="mock response")
    result.json.return_value = data
    return result


class BotTests(unittest.TestCase):
    def setUp(self):
        # 所有測試都攔截 Session；不可意外連上真實報名網站。
        self.session_factory = self.enterContext(patch.object(bot.requests, "Session"))
        self.session = self.session_factory.return_value.__enter__.return_value
        self.session.post.return_value = response(status=201)
        self.enterContext(patch.object(bot.time, "sleep"))
        self.enterContext(patch("builtins.print"))

    def poll(self):
        attempted = set()
        bot.poll_date("2026-09-27", [], {}, attempted, Lock())
        return attempted

    def test_default_dates_cover_sunday_and_monday(self):
        with patch.dict(bot.os.environ, {}, clear=True):
            offsets = bot.get_target_day_offsets()
        self.assertEqual(
            bot.get_target_dates(offsets, datetime(2026, 9, 22)),
            ["2026-09-27", "2026-09-28"],
        )

    def test_custom_offsets_deduplicate_and_cross_year(self):
        with patch.dict(bot.os.environ, {"TARGET_DAY_OFFSETS": " 1, 7,1 "}):
            offsets = bot.get_target_day_offsets()
        self.assertEqual(offsets, (1, 7))
        self.assertEqual(
            bot.get_target_dates(offsets, datetime(2026, 12, 31)),
            ["2027-01-01", "2027-01-07"],
        )

    def test_invalid_configuration(self):
        for value in ("", "0", "-1", "5,", "1.5", "abc", "366"):
            with self.subTest(value=value), patch.dict(bot.os.environ, {"TARGET_DAY_OFFSETS": value}):
                with self.assertRaises(ValueError):
                    bot.get_target_day_offsets()

    def test_location_and_visible_fun_filtering(self):
        divisions = [
            {"id": "hidden", "level": "fun", "courtCount": 0},
            {"id": "pro", "level": "pro", "courtCount": 2},
            {"id": "visible", "level": "FUN", "courtCount": "1"},
        ]
        selected = bot.select_registrations({"events": [
            None, event("wrong-location", "其他體育館"),
            event("hidden-only", divisions=divisions[:1]),
            event(None), event("valid", "西松高中", divisions),
        ]})
        self.assertEqual([(r["event_id"], r["division_id"]) for r in selected], [("valid", "visible")])

    def test_late_events_same_day_and_payload(self):
        self.session.get.side_effect = [
            response([event()]), response([event(), event("late")]), response([event("late")]),
        ]
        self.assertEqual(self.poll(), {("event-1", "fun-1"), ("late", "fun-1")})
        self.assertEqual(self.session.get.call_count, 3)
        self.assertEqual(self.session.post.call_count, 2)
        self.assertEqual(self.session.post.call_args.kwargs["json"], {
            "displayName": "Victor", "needsPaddle": False, "count": 2, "divisionId": "fun-1",
        })

    def test_get_timeout_and_bad_response_retry(self):
        self.session.get.side_effect = [
            requests.Timeout(), response({"events": None}), response([event()]),
        ]
        self.poll()
        self.assertEqual(self.session.get.call_count, 3)
        self.session.post.assert_called_once()

    def test_http_failure_and_empty_result_retry(self):
        self.session.get.side_effect = [response(status=503), response([]), response([event()])]
        self.poll()
        self.session.post.assert_called_once()

    def test_post_timeout_never_resubmits(self):
        self.session.get.return_value = response([event()])
        self.session.post.side_effect = requests.Timeout()
        self.poll()
        self.session.post.assert_called_once()

    def test_post_rejection_never_resubmits(self):
        self.session.get.return_value = response([event()])
        self.session.post.return_value = response(status=400)
        self.poll()
        self.session.post.assert_called_once()

    def test_fast_date_registers_while_other_date_is_waiting(self):
        fast_registered = Event()
        slow_started = Event()
        sessions = []

        def make_session():
            session = MagicMock()
            session.__enter__.return_value = session
            sessions.append(session)

            def get(url, **kwargs):
                if "2026-09-27" in url:
                    slow_started.set()
                    if not fast_registered.wait(3):
                        raise AssertionError("快日期報名不應等待慢日期")
                    return response([event("sunday")])
                if not slow_started.wait(3):
                    raise AssertionError("日期查詢沒有並行執行")
                return response([event("monday")])

            def post(url, **kwargs):
                if "/monday/" in url:
                    fast_registered.set()
                return response(status=201)

            session.get.side_effect = get
            session.post.side_effect = post
            return session

        self.session_factory.side_effect = make_session
        attempted = bot.poll_dates(["2026-09-27", "2026-09-28"], [], {})
        self.assertEqual(attempted, {("sunday", "fun-1"), ("monday", "fun-1")})
        self.assertEqual(len(sessions), 2)
        self.assertEqual(sum(session.get.call_count for session in sessions), 6)
        self.assertEqual(sum(session.post.call_count for session in sessions), 2)

    def test_duplicate_event_across_dates_submits_once(self):
        barrier = Barrier(2)

        def get(*args, **kwargs):
            barrier.wait(timeout=3)
            return response([event()])

        self.session.get.side_effect = get
        attempted = bot.poll_dates(["2026-09-27", "2026-09-28"], [], {})
        self.assertEqual(attempted, {("event-1", "fun-1")})
        self.session.post.assert_called_once()

    def test_worker_limit(self):
        barrier = Barrier(3)
        active = 0
        peak = 0
        lock = Lock()

        def poll(*args):
            nonlocal active, peak
            with lock:
                active += 1
                peak = max(peak, active)
            barrier.wait(timeout=3)
            with lock:
                active -= 1

        with patch.object(bot, "poll_date", side_effect=poll):
            bot.poll_dates([f"2026-09-{day}" for day in range(23, 29)], [], {})
        self.assertEqual(peak, 3)


if __name__ == "__main__":
    unittest.main()
