# Issue Lifecycle Comment Rules

Anyone reading a GitHub issue worked by `issue_worker/swarm_issue_worker.py`
must be able to tell, from the issue thread alone and without reading logs,
what state the AI's work on it is currently in. That means posting one
comment at each applicable transition below — never silently starting,
stopping, or finishing work — and every provider (Claude, Codex, Grok, and
any added later) posting in the same shape, so a reader never has to learn
a second format depending on who picked the issue up. The required
transitions, and the code that must keep producing them:

1. **Started** — `post_started_comment`, the moment a provider begins a
   work-round (first pass *or* a follow-up).
2. **Reworked / Completed** — `finalize_issue` → `render_pending_comment` /
   `post_pending_comment`, once a commit for this work-round has actually
   been delivered (see `issue-branch-delivery.md` — never before).
3. **Stopped because usage ran out** — `post_quota_comment`, when the
   provider's remaining quota drops below `minimum_remaining_percent`
   mid-run.
4. **Resumed** — `post_resumed_comment`, when a quota-paused session is
   later picked back up (not one of the original four the user asked for,
   but the natural counterpart to #3 and already implemented — keep it in
   sync with the same rules below).
5. **AI needs input** — `finalize_needs_input`, only when credentials,
   authority, unavailable external information, or an external user action
   makes autonomous progress impossible, or the adversarial UAT six-round
   cap is exhausted after publishing the reviewable PR. Apply `AI Needs Input`, remove
   `Ready For Testing`, and wait for a trusted-author comment.
6. **Question answered** — `finalize_question_answer`, for issues labelled
   `Question`. Post a grounded no-code answer and never apply `Ready For
   Testing`.

### Each comment is idempotent via an HTML marker

Every one of these comments opens with an HTML comment marker unique to
that specific event (issue number, provider, branch/session/commit as
appropriate) — e.g. `<!-- swarm-issue-worker:started:issue:278;provider:
claude;branch:ai/claude/issue-278 -->` or `<!-- swarm-issue-worker:commit:
<sha>;through-comment:<id> -->`. Before posting, the worker checks the
issue's existing comments for that exact marker and skips posting if it is
already there. A new comment-posting path added to this file must follow
the same pattern: mint a marker specific enough that re-running the same
event (a retried scheduler tick, a supervisor restart) can never double-post,
but distinct enough that a genuinely new event (a new work-round, a new
resume) always gets its own comment.

### Baseline templates — match this shape for any new or edited comment

**Started** (`post_started_comment`):
```
<!-- swarm-issue-worker:started:issue:<n>;provider:<key>;branch:<branch> -->
🤖 **<Provider> Bot** started working on this issue.

- Model: `<model>`
- Branch: `<branch>`
- <Provider> usage remaining: <usage>
```
("started working on" becomes "started follow-up work on" when
`work_type == "followup"`.)

**Reworked / Completed** (`render_pending_comment`):
```
<!-- swarm-issue-worker:commit:<sha>[;through-comment:<id>] -->
<Reworked|Completed> by **<Provider>**.

- Model: `<model>`
- Effort: `<effort>`
- Branch: `<branch>` → <pull_request_url>
- Commit: `<sha>` — <commit message>
<usage report line(s)>
- Adversarial UAT: <clean first pass|resolved after N rounds>, <N> test files added.
- Adversarial Cybersecurity: <PASS|FIXED|FINDINGS_CREATED|FAILED> — <N> in-scope finding(s), <N> fixed, <N> follow-up issue(s) filed; Critical <N> / High <N> / Medium <N> / Low <N>; <N> security test file(s) added.
<details><summary>AI completion summary</summary>

<the AI's own final output>
</details>
```
("Reworked" for a followup work-round, "Completed" otherwise — see
`render_pending_comment`'s `verb` selection.)

**Stopped for usage** (`post_quota_comment`):
```
<!-- swarm-issue-worker:quota-paused:issue:<n>;session:<session_id> -->
Work paused because **<Provider>** no longer has sufficient usage available.

- Model: `<model>`
- Session: `<session_id>`
- The current work and AI session were saved.
- The worker will wait for <Provider> specifically, include new trusted
  comments, and resume this same session automatically.
```

**Resumed** (`post_resumed_comment`):
```
<!-- swarm-issue-worker:resumed:issue:<n>;provider:<key>;session:<session_id>;at:<resume_token> -->
🤖 **<Provider> Bot** is resuming work on this issue.

- Model: `<model>`
...
```

**AI needs input** (`finalize_needs_input`):
```
<!-- swarm-issue-worker:needs-input:issue:<n>;provider:<key>[;through-comment:<id>] -->
# 🤖 AI needs your input

## Action required
<one exact action or question and the reply that resumes work>

## Summary
<why AI cannot continue autonomously>

## Recommendations
<preferred course, including a warning not to post secrets when applicable>

## Step-by-step guide
<concrete numbered setup/action instructions, or "- None.">

## How to resume
<trusted-author and sensitive-information reminder>
```

**Question answered** (`finalize_question_answer`):
```
<!-- swarm-issue-worker:question-answer:issue:<n>;provider:<key>[;through-comment:<id>] -->
# 🤖 AI answer

Answered by **<Provider>** after the normal pre-flight grading and routing flow.
No repository changes were made.

## Answer
...
## Evidence
...
## Recommendations
...
```

### What must never happen

- A work-round finishing without one terminal comment—"Reworked"/"Completed",
  "AI needs your input", "AI answer", or a quota pause—landing on the issue.
  Silence after real work is indistinguishable from the worker having crashed
  or never run.
- A "Reworked"/"Completed" comment whose `Commit:`/marker `sha` is not the
  commit this work-round actually produced (see `issue-branch-delivery.md`
  for the specific failure mode this guards against).
- A quota-pause with no "Started" comment ever having been posted for that
  work-round, or a "Started" comment for a work-round that never gets a
  matching "Reworked"/"Completed"/quota-pause — every "Started" must
  eventually be answered by exactly one of the other three.
- Two providers, or the same provider on two different transitions, using
  visibly different phrasing/structure for what is conceptually the same
  event. Extend the templates above rather than inventing a parallel
  format when adding a provider or a new transition.
- A `Question` issue changing repository state or receiving `Ready For
  Testing`; pre-flight grading and routing still run, but its worker pass is
  answer-only.
- Treating normal ambiguity as `AI Needs Input`. That state is reserved for
  genuine impossibility, and only a trusted-author comment after its marker
  may resume the issue. Automated CI comments may add evidence but are not a
  user answer unless explicitly configured as trusted.

### The adversarial stages are one work-round

The independent assessment and every fixer/tester exchange of *every* enabled
adversarial stage — UAT and cybersecurity — share one Started comment and one
terminal comment. Omit a stage's line when that stage is disabled. The
cybersecurity line is followed by a collapsed `Adversarial Cybersecurity
review` block with the counts, summary, remediations, filed issue links and
validation results; raw reviewer reasoning never goes on the issue. Quota pauses
and resumes use the existing idempotent notices and retain the current role,
phase, round count, test definition and execution-history row.

A cap-hit uses the existing AI Needs Input template with an explicit
**adversarial-test deadlock**, the delivered PR link and failing suite evidence.
Ask for adjudication against the spec, not credentials or environment setup.
Do not run no-code branch cleanup for this delivered PR. Only a trusted-author
reply starts another work-round; no per-round GitHub comments are posted.
