"""Per-card progress and the daily log check.

A log is time entered in Jira (Log time / worklog), the same entries as the
worklog report. A comment does not count. "Yesterday" is the previous working
day in the team's timezone, so a Monday morning looks at Friday.
"""
from datetime import datetime, timedelta, timezone

from .history import developer_of, entered_status, parse_ts
from .rules import comments_of, holder_name

# Statuses where the builder still owes a daily note on that card.
LOG_KEYS = ("development", "development_paused", "qa_fail")
# People we hold to a daily worklog while any of their cards are still open.
TRACKED_ROLES = {"developer", "devops", "automation", "unlisted"}


def previous_working_day(now, tz, working_days_only=True):
    """The date a morning check should hold people to."""
    day = now.astimezone(tz).date() - timedelta(days=1)
    if working_days_only:
        while day.weekday() >= 5:
            day -= timedelta(days=1)
    return day


def plain_text(body, limit=180):
    """First line of a Jira comment, whether it is plain text or ADF."""
    if body is None:
        return ""
    if isinstance(body, str):
        text = body
    else:
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
        text = " ".join(parts)
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    return text[: limit - 3].rstrip() + "..."


def _date_value(raw):
    if isinstance(raw, str) and raw:
        return raw[:10]
    if isinstance(raw, dict):
        value = raw.get("start") or raw.get("end") or ""
        return value[:10]
    return ""


def _by_builder(comment, dev_id, dev_name):
    author = comment.get("author") or {}
    if dev_id and author.get("accountId") == dev_id:
        return True
    return bool(dev_name) and author.get("displayName") == dev_name


def build_cards(issues, changelogs, dev_status, roles, target_field, tz, done_status, now=None, name_roles=None):
    """One row per parent card, including Done. Comment days are the builder's, in `tz`."""
    now = now or datetime.now(timezone.utc)
    cutoff = now.astimezone(tz) - timedelta(days=45)
    rows = []
    for issue in issues:
        fields = issue.get("fields") or {}
        status = (fields.get("status") or {}).get("name") or ""
        if not status:
            continue
        log = changelogs.get(issue["key"], [])
        dev_id, dev_name, _why = developer_of(issue, log, dev_status)
        assignee = fields.get("assignee") or {}
        if not dev_name:
            dev_id = assignee.get("accountId")
            dev_name = assignee.get("displayName") or "Unassigned"
        notes = []
        for comment in comments_of(issue):
            if not _by_builder(comment, dev_id, dev_name):
                continue
            when = parse_ts(comment.get("created"))
            if when:
                notes.append((when, comment))
        notes.sort(key=lambda pair: pair[0])
        last_at, last_note = "", ""
        days = []
        if notes:
            last_when, last_comment = notes[-1]
            last_at = last_when.isoformat()
            last_note = plain_text(last_comment.get("body"))
            days = sorted({
                when.astimezone(tz).date().isoformat()
                for when, _comment in notes
                if when.astimezone(tz) >= cutoff
            })
        since = entered_status(log, status, fields.get("created"))
        rows.append({
            "key": issue["key"],
            "summary": fields.get("summary") or "",
            "status": status,
            "developer": dev_name or "Unassigned",
            "role": (roles or {}).get(dev_id) or (name_roles or {}).get(dev_name) or ("unlisted" if dev_name else "none"),
            "holder": holder_name(issue),
            "target_date": _date_value(fields.get(target_field)) if target_field else "",
            "updated_at": fields.get("updated") or "",
            "stage_since": since.isoformat() if since else "",
            "last_comment_at": last_at,
            "last_note": last_note,
            "comment_days": ",".join(days),
        })
    return rows


def _fmt_when(value, tz, empty):
    stamp = parse_ts(value) if value else None
    if not stamp:
        return empty
    local = stamp.astimezone(tz)
    return f"{local.day} {local.strftime('%b')}, {local.strftime('%H:%M')}"


def _fmt_date(iso):
    if not iso:
        return "—"
    try:
        day = datetime.fromisoformat(iso[:10]).date()
    except ValueError:
        return iso[:10]
    return f"{day.day} {day.strftime('%b')}"


def _work_entries(worklogs, tz):
    """Group Jira worklogs by author, and by author plus issue."""
    by_person, by_card = {}, {}
    for raw in worklogs or []:
        started = parse_ts(raw.get("started"))
        author = (raw.get("author") or "").strip()
        key = raw.get("issue_key") or raw.get("key") or ""
        if not started or not author:
            continue
        entry = {
            "author": author,
            "issue_key": key,
            "started": started.isoformat(),
            "day": started.astimezone(tz).date().isoformat(),
            "time_spent": raw.get("time_spent") or "",
        }
        by_person.setdefault(author.casefold(), []).append(entry)
        by_card.setdefault((author.casefold(), key), []).append(entry)
    return by_person, by_card


