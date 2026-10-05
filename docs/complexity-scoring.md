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

## Routing cap (issue #401)

A repository may set, per provider, a ceiling model and effort. The router and
Jev run unchanged and their picks are recorded; the cap is a final clamp over
the router's pick, compared by `model_router.estimated_dollar_cost` (price and
effort aware, no rank table). A costlier pick is replaced by the cap's exact
model and effort; a cheaper or equal one stands and the Cap line says the cap
was not applied. The cap also bounds release upgrades (`latest_release`
`max_cost`), the adversarial tester floor and strict-epoch escalation, which
stop at the cap. If the cap sits below the capability floor, the cap wins and
the floor and expected-success gap are recorded. Manual pins and runs with
Dynamic Model Routing off are never capped. `routing_cap.py` owns the logic;
the record (cap, raw router pick, applied flag, final pick, estimated costs) is
`routing_decision.routing_cap` in the history database.

## Limitations

Only the Python analyzer measures cyclomatic complexity; other languages get
file-level counts and dependency heuristics. Coverage, duplication and cognitive
complexity are recorded only when a tool supplies them.
