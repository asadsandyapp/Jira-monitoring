"""Yesterday's log is Jira time entered on the previous working day."""
import unittest
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from monitor import db, progress

PKT = ZoneInfo("Asia/Karachi")
DEV = "2) Development"
UAT = "5) UAT - Pending"
MONDAY = datetime(2026, 10, 5, 6, 0, tzinfo=PKT)  # Monday morning


def comment(day, name="Waqar Tanveer", account="dev1", text="Opened the PR", hour=16):
    return {
        "created": f"2026-10-{day:02d}T{hour:02d}:00:00.000+0500",
        "author": {"displayName": name, "accountId": account},
        "body": {"type": "doc", "content": [
            {"type": "paragraph", "content": [{"type": "text", "text": text}]},
        ]},
    }


def card(key, status, comments, created_day=1):
    return {
        "key": key,
        "fields": {
            "summary": "Pipeline config",
            "status": {"name": status},
            "assignee": {"accountId": "dev1", "displayName": "Waqar Tanveer"},
            "comment": {"comments": comments},
            "created": f"2026-10-{created_day:02d}T09:00:00.000+0500",
            "updated": f"2026-10-{created_day:02d}T09:00:00.000+0500",
        },
    }


class WorkingDay(unittest.TestCase):
    def test_monday_checks_friday(self):
        self.assertEqual(
            progress.previous_working_day(MONDAY, PKT).isoformat(),
            "2026-10-02",
        )

    def test_tuesday_checks_monday(self):
        tuesday = datetime(2026, 10, 6, 9, 0, tzinfo=PKT)
        self.assertEqual(
            progress.previous_working_day(tuesday, PKT).isoformat(),
            "2026-10-05",
        )


def work(day, key="BSB-1", name="Waqar Tanveer", spent="1d", hour=9):
    return {
        "issue_key": key, "author": name, "author_id": "dev1",
        "started": f"2026-10-{day:02d}T{hour:02d}:00:00.000+0500",
        "time_spent": spent, "seconds": 28800,
    }


class DailyLog(unittest.TestCase):
    def board(self, issues, logs=None, now=MONDAY):
        rows = progress.build_cards(
            issues, {}, DEV, {"dev1": "developer"}, None, PKT, "7) Done", now,
        )
        return progress.present(
            rows, logs or [], now, PKT,
            {DEV, "2.1) Development - Paused", "3.1) QA Fail"},
            done_status="7) Done",
        )

    def test_friday_time_clears_monday(self):
        board = self.board([card("BSB-1", DEV, [])], [work(2)])
        self.assertEqual(board["missing"], [])
        self.assertEqual(board["cards"][0]["mark"], "logged")
        self.assertEqual(board["cards"][0]["note"], "1d on BSB-1")

    def test_a_comment_is_not_a_log(self):
        board = self.board([card("BSB-1", DEV, [comment(2)])])
        self.assertEqual(board["missing"][0]["developer"], "Waqar Tanveer")
        self.assertEqual(board["cards"][0]["mark"], "missed")

    def test_saturday_time_does_not_cover_friday(self):
        board = self.board([card("BSB-1", DEV, [])], [work(3, spent="5h")])
        self.assertEqual(board["missing"][0]["developer"], "Waqar Tanveer")
        self.assertIn("5h on BSB-1", board["missing"][0]["note"])

    def test_time_on_another_issue_covers_the_person(self):
        board = self.board(
            [card("BSB-1", DEV, []), card("BSB-2", DEV, [])],
            [work(2, key="BSB-99")],
        )
        self.assertEqual(board["missing"], [])
        marks = {row["key"]: row["mark"] for row in board["cards"]}
        self.assertEqual(marks, {"BSB-1": "missed", "BSB-2": "missed"})

    def test_someone_elses_time_is_not_a_log(self):
        board = self.board([card("BSB-1", DEV, [])], [work(2, name="Afifa Cheema")])
        self.assertEqual(len(board["missing"]), 1)

    def test_a_silent_uat_card_still_means_the_developer_did_not_log(self):
        board = self.board([card("BSB-1", UAT, [])])
        self.assertEqual(board["cards"][0]["mark"], "idle")
        self.assertEqual(board["missing"][0]["developer"], "Waqar Tanveer")

    def test_friday_time_on_a_uat_card_counts(self):
        board = self.board([card("BSB-1", UAT, [])], [work(2)])
        self.assertEqual(board["missing"], [])
        self.assertEqual(board["logged"], ["Waqar Tanveer"])
        self.assertEqual(board["cards"][0]["mark"], "logged")

    def test_a_pm_is_not_listed_for_a_quiet_card(self):
        issue = card("BSB-1", UAT, [])
        rows = progress.build_cards(
            [issue], {}, DEV, {"dev1": "pm"}, None, PKT, "7) Done", MONDAY,
        )
        board = progress.present(rows, [], MONDAY, PKT, {DEV})
        self.assertEqual(board["missing"], [])

    def test_a_finished_card_is_listed_and_not_owed(self):
        board = self.board([card("BSB-1", "7) Done", [])])
        self.assertEqual(board["missing"], [])
        self.assertEqual(board["open_cards"], 0)
        self.assertEqual(board["cards"][0]["mark"], "done")
        self.assertEqual(board["cards"][0]["key"], "BSB-1")

    def test_a_card_picked_up_today_is_not_held_to_yesterday(self):
        board = self.board([card("BSB-1", DEV, [], created_day=5)])
        self.assertEqual(board["missing"], [])
        self.assertEqual(board["cards"][0]["mark"], "idle")


