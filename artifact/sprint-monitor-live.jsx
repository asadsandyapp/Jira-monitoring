import React, { useState, useEffect } from "react";

const CLOUD_ID = "b364ad9e-1d5d-4c07-9b64-fbc456afb792";
const SITE = "https://blockapt.atlassian.net";
const SPRINT = "v8.8.0";
const PROJECT = "BSB";
const REVIEWERS = ["Celeste Rush", "Dulan Dissanayake"];
const ROLES = {
  "Afifa Cheema": "pm", "Malik Ahmad": "devops", "Abdullah Rafique": "automation",
  "Asad Tanveer": "developer", "Nabeel Qadri": "developer", "Rana Asad": "developer",
  "Talal Ghauri": "developer", "Waqar Tanveer": "developer",
  "Celeste Rush": "reviewer", "Dulan Dissanayake": "reviewer",
};
const ORDER = ["1) To Do", "2) Development", "3) QA", "4) Documentation", "5) UAT",
  "5.1) UAT - Fail", "5.2) UAT - Pass", "6) Final Checks", "7) Done"];
const COLOUR = {
  "1) To Do": "#8494A6", "2) Development": "#2F6FB8", "3) QA": "#1E8C86", "5) UAT": "#6A5AB8",
  "5.1) UAT - Fail": "#B01238", "5.2) UAT - Pass": "#4E9B3F", "7) Done": "#2C7A4B",
  "4) Documentation": "#9B7B2F", "6) Final Checks": "#2E7D6B",
};
const DONE = "7) Done";
const UAT = ["5) UAT", "5.1) UAT - Fail"];
const STORE_KEY = "sprint-monitor:snapshots";
const SCHEMA = 5;
const RULE_KEYS = ["overdue", "noReply", "waitingOnReviewer", "parked", "noBrief", "quiet", "noDates"];

/* Saved snapshots outlive code changes. Anything missing becomes an empty list
   rather than undefined, which is what crashed this on load. */
function normalise(v) {
  if (!v) return null;
  const flags = {};
  RULE_KEYS.forEach((k) => { flags[k] = Array.isArray(v.flags?.[k]) ? v.flags[k] : []; });
  return {
    counts: v.counts || {},
    flags,
    stale14: Array.isArray(v.stale14) ? v.stale14 : [],
    people: Array.isArray(v.people) ? v.people : [],
    inFlight: v.inFlight || 0,
    at: v.at || new Date().toISOString(),
  };
}
const BASE = `project = ${PROJECT} AND sprint = "${SPRINT}" AND issuetype NOT IN subTaskIssueTypes()`;
const HEAD = `Use the Atlassian tools with cloudId "${CLOUD_ID}".`;
const TAIL = `Reply with ONLY the JSON object. No prose, no explanation, no markdown code fences, no whitespace beyond what JSON requires.`;

/* Three narrow queries. One combined query overflows the reply budget and comes
   back truncated, which is unparseable. */
const STEPS = [
  {
    label: "stages and dates",
    prompt: `${HEAD}
Run searchJiraIssuesUsingJql once: jql \`${BASE}\`, fields ["status","assignee","duedate","customfield_10015"], maxResults 100.
Return {"counts":{"<status name>":<how many cards have it>},"live":[[key,status,assignee,dueDateOrNull,hasStartDate]]}
"counts" covers every card. "live" lists ONLY cards whose status is not "${DONE}". assignee is the displayName or "Unassigned". dueDate is the YYYY-MM-DD string or null. hasStartDate is 1 or 0 and reads customfield_10015.
${TAIL}`,
  },
  {
    label: "how long cards have sat",
    prompt: `${HEAD}
Run searchJiraIssuesUsingJql twice, each with fields ["summary"] and maxResults 100:
a) \`${BASE} AND NOT (status CHANGED AFTER -1d)\`
b) \`${BASE} AND status != "${DONE}" AND description IS EMPTY\`
Return {"s1":[issue keys from a],"nodesc":[keys from b]}
${TAIL}`,
  },
  {
    label: "the latest comment on each card",
    prompt: `${HEAD}
Run searchJiraIssuesUsingJql once: jql \`${BASE} AND status != "${DONE}"\`, fields ["comment"], maxResults 50.
For each card find its single newest comment.
Return {"latest":[[key,newestCommentAuthorDisplayName,newestCommentCreatedISO]]}
Skip any card that has no comments at all.
${TAIL}`,
  },
];

