#!/usr/bin/env python3
"""KLC outside-in status monitor.

Runs about every 5 minutes on GitHub Actions, i.e. OUTSIDE the KLC production server, so it can
report when that server is unreachable. It:
  1. checks that each KLC service answers with what that service normally returns
     (a Traefik "404 page not found", a 502/503, a certificate error or a timeout all count as DOWN);
  2. checks how many days are left on each HTTPS certificate;
  3. writes site/status.json (read by https://status.klc.lk) when anything changes and at least
     once an hour, so readers can tell the data is fresh;
  4. opens a GitHub issue when something goes down (GitHub emails the repository owner),
     comments on it every hour while it stays down, and closes it after two healthy runs in a row.

The status page must never depend on GitHub's issue API: every API call is best-effort.
Only Python's standard library is used. Nothing secret is stored here; GITHUB_TOKEN is the
short-lived token GitHub gives each run.
"""
import datetime as dt
import json
import os
import re
import socket
import ssl
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

STATUS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "site", "status.json")
USER_AGENT = "KLC-Status-Monitor/1.0 (+https://status.klc.lk)"
TIMEOUT = 20            # seconds per request
RETRIES = 2             # extra attempts for a failed check, so one blip is not an outage
RETRY_WAIT = 20         # seconds between attempts
CERT_WARN_DAYS = 14     # certificates renew ~30 days before expiry; under 14 means renewal is failing
REMIND_MINUTES = 60     # while something is down, add a reminder to the issue this often
CHANGE_NOTE_MINUTES = 15  # at most one "what is down changed" comment per this many minutes
CLOSE_AFTER_OK_RUNS = 2   # healthy runs in a row before an outage issue is closed (stops flapping)
COLOMBO = dt.timezone(dt.timedelta(hours=5, minutes=30))

# Each check: (url, expected HTTP status, text the body must contain).
# The APIs have no public health route yet, so they are checked with a path that does not exist:
# the app's own JSON reply ("message":"Cannot GET ...") proves the app itself answered, while
# Traefik alone would say "404 page not found". This proves the app is up, not that every feature
# works; replace with /health once the APIs have one.
SOMS_API = ("https://api.klc.lk/zz-status-probe", 404, '"message":"Cannot GET /zz-status-probe"')
SERVICES = [
    {"id": "soms", "name": "SOMS (staff)", "checks": [
        ("https://soms.klc.lk/login", 200, "<title>KLC SOMS</title>"), SOMS_API]},
    {"id": "parent", "name": "Parent Portal", "checks": [
        ("https://parent.klc.lk/", 200, "<title>KLC SOMS</title>"), SOMS_API]},
    {"id": "teacher", "name": "Teacher Portal", "checks": [
        ("https://teacher.klc.lk/", 200, "<title>KLC SOMS</title>"), SOMS_API]},
    {"id": "finance", "name": "Finance Portal", "checks": [
        ("https://finance.klc.lk/", 200, "<title>KLC SOMS</title>"),
        ("https://finance-api.klc.lk/api/v1/zz-status-probe", 404, '"message":"Cannot GET /api/v1/zz-status-probe"')]},
    {"id": "hr", "name": "HR", "checks": [
        ("https://hr.klc.lk/", 200, "<title>KLC HR Portal</title>"),
        ("https://hr-api.klc.lk/api/zz-status-probe", 404, '"message":"Cannot GET /api/zz-status-probe"')]},
    {"id": "call", "name": "Call Centre", "checks": [
        ("https://call.klc.lk/", 200, "<title>KLC Call Intelligence</title>"),
        ("https://call-api.klc.lk/api/v1/health", 200, '"status":"ok"')]},
    {"id": "reports", "name": "Academic Reports", "checks": [
        ("https://report.klc.lk/", 200, "<title>KLC Academic Reports</title>")]},
    {"id": "website", "name": "Website", "checks": [
        ("https://www.klc.lk/", 200, "<title>KLC Cambridge English")]},
]
# A site outside KLC: if this also fails, the problem is the runner's own network, not KLC.
CONTROL = ("https://www.githubstatus.com/", 200, "GitHub")

CERT_HOSTS = ["soms.klc.lk", "parent.klc.lk", "teacher.klc.lk", "finance.klc.lk", "api.klc.lk",
              "finance-api.klc.lk", "hr.klc.lk", "hr-api.klc.lk", "call.klc.lk", "call-api.klc.lk",
              "report.klc.lk", "www.klc.lk"]


def now_utc():
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0)


def iso(t):
    return t.isoformat().replace("+00:00", "Z")


def parse_iso(s):
    try:
        return dt.datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except ValueError:
        return None


def colombo(t):
    return t.astimezone(COLOMBO).strftime("%d %b %Y, %I:%M %p") + " Sri Lanka time"


