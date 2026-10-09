"""Take one snapshot of the open sprint. Run this on a schedule."""
import sys
from datetime import datetime, timedelta, timezone
from . import db, progress, rules, settings
from .history import parse_ts
from .jira import Jira, JiraError


def account_ids(jira, names, verbose):
    """Resolve display names to account ids. A miss still leaves the name, so a
    comment can be matched on the author even when user search is blocked."""
    ids = []
    for name in names:
        try:
            ids.append(jira.account_id(name))
        except JiraError as e:
            if verbose:
                print(f"  ! {e} — comments will be matched on the name '{name}'")
    return ids


def fill_comments(jira, issues, statuses, verbose, always=None):
    """Pull the full thread for cards whose pause or fail reason may sit past
    the first page of comments that search returns.

    `always` forces a full fetch. Search embeds the oldest page, so a daily
    log written later in the thread is invisible until we ask for the rest.
    """
    always = {name for name in (always or []) if name}
    wanted = {name for name in statuses if name} | always
    fetched = 0
    for issue in issues:
        status = issue["fields"]["status"]["name"]
        if status not in wanted:
            continue
        block = issue["fields"].get("comment") or {}
        have = block.get("comments") or []
        total = block.get("total")
        force = status in always
        if not force:
            if total is not None and total <= len(have):
                continue
            if total is None and have:
                continue
        issue.setdefault("fields", {}).setdefault("comment", {})
        issue["fields"]["comment"]["comments"] = jira.comments(issue["key"])
        fetched += 1
    if verbose and fetched:
        print(f"  fetched full comments for {fetched} cards")


def _between(start, end, done, total):
    if total <= 0:
        return end
    return start + (end - start) * min(done, total) / total


def gather_worklogs(jira, project, tz, now, verbose, on_each=None):
    """Time entries for the project, same source as Jira's worklog report.

    Includes every issue type. A worklog on a sub-task still counts for the
    person, which is how the report totals a day.
    """
    since = (now.astimezone(tz) - timedelta(days=45)).date()
    started_after = int((now - timedelta(days=45)).timestamp() * 1000)
    found = jira.search(
        f'project = "{project}" AND worklogDate >= "{since.isoformat()}"',
        ["worklog"],
    )
    rows, fetched = [], 0
    issue_count = len(found)
    for index, issue in enumerate(found, start=1):
        block = (issue.get("fields") or {}).get("worklog") or {}
        logs = block.get("worklogs") or []
        logged = block.get("total")
        if logged is None or logged > len(logs):
            logs = jira.worklogs(issue["key"], started_after)
            fetched += 1
        for entry in logs:
            started = parse_ts(entry.get("started"))
            if not started or started.astimezone(tz).date() < since:
                continue
            author = entry.get("author") or {}
            rows.append({
                "issue_key": issue["key"],
                "author": author.get("displayName") or "",
                "author_id": author.get("accountId") or "",
                "started": entry.get("started") or "",
                "time_spent": entry.get("timeSpent") or "",
                "seconds": entry.get("timeSpentSeconds") or 0,
            })
        if on_each:
            on_each(index, issue_count)
    if verbose:
        print(f"  worklogs: {len(rows)} on {len(found)} cards"
              f" ({fetched} needed a full read)")
    return rows


def sprint_name(issues, sprint_field):
    for issue in issues:
        value = issue["fields"].get(sprint_field)
        if isinstance(value, list) and value:
            active = [s for s in value if s.get("state") == "active"]
            return (active or value)[-1].get("name")
    return "Open sprint"


