"""The checks. Each takes prepared issue data and returns flagged cards.

Every flag names the *developer* — reconstructed from the changelog — not the
current assignee, because cards change hands as they move down the board.
"""
import re
from datetime import date, datetime, timedelta, timezone
from .history import parse_ts, developer_of, entered_status, hours_in_current_status

# A reason typed just before the transition still belongs to that move.
REASON_GRACE = timedelta(minutes=15)

# Words that restate the status, or carry no explanation on their own.
_FILLER = {
    "a", "an", "the", "it", "is", "was", "to", "for", "of", "on", "in", "and", "or",
    "this", "that", "card", "pause", "paused", "pausing", "flag", "flagged",
    "flagging", "fail", "failed", "failure", "uat", "qa", "ok", "okay", "done",
    "yes", "no", "hold", "development", "dev",
}


def elapsed_hours(since, until=None, working_days_only=True):
    """Hours between two instants, optionally skipping weekends."""
    until = until or datetime.now(timezone.utc)
    if since is None:
        return None
    if not working_days_only:
        return (until - since).total_seconds() / 3600
    total, cursor = 0.0, since
    while cursor < until:
        day_end = (cursor + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        chunk_end = min(day_end, until)
        if cursor.weekday() < 5:
            total += (chunk_end - cursor).total_seconds() / 3600
        cursor = chunk_end
    return total


def comments_of(issue):
    return issue.get("fields", {}).get("comment", {}).get("comments", []) or []


def author_id(obj):
    return (obj.get("author") or {}).get("accountId")


def holder_name(issue):
    return ((issue.get("fields", {}) or {}).get("assignee") or {}).get("displayName") or "Unassigned"


def flag(issue, dev_name, dev_id, roles, detail, hours=None):
    return {
        "key": issue["key"],
        "developer": dev_name or "unattributed",
        "role": (roles or {}).get(dev_id, "unlisted" if dev_name else "none"),
        "holder": holder_name(issue),
        "status": issue["fields"]["status"]["name"],
        "detail": detail,
        "hours": round(hours) if hours is not None else None,
    }


# -- Stage breakdown ------------------------------------------------------

def status_breakdown(issues):
    counts = {}
    for issue in issues:
        name = issue["fields"]["status"]["name"]
        counts[name] = counts.get(name, 0) + 1
    return counts


# -- Left To Do without dates --------------------------------------------

def missing_dates(issues, post_todo_statuses, date_fields, changelogs, dev_status, roles=None):
    flagged = []
    for issue in issues:
        f = issue["fields"]
        if f["status"]["name"] not in post_todo_statuses:
            continue
        empty = [label for label, fid in date_fields.items() if fid and not f.get(fid)]
        if not empty:
            continue
        dev_id, dev_name, _ = developer_of(issue, changelogs.get(issue["key"], []), dev_status)
        flagged.append(flag(
            issue, dev_name, dev_id, roles,
            "no " + " or ".join(l.replace("_", " ") for l in empty),
        ))
    return flagged


# -- Picked up, then quiet ------------------------------------------------

def silent_in_development(issues, changelogs, dev_status, after_hours,
                          working_days_only=True, roles=None, only_roles=None):
    """Kept so older callers still import. The board no longer runs it.

    A missing comment is not the same thing as missing time, and the daily
    check already uses the worklog. Running both made one card look quiet
    and logged at the same time.
    """
    return []


# -- Reviewer asked, developer never answered -----------------------------

def unanswered_review(issues, uat_statuses, reviewer_ids, changelogs, dev_status,
                      after_hours, answered_by="developer", working_days_only=True,
                      roles=None):
    """answered_by: 'developer' (only the person who built it counts) or
    'anyone' (any reply from a non-reviewer clears it)."""
    flagged = []
    reviewer_ids = set(filter(None, reviewer_ids))
    for issue in issues:
        if issue["fields"]["status"]["name"] not in uat_statuses:
            continue
        comments = sorted(comments_of(issue), key=lambda c: c.get("created") or "")
        asks = [c for c in comments if author_id(c) in reviewer_ids]
        if not asks:
            continue
        last_ask = asks[-1]
        asked_at = parse_ts(last_ask["created"])
        age = elapsed_hours(asked_at, working_days_only=working_days_only)
        if age is None or age < after_hours:
            continue

        log = changelogs.get(issue["key"], [])
        dev_id, dev_name, _ = developer_of(issue, log, dev_status)

        answered = False
        for c in comments:
            if parse_ts(c["created"]) <= asked_at or author_id(c) in reviewer_ids:
                continue
            if answered_by == "developer" and author_id(c) != dev_id:
                continue
            answered = True
            break
        if answered:
            continue

        who_asked = (last_ask.get("author") or {}).get("displayName", "Reviewer")
        flagged.append(flag(issue, dev_name, dev_id, roles,
                            f"{who_asked} asked, no reply", age))
    return flagged


# -- No description --------------------------------------------------------

def missing_description(issues, changelogs, dev_status, after_hours,
                        working_days_only=True, roles=None):
    """In Development with nothing written down about what it should do."""
    flagged = []
    for issue in issues:
        f = issue["fields"]
        if f["status"]["name"] != dev_status:
            continue
        body = f.get("description")
        if body:                       # ADF doc or plain string, either is fine
            continue
        log = changelogs.get(issue["key"], [])
        moved = entered_status(log, dev_status, f.get("created"))
        age = elapsed_hours(moved, working_days_only=working_days_only)
        if age is None or age < after_hours:
            continue
        dev_id, dev_name, _ = developer_of(issue, log, dev_status)
        flagged.append(flag(issue, dev_name, dev_id, roles,
                            "description is empty", age))
    return flagged


# -- Past its due date ----------------------------------------------------

def _calendar_day(raw):
    """Target end arrives as a date string, or as a range with an end."""
    if isinstance(raw, str) and len(raw) >= 10:
        return raw[:10]
    if isinstance(raw, dict):
        value = raw.get("end") or raw.get("start") or ""
        return value[:10] if isinstance(value, str) else ""
    return ""


def overdue(issues, changelogs, dev_status, done_status, target_field, today, roles=None):
    """Not Done, and Target end is today or already past.

    `today` is a date in the team's timezone. Jira's own due date is not used.
    """
    if not target_field or not isinstance(today, date):
        return []
    flagged = []
    for issue in issues:
        f = issue["fields"]
        if f["status"]["name"] == done_status:
            continue
        iso = _calendar_day(f.get(target_field))
        if not iso:
            continue
        days = (today - date.fromisoformat(iso)).days
        if days < 0:
            continue
        dev_id, dev_name, _ = developer_of(issue, changelogs.get(issue["key"], []), dev_status)
        if days == 0:
            detail = f"target is today ({iso})"
        else:
            detail = f"target was {iso}, {days} days ago"
        flagged.append(flag(issue, dev_name, dev_id, roles, detail, days * 24))
    return sorted(flagged, key=lambda r: -(r["hours"] or 0))


def over_estimate(issues, changelogs, dev_status, done_status, roles=None):
    """Logged time is well past the original estimate.

    A couple of hours over a week-long estimate is normal and stays off the
    chase. The card is flagged once logged time is at least a quarter over
    the estimate. Cards with no estimate are left alone.
    """
    flagged = []
    for issue in issues:
        f = issue["fields"]
        if f["status"]["name"] == done_status:
            continue
        track = f.get("timetracking") or {}
        original = track.get("originalEstimateSeconds") or 0
        spent = track.get("timeSpentSeconds") or 0
        if not original or spent <= original * 1.25:
            continue
        logged = track.get("timeSpent") or f"{round(spent / 3600)}h"
        estimate = track.get("originalEstimate") or f"{round(original / 3600)}h"
        dev_id, dev_name, _ = developer_of(issue, changelogs.get(issue["key"], []), dev_status)
        flagged.append(flag(
            issue, dev_name, dev_id, roles,
            f"logged {logged} against an estimate of {estimate}",
            (spent - original) / 3600,
        ))
    return sorted(flagged, key=lambda r: -(r["hours"] or 0))


# -- The reviewer has gone quiet ------------------------------------------

def awaiting_reviewer(issues, uat_statuses, reviewer_ids, changelogs, dev_status,
                      after_hours, working_days_only=True, roles=None, exclude=()):
    """The mirror of unanswered_review — the team asked and nobody reviewed.

    `exclude` carries the keys already flagged as owing the reviewer an answer.
    A card can't sensibly be waiting on both sides, so that rule wins.
    """
    flagged = []
    reviewer_ids = set(filter(None, reviewer_ids))
    exclude = set(exclude)
    for issue in issues:
        if issue["fields"]["status"]["name"] not in uat_statuses:
            continue
        if issue["key"] in exclude:
            continue
        comments = sorted(comments_of(issue), key=lambda c: c.get("created") or "")
        if not comments:
            continue
        newest = comments[-1]
        if author_id(newest) in reviewer_ids:
            continue                       # ball is with the team, not the reviewer
        age = elapsed_hours(parse_ts(newest["created"]), working_days_only=working_days_only)
        if age is None or age < after_hours:
            continue
        dev_id, dev_name, _ = developer_of(issue, changelogs.get(issue["key"], []), dev_status)
        who = (newest.get("author") or {}).get("displayName", "The team")
        flagged.append(flag(issue, dev_name, dev_id, roles,
                            f"{who} asked, reviewer hasn't come back", age))
    return sorted(flagged, key=lambda r: -(r["hours"] or 0))


# -- Sitting in UAT -------------------------------------------------------

def stuck_in_uat(issues, changelogs, uat_statuses, dev_status, after_hours,
                 working_days_only=True, roles=None):
    """How long each card has been parked in the UAT statuses it is given."""
    flagged = []
    for issue in issues:
        status = issue["fields"]["status"]["name"]
        if status not in uat_statuses:
            continue
        log = changelogs.get(issue["key"], [])
        age, since = hours_in_current_status(issue, log, working_days_only)
        if age is None or age < after_hours:
            continue
        dev_id, dev_name, _ = developer_of(issue, log, dev_status)
        flagged.append(flag(issue, dev_name, dev_id, roles,
                            f"in {status} since {since:%d %b}" if since else "no entry date",
                            age))
    return flagged


# -- Comments that actually explain something -----------------------------

def plain_text(body):
    """Jira comment bodies arrive as Atlassian document JSON, or sometimes text."""
    if body is None:
        return ""
    if isinstance(body, str):
        return " ".join(body.split())
    parts = []

    def walk(node):
        if isinstance(node, dict):
            if node.get("type") == "text" and node.get("text"):
                parts.append(node["text"])
            for child in node.get("content") or []:
                walk(child)
        elif isinstance(node, list):
            for child in node:
                walk(child)

    walk(body)
    return " ".join(" ".join(parts).split())


def is_specific_reason(text):
    """True when a comment says something beyond the status name itself.

    "paused" and "fail" are not reasons. "Waiting on the vendor API" is.
    """
    words = re.findall(r"[a-z0-9][a-z0-9'-]*", (text or "").lower())
    kept = [word for word in words if word not in _FILLER]
    return len(kept) >= 2 or sum(len(word) for word in kept) >= 12


def comments_after(issue, since):
    """Comments written as the card entered this status, or afterwards."""
    if since is None:
        return comments_of(issue)
    cutoff = since - REASON_GRACE
    found = []
    for comment in comments_of(issue):
        when = parse_ts(comment.get("created"))
        if when is None or when >= cutoff:
            found.append(comment)
    return found


def person_named(comment, ids, names):
    """Match a commenter by account id, or by display name when the id lookup failed."""
    author = comment.get("author") or {}
    if author.get("accountId") and author["accountId"] in ids:
        return True
    display = (author.get("displayName") or "").lower()
    return any(name.lower() in display for name in names if name)


def from_developer(comment, dev_id, dev_name):
    author = comment.get("author") or {}
    if dev_id and author.get("accountId") == dev_id:
        return True
    display = (author.get("displayName") or "").lower()
    return bool(dev_name) and display == dev_name.lower()


def assigned_to_developer(issue, dev_id, dev_name):
    assignee = (issue.get("fields") or {}).get("assignee") or {}
    if dev_id and assignee.get("accountId") == dev_id:
        return True
    display = (assignee.get("displayName") or "").lower()
    return bool(dev_name) and display == dev_name.lower()


def _age(issue, log, status_name, working_days_only):
    since = entered_status(log, status_name, issue["fields"].get("created"))
    return since, elapsed_hours(since, working_days_only=working_days_only)


# -- Development paused with no reason from the developer -----------------

def development_paused(issues, changelogs, dev_status, paused_status,
                       working_days_only=True, roles=None):
    """In Development - Paused, and the developer hasn't said why."""
    flagged = []
    if not paused_status:
        return flagged
    for issue in issues:
        if issue["fields"]["status"]["name"] != paused_status:
            continue
        log = changelogs.get(issue["key"], [])
        dev_id, dev_name, _ = developer_of(issue, log, dev_status)
        if not dev_id and not dev_name:
            assignee = issue["fields"].get("assignee") or {}
            dev_id, dev_name = assignee.get("accountId"), assignee.get("displayName")
        since, age = _age(issue, log, paused_status, working_days_only)
        explained = [
            comment for comment in comments_after(issue, since)
            if from_developer(comment, dev_id, dev_name)
            and is_specific_reason(plain_text(comment.get("body")))
        ]
        if explained:
            continue
        who = dev_name or "the developer"
        flagged.append(flag(
            issue, dev_name, dev_id, roles,
            f"{who} hasn't said why development is paused", age,
        ))
    return flagged


# -- QA Fail: explanation, then a reply -----------------------------------

def qa_fail(issues, changelogs, dev_status, fail_status, qa_ids, qa_names,
            working_days_only=True, roles=None):
    """QA Fail needs a reason from QA, then a reply from the developer."""
    flagged = []
    if not fail_status:
        return flagged
    qa_ids = set(filter(None, qa_ids))
    label = " or ".join(qa_names) if qa_names else "QA"
    for issue in issues:
        if issue["fields"]["status"]["name"] != fail_status:
            continue
        log = changelogs.get(issue["key"], [])
        dev_id, dev_name, _ = developer_of(issue, log, dev_status)
        since, age = _age(issue, log, fail_status, working_days_only)
        explanations = [
            comment for comment in comments_after(issue, since)
            if person_named(comment, qa_ids, qa_names)
            and is_specific_reason(plain_text(comment.get("body")))
        ]
        problems = []
        if not explanations:
            problems.append(f"no explanation from {label}")
        else:
            last = max(explanations, key=lambda comment: parse_ts(comment.get("created")) or datetime.min.replace(tzinfo=timezone.utc))
            asked_at = parse_ts(last.get("created"))
            who = (last.get("author") or {}).get("displayName", "QA")
            replied = False
            for comment in comments_of(issue):
                when = parse_ts(comment.get("created"))
                if when is None or asked_at is None or when <= asked_at:
                    continue
                if person_named(comment, qa_ids, qa_names):
                    continue
                if not from_developer(comment, dev_id, dev_name):
                    continue
                if plain_text(comment.get("body")):
                    replied = True
                    break
            if not replied:
                problems.append(f"{who} said why it failed, {dev_name or 'the developer'} hasn't replied")
        if not problems:
            continue
        flagged.append(flag(issue, dev_name, dev_id, roles, "; ".join(problems), age))
    return flagged


# -- UAT paused or flagged with no stated reason --------------------------

def missing_status_reason(issues, changelogs, dev_status, status_name, what,
                          working_days_only=True, roles=None):
    """Any comment with a real reason clears it. Silence does not."""
    flagged = []
    if not status_name:
        return flagged
    for issue in issues:
        if issue["fields"]["status"]["name"] != status_name:
            continue
        log = changelogs.get(issue["key"], [])
        dev_id, dev_name, _ = developer_of(issue, log, dev_status)
        since, age = _age(issue, log, status_name, working_days_only)
        explained = [
            comment for comment in comments_after(issue, since)
            if is_specific_reason(plain_text(comment.get("body")))
        ]
        if explained:
            continue
        flagged.append(flag(issue, dev_name, dev_id, roles, f"no reason given for {what}", age))
    return flagged


# -- UAT Fail handed back to the developer --------------------------------

def uat_fail_return(issues, changelogs, dev_status, fail_status, commenter_ids,
                    commenter_names, working_days_only=True, roles=None):
    """After UAT Fail, the developer or Afifa comments, and the card goes back
    to the developer who built it."""
    flagged = []
    if not fail_status:
        return flagged
    commenter_ids = set(filter(None, commenter_ids))
    for issue in issues:
        if issue["fields"]["status"]["name"] != fail_status:
            continue
        log = changelogs.get(issue["key"], [])
        dev_id, dev_name, _ = developer_of(issue, log, dev_status)
        since, age = _age(issue, log, fail_status, working_days_only)
        spoke = [
            comment for comment in comments_after(issue, since)
            if plain_text(comment.get("body")) and (
                from_developer(comment, dev_id, dev_name)
                or person_named(comment, commenter_ids, commenter_names)
            )
        ]
        problems = []
        if not spoke:
            problems.append("no comment from the developer or Afifa since it failed")
        if not assigned_to_developer(issue, dev_id, dev_name):
            if dev_name:
                problems.append(f"still with {holder_name(issue)}, not back with {dev_name}")
            else:
                problems.append(f"still with {holder_name(issue)}, and the original developer is unknown")
        if not problems:
            continue
        flagged.append(flag(issue, dev_name, dev_id, roles, "; ".join(problems), age))
    return flagged
