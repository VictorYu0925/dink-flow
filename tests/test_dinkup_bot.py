import unittest
from concurrent.futures import ThreadPoolExecutor
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
            {"id": "fun-1", "level": "fun", "label": "歡樂", "courtCount": 1}
        ],
    }


def response(data=None, status=200):
    if data is None and status == 201:
        data = {"snapshot": {"divisions": [
            {"id": division_id, "confirmed": [{"isMine": True}, {"isMine": True}]}
            for division_id in ("fun-1", "fun", "comp", "competitive")
        ]}}
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
        self.log = self.enterContext(patch("builtins.print"))
        self.email = self.enterContext(patch.object(bot, "send_success_email"))

    def poll(self):
        attempted = set()
        bot.poll_date("2026-09-27", [], {}, attempted, Lock())
        return attempted

    def test_default_dates_cover_four_to_six_days_ahead(self):
        with patch.dict(bot.os.environ, {}, clear=True):
            offsets = bot.get_target_day_offsets()
        self.assertEqual(
            bot.get_target_dates(offsets, datetime(2026, 9, 22)),
            ["2026-09-26", "2026-09-27", "2026-09-28"],
        )

    def test_wednesday_queries_sunday_opening(self):
        with patch.dict(bot.os.environ, {}, clear=True):
            offsets = bot.get_target_day_offsets()
        self.assertEqual(
            bot.get_target_dates(offsets, datetime(2026, 9, 30, 12)),
            ["2026-10-04", "2026-10-05", "2026-10-06"],
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
            {"id": "hidden", "level": "fun", "label": "歡樂", "courtCount": 0},
            {"id": "pro", "level": "pro", "courtCount": 2},
            {"id": "visible", "level": "FUN", "label": "歡樂", "courtCount": "1"},
        ]
        selected = bot.select_registrations({"events": [
            None, event("wrong-location", "其他體育館"),
            event("hidden-only", divisions=divisions[:1]),
            event(None), event("valid", "西松高中", divisions),
        ]})
        self.assertEqual([(r["event_id"], r["division_id"]) for r in selected], [("valid", "visible")])

    def test_coached_competitive_slot_registers_fun_instead(self):
        self.session.get.return_value = response([event(divisions=[
            {"id": "lesson", "level": "competitive", "courtCount": 1,
             "label": "新手進階班（有教練）針對比賽情境練", "capacity": 6, "confirmedCount": 1},
            {"id": "fun", "level": "fun", "label": "歡樂", "courtCount": 3},
        ])])
        self.assertEqual(self.poll(), {("event-1", "fun")})
        self.session.post.assert_called_once()
        self.assertEqual(self.session.post.call_args.kwargs["json"]["divisionId"], "fun")

    def test_label_overrides_api_level_and_preserves_fallback(self):
        selected = bot.select_registrations([event(divisions=[
            {"id": "fun", "level": "competitive", "label": "歡樂場雙打[懂規則]", "courtCount": 1},
            {"id": "comp", "level": "beginner", "label": "競技球敘3.0UP", "courtCount": 1},
        ])])
        self.assertEqual(selected[0]["division_id"], "fun")
        self.assertEqual(selected[0]["level"], "fun")
        self.assertEqual(selected[0]["fallback"]["division_id"], "comp")

    def test_lessons_and_unknown_labels_are_skipped(self):
        for label in (None, "", "  ", 123, "自由練習", "競技／歡樂",
                      "競技教練課", "歡樂課程", "競技教學", "歡樂體驗班",
                      "競技進階班", "歡樂基礎班", "競技訓練班"):
            with self.subTest(label=label):
                self.assertEqual(bot.select_registrations([event(divisions=[
                    {"id": "skip", "level": "competitive", "label": label, "courtCount": 1},
                ])]), [])
        self.assertEqual(bot.select_registrations([event(divisions=[
            {"id": "missing-label", "level": "fun", "courtCount": 1},
        ])]), [])

    def test_event_notes_do_not_exclude_social_division(self):
        data = event()
        data["notes"] = "教練課程詢問"
        self.assertEqual(bot.select_registrations([data])[0]["division_id"], "fun-1")

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

    def test_countdown_triggers_prefetch_only_once(self):
        prefetch = MagicMock()
        times = [
            datetime(2026, 9, 22, 11, 59, 28),
            datetime(2026, 9, 22, 11, 59, 29),
            datetime(2026, 9, 22, 11, 59, 30),
            datetime(2026, 9, 22, 11, 59, 30, 500000),
            datetime(2026, 9, 22, 11, 59, 59),
            datetime(2026, 9, 22, 12),
        ]
        with patch.object(bot, "datetime") as clock:
            clock.now.side_effect = times
            prefetch.side_effect = lambda: self.assertEqual(clock.now.call_count, 3)
            bot.wait_until_target_time(on_prefetch=prefetch)
        prefetch.assert_called_once_with()

    def test_cached_result_waits_for_noon_and_checks_once_after_post(self):
        gate = Event()
        waiting = Event()
        real_wait = gate.wait

        def wait():
            waiting.set()
            return real_wait()

        self.session.get.side_effect = [response([event()]), response([event(), event("new")])]
        with patch.object(gate, "wait", side_effect=wait), ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(bot.poll_date, "2026-09-27", [], {}, set(), Lock(), gate)
            try:
                self.assertTrue(waiting.wait(3))
                self.assertEqual(self.session.get.call_count, 1)
                self.assertEqual(self.session.get.call_args.kwargs["timeout"], (5, 15))
                self.session.post.assert_not_called()
            finally:
                gate.set()
            future.result(timeout=3)
        self.assertEqual(self.session.get.call_count, 2)
        self.assertEqual(self.session.get.call_args.kwargs["timeout"], (5, 10))
        self.assertEqual(self.session.post.call_count, 2)

    def test_empty_or_failed_prefetch_has_two_followup_queries(self):
        for first in (response([]), requests.Timeout()):
            with self.subTest(first=first):
                self.session.reset_mock()
                gate = Event()
                gate.set()
                self.session.get.side_effect = [first, response([event()]), response([event()])]
                bot.poll_date("2026-09-27", [], {}, set(), Lock(), gate)
                self.assertEqual(self.session.get.call_count, 3)
                self.session.post.assert_called_once()

    def test_slow_prefetch_does_not_block_countdown_or_fast_date_post(self):
        slow_started = Event()
        fast_started = Event()
        fast_posted = Event()

        def make_session():
            session = MagicMock()
            session.__enter__.return_value = session

            def get(url, **kwargs):
                if "2026-09-27" in url:
                    slow_started.set()
                    if not fast_posted.wait(3):
                        raise AssertionError("慢 GET 阻塞了開搶訊號或另一日 POST")
                    return response([event("slow")])
                fast_started.set()
                return response([event("fast")])

            def post(url, **kwargs):
                if "/fast/" in url:
                    fast_posted.set()
                return response(status=201)

            session.get.side_effect = get
            session.post.side_effect = post
            return session

        def countdown(on_prefetch):
            on_prefetch()
            self.assertTrue(slow_started.wait(3))
            self.assertTrue(fast_started.wait(3))
            self.assertFalse(fast_posted.is_set())
            on_prefetch()  # 即使 callback 被重複呼叫，也不可重複提交。

        self.session_factory.side_effect = make_session
        with patch.object(bot, "wait_until_target_time", side_effect=countdown):
            attempted = bot.poll_dates(["2026-09-27", "2026-09-28"], [], {}, wait_for_noon=True)
        self.assertEqual(attempted, {("slow", "fun-1"), ("fast", "fun-1")})
        self.assertEqual(self.session_factory.call_count, 2)

    def test_late_start_uses_original_three_queries(self):
        self.session.get.return_value = response([event()])
        with patch.object(bot, "datetime") as clock:
            clock.now.return_value = datetime(2026, 9, 22, 12, 0, 1)
            bot.poll_dates(["2026-09-27"], [], {}, wait_for_noon=True)
        self.assertEqual(self.session.get.call_count, 3)
        self.session.post.assert_called_once()

    def test_skipped_prefetch_window_uses_original_queries(self):
        self.session.get.return_value = response([event()])
        with patch.object(bot, "datetime") as clock:
            clock.now.side_effect = [datetime(2026, 9, 22, 11, 59, 29)] + [datetime(2026, 9, 22, 12)] * 4
            bot.poll_dates(["2026-09-27"], [], {}, wait_for_noon=True)
        self.assertEqual(self.session.get.call_count, 3)
        self.session.post.assert_called_once()

    def test_interrupted_countdown_releases_workers_without_post(self):
        self.session.get.return_value = response([event()])

        def countdown(on_prefetch):
            on_prefetch()
            raise RuntimeError("倒數中斷")

        with patch.object(bot, "wait_until_target_time", side_effect=countdown):
            with self.assertRaisesRegex(RuntimeError, "倒數中斷"):
                bot.poll_dates(["2026-09-27"], [], {}, wait_for_noon=True)
        self.session.post.assert_not_called()

    def test_all_timeouts_fail_instead_of_reporting_no_events(self):
        self.session.get.side_effect = requests.ReadTimeout("read timeout")
        with self.assertRaisesRegex(RuntimeError, "2026-09-27"):
            bot.poll_dates(["2026-09-27"], [], {})
        self.assertEqual(self.session.get.call_count, 3)
        self.session.post.assert_not_called()
        messages = "\n".join(str(call.args[0]) for call in self.log.call_args_list)
        self.assertIn("所有查詢均失敗", messages)
        self.assertNotIn("未找到符合", messages)

    def test_valid_empty_response_is_not_query_failure(self):
        self.session.get.return_value = response({"events": []})
        self.assertEqual(bot.poll_dates(["2026-09-27"], [], {}), set())
        self.session.post.assert_not_called()
        self.log.assert_any_call("❌ 未找到符合松山/西松場地的競技或歡樂分組。")

    def test_fun_preferred_regardless_of_api_order(self):
        competitive = {"id": "competitive", "level": "competitive", "label": "競技", "courtCount": 1}
        fun = {"id": "fun", "level": " Fun ", "label": "歡樂", "courtCount": 1}
        for divisions in ([competitive, fun], [fun, competitive]):
            with self.subTest(divisions=divisions):
                selected = bot.select_registrations([event(divisions=divisions)])
                self.assertEqual(len(selected), 1)
                self.assertEqual(selected[0]["division_id"], "fun")

    def test_invalid_fun_falls_back_to_competitive(self):
        competitive = {"id": "competitive", "level": "competitive", "label": "競技", "courtCount": 1}
        for fun in (
            {"id": "fun", "level": "fun", "label": "歡樂", "courtCount": 0},
            {"id": None, "level": "fun", "label": "歡樂", "courtCount": 1},
            {"id": "fun", "level": "fun", "label": "歡樂", "courtCount": "invalid"},
        ):
            with self.subTest(fun=fun):
                selected = bot.select_registrations([event(divisions=[fun, competitive])])
                self.assertEqual(selected[0]["division_id"], "competitive")

    def test_fun_payload_and_no_switch_during_followup(self):
        fun = event(divisions=[{"id": "fun", "level": "fun", "label": "歡樂", "courtCount": 1}])
        for first, second in ((fun, event()), (event(), fun)):
            with self.subTest(first=first):
                self.session.reset_mock()
                self.session.get.side_effect = [response([first]), response([second]), response([second])]
                self.poll()
                self.session.post.assert_called_once()
                self.assertEqual(self.session.post.call_args.kwargs["json"]["divisionId"], first["divisions"][0]["id"])

    def test_http_or_schema_errors_are_not_valid_empty_responses(self):
        for result in (response(status=503), response({"error": "unauthorized"}), response(None)):
            with self.subTest(result=result):
                self.session.get.return_value = result
                with self.assertRaises(RuntimeError):
                    bot.poll_dates(["2026-09-27"], [], {})
        self.session.post.assert_not_called()

    def test_failed_date_does_not_prevent_other_date_registration(self):
        def get(url, **kwargs):
            if "2026-09-27" in url:
                raise requests.ReadTimeout()
            return response([event("monday")])

        self.session.get.side_effect = get
        with self.assertRaisesRegex(RuntimeError, "2026-09-27"):
            bot.poll_dates(["2026-09-27", "2026-09-28"], [], {})
        self.session.post.assert_called_once()

    def fallback_event(self, **fun_fields):
        return event(divisions=[
            {"id": "fun", "level": "fun", "label": "歡樂", "courtCount": 1, **fun_fields},
            {"id": "competitive", "level": "competitive", "label": "競技", "courtCount": 1},
        ])

    def test_fun_full_or_one_place_left_selects_competitive(self):
        for fields in (
            {"capacity": 8, "confirmedCount": 8},
            {"capacity": 8, "confirmedCount": 7},
            {"capacity": 2, "confirmed": [{}]},
        ):
            with self.subTest(fields=fields):
                selected = bot.select_registrations([self.fallback_event(**fields)])
                self.assertEqual(selected[0]["division_id"], "competitive")

    def test_unknown_capacity_or_two_free_places_still_prefers_fun(self):
        for fields in ({}, {"capacity": 8}, {"capacity": 8, "confirmedCount": 6}):
            with self.subTest(fields=fields):
                selected = bot.select_registrations([self.fallback_event(**fields)])
                self.assertEqual(selected[0]["division_id"], "fun")
                self.assertEqual(selected[0]["fallback"]["division_id"], "competitive")

    def test_explicit_rejection_falls_back_once_without_extra_get(self):
        for status in (400, 403, 404, 409, 422):
            with self.subTest(status=status):
                self.session.reset_mock()
                self.session.get.return_value = response([self.fallback_event()])
                self.session.post.side_effect = [response({"error": "名額已滿"}, status), response(status=201)]
                attempted = self.poll()
                self.assertEqual(attempted, {("event-1", "fun"), ("event-1", "competitive")})
                self.assertEqual(self.session.get.call_count, 3)
                self.assertEqual([c.kwargs["json"]["divisionId"] for c in self.session.post.call_args_list], ["fun", "competitive"])
                self.assertTrue(all(c.kwargs["json"]["count"] == 2 for c in self.session.post.call_args_list))

    def test_competitive_rejection_or_timeout_stops_after_two_posts(self):
        for fallback_result in (response({"error": "名額已滿"}, 409), requests.Timeout()):
            with self.subTest(fallback_result=fallback_result):
                self.session.reset_mock()
                self.session.get.return_value = response([self.fallback_event()])
                self.session.post.side_effect = [response({"error": "名額已滿"}, 409), fallback_result]
                self.poll()
                self.assertEqual(self.session.post.call_count, 2)

    def test_accepted_or_uncertain_fun_never_falls_back(self):
        for result in (
            response(status=201), response({"snapshot": {"waitlisted": [{}]}}, 200),
            requests.Timeout(), requests.ConnectionError(),
            response({"error": "server error"}, 500), response({"error": "timeout"}, 408),
            response({"error": "unauthorized"}, 401), response({"error": "rate limit"}, 429),
            response({"error": "已報名此活動"}, 409), response({"error": "Already registered"}, 400),
            response({"error": "已進入候補"}, 409), response(status=400),
        ):
            with self.subTest(result=result):
                self.session.reset_mock()
                self.session.get.return_value = response([self.fallback_event()])
                self.session.post.side_effect = [result]
                self.poll()
                self.session.post.assert_called_once()

    def test_fallback_is_not_duplicated_across_concurrent_dates(self):
        barrier = Barrier(2)

        def get(*args, **kwargs):
            barrier.wait(timeout=3)
            return response([self.fallback_event()])

        self.session.get.side_effect = get
        self.session.post.side_effect = [response({"error": "名額已滿"}, 409), response(status=201)]
        bot.poll_dates(["2026-09-27", "2026-09-28"], [], {})
        self.assertEqual(self.session.post.call_count, 2)

    def test_two_person_limit_and_dupr_are_respected(self):
        for fields in (
            {"repeatRegistrationEnabled": 0},
            {"repeatRegistrationEnabled": 1, "repeatRegistrationLimit": 1},
        ):
            data = {**event(), **fields}
            self.assertEqual(bot.select_registrations([data]), [])
        data = event()
        data["divisions"][0]["duprEnabled"] = True
        self.assertEqual(bot.select_registrations([data]), [])

    def test_existing_registration_is_not_submitted_again(self):
        for status in ("confirmed", "waitlisted"):
            data = event()
            data["myRegistrationSummary"] = [{"status": status, "count": 1}]
            self.assertEqual(bot.select_registrations([data]), [])

    def test_invalid_authentication_has_actionable_error(self):
        self.session.get.return_value = response({"user": None})
        with self.assertRaisesRegex(RuntimeError, "AUTH_JSON_CONTENT"):
            bot.validate_authentication([], {})
        self.session.post.assert_not_called()

    def test_valid_authentication(self):
        self.session.get.return_value = response({"user": {"id": "me"}})
        bot.validate_authentication([], {})
        self.session.get.assert_called_once()

    def test_notification_contains_only_verified_success(self):
        self.session.get.return_value = response([event()])
        bot.poll_dates(["2026-11-15"], [], {})
        records = self.email.call_args.args[0]
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["date"], "2026-11-15")
        self.assertEqual(records[0]["level"], "fun")

    def test_unconfirmed_or_rejected_registration_fails_run(self):
        for result in (
            response({"snapshot": {"divisions": [
                {"id": "fun-1", "confirmed": [{"isMine": True}, {"isMine": False}],
                 "waitlisted": [{"isMine": True}]},
            ]}}, 201),
            response({"error": "login required"}, 401),
            response({}, 200),
        ):
            with self.subTest(result=result):
                self.session.reset_mock()
                self.session.get.return_value = response([event()])
                self.session.post.return_value = result
                with self.assertRaises(RuntimeError):
                    bot.poll_dates(["2026-11-15"], [], {})
                self.session.post.assert_called_once()
                self.assertEqual(self.email.call_args.args[0], [])

    def test_mail_failure_does_not_resubmit_registration(self):
        self.session.get.return_value = response([event()])
        self.email.side_effect = RuntimeError("mail unavailable")
        with self.assertRaisesRegex(RuntimeError, "mail unavailable"):
            bot.poll_dates(["2026-11-15"], [], {})
        self.session.post.assert_called_once()

    def test_non_capacity_rejection_never_switches_division(self):
        for error in ("尚未開放報名", "每帳號最多 1 位", "禁止報名"):
            self.assertFalse(bot.can_fallback_after_rejection(response({"error": error}, 403)))


