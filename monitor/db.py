"""Snapshot storage. One row set per collector run, so trends come for free."""
import sqlite3
from datetime import datetime, timezone

SCHEMA = """
CREATE TABLE IF NOT EXISTS snapshots (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  taken_at TEXT NOT NULL,
  sprint TEXT,
  issue_count INTEGER
);
CREATE TABLE IF NOT EXISTS status_counts (
  snapshot_id INTEGER REFERENCES snapshots(id) ON DELETE CASCADE,
  status TEXT NOT NULL,
  count INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS flags (
  snapshot_id INTEGER REFERENCES snapshots(id) ON DELETE CASCADE,
  rule TEXT NOT NULL,
  issue_key TEXT NOT NULL,
  developer TEXT,
  role TEXT,
  holder TEXT,
  status TEXT,
  detail TEXT,
  hours INTEGER
);
CREATE TABLE IF NOT EXISTS cards (
  snapshot_id INTEGER REFERENCES snapshots(id) ON DELETE CASCADE,
  issue_key TEXT NOT NULL,
  summary TEXT,
  status TEXT,
  developer TEXT,
  role TEXT,
  holder TEXT,
  target_date TEXT,
  updated_at TEXT,
  stage_since TEXT,
  last_comment_at TEXT,
  last_note TEXT,
  comment_days TEXT
);
CREATE TABLE IF NOT EXISTS worklogs (
  snapshot_id INTEGER REFERENCES snapshots(id) ON DELETE CASCADE,
  issue_key TEXT,
  author TEXT,
  author_id TEXT,
  started TEXT,
  time_spent TEXT,
  seconds INTEGER
);
CREATE INDEX IF NOT EXISTS idx_flags_snapshot ON flags(snapshot_id);
CREATE INDEX IF NOT EXISTS idx_worklogs_snapshot ON worklogs(snapshot_id);
CREATE INDEX IF NOT EXISTS idx_counts_snapshot ON status_counts(snapshot_id);
CREATE INDEX IF NOT EXISTS idx_cards_snapshot ON cards(snapshot_id);
"""


def connect(path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def save_snapshot(conn, sprint, issue_count, counts, flags_by_rule, cards=None, worklogs=None):
    taken_at = datetime.now(timezone.utc).isoformat()
    cur = conn.execute(
        "INSERT INTO snapshots (taken_at, sprint, issue_count) VALUES (?,?,?)",
        (taken_at, sprint, issue_count),
    )
    sid = cur.lastrowid
    conn.executemany(
        "INSERT INTO status_counts (snapshot_id, status, count) VALUES (?,?,?)",
        [(sid, s, c) for s, c in counts.items()],
    )
    rows = []
    for rule, flags in flags_by_rule.items():
        for f in flags:
            rows.append((sid, rule, f["key"], f["developer"], f.get("role", "unlisted"),
                         f.get("holder"), f["status"], f["detail"], f["hours"]))
    conn.executemany(
        "INSERT INTO flags (snapshot_id, rule, issue_key, developer, role, holder,"
        " status, detail, hours) VALUES (?,?,?,?,?,?,?,?,?)",
        rows,
    )
    conn.executemany(
        "INSERT INTO cards (snapshot_id, issue_key, summary, status, developer, role,"
        " holder, target_date, updated_at, stage_since, last_comment_at, last_note,"
        " comment_days) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [(
            sid, card["key"], card.get("summary"), card.get("status"),
            card.get("developer"), card.get("role"), card.get("holder"),
            card.get("target_date"), card.get("updated_at"), card.get("stage_since"),
            card.get("last_comment_at"), card.get("last_note"), card.get("comment_days"),
        ) for card in (cards or [])],
    )
    conn.executemany(
        "INSERT INTO worklogs (snapshot_id, issue_key, author, author_id, started,"
        " time_spent, seconds) VALUES (?,?,?,?,?,?,?)",
        [(
            sid, log.get("issue_key"), log.get("author"), log.get("author_id"),
            log.get("started"), log.get("time_spent"), log.get("seconds") or 0,
        ) for log in (worklogs or [])],
    )
    conn.commit()
    return sid


def latest_snapshot(conn):
    return conn.execute("SELECT * FROM snapshots ORDER BY id DESC LIMIT 1").fetchone()


