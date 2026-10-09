"""Dashboard. Reads snapshots only — collection happens in collect.py.

The page leads with cards a person has to act on. Missing dates and empty
briefs are paperwork: collected, but folded away so they don't bury the chase.
"""
from collections import Counter
from datetime import datetime, timezone
import threading
import time
from flask import Flask, jsonify, redirect, render_template, request, url_for
from . import collect, db, progress, settings
from .jira import Jira, JiraError

# rank: lower is the problem to put in the headline when a card trips several.
# hygiene: true means the card is paperwork unless something above it also fired.
RULES = {
    "qa_fail": {"title": "QA fail not explained", "short": "QA fail", "rank": 1},
    "uat_fail_return": {"title": "UAT fail not handed back", "short": "UAT fail", "rank": 2},
    "uat_flagged": {"title": "UAT flagged with no reason", "short": "UAT flagged", "rank": 3},
    "overdue": {"title": "Target date reached", "short": "past target", "rank": 4},
    "over_estimate": {"title": "Over its estimate", "short": "over estimate", "rank": 5},
    "unanswered_review": {"title": "Waiting on a reply", "short": "no reply", "rank": 6},
    "development_paused": {"title": "Development paused with no reason", "short": "dev pause", "rank": 7},
    "uat_paused": {"title": "UAT paused with no reason", "short": "UAT pause", "rank": 8},
    "stuck_in_uat": {"title": "Parked in UAT", "short": "parked in UAT", "rank": 9},
    "awaiting_reviewer": {"title": "Waiting on the reviewer", "short": "reviewer quiet", "rank": 10},
    "missing_description": {"title": "Being built with no brief", "short": "no brief", "rank": 30, "hygiene": True},
    "missing_dates": {"title": "Started without dates", "short": "no dates", "rank": 31, "hygiene": True},
}

STAGE_COLOURS = {
    "todo": "#8494A6", "development": "#2F6FB8", "development_paused": "#8AA4C4",
    "qa": "#1E8C86", "qa_fail": "#C45C26", "documentation": "#5D6C7B",
    "uat": "#6A5AB8", "uat_pending": "#6A5AB8", "uat_in_progress": "#4C3F9A",
    "uat_paused": "#9A90C8", "uat_flagged": "#C47B12",
    "uat_fail": "#B01238", "uat_pass": "#4E9B3F",
    "final_checks": "#3D6B8A", "done": "#2C7A4B",
}

CHASE_RULES = [key for key, meta in RULES.items() if not meta.get("hygiene")]
_SKIP_PILE = {"todo", "done"}


def as_days(hours):
    if not hours:
        return "—"
    return f"{hours / 24:.1f}d" if hours >= 24 else f"{hours}h"


def _meta(rule):
    return RULES.get(rule, {"title": rule, "short": rule, "rank": 50})


def _change(values):
    if not values or len(values) < 2:
        return None
    delta = values[-1] - values[-2]
    if delta == 0:
        return "level with the last run"
    return f"{abs(delta)} {'more' if delta > 0 else 'fewer'} than the last run"


