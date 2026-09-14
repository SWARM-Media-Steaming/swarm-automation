# Issue Lifecycle Comment Rules

Anyone reading a GitHub issue worked by `issue_worker/swarm_issue_worker.py`
must be able to tell, from the issue thread alone and without reading logs,
what state the AI's work on it is currently in. That means posting one
comment at each of the transitions below — never silently starting,
stopping, or finishing work — and every provider (Claude, Codex, Grok, and
any added later) posting in the same shape, so a reader never has to learn
a second format depending on who picked the issue up. The four required
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

### What must never happen

- A work-round finishing without one of "Reworked"/"Completed" landing on
  the issue — silence on the issue after real work is indistinguishable
  from the worker having crashed or never run.
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