def safe(s):
    """Network text going into a public issue: keep it plain and inside a code span
    (no @mentions, links, images or HTML can render)."""
    return "`" + re.sub(r"[^\w .,:;/()'=+\-]", "?", str(s))[:200] + "`"


def fetch(url):
    """Return (status, body_text, error_text)."""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Cache-Control": "no-cache"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            return r.status, r.read(400_000).decode("utf-8", "replace"), ""
    except urllib.error.HTTPError as e:
        try:
            body = e.read(400_000).decode("utf-8", "replace")
        except Exception:  # noqa: BLE001
            body = ""
        return e.code, body, ""
    except Exception as e:  # noqa: BLE001 - timeouts, refused connections, TLS errors
        return 0, "", f"{type(e).__name__}: {e}"


def check_once(url, want_status, want_text):
    status, body, err = fetch(url)
    if err:
        return False, err
    if status != want_status:
        return False, f"HTTP {status} (expected {want_status})"
    if want_text not in body:
        return False, f"HTTP {status} but the expected page content was missing"
    return True, f"HTTP {status}"


def check(url, want_status, want_text):
    ok, detail = check_once(url, want_status, want_text)
    for _ in range(RETRIES):
        if ok:
            break
        time.sleep(RETRY_WAIT)
        ok, detail = check_once(url, want_status, want_text)
    return ok, detail


def cert_days_left(host):
    """Days until the certificate expires, or None if it could not be read (that host's own
    service check already reports invalid or unreachable certificates)."""
    ctx = ssl.create_default_context()
    try:
        with socket.create_connection((host, 443), timeout=TIMEOUT) as sock:
            with ctx.wrap_socket(sock, server_hostname=host) as tls:
                not_after = tls.getpeercert()["notAfter"]
        expires = dt.datetime.fromtimestamp(ssl.cert_time_to_seconds(not_after), dt.timezone.utc)
        return (expires - now_utc()).days
    except Exception:  # noqa: BLE001
        return None


def gh_api(method, path, payload=None):
    """Best-effort GitHub API call. Returns the parsed reply, or None on any failure.
    Without a token (local dry run) it only prints what it would do and returns None."""
    token = os.environ.get("GITHUB_TOKEN")
    repo = os.environ.get("GITHUB_REPOSITORY")
    if not token or not repo:
        print(f"[dry-run] would call GitHub API: {method} {path}")
        return None
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        f"https://api.github.com/repos/{repo}{path}", data=data, method=method,
        headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
                 "User-Agent": USER_AGENT, "X-GitHub-Api-Version": "2022-11-28"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            return json.loads(r.read() or b"{}")
    except Exception as e:  # noqa: BLE001
        print(f"::warning::GitHub API {method} {path} failed: {type(e).__name__}: {e}")
        return None


def open_issue_numbers(label):
    found = gh_api("GET", f"/issues?state=open&labels={label}&per_page=20")
    return [i["number"] for i in found if "pull_request" not in i] if isinstance(found, list) else []


def create_issue(title, body, label):
    owner = os.environ.get("GITHUB_REPOSITORY_OWNER", "")
    payload = {"title": title, "body": body, "labels": [label]}
    if owner:
        payload["assignees"] = [owner]   # makes the owner a participant: the most reliably emailed case
    issue = gh_api("POST", "/issues", payload)
    return issue.get("number") if isinstance(issue, dict) else None


def comment(number, body):
    return gh_api("POST", f"/issues/{number}/comments", {"body": body}) is not None


def close(number, body):
    comment(number, body)
    return gh_api("PATCH", f"/issues/{number}", {"state": "closed", "state_reason": "completed"}) is not None


def load_previous():
    try:
        with open(STATUS_FILE, encoding="utf-8") as f:
            return json.load(f)
    except OSError:
        return {}
    except ValueError:
        print("::error::site/status.json is not valid JSON; starting from a clean state")
        return {}


