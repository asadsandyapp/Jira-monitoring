"""The pause, fail, and flag checks, against made-up cards."""
import unittest
from datetime import date
from monitor import rules

DEV = "2) Development"
PAUSED = "2.1) Development - Paused"
QA_FAIL = "3.1) QA Fail"
UAT_PAUSE = "5.2) UAT - Paused"
UAT_FLAG = "5.3) UAT - Flagged"
UAT_FAIL = "5.4) UAT - Fail"

WAQAR = {"accountId": "dev1", "displayName": "Waqar Tanveer"}
CELESTE = {"accountId": "rev1", "displayName": "Celeste Rush"}


def ts(day, hour=9, minute=0):
    return f"2026-10-{day:02d}T{hour:02d}:{minute:02d}:00.000+0000"


def comment(day, hour, name, account, text, minute=0):
    return {
        "created": ts(day, hour, minute),
        "author": {"displayName": name, "accountId": account},
        "body": {"type": "doc", "content": [
            {"type": "paragraph", "content": [{"type": "text", "text": text}]},
        ]},
    }


def history(left_to):
    return [
        {"created": ts(1), "author": WAQAR, "items": [
            {"field": "status", "fromString": "1) To Do", "toString": DEV},
        ]},
        {"created": ts(1, 10), "author": WAQAR, "items": [
            {"field": "assignee", "from": None, "fromString": None,
             "to": "dev1", "toString": "Waqar Tanveer"},
        ]},
        {"created": ts(2), "author": WAQAR, "items": [
            {"field": "status", "fromString": DEV, "toString": left_to},
        ]},
    ]


def issue(key, status, comments, assignee=None):
    return {
        "key": key,
        "fields": {
            "status": {"name": status},
            "assignee": assignee or WAQAR,
            "comment": {"comments": comments},
            "created": ts(1, 8),
        },
    }


class ReasonText(unittest.TestCase):
    def test_status_word_alone_is_not_a_reason(self):
        self.assertFalse(rules.is_specific_reason("paused"))
        self.assertFalse(rules.is_specific_reason("UAT fail"))

    def test_a_real_explanation_is_a_reason(self):
        self.assertTrue(rules.is_specific_reason("Waiting on the vendor API"))
        self.assertTrue(rules.is_specific_reason("API down"))


class DevelopmentPaused(unittest.TestCase):
    def check(self, comments, assignee=None):
        card = issue("BSB-1", PAUSED, comments, assignee)
        return rules.development_paused([card], {"BSB-1": history(PAUSED)}, DEV, PAUSED)

    def test_developer_reason_clears_it(self):
        found = self.check([comment(2, 10, "Waqar Tanveer", "dev1", "Waiting on the vendor API")])
        self.assertEqual(found, [])

    def test_the_word_paused_does_not_clear_it(self):
        found = self.check([comment(2, 10, "Waqar Tanveer", "dev1", "paused")])
        self.assertEqual(len(found), 1)
        self.assertIn("hasn't said why", found[0]["detail"])

    def test_someone_elses_reason_does_not_clear_it(self):
        found = self.check([
            comment(2, 10, "Afifa Cheema", "pm1", "Holding this until the design is signed off"),
        ])
        self.assertEqual(len(found), 1)

    def test_a_reason_from_before_the_pause_does_not_count(self):
        found = self.check([comment(1, 11, "Waqar Tanveer", "dev1", "Starting the login work today")])
        self.assertEqual(len(found), 1)

    def test_a_reason_typed_just_before_the_move_counts(self):
        found = self.check([comment(2, 8, "Waqar Tanveer", "dev1", "Blocked on the vendor contract", minute=50)])
        self.assertEqual(found, [])


class QaFail(unittest.TestCase):
    def check(self, comments, assignee=None):
        card = issue("BSB-2", QA_FAIL, comments, assignee)
        return rules.qa_fail(
            [card], {"BSB-2": history(QA_FAIL)}, DEV, QA_FAIL,
            [], ["Afifa Cheema", "Mamoon"],
        )

    def test_explanation_and_reply_clear_it(self):
        found = self.check([
            comment(2, 10, "Mamoon Ali", "qa2", "Login breaks when the token expires"),
            comment(2, 12, "Waqar Tanveer", "dev1", "Looking at the token refresh"),
        ])
        self.assertEqual(found, [])

    def test_explanation_without_a_reply_is_flagged(self):
        found = self.check([
            comment(2, 10, "Afifa Cheema", "pm1", "The empty state never renders"),
        ])
        self.assertEqual(len(found), 1)
        self.assertIn("hasn't replied", found[0]["detail"])

    def test_no_qa_comment_is_flagged(self):
        found = self.check([])
        self.assertIn("no explanation from Afifa Cheema or Mamoon", found[0]["detail"])

    def test_the_word_fail_is_not_an_explanation(self):
        found = self.check([comment(2, 10, "Mamoon Ali", "qa2", "fail")])
        self.assertIn("no explanation", found[0]["detail"])


