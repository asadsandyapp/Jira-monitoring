"""Read a card's real story out of its changelog.

The assignee field only tells you who holds a card *now* — it moves to QA at
QA, and to the reviewer at UAT. To know who actually built something you have
to reconstruct who held it while it sat in Development.
"""
from datetime import datetime, timezone

JIRA_TS = "%Y-%m-%dT%H:%M:%S.%f%z"


def parse_ts(value):
    if not value:
        return None
    try:
        return datetime.strptime(value, JIRA_TS)
    except ValueError:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))


def status_changes(changelog):
    """[(when, from_status, to_status, author_id, author_name)] oldest first."""
    out = []
    for entry in changelog:
        author = entry.get("author") or {}
        for item in entry.get("items", []):
            if item.get("field") == "status":
                out.append((
                    parse_ts(entry.get("created")),
                    item.get("fromString"), item.get("toString"),
                    author.get("accountId"), author.get("displayName"),
                ))
    return sorted(out, key=lambda c: c[0] or datetime.min.replace(tzinfo=timezone.utc))


def assignee_changes(changelog):
    """[(when, from_id, from_name, to_id, to_name)] oldest first."""
    out = []
    for entry in changelog:
        for item in entry.get("items", []):
            if item.get("field") == "assignee":
                out.append((
                    parse_ts(entry.get("created")),
                    item.get("from"), item.get("fromString"),
                    item.get("to"), item.get("toString"),
                ))
    return sorted(out, key=lambda c: c[0] or datetime.min.replace(tzinfo=timezone.utc))


def assignee_at(changelog, when, current_id, current_name, inclusive=True):
    """Who held the card at a given moment.

    inclusive=False means "just before". Jira writes a status change and the
    reassignment that goes with it into one changelog entry with one timestamp,
    so asking who held a card as it left Development has to exclude the handover
    happening in that same breath — otherwise you get the person it went to.
    """
    changes = assignee_changes(changelog)
    if not changes:
        return current_id, current_name
    before = [c for c in changes if c[0] and (c[0] <= when if inclusive else c[0] < when)]
    if before:
        return before[-1][3], before[-1][4]
    return changes[0][1], changes[0][2]   # whoever held it before the first change


def entered_status(changelog, status_name, created=None):
    """When the card most recently entered this status."""
    stamps = [c[0] for c in status_changes(changelog) if c[2] == status_name]
    if stamps:
        return stamps[-1]
    return parse_ts(created)   # never transitioned — it was created here


def left_status(changelog, status_name, since):
    """When it moved on from that status, or None if it's still there."""
    for when, frm, _to, _a, _n in status_changes(changelog):
        if frm == status_name and when and when > since:
            return when
    return None


def developer_of(issue, changelog, dev_status):
    """Who built this card.

    Preference order: whoever was assigned when it left Development (that's the
    person who finished it), then whoever was assigned when it entered, then
    whoever dragged it across the board.
    """
    fields = issue.get("fields", {}) or {}
    current = fields.get("assignee") or {}
    cur_id, cur_name = current.get("accountId"), current.get("displayName")

    entered = entered_status(changelog, dev_status)
    if not entered:
        return None, None, "never reached development"

    moved_on = left_status(changelog, dev_status, entered)
    who_id, who_name = assignee_at(
        changelog, moved_on or datetime.now(timezone.utc), cur_id, cur_name,
        inclusive=moved_on is None,
    )
    if who_id:
        return who_id, who_name, "held it in development"

    who_id, who_name = assignee_at(changelog, entered, cur_id, cur_name)
    if who_id:
        return who_id, who_name, "assigned when work started"

    for when, _frm, to, aid, aname in status_changes(changelog):
        if to == dev_status and when == entered and aid:
            return aid, aname, "moved it to development"
    return None, None, "nobody assigned"


def hours_in_current_status(issue, changelog, working_days_only=True):
    from .rules import elapsed_hours
    status = issue["fields"]["status"]["name"]
    since = entered_status(changelog, status, issue["fields"].get("created"))
    return elapsed_hours(since, working_days_only=working_days_only), since
