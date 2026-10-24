"""Check when the watchdog re-runs, retries, alerts and recovers."""
import datetime as dt
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import watchdog
from watchdog import Schedule, decide, other_successes, sync_issue

UTC = dt.timezone.utc
POLICE = Schedule((ROOT / ".github" / "workflows" / "daily-refresh.yml").read_text(encoding="utf-8"))
TRAKT = Schedule((ROOT / ".github" / "workflows" / "trakt-suggested-refresh.yml").read_text(encoding="utf-8"))
JENNY = Schedule((ROOT / ".github" / "workflows" / "trakt-suggested-refresh-jenny.yml").read_text(encoding="utf-8"))
# The decision tests reason about a fixed six-hourly example, independent of the live file.
SIX_HOURLY = Schedule("    - cron: '41 1,7,13,19 * * *'\n")
WORKFLOW = {"name": "Daily UK police documentary refresh", "html_url": "https://example.test/wf"}


def at(text):
    return dt.datetime.fromisoformat(text).replace(tzinfo=UTC)


def run(created, status="completed", conclusion="success", number=1):
    return {"created_at": created + "Z", "status": status, "conclusion": conclusion,
            "html_url": f"https://example.test/run/{number}"}


class FakeAPI:
    def __init__(self, responses=None):
        self.calls = []
        self.responses = responses or {}

    def call(self, method, path, params=None, payload=None):
        self.calls.append((method, path, payload))
        return self.responses.get(path)


class ScheduleTests(unittest.TestCase):
    def test_reads_the_real_workflow_schedules(self):
        self.assertEqual(POLICE.times, [dt.time(6, 23)])
        self.assertEqual(POLICE.longest_gap, dt.timedelta(hours=24))
        self.assertEqual(TRAKT.times, [dt.time(h, 17) for h in range(24)])
        self.assertEqual(TRAKT.longest_gap, dt.timedelta(hours=1))
        self.assertEqual(SIX_HOURLY.longest_gap, dt.timedelta(hours=6))

    def test_slot_only_counts_once_its_grace_has_passed(self):
        self.assertEqual(POLICE.last_slot(at("2026-10-18T06:40")), at("2026-10-17T06:23"))
        self.assertEqual(POLICE.last_slot(at("2026-10-18T06:52")), at("2026-10-18T06:23"))
        self.assertEqual(SIX_HOURLY.last_slot(at("2026-10-18T01:00")), at("2026-10-17T19:41"))
        self.assertEqual(SIX_HOURLY.last_slot(at("2026-10-18T10:19")), at("2026-10-18T07:41"))

    def test_hourly_trakt_is_topped_up_every_hour_when_github_skips_it(self):
        # Worst case: GitHub never starts the schedule, so the only runs are
        # the watchdog's own dispatches at :52. Each hour must get one.
        self.assertEqual(TRAKT.last_slot(at("2026-10-18T14:52")), at("2026-10-18T14:17"))
        result = decide(at("2026-10-18T14:52"), TRAKT, [run("2026-10-18T13:52:30")])
        self.assertEqual((result.status, result.actions, result.alert), ("missed", ["dispatch"], None))
        result = decide(at("2026-10-18T14:52"), TRAKT, [run("2026-10-18T14:20:00")])
        self.assertEqual((result.status, result.actions), ("ok", []))

    def test_jennys_hourly_slot_is_due_by_the_same_watchdog_tick(self):
        self.assertIn("trakt-suggested-refresh-jenny.yml", watchdog.WATCHED)
        self.assertEqual(JENNY.times, [dt.time(h, 22) for h in range(24)])
        # :22 plus the 25-minute grace has passed at :52, so both rows are topped up together.
        self.assertEqual(JENNY.last_slot(at("2026-10-18T14:52")), at("2026-10-18T14:22"))
        result = decide(at("2026-10-18T14:52"), JENNY, [run("2026-10-18T13:52:40")])
        self.assertEqual((result.status, result.actions, result.alert), ("missed", ["dispatch"], None))
        # 1 h gap + 25 min grace + 3 h margin before an overdue alert.
        result = decide(at("2026-10-18T18:52"), TRAKT, [run("2026-10-18T14:20:00")])
        self.assertEqual(result.alert[0], "overdue:Sun 18 Oct 14:20 UTC")

    def test_rejects_schedules_it_cannot_reason_about(self):
        for cron in ("*/30 * * * *", "0 6 * * 1"):
            with self.subTest(cron=cron), self.assertRaises(ValueError):
                Schedule(f"    - cron: '{cron}'\n")