def _latest(entries):
    if not entries:
        return None
    return max(entries, key=lambda entry: entry["started"])


def _spent_note(entry):
    if not entry:
        return ""
    spent = entry.get("time_spent") or "time"
    key = entry.get("issue_key") or ""
    return f"{spent} on {key}" if key else spent


def present(cards, worklogs, now, tz, log_statuses, working_days_only=True, done_status=None):
    """Mark each card and list the people who logged no time yesterday.

    Time is a Jira worklog. It counts for the person on the day it was
    started, on any issue in the project, which is how the worklog report
    totals a day.
    """
    yesterday = previous_working_day(now, tz, working_days_only)
    yiso = yesterday.isoformat()
    by_person, by_card = _work_entries(worklogs, tz)
    annotated = []
    for raw in cards:
        card = dict(raw)
        card["key"] = card.get("key") or card.get("issue_key")
        author = (card.get("developer") or "").strip().casefold()
        entries = by_card.get((author, card["key"]), [])
        latest = _latest(entries)
        since = parse_ts(card.get("stage_since"))
        closed = bool(done_status) and card.get("status") == done_status
        expects = (not closed) and card.get("status") in log_statuses
        if expects and since and since.astimezone(tz).date() > yesterday:
            expects = False
        logged = any(entry["day"] == yiso for entry in entries)
        if closed:
            mark = "done"
        elif logged:
            mark = "logged"
        elif expects:
            mark = "missed"
        else:
            mark = "idle"
        card.update({
            "expects": expects,
            "logged": logged,
            "mark": mark,
            "when": _fmt_when(latest["started"] if latest else "", tz, "No time logged"),
            "updated": _fmt_when(card.get("updated_at"), tz, "—"),
            "target_label": _fmt_date(card.get("target_date")),
            "note": _spent_note(latest),
        })
        annotated.append(card)

    annotated.sort(key=lambda card: (
        0 if card["mark"] == "missed" else 1,
        card.get("developer") or "",
        card.get("key") or "",
    ))

    people = {}
    for card in annotated:
        since = parse_ts(card.get("stage_since"))
        closed = card.get("mark") == "done"
        held_then = (not closed) and (since is None or since.astimezone(tz).date() <= yesterday)
        rec = people.setdefault(card["developer"], {
            "developer": card["developer"],
            "role": card.get("role") or "unlisted",
            "silent": [],
            "logged_any": False,
            "expects": False,
            "held_then": False,
            "last_at": "",
            "last_note": "",
        })
        if card.get("role") and card["role"] not in ("unlisted", "none"):
            rec["role"] = card["role"]
        if held_then:
            rec["held_then"] = True
        if card["expects"]:
            rec["expects"] = True
            if not card["logged"]:
                rec["silent"].append(card)
        elif held_then and not card["logged"]:
            rec["silent"].append(card)
        person_logs = by_person.get((card.get("developer") or "").strip().casefold(), [])
        if any(entry["day"] == yiso for entry in person_logs):
            rec["logged_any"] = True
        latest = _latest(person_logs)
        if latest and latest["started"] > (rec["last_at"] or ""):
            rec["last_at"] = latest["started"]
            rec["last_note"] = _spent_note(latest)

    def _tracked(rec):
        if not rec["held_then"]:
            return False
        if rec["role"] in TRACKED_ROLES:
            return True
        return rec["expects"]

    missing = [rec for rec in people.values() if _tracked(rec) and not rec["logged_any"]]
    for rec in missing:
        rec["when"] = _fmt_when(rec["last_at"], tz, "No time logged")
        rec["note"] = rec["last_note"] or "No time logged in Jira"
    missing.sort(key=lambda rec: (-len(rec["silent"]), rec["developer"]))

    due = {rec["developer"] for rec in people.values() if _tracked(rec)}
    logged = sorted(rec["developer"] for rec in people.values() if _tracked(rec) and rec["logged_any"])
    return {
        "yesterday": yesterday,
        "yesterday_label": f"{yesterday.strftime('%A')} {yesterday.day} {yesterday.strftime('%b')}",
        "cards": annotated,
        "missing": missing,
        "logged": logged,
        "due_people": len(due),
        "open_cards": sum(1 for card in annotated if card["mark"] != "done"),
    }