def assemble(flag_rows, ladder):
    """Turn raw flags into the three things the page shows: who to talk to,
    the cards to chase, and the paperwork that can wait."""
    cards = {}
    for row in flag_rows:
        if row["rule"] == "silent_in_development":
            continue
        meta = _meta(row["rule"])
        card = cards.setdefault(row["issue_key"], {
            "key": row["issue_key"],
            "developer": row["developer"],
            "role": row["role"],
            "holder": row["holder"],
            "flags": [],
        })
        card["flags"].append({
            "rule": row["rule"],
            "rank": meta["rank"],
            "title": meta["title"],
            "short": meta["short"],
            "hygiene": bool(meta.get("hygiene")),
            "detail": row["detail"],
            "status": row["status"],
            "hours": row["hours"],
            "developer": row["developer"],
            "role": row["role"],
            "holder": row["holder"],
        })

    for card in cards.values():
        card["flags"].sort(key=lambda flag: (flag["rank"], -(flag["hours"] or 0)))
        top = card["flags"][0]
        card["developer"] = top["developer"]
        card["role"] = top["role"]
        card["holder"] = top["holder"]
        card["title"] = top["title"]
        card["detail"] = top["detail"]
        card["status"] = top["status"]
        card["hours"] = top["hours"]
        card["rank"] = top["rank"]
        card["show_holder"] = bool(card["holder"]) and card["holder"] != card["developer"]
        others = [flag["short"] for flag in card["flags"][1:]]
        card["also"] = ", ".join(others)
        card["hygiene_only"] = all(flag["hygiene"] for flag in card["flags"])

    chase = [card for card in cards.values() if not card["hygiene_only"]]
    chase.sort(key=lambda card: (card["rank"], -(card["hours"] or 0), card["key"]))
    paperwork = [card for card in cards.values() if card["hygiene_only"]]
    paperwork.sort(key=lambda card: (-(card["hours"] or 0), card["key"]))

    talk = []
    by_person = {}
    for card in chase:
        rec = by_person.setdefault(card["developer"], {
            "developer": card["developer"], "role": card["role"],
            "count": 0, "problems": [],
        })
        rec["count"] += 1
        short = card["flags"][0]["short"]
        if short not in rec["problems"]:
            rec["problems"].append(short)
    talk = sorted(by_person.values(), key=lambda rec: (-rec["count"], rec["developer"]))
    for rec in talk:
        rec["problems"] = ", ".join(rec["problems"])

    composition = Counter(card["flags"][0]["short"] for card in chase)
    chase_note = " · ".join(f"{n} {name}" for name, n in composition.most_common())
    if chase:
        oldest = max(chase, key=lambda card: (card["hours"] or 0, card["key"]))
        oldest_value = as_days(oldest["hours"])
        oldest_note = f"{oldest['key']} · {oldest['flags'][0]['short']}"
        oldest_hot = (oldest["hours"] or 0) >= 24
    else:
        oldest_value, oldest_note, oldest_hot = "—", "No card is waiting", False

    inflight = [stage for stage in ladder if stage["key"] not in _SKIP_PILE and stage["count"]]
    if inflight:
        order = {stage["key"]: i for i, stage in enumerate(ladder)}
        pile = max(inflight, key=lambda stage: (stage["count"], order[stage["key"]]))
        pile_value, pile_note = str(pile["count"]), pile["name"]
    else:
        pile_value, pile_note = "0", "Nothing in flight"

    kpis = [
        {
            "label": "Chase now",
            "value": str(len(chase)),
            "note": chase_note or "Nothing needs a person",
            "hot": bool(chase),
        },
        {
            "label": "Longest wait",
            "value": oldest_value,
            "note": oldest_note,
            "hot": oldest_hot,
        },
        {
            "label": "Biggest pile",
            "value": pile_value,
            "note": pile_note,
            "hot": False,
        },
    ]
    return {
        "kpis": kpis,
        "talk": talk,
        "chase": chase,
        "paperwork": paperwork,
    }


def _sprint_choices(cfg, conn):
    """Names to offer on the board: live sprints from Jira, then ones already saved."""
    saved = db.saved_sprints(conn)
    cached = getattr(_sprint_choices, "cache", None)
    now = time.time()
    fresh = cached and cached[2] == cfg.project and now - cached[0] < (600 if cached[1] else 30)
    if fresh:
        live = cached[1]
    else:
        try:
            live = Jira(cfg.site, cfg.email, cfg.token).sprint_names(cfg.project)
        except Exception:
            # The board still opens from the saved snapshot if Jira is unreachable.
            live = cached[1] if cached else []
        _sprint_choices.cache = (now, live, cfg.project)
    names = []
    for name in (*live, *saved):
        if name and name not in names and name != "Open sprint":
            names.append(name)
    return names


