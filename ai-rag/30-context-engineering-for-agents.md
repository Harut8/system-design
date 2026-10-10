# 30 — context engineering for agents

> **Prerequisites:** [`06-context-engineering.md`](06-context-engineering.md) (single-prompt budgets,
> lost-in-the-middle §5, compaction §7, caching §12; its §15 is a one-page sketch of agent context,
> this chapter is the long version), [`22-agent-orchestration-patterns.md`](22-agent-orchestration-patterns.md)
> (the loop and orchestrator-worker, treated here as a context-isolation device),
> [`24-tool-calling-and-enterprise-integration.md`](24-tool-calling-and-enterprise-integration.md)
> (this chapter is about what each tool costs in tokens, not in risk), and
> [`25-memory-and-state-management.md`](25-memory-and-state-management.md) (long-term memory; §8 here
> covers only in-loop notes). Useful: [`05-query-understanding.md`](05-query-understanding.md) (§5.6
> agentic search), [`23-multi-llm-model-gateway.md`](23-multi-llm-model-gateway.md), and the lab
> [`labs/tool-registry/`](labs/tool-registry/README.md) used in §3 and Lab 2.

---

## Contents

0. [Start here — the whole chapter in plain words](#start-here--the-whole-chapter-in-plain-words)
1. [Context as a finite attention budget](#1-context-as-a-finite-attention-budget)
2. [The tool-definition tax](#2-the-tool-definition-tax)
3. [Tool search and deferred loading](#3-tool-search-and-deferred-loading)
4. [Writing tools that are cheap to use](#4-writing-tools-that-are-cheap-to-use)
5. [Code execution over tool calls ("code mode")](#5-code-execution-over-tool-calls-code-mode)
6. [Sub-agents for context isolation](#6-sub-agents-for-context-isolation)
7. [Agent Skills and progressive disclosure](#7-agent-skills-and-progressive-disclosure)
8. [Long-horizon techniques: clearing, compaction, notes](#8-long-horizon-techniques-clearing-compaction-notes)
9. [Just-in-time vs up-front retrieval](#9-just-in-time-vs-up-front-retrieval)
10. [Prompt-cache-aware agent loops](#10-prompt-cache-aware-agent-loops)
11. [The budget model: 50 turns, with and without](#11-the-budget-model-50-turns-with-and-without)
12. [Putting it together: a decision procedure](#12-putting-it-together-a-decision-procedure)
13. [Anti-patterns](#13-anti-patterns)
14. [Interview questions](#14-interview-questions)
15. [Lab exercises](#15-lab-exercises)
16. [Real-world cases](#16-real-world-cases)
17. [Sources](#sources)

---

## Start here — the whole chapter in plain words

**The problem.** A chatbot answers once. An agent works: it reads a file, runs a search, calls an
API, reads the answer, decides what to do next, and repeats — often dozens of times. The model has
no memory between calls, so on every step your code re-sends *everything so far*: the instructions,
the description of every tool, and every result of every earlier step. That pile only grows. Two
bad things follow. It gets expensive, because you pay for the whole pile every step. And the model
gets worse at using it, because a huge pile is harder to pay attention to than a small, relevant
one.

**A real-world example.** A coding agent has connected five tool servers (code hosting, chat,
error tracking, dashboards, logs). Anthropic's tool-search documentation says a similar
multi-server setup "can consume ~55k tokens in definitions before Claude does any work". The user
types one line: "why did last night's deploy fail?" The agent has already spent 55,000 tokens of
its window just on the *menu* of things it could do, and will re-send that menu on every one of
its 30 steps. Then step 3 greps a log and pastes back 12,000 tokens of output, of which 40 tokens
matter. By step 20, the window is mostly stale log lines.

This chapter is the set of habits that prevent that:

1. **Show only the menu items you need** (§3 tool search; §7 skills).
2. **Write tools whose answers are small and self-explanatory** (§4).
3. **Do the heavy lifting somewhere the model doesn't have to read** (§5 code execution; §6 sub-agents).
4. **Throw away what is stale; write down what matters** (§8).
5. **Fetch on demand rather than preloading** (§9).
6. **Don't reshuffle the top of the prompt**, or the provider's cache stops giving you a discount (§10).

| Term | Plain meaning | Everyday analogy |
|---|---|---|
| Context window | all tokens the model sees on one call | a desk: finite surface, everything on it is "in view" |
| Context rot | accuracy falls as the window fills, even below the limit | a desk so cluttered you stop noticing the one page you need |
| Tool definition tax | tokens spent describing tools on every call | printing the whole restaurant menu on every order slip |
| Tool search / deferred loading | tools are searchable; only matches enter the window | a menu with an index; you look up "dessert" only when you want dessert |
| Code mode | the model writes code that calls tools; big intermediate data stays in the sandbox | giving an assistant a spreadsheet to filter, not reading it aloud |
| Sub-agent | a worker run in its own fresh window that returns a short summary | sending an intern to read 40 papers and report in one page |
| Skill | a folder of instructions + scripts, with only its name and description pre-loaded | a recipe binder where only the spine label is on the shelf |
| Context editing | automatically clear old tool results from the window | clearing yesterday's papers off the desk |
| Compaction | summarize the history and continue from the summary | meeting minutes replacing the transcript |

### Symbols and parameters used in this chapter

| Symbol | What it means | Value in the worked examples | Simple example |
|---|---|---|---|
| `T` | number of turns (model calls) in a trajectory | 50 | 50 tool-using steps |
| `S` | system prompt tokens | 3,000 | role, rules, formatting |
| `N`, `d` | number of tools, tokens per tool definition | 120, 450 | `N·d = 54,000` |
| `U` | first user message tokens | 200 | one paragraph task |
| `o` | assistant tokens per turn (tool call + short text) | 300 | `{"name":"grep",...}` |
| `r` | tool result tokens per turn | 3,000 | a search result page |
| `F` | fixed prefix = `S + N·d + U` | 57,200 | everything before turn 1's output |
| `C_t` | input tokens on turn `t` | `F + (t−1)(o+r)` | grows linearly |
| `Σ C_t` | total input tokens billed over the trajectory | 6,902,500 (naive) | the quadratic-in-`T` bill |
| `k` | recent tool results kept verbatim by context editing | 3 | the default `keep` is 3 tool uses |
| `σ` | tokens left behind by a cleared result (placeholder) | 30 | "[cleared]" |
| `W` | model context window | 200,000 | |
| cache read / write multipliers | cost of a cached token vs a normal input token | 0.10× / 1.25× (5-minute write) | published list multipliers; check the current pricing page |

**Read this first about the numbers.** The token counts in §11 and the labs are *arithmetic
examples under stated assumptions*, not measurements of any product. Figures attributed to
Anthropic (for example 150,000 → 2,000 tokens, or 49% → 74%) are quoted as stated in the cited
post, in the context in which they report them, and should not be assumed to transfer to your
workload. Where an OpenAI or Google equivalent of a technique exists, this chapter does not
describe it, because it was not verified against those vendors' primary docs for this edition.

---

## 1. Context as a finite attention budget

> **In plain words.** A model can *accept* 200,000 tokens, but that does not mean it *uses* them
> equally well. The more you put in, the more the model has to pick the right needle out of, and
> the more likely it is to pick a wrong one. So every token in the window should earn its place.
>
> **Real-world example.** An agent with a 12,000-token window of only the relevant files and the
> last three results outperforms (in practice, and in the research below) the same agent with a
> 150,000-token window containing everything it has ever seen — even though both fit.

### 1.1 What the sources actually say

Anthropic's engineering post "Effective context engineering for AI agents" defines the field as
"the set of strategies for curating and maintaining the optimal set of tokens (information) during
LLM inference", explicitly including everything beyond the prompt text (tools, message history,
external data). It describes **context rot** as the pattern where, as the number of tokens in the
window increases, "the model's ability to accurately recall information from that context
decreases", and says this applies across models, though some degrade more gently than others. It
frames the cause as a finite **attention budget** — each new token depletes it — so that "good
context engineering means finding the smallest possible set of high-signal tokens that maximize the
likelihood of some desired outcome."

The term "context rot" comes from Chroma's technical report, *Context Rot: How Increasing Input
Tokens Impacts LLM Performance* (Hong, Troynikov, Huber; published 14 July 2025). The report evaluates 18 LLMs including GPT-4.1, Claude 4, Gemini 2.5 and
Qwen3 models on deliberately simple tasks (a LongMemEval-based conversational QA task and a
synthetic task of replicating repeated words) and finds that performance becomes increasingly
unreliable as input length grows, even on tasks that are trivial at short length. Treat that as
the *direction* of the effect; do not extract a numeric threshold from it.

## 2. The tool-definition tax

> **In plain words.** Every tool the agent *could* use has to be described to the model, in tokens,
> on *every* call. With 5 tools that is a rounding error. With 100+ it is most of your window, and
> it also confuses the model about which one to pick.
>
> **Real-world example.** Anthropic's "Advanced tool use" post describes a five-server setup of
> 58 tools "consuming approximately 55K tokens before the conversation even starts", and says
> they have seen tool definitions consume 134K tokens before optimization.

### 2.1 Two separate costs

The tool-search documentation names both: loading every definition up front causes

1. **Context bloat.** "A typical multiserver setup (GitHub, Slack, Sentry, Grafana, and Splunk) can
   consume ~55k tokens in definitions before Claude does any work." Tool search "typically reduces
   this by over 85 percent, loading only the 3–5 tools Claude needs for a given request."
2. **Selection accuracy.** "Claude's ability to pick the right tool degrades once you exceed 30–50
   available tools." Anthropic's "Advanced tool use" post reports MCP-benchmark accuracy with Tool
   Search enabled: "Opus 4 improved from 49% to 74%, and Opus 4.5 improved from 79.5% to 88.1%."
   (These are Anthropic's own evaluations on their MCP setup; they show direction and rough
   magnitude, not what your catalog will do.)

### 2.2 The arithmetic

`tax per run = N·d·T` input tokens (before caching). With `N = 120`, `d = 450` (a mid-sized
definition with description, a few typed parameters and one example), `T = 50`:

```
N·d       = 120 × 450         =     54,000 tokens in the prefix
N·d·T     = 54,000 × 50       =  2,700,000 input tokens billed for definitions alone
```

Prompt caching ([`06`](06-context-engineering.md) §12, §10 below) cuts the *price* of these
tokens but not their *presence*: they still occupy 27% of a 200K window and they still dilute
attention. Caching fixes the bill, not the rot.

How large is `d` in practice? [`06`](06-context-engineering.md) §15.1 gives rough ranges (50–100
tokens for a one-or-two-parameter function, 200–500 for a nested schema, 500–1,000 with examples).
Count your own: serialize the tool list to JSON and run it through your provider's token counter
(`POST /v1/messages/count_tokens` on the Anthropic API accepts a `tools` list).

## 3. Tool search and deferred loading

> **In plain words.** Instead of printing all 120 tool descriptions in the prompt, give the model
> one tool, "search for tools". When it needs a capability it searches, and only the 3–5 matching
> tool descriptions are added to the conversation.
>
> **Real-world example.** The agent in §2 starts with 4 frequently used tools plus the search tool
> (about 2,300 tokens in our assumptions instead of 54,000). At step 6 it needs to open a ticket;
> it searches "create ticket jira", the 5 best matches are loaded, it picks one and calls it.

### 3.1 The Anthropic API implementation (as documented)

Details below are from the Anthropic "Tool search tool" documentation (fetched for this edition).

- Two server-side variants: `tool_search_tool_regex_20251119` (the model writes Python `re.search()` patterns,
  max 200 characters) and `tool_search_tool_bm25_20251119` (natural language, max 500 characters). Both search
  tool names, descriptions, argument names and argument descriptions.
- You still send **every** definition in `tools` on every request and set `defer_loading: true` on those that
  should not enter the context. At least one tool (normally the search tool) must be non-deferred (all deferred
  is a 400); the docs recommend keeping 3–5 hot tools non-deferred.
- A search returns `tool_reference` blocks (5 by default; the model may set `limit`, 1 to 10,000), which the API
  expands into full definitions. Maximum 10,000 deferred tools per request. For MCP-connector tools set
  `defer_loading` on the `mcp_toolset` `default_config`.
- **Custom search:** your own tool returns a normal `tool_result` whose content is `{"type": "tool_reference",
  "tool_name": "..."}` blocks for tools present in the top-level `tools`.
- Use it, per the docs, at 10+ tools, definitions over ~10k tokens, falling accuracy, or multiple MCP servers
  (200+ tools); skip it under 10 tools or ~100 tokens of definitions. Index-design tips: prefix names by
  service/resource, put task keywords in descriptions, describe the available categories in the system prompt,
  and monitor what gets discovered.

```python
# Shape of a request (see the docs for the current model IDs).
tools = [
    {"type": "tool_search_tool_bm25_20251119", "name": "tool_search_tool_bm25"},
    {"name": "github_list_pull_requests", "description": "...", "input_schema": {...}},  # always loaded
    {"name": "jira_create_ticket", "description": "...", "input_schema": {...},
     "defer_loading": True},
    # ... 100 more with defer_loading True
]
```

### 3.2 Why this does not break the prompt cache

The caching rules matter more than they look. The documentation states that deferred tools "are not
included in the system-prompt prefix"; when one is discovered, the API "appends a `tool_reference`
block inline in the conversation, then expands it". "The prefix is untouched, so prompt caching is
preserved." Contrast the naive alternative — your code rewriting the `tools` list as the agent's
needs change — which, per the cache-invalidation table (§10.1), invalidates the entire cache
(tools, system, messages). One detail: a tool with `defer_loading: true` cannot also carry
`cache_control` (400); put the breakpoint on a non-deferred tool.

### 3.3 Building your own: the registry as a search index

- **Authorization before ranking.** `DiscoveryService.search` filters with `_is_allowed` *before* scoring, so an
  agent cannot infer unusable tools from a truncated top-K and is never crowded out by them.
- **Health and dependencies.** Circuit-broken tools are demoted; `SearchResult.dependencies`
  (`REQUIRES_OUTPUT_OF`) lets the agent load a chain in one search.
- **What it lacks for context engineering.** Its scorer is `difflib.SequenceMatcher` plus a popularity prior
  (semantic similarity is stubbed). You want BM25, optional embeddings, and a *detail level* in the response
  (Anthropic's code-mode post describes a `search_tools` tool with name-only, name+description, or full-schema
  detail). Lab 2 builds a BM25 index over `_collect_candidates()` and `_is_allowed()`.

## 4. Writing tools that are cheap to use

> **In plain words.** A tool is an interface for a model, and the model pays for every character it
> reads back. Short, well-named tools with small default answers and clear error messages keep both
> the menu and the replies small.
>
> **Real-world example.** A "find Slack user" tool returns the raw API JSON: 206 tokens with ids,
> timestamps, thread ids. Anthropic's example shows the same answer in a concise form takes 72.

Source: Anthropic, "Writing effective tools for agents". Every number below is as the post states
it.

### 4.1 Namespacing and consolidation

The post advises grouping related tools under common prefixes — by service (`asana_search`,
`jira_search`) or by resource (`asana_projects_search`, `asana_users_search`) — so the agent picks
the right tool, and notes that prefix vs suffix naming had a "non-trivial" effect on their
evaluations and the best choice varies by LLM, so test your own scheme. The tool-registry lab's
`namespace.name` references (`payments.issue_refund`, `crm.find_customer`) and glob-scoped
`allowed_tool_patterns` (`crm.*`) are the same idea at registry level; the wire name the model
sees is usually `payments_issue_refund` (flat, because most providers restrict tool-name
characters; check your provider).

Consolidate where the *job* is one thing: instead of `list_users`, `list_events`, `create_event`,
the post's example is a single `schedule_event` that finds availability and schedules, because
agents tend to waste context walking multi-step chains an API-shaped tool set forces on them.
(Consolidation trades definition size for ambiguity inside one tool; keep parameters few and
unambiguous.)

### 4.2 Token-efficient responses

- **`response_format` parameter.** The post's example is an enum with `DETAILED` and `CONCISE`; in
  their Slack example the detailed response was 206 tokens and the concise one 72 ("~⅓ of the
  tokens"). The concise form drops identifiers the agent does not need *for this step* but keeps
  the ones needed to ask for more (the detailed form is one more call away).
- **Pagination, filtering, truncation with guidance.** For Claude Code, the post says tool
  responses are restricted to 25,000 tokens by default. A truncation message should *steer*: "result
  truncated at 25,000 tokens; narrow with `path=` or `query=`", pairing the limit with advice
  toward several small targeted searches rather than one broad one.
- **Return meaning, not plumbing.** Prefer names and human-readable fields over opaque UUIDs
  where the agent must reason about them; the post recommends resolving identifiers to
  semantically meaningful language where possible, with ids available through the detailed format.



### 4.4 Descriptions are prompts

The post says to write descriptions the way you would explain the tool to a new team member
(specialized query formats, niche terminology, relationships between resources), to name
parameters unambiguously (`user_id` not `user`), and that small description refinements can have
large effects; it reports a case where Claude was appending "2025" to a web-search query until the
description was fixed, and says refining tool descriptions contributed to Claude Sonnet 3.5's
state-of-the-art SWE-bench Verified result at the time. Two corollaries for the budget: spend
description tokens on *disambiguation and when-to-use*, not restating the schema; and with tool
search (§3) the description is also your **search keywords**, so it has a second job.

## 5. Code execution over tool calls ("code mode")

> **In plain words.** With ordinary tool calling, the model reads every result. If a task is
> "download this 2-hour meeting transcript from the drive and put it in the CRM", the whole
> transcript passes *through the model twice*. With code mode the model writes a few lines of code
> that move the data directly; the model only sees what the code prints.
>
> **Real-world example.** "Find the 5 overdue invoices among 10,000 rows." Tool-call style: the
> 10,000 rows enter the window. Code style: the code filters in the sandbox and the model sees
> 5 rows.

Source: Anthropic, "Code execution with MCP: building more efficient agents".

### 5.1 The two costs it removes

The post names them: (1) loading every tool definition up front, which "slows down agents and
increases costs" and, with thousands of tools, can mean "hundreds of thousands of tokens" before the
request is read; (2) intermediate results flowing through the model between calls — the example it
gives is a roughly 2-hour meeting transcript, which "could mean processing an additional 50,000
tokens", and it notes that copying large data between calls by hand also risks model error.

### 5.2 The pattern

1. Present MCP servers as a **code API** in the sandbox filesystem — the post's example layout is
   a tree like `./servers/google-drive/getDocument.ts`, one file per tool.
2. The agent **lists the directory and reads only the files it needs** (progressive disclosure of
   tool definitions). The post also describes a `search_tools` tool with a detail-level parameter
   (name only, name and description, or full schema) as an alternative or complement.
3. The agent **writes code** that calls those functions, loops, filters, joins and aggregates in the
   sandbox. Only what the code explicitly logs or returns is seen by the model; "intermediate
   results stay in the execution environment by default".
4. Agent can **persist** intermediate files (a CSV in a workspace folder) and **save reusable
   functions** — the post connects this to Skills (§7): a saved function plus a `SKILL.md`
   becomes a skill.





### 5.3 The number Anthropic reports, and how to read it

The post states that a workflow dropped "from 150,000 tokens to 2,000 tokens", a 98.7% saving in
time and cost. Read it as: *for the example workflow in that post* — a case dominated by large
intermediate data and many tool definitions — not as a general expected saving. The separate
"Advanced tool use" post gives a more conservative figure for the same idea (Programmatic Tool
Calling): average usage dropping "from 43,588 to 27,297 tokens, a 37% reduction on complex research
tasks". Use the second as a planning prior and the first as the best case.

### 5.5 Costs of code mode (be honest about them)

The post itself says running agent-generated code needs "secure sandboxing, resource limits, and
monitoring", adding operational overhead that direct tool calls avoid, and advises weighing token,
latency and composition benefits against it. Add:

| Cost | Detail |
|---|---|
| Security surface | arbitrary code generated from untrusted context; the sandbox is now the trust boundary ([`17`](17-safety-guardrails-and-prompt-injection.md), [`28`](28-agentic-security-owasp-and-mcp-threats.md)) |
| Debugging | errors are code errors; the model sees tracebacks (cap their size, §4) |
| Authorization | per-call approval gates ([`24`](24-tool-calling-and-enterprise-integration.md) §11) must be enforced inside the sandbox's tool wrappers, not by the model's say-so |
| Observability | the model's context no longer shows what happened; log the sandbox's tool calls ([`24`](24-tool-calling-and-enterprise-integration.md) §13) |
| Less fit | one-shot, small-output, single-tool tasks pay overhead for nothing |

Rule of thumb: use code mode when a task touches **more than a few tools, or any tool whose result
is large and only partly needed**; use direct tool calls for single, small, high-risk,
approval-gated actions.

---

## 6. Sub-agents for context isolation

> **In plain words.** Instead of one agent that reads 40 pages and carries all of them forever, a
> lead agent sends helpers. Each helper has its own clean window, does the reading, and reports back
> a short summary. The lead's window only grows by the summaries.
>
> **Real-world example.** A research question needs 12 web searches. A lead agent spawns 4
> sub-agents, each runs 3 searches and returns about 800 tokens. The lead's window grows by 3,200
> tokens, not by the 60,000 tokens of raw pages the helpers read.

### 6.1 What the sources say

Anthropic's context-engineering post describes sub-agent architectures as specialized sub-agents
handling "focused tasks with clean context windows", with the main agent coordinating and each
sub-agent returning only a condensed summary of its work. Anthropic's "How we built our multi-agent
research system" describes an orchestrator-worker design: a lead agent plans, spawns sub-agents that
search in parallel, and then synthesizes their results.

### 6.2 Anthropic's published numbers — and the multiplier

From the multi-agent research post:

- A multi-agent setup with Claude Opus 4 as lead and Claude Sonnet 4 sub-agents "outperformed
  single-agent Claude Opus 4 by 90.2% on our internal research eval".
- Token cost: "agents typically use about 4× more tokens than chat interactions, and multi-agent
  systems use about 15× more tokens than chats."
- On BrowseComp, "token usage by itself explains 80% of the variance" in performance; token usage,
  number of tool calls and model choice together explained 95%.

The correct reading is subtle and is a common interview trap: **sub-agents do not save tokens
overall; they spend more tokens in total in exchange for (a) parallelism, (b) a lead agent whose
window stays small and clean, and (c) more total reasoning capacity than one window can hold.** The
15× is a cost multiplier on the *whole system*; the isolation benefit is on the *lead's* window.
The post is explicit that this only pays off when the task is valuable enough to justify it.

### 6.3 When sub-agents help, and when they do not

| Situation | Sub-agents? | Why |
|---|---|---|
| Breadth-first research: many independent directions | Yes | parallelizable; each direction's raw reading is discarded |
| Heavy raw-data reading with a small answer (log triage, repo-wide scan) | Yes | the reading happens in a disposable window |
| Tightly coupled work where every step depends on the last (a code change across interdependent files) | Usually no | the post says domains where all agents must share the same context, or with many inter-agent dependencies, are "not a good fit for multi-agent systems today" and cites coding as having fewer parallelizable tasks |
| Low-value or latency-sensitive queries | No | 15× tokens, plus orchestration latency |
| A task one agent finishes in 10 turns with small results | No | overhead only |
| Context is full of stale material, not of a separable sub-task | No — use clearing/compaction (§8) | isolation is the wrong tool |

### 6.4 Designing the hand-off (where these systems actually fail)

- **Delegation brief.** A sub-agent starts empty: give it the objective, output format, tools/sources and
  task boundary. The research post reports early failures from vague delegation (duplicated work).
- **Condensed return.** Specify a schema and length cap: findings, evidence pointers, open questions,
  not transcripts. The post describes sub-agents writing outputs to external storage and passing back
  lightweight references.
- **Bound the fan-out:** cap sub-agents, turns and tokens per worker in the harness; the post reports
  over-investment in simple queries without a scale rule in the prompt.
- **Security.** The summary a worker returns is an injection channel into the lead
  ([`17`](17-safety-guardrails-and-prompt-injection.md)).



[`22`](22-agent-orchestration-patterns.md) covers the orchestration patterns themselves, and
[`21`](21-langgraph-deep-dive.md) the graph machinery; the point added here is that *the returned
summary is the only thing worth designing carefully*.

---

## 7. Agent Skills and progressive disclosure

> **In plain words.** A skill is a folder containing a how-to document (`SKILL.md`) and optionally
> scripts and reference files. Only its name and one-line description sit in the prompt. If the
> task matches, the agent opens the instructions; if those mention a big reference file or a
> script, it opens or runs that only when needed.
>
> **Real-world example.** A PDF skill: the prompt carries one line ("extract text and fill forms
> in PDFs"). When a PDF task arrives the agent reads the instructions. For form filling it runs a
> bundled script and never loads the script's source into the window.

### 7.1 The format (as published)

From Anthropic's "Equipping agents for the real world with Agent Skills": a skill is a directory
with a `SKILL.md` that "must start with YAML frontmatter that contains some required metadata:
`name` and `description`", followed by the instructions in the body. The post describes three
levels of **progressive disclosure**:

1. **Metadata** (`name`, `description`) of every installed skill is pre-loaded into the system
   prompt at startup — enough for the model to know when each skill applies.
2. **The `SKILL.md` body** is read only if the agent judges the skill relevant.
3. **Linked files** (more markdown, templates, scripts) are navigated "only as needed". Skills can
   also include code that the agent runs as a tool; the post's PDF example is a pre-written Python
   script that extracts form fields, which the agent can execute without loading the script or the
   PDF into context.

The post (with a 18 December 2025 update) states that Anthropic has published Agent Skills as an
**open standard** for cross-platform portability, linking to agentskills.io. The specification
text gives these constraints:

| Field | Required | Constraint (per the spec text) |
|---|---|---|
| `name` | yes | 1–64 chars; lowercase letters, digits, hyphens; no leading/trailing/consecutive hyphens; must match the parent directory name |
| `description` | yes | 1–1024 chars; what the skill does **and when to use it** |
| `license` | no | short license name or bundled file reference |
| `compatibility` | no | 1–500 chars; environment requirements |
| `metadata` | no | string-to-string map |
| `allowed-tools` | no | space-separated pre-approved tools; marked experimental, support varies |

and this budget guidance: metadata about 100 tokens per skill; the `SKILL.md` body under about
5,000 tokens recommended; keep `SKILL.md` under 500 lines; move detail into `scripts/`,
`references/`, `assets/`; keep references one level deep from `SKILL.md`.

```
pdf-forms/
├── SKILL.md            # frontmatter + "when to use" + steps
├── references/         # long docs, loaded only when SKILL.md says so
│   └── field-types.md
└── scripts/
    └── extract_fields.py   # executed, not read
```



### 7.3 Skills vs MCP vs system prompt vs tools

| Mechanism | What it is | Always in context | Best for | Weak at |
|---|---|---|---|---|
| System prompt | instructions in every call | all of it | invariants: role, safety rules, output format | anything procedure-specific; it is paid every turn |
| Tool (native/function) | a callable with a schema | its definition (unless deferred, §3) | actions with typed inputs/outputs | long procedural knowledge |
| MCP server | a protocol for exposing tools/resources/prompts from another process ([`26`](26-mcp-and-agent-protocols.md)) | tool definitions (unless deferred/code-mode) | connecting to live systems and data, shared across clients | carrying the "how we do things" know-how; large definition surface |
| Agent Skill | instructions + code + resources, disclosed progressively | name + description only | repeatable procedures, domain know-how, bundled scripts | live data access by itself (a skill may *call* tools or run scripts) |
| Sub-agent | a separate window with its own prompt | none in the lead | context isolation, parallel work | tight coupling; token cost (§6) |

They compose: MCP provides the *capability* (query the CRM), a skill provides the *procedure* (how
our team qualifies a lead, using that capability), and the system prompt holds only the invariants.

### 7.4 Security: a skill is code and instructions you load on trust

The post warns that "malicious skills may introduce vulnerabilities in the environment where
they're used", recommends installing skills only from trusted sources, and advises auditing
dependencies, bundled resources and instructions that reach external networks. Because progressive
disclosure means the body is loaded *because the model decided to*, a hostile description can
attract loading, and a hostile body is then instructions in the window. Pin versions, review diffs,
and treat `allowed-tools` as a request, not a grant (the spec marks it experimental). See
[`28`](28-agentic-security-owasp-and-mcp-threats.md).

---

## 8. Long-horizon techniques: clearing, compaction, notes

> **In plain words.** Eventually even a well-run task outgrows the window. Three tools handle it:
> automatically delete old tool outputs that are no longer needed (clearing), replace the whole
> history with a summary (compaction), and keep important facts in a file the agent can re-read
> (notes). Use all three, in that order of preference.
>
> **Real-world example.** An agent migrating 200 files: after file 40 the window is full of
> diffs for files 1–37. Clearing drops those diffs but keeps the "37 done: list" note in a file.
> At file 120 compaction replaces the remaining chatter with a one-page state summary.

The anchor point: [`06`](06-context-engineering.md) §7 and §8 cover summarization and conversation
history for RAG; [`25`](25-memory-and-state-management.md) §3–§4 cover trimming and summary memory.
This section covers the API-level mechanisms and the policy for tool-heavy loops.

### 8.1 The ladder: cheapest, least lossy first

| Rung | Technique | Lossiness | Cost | Trigger |
|---|---|---|---|---|
| 1 | Make results small at the source (§4, §5) | none | free | always |
| 2 | **Clear stale tool results** (context editing) | low (reproducible by re-calling the tool) | free; invalidates cache once | ~50% of window |
| 3 | **Notes / memory files** | none if written *before* it is needed | small | continuously |
| 4 | **Compaction** (summarize history) | medium (summary can drop a detail) | one model call | ~75–85% of window |
| 5 | **Sub-agent restart** with notes as hand-off | medium | spawn cost | when a sub-task finishes |

### 8.2 Context editing (clearing stale tool results)

Anthropic's context-editing docs (beta header `context-management-2025-06-27` at fetch time)
describe two server-side strategies, applied before the prompt reaches the model while the client
keeps the full history:

- `clear_tool_uses_20250919` — clears old tool results (optionally inputs). Parameters and
  defaults per the docs: `trigger` (default 100,000 input tokens; can be `input_tokens` or
  `tool_uses`), `keep` (default 3 tool uses — the most recent pairs are retained), `clear_at_least`
  (minimum tokens to clear per application; if the API cannot clear that much the strategy is not
  applied), `exclude_tools` (tool names never cleared), `clear_tool_inputs` (default `false`).
  Cleared results are replaced with placeholder text telling the model they were removed.
- `clear_thinking_20251015` — manages earlier `thinking` blocks (`keep`: `{"type":
  "thinking_turns","value":N}` or `"all"`; model-specific defaults; must be listed first when
  combined).
- The response reports `context_management.applied_edits` (for example `cleared_tool_uses` and
  `cleared_input_tokens`), and `count_tokens` returns both the post-edit `input_tokens` and
  `context_management.original_input_tokens`.

```json
"context_management": {
  "edits": [{
    "type": "clear_tool_uses_20250919",
    "trigger": {"type": "input_tokens", "value": 30000},
    "keep": {"type": "tool_uses", "value": 3},
    "clear_at_least": {"type": "input_tokens", "value": 5000},
    "exclude_tools": ["web_search"]
  }]
}
```

**Cache interaction (important).** Per the same docs, clearing tool results *invalidates cached
prefixes*, so `clear_at_least` exists to make each clearing worth the cache re-write cost; after
it, later requests can reuse the new prefix. So clearing should be **batched** (clear a lot, rarely)
rather than nibbling every turn — see §10.2 and the cost lines in §11.

**What to exclude.** Anything the agent cannot cheaply regenerate or that encodes a decision:
memory-tool calls, a plan or todo tool, results of expensive/irreversible calls. The docs pair
this with the memory tool: when context approaches the clearing threshold the model receives an
automatic warning to save important information to memory files first.

### 8.3 Compaction

Anthropic documents server-side compaction: the API summarizes older turns into a `compaction`
block that replaces them, so you need no summarizer of your own. As fetched for this edition, there
are two flavors (and the docs say both are beta; names and headers have been moving, so check the
current page before copying):

- **At a token threshold:** an edit `{"type": "compact_20260112", "trigger": {"type":
  "input_tokens", "value": 150000}}` inside `context_management.edits` (trigger default 150,000;
  minimum 50,000; `input_tokens` the only trigger type), beta header `compact-2026-01-12`. A
  `pause_after_compaction` flag (default `false`) returns with `stop_reason: "compaction"` so you can
  re-insert recent turns; an `instructions` string *replaces* the default summarization prompt; and
  `cache_control` on the `compaction` block caches the summary.
- **On demand:** you decide when, by sending a request; beta header `compact-2026-09-04` and a
  top-level `compaction` parameter, with options to keep recent turns verbatim and to compact in the
  background. The overview page says to prefer this one where available.
- The context-editing page states server-side compaction is the recommended primary strategy for
  most long-running conversations, with context editing for finer control.

**What a good summary keeps** (write this into `instructions` or your own prompt):

1. The user's goal and every explicit constraint ("don't touch prod").
2. Decisions made *and the reason*, so they are not re-litigated.
3. Current state: what is done, what is in progress, what is next.
4. Identifiers the agent will need again: file paths, ticket ids, branch names, error signatures.
5. Open questions and known dead ends (so they are not retried).

### 8.4 Structured note-taking and the memory tool

Anthropic's context-engineering post describes structured note-taking: the agent regularly writes
notes persisted outside the window and pulls them back in later, citing a game-playing agent that
keeps tallies and objectives across thousands of steps. Anthropic's memory tool makes it a
first-class capability (from the docs):

- Tool entry `{"type": "memory_20250818", "name": "memory"}`; it is **client-side**: the model
  requests file operations under `/memories` and *your* handler executes them against storage you
  control.
- Commands: `view` (directory listing or file, optional `view_range`), `create`, `str_replace`,
  `insert`, `delete`, `rename`.
- When the tool is present the API adds a system-prompt protocol telling the model to view its
  memory directory first and to assume interruption ("your context window might be reset at any
  moment"), recording progress as it goes.
- Security is your job: validate every path stays within `/memories` (the docs warn about
  `../` and URL-encoded traversal), cap file sizes, expire stale files, and strip sensitive data.
- It pairs with compaction: compaction keeps the active context small; memory preserves what must
  survive summarization. The docs also describe a multi-session pattern: an initializer session
  creates a progress log and feature checklist; each later session reads them first and updates
  them before ending; mark a feature complete only after end-to-end verification.

```python
# Minimal client-side handler: the part that matters is the path check.
from pathlib import Path
ROOT = Path("/var/agent/memories").resolve()

def safe(path: str) -> Path:
    p = (ROOT / path.removeprefix("/memories").lstrip("/")).resolve()
    if ROOT != p and ROOT not in p.parents:
        raise PermissionError(f"path escapes /memories: {path}")
    return p
```

## 9. Just-in-time vs up-front retrieval

> **In plain words.** Up-front retrieval fetches everything that might help before the model starts
> (classic RAG). Just-in-time retrieval gives the model *pointers* (file paths, search tools) and
> lets it fetch what it needs when it needs it. The first is fast and cheap per question; the
> second is flexible but costs more turns.
>
> **Real-world example.** "Fix the failing test in our monorepo." Up-front: embed the repo, retrieve
> 20 chunks, hope. JIT: the agent runs `grep`, `ls`, and reads three files, following references as
> it learns what is relevant.

### 9.1 What Anthropic's post says

The context-engineering post describes agents that "maintain lightweight identifiers (file paths,
stored queries, web links, etc.) and use these references to dynamically load data into context at
runtime using tools" instead of pre-processing everything up front. It notes that references carry
information of their own (a file named `test_utils.py` in a `tests/` folder means something that a
file in `src/core_logic/` does not) and that this enables *progressive disclosure*: the agent
assembles understanding layer by layer, keeping in working memory only what is needed. It also names
the trade-off, that runtime exploration is slower than retrieving pre-computed data, and that a
hybrid is common: some data retrieved up front for speed, with further exploration at the agent's
discretion. Tool search (§3), skills (§7) and the memory tool (§8.4) are the same principle applied
to tool definitions, procedures and notes.

## 10. Prompt-cache-aware agent loops

> **In plain words.** Providers give a big discount when the start of your prompt is byte-for-byte
> identical to the previous call. An agent loop re-sends the same start every turn, so it is the
> perfect customer for the discount — provided you never edit the start. Almost every technique in
> this chapter edits something, so the skill is knowing which edits are safe.
>
> **Real-world example.** An agent that re-sorts its tool list or inserts the current time into the
> system prompt every turn pays full price for 50,000 prefix tokens on every one of 50 turns.
> Moving the time to the end of the latest user message restores the discount.

[`06`](06-context-engineering.md) §12 covers how prompt caching works and its economics, and
[`23`](23-multi-llm-model-gateway.md) covers cache-aware routing. This section is the
agent-loop-specific rules.

### 10.1 What invalidates the cache (Anthropic documentation)

The cache follows a prefix hierarchy `tools` → `system` → `messages`; a change at one level
invalidates that level and everything after it. From the "Tool use with prompt caching" page:

| Change | Invalidates |
|---|---|
| Modifying tool definitions | entire cache (tools, system, messages) |
| Toggling web search or citations | system and messages caches |
| Changing `tool_choice` | messages cache |
| Changing `disable_parallel_tool_use` | messages cache |
| Toggling images present/absent | messages cache |
| Changing thinking parameters / `output_config.effort` | messages cache always; tool and system caches too on models that render the configuration ahead of them |

Place `cache_control: {"type": "ephemeral"}` on the **last tool** to cache the entire tool-definition
prefix up to it (for `mcp_toolset`, on the toolset entry itself). There is a limit of four
breakpoints per request. Check TTLs and prices on the current Prompt Caching page; this chapter's
cost model uses 0.10× for reads and 1.25× for 5-minute writes, as parameters you should replace
with your provider's current figures.

### 10.2 Rules for a cache-friendly agent loop

1. **Order from most stable to least stable:** tools → system prompt → pinned project context →
   conversation, with a breakpoint at the end of each stable region and one near the end of the
   conversation that moves forward each turn.
2. **Never reorder or regenerate the tool list.** Sort deterministically and serialize schemas with
   stable key order (the registry lab's `_schema_hash` uses canonical JSON for this reason).
3. **Add tools without touching the prefix:** deferred loading (§3.2). Rewriting `tools` per step is the
   cache-busting version of the right idea.
4. **No volatile data in the prefix** (timestamps, request ids, per-user state); append it to the newest message.
5. **Do not flip modes mid-session** (`tool_choice`, thinking/effort, parallel-tool flags); if you must
   vary `tool_choice`, put breakpoints before the variation point.
6. **Append-only history between edit events.** Mutating an earlier message invalidates everything after
   it, so clearing and compaction are discrete, batched events: trigger high, use `clear_at_least`.
7. **Keep the compaction summary cacheable** (`cache_control` on the `compaction` block, plus a breakpoint
   at the end of the system prompt).
8. **Sub-agents get their own caches:** give workers an identical shared prefix so launches within the TTL hit.

## 11. The budget model: 50 turns, with and without

> **In plain words.** Add up, turn by turn, how many tokens the model is sent. The naive agent's
> total grows like the *square* of the number of turns, because every turn re-sends everything
> before it. Each technique cuts a different term of the sum.
>
> **Real-world example.** With our assumptions, a naive 50-turn agent is sent 6.9 million input
> tokens and overflows a 200K window at turn 45. The same task with tool search, result clearing
> and code mode is sent 0.85 million and never exceeds 27K per call.

**These are computed examples under the assumptions below, not measurements.** Lab 1 (§15)
reproduces every number.

### 11.1 Assumptions

`T = 50` turns; `S = 3,000`; `U = 200`; `N = 120` tools at `d = 450` (so `N·d = 54,000`);
`o = 300` assistant tokens per turn; `r = 3,000` tool-result tokens per turn; `W = 200,000`.
So `F = S + N·d + U = 57,200`.

### 11.2 The naive trajectory

```
C_t = F + (t − 1)(o + r) = 57,200 + 3,300 (t − 1)
C_50 = 57,200 + 49 × 3,300 = 218,900            > W = 200,000   (first exceeds W at t = 45)
Σ C_t = 50 F + (o + r) × (0 + 1 + … + 49)
      = 50 × 57,200 + 3,300 × 1,225
      = 2,860,000 + 4,042,500 = 6,902,500
```

Two terms: the **prefix term** `T·F` (2.86M; 2.7M of it is tool definitions) is linear in turns;
the **history term** `(o+r)·T(T−1)/2` (4.04M) is quadratic. Tool search and skills attack the
first, clearing/code mode/sub-agents attack the second.

### 11.3 Each technique, modeled

| Technique | Model change | `C_50` | `Σ C_t` | vs naive `Σ` |
|---|---|---|---|---|
| Naive | none | 218,900 | 6,902,500 | 100% (overflows at turn 45) |
| Tool search | prefix tools = 500 (search tool) + (4 hot + up to 8 discovered) × 450; one new tool discovered about every 3 turns | 170,800 | 4,452,500 | 64.5% |
| Context editing | keep the last `k = 3` results; older ones become `σ = 30`-token placeholders | 82,280 | 3,691,930 | 53.5% |
| Code mode | model sees 300 tokens per step instead of 3,000 | 86,600 | 3,595,000 | 52.1% |
| Tool search + editing | both | 34,180 | 1,241,930 | 18.0% |
| All three | tool search + editing + code mode | 26,080 | 853,130 | 12.4% |

Sanity check of one row by hand (tool search, `t = 50`): tools = 500 + 12 × 450 = 5,900; history
= 49 × 3,300 = 161,700; plus `S + U = 3,200`; total 170,800. Context editing, `t = 50`: history =
49 × 300 (assistant text) + 3 × 3,000 (kept results) + 46 × 30 (placeholders) = 14,700 + 9,000 +
1,380 = 25,080; plus 54,000 + 3,200 = 82,280.

### 11.4 Adding the cache

Take the multipliers as parameters: cache read `0.10×`, 5-minute write `1.25×` the normal input
price (replace with your provider's current figures). The cost of a turn is `0.10 × (cached prefix) +
1.25 × (new tokens)`, with a full `1.25 × C_t` on turn 1 and on any cache miss. In base-token units:

| Scenario | Input cost (base-token units) | Note |
|---|---|---|
| Naive, no caching | 6,902,500 | |
| Naive, stable prefix, cached | 941,985 | 13.6% of uncached |
| Tool search, cached | 641,670 | prefix is small *and* cached |
| Naive but prefix changes every turn (cache miss each turn) | 8,628,125 | 1.25 × 6,902,500: worse than not caching, 9.2× the cached case |
| Tool search + clearing, clearing every turn (miss every turn) | 1,552,412 | the cost of nibbling every turn |

Takeaways: (1) caching is worth roughly an order of magnitude here and a cache-busting bug erases it
(§10); (2) tool search still helps *after* caching (941,985 → 641,670) and, unlike caching, also
protects accuracy and the window; (3) clearing is the one technique that fights the cache — do it in
batches (§10.2). Lab 1 describes a 10-line cost function to explore the batching trade-off for your
own numbers.

## 12. Putting it together: a decision procedure

Measure first (tool-def, history and result tokens per call; cache read/write tokens; context size at
failure: [`14`](14-agent-evaluation.md), [`06`](06-context-engineering.md) §16.6). Then: many tools →
§3/§4; big partly-used results → §4/§5; separable read-heavy work worth ~15× → §6; repeated
procedures → §7; long tasks → clear at ~50%, notes continuously, compact at ~75–85% (§8); evolving
sources → just-in-time (§9); audit cache hygiene (§10); re-run the trajectory suite after every change.

---

## 13. Anti-patterns

| Anti-pattern | Why it hurts | Fix |
|---|---|---|
| "The window is 1M tokens, so load everything" | context rot (§1) and the quadratic bill (§11) | budget and curate |
| Wrapping every REST endpoint as a tool | `N·d` explodes, near-duplicates, mis-selection | job-shaped tools (§4); deferral (§3) |
| Rewriting the `tools` list each turn to "load only relevant tools" | invalidates the whole cache (§10.1) | deferred loading or a stable superset |
| Timestamp / user name / request id at the top of the system prompt | cache miss on every call | put volatile data at the tail |
| Raw JSON/HTML/stack traces as tool results | noise repeated until cleared | concise formats, truncation with guidance, size caps |
| Clearing results every turn | a cache miss per turn (§11.4) | batch with a high trigger and `clear_at_least` |
| Summarizing with no pinned constraints | summary silently drops "don't touch prod" | pin invariants; eval compaction (§8.3) |
| Sub-agents for tightly coupled edits | duplicated/contradictory work at 15× cost | single agent plus notes |
| Sub-agent returns full transcripts | lead's window fills anyway | schema + length cap + references |
| Code mode with no sandbox limits and no tool-level approval gates | model-written code gets real authority | sandbox, resource limits, enforcement in wrappers (§5.5) |

---

## 14. Interview questions

**1. "Why does a 50-turn agent cost far more than 50 single calls?"** Every call re-sends prefix and
history: `Σ C_t = T·F + (o+r)·T(T−1)/2` (§11.2: 6.9M input tokens, 218,900-token final context in the
worked example). The prefix term is attacked by tool search and skills, the quadratic term by
clearing, code mode and sub-agents. Caching cuts price, not window pressure or rot.

**2. "What is context rot; does a bigger window fix it?"** Recall and reasoning accuracy decline as input
grows even below the limit (Anthropic's context post; Chroma's 18-model study: direction, not a
threshold). A bigger window raises the ceiling, not the attention budget.

**3. "150 tools across 6 MCP servers: what do you do?"** Quantify `N·d·T`; note both costs (tokens
and selection accuracy, which Anthropic documents as degrading past 30–50 tools); scope by principal,
consolidate duplicates, defer loading behind a search tool with 3–5 hot tools, namespace, keyword
descriptions, keep the prefix stable. Third-party tool descriptions are untrusted input.

**4. "Tool search vs rewriting the tool list per step?"** Rewriting `tools` invalidates the whole cache;
deferred loading appends a `tool_reference` inline and leaves the prefix untouched.

**5. "Do sub-agents save tokens?"** No: Anthropic reports about 4× (agents) and about 15× (multi-agent)
over chat. They protect the lead's window and add parallelism. Use for separable read-heavy work worth
the cost; avoid for tightly coupled work; design the brief and the condensed return; treat the return
as untrusted.

**6. "A cached prompt now costs 8× more. Why?"** Something in the prefix changed per request: tool order
or serialization, a timestamp, toggled `tool_choice`/thinking/effort, images present/absent, a flag
enabling web search, a tool schema edit, or clearing every turn. Check `cache_read_input_tokens`
vs `cache_creation_input_tokens`.

**7. "Compaction dropped a constraint. Prevent it."** Pin invariants outside the compactable region or
re-inject them; a summary prompt enumerating constraints, decisions, state, ids; a notes file as source
of truth; recent turns verbatim; an eval that forces compaction at turn N and asserts the constraint.

---

## 15. Lab exercises

All labs use Python 3 standard library; Labs 2 and 3 import the existing `labs/tool-registry/`
modules. Put the files in a scratch directory; adjust the `sys.path` line in Lab 2 to your clone's
path. Outputs shown are from running exactly this code.

### Lab 1 — simulate context growth and cost (`sim.py`)

Goal: reproduce §11 and explore parameters (`n_tools=30`, `result=800`, `keep_recent=10`, smaller `window`).

```python
from dataclasses import dataclass

@dataclass
class Cfg:
    turns: int = 50
    system: int = 3_000
    user: int = 200
    n_tools: int = 120
    tool_tokens: int = 450    
    out: int = 300            
    result: int = 3_000       
    defer_tools: bool = False
    always_loaded: int = 4    
    search_tool: int = 500    
    discovered_per_task: int = 8 
    clear_old_results: bool = False
    keep_recent: int = 3
    stub: int = 30            
    code_mode: bool = False
    code_result: int = 300    
    window: int = 200_000

def trajectory(c: Cfg):
    rows = []
    for t in range(1, c.turns + 1):
        if c.defer_tools:
            loaded = min(c.discovered_per_task, t // 3)
            tools = c.search_tool + (c.always_loaded + loaded) * c.tool_tokens
        else:
            tools = c.n_tools * c.tool_tokens
        res = c.code_result if c.code_mode else c.result
        hist = 0
        for i in range(1, t):             
            age = t - i                    
            r = res
            if c.clear_old_results and age > c.keep_recent:
                r = c.stub
            hist += c.out + r
        rows.append(c.system + tools + c.user + hist)
    return rows

def report(name, c):
    rows = trajectory(c)
    over = next((i + 1 for i, x in enumerate(rows) if x > c.window), None)
    print(f"{name:34s} final={rows[-1]:>9,}  sum_input={sum(rows):>11,}  first_over_window={over}")
    return rows

if __name__ == "__main__":
    base = Cfg()
    report("naive", base)
    report("+ tool search", Cfg(defer_tools=True))
    report("+ context editing", Cfg(clear_old_results=True))
    report("+ code mode", Cfg(code_mode=True))
    report("tool search + editing", Cfg(defer_tools=True, clear_old_results=True))
    report("all three", Cfg(defer_tools=True, clear_old_results=True, code_mode=True))
```

Output:

```
naive                              final=  218,900  sum_input=  6,902,500  first_over_window=45
+ tool search                      final=  170,800  sum_input=  4,452,500  first_over_window=None
+ context editing                  final=   82,280  sum_input=  3,691,930  first_over_window=None
+ code mode                        final=   86,600  sum_input=  3,595,000  first_over_window=None
tool search + editing              final=   34,180  sum_input=  1,241,930  first_over_window=None
all three                          final=   26,080  sum_input=    853,130  first_over_window=None
```

Cost with caching: charge `0.10 × (previous context)` plus `1.25 × (new tokens)` per turn, or
`1.25 × C_t` on turn 1 and on any miss (a 10-line function over `trajectory()`'s rows).

Output: `uncached naive 6,902,500`, `cached naive 941,985`, `cached tool search 641,670`, `cache
busted/turn 8,628,125`, `edit each turn (miss every turn) 1,552,412`.

### Lab 2 — a tool-search index over the tool-registry lab (`toolsearch.py`)

Goal: BM25 over `DiscoveryService._collect_candidates()`, with authorization applied *before*
ranking, using `Principal`/`_is_allowed` from the lab. Then break it on purpose.

```python
import math, re, sys
from collections import Counter
sys.path.insert(0, "/home/user/system-design/ai-rag/labs/tool-registry")
import models as M, registry as R, discovery as DS

TOK = re.compile(r"[a-z0-9]+")
def tokens(s): return TOK.findall(s.lower().replace("_", " ").replace(".", " "))

class BM25ToolIndex:
    def __init__(self, discovery, k1=1.5, b=0.75):
        self.k1, self.b = k1, b
        self.docs = {}                     
        for c in discovery._collect_candidates():
            self.docs[c["tool_ref"]] = (tokens(c["tool_ref"] + " " + c["description"] + " " + " ".join(c["tags"])), c["description"])
        self.N = len(self.docs)
        self.avgdl = sum(len(t) for t, _ in self.docs.values()) / max(1, self.N)
        self.df = Counter(w for t, _ in self.docs.values() for w in set(t))

    def search(self, query, allowed=None, limit=5):
        q = tokens(query)
        scored = []
        for ref, (toks, desc) in self.docs.items():
            if allowed is not None and ref not in allowed:
                continue
            tf, dl, s = Counter(toks), len(toks), 0.0
            for w in q:
                if w not in tf: continue
                idf = math.log(1 + (self.N - self.df[w] + 0.5) / (self.df[w] + 0.5))
                s += idf * tf[w] * (self.k1 + 1) / (tf[w] + self.k1 * (1 - self.b + self.b * dl / self.avgdl))
            if s > 0: scored.append((round(s, 3), ref))
        return sorted(scored, reverse=True)[:limit]

def build_registry():
    reg = R.ToolRegistry()
    catalog = {
        "github": ["create issue", "list pull requests", "merge pull request", "search code", "get commit", "list branches"],
        "slack": ["post message", "list channels", "search messages", "upload file", "set reminder"],
        "jira": ["create ticket", "transition ticket", "search tickets", "add comment"],
        "payments": ["issue refund", "capture charge", "list invoices", "create payout"],
        "crm": ["find customer", "update contact", "list opportunities"],
        "calendar": ["create event", "find free slot", "cancel event"],
    }
    for ns, verbs in catalog.items():
        for v in verbs:
            name = v.replace(" ", "_")
            td = M.ToolDefinition(
                metadata=M.ToolMetadata(name=name, namespace=ns, owner_team=f"{ns}-team", tags=(ns,)),
                spec=M.ToolSpec(description=f"{v.capitalize()} in {ns}",
                                annotations=M.Annotation(read_only=v.split()[0] in {"list", "search", "get", "find"},
                                                         destructive=False, requires_approval=False)),
                version="1.0.0")
            res = reg.publish(td)
            assert res.ok, res.errors
            reg.approve(res.tool_version.tool_version_id)
    return reg

if __name__ == "__main__":
    reg = build_registry(); disc = DS.DiscoveryService(reg); idx = BM25ToolIndex(disc)
    print(len(idx.docs), "tools indexed")
    for q in ["refund a customer payment", "open a ticket", "search pull requests"]:
        print(q, "->", idx.search(q))
    agent = M.Principal(agent_id="support_bot", allowed_tool_patterns=("crm.*", "jira.*"))
    allowed = {c["tool_ref"] for c in disc._collect_candidates() if disc._is_allowed(c, agent)}
    print("scoped:", idx.search("refund a customer payment", allowed=allowed))
```

Output:

```
25 tools indexed
refund a customer payment -> [(4.114, 'payments.issue_refund'), (4.114, 'crm.find_customer')]
open a ticket -> [(3.377, 'jira.transition_ticket'), (3.377, 'jira.create_ticket')]
search pull requests -> [(6.944, 'github.list_pull_requests'), (3.131, 'github.merge_pull_request'), (2.892, 'slack.search_messages'), (2.892, 'jira.search_tickets'), (2.892, 'github.search_code')]
scoped: [(4.114, 'crm.find_customer')]
```

Read the output critically: "refund a customer payment" ties `payments.issue_refund` with
`crm.find_customer`, and "open a ticket" cannot tell `create_ticket` from `transition_ticket` — lexical
matching does not know "open" means "create". Exercises: add a synonym map; add a `detail` parameter
(`name`/`description`/`schema`); demote tools by `DiscoveryService._health_scores`; measure top-1/top-3 hit
rate on 30 hand-written queries; verify out-of-scope tools never appear even when they would rank first.

### Lab 3 — a clearing + compaction policy (`compact.py`)

Goal: the §8 ladder as code: clear stale tool results at 50% of the window, compact at 75%, pin the
task statement, keep recent turns, exclude the `memory` tool from clearing. The summarizer is a
stub; replace it with an LLM call whose prompt lists what to keep (§8.3).

```python
from dataclasses import dataclass, field

def ntok(s: str) -> int: return max(1, len(s) // 4) 

@dataclass
class Msg:
    role: str               
    text: str
    tool: str = ""          
    pinned: bool = False    

@dataclass
class Policy:
    window: int = 200_000
    clear_trigger: float = 0.50   
    compact_trigger: float = 0.75 
    keep_recent_results: int = 3
    keep_recent_turns: int = 6    
    exclude_tools: frozenset = frozenset({"memory"})
    stub: str = "[tool result cleared to save context]"

def total(msgs): return sum(ntok(m.text) for m in msgs)

def clear_stale(msgs, p: Policy):
    idx = [i for i, m in enumerate(msgs) if m.role == "tool" and m.tool not in p.exclude_tools and not m.pinned]
    for i in idx[:-p.keep_recent_results] if p.keep_recent_results else idx:
        msgs[i] = Msg("tool", p.stub, msgs[i].tool)
    return msgs

def compact(msgs, p: Policy, summarize):
    pinned = [m for m in msgs if m.pinned]
    rest = [m for m in msgs if not m.pinned]
    head, tail = rest[:-p.keep_recent_turns], rest[-p.keep_recent_turns:]
    if not head: return msgs
    summary = Msg("user", "[summary of earlier work]\n" + summarize(head), pinned=False)
    return pinned + [summary] + tail

def step(msgs, p: Policy, summarize):
    used = total(msgs)
    if used > p.compact_trigger * p.window:
        return compact(clear_stale(msgs, p), p, summarize), "compact"
    if used > p.clear_trigger * p.window:
        return clear_stale(msgs, p), "clear"
    return msgs, "none"

def stub_summarizer(head):        
    tools = sorted({m.tool for m in head if m.role == "tool"})
    return f"{len(head)} messages condensed; tools used: {', '.join(tools)}"

if __name__ == "__main__":
    p = Policy(window=8_000)     
    msgs = [Msg("user", "Migrate service X to the new API. Do not touch prod.", pinned=True)]
    for t in range(1, 41):
        msgs.append(Msg("assistant", f"calling tool for step {t} " * 5))
        msgs.append(Msg("tool", "row " * 1200, tool="sql" if t % 2 else "grep")) 
        msgs, act = step(msgs, p, stub_summarizer)
        if act == "compact" or t in (1, 4, 40):
            print(f"turn {t:2d} action={act:7s} tokens={total(msgs):>6,} msgs={len(msgs)}")
```

Output (window shrunk to 8,000 so it triggers in 40 turns):

```
turn  1 action=none    tokens= 1,243 msgs=3
turn  4 action=clear   tokens= 3,742 msgs=9
turn 31 action=compact tokens= 3,723 msgs=8
turn 40 action=clear   tokens= 4,083 msgs=26
```

Exercises: make clearing batch-aware (`clear_at_least`) and count cache misses; pin a `constraints` message and assert it survives 3 compactions; replace `stub_summarizer` with an LLM call; replay with compaction forced at turn 20 and compare the final state on a deterministic fake environment.

---

## 16. Real-world cases

> **In plain words.** Cases 1–3 are publicly documented, with numbers as the vendor states them.
> Cases 4–5 are labeled composites: arithmetic examples of failure modes this chapter describes.
>
> **Real-world example.** Tool menu eats the window → Case 1; reading the same data twice → Case 2;
> parallel agents multiply the bill → Case 3; cache silently disabled → Case 4; summary loses a
> rule → Case 5.

**Case 1 — Tool definitions before the first word (Anthropic, documented).** Anthropic's "Advanced
tool use" post describes a five-server setup (58 tools) "consuming approximately 55K tokens before
the conversation even starts", and says it has seen tool definitions consume 134K tokens before
optimization. The documented remedy is the Tool Search Tool, reported there as improving MCP-eval
accuracy from 49% to 74% (Opus 4) and from 79.5% to 88.1% (Opus 4.5); the tool-search docs add that
tool search typically cuts definition tokens by over 85%. Lesson: the cost shows up in both the
window and the accuracy, and it is a property of the *catalog*, not the task.
Source: [advanced tool use](https://www.anthropic.com/engineering/advanced-tool-use),
[tool search docs](https://platform.claude.com/docs/en/agents-and-tools/tool-use/tool-search-tool).

**Case 2 — Moving a transcript through the model twice (Anthropic, documented).** The "Code
execution with MCP" post's example: copying a roughly 2-hour meeting transcript from one system to
another through direct tool calls means the content flows through the model (about 50,000
additional tokens in their estimate) and the post reports a workflow falling from 150,000 to 2,000
tokens (98.7%) when the agent wrote code and kept the data in the execution environment. Lesson:
tokens that the model does not need to *read* should not enter the window; and this is the best-case
figure for that example, with sandbox overhead as the trade-off.
Source: [code execution with MCP](https://www.anthropic.com/engineering/code-execution-with-mcp).

**Case 3 — Parallel sub-agents buy quality with tokens (Anthropic, documented).** The multi-agent
research system post reports a lead + sub-agent configuration beating a single-agent baseline by
90.2% on their internal research eval, at about 15× the tokens of a chat interaction (agents alone
about 4×), and that token usage explained 80% of the variance on BrowseComp. It also names coding as
a weaker fit because of dependencies between sub-tasks. Lesson: sub-agents are a spend decision, not
a saving.
Source: [multi-agent research system](https://www.anthropic.com/engineering/multi-agent-research-system).

**Case 4 — Illustrative scenario (composite, not a specific company): the timestamp in the system
prompt.** A support agent runs 40-turn sessions with a 60,000-token prefix growing about 2,500 tokens
per turn. A change adds "Current time: ..." to the first line of the system prompt, so every call has
a unique prefix. Arithmetic example with §11.4's multipliers: `Σ C_t = 40 × 60,000 + 2,500 × 780 =
4,350,000`; miss-every-turn costs `1.25 × 4,350,000 = 5,437,500` units versus about 621,000 with a
stable prefix, roughly 8.8×. Fix: move the clock to the tail and alert on
`cache_read_input_tokens / total_input_tokens`.

**Case 5 — Illustrative scenario (composite, not a specific company): the summary that forgot the
freeze.** A migration agent is told "no changes to the billing service until Friday". After a 4-hour
run, threshold compaction produces a summary listing completed services and the next batch but
not the freeze, because the instruction was in the first message and the summarizer weighted recent
work. The next batch touches billing. Detection: a forced-compaction replay in the eval suite
asserting that `billing` is never modified. Fix: pin the constraint in a notes file the agent
re-reads at the start of each phase, give the summarizer an explicit "constraints and prohibitions"
section in its instructions, and keep recent turns verbatim.

---

## Sources

Fetched while writing this chapter (all accessed 2026-10-10):

- Anthropic Engineering, "Effective context engineering for AI agents": https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents
- Anthropic Engineering, "Writing effective tools for agents": https://www.anthropic.com/engineering/writing-tools-for-agents
- Anthropic Engineering, "Code execution with MCP: building more efficient agents": https://www.anthropic.com/engineering/code-execution-with-mcp
- Anthropic Engineering, "Equipping agents for the real world with Agent Skills": https://www.anthropic.com/engineering/equipping-agents-for-the-real-world-with-agent-skills
- Anthropic Engineering, "How we built our multi-agent research system": https://www.anthropic.com/engineering/multi-agent-research-system
- Anthropic Engineering, "Introducing advanced tool use": https://www.anthropic.com/engineering/advanced-tool-use
- Claude docs, Tool search tool: https://platform.claude.com/docs/en/agents-and-tools/tool-use/tool-search-tool
- Claude docs, Tool use with prompt caching: https://platform.claude.com/docs/en/agents-and-tools/tool-use/tool-use-with-prompt-caching
- Claude docs, Context editing: https://platform.claude.com/docs/en/build-with-claude/context-editing
- Claude docs, Compaction overview: https://platform.claude.com/docs/en/build-with-claude/compaction and Compaction at a token threshold: https://platform.claude.com/docs/en/build-with-claude/compaction-threshold
- Claude docs, Memory tool: https://platform.claude.com/docs/en/agents-and-tools/tool-use/memory-tool
- Agent Skills specification text, read from the specification repository on GitHub: https://agentskills.io ; Anthropic's skills repository confirming the Agent Skills standard pointer: https://github.com/anthropics/skills
- Chroma, "Context Rot: How Increasing Input Tokens Impacts LLM Performance" (Hong, Troynikov, Huber, 14 July 2025): https://research.trychroma.com/context-rot