def run(config_path="config.yaml", verbose=True, sprint=None, on_progress=None):
    cfg = settings.load(config_path)
    if not cfg.credentials_ready:
        raise settings.ConfigError(
            "Add JIRA_EMAIL and JIRA_API_TOKEN to .env, then start the dashboard again."
        )
    chosen = (sprint or "").strip()
    if chosen:
        cfg.sprint = chosen

    def report(pct, label):
        if on_progress:
            on_progress(int(pct), label)

    report(4, "Connecting to Jira")
    jira = Jira(cfg.site, cfg.email, cfg.token)
    jira.check_auth()
    report(8, "Connected")

    sprint_field = jira.field_id(cfg.field_names.get("sprint", "Sprint"))
    date_fields = {
        label: jira.field_id(cfg.field_names.get(label))
        for label in ("start_date", "target_date")
    }
    target_field = date_fields.get("target_date")
    for label, fid in date_fields.items():
        if fid is None and verbose:
            print(f"  ! no Jira field named '{cfg.field_names.get(label)}' — skipping the {label} check")

    required = cfg.rules.get("missing_dates", {}).get("require", ["start_date", "target_date"])
    date_fields = {k: v for k, v in date_fields.items() if k in required}

    fields = ["summary", "status", "assignee", "comment", "updated", "created",
              "description", "timetracking"]
    fields += [f for f in [sprint_field, target_field, *date_fields.values()] if f and f not in fields]

    report(12, "Finding cards")
    issues = jira.search(
        cfg.sprint_jql(), fields, expand="changelog",
        on_page=lambda count: report(min(22, 12 + count // 20), f"Finding cards, {count} so far"),
    )
    report(24, f"Found {len(issues)} cards")
    if not issues and cfg.sprint not in (None, "", "open"):
        raise JiraError(
            f"No cards found in sprint '{cfg.sprint}'. "
            "The name has to match Jira exactly, for example v8.8.0."
        )
    if verbose:
        print(f"  {len(issues)} cards"
              + ("" if cfg.include_subtasks else " (sub-tasks excluded)"))
    open_sprints = {
        s.get("name") for i in issues for s in (i["fields"].get(sprint_field) or [])
        if s.get("state") == "active"
    }
    if verbose and len(open_sprints) > 1:
        print(f"  ! {len(open_sprints)} sprints are open at once: {', '.join(sorted(open_sprints))}")
        print("    Set 'sprint:' in config.yaml to the one you mean, and close the rest in Jira.")

    # History is needed for every card now: it's the only way to tell who
    # actually built something once the card has been handed to QA or UAT.
    dev_status = cfg.status("development")
    changelogs = jira.changelogs_for(
        issues,
        on_each=lambda done, total: report(
            _between(24, 58, done, total),
            f"Reading history, {done} of {total}",
        ),
    )
    report(58, "Reading comments")
    if verbose:
        print(f"  history read for {len(changelogs)} cards "
              f"({getattr(jira, 'last_changelog_fetches', 0)} needed a separate call)")

    review_cfg = cfg.rules.get("unanswered_review", {})
    reviewer_ids = account_ids(jira, review_cfg.get("reviewers", []), verbose)
    qa_names = cfg.rules.get("qa_fail", {}).get("qa", ["Afifa Cheema", "Mamoon"])
    qa_ids = account_ids(jira, qa_names, verbose)
    fail_names = cfg.rules.get("uat_fail_return", {}).get("commenters", ["Afifa Cheema"])
    fail_ids = account_ids(jira, fail_names, verbose)

    comment_statuses = [
        cfg.workflow.get("development_paused"),
        cfg.workflow.get("qa_fail"),
        cfg.workflow.get("uat_paused"),
        cfg.workflow.get("uat_flagged"),
        cfg.workflow.get("uat_fail"),
    ]
    fill_comments(jira, issues, comment_statuses, verbose)
    report(64, "Matching people")

    roles = {}
    for name, role in cfg.people.items():
        try:
            roles[jira.account_id(name)] = role
        except JiraError as e:
            if verbose:
                print(f"  ! {e}")
    counts = rules.status_breakdown(issues)
    owed_to_reviewer = rules.unanswered_review(
        issues, cfg.uat_statuses(), reviewer_ids, changelogs, dev_status,
        review_cfg.get("after_hours", 24), review_cfg.get("answered_by", "developer"),
        cfg.working_days_only, roles,
    )
    flags = {
        "missing_dates": rules.missing_dates(
            issues, cfg.post_todo_statuses(), date_fields, changelogs, dev_status, roles
        ),
        "unanswered_review": owed_to_reviewer,
        "missing_description": rules.missing_description(
            issues, changelogs, dev_status,
            cfg.rules.get("missing_description", {}).get("after_hours", 24),
            cfg.working_days_only, roles,
        ),
        "overdue": rules.overdue(
            issues, changelogs, dev_status, cfg.status("done"),
            target_field, datetime.now(cfg.tz).date(), roles,
        ),
        "over_estimate": rules.over_estimate(
            issues, changelogs, dev_status, cfg.status("done"), roles,
        ),
        "awaiting_reviewer": rules.awaiting_reviewer(
            issues, cfg.uat_statuses(), reviewer_ids, changelogs, dev_status,
            cfg.rules.get("awaiting_reviewer", {}).get("after_hours", 72),
            cfg.working_days_only, roles, {f["key"] for f in owed_to_reviewer},
        ),
        "stuck_in_uat": rules.stuck_in_uat(
            issues, changelogs, cfg.parked_uat_statuses(), dev_status,
            cfg.rules.get("stuck_in_uat", {}).get("after_days", 2) * 24,
            cfg.working_days_only, roles,
        ),
        "development_paused": rules.development_paused(
            issues, changelogs, dev_status, cfg.workflow.get("development_paused"),
            cfg.working_days_only, roles,
        ),
        "qa_fail": rules.qa_fail(
            issues, changelogs, dev_status, cfg.workflow.get("qa_fail"),
            qa_ids, qa_names, cfg.working_days_only, roles,
        ),
        "uat_paused": rules.missing_status_reason(
            issues, changelogs, dev_status, cfg.workflow.get("uat_paused"),
            "the UAT pause", cfg.working_days_only, roles,
        ),
        "uat_flagged": rules.missing_status_reason(
            issues, changelogs, dev_status, cfg.workflow.get("uat_flagged"),
            "the UAT flag", cfg.working_days_only, roles,
        ),
        "uat_fail_return": rules.uat_fail_return(
            issues, changelogs, dev_status, cfg.workflow.get("uat_fail"),
            fail_ids, fail_names, cfg.working_days_only, roles,
        ),
    }

    report(68, "Checking the sprint")
    cards = progress.build_cards(
        issues, changelogs, dev_status, roles, target_field, cfg.tz,
        cfg.status("done"), name_roles=cfg.people,
    )
    report(72, "Finding time entries")
    worklogs = gather_worklogs(
        jira, cfg.project, cfg.tz, datetime.now(timezone.utc), verbose,
        on_each=lambda done, total: report(
            _between(72, 96, done, total),
            f"Reading worklogs, {done} of {total}",
        ),
    )
    report(97, "Saving the sprint")
    conn = db.connect(cfg.database)
    label = cfg.sprint if cfg.sprint not in (None, "", "open") else sprint_name(issues, sprint_field)
    sid = db.save_snapshot(
        conn, label, len(issues), counts, flags, cards, worklogs,
    )
    if chosen:
        settings.remember_sprint(config_path, chosen)
    if verbose:
        for rule, found in flags.items():
            print(f"  {rule}: {len(found)} flagged")
        print(f"  cards: {len(cards)}")
        print(f"  saved snapshot {sid} to {cfg.database}")
    report(100, "Done")
    return sid


if __name__ == "__main__":
    try:
        run(sys.argv[1] if len(sys.argv) > 1 else "config.yaml")
    except (JiraError, settings.ConfigError) as e:
        print(f"error: {e}", file=sys.stderr)
        sys.exit(1)