async function ask(prompt) {
  let res;
  try {
    res = await fetch("https://api.anthropic.com/v1/messages", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        model: "claude-sonnet-4-6",
        max_tokens: 1000,
        messages: [{ role: "user", content: prompt }],
        mcp_servers: [{ type: "url", url: "https://mcp.atlassian.com/v1/mcp", name: "atlassian" }],
      }),
    });
  } catch (e) {
    throw new Error(`Couldn't reach the API: ${e.message}`);
  }
  const data = await res.json();
  if (data.error) throw new Error(`API said: ${data.error.message || JSON.stringify(data.error)}`);

  const blocks = data.content || [];
  const text = blocks.filter((b) => b.type === "text").map((b) => b.text).join("\n").trim();
  const toolCalls = blocks.filter((b) => b.type === "mcp_tool_use").length;

  if (!text) {
    throw new Error(
      toolCalls
        ? `Jira was queried (${toolCalls} calls) but no answer came back — the result was probably too large.`
        : "Nothing came back, and Jira was never queried. The Atlassian connector may not be reachable from here."
    );
  }
  const start = text.indexOf("{"), end = text.lastIndexOf("}");
  if (start === -1 || end <= start) throw new Error(`Expected JSON, got: "${text.slice(0, 160)}"`);
  try {
    return JSON.parse(text.slice(start, end + 1));
  } catch (e) {
    const tail = text.slice(Math.max(start, end - 80), end + 1);
    throw new Error(`The reply was cut off mid-JSON. It ended with: "${tail}"`);
  }
}

/* Bucketed thresholds can't answer "how many days exactly" — that needs each
   card's own changelog. Only fetched for cards actually sitting in UAT, so
   it's at most a dozen calls, not the whole sprint. */
async function fetchExactAges(keys) {
  if (!keys.length) return {};
  const prompt = `${HEAD}
For each of these issue keys, call getJiraIssue with expand "changelog": ${keys.join(", ")}.
In each issue's changelog, find the most recent history entry whose status field changed TO the issue's current status. Use that entry's "created" timestamp. If no such entry exists (the card was created directly in its current status), use the issue's own "created" field instead.
Return ONLY {"exact":[[key,thatTimestampISO]]}, one entry per key above.
${TAIL}`;
  const data = await ask(prompt);
  const out = {};
  (data.exact || []).forEach(([k, iso]) => { out[k] = iso; });
  return out;
}

