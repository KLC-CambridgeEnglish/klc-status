"""Tests for the SOMS automatic-reminder check in monitor.py (standard library only, no network).

Run: python -m unittest test_monitor_reminders.py
"""
import datetime as dt
import json
import os
import tempfile
import unittest
from unittest import mock

import monitor

T0 = dt.datetime(2027, 1, 18, 11, 0, tzinfo=dt.timezone.utc)
HEALTH_OK = {"service": "assessment-reminders", "state": "OK", "systemHold": False, "heldSince": None, "adminHold": False,
             "paused": False, "lastRunStatus": "COMPLETED", "lastRunDate": "2027-01-18", "lastRunStopped": None,
             "checkedAt": "2027-01-18T11:05:00.000Z"}
HEALTH_HELD = dict(HEALTH_OK, state="SYSTEM_HOLD", systemHold=True, heldSince="2027-01-18T10:42:13.123Z",
                   lastRunStatus="STOPPED", lastRunStopped="CONSECUTIVE_FAILURES")


def body(doc):
    return json.dumps(doc)


class Verdicts(unittest.TestCase):
    def test_held(self):
        w, v = monitor.reminder_warnings(200, body(HEALTH_HELD), "", False)
        self.assertEqual(v, "HELD")
        self.assertEqual(w, ["automatic assessment reminders are on a system hold after repeated failed sends "
                             "(since 2027-01-18T10:42:13Z)"])

    def test_ok(self):
        self.assertEqual(monitor.reminder_warnings(200, body(HEALTH_OK), "", False), ([], "OK"))

    def test_admin_hold_paused_and_off_are_not_a_system_hold(self):
        for state in ("ADMIN_HOLD", "PAUSED", "OFF"):
            self.assertEqual(monitor.reminder_warnings(200, body(dict(HEALTH_OK, state=state)), "", False), ([], "OK"))

    def test_not_deployed_is_silent(self):
        nest_404 = '{"message":"Cannot GET /public/assessment-reminders/health","error":"Not Found","statusCode":404}'
        self.assertEqual(monitor.reminder_warnings(404, nest_404, "", False), ([], "NOT_DEPLOYED"))

    def test_traefik_404_is_unreadable(self):
        w, v = monitor.reminder_warnings(404, "404 page not found", "", False)
        self.assertEqual(v, "UNREADABLE")
        self.assertEqual(len(w), 1)

    def test_server_error_and_timeout(self):
        self.assertEqual(monitor.reminder_warnings(502, "Bad Gateway", "", False)[1], "UNREADABLE")
        w, v = monitor.reminder_warnings(0, "", "TimeoutError: timed out", False)
        self.assertEqual(v, "UNREADABLE")
        self.assertIn("no answer", w[0])

    def test_soms_api_down_adds_no_second_warning(self):
        self.assertEqual(monitor.reminder_warnings(0, "", "ConnectionRefusedError", True), ([], "UNREADABLE"))

    def test_unknown_state_from_a_database_error(self):
        unknown = {"service": "assessment-reminders", "state": "UNKNOWN", "systemHold": None, "heldSince": None,
                   "adminHold": None, "paused": None, "lastRunStatus": None, "lastRunDate": None,
                   "lastRunStopped": None, "checkedAt": "2027-01-18T11:05:00.000Z"}
        w, v = monitor.reminder_warnings(200, body(unknown), "", False)
        self.assertEqual(v, "UNREADABLE")
        self.assertEqual(len(w), 1)

    def test_not_json_or_unexpected_shape(self):
        self.assertEqual(monitor.reminder_warnings(200, "<html>hello</html>", "", False)[1], "UNREADABLE")
        self.assertEqual(monitor.reminder_warnings(200, "[1,2]", "", False)[1], "UNREADABLE")
        self.assertEqual(monitor.reminder_warnings(200, body(dict(HEALTH_OK, service="other")), "", False)[1], "UNREADABLE")
        self.assertEqual(monitor.reminder_warnings(200, body(dict(HEALTH_OK, state="WEIRD")), "", False)[1], "UNREADABLE")


