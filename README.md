# Sprint monitor

A small read-only service that snapshots your open Jira sprint on a schedule and
serves a dashboard: where every card sits in the workflow, plus three hygiene
checks that Jira itself can't express.

Built for **BSB / BAP_SCRUM_Board** on `blockapt.atlassian.net`, but every status
and field name lives in `config.yaml`, so it moves to other projects without code
changes.

## What it checks

| Check | Rule |
|---|---|
| Stage breakdown | Live count per workflow status, parent cards only |
| Past its due date | Still in flight with the due date behind us, worst first |
| Waiting on a reply | A reviewer commented on a UAT card and the developer who built it hasn't answered |
| Parked in UAT | How long each card has sat in `5) UAT` or `5.1) UAT - Fail` |
| Being built with no brief | In `2) Development` past the threshold with an empty description |
| Picked up, then quiet | In `2) Development` past the threshold with no comment since it moved there |
| Started without dates | Left `1) To Do` but start date or target date is still empty |

None of the last four can be done in JQL. Jira can find *when* a card moved, but
it can't compare that against comment timestamps — so the collector pulls
comments and history and does the comparison itself.

## Scope: parent cards only

Sub-tasks are excluded by default (`include_subtasks: false`). They carry their
own status but no independent delivery meaning, and counting them badly distorts
the picture — in v8.8.0, 57 of 87 cards were sub-tasks, and including them made
the sprint read as 54 Done out of 87 when only 2 of the 30 real cards were done.

`sprint:` pins the sprint by name. Use `open` for every open sprint only if you
actually run one at a time — BSB currently has five sprints in the open state,
several ended over a year ago, so `openSprints()` pulls in hundreds of unrelated
cards. The collector warns when it sees more than one.

## Who built the card

Cards change hands as they move: QA takes them at `3) QA`, the reviewer takes
them at `5) UAT`. So the assignee field tells you who *holds* a card, not who
built it, and chasing the assignee of a UAT card just means chasing Celeste.

Every flag is therefore attributed from the changelog, to whoever was assigned
while the card sat in Development. The dashboard shows both: **Built by** is the
person to chase, **Now with** is where the card is parked. When a reviewer asks
a question, only a reply from the developer clears the flag — a reply from the
PM doesn't, unless you set `answered_by: anyone`.

Attribution falls back through: assigned when it left Development, then assigned
when it entered, then whoever dragged it across the board. Cards that never
reached Development show as *unattributed*.

## The team

`config.yaml` carries a roster so the dashboard can tell roles apart:

| Person | Role |
|---|---|
| Afifa Cheema | pm |
| Malik Ahmad | devops |
| Abdullah Rafique | automation |
| Asad Tanveer, Nabeel Qadri, Rana Asad, Talal Ghauri, Waqar Tanveer | developer |

Roles do two things. Every flag is tagged with the assignee's role, and a
"who's carrying the flags" panel sits at the top of the dashboard so you can see
whether a rule is catching the whole team or one person having a bad week.

Anyone assigned a card who isn't in the roster shows up as **unlisted** in red —
that's the signal to add them, or to check why an outside account is holding
sprint work.

By default every rule checks everyone. If DevOps or PM cards shouldn't be held
to the commenting rule, scope it:

```yaml
silent_in_development:
  after_hours: 24
  only_roles: [developer, automation]
```

Celeste isn't in the roster because she's the reviewer, not an assignee — she
lives under `unanswered_review.reviewers`. Add anyone else who reviews UAT there.

## Credentials

This needs a Jira API token. The Atlassian connector in the Claude app can't be
used here — that's an OAuth grant held on Anthropic's servers and tied to a chat
session, with no way to hand it to a process on your own machine.

You create the token yourself, from your own account, at
**id.atlassian.com/manage-profile/security/api-tokens**. It carries exactly the
permissions your Jira account already has, and this app only ever makes GET
requests. If that page is blocked, your Atlassian admin has disabled token
creation org-wide; ask them for a service account with browse access to BSB.

## Setup

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

cp config.example.yaml config.yaml
export JIRA_EMAIL="you@company.com"
export JIRA_API_TOKEN="..."        # id.atlassian.com -> API tokens
```

Use a Jira account with **read-only** project access. The token never goes in
`config.yaml` and no issue data leaves your network — everything is stored in a
local SQLite file.

Then fill in the ids that vary by site:

```bash
python discover.py Celeste
```

That prints your real field ids (start date, target date, sprint), every status
in the project, and the accountId for anyone you name. Copy the ones you need
into `config.yaml`.

## Running

```bash
./run
```

That reads the sprint from Jira, starts the dashboard at http://127.0.0.1:8000, and opens it. Stop it with Ctrl+C. `python -m monitor` does the same thing once the virtualenv is active.

To take a snapshot without leaving the server running, use `python -m monitor.collect`.

Schedule collection — once a morning is usually enough, twice if you want the
afternoon state too:

```cron
0 8,14 * * 1-5  cd /srv/sprint-monitor && .venv/bin/python -m monitor.collect >> collect.log 2>&1
```

Every run writes a full snapshot, so the trend lines build up on their own. The
dashboard only reads; it never calls Jira.

## Tuning the rules

In `config.yaml`:

- `silent_in_development.after_hours` — how long a card can sit untouched before
  it's flagged. Start at 24 and raise it if the list is noisy.
- `unanswered_review.reviewers` — display names or accountIds.
- `unanswered_review.answered_by` — `assignee` flags a card unless the assignee
  personally replies; `anyone` accepts a reply from any non-reviewer.
- `working_days_only` — on by default, so a card that moves Friday afternoon
  isn't flagged first thing Monday.

## Cost and load

One collector run is roughly: one paged search over the sprint, plus one
changelog call per card currently in Development. For a 60-card sprint that's
well under 30 API calls. The client backs off on 429 and retries. Running twice
a day sits far inside Atlassian's rate limits.

## Notes

- Jira comments aren't threaded, so "didn't reply" means *no comment after the
  reviewer's last one*, not a missing reply on a thread.
- `discover.py` samples 1000 issues to list statuses; a status nobody has used
  recently may not show up.
- I couldn't run this against your live Jira from where it was built — the four
  rules are unit-tested against synthetic issues, but the first real run is
  yours. `python -m monitor.collect` prints what it found before saving.
