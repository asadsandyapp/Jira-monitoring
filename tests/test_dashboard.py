"""The dashboard ranks a real handoff above a missing date."""
import unittest
from monitor.web import assemble


def flag(key, rule, detail, developer="Waqar Tanveer", hours=10, holder=None, status="3) QA"):
    return {
        "issue_key": key, "rule": rule, "detail": detail,
        "developer": developer, "role": "developer",
        "holder": holder or developer, "status": status, "hours": hours,
    }


LADDER = [
    {"key": "todo", "name": "1) To Do", "count": 4},
    {"key": "development", "name": "2) Development", "count": 2},
    {"key": "uat_pending", "name": "5) UAT - Pending", "count": 9},
    {"key": "done", "name": "7) Done", "count": 12},
]


class Assemble(unittest.TestCase):
    def test_missing_dates_stay_out_of_the_chase(self):
        view = assemble([
            flag("BSB-1", "missing_dates", "no start date"),
            flag("BSB-2", "qa_fail", "no explanation from Afifa", hours=20, holder="Afifa Cheema"),
        ], LADDER)
        self.assertEqual([card["key"] for card in view["chase"]], ["BSB-2"])
        self.assertEqual([card["key"] for card in view["paperwork"]], ["BSB-1"])
        self.assertEqual(view["kpis"][0]["value"], "1")
        self.assertEqual(view["kpis"][0]["note"], "1 QA fail")

    def test_a_real_problem_is_the_headline_when_dates_are_also_missing(self):
        view = assemble([
            flag("BSB-3", "missing_dates", "no start date", hours=5),
            flag("BSB-3", "uat_fail_return", "still with Celeste", hours=30, holder="Celeste Rush"),
        ], LADDER)
        card = view["chase"][0]
        self.assertEqual(card["title"], "UAT fail not handed back")
        self.assertIn("no dates", card["also"])
        self.assertEqual(view["paperwork"], [])
        self.assertTrue(card["show_holder"])

    def test_the_worse_problem_sorts_first(self):
        view = assemble([
            flag("BSB-4", "overdue", "target was 2026-10-01, 8 days ago", hours=100),
            flag("BSB-5", "qa_fail", "no explanation", hours=2),
        ], LADDER)
        self.assertEqual([card["key"] for card in view["chase"]], ["BSB-5", "BSB-4"])

    def test_a_missing_comment_is_not_shown_as_a_chase(self):
        view = assemble([
            flag("BSB-4", "silent_in_development", "no comment", hours=100),
        ], LADDER)
        self.assertEqual(view["chase"], [])

    def test_pile_ignores_to_do_and_done(self):
        view = assemble([], LADDER)
        self.assertEqual(view["kpis"][2]["value"], "9")
        self.assertEqual(view["kpis"][2]["note"], "5) UAT - Pending")
        self.assertEqual(view["kpis"][0]["value"], "0")
        self.assertFalse(view["kpis"][0]["hot"])

    def test_a_tied_pile_prefers_the_later_stage(self):
        ladder = [
            {"key": "development", "name": "2) Development", "count": 4},
            {"key": "qa", "name": "3) QA", "count": 4},
        ]
        view = assemble([], ladder)
        self.assertEqual(view["kpis"][2]["note"], "3) QA")

    def test_talk_to_lists_the_headline_only(self):
        view = assemble([
            flag("BSB-6", "qa_fail", "no explanation", developer="Waqar Tanveer"),
            flag("BSB-6", "missing_dates", "no start date", developer="Waqar Tanveer"),
            flag("BSB-7", "development_paused", "hasn't said why", developer="Waqar Tanveer"),
        ], LADDER)
        self.assertEqual(view["talk"][0]["count"], 2)
        self.assertEqual(view["talk"][0]["problems"], "QA fail, dev pause")


class NarrowToDeveloper(unittest.TestCase):
    def test_a_name_keeps_only_that_builder(self):
        from monitor.web import narrow_to, developers_in
        cards = [
            {"developer": "Waqar Tanveer", "role": "developer", "issue_key": "BSB-1"},
            {"developer": "Rana Asad", "role": "developer", "issue_key": "BSB-2"},
        ]
        flags = [
            {"developer": "Waqar Tanveer", "issue_key": "BSB-1"},
            {"developer": "Rana Asad", "issue_key": "BSB-2"},
        ]
        kept_cards, kept_flags = narrow_to(cards, flags, "Waqar Tanveer")
        self.assertEqual([card["issue_key"] for card in kept_cards], ["BSB-1"])
        self.assertEqual([flag["issue_key"] for flag in kept_flags], ["BSB-1"])
        self.assertEqual(
            [person["name"] for person in developers_in(cards)],
            ["Rana Asad", "Waqar Tanveer"],
        )

    def test_an_empty_name_leaves_the_sprint_whole(self):
        from monitor.web import narrow_to
        cards = [{"developer": "Waqar Tanveer"}]
        self.assertEqual(narrow_to(cards, [], "")[0], cards)


class MissingCredentials(unittest.TestCase):
    def test_the_board_opens_before_the_token_is_added(self):
        import os
        from unittest.mock import patch
        from monitor import web

        with patch.dict(os.environ, {"JIRA_EMAIL": "", "JIRA_API_TOKEN": ""}):
            app = web.create_app("config.example.yaml")
        client = app.test_client()
        response = client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Add the Jira token", response.data)
        self.assertNotIn(b"JIRA_API_TOKEN=", response.data)


class LoadSprint(unittest.TestCase):
    def _post(self, data, steps):
        from monitor import web
        seen = {}

        def fake_run(path, verbose=True, sprint=None, on_progress=None):
            seen["sprint"] = sprint
            for pct, label in steps:
                if on_progress:
                    on_progress(pct, label)

        original = web.collect.run
        web.collect.run = fake_run
        try:
            app = web.create_app()
            app.config["TESTING"] = True
            client = app.test_client()
            response = client.post("/sprint", data=data)
            app.job_thread.join(2)
            progress = client.get("/sprint/progress").get_json()
        finally:
            web.collect.run = original
        return seen, response, progress

    def test_a_typed_name_opens_the_loader_and_finishes(self):
        seen, response, progress = self._post({"typed": "v8.9.0"}, [(40, "Reading history"), (100, "Done")])
        self.assertEqual(response.status_code, 302)
        self.assertIn("/loading", response.headers["Location"])
        self.assertIn("sprint=v8.9.0", response.headers["Location"])
        self.assertEqual(seen["sprint"], "v8.9.0")
        self.assertTrue(progress["done"])
        self.assertEqual(progress["percent"], 100)
        self.assertIn("sprint=v8.9.0", progress["url"])

    def test_a_picked_name_is_used_when_the_box_is_empty(self):
        seen, response, progress = self._post({"picked": "v8.8.0"}, [])
        self.assertEqual(response.status_code, 302)
        self.assertEqual(seen["sprint"], "v8.8.0")
        self.assertTrue(progress["done"])


if __name__ == "__main__":
    unittest.main()