class NoPersonalData(unittest.TestCase):
    """Whatever the body carries, only fixed words and a validated time reach a warning (and so an issue)."""

    def test_unknown_keys_never_appear(self):
        doc = dict(HEALTH_HELD, studentName="KAMAL PERERA", phone="0771234567", reason="Parent 0771234567 refused",
                   counts={"failed": 5})
        w, v = monitor.reminder_warnings(200, body(doc), "", False)
        self.assertEqual(v, "HELD")
        text = " ".join(w)
        for secret in ("KAMAL", "0771234567", "refused", "failed\": 5"):
            self.assertNotIn(secret, text)

    def test_held_since_is_validated(self):
        for junk in ("KAMAL PERERA <script>", "", "2027-13-45", 12345, None):
            w, _ = monitor.reminder_warnings(200, body(dict(HEALTH_HELD, heldSince=junk)), "", False)
            self.assertIn("since an unknown time", w[0])
            self.assertNotIn("KAMAL", w[0])

    def test_unreadable_never_echoes_the_body(self):
        w, _ = monitor.reminder_warnings(500, "Error for KAMAL PERERA 0771234567", "", False)
        self.assertNotIn("KAMAL", " ".join(w))
        self.assertNotIn("0771234567", " ".join(w))


class Issue(unittest.TestCase):
    def setUp(self):
        self.calls = []
        self.open_numbers = []
        patches = [
            mock.patch.object(monitor, "create_issue", side_effect=self.fake_create),
            mock.patch.object(monitor, "comment", side_effect=lambda n, b: self.calls.append(("comment", n, b)) or True),
            mock.patch.object(monitor, "close", side_effect=lambda n, b: self.calls.append(("close", n, b)) or True),
            mock.patch.object(monitor, "open_issue_numbers", side_effect=lambda label: list(self.open_numbers)),
            mock.patch.object(monitor, "ensure_label", side_effect=lambda *a: self.calls.append(("label",) + a) or True),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def fake_create(self, title, body_text, label):
        self.calls.append(("create", title, body_text, label))
        return 41

    def held(self):
        return monitor.reminder_warnings(200, body(HEALTH_HELD), "", False)

    def test_held_opens_one_issue_with_the_reminders_label(self):
        w, v = self.held()
        issue, last, streak = monitor.reminder_issue_step(v, w, None, None, 0, T0)
        self.assertEqual((issue, last, streak), (41, T0, 0))
        self.assertEqual(self.calls[0][:2], ("label", "reminders"))   # the label is made first if it is missing
        kind, title, text, label = self.calls[1]
        self.assertEqual((kind, label), ("create", "reminders"))
        self.assertEqual(title, "SOMS automatic reminders are on a system hold")
        self.assertIn("Review the failed reminders in SOMS → Reminder Automation (sign-in required), then press Resume.", text)
        self.assertIn("`automatic assessment reminders are on a system hold", text)

    def test_an_open_issue_is_reused(self):
        self.open_numbers = [17]
        w, v = self.held()
        issue, last, _ = monitor.reminder_issue_step(v, w, None, None, 0, T0)
        self.assertEqual((issue, last), (17, T0))
        # found, nothing created; it is reminded at once (no notice time is known), as the backup issue is
        self.assertEqual([(c[0], c[1]) for c in self.calls], [("comment", 17)])

    def test_comment_at_most_once_a_day(self):
        w, v = self.held()
        r = monitor.reminder_issue_step(v, w, 41, T0, 0, T0 + dt.timedelta(hours=5))
        self.assertEqual(r, (41, T0, 0))
        self.assertEqual(self.calls, [])
        r = monitor.reminder_issue_step(v, w, 41, T0, 0, T0 + dt.timedelta(hours=24))
        self.assertEqual(r, (41, T0 + dt.timedelta(hours=24), 0))
        self.assertEqual(self.calls[0][0], "comment")

    def test_closes_after_two_ok_runs_in_a_row(self):
        r = monitor.reminder_issue_step("OK", [], 41, T0, 0, T0 + dt.timedelta(minutes=5))
        self.assertEqual(r, (41, T0, 1))
        self.assertEqual(self.calls, [])
        r = monitor.reminder_issue_step("OK", [], 41, T0, 1, T0 + dt.timedelta(minutes=10))
        self.assertEqual(r, (None, None, 0))
        self.assertEqual(self.calls[0][0], "close")

    def test_held_again_resets_the_count(self):
        w, v = self.held()
        r = monitor.reminder_issue_step(v, w, 41, T0, 1, T0 + dt.timedelta(minutes=10))
        self.assertEqual(r[2], 0)

    def test_not_deployed_and_unreadable_neither_close_nor_reopen(self):
        for verdict in ("NOT_DEPLOYED", "UNREADABLE"):
            self.calls.clear()
            self.assertEqual(monitor.reminder_issue_step(verdict, ["x"], 41, T0, 1, T0), (41, T0, 1))
            self.assertEqual(monitor.reminder_issue_step(verdict, ["x"], None, None, 0, T0), (None, None, 0))
            self.assertEqual(self.calls, [])

    def test_ok_without_an_issue_does_nothing(self):
        self.assertEqual(monitor.reminder_issue_step("OK", [], None, None, 0, T0), (None, None, 0))
        self.assertEqual(self.calls, [])


class MainDryRun(unittest.TestCase):
    """main() end to end with every network call faked and no GitHub token (the monitor's own dry run)."""

    def run_main(self, reminder_answer, prev=None):
        tmp = tempfile.mkdtemp()
        status_file = os.path.join(tmp, "status.json")
        if prev is not None:
            with open(status_file, "w", encoding="utf-8") as f:
                json.dump(prev, f)
        heartbeat = json.dumps({"ok": True, "last_success": monitor.iso(monitor.now_utc()), "disk_free_pct": 60})
        drill = json.dumps({"ok": True, "drill_at": monitor.iso(monitor.now_utc())})

        def fake_fetch(url):
            if url == monitor.REMINDER_HEALTH:
                return reminder_answer
            if url == monitor.BACKUP_HEARTBEAT:
                return 200, heartbeat, ""
            if url == monitor.RESTORE_DRILL:
                return 200, drill, ""
            raise AssertionError(f"unexpected fetch {url}")

        env = {k: v for k, v in os.environ.items() if k not in ("GITHUB_TOKEN", "GITHUB_REPOSITORY", "GITHUB_OUTPUT", "TEST_ALERT")}
        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch.object(monitor, "STATUS_FILE", status_file), \
                mock.patch.object(monitor, "check", return_value=(True, "HTTP 200")), \
                mock.patch.object(monitor, "cert_days_left", return_value=60), \
                mock.patch.object(monitor, "fetch", side_effect=fake_fetch):
            self.assertEqual(monitor.main(), 0)
        with open(status_file, encoding="utf-8") as f:
            return json.load(f)

    def test_held_in_dry_run_keeps_reminders_out_of_the_public_services(self):
        doc = self.run_main((200, body(HEALTH_HELD), ""))
        self.assertEqual(doc["state"], "operational")
        self.assertNotIn("reminders", [s["id"] for s in doc["services"]])
        for key in ("reminder_issue", "reminder_last_notice", "reminder_ok_streak"):
            self.assertIn(key, doc["_monitor"])
        self.assertIsNone(doc["_monitor"]["reminder_issue"])   # dry run: GitHub was never called
        self.assertNotIn("assessment", json.dumps(doc))

    def test_not_deployed_runs_quietly(self):
        doc = self.run_main((404, '{"message":"Cannot GET /public/assessment-reminders/health"}', ""))
        self.assertEqual(doc["_monitor"]["reminder_ok_streak"], 0)


if __name__ == "__main__":
    unittest.main()