def narrow_to(cards, flags, who):
    """Keep one builder's cards and flags. An empty name leaves the sprint whole."""
    if not who:
        return cards, flags
    return (
        [card for card in cards if card.get("developer") == who],
        [flag for flag in flags if flag.get("developer") == who],
    )


def developers_in(cards):
    """Builders on this sprint, one row each, in name order."""
    found = {}
    for card in cards:
        name = card.get("developer") or ""
        if name and name not in found:
            found[name] = card.get("role") or ""
    return [{"name": name, "role": found[name]} for name in sorted(found)]


def create_app(config_path="config.yaml"):
    cfg = settings.load(config_path)
    app = Flask(__name__)
    if not cfg.credentials_ready:
        @app.route("/", methods=["GET", "POST"])
        @app.route("/<path:_path>", methods=["GET", "POST"])
        def needs_credentials(_path=""):
            return render_template("setup.html")

        return app

    app.job_lock = threading.Lock()
    app.job = {
        "sprint": "", "percent": 0, "label": "Starting",
        "done": False, "error": "", "url": "",
    }
    app.job_thread = None
    app.job_busy = False

    def _note(pct, label):
        with app.job_lock:
            if pct < app.job["percent"] and not app.job["done"]:
                pct = app.job["percent"]
            app.job["percent"] = max(0, min(100, int(pct)))
            app.job["label"] = label

    @app.route("/sprint", methods=["POST"])
    def load_sprint():
        typed = (request.form.get("typed") or "").strip()
        picked = (request.form.get("picked") or "").strip()
        name = typed or picked
        if not name or len(name) > 80 or any(ord(char) < 32 for char in name):
            return redirect(url_for(
                "dashboard",
                error="Enter the sprint name as it appears in Jira.",
            ))
        with app.job_lock:
            running = app.job_busy or (app.job_thread is not None and app.job_thread.is_alive())
            if running:
                name = app.job["sprint"] or name
            else:
                app.job_busy = True
                app.job = {
                    "sprint": name, "percent": 0, "label": "Starting",
                    "done": False, "error": "", "url": "",
                }
        if not running:
            target = url_for("dashboard", sprint=name)

            def work():
                try:
                    collect.run(config_path, verbose=True, sprint=name, on_progress=_note)
                    _note(100, "Opening the board")
                    with app.job_lock:
                        app.job["done"] = True
                        app.job["url"] = target
                        app.job["error"] = ""
                except Exception as exc:
                    with app.job_lock:
                        app.job["done"] = True
                        app.job["error"] = str(exc)[:300]
                        app.job["url"] = ""
                finally:
                    with app.job_lock:
                        app.job_busy = False

            app.job_thread = threading.Thread(target=work, daemon=True)
            app.job_thread.start()
        return redirect(url_for("loading", sprint=name))

    @app.route("/loading")
    def loading():
        sprint = (request.args.get("sprint") or "").strip()
        return render_template("loading.html", sprint=sprint, project=cfg.project)

    @app.route("/sprint/progress")
    def sprint_progress():
        with app.job_lock:
            return jsonify(dict(app.job))

    @app.route("/")
    def dashboard():
        conn = db.connect(cfg.database)
        wanted = (request.args.get("sprint") or "").strip()
        load_error = (request.args.get("error") or "").strip()[:300]
        choices = _sprint_choices(cfg, conn)
        if not wanted:
            return render_template(
                "empty.html", project=cfg.project, sprint_choices=choices, load_error=load_error,
            )
        snap = db.latest_for_sprint(conn, wanted)
        if not snap:
            return render_template(
                "empty.html", project=cfg.project, sprint_choices=choices,
                load_error=load_error or f"No saved read for '{wanted}' yet. Load it from Jira.",
            )

        selected_stage = request.args.get("stage") or ""
        selected_who = request.args.get("who") or ""
        log_statuses = {name for key in progress.LOG_KEYS if (name := cfg.workflow.get(key))}
        all_cards = [dict(row) for row in db.cards_for(conn, snap["id"])]
        developers = developers_in(all_cards)
        all_flags = [dict(row) for row in db.all_flags(conn, snap["id"])]
        raw_cards, flag_rows = narrow_to(all_cards, all_flags, selected_who)

        counts = {}
        for card in raw_cards:
            status = card.get("status") or ""
            if status:
                counts[status] = counts.get(status, 0) + 1
        if not selected_who and not counts:
            counts = db.counts_for(conn, snap["id"])
        total = sum(counts.values()) or 1
        ladder = []
        for key, name in cfg.workflow.items():
            n = counts.get(name, 0)
            if not n:
                continue
            ladder.append({
                "key": key, "name": name, "count": n,
                "share": n / total * 100,
                "colour": STAGE_COLOURS.get(key, "#8494A6"),
            })

        view = assemble(flag_rows, ladder)
        if not selected_who:
            trend = db.distinct_trend(conn, CHASE_RULES, sprint=snap["sprint"])
            view["kpis"][0]["change"] = _change(trend)

        summaries = {card["issue_key"]: card.get("summary") or "" for card in all_cards}
        for card in (*view["chase"], *view["paperwork"]):
            card["summary"] = summaries.get(card["key"], "")
        raw_logs = [dict(row) for row in db.worklogs_for(conn, snap["id"])]
        board = progress.present(
            raw_cards, raw_logs, datetime.now(timezone.utc), cfg.tz,
            log_statuses, cfg.working_days_only, cfg.status("done"),
        ) if raw_cards else None

        shown = board["cards"] if board else []
        if selected_stage:
            shown = [card for card in shown if card["status"] == selected_stage]

        if board and board["open_cards"] == 0:
            no_log = {
                "label": "No log yesterday",
                "value": "—",
                "note": "No open cards" if selected_who else "Sprint is complete",
                "hot": False,
                "href": "#missing-log",
            }
        elif board:
            note = board["yesterday_label"]
            if board["due_people"]:
                word = "person" if board["due_people"] == 1 else "people"
                note = f"{note} · {board['due_people']} {word} had work in progress"
            no_log = {
                "label": "No log yesterday",
                "value": str(len(board["missing"])),
                "note": note,
                "hot": bool(board["missing"]),
                "href": "#missing-log",
            }
        else:
            no_log = {
                "label": "No log yesterday",
                "value": "—",
                "note": "Collect again to fill this",
                "hot": False,
                "href": "#missing-log",
            }
        view["kpis"].insert(0, no_log)
        view["kpis"][1]["href"] = "#chase"
        view["kpis"][2]["href"] = "#chase"
        view["kpis"][3]["href"] = "#stages"

        taken = datetime.fromisoformat(snap["taken_at"]).astimezone(cfg.tz)
        viewing = snap["sprint"] or ""

        def keep(**extra):
            params = {"sprint": viewing} if viewing else {}
            if selected_who and "who" not in extra:
                params["who"] = selected_who
            if selected_stage and "stage" not in extra:
                params["stage"] = selected_stage
            for key, value in extra.items():
                if value:
                    params[key] = value
                else:
                    params.pop(key, None)
            return url_for("dashboard", **params)

        return render_template(
            "dashboard.html",
            sprint=viewing, issue_count=len(raw_cards) if selected_who else snap["issue_count"],
            sprint_total=snap["issue_count"],
            taken=taken.strftime("%d %b, %H:%M"),
            timezone=cfg.timezone,
            ladder=ladder, as_days=as_days,
            project=cfg.project, site=cfg.site,
            board=board, progress=shown,
            developers=developers,
            selected_stage=selected_stage, selected_who=selected_who,
            sprint_choices=choices, load_error=load_error, keep=keep,
            **view,
        )

    return app


if __name__ == "__main__":
    create_app().run(debug=False, port=8000)