class DecisionTests(unittest.TestCase):
    def test_today_missed_police_slot_is_dispatched(self):
        # 18 Oct: 06:23 due, last scheduled run arrived 10:53 the day before.
        runs = [run("2026-10-17T10:53:52"), run("2026-10-16T11:34:12")]
        result = decide(at("2026-10-18T06:52"), POLICE, runs)
        self.assertEqual((result.status, result.actions, result.alert), ("missed", ["dispatch"], None))

    def test_late_or_running_refresh_is_left_alone(self):
        for status in ("queued", "in_progress"):
            with self.subTest(status=status):
                runs = [run("2026-10-18T06:53:00", status, None), run("2026-10-17T10:53:52")]
                result = decide(at("2026-10-18T07:52"), POLICE, runs)
                self.assertEqual((result.status, result.actions), ("running", []))

    def test_success_since_slot_is_ok(self):
        result = decide(at("2026-10-18T08:52"), SIX_HOURLY, [run("2026-10-18T07:44:00"), run("2026-10-18T01:45:00")])
        self.assertEqual((result.status, result.actions, result.alert), ("ok", [], None))

    def test_one_failure_retries_and_two_alert_without_more_runs(self):
        first = [run("2026-10-18T07:43:00", conclusion="failure", number=7), run("2026-10-18T06:13:53")]
        result = decide(at("2026-10-18T08:52"), SIX_HOURLY, first)
        self.assertEqual((result.status, result.actions, result.alert), ("retrying", ["dispatch"], None))
        second = [run("2026-10-18T08:53:00", conclusion="timed_out", number=8)] + first
        result = decide(at("2026-10-18T09:52"), SIX_HOURLY, second)
        self.assertEqual((result.status, result.actions), ("failing", []))
        self.assertEqual(result.alert[0], "failing:2026-10-18T07:41")
        self.assertIn("https://example.test/run/8", result.alert[1])

    def test_cancelled_runs_do_not_count_as_attempts(self):
        runs = [run("2026-10-18T07:50:00", conclusion="cancelled"), run("2026-10-18T06:13:53")]
        result = decide(at("2026-10-18T08:52"), SIX_HOURLY, runs)
        self.assertEqual((result.status, result.actions), ("missed", ["dispatch"]))

    def test_overdue_refresh_alerts_while_requesting_another_run(self):
        # A daily schedule may go 24 h + 25 min grace + 3 h without a success.
        result = decide(at("2026-10-19T08:52"), POLICE, [run("2026-10-18T07:00:00")])
        self.assertEqual((result.actions, result.alert), (["dispatch"], None))
        result = decide(at("2026-10-19T10:52"), POLICE, [run("2026-10-18T07:00:00")])
        self.assertEqual(result.actions, ["dispatch"])
        self.assertEqual(result.alert[0], "overdue:Sun 18 Oct 07:00 UTC")
        result = decide(at("2026-10-19T08:52"), POLICE, [run("2026-10-19T06:40:00", conclusion="failure"),
                                                          run("2026-10-17T07:00:00")])
        self.assertEqual((result.status, result.actions), ("retrying", ["dispatch"]))
        self.assertTrue(result.alert[0].startswith("overdue:"))
        result = decide(at("2026-10-19T08:52"), POLICE, [])
        self.assertEqual(result.alert[0], "overdue:never")

    def test_a_success_found_elsewhere_counts_when_the_run_list_is_empty(self):
        # 22 Oct 16:52: GitHub listed no Trakt runs, but the 15:52 refresh had committed.
        commit = [at("2026-10-22T15:52:54")]
        result = decide(at("2026-10-22T16:52"), TRAKT, [], other_successes=commit)
        self.assertEqual((result.status, result.actions, result.alert), ("missed", ["dispatch"], None))
        self.assertEqual(result.last_success, commit[0])
        # Evidence from after the due time means the slot was served: nothing to do.
        result = decide(at("2026-10-22T16:52"), TRAKT, [], other_successes=[at("2026-10-22T16:20:10")])
        self.assertEqual((result.status, result.actions, result.alert), ("ok", [], None))
        # With no evidence anywhere, it still alerts.
        self.assertEqual(decide(at("2026-10-22T16:52"), TRAKT, []).alert[0], "overdue:never")

    def test_inactivity_disable_is_healed_but_manual_disable_is_respected(self):
        runs = [run("2026-10-17T10:53:52")]
        result = decide(at("2026-10-18T06:52"), POLICE, runs, "disabled_inactivity")
        self.assertEqual(result.actions, ["enable", "dispatch"])
        result = decide(at("2026-10-18T06:52"), POLICE, runs, "disabled_manually")
        self.assertEqual((result.status, result.actions, result.alert), ("disabled by hand", [], None))


