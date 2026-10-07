"""Run with:  python3 -m unittest -v   (from the repository folder)"""

import tempfile
import unittest
from datetime import datetime
from pathlib import Path

import monitor

FIXTURE = Path(__file__).parent / "fixtures" / "calendar_en_2026-10-05.html"
NOW = datetime(2026, 10, 5, 21, 40, tzinfo=monitor.ROME)


def live_page() -> str:
    return FIXTURE.read_text(encoding="utf-8")


def page_with_open_seats(seats: str = "4") -> str:
    """Today's page, but with the Perugia CEnT@HOME session reopened."""
    html = live_page()
    start = html.index("Università degli studi di Perugia")
    end = html.index("</tr>", start)
    row = html[start:end]
    new_row = (row.replace("---", seats, 1)
                  .replace("NOT LONGER AVAILABLE", "AVAILABLE SEATS", 1))
    return html[:start] + new_row + html[end:]


class Outbox:
    def __init__(self, fail=False):
        self.sent, self.fail = [], fail

    def __call__(self, subject, body):
        if self.fail:
            raise RuntimeError("SMTP down")
        self.sent.append((subject, body))


class ParseTests(unittest.TestCase):
    def test_live_page_home_sessions_only(self):
        sessions = monitor.parse(live_page())
        self.assertEqual(len(sessions), 5)                      # 5 CENT@HOME rows on 5 Oct
        self.assertTrue(all(s["mode"] == "CENT@HOME" for s in sessions))
        self.assertFalse(any(s["available"] for s in sessions))  # all sold out
        perugia = sessions[0]
        self.assertEqual(perugia["university"], "Università degli studi di Perugia")
        self.assertEqual(perugia["date"], "15/10/2026")
        self.assertEqual(perugia["deadline"], "09/10/2026")

    def test_uni_sessions_with_seats_are_ignored(self):
        # Siena and Udine (CENT@UNI) have seats on the live page; they must not count.
        names = {s["university"] for s in monitor.parse(live_page())}
        self.assertFalse(any("Siena" in n or "Udine" in n for n in names))

    def test_open_seats_detected(self):
        s = monitor.parse(page_with_open_seats("4"))[0]
        self.assertTrue(s["available"])
        self.assertEqual(s["seats"], 4)

    def test_available_without_number(self):
        s = monitor.parse(page_with_open_seats("---"))[0]
        self.assertTrue(s["available"])
        self.assertIsNone(s["seats"])
        self.assertIn("exact number", monitor.places_text(s))

    def test_italian_labels_also_work(self):
        html = page_with_open_seats("2")
        for en, it in [("FORMAT", "MODALITÀ"), ("UNIVERSITY", "UNIVERSITÀ"), ("SEATS", "POSTI"),
                       ("STATE", "STATO"), (">DATE<", ">DATA TEST<"),
                       ("BOOKINGS DEADLINE", "FINE ISCRIZIONI"), ("CENT@HOME", "CENT@CASA"),
                       ("AVAILABLE SEATS", "POSTI DISPONIBILI"), ("NOT LONGER AVAILABLE", "POSTI ESAURITI")]:
            html = html.replace(en, it)
        sessions = monitor.parse(html)
        self.assertEqual(len(sessions), 5)
        self.assertEqual([s["seats"] for s in sessions if s["available"]], [2])

    def test_not_longer_available_is_not_available(self):
        self.assertFalse(monitor.is_available("NOT LONGER AVAILABLE", None))
        self.assertFalse(monitor.is_available("POSTI ESAURITI", None))
        self.assertFalse(monitor.is_available("AVAILABLE SEATS", 0))
        self.assertTrue(monitor.is_available("AVAILABLE SEATS", 3))

    def test_broken_page_raises(self):
        with self.assertRaises(monitor.CheckFailed):
            monitor.parse("<html><body>Service unavailable</body></html>")


class CheckTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._orig = monitor.STATE_FILE
        monitor.STATE_FILE = Path(self.tmp.name) / "state.json"

    def tearDown(self):
        monitor.STATE_FILE = self._orig
        self.tmp.cleanup()

    def run_check(self, html, outbox):
        return monitor.check(html=html, now=NOW, send=outbox)

    def test_no_email_when_all_full(self):
        box = Outbox()
        self.assertEqual(self.run_check(live_page(), box), 0)
        self.assertEqual(box.sent, [])

    def test_email_when_place_opens_with_date_university_places(self):
        box = Outbox()
        self.run_check(page_with_open_seats("4"), box)
        self.assertEqual(len(box.sent), 1)
        subject, body = box.sent[0]
        self.assertIn("Perugia", subject)
        self.assertIn("Date:        15/10/2026", body)
        self.assertIn("University:  Università degli studi di Perugia", body)
        self.assertIn("Places:      4 places available", body)
        self.assertIn(monitor.BOOKING_URL, body)

    def test_no_repeat_email_for_same_places(self):
        box = Outbox()
        for _ in range(3):
            self.run_check(page_with_open_seats("4"), box)
        self.assertEqual(len(box.sent), 1)

    def test_fewer_places_silent_more_places_alerts(self):
        box = Outbox()
        self.run_check(page_with_open_seats("4"), box)
        self.run_check(page_with_open_seats("2"), box)   # someone booked: quiet
        self.assertEqual(len(box.sent), 1)
        self.run_check(page_with_open_seats("6"), box)   # places added: alert
        self.assertEqual(len(box.sent), 2)

    def test_sold_out_then_reopened_alerts_again(self):
        box = Outbox()
        self.run_check(page_with_open_seats("1"), box)
        self.run_check(live_page(), box)                 # sold out again
        self.run_check(page_with_open_seats("1"), box)   # reopened
        self.assertEqual(len(box.sent), 2)

    def test_failed_email_is_retried_next_check(self):
        self.assertEqual(self.run_check(page_with_open_seats("4"), Outbox(fail=True)), 1)
        box = Outbox()
        self.run_check(page_with_open_seats("4"), box)
        self.assertEqual(len(box.sent), 1)

    def test_problem_email_once_after_three_failures_then_recovery(self):
        box = Outbox()
        broken = "<html>maintenance</html>"
        codes = [self.run_check(broken, box) for _ in range(5)]
        self.assertEqual(codes, [0, 0, 1, 1, 1])         # red only from the 3rd failure
        self.assertEqual([s for s, _ in box.sent], ["CEnT@HOME monitor: problem reading CISIA"])
        self.run_check(live_page(), box)
        self.assertEqual(box.sent[-1][0], "CEnT@HOME monitor: working again")
        self.assertEqual(len(box.sent), 2)

    def test_failure_does_not_forget_known_places(self):
        box = Outbox()
        self.run_check(page_with_open_seats("4"), box)
        self.run_check("<html>down</html>", box)
        self.run_check(page_with_open_seats("4"), box)   # same places as before the outage
        self.assertEqual(len(box.sent), 1)


if __name__ == "__main__":
    unittest.main()


class SendEmailTests(unittest.TestCase):
    def test_one_standard_message_per_recipient(self):
        import os, smtplib
        from unittest import mock
        sent = []

        class FakeSMTP:
            def __init__(self, *a, **k): pass
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def login(self, user, pw): pass
            def send_message(self, msg):
                if msg["To"] == "bad@gmail.com":
                    raise smtplib.SMTPRecipientsRefused({})
                sent.append(msg)

        env = {"GMAIL_ADDRESS": "me@gmail.com", "GMAIL_APP_PASSWORD": "abcd efgh",
               "EMAIL_TO": "a@gmail.com, bad@gmail.com ,b@gmail.com"}
        with mock.patch.dict(os.environ, env), mock.patch("smtplib.SMTP_SSL", FakeSMTP), \
                mock.patch("time.sleep"):
            monitor.send_email("CEnT@HOME place available: Sapienza Università di Roma – 15/10/2026",
                               "University:  Sapienza Università di Roma")
        self.assertEqual([m["To"] for m in sent], ["a@gmail.com", "b@gmail.com"])
        m = sent[0]
        self.assertEqual(m["Subject"], "CEnT@HOME place available: Sapienza Universita di Roma - 15/10/2026")
        self.assertTrue(m["Date"] and m["Message-ID"].endswith("@gmail.com>"))
        self.assertIn("Università", m.get_content())   # body keeps the accent