class UatPauseAndFlag(unittest.TestCase):
    def test_a_reason_clears_a_pause(self):
        card = issue("BSB-3", UAT_PAUSE, [
            comment(2, 11, "Celeste Rush", "rev1", "Customer asked to hold until the audit"),
        ])
        found = rules.missing_status_reason(
            [card], {"BSB-3": history(UAT_PAUSE)}, DEV, UAT_PAUSE, "the UAT pause",
        )
        self.assertEqual(found, [])

    def test_paused_alone_does_not_clear_it(self):
        card = issue("BSB-3", UAT_PAUSE, [comment(2, 11, "Celeste Rush", "rev1", "paused")])
        found = rules.missing_status_reason(
            [card], {"BSB-3": history(UAT_PAUSE)}, DEV, UAT_PAUSE, "the UAT pause",
        )
        self.assertIn("no reason given for the UAT pause", found[0]["detail"])

    def test_a_flag_with_no_comment_is_flagged(self):
        card = issue("BSB-4", UAT_FLAG, [])
        found = rules.missing_status_reason(
            [card], {"BSB-4": history(UAT_FLAG)}, DEV, UAT_FLAG, "the UAT flag",
        )
        self.assertIn("no reason given for the UAT flag", found[0]["detail"])


class UatFailReturn(unittest.TestCase):
    def check(self, comments, assignee):
        card = issue("BSB-5", UAT_FAIL, comments, assignee)
        return rules.uat_fail_return(
            [card], {"BSB-5": history(UAT_FAIL)}, DEV, UAT_FAIL,
            [], ["Afifa Cheema"],
        )

    def test_afifa_comment_and_back_with_the_developer_clears_it(self):
        found = self.check([
            comment(2, 12, "Afifa Cheema", "pm1", "Sending this back"),
        ], WAQAR)
        self.assertEqual(found, [])

    def test_a_comment_while_it_is_still_with_the_reviewer_is_flagged(self):
        found = self.check([
            comment(2, 12, "Waqar Tanveer", "dev1", "I can take this back"),
        ], CELESTE)
        self.assertEqual(len(found), 1)
        self.assertIn("not back with Waqar Tanveer", found[0]["detail"])
        self.assertNotIn("no comment", found[0]["detail"])

    def test_back_with_the_developer_but_silent_is_flagged(self):
        found = self.check([], WAQAR)
        self.assertIn("no comment from the developer or Afifa", found[0]["detail"])
        self.assertNotIn("not back with", found[0]["detail"])

    def test_neither_comment_nor_reassignment(self):
        found = self.check([], CELESTE)
        self.assertIn("no comment from the developer or Afifa", found[0]["detail"])
        self.assertIn("still with Celeste Rush", found[0]["detail"])


class TargetAndEstimate(unittest.TestCase):
    def test_a_reached_target_is_flagged_and_a_future_one_is_not(self):
        today = date(2026, 10, 9)
        late = issue("BSB-1", DEV, [])
        late["fields"]["customfield_1"] = "2026-10-02"
        future = issue("BSB-2", DEV, [])
        future["fields"]["customfield_1"] = "2026-10-20"
        done = issue("BSB-3", "7) Done", [])
        done["fields"]["customfield_1"] = "2026-10-01"
        due_today = issue("BSB-4", DEV, [])
        due_today["fields"]["customfield_1"] = {"end": "2026-10-09"}
        blank = issue("BSB-5", DEV, [])
        found = rules.overdue(
            [late, future, done, due_today, blank], {}, DEV, "7) Done",
            "customfield_1", today,
        )
        by_key = {row["key"]: row["detail"] for row in found}
        self.assertEqual(set(by_key), {"BSB-1", "BSB-4"})
        self.assertIn("7 days ago", by_key["BSB-1"])
        self.assertIn("today", by_key["BSB-4"])

    def test_logged_time_past_the_estimate_is_flagged(self):
        over = issue("BSB-1", DEV, [])
        over["fields"]["timetracking"] = {
            "originalEstimate": "1d", "timeSpent": "2d",
            "originalEstimateSeconds": 28800, "timeSpentSeconds": 57600,
        }
        inside = issue("BSB-2", DEV, [])
        inside["fields"]["timetracking"] = {
            "originalEstimate": "2d", "timeSpent": "1d",
            "originalEstimateSeconds": 57600, "timeSpentSeconds": 28800,
        }
        slight = issue("BSB-4", DEV, [])
        slight["fields"]["timetracking"] = {
            "originalEstimate": "2w", "timeSpent": "2w 2h",
            "originalEstimateSeconds": 288000, "timeSpentSeconds": 295200,
        }
        bare = issue("BSB-3", DEV, [])
        found = rules.over_estimate([over, inside, slight, bare], {}, DEV, "7) Done")
        self.assertEqual([row["key"] for row in found], ["BSB-1"])
        self.assertIn("2d", found[0]["detail"])
        self.assertIn("1d", found[0]["detail"])


if __name__ == "__main__":
    unittest.main()
