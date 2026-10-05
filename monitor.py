"""CEnT@HOME seat monitor for CISIA's public CEnT-S calendar.

Checks the calendar, and emails you (via your own Gmail, free) when a
CEnT@HOME (= CENT@CASA, from home) session has places available.

Uses only the Python standard library: nothing to install.

Usage:
    python monitor.py                # normal check (what GitHub runs every 20 min)
    python monitor.py --show         # just print the CEnT@HOME sessions, send nothing
    python monitor.py --test-email   # send a test email to check your Gmail setup
"""

from __future__ import annotations

import argparse
import json
import os
import smtplib
import sys
import time
import urllib.request
from datetime import datetime
from email.message import EmailMessage
from html.parser import HTMLParser
from pathlib import Path
from zoneinfo import ZoneInfo

CALENDAR_URL = "https://testcisia.it/calendario.php?tolc=cents&l=gb&lingua=inglese"
BOOKING_URL = "https://www.cisiaonline.it/cent/tutto-sul-CEnT/come-prenotare-un-CEnT"
STATE_FILE = Path(__file__).with_name("state.json")
ROME = ZoneInfo("Europe/Rome")

# Send a "monitor problem" email after this many failed checks in a row
# (3 checks x 20 min = about 1 hour), so a short CISIA hiccup stays quiet.
FAILURES_BEFORE_WARNING = 3

# The page exists in English and Italian; accept both sets of labels.
HOME_MODES = {"CENT@HOME", "CENT@CASA"}
HEADERS = {
    "mode": ("FORMAT", "MODALITÀ", "MODALITA'", "MODALITA"),
    "university": ("UNIVERSITY", "UNIVERSITÀ", "UNIVERSITA'", "UNIVERSITA"),
    "city": ("CITY", "CITTÀ", "CITTA'", "CITTA"),
    "deadline": ("BOOKINGS DEADLINE", "FINE ISCRIZIONI"),
    "seats": ("SEATS", "POSTI"),
    "status": ("STATE", "STATO"),
    "date": ("DATE", "DATA TEST", "DATA"),
}


class CheckFailed(Exception):
    """The calendar could not be fetched or read. Nothing is concluded about seats."""


# ---------------------------------------------------------------- fetch & parse

def fetch(url: str = CALENDAR_URL, attempts: int = 3) -> str:
    last_error = None
    for attempt in range(1, attempts + 1):
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": "Mozilla/5.0 (CEnT@HOME seat monitor; read-only, every 20 min)",
                "Accept-Language": "en",
            })
            with urllib.request.urlopen(req, timeout=30) as resp:
                charset = resp.headers.get_content_charset() or "utf-8"
                return resp.read().decode(charset, errors="replace")
        except Exception as exc:  # network error, timeout, HTTP 5xx...
            last_error = exc
            print(f"  fetch attempt {attempt}/{attempts} failed: {exc}")
            if attempt < attempts:
                time.sleep(10 * attempt)
    raise CheckFailed(f"Could not download the CISIA calendar: {last_error}")


class _TableReader(HTMLParser):
    """Collects the text of every cell of every row in the page."""

    def __init__(self):
        super().__init__()
        self.rows: list[list[str]] = []
        self._row: list[str] | None = None
        self._cell: list[str] | None = None

    def handle_starttag(self, tag, attrs):
        if tag == "tr":
            self._row = []
        elif tag in ("td", "th") and self._row is not None:
            self._cell = []

    def handle_endtag(self, tag):
        if tag in ("td", "th") and self._row is not None and self._cell is not None:
            self._row.append(" ".join("".join(self._cell).split()))
            self._cell = None
        elif tag == "tr" and self._row is not None:
            if self._row:
                self.rows.append(self._row)
            self._row = None

    def handle_data(self, data):
        if self._cell is not None:
            self._cell.append(data)


def _find_columns(header: list[str]) -> dict[str, int] | None:
    upper = [h.upper() for h in header]
    cols = {}
    for key, names in HEADERS.items():
        for i, h in enumerate(upper):
            if h in names:
                cols[key] = i
                break
    required = {"mode", "university", "seats", "status", "date"}
    return cols if required <= cols.keys() else None


