# Native prompt caching and session continuity

CHOMP optimizes the existing Claude Code and Codex CLI integrations automatically.
There is no setting, inference cache, API client, or additional service. Native
CLI login, subscriptions, system prompts, tool permissions and compaction stay
under each CLI's control. Grok retains its existing execution path: it may
`--resume` a session it actually started (quota pause, worker restart). A leftover
Claude or Codex UUID with `resume=True` must not reach Grok. `prepare_cli_session`
fails closed in that case (`resume=False`, a new Grok session id, full current
prompt), matching official provider handoff.

## Session lifecycle

`Worker.run_ai` applies `PromptSessionMixin` **after** routing, tester floors,
latest-release upgrades and escalation. `in-progress-issue.json` holds a bounded
`cli_sessions` map; quota-paused state carries it through the existing persistence
path. Entries contain only a UUID, provider/model/effort, role, timestamp, commit,
context digest and (when supplied) cumulative usage counters. There are no stored
source snapshots, prompts, completions or cross-issue session searches.

A compatible successful session is reused for primary work or successive fixes
within the same UAT/security epoch. UAT fixers, security fixers and primary workers
have separate identities. Every new UAT or cybersecurity assessment gets a fresh
session, even if the provider and model match the implementer. Only an interrupted
assessment may resume its own exact stage/epoch/round. The post-delivery
architecture-documentation review is likewise always one fresh session that is
never remembered or resumed, and its usage rows carry the `documentation` role. A rejected report starts a
fresh assessment (its `retry_rejection` generation is part of the tester identity, so the discarded
conversation cannot be resumed; a later interrupted retry may resume itself). Epoch escalation discards old fixer continuity.

Compatibility requires the same resolved checkout, GitHub repository, issue
number/title/body/labels, base commit, branch, provider, model, effort and repository
instruction digest. Root/nested AGENTS.md and CLAUDE.md, `.claude` and `.codex`
instructions are read and hashed again; symlinks outside the checkout are not
followed. The previous HEAD must remain an ancestor of current HEAD. Source edits
on that branch are expected, and agents must re-read them. A 24-hour idle bound,
changed instructions/requirements, rewound history, changed model/effort, a failed
execution or incompatible/missing metadata starts a fresh session. Legacy state
without metadata remains readable and safely restarts with current context.

A CLI process launch alone is not proof a session exists. Claude's session-bearing
system/result event or Codex's `thread.started` confirms its identity. UUIDs are
passed explicitly; never use `--last`, session names or cross-directory discovery.

A failed resume caused by a missing/expired/corrupt session or exhausted context
gets one fresh retry. Primary prompts are reconstructed from the current issue,
repository and current continuation. Adversarial prompts are reconstructed from
current spec/diff/findings/tests. Every attempt is accounted separately. Model
rejection still uses the existing bounded model fallback. Ordinary provider errors
and quota pauses retain their existing treatment. Successful native compaction is
not treated as a failure and does not trigger a reset. A compaction the CLI itself
reports as an error (its `compact_boundary` event carries `is_error`, or is itself
typed `error`/`turn.failed`) is a resume failure and gets the one fresh retry; the
`compact_boundary` subtype alone is only success evidence on a non-error event. The
event decides, not its wording: `is_error` (as a boolean, number or string) marks a
failed compaction even with a terse, empty or success-sounding message, an
`error`/`turn.failed` event carrying `compact_boundary` is a failed compaction even
with no `is_error` and no exhaustion code, and a later failed compaction is never
hidden by an earlier successful one in the same stream. Claude Code's own failed
compact is a `system`/`status` event with `compact_result=failed` (optional
`compact_error`), a result event whose errors name the compaction failure, or the
CLI's "Compaction failed" prose. `compact_result=success` and Codex
`context_compacted` are left alone. Likewise, the presence of an adversarial loop's `retry_rejection`
marker, even a damaged one, means the assessment was discarded and must start fresh.

## Context construction

CLI-owned system/repository instructions retain their native order. CHOMP's role
instructions precede the stable issue/repository requirements and then current
amendments, patch, suite results and findings. Fix continuations omit the unchanged
issue body already retained in that session; they always refresh the patch and
review findings. Primary continuations avoid reinjecting unchanged engineering
knowledge. A fresh/recovered session receives full requirements. CHOMP does not
intercept CLI compaction or cache source text for a later execution.