def latest_for_sprint(conn, sprint):
    return conn.execute(
        "SELECT * FROM snapshots WHERE sprint = ? ORDER BY id DESC LIMIT 1",
        (sprint,),
    ).fetchone()


def saved_sprints(conn):
    rows = conn.execute(
        "SELECT sprint FROM snapshots WHERE sprint IS NOT NULL AND sprint != '' "
        "GROUP BY sprint ORDER BY MAX(id) DESC"
    ).fetchall()
    return [row["sprint"] for row in rows]


def counts_for(conn, snapshot_id):
    return {
        r["status"]: r["count"]
        for r in conn.execute(
            "SELECT status, count FROM status_counts WHERE snapshot_id=?", (snapshot_id,)
        )
    }


def worklogs_for(conn, snapshot_id):
    return conn.execute(
        "SELECT * FROM worklogs WHERE snapshot_id=?",
        (snapshot_id,),
    ).fetchall()


def cards_for(conn, snapshot_id):
    return conn.execute(
        "SELECT * FROM cards WHERE snapshot_id=? ORDER BY developer, issue_key",
        (snapshot_id,),
    ).fetchall()


def all_flags(conn, snapshot_id):
    return conn.execute(
        "SELECT * FROM flags WHERE snapshot_id=?",
        (snapshot_id,),
    ).fetchall()


def distinct_trend(conn, rules, limit=30, sprint=None):
    """How many different cards each of the last N snapshots flagged for these rules."""
    if sprint:
        snaps = conn.execute(
            "SELECT id FROM snapshots WHERE sprint = ? ORDER BY id DESC LIMIT ?",
            (sprint, limit),
        ).fetchall()[::-1]
    else:
        snaps = conn.execute(
            "SELECT id FROM snapshots ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()[::-1]
    if not snaps or not rules:
        return []
    ids = [s["id"] for s in snaps]
    rows = conn.execute(
        f"""SELECT snapshot_id, COUNT(DISTINCT issue_key) n FROM flags
            WHERE snapshot_id IN ({",".join("?" * len(ids))})
              AND rule IN ({",".join("?" * len(rules))})
            GROUP BY snapshot_id""",
        (*ids, *rules),
    ).fetchall()
    by_id = {r["snapshot_id"]: r["n"] for r in rows}
    return [by_id.get(i, 0) for i in ids]


def flags_for(conn, snapshot_id, rule):
    return conn.execute(
        "SELECT * FROM flags WHERE snapshot_id=? AND rule=? ORDER BY hours DESC NULLS LAST, issue_key",
        (snapshot_id, rule),
    ).fetchall()


def by_person(conn, snapshot_id):
    """One row per person carrying at least one flag, worst first."""
    rows = conn.execute(
        "SELECT developer, role, rule, COUNT(*) n FROM flags WHERE snapshot_id=?"
        " GROUP BY developer, role, rule", (snapshot_id,)
    ).fetchall()
    people = {}
    for r in rows:
        rec = people.setdefault(r["developer"], {
            "developer": r["developer"], "role": r["role"] or "unlisted",
            "missing_dates": 0, "silent_in_development": 0, "overdue": 0, "missing_description": 0,
            "unanswered_review": 0, "stuck_in_uat": 0, "awaiting_reviewer": 0,
            "development_paused": 0, "qa_fail": 0, "uat_paused": 0, "uat_flagged": 0,
            "uat_fail_return": 0, "total": 0,
        })
        rec[r["rule"]] = r["n"]
        rec["total"] += r["n"]
    return sorted(people.values(), key=lambda p: -p["total"])


def trend(conn, limit=30):
    """Flag totals per rule over the last N snapshots, oldest first."""
    snaps = conn.execute(
        "SELECT id, taken_at FROM snapshots ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()[::-1]
    series = {}
    for s in snaps:
        rows = conn.execute(
            "SELECT rule, COUNT(*) n FROM flags WHERE snapshot_id=? GROUP BY rule", (s["id"],)
        ).fetchall()
        by_rule = {r["rule"]: r["n"] for r in rows}
        for rule in ("missing_dates", "silent_in_development", "unanswered_review",
                     "stuck_in_uat", "overdue", "missing_description",
                     "awaiting_reviewer", "development_paused", "qa_fail",
                     "uat_paused", "uat_flagged", "uat_fail_return"):
            series.setdefault(rule, []).append(by_rule.get(rule, 0))
    return [s["taken_at"] for s in snaps], series
