# The in-chat version

`sprint-monitor-live.jsx` is the dashboard that runs as an artifact inside the
Claude app. It needs no token: it reaches Jira through the Atlassian connector
by calling the Anthropic API with the MCP server attached.

It is not a substitute for the app in the parent folder. It can't read
changelogs at volume, so:

- flags are attributed to whoever currently holds a card, which for anything in
  UAT is the reviewer, not the developer
- card ages are bucketed (over 1 day, 2 days, 14 days) rather than exact
- "no reply" clears if anyone who isn't a reviewer answers, not specifically the
  developer
- snapshots accumulate only when someone presses Refresh

Paste the file into a Claude conversation to use it. The constants at the top
(cloud id, sprint name, reviewers, roster) are the only things to change.