function analyse(a, b, c, exact) {
  const counts = a.counts || {};
  const cards = (a.live || []).map((r) => ({
    key: r[0], status: r[1], who: r[2] || "Unassigned", due: r[3] || null, start: !!r[4],
  }));
  const s1 = new Set(b.s1 || []);
  const nodesc = new Set(b.nodesc || []);
  const lastComment = {};
  (c.latest || []).forEach((r) => { lastComment[r[0]] = { by: r[1], at: r[2] }; });
  const ageDays = (iso) => (iso ? Math.round((Date.now() - new Date(iso).getTime()) / 864e5) : null);
  const overdueBy = (d) => (d ? Math.floor((Date.now() - new Date(d + "T23:59:59").getTime()) / 864e5) : -1);
  const QUIET_AFTER = 2;   // days without a word before a card counts as quiet

  const owed = new Set(cards.filter((x) => {
    const lc = lastComment[x.key];
    return UAT.includes(x.status) && lc && REVIEWERS.includes(lc.by) && (ageDays(lc.at) || 0) >= 1;
  }).map((x) => x.key));

  const flags = {
    noReply: cards.filter((x) => {
      const lc = lastComment[x.key];
      return UAT.includes(x.status) && lc && REVIEWERS.includes(lc.by) && (ageDays(lc.at) || 0) >= 1;
    }).map((x) => ({ ...x, why: `${lastComment[x.key].by} asked ${ageDays(lastComment[x.key].at)}d ago, no reply` })),
    /* A card can't be waiting on both sides — owing the reviewer a reply wins. */
    waitingOnReviewer: cards.filter((x) => {
      const lc = lastComment[x.key];
      if (owed.has(x.key)) return false;
      return UAT.includes(x.status) && lc && !REVIEWERS.includes(lc.by) && (ageDays(lc.at) || 0) >= 3;
    }).map((x) => ({ ...x, why: `${lastComment[x.key].by} asked ${ageDays(lastComment[x.key].at)}d ago, reviewer hasn't come back` })),
    parked: cards.filter((x) => UAT.includes(x.status) && exact[x.key] && ageDays(exact[x.key]) >= 2)
      .map((x) => {
        const d = ageDays(exact[x.key]);
        return { ...x, why: `no status change in ${d} day${d === 1 ? "" : "s"}`, _days: d };
      })
      .sort((x, y) => y._days - x._days),
    overdue: cards.filter((x) => overdueBy(x.due) > 0)
      .sort((x, y) => overdueBy(y.due) - overdueBy(x.due))
      .map((x) => ({ ...x, why: `due ${x.due}, ${overdueBy(x.due)} days ago` })),
    noBrief: cards.filter((x) => x.status === "2) Development" && nodesc.has(x.key) && s1.has(x.key))
      .map((x) => ({ ...x, why: "in Development a day or more with an empty description" })),
    quiet: cards.filter((x) => {
      if (!["2) Development", "3) QA"].includes(x.status)) return false;
      const lc = lastComment[x.key];
      if (!lc) return s1.has(x.key);              // never a word, and not moving
      return (ageDays(lc.at) || 0) >= QUIET_AFTER;
    }).map((x) => {
      const lc = lastComment[x.key];
      return { ...x, why: lc ? `last update ${ageDays(lc.at)} days ago, from ${lc.by}` : "no comment at all" };
    }),
    noDates: cards.filter((x) => !x.due || !x.start)
      .map((x) => ({ ...x, why: "no " + [!x.start && "start date", !x.due && "due date"].filter(Boolean).join(" or ") })),
  };
  const people = {};
  Object.values(flags).flat().forEach((f) => { people[f.who] = (people[f.who] || 0) + 1; });
  return normalise({
    counts, flags,
    stale14: flags.parked.filter((f) => f._days >= 14).map((f) => f.key),
    inFlight: cards.length, at: new Date().toISOString(),
    people: Object.entries(people).sort((x, y) => y[1] - x[1]),
  });
}

function Spark({ values, colour }) {
  if (!values || values.length < 2) return null;
  const top = Math.max(...values, 1), w = 120, h = 28, step = w / (values.length - 1);
  const pts = values.map((v, i) => `${(i * step).toFixed(1)},${(h - (v / top) * (h - 4) - 2).toFixed(1)}`).join(" ");
  return <svg viewBox={`0 0 ${w} ${h}`} className="w-24 h-7"><polyline points={pts} fill="none" stroke={colour} strokeWidth="2" strokeLinejoin="round" /></svg>;
}

