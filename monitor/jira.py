"""Thin read-only Jira Cloud REST client.

Only GETs. Handles pagination, 429 backoff, and resolving human field names
to customfield_* ids so config.yaml stays readable.
"""
import time
import requests
from requests.auth import HTTPBasicAuth


class JiraError(RuntimeError):
    pass


class Jira:
    def __init__(self, site, email, token, timeout=30):
        self.site = site.rstrip("/")
        self.auth = HTTPBasicAuth(email, token)
        self.timeout = timeout
        self.session = requests.Session()
        self._field_cache = None
        self._user_cache = {}
        # A token that starts with ATATT is scoped. Atlassian counts the call
        # as "last accessed", then rejects it on yoursite.atlassian.net with
        # an empty sprint. Scoped tokens have to go through the gateway.
        self.base = self._api_base(token)

    def _api_base(self, token):
        if not token.startswith("ATATT"):
            return f"{self.site}/rest/api/3"
        info = requests.get(f"{self.site}/_edge/tenant_info", timeout=self.timeout)
        if not info.ok:
            raise JiraError(f"Could not read the Jira cloud id from {self.site}.")
        cloud_id = info.json().get("cloudId")
        if not cloud_id:
            raise JiraError(f"No cloud id in the tenant info for {self.site}.")
        return f"https://api.atlassian.com/ex/jira/{cloud_id}/rest/api/3"

    def _get(self, path, params=None, attempt=1):
        url = path if path.startswith("http") else f"{self.base}{path}"
        r = self.session.get(
            url,
            params=params,
            auth=self.auth,
            headers={"Accept": "application/json"},
            timeout=self.timeout,
        )
        if r.status_code == 429 and attempt <= 5:
            wait = int(r.headers.get("Retry-After", 2 ** attempt))
            time.sleep(min(wait, 60))
            return self._get(path, params, attempt + 1)
        if r.status_code == 401:
            raise JiraError(
                "Jira rejected the token (401). On the token page, open View scopes "
                "and make sure read:jira-work and read:jira-user are included. "
                "A scoped token without those still shows as recently accessed, "
                "then returns an empty sprint."
            )
        if r.status_code == 403:
            raise JiraError("The Jira account lacks permission for this project (403).")
        if not r.ok:
            raise JiraError(f"{r.status_code} from {url}: {r.text[:300]}")
        return r.json()

    def check_auth(self):
        """Confirm the token is accepted before searching.

        A rejected token does not always get a 401. Jira answers the search
        as an anonymous visitor instead, which is an empty sprint, not an error.
        """
        self._get("/myself")

    # -- fields -----------------------------------------------------------

    def fields(self):
        if self._field_cache is None:
            self._field_cache = self._get("/field")
        return self._field_cache

    def field_id(self, display_name):
        """'Start date' -> 'customfield_10015'. Returns None if not found."""
        if not display_name:
            return None
        wanted = display_name.strip().lower()
        for f in self.fields():
            if f.get("name", "").strip().lower() == wanted:
                return f["id"]
        return None

    # -- users ------------------------------------------------------------

    def account_id(self, name_or_id):
        """Accept a display name or an accountId; always return an accountId."""
        if not name_or_id:
            return None
        if name_or_id in self._user_cache:
            return self._user_cache[name_or_id]
        # Looks like an accountId already.
        if ":" in name_or_id or len(name_or_id) >= 24 and " " not in name_or_id:
            self._user_cache[name_or_id] = name_or_id
            return name_or_id
        hits = self._get("/user/search", {"query": name_or_id, "maxResults": 5})
        if not hits:
            raise JiraError(f"No Jira user matched '{name_or_id}'. Run discover.py to find them.")
        if len(hits) > 1:
            names = ", ".join(f"{h.get('displayName')} ({h['accountId']})" for h in hits)
            raise JiraError(
                f"'{name_or_id}' matched more than one user: {names}. "
                "Put the accountId in config.yaml instead."
            )
        self._user_cache[name_or_id] = hits[0]["accountId"]
        return hits[0]["accountId"]

    # -- issues -----------------------------------------------------------

    def search(self, jql, fields, page_size=100, max_issues=5000, expand=None, on_page=None):
        """Page through /search/jql. Returns a list of issues."""
        issues, token = [], None
        while True:
            params = {"jql": jql, "fields": ",".join(fields), "maxResults": page_size}
            if expand:
                params["expand"] = expand
            if token:
                params["nextPageToken"] = token
            page = self._get("/search/jql", params)
            issues.extend(page.get("issues", []))
            if on_page:
                on_page(len(issues))
            token = page.get("nextPageToken")
            if page.get("isLast", True) or not token or len(issues) >= max_issues:
                return issues

    def changelogs_for(self, issues, on_each=None):
        """Changelog per issue key, using what the search already returned and
        only calling the API for issues whose history was truncated."""
        out, fetched = {}, 0
        total = len(issues)
        for index, issue in enumerate(issues, start=1):
            embedded = issue.get("changelog") or {}
            histories = embedded.get("histories")
            complete = histories is not None and embedded.get("total", 0) <= len(histories)
            if complete:
                out[issue["key"]] = histories
            else:
                out[issue["key"]] = self.changelog(issue["key"])
                fetched += 1
            if on_each:
                on_each(index, total)
        self.last_changelog_fetches = fetched
        return out

    def changelog(self, issue_key):
        """All status changes for one issue, oldest first."""
        entries, start = [], 0
        while True:
            page = self._get(f"/issue/{issue_key}/changelog", {"startAt": start, "maxResults": 100})
            entries.extend(page.get("values", []))
            start += len(page.get("values", []))
            if page.get("isLast", True) or start >= page.get("total", 0):
                return entries

    def comments(self, issue_key):
        """Every comment on one issue, oldest first.

        Search only embeds the first page, which drops a reason left later
        on a card that already has a long thread.
        """
        comments, start = [], 0
        while True:
            page = self._get(
                f"/issue/{issue_key}/comment",
                {"startAt": start, "maxResults": 100},
            )
            batch = page.get("comments") or []
            comments.extend(batch)
            start += len(batch)
            if not batch or start >= page.get("total", 0):
                return comments

    def worklogs(self, issue_key, started_after=None):
        """Worklogs on one issue, oldest first.

        started_after is a UNIX timestamp in milliseconds. Search only embeds
        the first page, which can hide the latest time entry.
        """
        worklogs, start = [], 0
        while True:
            params = {"startAt": start, "maxResults": 100}
            if started_after is not None:
                params["startedAfter"] = int(started_after)
            page = self._get(f"/issue/{issue_key}/worklog", params)
            batch = page.get("worklogs") or []
            worklogs.extend(batch)
            start += len(batch)
            if not batch or start >= page.get("total", 0):
                return worklogs

    def sprint_names(self, project):
        """Sprint names to offer on the board.

        Read from the Sprint field on recent cards. Active and future names
        come first, then the closed ones those cards still carry. The agile
        board API is not used: a scoped token that can search issues often
        cannot list boards.
        """
        sprint_field = self.field_id("Sprint")
        if not sprint_field:
            return []
        issues = self.search(
            f'project = "{project}" AND updated >= -180d ORDER BY updated DESC',
            [sprint_field],
            max_issues=300,
        )
        groups = {"active": [], "future": [], "closed": []}
        for issue in issues:
            for sprint in (issue.get("fields") or {}).get(sprint_field) or []:
                if not isinstance(sprint, dict):
                    continue
                name = (sprint.get("name") or "").strip()
                state = sprint.get("state") if sprint.get("state") in groups else "closed"
                if name and name not in groups[state]:
                    groups[state].append(name)
        return groups["active"] + groups["future"] + groups["closed"][:12]
