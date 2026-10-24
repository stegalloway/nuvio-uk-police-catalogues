#!/usr/bin/env python3
"""Re-run missed catalogue refreshes and raise one GitHub issue when they keep failing.

GitHub delays and sometimes drops scheduled runs. Each watched workflow's own cron
lines define its due times; after a short grace period the watchdog dispatches a
missed slot once, retries one failure, and alerts after a second failure or when
no refresh has succeeded for longer than the schedule allows.

GitHub's per-workflow run list can also come back without recent runs (22 Oct 2026:
"last success never" for three hours while every hourly refresh succeeded). Before
treating a refresh as missed, the watchdog looks for a success in two other places:
the repository-wide run list and the refresh's own commit on the branch.
"""
import argparse
import dataclasses
import datetime as dt
import json
import os
import pathlib
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"
WATCHED = ("daily-refresh.yml", "trakt-suggested-refresh.yml", "trakt-suggested-refresh-jenny.yml")
BRANCH = "main"
GRACE = dt.timedelta(minutes=25)
OVERDUE_MARGIN = dt.timedelta(hours=3)
ACTIVE = {"queued", "in_progress", "waiting", "pending", "requested"}
FAILED = {"failure", "timed_out", "startup_failure"}
ISSUE_PREFIX = "Catalogue refresh alert: "
KEY_MARK = re.compile(r"<!-- watchdog-key: (.*?) -->")
# Each refresh commits under its own message when its output changed; the police refresh
# always does (its status file records the run time). A matching commit is proof of a success.
COMMIT_MESSAGES = {"daily-refresh.yml": "Daily UK police catalogue refresh",
                   "trakt-suggested-refresh.yml": "Refresh Trakt Suggested catalogues",
                   "trakt-suggested-refresh-jenny.yml": "Refresh Jenny's Trakt Suggested catalogues"}


def utc(raw):
    return dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))


def stamp(moment):
    return moment.strftime("%a %d %b %H:%M UTC") if moment else "never"


class Schedule:
    """Daily UTC due times from a workflow's `minute hour * * *` cron lines."""

    def __init__(self, workflow_text):
        times = set()
        for expr in re.findall(r"cron:\s*['\"]([^'\"]+)['\"]", workflow_text):
            fields = expr.split()
            if len(fields) != 5 or fields[2:] != ["*", "*", "*"]:
                raise ValueError(f"Unsupported cron for the watchdog: {expr!r}")
            minutes = [int(m) for m in fields[0].split(",")]
            hours = range(24) if fields[1] == "*" else [int(h) for h in fields[1].split(",")]
            times.update(dt.time(h, m) for h in hours for m in minutes)
        if not times:
            raise ValueError("Workflow has no schedule to watch")
        self.times = sorted(times)
        marks = [t.hour * 60 + t.minute for t in self.times]
        gaps = [b - a for a, b in zip(marks, marks[1:])] + [marks[0] + 1440 - marks[-1]]
        self.longest_gap = dt.timedelta(minutes=max(gaps))

    def last_slot(self, now):
        """Latest due time whose grace period has already passed."""
        cutoff = now - GRACE
        for days in (0, 1):
            day = (cutoff - dt.timedelta(days=days)).date()
            for t in reversed(self.times):
                slot = dt.datetime.combine(day, t, dt.timezone.utc)
                if slot <= cutoff:
                    return slot
        raise AssertionError("a daily schedule always has a slot in the previous day")


@dataclasses.dataclass
class Result:
    status: str
    slot: dt.datetime
    last_success: dt.datetime = None
    latest: dict = None
    actions: list = dataclasses.field(default_factory=list)
    alert: tuple = None  # (key, message)


def decide(now, schedule, runs, state="active", other_successes=()):
    """Choose what to do for one workflow from its recent runs (newest first).

    `other_successes` are success times found outside the workflow's own run list.
    """
    slot = schedule.last_slot(now)
    successes = [utc(r["created_at"]) for r in runs if r.get("conclusion") == "success"] + list(other_successes)
    result = Result("ok", slot, max(successes, default=None), runs[0] if runs else None)
    if state == "disabled_manually":
        result.status = "disabled by hand"
        return result
    if state == "disabled_inactivity":
        result.actions.append("enable")
    recent = [r for r in runs if utc(r["created_at"]) >= slot]
    if any(r["status"] in ACTIVE for r in recent):
        result.status = "running"
        return result
    if any(r.get("conclusion") == "success" for r in recent) or any(t >= slot for t in other_successes):
        return result
    failures = [r for r in recent if r.get("conclusion") in FAILED]
    if len(failures) >= 2:
        result.status = "failing"
        result.alert = (f"failing:{slot:%Y-%m-%dT%H:%M}",
                        f"The {stamp(slot)} refresh failed twice (original run and one retry). "
                        f"Latest failure: {failures[0]['html_url']}")
        return result
    result.status = "retrying" if failures else "missed"
    result.actions.append("dispatch")
    allowed = schedule.longest_gap + GRACE + OVERDUE_MARGIN
    if result.last_success is None or now - result.last_success > allowed:
        result.alert = (f"overdue:{stamp(result.last_success)}",
                        f"No refresh has succeeded since {stamp(result.last_success)}; "
                        f"this schedule should never go longer than {allowed}. "
                        "The watchdog has requested another run.")
    return result


class GitHub:
    def __init__(self, repo, token=None, base="https://api.github.com"):
        self.repo, self.token, self.base = repo, token, base.rstrip("/")

    def call(self, method, path, params=None, payload=None):
        url = f"{self.base}/repos/{self.repo}{path}"
        if params:
            url += "?" + urllib.parse.urlencode(params)
        headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28",
                   "User-Agent": "Nuvio-Catalogue-Watchdog/1.0"}
        if self.token:
            headers["Authorization"] = "Bearer " + self.token
        data = None
        if payload is not None:
            data = json.dumps(payload).encode()
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        with urllib.request.urlopen(req, timeout=25) as r:
            body = r.read()
        return json.loads(body) if body else None


