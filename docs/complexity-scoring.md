# Repository-aware complexity scoring

Always enabled; no setting disables it. Implementation:
`issue_worker/repository_complexity.py` (profiling, scoring, formatting, store),
`issue_worker/complexity_worker.py` (worker wiring), and
`dynamic_router.apply_complexity_requirements` (routing). Tests:
`issue_worker/test_repository_complexity.py`.

## Boundaries

| Stage | Owner |
| --- | --- |
| Measurement | local pluggable analyzers, cached/versioned profiles |
| AI interpretation | Jev, else the configured AI fallback; deterministic fallback with reduced confidence |
| Routing | dynamic router: capability/context/security floors, then cost |
| Execution | worker agent |
| Validation | adversarial UAT / Cybersecurity |
| Calibration | recorded prediction vs actual, deterministic and versioned |

Flow: Repository -> static analysis -> repository + component profiles -> issue
-> relevant context retrieval -> Jev/AI evaluation -> requirements vector ->
dynamic router -> worker -> adversarial agents -> actual metrics -> history.

## Profiles

Full scan when no compatible profile exists; diff-aware refresh after the
default branch advances; weekly full verification (also after a large change).
Each profile records profile version, commit, timestamp, scoring version and
analyzer versions. Unavailable metrics are listed, never invented. Per-component
profiles let a small UI change avoid the repository-wide score.

## Vector and requirements

implementation_complexity, change_surface, architecture_risk, security_risk,
uncertainty, repository_complexity, relevant_component_complexity (0-100) and
confidence (0-1), plus scope indicators (files/modules, services, database, API,
infrastructure, security-sensitive). Requirements: capability floor, reasoning,
context requirement, estimated fix rounds. High change surface raises context;
high uncertainty raises reasoning; high security risk raises the security-reasoning
floor and informs adversarial cybersecurity; high architecture risk raises the
capability and reasoning floor.

## Output and persistence

Issues get a `Complexity Analysis` section, then the `Routing Decision`. The
history database holds all metrics, vectors, requirements, selected model and
effort, candidate scores, versions and commit, plus predicted vs actual (files,
rounds, UAT/Cyber findings, tokens, cost, outcome). Similar completed issues
inform difficulty, rounds and confidence with sample-size damping.

## Limitations

Only the Python analyzer measures cyclomatic complexity; other languages get
file-level counts and dependency heuristics. Coverage, duplication and cognitive
complexity are recorded only when a tool supplies them.