def is_available(status: str, seats: int | None) -> bool:
    s = status.upper()
    if "NOT" in s or "NON " in s or "ESAURIT" in s or "CHIUS" in s or "CLOSED" in s:
        return False
    if s.startswith("AVAILABLE") or "DISPONIBIL" in s:
        return seats is None or seats > 0
    return False


def parse(html: str) -> list[dict]:
    """Return every CEnT@HOME session on the page.

    Raises CheckFailed if the calendar table can't be recognised, so a
    changed or broken page is never mistaken for "no seats".
    """
    reader = _TableReader()
    reader.feed(html)
    cols = None
    sessions = []
    for row in reader.rows:
        if cols is None:
            cols = _find_columns(row)
            continue
        if len(row) <= max(cols.values()):
            continue
        mode = row[cols["mode"]].upper().replace(" ", "")
        if mode not in HOME_MODES:
            continue
        seats_text = row[cols["seats"]].strip()
        seats = int(seats_text) if seats_text.isdigit() else None
        status = row[cols["status"]]
        sessions.append({
            "mode": mode,
            "university": row[cols["university"]],
            "city": row[cols["city"]] if "city" in cols else "",
            "date": row[cols["date"]],
            "deadline": row[cols["deadline"]] if "deadline" in cols else "",
            "seats": seats,
            "status": status,
            "available": is_available(status, seats),
        })
    if cols is None:
        raise CheckFailed("The calendar table was not found on the page "
                          "(CISIA may have changed the page layout).")
    return sessions


def key(s: dict) -> str:
    return f"{s['university']} | {s['date']}"


# ---------------------------------------------------------------- state

def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except ValueError:
            print("  state.json was unreadable; starting fresh")
    return {"available": {}, "failures": 0, "problem_emailed": False}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
                          encoding="utf-8")


def sessions_to_alert(sessions: list[dict], previous: dict) -> list[dict]:
    """Available sessions that are new, reopened, or have MORE places than last time."""
    alert = []
    for s in sessions:
        if not s["available"]:
            continue
        k = key(s)
        if k not in previous:
            alert.append(s)  # newly available (or reopened)
        else:
            before = previous[k]
            if s["seats"] is not None and (before is None or s["seats"] > before):
                alert.append(s)  # more places than last time
    return alert


# ---------------------------------------------------------------- email

def places_text(s: dict) -> str:
    if s["seats"] is not None:
        return f"{s['seats']} place{'s' if s['seats'] != 1 else ''} available"
    return "Places available (CISIA does not show the exact number)"


def build_alert(alert: list[dict], now: datetime) -> tuple[str, str]:
    first = alert[0]
    if len(alert) == 1:
        subject = f"CEnT@HOME place available: {first['university']} – {first['date']}"
    else:
        subject = f"CEnT@HOME places available: {len(alert)} sessions"
    lines = ["CEnT@HOME (from home) places are available:", ""]
    for s in alert:
        lines += [
            f"Date:        {s['date']}",
            f"University:  {s['university']}",
            f"Places:      {places_text(s)}",
        ]
        if s["deadline"]:
            lines.append(f"Book by:     {s['deadline']}, 14:00 Italian time")
        lines.append("")
    lines += [
        "Book quickly, places go fast:",
        BOOKING_URL,
        "",
        f"Calendar: {CALENDAR_URL}",
        f"Checked: {now:%d/%m/%Y %H:%M} Italian time",
    ]
    return subject, "\n".join(lines)


def send_email(subject: str, body: str) -> None:
    sender = os.environ.get("GMAIL_ADDRESS", "").strip()
    password = os.environ.get("GMAIL_APP_PASSWORD", "").replace(" ", "").strip()
    to = [a.strip() for a in os.environ.get("EMAIL_TO", sender).split(",") if a.strip()]
    if not sender or not password or not to:
        raise RuntimeError("Email is not set up: add the GMAIL_ADDRESS and GMAIL_APP_PASSWORD "
                           "secrets (and optionally EMAIL_TO) in GitHub.")
    msg = EmailMessage()
    msg["From"] = f"CEnT@HOME monitor <{sender}>"
    msg["To"] = ", ".join(to)
    msg["Subject"] = subject
    msg.set_content(body)
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as smtp:
        smtp.login(sender, password)
        smtp.send_message(msg)
    print(f"  email sent to {len(to)} recipient(s): {subject}")