def other_successes(api, name):
    """Success times for one workflow from the repo-wide run list and its commits on the branch."""
    found = []
    try:
        runs = api.call("GET", "/actions/runs", {"branch": BRANCH, "status": "success", "per_page": 100})
        found += [utc(r["created_at"]) for r in (runs or {}).get("workflow_runs", [])
                  if r.get("path", "").split("@")[0].endswith("/" + name) and r.get("conclusion") == "success"]
    except (urllib.error.URLError, ValueError, KeyError):
        pass
    message = COMMIT_MESSAGES.get(name)
    if message:
        try:
            commits = api.call("GET", "/commits", {"sha": BRANCH, "per_page": 100})
            found += [utc(c["commit"]["committer"]["date"]) for c in commits or []
                      if c["commit"]["message"].split("\n", 1)[0] == message]
        except (urllib.error.URLError, ValueError, KeyError):
            pass
    return found


def issue_body(owner, workflow, result, message, key):
    latest = result.latest
    latest_line = f"[{latest['conclusion'] or latest['status']}]({latest['html_url']})" if latest else "none"
    mention = f"@{owner} " if owner else ""
    return (f"{mention}{message}\n\n"
            f"- Workflow: [{workflow['name']}]({workflow['html_url']})\n"
            f"- Last success: {stamp(result.last_success)}\n"
            f"- Latest run: {latest_line}\n\n"
            "The watchdog closes this issue itself after the next successful refresh.\n"
            f"<!-- watchdog-key: {key} -->\n")


def sync_issue(api, issues, workflow, result, owner):
    """Keep exactly one open alert per workflow; only a new problem adds a comment."""
    title = ISSUE_PREFIX + workflow["name"]
    issue = next((i for i in issues if i["title"] == title), None)
    if result.status == "ok":
        if issue:
            # One edit closes it with the recovery noted: a comment would be one more email.
            body = (issue.get("body") or "") + f"\n\nRecovered: a refresh succeeded at {stamp(result.last_success)}.\n"
            api.call("PATCH", f"/issues/{issue['number']}",
                     payload={"state": "closed", "state_reason": "completed", "body": body})
        return "closed alert" if issue else None
    if not result.alert:
        return None
    key, message = result.alert
    body = issue_body(owner, workflow, result, message, key)
    if issue is None:
        api.call("POST", "/issues", payload={"title": title, "body": body})
        return "opened alert"
    current = KEY_MARK.search(issue.get("body") or "")
    if current and current.group(1) == key:
        return None
    api.call("PATCH", f"/issues/{issue['number']}", payload={"body": body})
    api.call("POST", f"/issues/{issue['number']}/comments", payload={"body": message})
    return "updated alert"


def run():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dry-run", action="store_true", help="report decisions without dispatching or touching issues")
    parser.add_argument("--now", help="ISO time to evaluate instead of the current time")
    args = parser.parse_args()
    now = utc(args.now) if args.now else dt.datetime.now(dt.timezone.utc)
    api = GitHub(os.environ.get("GITHUB_REPOSITORY", "stegalloway/nuvio-uk-police-catalogues"),
                 os.environ.get("GITHUB_TOKEN"), os.environ.get("GITHUB_API_URL", "https://api.github.com"))
    owner = os.environ.get("GITHUB_REPOSITORY_OWNER", "")
    issues = [i for i in api.call("GET", "/issues", {"state": "open", "per_page": 100}) if "pull_request" not in i]
    lines, errors = [], 0
    for name in WATCHED:
        schedule = Schedule((WORKFLOWS / name).read_text(encoding="utf-8"))
        try:
            workflow = api.call("GET", f"/actions/workflows/{name}")
        except urllib.error.HTTPError as exc:
            if exc.code != 404:
                raise
            # A watched workflow not yet on GitHub must not stop the others.
            lines.append(f"{name}: not on GitHub yet; skipped")
            continue
        runs = api.call("GET", f"/actions/workflows/{name}/runs", {"branch": BRANCH, "per_page": 30})["workflow_runs"]
        result = decide(now, schedule, runs, workflow["state"])
        if result.status in ("missed", "retrying", "failing"):
            found = other_successes(api, name)
            if found:
                result = decide(now, schedule, runs, workflow["state"], found)
        done = []
        if not args.dry_run:
            for action in result.actions:
                try:
                    if action == "enable":
                        api.call("PUT", f"/actions/workflows/{name}/enable")
                    else:
                        api.call("POST", f"/actions/workflows/{name}/dispatches", payload={"ref": BRANCH})
                    done.append(action)
                except urllib.error.HTTPError as exc:
                    errors += 1
                    result.alert = (f"api:{action}:{exc.code}",
                                    f"The watchdog could not {action} this workflow (HTTP {exc.code}).")
            note = sync_issue(api, issues, workflow, result, owner)
            if note:
                done.append(note)
        else:
            done = [f"would {a}" for a in result.actions] + (["would alert"] if result.alert else [])
        lines.append(f"{workflow['name']}: {result.status}; due {stamp(result.slot)}; "
                     f"last success {stamp(result.last_success)}; {', '.join(done) or 'no action'}"
                     + (f"\n  alert: {result.alert[1]}" if result.alert else ""))
    report = "\n".join(lines)
    print(report)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as handle:
            handle.write("\n".join("- " + line for line in lines) + "\n")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(run())
