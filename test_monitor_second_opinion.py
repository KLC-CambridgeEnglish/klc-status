"""Tests for the second opinion in monitor.py (standard library only, no network).

One runner is one place on the internet. When it gets no answer at all from KLC it must record nothing and ask
for a look from another place; only that second look may record an outage and email the owner.

Run: python -m unittest test_monitor_second_opinion.py
"""
import json
import os
import tempfile
import unittest
from unittest import mock

import monitor

UP = (True, "HTTP 200", True)
NO_ANSWER = (False, "URLError: <urlopen error timed out>", False)      # nothing came back at all
ERROR_PAGE = (False, "HTTP 502 (expected 200)", True)                   # the server's side answered, wrongly
TIMED_OUT = (0, "", "URLError: <urlopen error timed out>")
SCRUBBED = ("GITHUB_TOKEN", "GITHUB_REPOSITORY", "GITHUB_OUTPUT", "TEST_ALERT", "SECOND_OPINION", "TEST_SECOND_OPINION")


class Answered(unittest.TestCase):
    """check_once / check say whether anything came back, not only whether it was right."""

    def one(self, reply):
        with mock.patch.object(monitor, "fetch", return_value=reply):
            return monitor.check_once("https://x.example/", 200, "<title>KLC")

    def test_nothing_back_is_not_an_answer(self):
        self.assertEqual(self.one(TIMED_OUT), (False, "URLError: <urlopen error timed out>", False))

    def test_an_error_page_is_an_answer(self):
        self.assertEqual(self.one((502, "Bad Gateway", "")), (False, "HTTP 502 (expected 200)", True))

    def test_the_wrong_page_is_an_answer(self):
        ok, _, answered = self.one((200, "<title>Something else</title>", ""))
        self.assertEqual((ok, answered), (False, True))

    def test_the_right_page(self):
        self.assertEqual(self.one((200, "<title>KLC SOMS</title>", "")), UP)

    def test_check_reports_the_last_attempt(self):
        replies = [TIMED_OUT, TIMED_OUT, (200, "<title>KLC SOMS</title>", "")]
        with mock.patch.object(monitor, "fetch", side_effect=replies), mock.patch.object(monitor.time, "sleep"):
            self.assertEqual(monitor.check("https://x.example/", 200, "<title>KLC"), UP)
        with mock.patch.object(monitor, "fetch", return_value=TIMED_OUT), mock.patch.object(monitor.time, "sleep"):
            self.assertEqual(monitor.check("https://x.example/", 200, "<title>KLC"), NO_ANSWER)


class Rule(unittest.TestCase):
    def test_when_a_second_opinion_is_needed(self):
        ask = monitor.needs_second_opinion
        self.assertTrue(ask(any_down=True, klc_answered=False, control_ok=True))    # 24 and 25 Nov 2026
        self.assertTrue(ask(any_down=True, klc_answered=False, control_ok=False))
        self.assertTrue(ask(any_down=True, klc_answered=True, control_ok=False))    # this runner's network is in doubt
        self.assertFalse(ask(any_down=True, klc_answered=True, control_ok=True))    # KLC answered: a real fault, record it
        self.assertFalse(ask(any_down=False, klc_answered=True, control_ok=True))
        self.assertFalse(ask(any_down=False, klc_answered=True, control_ok=False))  # nothing down: nothing to doubt
        self.assertFalse(ask(any_down=[], klc_answered=False, control_ok=True))