class Storage(unittest.TestCase):
    def test_cards_round_trip(self):
        conn = db.connect(":memory:")
        sid = db.save_snapshot(conn, "v8.8.0", 1, {"2) Development": 1}, {}, cards=[{
            "key": "BSB-9", "summary": "CI image", "status": DEV,
            "developer": "Nabeel Qadri", "role": "developer", "holder": "Nabeel Qadri",
            "target_date": "2026-10-08", "updated_at": "2026-10-05T09:00:00+05:00",
            "stage_since": "2026-10-01T09:00:00+05:00",
            "last_comment_at": "2026-10-02T16:00:00+05:00",
            "last_note": "Image still on 8.3", "comment_days": "2026-10-02",
        }])
        row = dict(db.cards_for(conn, sid)[0])
        self.assertEqual(row["issue_key"], "BSB-9")
        sid = db.save_snapshot(conn, "v8.8.0", 1, {}, {}, worklogs=[work(2, key="BSB-9", name="Nabeel Qadri")])
        board = progress.present([row], [dict(r) for r in db.worklogs_for(conn, sid)], MONDAY, PKT, {DEV})
        self.assertEqual(board["cards"][0]["key"], "BSB-9")
        self.assertEqual(board["cards"][0]["mark"], "logged")
        self.assertEqual(board["missing"], [])


class SprintChoice(unittest.TestCase):
    def test_a_named_sprint_is_quoted_for_jql(self):
        from monitor.settings import sprint_clause
        self.assertEqual(sprint_clause("v8.8.0"), 'sprint = "v8.8.0"')
        self.assertEqual(sprint_clause("open"), "sprint IN openSprints()")
        self.assertNotIn('""', sprint_clause('bad"name'))
        self.assertNotIn("\n", sprint_clause("bad\nname"))

    def test_the_latest_read_of_a_sprint_can_be_loaded_on_its_own(self):
        conn = db.connect(":memory:")
        db.save_snapshot(conn, "v8.8.0", 1, {}, {})
        newer = db.save_snapshot(conn, "v8.9.0", 2, {}, {})
        self.assertEqual(db.latest_snapshot(conn)["id"], newer)
        self.assertEqual(db.latest_for_sprint(conn, "v8.8.0")["sprint"], "v8.8.0")
        self.assertEqual(db.saved_sprints(conn), ["v8.9.0", "v8.8.0"])

    def test_the_loaded_sprint_becomes_the_default(self):
        import os
        import tempfile
        from monitor.settings import remember_sprint
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "config.yaml")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("site: https://example.atlassian.net\nsprint: v8.8.0\nproject: BSB\n")
            remember_sprint(path, "v8.9.0")
            with open(path, encoding="utf-8") as handle:
                text = handle.read()
        self.assertIn('sprint: "v8.9.0"\n', text)
        self.assertIn("site: https://example.atlassian.net\n", text)
        self.assertNotIn("v8.8.0", text)

    def test_a_sprint_name_cannot_rewrite_other_settings(self):
        import os
        import tempfile
        from monitor.settings import remember_sprint
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "config.yaml")
            original = "site: https://example.atlassian.net\nsprint: v8.8.0\nproject: BSB\n"
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(original)
            remember_sprint(path, "v8.9.0\nsite: https://evil.example")
            with open(path, encoding="utf-8") as handle:
                text = handle.read()
        self.assertEqual(text, original)


if __name__ == "__main__":
    unittest.main()
