#!/usr/bin/env python3
"""Print the Jira ids you need for config.yaml: fields, statuses, reviewers.

    python discover.py                 # fields + statuses in your project
    python discover.py Celeste         # also look up a person
"""
import os
import sys
import yaml
from dotenv import load_dotenv
from monitor.jira import Jira, JiraError

load_dotenv()

email = os.environ.get("JIRA_EMAIL")
token = os.environ.get("JIRA_API_TOKEN")
if not email or not token:
    sys.exit("Set JIRA_EMAIL and JIRA_API_TOKEN in .env (or the environment) first.")

cfg = yaml.safe_load(open("config.yaml" if os.path.exists("config.yaml") else "config.example.yaml"))
jira = Jira(cfg["site"], email, token)

print("Date-ish and sprint fields")
for f in jira.fields():
    name = f.get("name", "")
    if any(w in name.lower() for w in ("date", "sprint", "due", "target", "start")):
        print(f"  {name!r}  ->  {f['id']}")

print("\nStatuses used in this project")
issues = jira.search(f'project = "{cfg["project"]}"', ["status"], page_size=100, max_issues=1000)
seen = {}
for i in issues:
    s = i["fields"]["status"]
    seen[s["name"]] = s["id"]
for name, sid in sorted(seen.items()):
    print(f"  {name!r}  (id {sid})")
print("  (sampled 1000 issues; a rarely used status may not appear)")

for query in sys.argv[1:]:
    print(f"\nPeople matching {query!r}")
    try:
        for u in jira._get("/user/search", {"query": query, "maxResults": 10}):
            print(f"  {u.get('displayName')}  ->  {u['accountId']}")
    except JiraError as e:
        print(f"  {e}")