## Usage and cost semantics

Migration **10** adds nullable columns to the existing `ai_token_usage` table:
`session_reused`, `session_role`, `cache_input_tokens`, `cache_savings_estimate`,
and `reported_cost`. The existing `agent_run_id` is the native session UUID.
Provider, model, reasoning effort, agent, read/write/input/output/reasoning tokens,
duration and estimate provenance retain their existing columns. Router usage is
never attributed to an unrelated implementation session.

- Cache-hit efficiency is **cache reads / total input represented by matching
  observations**, weighted by tokens. For Claude, total input includes uncached
  input plus cache reads plus cache writes. For Codex, input already includes reads.
  Rows missing the required counters are excluded from the ratio, with coverage
  reported. Missing values stay NULL / `—`, including historical rows.
- Codex `turn.completed` usage takes precedence over cumulative `token_count`
  records. Session totals are differenced against the same session's previous
  counters. Unknown baselines or counters reset during compaction are unavailable,
  never counted again as a new invocation's entire usage.
- `reported_cost` preserves Claude's `total_cost_usd` when supplied. This is a
  provider-reported usage cost, **not verified subscription billing**. Codex does
  not normally expose a monetary cost; its value remains unavailable.
- Estimated API-equivalent savings use the existing effective-dated pricing
  catalog and rates saved with the invocation. Cache-write premiums subtract from
  the cache-read discount; the net figure can be negative. Unknown prices remain
  unavailable. These estimates are never represented as realized subscription
  savings. Historical estimates are never repriced.

Feedback → Usage & cost exposes aggregate efficiency, reuse, reported cost and
estimated savings, with provider/model/agent filters and groupings. Invocation
rows (also in execution history) show session IDs, cache reads/writes and costs.
The existing GitHub AI Usage report adds a cache-efficiency summary next to its
per-agent token/cost breakdown; no extra comment/reporting service is created.
Architecture documentation review is the `documentation` agent bucket, not
Primary; `session_role` and `agent_type` must agree.

## Measurement-driven routing

`usage_report.cache_routing_evidence` requires at least **20 calls across five
issues in the last 30 days**, at least **95% success**, complete priced cache
observations, and one pricing rate/version. Repository, provider, model, effort
and role must match. Failed calls count toward the gate. Historical API-equivalent
cost discounts are capped at 50% for routing and supplied to the existing dynamic
router prompt. Saved-attempt cost comparisons also apply this evidence to their
existing estimates. Insufficient evidence leaves routing unchanged.

Capability, safety, context fit, required effort, provider availability, manual
configuration, tester independence and fallback behavior take precedence. A higher
required reasoning effort can start a fresh session. Routing still selects the
model first; continuity only reuses a compatible session for that selected route.
Measurements across issues are statistical evidence, never permission to reuse
one issue's session for another issue.

## Validation and operations

`python3 -m unittest discover -s issue_worker -p 'test_prompt_caching.py'` covers
session boundaries, recovery, stale instructions/history, compaction, telemetry,
legacy records and routing evidence gates. Existing worker, adversarial, pricing,
reporting and frontend suites remain regression gates.

`python3 issue_worker/benchmark_prompt_caching.py --provider codex --model <model>`
(or `--provider claude`) performs six native CLI calls in disposable repositories:
implementation, a break/fix requirement and an independent review, both with and
without reuse. It uses the existing login, can consume account usage, times each
call, captures provider counters/costs when exposed, and checks the resulting code
and unchanged test fixture after every call. Reviews receive fresh sessions and
may not edit the implementation. Only numeric telemetry and outcomes are printed;
transcripts and credentials are not logged. Each call has a 180-second timeout.
This developer benchmark is not a product configuration toggle.

One small benchmark does not establish causal savings: native caches may also hit
on fresh sessions, tooling/compaction can dominate, and providers vary. Do not feed
synthetic results or the six-call benchmark into the routing history.

Deploy with the normal app/worker update and restart the worker to load the new
Python code. The additive SQLite migration runs automatically when history opens;
no manual migration, authentication change or new infrastructure is required.
