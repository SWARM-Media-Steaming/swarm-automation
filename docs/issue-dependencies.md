# Issue dependencies

The issue worker honors dependencies between issues, so an issue is not started
against a codebase that lacks its prerequisite.

## Syntax

`Depends on #N`, `Blocked by #N`, `Requires #N` (case-insensitive, optional colon,
lists such as `#1, #2 and #3`) and `owner/repo#N` for a repository with the same
owner. They are read from the issue body and from comments by trusted authors
(`--trusted-followup-author`). References inside fenced code blocks or inline code
are ignored, as are other owners, the issue itself and anything after the first 20
references (logged). The parser is one linear pass with input, line and count caps.

## Gating

Only first-pass issues are gated (follow-ups and resumed work already started).
A dependency is satisfied when it is closed and a merged pull request that delivers it
(branch `.../issue-N`, `#N` in the title, or a closing keyword in the body) targets
the integration or base branch. It is not satisfied when it is open, closed without
merged work (including "not planned"), or could not be checked: a GitHub error is
retried on the next tick and never fails the issue or aborts the tick. Every tick
re-evaluates; blocked issues are skipped while priority and lowest-number order
among eligible issues is unchanged. Cycles among waiting issues are detected
(Tarjan) and reported instead of waited on forever.

## Comments and labels

Idempotent HTML-marker comments, following `.claude/rules/issue-lifecycle-comments.md`:

- `swarm-issue-worker:waiting:issue:<n>;on:<repo#N,...>`: "Waiting on dependencies"
  (once per blocker set since the last release). No Started comment, since no work began.
- `swarm-issue-worker:dependency-released:issue:<n>;on:...`: once, when a waiting issue
  becomes eligible. The normal Started comment follows when work begins.
- `swarm-issue-worker:dependency-cycle:issue:<n>;members:...`.

No label is applied: labels drive other automation (`AI Needs Input`,
`Ready For Testing`) and the comment plus the re-evaluated gate carry the state.

## Prompt and Jev context

Each satisfied dependency adds its number, title, merged PR title and up to six
changed files to the AI prompt and to the Jev issue context (`[prerequisites]`),
sanitized, one line each, capped at 1,800 characters, labelled reference data. The
dependency count is not a routing input; `SCORING_VERSION` is unchanged.