function Section({ title, blurb, rows, history, colour, stale14 }) {
  rows = rows || [];
  stale14 = stale14 || [];
  return (
    <section className="bg-white border border-slate-200 rounded mb-5 px-6 py-5">
      <div className="flex justify-between items-start gap-6">
        <div>
          <h2 className="text-base font-semibold flex items-center gap-2 m-0">
            {title}
            <span className="text-xs font-semibold rounded-full px-2 py-0.5"
              style={{ background: rows.length ? "#B01238" : "#DEE3E9", color: rows.length ? "#fff" : "#5D6C7B" }}>
              {rows.length}
            </span>
          </h2>
          <p className="text-slate-500 text-sm m-0 mt-1">{blurb}</p>
        </div>
        <Spark values={history} colour={colour} />
      </div>
      {rows.length === 0 ? (
        <p className="text-slate-500 text-sm mt-4 mb-0">Nothing to chase here.</p>
      ) : (
        <table className="w-full mt-4 text-sm border-collapse">
          <thead>
            <tr className="text-slate-500 text-xs">
              <th className="text-left font-semibold pb-2 pr-3 border-b border-slate-200">Card</th>
              <th className="text-left font-semibold pb-2 pr-3 border-b border-slate-200">Now with</th>
              <th className="text-left font-semibold pb-2 pr-3 border-b border-slate-200">Stage</th>
              <th className="text-left font-semibold pb-2 border-b border-slate-200">What's wrong</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => (
              <tr key={r.key} style={stale14.includes(r.key) ? { color: "#B01238" } : undefined}>
                <td className="py-2 pr-3 border-b border-slate-100 font-semibold whitespace-nowrap">
                  <a href={`${SITE}/browse/${r.key}`} target="_blank" rel="noreferrer" className="text-blue-700">{r.key}</a>
                </td>
                <td className="py-2 pr-3 border-b border-slate-100 whitespace-nowrap">
                  {r.who}<span className="ml-2 text-xs text-slate-400">{ROLES[r.who] || "unlisted"}</span>
                </td>
                <td className="py-2 pr-3 border-b border-slate-100 whitespace-nowrap">{r.status}</td>
                <td className="py-2 border-b border-slate-100">{r.why}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </section>
  );
}

export default function SprintMonitor() {
  const [data, setData] = useState(null);
  const [history, setHistory] = useState([]);
  const [step, setStep] = useState(null);
  const [error, setError] = useState(null);

  useEffect(() => {
    (async () => {
      try {
        const saved = await window.storage.get(STORE_KEY);
        const snaps = JSON.parse(saved.value) || [];
        setHistory(snaps);
        const last = snaps[snaps.length - 1];
        if (last && last.v === SCHEMA) setData(normalise(last.view));
      } catch (e) { /* first run, nothing saved */ }
    })();
  }, []);

  async function refresh() {
    setError(null);
    const results = [];
    for (let i = 0; i < STEPS.length; i++) {
      setStep(`Reading ${STEPS[i].label}… (${i + 1} of ${STEPS.length + 1})`);
      try {
        results.push(await ask(STEPS[i].prompt));
      } catch (e) {
        setError(`Failed while reading ${STEPS[i].label}. ${e.message}`);
        setStep(null);
        return;
      }
    }
    const uatKeys = (results[0].live || [])
      .filter((r) => UAT.includes(r[1]))
      .map((r) => r[0]);
    let exact = {};
    setStep(`Reading exact ages for ${uatKeys.length} UAT card(s)… (${STEPS.length + 1} of ${STEPS.length + 1})`);
    try {
      exact = await fetchExactAges(uatKeys);
    } catch (e) {
      setError(`Read the sprint fine, but couldn't get exact UAT ages. ${e.message}`);
      setStep(null);
      return;
    }
    const view = analyse(results[0], results[1], results[2], exact);
    setData(view);
    const snaps = [...history, {
      at: view.at, v: SCHEMA,
      flags: Object.fromEntries(Object.entries(view.flags).map(([k, v]) => [k, v.length])),
      view,
    }].slice(-30);
    setHistory(snaps);
    try { await window.storage.set(STORE_KEY, JSON.stringify(snaps)); } catch (e) { /* trends only */ }
    setStep(null);
  }

  const trend = (rule) => history.map((s) => s.flags?.[rule] ?? 0);
  const total = data ? Object.values(data.counts).reduce((a, b) => a + b, 0) : 0;

  async function forget() {
    try { await window.storage.delete(STORE_KEY); } catch (e) { /* nothing saved */ }
    setHistory([]); setData(null); setError(null);
  }

  return (
    <div className="bg-slate-50 min-h-screen p-6" style={{ fontVariantNumeric: "tabular-nums" }}>
      <div className="max-w-4xl mx-auto">
        <header className="flex justify-between items-start gap-6 mb-7">
          <div>
            <h1 className="text-2xl font-semibold tracking-tight m-0">{SPRINT}</h1>
            <p className="text-slate-500 text-sm m-0 mt-1">
              {data ? `${total} cards in ${PROJECT} · ${data.inFlight} still in flight · read ${new Date(data.at).toLocaleString()}`
                : "No data yet. Refresh to read the sprint."}
              {history.length > 1 ? ` · ${history.length} snapshots kept` : ""}
            </p>
          </div>
          <button onClick={refresh} disabled={!!step}
            className="bg-slate-900 text-white text-sm font-semibold rounded px-4 py-2 disabled:opacity-50 whitespace-nowrap">
            {step ? "Reading…" : "Refresh"}
          </button>
        </header>

        {step && <div className="bg-white border border-slate-200 rounded px-5 py-4 mb-5 text-sm text-slate-600">{step}</div>}

        {error && (
          <div className="bg-red-50 border border-red-200 rounded px-5 py-4 mb-5 text-sm">
            <p className="text-red-800 font-semibold m-0">Couldn't read the sprint</p>
            <p className="text-red-700 m-0 mt-1">{error}</p>
            <p className="text-red-600 m-0 mt-2 text-xs">Refreshing again usually works. The message above says which step broke.</p>
          </div>
        )}

        {data && (
          <>
            <section className="mb-10">
              <div className="flex h-11 rounded overflow-hidden bg-slate-200">
                {ORDER.filter((s) => data.counts[s]).map((s) => (
                  <span key={s} title={`${s}: ${data.counts[s]}`}
                    className="flex items-center justify-center text-white text-sm font-semibold"
                    style={{ width: `${(data.counts[s] / total) * 100}%`, background: COLOUR[s] }}>
                    {data.counts[s] / total > 0.07 ? data.counts[s] : ""}
                  </span>
                ))}
              </div>
              <ol className="grid grid-cols-2 sm:grid-cols-4 gap-x-5 gap-y-1 mt-4 p-0 list-none">
                {ORDER.filter((s) => data.counts[s]).map((s) => (
                  <li key={s} className="flex items-baseline gap-2">
                    <span className="w-2 h-2 rounded-full shrink-0" style={{ background: COLOUR[s] }} />
                    <span className="font-semibold">{data.counts[s]}</span>
                    <span className="text-slate-500 text-sm">{s}</span>
                  </li>
                ))}
              </ol>
            </section>

            {data.people.length > 0 && (
              <section className="bg-white border border-slate-200 rounded mb-5 px-6 py-5">
                <h2 className="text-base font-semibold m-0">Cards per person</h2>
                <p className="text-slate-500 text-sm m-0 mt-1">
                  Current holder, not the developer who built it — UAT cards sit with the reviewer.
                </p>
                <table className="w-full mt-4 text-sm border-collapse">
                  <tbody>
                    {data.people.map(([who, n]) => (
                      <tr key={who}>
                        <td className="py-2 border-b border-slate-100">{who}</td>
                        <td className="py-2 border-b border-slate-100 text-slate-500 text-xs">{ROLES[who] || "unlisted"}</td>
                        <td className="py-2 border-b border-slate-100 text-right font-semibold">{n}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </section>
            )}

            <Section title="Past its due date" colour="#B01238" stale14={data.stale14}
              blurb="Still in flight with the due date behind us. Worst first."
              rows={data.flags.overdue} history={trend("overdue")} />
            <Section title="Waiting on a reply" colour="#B01238" stale14={data.stale14}
              blurb="The newest comment on a UAT card is from a reviewer, over a day ago."
              rows={data.flags.noReply} history={trend("noReply")} />
            <Section title="Waiting on the reviewer" colour="#6A5AB8" stale14={data.stale14}
              blurb="In UAT where the team asked a question three days ago or more and the reviewer hasn't replied."
              rows={data.flags.waitingOnReviewer} history={trend("waitingOnReviewer")} />
            <Section title="Parked in UAT" colour="#6A5AB8" stale14={data.stale14}
              blurb="In UAT with no status change for two days or more, oldest first. Red rows are 14+ days."
              rows={data.flags.parked} history={trend("parked")} />
            <Section title="Being built with no brief" colour="#C2410C" stale14={data.stale14}
              blurb="In Development for a day or more with the description still empty — nothing written down about what it should do."
              rows={data.flags.noBrief} history={trend("noBrief")} />
            <Section title="No word in two days" colour="#2F6FB8" stale14={data.stale14}
              blurb="In Development or QA with no comment for two days, so nobody knows where it stands."
              rows={data.flags.quiet} history={trend("quiet")} />
            <Section title="Started without dates" colour="#1E8C86" stale14={data.stale14}
              blurb="Past To Do, but start date or due date is still empty."
              rows={data.flags.noDates} history={trend("noDates")} />
          </>
        )}

        <p className="text-slate-400 text-xs mt-6">
          Parent cards only, sub-tasks excluded. Snapshots are saved in this artifact and build the
          trend lines; they're visible only to you.
          {history.length > 0 && (
            <button onClick={forget} className="underline ml-1">Clear saved snapshots</button>
          )}
        </p>
      </div>
    </div>
  );
}
