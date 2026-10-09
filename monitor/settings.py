"""Configuration loading. Credentials come from the environment only."""
import os
import pathlib
import re
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
import yaml
from dotenv import load_dotenv

load_dotenv()


class ConfigError(RuntimeError):
    pass


class Settings:
    def __init__(self, data: dict):
        self.site = data["site"].rstrip("/")
        self.project = data["project"]
        self.sprint = data.get("sprint", "open")
        self.include_subtasks = bool(data.get("include_subtasks", False))
        self.field_names = data.get("fields", {})
        self.workflow = data["workflow"]
        self.rules = data.get("rules", {})
        self.people = data.get("people", {})
        self.database = data.get("database", "snapshots.db")
        self.timezone = data.get("timezone") or "Asia/Karachi"
        try:
            self.tz = ZoneInfo(self.timezone)
        except ZoneInfoNotFoundError:
            raise ConfigError(f"Unknown timezone '{self.timezone}'.")

        self.email = (os.environ.get("JIRA_EMAIL") or "").strip()
        self.token = (os.environ.get("JIRA_API_TOKEN") or "").strip()

    @property
    def credentials_ready(self) -> bool:
        return bool(self.email and self.token)

    def sprint_jql(self) -> str:
        clause = f'project = "{self.project}" AND {sprint_clause(self.sprint)}'
        if not self.include_subtasks:
            clause += " AND issuetype NOT IN subTaskIssueTypes()"
        return clause

    def names_with_role(self, *roles) -> list:
        return [n for n, r in self.people.items() if r in roles]

    @property
    def working_days_only(self) -> bool:
        return bool(self.rules.get("working_days_only", True))

    def status(self, key: str) -> str:
        try:
            return self.workflow[key]
        except KeyError:
            raise ConfigError(f"workflow.{key} is missing from config.yaml")

    def _names(self, *keys) -> list:
        return [self.workflow[k] for k in keys if k in self.workflow]

    def post_todo_statuses(self) -> list:
        """Everything a card can sit in once it has left To Do."""
        return [name for key, name in self.workflow.items() if key not in ("todo", "done")]

    def uat_statuses(self) -> list:
        """Where a reviewer and a developer can still owe each other a reply."""
        return self._names("uat", "uat_pending", "uat_in_progress", "uat_fail")

    def parked_uat_statuses(self) -> list:
        """Waiting in review. Paused, flagged, and failed have their own checks."""
        return self._names("uat", "uat_pending", "uat_in_progress")


def sprint_clause(sprint) -> str:
    """JQL scope for one sprint name, or every open sprint."""
    if sprint in (None, "", "open"):
        return "sprint IN openSprints()"
    safe = " ".join(str(sprint).split())
    safe = safe.replace("\\", "\\\\").replace('"', '\\"')
    return f'sprint = "{safe}"'


def remember_sprint(path, name):
    """Keep the sprint the board just loaded as the default for the next run.

    The name is written as one quoted line. A name with a newline or other
    control character is ignored, so a sprint load cannot add keys to config.yaml.
    """
    if not isinstance(name, str) or not name or len(name) > 80:
        return
    if any(ord(char) < 32 for char in name):
        return
    p = pathlib.Path(path)
    if not p.exists():
        return
    shown = '"' + name.replace("\\", "\\\\").replace('"', "") + '"'
    line = f"sprint: {shown}"
    if "\n" in line or "\r" in line:
        return
    text = p.read_text()
    new, count = re.subn(r"(?m)^sprint:.*$", line, text, count=1)
    if count:
        p.write_text(new)


def load(path="config.yaml") -> Settings:
    p = pathlib.Path(path)
    if not p.exists():
        raise ConfigError(f"{path} not found. Copy config.example.yaml and edit it.")
    return Settings(yaml.safe_load(p.read_text()))
