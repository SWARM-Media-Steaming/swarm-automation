# Issue Branch Delivery Rules

Work an AI provider does against a GitHub issue (`issue_worker/swarm_issue_worker.py`)
is not "done" when a commit exists in the local working copy. It is done only
once that commit is on GitHub, on the issue's branch, represented by a pull
request the configured automation can act on. A local-only commit is
indistinguishable, from everyone else's point of view, from the work never
having happened at all — and worse than that, it can make the issue *look*
finished (a comment gets posted, a label gets added) while nothing actually
shipped. Follow the rules below for any change to the delivery path
(`finalize_issue`, `deliver_pull_request`, `push_ref`, `merge_pull_request`)
so this class of bug — a completion comment describing work that was never
pushed — cannot recur silently.

### The delivery sequence is mandatory and ordered

For every work-round that produces a commit, in this order:

0. When `adversarial_uat_enabled` is on, finish the independent assessment and
   all local fix/re-test rounds before pushing. No intermediate round pushes
   or PRs. A clean pass follows steps 1–3 below. A six-round deadlock also
   pushes and opens/reuses the issue PR, but calls `deliver_pull_request` with
   `allow_automation=False`, then `finalize_needs_input(delivery=...)` instead
   of `finalize_issue`. The delivery-only flag is mandatory: auto-approval,
   merging and promotion live inside `deliver_pull_request`. Cap-hit PRs also
   carry a durable body marker that the later PR reconciler must skip. Only
   a passing UAT follow-up clears that automation hold. Retain that
   branch and its failing tests for a human to adjudicate.
1. **Push** the commit to the issue's remote branch (`push_ref`,
   `expected_branch()`). Nothing past this point may run against a commit
   that has not been pushed.
2. **Open or reuse** the integration-branch pull request for that exact
   branch (`deliver_pull_request`).
3. **Comment and label** the issue to reflect what actually happened
   (`finalize_issue` → `post_pending_comment`, `add_pending_label`) — see
   `issue-lifecycle-comments.md` for the comment shape itself.

A step later in this sequence must never run on the assumption that an
earlier step already happened elsewhere. In particular: `finalize_issue`
building its `pending` comment payload from a `commit_sha` is only valid
once `deliver_pull_request` has confirmed *that specific commit* is on
GitHub — not merely that a commit exists locally.

### Reusing a branch name is not proof anything on it was delivered

Issue branches are reused across multiple work-rounds on the same issue
(`ai/<provider>/issue-<n>`, unchanged across a first pass and every later
follow-up). `deliver_pull_request`'s short-circuit for "this branch's PR is
already merged" checks `gh pr list --head <branch> --state all --limit 1` —
that finds the *most recent PR GitHub has ever recorded for that branch
name*, which for a reused branch can be a PR from a **previous, already-
merged work-round**, unrelated to the new commit currently sitting in the
local checkout. Treating that as "nothing to do" silently discards the new
commit: it is never pushed, no PR is opened, and no error is raised — the
work-round can then still report success (see
`issue-lifecycle-comments.md`), describing changes that exist nowhere on
GitHub.

Any "already delivered" recovery check here — and any future recovery
check added to this function — must verify the *specific commit being
delivered* is actually reachable from what that recovered PR/commit
represents (e.g. `git merge-base --is-ancestor <new commit> <merged sha>`
after fetching so the merged commit is available to check against), not
just that a PR by that name exists in some merged state. When that
ancestry can't be confirmed, the safe default is to fall through to the
normal push-and-open/reuse-PR path — never to skip delivery.

### Failure must be loud, not silently absorbed into a "finished" state

`push_ref`, `deliver_pull_request`, and everything `finalize_issue` calls
before writing/posting the `pending` record must raise `WorkerError` on
failure rather than return a value that lets the caller proceed as if
delivery succeeded. Do not add a code path that catches a delivery failure
and continues on to comment/label the issue anyway — a failed delivery
should leave the issue exactly as it was before the work-round started (no
misleading "Completed"/"Reworked" comment, no `Ready For Testing` label),
so the next run or a human can tell delivery is still outstanding.