class EmailTests(unittest.TestCase):
    def setUp(self):
        self.post = self.enterContext(patch.object(bot.requests, "post"))
        self.post.return_value = response({"id": "email-1"})
        self.enterContext(patch("builtins.print"))
        self.enterContext(patch.dict(bot.os.environ, {
            "NOTIFY_EMAIL": "recipient@example.com", "RESEND_API_KEY": "re_test_key",
        }, clear=True))
        self.registrations = [{"date": "2026-11-15", "title": "球敘", "location": "松山", "level": "fun"}]

    def test_resend_email_content(self):
        bot.send_success_email(self.registrations)
        self.post.assert_called_once()
        args, kwargs = self.post.call_args
        self.assertEqual(args, ("https://api.resend.com/emails",))
        self.assertEqual(kwargs["headers"], {"Authorization": "Bearer re_test_key"})
        self.assertEqual(kwargs["timeout"], (5, 20))
        self.assertFalse(kwargs["allow_redirects"])
        message = kwargs["json"]
        self.assertEqual(message["to"], ["recipient@example.com"])
        self.assertEqual(message["from"], "DinkUp <onboarding@resend.dev>")
        self.assertIn("2026-11-15", message["text"])
        self.assertIn("正取：2 位", message["text"])
        self.assertNotIn("re_test_key", str(message))

    def test_custom_or_blank_sender(self):
        for sender, expected in (("bot@example.com", "bot@example.com"), ("", "DinkUp <onboarding@resend.dev>")):
            with patch.dict(bot.os.environ, {"RESEND_FROM": sender}):
                bot.send_success_email(self.registrations)
                self.assertEqual(self.post.call_args.kwargs["json"]["from"], expected)

    def test_rejection_does_not_expose_response_or_retry(self):
        for status in (302, 401, 403, 429, 500):
            with self.subTest(status=status):
                self.post.reset_mock()
                self.post.return_value = response({"message": "re_test_key"}, status)
                with self.assertRaisesRegex(RuntimeError, str(status)) as error:
                    bot.send_success_email(self.registrations)
                self.assertNotIn("re_test_key", str(error.exception))
                self.post.assert_called_once()

    def test_timeout_does_not_retry(self):
        self.post.side_effect = requests.Timeout()
        with self.assertRaises(requests.Timeout):
            bot.send_success_email(self.registrations)
        self.post.assert_called_once()

    def test_malformed_success_is_not_reported_as_sent(self):
        for data in ({}, [], {"id": ""}, {"id": 1}):
            self.post.return_value = response(data)
            with self.assertRaises(ValueError):
                bot.send_success_email(self.registrations)
        self.post.return_value.json.side_effect = ValueError("invalid JSON")
        with self.assertRaises(ValueError):
            bot.send_success_email(self.registrations)

    def test_missing_settings_and_empty_results(self):
        with patch.dict(bot.os.environ, {}, clear=True):
            bot.send_success_email([])
            with self.assertRaisesRegex(RuntimeError, "NOTIFY_EMAIL"):
                bot.send_success_email(self.registrations)
        self.post.assert_not_called()


if __name__ == "__main__":
    unittest.main()
