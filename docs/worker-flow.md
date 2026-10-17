# Issue worker flow

How `issue_worker/` picks up an issue, works it, lets the adversarial reviewers
attack the result, and delivers it. This is the reference for the whole flow;
the diagrams are [Mermaid](https://mermaid.js.org/). They render on GitHub, and `docs/worker-flow.html` is the same content as a standalone visual page.

> **Keep this file in step with the code.** Any change to the flow, a limit, an
> exit code, a label, a comment or a state file must update the matching diagram
> and table in the same change. The rule is
> `.claude/rules/worker-flow-diagram.md`; `test_worker_flow_docs.py` fails when a
> limit or a named function here drifts from the code, or when `docs/worker-flow.html` is
> not rebuilt (`python3 scripts/build_worker_flow_html.py`).

Contents

1. [The big picture](#1-the-big-picture)
2. [One worker run, and how an issue is selected](#2-one-worker-run-and-how-an-issue-is-selected)
3. [Starting work and the implementation pass](#3-starting-work-and-the-implementation-pass)
4. [The adversarial pipeline](#4-the-adversarial-pipeline)
5. [Inside one adversarial stage: rounds and epochs](#5-inside-one-adversarial-stage-rounds-and-epochs)
6. [What happens to a finding](#6-what-happens-to-a-finding)
7. [Delivery](#7-delivery)
8. [Pauses, holds and recovery](#8-pauses-holds-and-recovery)
9. [Limits and counts](#9-limits-and-counts)
10. [Comments and labels on the issue](#10-comments-and-labels-on-the-issue)
11. [Where state lives](#11-where-state-lives)
12. [Code map](#12-code-map)

---

## 1. The big picture

A scheduler starts one worker run per repository on an interval. A run does at
most one unit of work (one issue, or one resumed checkpoint), then exits with a
code that tells the scheduler what happened.

Before starting a run, the scheduler leaves a checkout with tracked changes
alone unless the worker owns them: a saved `in-progress-issue.json`, or an
`integration-recovery.json` for a half-finished `ai-main` <- `main` merge. The
worker finishes that merge on its next run.

```mermaid
flowchart TD
    S["Scheduler<br/>install_swarm_issue_cron.py<br/>every interval, repos in parallel"] --> R["One worker run<br/>Worker.run"]
    R --> W{"What did this run do?"}
    W -->|"nothing to do"| X0["exit 0"]
    W -->|"issue delivered or answered"| X10["exit 10"]
    W -->|"provider out of usage, session saved"| X11["exit 11"]
    W -->|"no provider has enough usage"| X12["exit 12"]
    W -->|"strict adversarial epochs renewed 3 times"| X13["exit 13"]
    W -->|"same failure repeated, issue held"| X14["exit 14"]
    W -->|"self-recovering condition"| X15["exit 15"]
    X10 --> AGAIN["Run again immediately<br/>progress codes are 10, 11 and 13"]
    X11 --> AGAIN
    X13 --> AGAIN
    X0 --> WAIT["Wait for the next interval"]
    X12 --> WAIT
    X14 --> WAIT
    X15 --> WAIT
    AGAIN --> R
    WAIT --> R
```

| Exit code | Meaning | Scheduler treats it as |
|---|---|---|
| 0 | Nothing to do right now | idle |
| 10 | `ISSUE_COMPLETED_EXIT_CODE`: delivered, answered, or asked for input | progress |
| 11 | `QUOTA_PAUSED_EXIT_CODE`: provider usage ran out; session saved | progress |
| 12 | `PROVIDER_UNAVAILABLE_EXIT_CODE`: no enabled provider has its minimum remaining usage | idle |
| 13 | `ADVERSARIAL_EPOCH_YIELD_EXIT_CODE`: a strict stage renewed 3 epochs in this process; the checkpoint resumes next pass | progress |
| 14 | `AUTOMATION_FAILED_EXIT_CODE`: the issue is held after a repeated or deliberate failure | idle |
| 15 | `RETRY_LATER_EXIT_CODE`: a self-recovering condition (GitHub hiccup, rejected tester answer); logged quietly | idle |

---

## 2. One worker run, and how an issue is selected

```mermaid
flowchart TD
    A["Worker.run<br/>takes the per-repo worker lock"] --> B["deliver_pending<br/>finish an interrupted comment or label"]
    B --> C["reconcile_issue_pull_requests<br/>approve and squash-merge open issue PRs<br/>when auto-approve is on"]
    C --> D["reconcile_orphan_issue_branches<br/>remove branches stranded by no-code results"]
    D --> E{"Is the saved issue held<br/>after repeated failure?"}
    E -->|"yes, build unchanged"| E1["park_held_issue<br/>shelve it, keep branch and checkpoint"]
    E1 --> E2["exit 14, or carry on with the queue"]
    E -->|"no, or a newer build is installed"| F["refresh_complexity_profile<br/>repository analysis, never blocks"]
    F --> G{"Quota-paused attempt<br/>waiting to resume?"}
    G -->|"yes"| G1["prepare_paused_resume<br/>resume the same session,<br/>or hand off to another provider"]
    G -->|"no"| H{"Is CI monitoring on and the<br/>integration branch failing?"}
    H -->|"yes"| H1["monitor_repository_actions<br/>file a CI failure issue and work it now"]
    H -->|"no"| I["select_issue"]
    G1 --> J["run_selected_issue"]
    H1 --> J
    I --> J
```

### `select_issue`

```mermaid
flowchart TD
    A["Open issues assigned to the configured assignee"] --> B["Drop completed, paused and parked issues"]
    B --> C{"Saved in-progress issue<br/>still open and assigned?"}
    C -->|"yes"| C1["Resume it first<br/>always wins"]
    C -->|"no"| D["Build the ready list"]
    D --> D1["Not completed before:<br/>first pass"]
    D --> D2["Completed before, with a new trusted<br/>comment after the completion marker:<br/>follow-up rework"]
    D1 --> E["gate_on_dependencies<br/>drop first-pass issues whose<br/>Depends on #N are not merged"]
    D2 --> E
    E --> F["Pick by priority:<br/>Urgent, High, Medium, Low<br/>Project Priority field first, then labels"]
    F --> G["Ties: lowest issue number"]
```

Follow-up comments only count from **trusted authors**
(`--trusted-followup-author`). Comments from bots, including the worker's own,
are not instructions.

---

## 3. Starting work and the implementation pass

```mermaid
flowchart TD
    A["run_selected_issue"] --> B{"Resuming a saved attempt?"}
    B -->|"yes"| B1["Check the pinned provider's usage<br/>hand off to another provider,<br/>or pause for quota and exit 11"]
    B -->|"no"| B2["choose_provider<br/>preferred provider with enough usage<br/>exit 12 if none"]
    B1 --> C["ensure_bot_auth"]
    B2 --> C
    C --> D["prepare_issue_complexity<br/>repository-aware complexity analysis<br/>Jev or the AI fallback"]
    D --> E["maybe_apply_dynamic_routing<br/>score models by capability then cost,<br/>then clamp to the repository routing cap"]
    E --> F["prepare_repository<br/>branch ai/provider/issue-N<br/>recover an earlier commit if one exists"]
    F --> G["preflight_adversarial_definition"]
    G --> H["Post the Started comment"]
    H --> I{"Adversarial checkpoint<br/>already exists?"}
    I -->|"yes"| P["run_adversarial_pipeline<br/>section 4"]
    I -->|"no"| J["build_prompt and run_ai<br/>the implementation pass"]
    J --> K{"AI result"}
    K -->|"usage ran out"| K1["Save session, post quota comment,<br/>exit 11"]
    K -->|"session failed"| K2["WorkerError: state preserved,<br/>counted toward the failure limit"]
    K -->|"asks for input"| K3{"Only about protected tests?"}
    K3 -->|"yes"| K4["route_protected_test_block<br/>label adversarial-protected,<br/>a fresh tester adjudicates"]
    K3 -->|"no"| K5["finalize_needs_input<br/>AI Needs Input comment and label<br/>wait for a trusted reply, exit 10"]
    K -->|"answers a Question issue"| K6["finalize_question_answer<br/>code-free answer, exit 10"]
    K -->|"no change: environment only"| K7["finalize_environment_only<br/>comment, exit 10"]
    K -->|"no change at all"| K8["escalate_after_no_change<br/>stronger attempt next time,<br/>exit 15"]
    K -->|"made a commit"| L["Version policy:<br/>discard any AI edit to VERSION,<br/>worker writes the minor bump if labelled"]
    L --> L0["guard_staged_commit()<br/>build output left out,<br/>oversized commit refused"]
    L0 --> M["Validate: still on the issue branch,<br/>history only grows, commit messages,<br/>issue reference present, tree clean"]
    M --> N{"Any adversarial stage enabled?"}
    N -->|"yes"| P
    N -->|"no"| Q["finalize_issue<br/>section 7"]
```

Notes

- `Question` issues never change the repository and never enter the adversarial
  loop. Pre-flight grading and routing still run.
- Only the worker writes `VERSION` (see `.claude/rules/versioning.md`).
- Routing never overrides a manual model selection, and a started session is
  never re-routed to a different model unless its model was retired.

---

## 4. The adversarial pipeline

The stages are independent switches. Each runs its own loop to completion before
the next starts, then delivery happens once.

```mermaid
flowchart LR
    I["Implementation<br/>commit on the issue branch"] --> U{"adversarial_uat_enabled?"}
    U -->|"yes"| U1["Adversarial UAT<br/>verdict = suite exit codes"]
    U -->|"no"| C
    U1 --> C{"adversarial_security_enabled?"}
    C -->|"yes"| C1["Adversarial cybersecurity<br/>verdict = suite exit codes<br/>plus in-scope findings from a fresh reviewer"]
    C -->|"no"| D
    C1 --> D["Delivery<br/>section 7"]
```

Rules that hold for both stages (they share `adversarial_core.py`):

- **Fresh context.** Every tester or reviewer starts a new session. It sees the
  issue, trusted amendments, the current diff, repository conventions and the
  failed approaches so far. It never sees the implementer's transcript or
  reasoning. It is also told which out-of-scope findings are already tracked.
- **Ownership.** The fixer changes product code only. It cannot change, disable
  or retire anything under `tests/adversarial/`; the worker restores such edits
  and turns them into a dispute. UAT owns `tests/adversarial/`; cybersecurity
  owns `tests/adversarial/security/`. Neither may edit the other's suites.
- **Tester floor.** A tester never runs below the scored STANDARD-task model at
  medium effort, and never weaker than the first assessment's tester.
- **Provider choice.** Capacity is checked for every phase. Another provider is
  preferred for testing, and the dynamic router is reused when enabled.

---

## 5. Inside one adversarial stage: rounds and epochs

Round 0 is the independent assessment. One counted round is one fixer pass plus
one fresh re-test. Three counted rounds are one epoch.

```mermaid
flowchart TD
    A["Round 0<br/>independent assessment by a fresh tester"] --> B{"Blocking failures<br/>or findings?"}
    B -->|"none"| OK["Clean first pass<br/>stage done"]
    B -->|"yes"| F["Round n: fixer pass"]
    F --> F1{"Fixer already returned no change 3 times<br/>for this exact failure on this exact<br/>provider, model and effort?"}
    F1 -->|"yes"| F2["Skip the fixer call<br/>the round still re-tests"]
    F1 -->|"no"| F3["Fixer edits product code<br/>worker restores any test edits"]
    F2 --> T
    F3 --> T["Round n: fresh tester re-test<br/>revises tests only to answer a dispute,<br/>runs suites, returns the structured result"]
    T --> V{"Result valid?<br/>edits allowed, JSON well formed"}
    V -->|"rejected"| V1["Restore the edits, tell the next tester why<br/>step the tester up"]
    V1 --> V2{"Rejected 3 times?"}
    V2 -->|"yes"| BE["Deliver the latest commit as best effort<br/>even in strict mode"]
    V2 -->|"no"| T
    V -->|"accepted"| G["File or suppress out-of-scope findings<br/>section 6"]
    G --> H["Run the blocking suites"]
    H --> I{"Clean?"}
    I -->|"yes"| OK2["Resolved after n rounds<br/>stage done"]
    I -->|"no"| J{"Was that round 3 of the epoch?"}
    J -->|"no"| F
    J -->|"yes"| K{"Merge policy"}
    K -->|"best effort"| BE
    K -->|"strict"| L["close_adversarial_epoch<br/>record progress and plan escalation"]
    L --> M{"Stop conditions"}
    M -->|"4 epochs in a row without progress,<br/>12 epochs, or 8 hours"| HOLD["hold_stalled_stage<br/>Automation Failed hold, issue parked"]
    M -->|"none hit"| N["Next epoch: re-route, raise effort,<br/>change provider if no progress,<br/>strongest model after 2 stalled epochs,<br/>never above the routing cap"]
    N --> O{"3 epochs in this process?"}
    O -->|"yes"| Y["Checkpoint and exit 13<br/>scheduler resumes it"]
    O -->|"no"| F
```

How the pieces fit

- **Progress.** An epoch made progress only if fewer suites fail and fewer
  blocking findings remain than at its start. Failure and patch fingerprints
  detect a repeated failure or a repeated patch; either forces a different
  strategy next epoch.
- **Strict vs best effort.** `adversarial_best_effort_merge` is off by default
  and never inferred. Strict never merges known failures and never becomes
  `AI Needs Input` just because findings remain. Best effort stops after the
  first epoch and delivers the latest commit labelled **Best-effort merge with
  unresolved adversarial results**, with the unresolved notes filed as a
  follow-up issue.
- **Stop conditions are the only way out of strict mode** besides a clean
  verdict. A held issue is parked and the queue continues; a newer worker build
  or a removed `Automation Failed` label releases it with a fresh budget.
- **Quota.** Any phase can pause for usage (exit 11). The session, phase, round
  and epoch are checkpointed and resume exactly there.

---

## 6. What happens to a finding

The tester or reviewer returns findings in two buckets.

```mermaid
flowchart TD
    R["Tester or reviewer result"] --> A{"Which bucket?"}
    A -->|"In scope: caused by or needed for this issue"| B{"Stage"}
    B -->|"UAT: a failing acceptance suite"| B1["Fixer repairs the product code<br/>in this issue's next round"]
    B -->|"Cybersecurity: high or medium confidence"| B2["Fixer repairs it in this issue<br/>a new reviewer must stop reporting it"]
    B -->|"Cybersecurity: low confidence"| B3["Advisory only<br/>never blocks, never fixed, never filed"]
    A -->|"Out of scope: legitimate but unrelated"| C["file_stage_findings"]
    C --> D{"Same marker already filed<br/>for this issue?"}
    D -->|"yes"| SKIP["Skip, log it"]
    D -->|"no"| E{"Reworded title of one<br/>already filed for this issue?"}
    E -->|"yes"| SKIP
    E -->|"no"| F{"Open issue with the stage label<br/>holds the same suites, or a title<br/>within one word?"}
    F -->|"yes"| SUP["Suppress as a duplicate"]
    F -->|"no"| G["Shortlist open issues sharing<br/>at least 3 content words"]
    G --> H{"Any candidates?"}
    H -->|"no"| CAP
    H -->|"yes"| I["judge_duplicate<br/>a cheap model decides duplicate or new<br/>advisory, fails open"]
    I --> J{"Duplicate with confidence<br/>0.70 or more?<br/>0.90 for High and Critical"}
    J -->|"yes"| SUP
    J -->|"no, failed, or malformed"| CAP{"Already 5 follow-ups filed<br/>by this stage for this issue?"}
    CAP -->|"no"| NEW["File a new labelled, assigned issue<br/>adversarial-uat or adversarial-security"]
    CAP -->|"yes"| OVF["Record it in one unassigned overflow issue<br/>for this source issue and stage<br/>one notice on the source issue"]
    SUP --> K{"Did the new report add facts<br/>the tracked issue lacks?"}
    K -->|"yes"| L["note_rediscovery<br/>add them to a marked block in the tracked<br/>issue description, plus one comment"]
    K -->|"no"| M["Nothing added"]
```

Details

- A finding may cite out-of-scope `suite_ids`. After its follow-up issue is
  filed (or found as a duplicate) those suites stay registered in
  `.swarm/tests.json` but no longer block this issue's verdict.
- A finding that cites a suite under `tests/adversarial/` is labelled
  `adversarial-protected`. Its follow-up skips implementation: a fresh tester
  adjudicates, because only an adjudicator may revise a protected test.
- GitHub failures while searching, commenting or filing are logged and retried;
  they never abort the round. A model failure files the finding, because a
  duplicate costs noise while a suppressed vulnerability costs far more.
- Filed issues and their URLs are kept in execution history so the app can show
  the non-blocking follow-ups.

---

## 7. Delivery

Nothing is delivered until every enabled stage is `done` and the worktree is
clean. A completion comment must never describe a commit that was not pushed.

```mermaid
flowchart TD
    A["All stages done"] --> B{"Outcome"}
    B -->|"clean first pass or resolved"| C["finalize_issue"]
    B -->|"best-effort cap hit"| C2["finalize_issue with the best-effort override<br/>files a follow-up for unresolved notes"]
    C --> D["synchronize_issue_delivery<br/>merge the latest integration branch<br/>into the issue branch locally"]
    C2 --> D
    D --> E{"Conflicts?"}
    E -->|"yes"| E1["AI resolves them,<br/>then every enabled stage reruns<br/>no earlier PASS is reused"]
    E1 --> D
    E -->|"no"| F["push_ref<br/>push the issue branch"]
    F --> G["deliver_pull_request<br/>open or reuse the PR to the integration branch"]
    G --> H{"auto-approve on, or best effort?"}
    H -->|"yes"| I["Approve and squash-merge into the integration branch<br/>delete the remote branch"]
    I --> J{"auto-promote and auto-approve both on?"}
    J -->|"yes"| J1["Promote the integration branch to the base branch"]
    J -->|"no"| K
    J1 --> K
    H -->|"no"| K["Leave the PR for a person"]
    K --> L["Post the Completed or Reworked comment<br/>add Ready For Testing"]
    L --> M["Architecture docs review<br/>only if enabled, only after delivery,<br/>never raises, never edits the repo"]
    M --> N["record_completed<br/>finish execution history<br/>clear the in-progress checkpoint"]
```

The order is mandatory: **push, then PR, then comment and label.** A failure in
any delivery step raises and leaves the issue exactly as it was before the
round. A reused branch name is never proof that a commit was delivered; an
"already merged" shortcut must prove the specific commit is an ancestor of the
merged one.

---

## 8. Pauses, holds and recovery

```mermaid
flowchart TD
    A["Something stops forward progress"] --> B{"What?"}
    B -->|"Provider usage below its minimum"| Q["Quota pause<br/>save session, phase, round and epoch<br/>post the quota comment, exit 11"]
    Q --> Q1["Later: resume the same session<br/>or hand off to another provider<br/>post the Resumed comment"]
    B -->|"Needs credentials, authority or an outside action"| N["AI Needs Input<br/>comment and label, wait"]
    N --> N1["A trusted author replies:<br/>the issue is picked up as a follow-up"]
    B -->|"Same deterministic failure 3 times"| H["Automation Failed hold"]
    B -->|"Strict adversarial loop stopped converging"| H
    H --> H1["Label Automation Failed, one comment,<br/>branch and checkpoint kept,<br/>park_held_issue, queue continues"]
    H1 --> H2["Released by a newer worker build or app version,<br/>or by removing the label<br/>fresh budget, Resumed transition posted"]
    B -->|"Self-recovering condition"| RL["exit 15<br/>quiet, retried next pass"]
    B -->|"Tester answer rejected 3 times"| BE["Best-effort delivery<br/>even in strict mode"]
    B -->|"Missing commit or dirty worktree"| H
    B -->|"Provider refuses the prompt as too large"| PL["bounded_patch()<br/>retry once with a smaller patch summary"]
    PL -->|"still refused"| H
```

| Situation | Result | Comment on the issue |
|---|---|---|
| Usage runs out mid-run | exit 11, session saved, resumes later | quota paused, then resumed |
| AI needs a person | `AI Needs Input` label, waits for a trusted reply | AI needs your input |
| Question issue | code-free answer, no `Ready For Testing` | AI answer |
| Same failure 3 times | held and parked | Automation needs attention |
| Prompt refused as too large, again after a smaller retry | held and parked at once | Automation needs attention |
| Strict loop stalls or exhausts its budget | held and parked | Automation needs attention, plain language |
| Protected test blocks the fixer | `adversarial-protected`, fresh adjudicator | routed to protected-test review |
| Tester result rejected 3 times | best-effort delivery | completion comment naming the rejection |

---

## 9. Limits and counts

Values are checked against the code by `test_worker_flow_docs.py`; change both
together.

| Constant | Value | Meaning |
|---|---|---|
| `MAX_ROUNDS` | 3 | counted fix and re-test rounds in one epoch (round 0 is not counted) |
| `STRICT_EPOCHS_PER_RUN` | 3 | strict epochs per worker process before exit 13 |
| `STALLED_EPOCHS_BEFORE_STRONGEST` | 2 | stalled epochs before the strongest model is used |
| `STALLED_EPOCHS_BEFORE_HOLD` | 4 | stalled epochs in a row before the issue is held |
| `MAX_EPOCHS_PER_BUDGET` | 12 | epochs since the budget began before the issue is held |
| `MAX_HOURS_PER_BUDGET` | 8 | hours since the budget began before the issue is held |
| `FUTILE_EMPTY_FIXES` | 3 | empty fixes for the same failure and choice before the fixer is skipped |
| `AUTOMATION_FAILURE_LIMIT` | 3 | identical failures before a hold (also the tester-rejection bound) |
| `ADVERSARIAL_DIFF_MAX_CHARS` | 400000 | characters of patch embedded in a tester prompt; over it the patch is summarised, and halved for the one retry after a provider refuses the prompt as too large |
| `MAX_COMMIT_FILES` | 500 | staged files above which a worker commit fails instead of flowing into prompts and the pull request |
| `MAX_COMMIT_CHANGED_LINES` | 200000 | staged changed lines above which a worker commit fails |
| `MAX_FILED_PER_STAGE` | 5 | follow-up issues one stage files per source issue |
| `MAX_CANDIDATES` | 8 | open issues sent to the duplicate judge |
| `DUPLICATE_MIN_CONFIDENCE` | 0.7 | confidence needed to suppress a duplicate |
| `DUPLICATE_MIN_CONFIDENCE_SEVERE` | 0.9 | confidence needed for High and Critical findings |
| `MAX_CONTEXT_ENTRIES` | 5 | rediscovery notes kept in a tracked issue's description |
| `MAX_OVERFLOW_ENTRIES` | 30 | held-back findings kept in an overflow issue |

Counting rules

- Round 0 plus up to 3 counted rounds is the first epoch; each later epoch adds
  3 counted rounds. Round numbers are cumulative; the epoch is
  `(round - 1) // 3 + 1`.
- The first epoch's boundary logs read `N of 3`; later epochs append
  `in epoch N`.
- Worst case for one stage in strict mode before a hold: 4 stalled epochs, so
  12 counted rounds, fewer when progress is made, never more than 12 epochs or
  8 hours.
- Test counts in history are test files changed and suites failing. Capacity
  consumed is an approximate percentage-point drop in remaining quota, never
  tokens or dollars.

---

## 10. Comments and labels on the issue

One comment per transition, each opening with a unique HTML marker so a retry
never double-posts. Every provider uses the same shape.

| Transition | Function | Label effect |
|---|---|---|
| Started | `post_started_comment` | none |
| Completed or Reworked | `post_pending_comment` | adds `Ready For Testing` |
| Stopped for usage | `post_quota_comment` | none |
| Resumed | `post_resumed_comment` | none |
| AI needs input | `finalize_needs_input` | adds `AI Needs Input`, removes `Ready For Testing` |
| Question answered | `finalize_question_answer` | never adds `Ready For Testing` |
| Automation failed | `post_automation_failure` | adds `Automation Failed` |
| Routed to protected-test review | `route_protected_test_block` | adds `adversarial-protected` |

The adversarial stages are part of **one** work-round: one Started comment and
one terminal comment, with a line per enabled stage. There are no per-round
GitHub comments. Follow-up issues the stages file carry `adversarial-uat` or
`adversarial-security`, and `adversarial-protected` when they cite a protected
suite.

---

## 11. Where state lives

Under the repository's worker state directory
(`~/.local/state/swarm-issue-worker/<repo>/` on the desktop):

| File or folder | Holds |
|---|---|
| `in-progress-issue.json` | the active attempt: provider, session, routing decision, each adversarial stage's phase, round, epoch, approaches, filed and held-back findings, usage events |
| `integration-recovery.json` | a pinned `ai-main` <- `main` merge in progress, resumed by the next run |
| `worker.lock` | the per-repository lock |
| `completed-issues` | issue numbers already delivered |
| `quota-paused-issues/` | attempts waiting for usage |
| `parked-issues/` | held attempts, released by a new build or a removed label |
| `abandoned/` | archived attempts for closed or unassigned issues |
| `last-ai-output.log`, `last-ai-diagnostic.log` | the latest AI session's output and diagnostics |
| `../swarm-automation.sqlite3` | execution history: rounds, epochs, usage, filed findings |

The checkpoint is what makes every pause, hold and yield resumable. It is
deleted or archived when the issue finishes.

---

## 12. Code map

| Area | Where |
|---|---|
| Scheduler | `install_swarm_issue_cron.py` |
| Run order and issue selection | `Worker.run()`, `select_issue()` in `swarm_issue_worker.py` |
| Implementation pass | `run_selected_issue()` |
| Adversarial loop | `run_adversarial_stage()`, `close_adversarial_epoch()`, `hold_stalled_stage()` in `adversarial_core.py` |
| Stage definitions | `adversarial_uat.py`, `adversarial_security.py` |
| Fixer skip | `fixer_is_futile()` |
| Finding filing | `file_stage_findings()`, `judge_duplicate()`, `note_rediscovery()`, `record_overflow_findings()` |
| Duplicate rules | `finding_dedup.py` |
| Delivery | `finalize_issue()`, `deliver_pull_request()`, `synchronize_issue_delivery()` |
| Holds | `record_automation_failure()`, `park_held_issue()` |