class EvidenceTests(unittest.TestCase):
    def test_reads_successes_from_the_repo_run_list_and_refresh_commits(self):
        def commit(message, date):
            return {"commit": {"message": message, "committer": {"date": date + "Z"}}}
        api = FakeAPI({
            "/actions/runs": {"workflow_runs": [
                dict(run("2026-10-22T15:52:13"), path=".github/workflows/trakt-suggested-refresh.yml"),
                dict(run("2026-10-22T15:41:47"), path=".github/workflows/daily-refresh.yml"),
                dict(run("2026-10-22T14:52:00", conclusion="failure"), path=".github/workflows/trakt-suggested-refresh.yml"),
            ]},
            "/commits": [commit("Refresh Trakt Suggested catalogues", "2026-10-22T15:52:54"),
                         commit("Daily UK police catalogue refresh", "2026-10-22T15:42:10"),
                         commit("Refresh Trakt Suggested catalogues\n\nbody", "2026-10-22T14:53:00"),
                         commit("Keep the year out of upcoming card lines", "2026-10-22T14:00:00")],
        })
        self.assertEqual(sorted(other_successes(api, "trakt-suggested-refresh.yml")),
                         [at("2026-10-22T14:53:00"), at("2026-10-22T15:52:13"), at("2026-10-22T15:52:54")])
        self.assertEqual(sorted(other_successes(api, "daily-refresh.yml")),
                         [at("2026-10-22T15:41:47"), at("2026-10-22T15:42:10")])
        self.assertTrue(all(method == "GET" for method, _, _ in api.calls))

    def test_jennys_runs_and_commits_never_count_as_stes_or_the_reverse(self):
        def commit(message, date):
            return {"commit": {"message": message, "committer": {"date": date + "Z"}}}
        api = FakeAPI({
            "/actions/runs": {"workflow_runs": [
                dict(run("2026-10-22T15:52:20"), path=".github/workflows/trakt-suggested-refresh-jenny.yml"),
                dict(run("2026-10-22T15:52:13"), path=".github/workflows/trakt-suggested-refresh.yml"),
            ]},
            "/commits": [commit("Refresh Jenny's Trakt Suggested catalogues", "2026-10-22T15:53:01"),
                         commit("Refresh Trakt Suggested catalogues", "2026-10-22T15:52:54")],
        })
        self.assertEqual(sorted(other_successes(api, "trakt-suggested-refresh-jenny.yml")),
                         [at("2026-10-22T15:52:20"), at("2026-10-22T15:53:01")])
        self.assertEqual(sorted(other_successes(api, "trakt-suggested-refresh.yml")),
                         [at("2026-10-22T15:52:13"), at("2026-10-22T15:52:54")])

    def test_an_unreachable_source_is_skipped(self):
        class Down(FakeAPI):
            def call(self, method, path, params=None, payload=None):
                raise watchdog.urllib.error.URLError("down")
        self.assertEqual(other_successes(Down(), "daily-refresh.yml"), [])


class IssueTests(unittest.TestCase):
    def failing(self):
        runs = [run("2026-10-18T07:30:00", conclusion="failure", number=2),
                run("2026-10-18T06:53:00", conclusion="failure", number=1)]
        return decide(at("2026-10-18T07:52"), POLICE, runs)

    def test_one_issue_per_problem_then_closed_on_recovery(self):
        api = FakeAPI()
        result = self.failing()
        self.assertEqual(sync_issue(api, [], WORKFLOW, result, "stegalloway"), "opened alert")
        method, path, payload = api.calls[0]
        self.assertEqual((method, path), ("POST", "/issues"))
        self.assertEqual(payload["title"], watchdog.ISSUE_PREFIX + WORKFLOW["name"])
        self.assertTrue(payload["body"].startswith("@stegalloway "))

        issue = {"number": 5, "title": payload["title"], "body": payload["body"]}
        api.calls.clear()
        self.assertIsNone(sync_issue(api, [issue], WORKFLOW, result, "stegalloway"))
        self.assertEqual(api.calls, [])

        result.alert = ("failing:2026-10-19T06:23", "Next day also failed.")
        self.assertEqual(sync_issue(api, [issue], WORKFLOW, result, "stegalloway"), "updated alert")
        self.assertEqual([c[:2] for c in api.calls], [("PATCH", "/issues/5"), ("POST", "/issues/5/comments")])

        api.calls.clear()
        ok = decide(at("2026-10-19T07:52"), POLICE, [run("2026-10-19T06:30:00")])
        self.assertEqual(sync_issue(api, [issue], WORKFLOW, ok, "stegalloway"), "closed alert")
        # One edit, no comment: the recovery note goes in the body as the issue closes.
        self.assertEqual(len(api.calls), 1)
        method, path, payload = api.calls[0]
        self.assertEqual((method, path, payload["state"], payload["state_reason"]), ("PATCH", "/issues/5", "closed", "completed"))
        self.assertTrue(payload["body"].startswith(issue["body"]))
        self.assertIn("Recovered: a refresh succeeded at Mon 19 Oct 06:30 UTC.", payload["body"])

    def test_quiet_when_nothing_is_wrong(self):
        api = FakeAPI()
        ok = decide(at("2026-10-18T08:52"), SIX_HOURLY, [run("2026-10-18T07:44:00")])
        self.assertIsNone(sync_issue(api, [], WORKFLOW, ok, "stegalloway"))
        missed = decide(at("2026-10-18T08:52"), SIX_HOURLY, [run("2026-10-18T06:13:53")])
        self.assertIsNone(sync_issue(api, [], WORKFLOW, missed, "stegalloway"))
        self.assertEqual(api.calls, [])


if __name__ == "__main__":
    unittest.main()