def main():
    t = now_utc()
    prev = load_previous()
    internal = prev.get("_monitor", {}) if isinstance(prev.get("_monitor"), dict) else {}

    # 1. service checks, run in parallel (a check shared by several services is fetched once)
    unique = sorted({c for svc in SERVICES for c in svc["checks"]} | {CONTROL})
    with ThreadPoolExecutor(max_workers=len(unique)) as pool:
        cache = dict(zip(unique, pool.map(lambda c: check(*c), unique)))
    results = []
    for svc in SERVICES:
        problems = [f"{c[0]}: {cache[c][1]}" for c in svc["checks"] if not cache[c][0]]
        results.append({"id": svc["id"], "name": svc["name"], "ok": not problems, "problems": problems})
    down = [r for r in results if not r["ok"]]

    for r in results:
        print(("OK   " if r["ok"] else "DOWN ") + r["name"] + "".join("\n       " + p for p in r["problems"]))
    if down and not cache[CONTROL][0]:
        print(f"::warning::The monitor's own network looks broken ({cache[CONTROL][1]}); nothing recorded this run.")
        return 0

    state = "operational" if not down else ("outage" if len(down) == len(results) else "partial")
    down_ids = sorted(r["id"] for r in down)
    prev_down_ids = sorted(s.get("id") for s in prev.get("services", []) if not s.get("ok", True))
    changed = state != prev.get("state") or down_ids != prev_down_ids

    # 2. certificates close to expiry
    with ThreadPoolExecutor(max_workers=len(CERT_HOSTS)) as pool:
        days_left = dict(zip(CERT_HOSTS, pool.map(cert_days_left, CERT_HOSTS)))
    cert_warnings = [f"{h}: certificate expires in {d} days" for h, d in days_left.items()
                     if d is not None and d < CERT_WARN_DAYS]
    for w in cert_warnings:
        print("CERT " + w)
    print(f"state={state} changed={changed}")

    # 3. outage issue (best-effort; GitHub emails the owner about new issues and comments)
    names = ", ".join(r["name"] for r in down)
    details = "\n".join(f"- **{r['name']}**: " + "; ".join(safe(p) for p in r["problems"]) for r in down)
    issue_no = internal.get("incident_issue")
    last_note = parse_iso(internal.get("incident_last_notice", ""))
    ok_streak = internal.get("ok_streak", 0) + 1 if state == "operational" else 0
    if state != "operational":
        if not issue_no:
            existing = open_issue_numbers("outage")
            issue_no = existing[0] if existing else None
        if not issue_no:
            issue_no = create_issue(
                f"KLC systems not answering: {names}",
                f"Detected by the outside monitor at {colombo(t)}.\n\n{details}\n\n"
                "This issue closes itself when everything answers again. Status page: https://status.klc.lk",
                "outage")
            if issue_no:
                last_note = t
        else:
            mins = (t - last_note).total_seconds() / 60 if last_note else 1e9
            if (changed and mins >= CHANGE_NOTE_MINUTES) or mins >= REMIND_MINUTES:
                if comment(issue_no, f"Still not answering at {colombo(t)}: {names}.\n\n{details}"):
                    last_note = t
    elif issue_no and ok_streak >= CLOSE_AFTER_OK_RUNS:
        if close(issue_no, f"All KLC systems are answering again at {colombo(t)}."):
            for extra in open_issue_numbers("outage"):   # tidy any duplicates from earlier failures
                if extra != issue_no:
                    close(extra, "Closed together with the main outage issue: everything is answering again.")
            issue_no, last_note = None, None

    # certificate issue (only real "expires soon" warnings)
    cert_issue = internal.get("cert_issue")
    cert_last = parse_iso(internal.get("cert_last_notice", ""))
    if cert_warnings:
        text = "\n".join("- " + safe(w) for w in cert_warnings)
        if not cert_issue:
            existing = open_issue_numbers("certificate")
            cert_issue = existing[0] if existing else None
        if not cert_issue:
            cert_issue = create_issue(
                "KLC security certificate needs attention",
                f"Found by the outside monitor at {colombo(t)}.\n\n{text}\n\n"
                "Certificates normally renew themselves about 30 days before they expire, "
                "so this usually means renewal is failing on the server.",
                "certificate")
            if cert_issue:
                cert_last = t
        elif not cert_last or (t - cert_last).total_seconds() >= 24 * 3600:
            if comment(cert_issue, f"Still needs attention at {colombo(t)}:\n\n{text}"):
                cert_last = t
    elif cert_issue:
        if close(cert_issue, f"All certificates are fine again at {colombo(t)}."):
            cert_issue, cert_last = None, None

    # 4. status.json: on any change, and at least once an hour as a freshness heartbeat
    new_internal = {
        "incident_issue": issue_no,
        "incident_last_notice": iso(last_note) if last_note else "",
        "ok_streak": ok_streak if issue_no else 0,
        "cert_issue": cert_issue,
        "cert_last_notice": iso(cert_last) if cert_last else "",
    }
    heartbeat = str(prev.get("checked_at", ""))[:13] != iso(t)[:13]
    publish = changed or heartbeat
    written = publish or new_internal != internal
    if written:
        doc = {
            "state": state,
            "since": iso(t) if changed else prev.get("since", iso(t)),
            "checked_at": iso(t) if publish else prev.get("checked_at", iso(t)),
            "services": [{"id": r["id"], "name": r["name"], "ok": r["ok"]} for r in results],
            "_monitor": new_internal,
        }
        with open(STATUS_FILE, "w", encoding="utf-8") as f:
            json.dump(doc, f, ensure_ascii=False, indent=2)
            f.write("\n")
        print("status.json updated")

    out = os.environ.get("GITHUB_OUTPUT")
    if out:
        with open(out, "a", encoding="utf-8") as f:
            f.write(f"state={state}\nwritten={'true' if written else 'false'}\ndeploy={'true' if publish else 'false'}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