class Runs(unittest.TestCase):
    """main() end to end with every network and GitHub call faked."""

    def run_main(self, klc, control=UP, env=None, overrides=None):
        """klc: what every KLC check gets back; overrides: {url: result} for single checks."""
        tmp = tempfile.mkdtemp()
        status_file, out_file = os.path.join(tmp, "status.json"), os.path.join(tmp, "out.txt")
        answers = {c[0]: klc for svc in monitor.SERVICES for c in svc["checks"]}
        answers[monitor.CONTROL[0]] = control
        answers.update(overrides or {})
        self.issues, self.fetched, self.certs = [], [], []
        now = monitor.iso(monitor.now_utc())
        files = {
            monitor.BACKUP_HEARTBEAT: json.dumps({"ok": True, "last_success": now, "disk_free_pct": 60}),
            monitor.RESTORE_DRILL: json.dumps({"ok": True, "drill_at": now}),
            monitor.TEACH_WATCH: json.dumps({"ok": True, "checked_at": now}),
        }

        def fake_fetch(url):
            self.fetched.append(url)
            if url in files:
                return 200, files[url], ""
            if url == monitor.REMINDER_HEALTH:
                return 404, '{"message":"Cannot GET /public/assessment-reminders/health"}', ""
            raise AssertionError(f"unexpected fetch {url}")

        def fake_create(title, text, label):
            self.issues.append((title, text, label))
            return 77

        def fake_cert(host):
            self.certs.append(host)
            return 60

        clean = {k: v for k, v in os.environ.items() if k not in SCRUBBED}
        clean["GITHUB_OUTPUT"] = out_file
        clean.update(env or {})
        with mock.patch.dict(os.environ, clean, clear=True), \
                mock.patch.object(monitor, "STATUS_FILE", status_file), \
                mock.patch.object(monitor, "check", side_effect=lambda url, status, text: answers[url]), \
                mock.patch.object(monitor, "cert_days_left", side_effect=fake_cert), \
                mock.patch.object(monitor, "fetch", side_effect=fake_fetch), \
                mock.patch.object(monitor, "create_issue", side_effect=fake_create), \
                mock.patch.object(monitor, "open_issue_numbers", return_value=[]), \
                mock.patch.object(monitor, "comment", return_value=True), \
                mock.patch.object(monitor, "close", return_value=True), \
                mock.patch.object(monitor, "ensure_label", return_value=True):
            self.assertEqual(monitor.main(), 0)
        doc = None
        if os.path.exists(status_file):
            with open(status_file, encoding="utf-8") as f:
                doc = json.load(f)
        outputs = {}
        if os.path.exists(out_file):
            with open(out_file, encoding="utf-8") as f:
                outputs = dict(line.split("=", 1) for line in f.read().splitlines())
        return doc, outputs

    # --- the case that emailed the owner twice: one runner cannot reach KLC at all -------------------------

    def test_first_runner_with_no_answer_records_nothing_and_asks_for_another_look(self):
        doc, out = self.run_main(NO_ANSWER)
        self.assertIsNone(doc)                                   # status.json untouched: the page does not turn red
        self.assertEqual(self.issues, [])                        # no issue, so no email
        self.assertEqual(out, {"second_opinion": "true", "written": "false", "deploy": "false"})
        self.assertEqual((self.fetched, self.certs), ([], []))   # and it hands over at once

    def test_second_runner_that_reaches_klc_records_a_healthy_state(self):
        doc, out = self.run_main(UP, env={"SECOND_OPINION": "true"})
        self.assertEqual(doc["state"], "operational")
        self.assertEqual(self.issues, [])
        self.assertEqual((out["state"], out["written"]), ("operational", "true"))
        self.assertNotIn("second_opinion", out)

    # --- a real outage is still reported -------------------------------------------------------------------

    def test_second_runner_with_no_answer_either_records_the_outage_and_opens_the_issue(self):
        doc, out = self.run_main(NO_ANSWER, env={"SECOND_OPINION": "true"})
        self.assertEqual(doc["state"], "outage")
        self.assertTrue(all(not s["ok"] for s in doc["services"]))
        self.assertEqual(doc["_monitor"]["incident_issue"], 77)
        self.assertEqual(len(self.issues), 1)
        title, text, label = self.issues[0]
        self.assertEqual(label, "outage")
        self.assertTrue(title.startswith("KLC systems not answering: SOMS (staff), Parent Portal"))
        self.assertIn("(checked from two separate monitor locations)", text)
        self.assertEqual(out, {"state": "outage", "written": "true", "deploy": "true"})
        # the alert is not held up reading certificates and server files that cannot answer either
        self.assertEqual((self.fetched, self.certs), ([], []))

    def test_error_pages_are_a_real_fault_and_are_recorded_by_the_first_runner(self):
        doc, out = self.run_main(ERROR_PAGE)
        self.assertEqual(doc["state"], "outage")
        self.assertEqual(len(self.issues), 1)
        self.assertNotIn("two separate monitor locations", self.issues[0][1])
        self.assertNotIn("second_opinion", out)
        self.assertEqual(len(self.certs), len(monitor.CERT_HOSTS))   # KLC answers, so everything else is read too

    def test_one_address_not_answering_while_others_do_is_recorded_by_the_first_runner(self):
        doc, out = self.run_main(UP, overrides={"https://report.klc.lk/": NO_ANSWER})
        self.assertEqual(doc["state"], "partial")
        self.assertEqual([s["id"] for s in doc["services"] if not s["ok"]], ["reports"])
        self.assertEqual(len(self.issues), 1)
        self.assertNotIn("second_opinion", out)

    def test_a_healthy_first_run_is_unchanged(self):
        doc, out = self.run_main(UP)
        self.assertEqual(doc["state"], "operational")
        self.assertEqual(self.issues, [])
        self.assertEqual(out, {"state": "operational", "written": "true", "deploy": "true"})

    # --- the control site (a site outside KLC) -------------------------------------------------------------

    def test_control_site_silent_too_asks_for_another_look_then_stays_quiet(self):
        broken = (False, "URLError: <urlopen error timed out>", False)
        doc, out = self.run_main(ERROR_PAGE, control=broken)
        self.assertIsNone(doc)
        self.assertEqual(out.get("second_opinion"), "true")
        # the second runner sees the same: its own network is in doubt, so nothing is recorded (the old rule)
        doc, out = self.run_main(ERROR_PAGE, control=broken, env={"SECOND_OPINION": "true"})
        self.assertIsNone(doc)
        self.assertEqual((self.issues, out), ([], {}))

    def test_control_site_silent_but_klc_healthy_is_a_normal_run(self):
        doc, out = self.run_main(UP, control=NO_ANSWER)
        self.assertEqual(doc["state"], "operational")
        self.assertNotIn("second_opinion", out)

    # --- the manual test switch ----------------------------------------------------------------------------

    def test_manual_test_forces_the_second_look_and_the_second_runner_ignores_the_switch(self):
        doc, out = self.run_main(UP, env={"TEST_SECOND_OPINION": "true"})
        self.assertIsNone(doc)
        self.assertEqual(out, {"second_opinion": "true", "written": "false", "deploy": "false"})
        doc, out = self.run_main(UP, env={"TEST_SECOND_OPINION": "true", "SECOND_OPINION": "true"})
        self.assertEqual(doc["state"], "operational")
        self.assertNotIn("second_opinion", out)


if __name__ == "__main__":
    unittest.main()