# ---------------------------------------------------------------- main

def print_sessions(sessions: list[dict]) -> None:
    if not sessions:
        print("  No CEnT@HOME sessions are listed right now.")
    for s in sessions:
        mark = "AVAILABLE" if s["available"] else "full/closed"
        seats = s["seats"] if s["seats"] is not None else "---"
        print(f"  [{mark:11}] {s['date']}  {s['university']}  seats={seats}  ({s['status']})")


def check(html: str | None = None, now: datetime | None = None, send=send_email) -> int:
    now = now or datetime.now(ROME)
    state = load_state()
    print(f"Checking {CALENDAR_URL} at {now:%d/%m/%Y %H:%M} Italian time")

    try:
        sessions = parse(html if html is not None else fetch())
    except CheckFailed as exc:
        state["failures"] = state.get("failures", 0) + 1
        print(f"::warning::Check failed ({state['failures']} in a row): {exc}")
        if state["failures"] >= FAILURES_BEFORE_WARNING and not state.get("problem_emailed"):
            try:
                send("CEnT@HOME monitor: problem reading CISIA",
                     f"The monitor has failed {state['failures']} checks in a row.\n\n"
                     f"Reason: {exc}\n\n"
                     "Until this is fixed you may MISS available places. Check the calendar "
                     f"yourself: {CALENDAR_URL}\n\n"
                     "Details are in the GitHub Actions log of the repository. You will get "
                     "one email when it works again.")
                state["problem_emailed"] = True
            except Exception as mail_exc:
                print(f"::error::Could not send the problem email either: {mail_exc}")
        save_state(state)
        # Turn the run red (GitHub then also notifies you) only after repeated failures.
        return 1 if state["failures"] >= FAILURES_BEFORE_WARNING else 0

    print_sessions(sessions)
    if state.get("problem_emailed"):
        try:
            send("CEnT@HOME monitor: working again",
                 f"The monitor can read the CISIA calendar again (after {state['failures']} "
                 "failed checks). Alerts are back on.")
        except Exception as mail_exc:
            print(f"::warning::Could not send the recovery email: {mail_exc}")
    state["failures"] = 0
    state["problem_emailed"] = False

    previous = state.get("available", {})
    alert = sessions_to_alert(sessions, previous)
    if alert:
        subject, body = build_alert(alert, now)
        try:
            send(subject, body)
        except Exception as exc:
            # Don't remember these sessions, so the next check tries the email again.
            print(f"::error::Places are available but the email could not be sent: {exc}")
            state["available"] = {k: v for k, v in previous.items()
                                  if k in {key(s) for s in sessions if s["available"]}}
            save_state(state)
            return 1
    else:
        print("  Nothing new to report, no email sent.")

    # Only saved when something actually changes, so the repo isn't flooded with commits.
    state["available"] = {key(s): s["seats"] for s in sessions if s["available"]}
    save_state(state)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--show", action="store_true", help="print sessions, send nothing, save nothing")
    ap.add_argument("--test-email", action="store_true", help="send a test email")
    args = ap.parse_args()

    if args.show:
        print_sessions(parse(fetch()))
        return 0
    if args.test_email:
        try:
            sessions = parse(fetch())
            summary = "\n".join(f"- {s['date']}  {s['university']}: "
                                f"{places_text(s) if s['available'] else 'full / closed'}"
                                for s in sessions) or "- none listed right now"
        except CheckFailed as exc:
            summary = f"(could not read the calendar right now: {exc})"
        send_email("CEnT@HOME monitor: test email",
                   "Your monitor can send email. You'll get a message like this when a "
                   "CEnT@HOME place becomes available.\n\nCEnT@HOME sessions right now:\n"
                   f"{summary}\n\nCalendar: {CALENDAR_URL}")
        return 0
    return check()


if __name__ == "__main__":
    sys.exit(main())
