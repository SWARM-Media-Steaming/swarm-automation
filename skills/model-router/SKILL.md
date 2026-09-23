# Dynamic Model Router (issue #195)

A reusable, provider-agnostic component that chooses a
provider/model/reasoning-effort combination reasonably expected to complete a
given task. When Cost Consideration is on, it prefers the least expensive,
least token-intensive combination that still clears a minimum success bar —
never simply the strongest model, and never a cheaper model that is not
expected to succeed. When Cost Consideration is off, dollar cost and token
consumption have no weight. It does not decide *what* a task is; that classification
(complexity, task type) is produced elsewhere (today, the existing pre-flight
AI grading call in `issue_worker/dynamic_router.py`). This router answers the
second question: given that classification and which providers/models/efforts
are actually available, which one should run it.

## Files

- `models.yaml` — the cross-provider model catalog: one entry per
  `(provider, model)`, its capability/cost/token-efficiency/latency ranks
  (1-5, comparable across providers), and — only where actually measured —
  benchmark data (Coding Agent Index, DeepSWE, Terminal-Bench, SWE-Atlas-QnA,
  cost/tokens/runtime per task). `model` slugs must match the exact strings
  the app already invokes providers with (`issue_worker/dynamic_router.py`'s
  `_MODEL_CATALOG`, `src/config.rs`'s `default_routing_tiers()`); a model
  missing here cannot be routed to. Adding a model needs no code change — add
  an entry and, once it has real usage, fill in its benchmark numbers.
- `routing-rules.yaml` — complexity bands (`TRIVIAL` through `EXTREME`), task
  type groupings and their benchmark emphasis, the scoring weights, and the
  overqualification / unnecessary-reasoning penalty terms. All of it is a
  prior the scoring formula reads, not a lookup table — see "How routing
  works" below.
- This file — how the two above fit together and how the engine scores a
  candidate.

## Engine

`issue_worker/model_router.py` is the actual scoring engine (pure Python
stdlib, no AI calls, no repository I/O — see its module docstring). It is
consumed by `issue_worker/dynamic_router.py`'s `_scored_tier_decision`, which
uses it whenever Dynamic Model Routing is enabled and the tool the pre-flight
grader picked needs a deterministic model/effort decision (its own free-choice
pick was invalid, or its tool pick was overruled) — see that module's
docstring for the full picture of how AI-graded classification and this
scoring engine fit together, and `skills/swarm-automation-dev/SKILL.md`'s
"Dynamic model routing" section for the surrounding feature.

`issue_worker/model_router_yaml.py` is a small, dependency-free loader for the
YAML subset these two files use (block/flow mappings and sequences, scalars,
`#` comments — see its module docstring for the exact grammar). Every other
module in `issue_worker/` sticks to the Python standard library, and the app
invokes the user's own `python3` with no install step, so this avoids adding
a PyYAML dependency the packaged app cannot guarantee is present.

Both YAML files are bundled into the packaged app as a sibling of
`issue_worker/` (see `tauri.conf.json`'s `bundle.resources`), so
`model_router.py`'s `_config_dir()` — `Path(__file__).resolve().parent.parent
/ "skills" / "model-router"` — resolves correctly both in a source checkout
and inside the installed app.

## Complexity levels

`TRIVIAL`, `SIMPLE`, `STANDARD`, `COMPLEX`, `VERY_COMPLEX`, `EXTREME` — see
`routing-rules.yaml`'s `complexity_bands`. Each band carries a
`min_capability` (1-5) a model should meet to be considered a safe fit, and
maps onto the existing 1-10 pre-flight complexity grade via `ai_grade_range`.
Falling short of `min_capability` does not exclude a model outright — it
lowers its `expected_success` score — so a constrained catalog (e.g. only one
provider enabled) still routes somewhere instead of raising.

## Task types

The twenty types listed in `TASK_TYPES` (`issue_worker/model_router.py`), each
in exactly one `task_type_groups` entry in `routing-rules.yaml`. A group sets
`benchmark_emphasis` (how much weight DeepSWE / Terminal-Bench / SWE-Atlas-QnA
get when scoring `coding_capability`) and `extra_weights` (bonus weight for a
scoring term, e.g. mechanical work upweights cost/token/latency efficiency,
repository comprehension upweights `context_fit`).

## How routing works

`route(RouteRequest, catalog=..., rules=..., availability=...)` scores every
`(model, effort)` pair that is both `active` and allowed by `availability`,
at every effort the model supports that is at or above the complexity band's
minimum effort floor (`min_effort_by_complexity`). For each candidate:

```
score =
    expected_success        # meets/falls short of the band's min_capability
  + task_fit                # keyword overlap between the model's declared
  |                         # strengths/weaknesses and the task type
  + coding_capability        # normalized DeepSWE/Terminal-Bench/SWE-Atlas-QnA/
  |                         # Coding Agent Index at this exact effort, weighted
  |                         # by the task type's benchmark_emphasis; falls back
  |                         # to relative_capability/5 (HEURISTIC) when no
  |                         # benchmark exists at that effort — a measured
  |                         # "max" result is never assumed to hold at
  |                         # "medium"/"high"
  + context_fit              # SWE-Atlas-QnA, else relative_capability
  + cost_efficiency           \  measured dollars/tokens at this exact effort
  + token_efficiency           > when data_quality is MEASURED; otherwise
  + latency                   /  relative_cost / _token_efficiency / _latency
  |                         # cost/token weights are 0 unless cost
  |                         # consideration is on; latency_sensitive and
  |                         # token_sensitive still apply sensitivity_boost
  + reliability              # coding_capability, discounted when its data is
  |                         # HEURISTIC rather than MEASURED — only weighted
  |                         # for task types that need it (deep_reasoning group)
  - overqualification_penalty        # relative_capability beyond what the band needs
  - unnecessary_reasoning_penalty    # effort levels beyond the band's floor
```

Weights live in `routing-rules.yaml`'s `weights` as two sets, selected by
`RouteRequest.cost_consideration_enabled` (the UI "Optimize routing for cost"
toggle, passed through as `routing_optimization` — never inferred from the
prompt):

- `cost_consideration_off` — expected success, task fit, coding, context, and
  latency. `cost_efficiency` and `token_efficiency` are 0.
- `cost_consideration_on` — the same capability terms plus independent
  `cost_efficiency` (estimated dollar cost) and `token_efficiency` weights.

Dollar cost and token efficiency are scored separately. Measured
`benchmark_cost_per_task` / `benchmark_tokens_per_task` are used only at the
exact effort they were recorded (`data_quality: MEASURED`); otherwise the
relative 1–5 ranks are used. Missing prices are never fabricated.

When cost consideration is on, candidates below `minimum_expected_success`
are removed first, then any remaining candidate more than
`cost_optimization_quality_tolerance` behind the strongest expected-success
score is removed, then the cost-aware weights pick the winner. Cost
optimization therefore cannot select a model the router already considers
underpowered. `unnecessary_reasoning_penalty_per_level` is multiplied by
`cost_consideration_unnecessary_reasoning_multiplier` so High is preferred
over XHigh/Max when both are expected to succeed.

Task type `extra_weights` still apply; cost/token extras are skipped while
cost consideration is off. The highest-scoring remaining candidate wins.
Within `tie_break_margin`, cost-on ties break toward cheaper then lower
effort; cost-off ties break toward higher capability. `preferred_provider`
in `RoutingAvailability` only breaks ties among those near-equal candidates;
it never overrides a clearly better-scoring one.

Output matches the documented shape (`RoutingDecision.as_dict()`):
`provider`, `agent`, `model`, `effort`, `complexity`, `task_type`,
`confidence`, `reason`, `cost_consideration_enabled`, plus up to two
`alternatives`.

## Availability and unknown models

`RoutingAvailability` filters by enabled agent (`claude`/`codex`/`grok`),
explicit disabled models, and an optional allow-list. A model named in an
allow-list that is not in `models.yaml` is simply inert — the router never
raises for referencing an unknown slug, it just cannot select it, per the
issue's "unknown/new models remain excluded from automatic routing until
enough metadata exists" requirement. If nothing is eligible after filtering,
`route()` raises `ModelRouterError` rather than guessing.

## Tests

`issue_worker/test_model_router.py` and `issue_worker/test_model_router_yaml.py`
— deterministic, no live API calls, covering complexity/task-type routing,
sensitivity flags, overqualification, disabled providers/models, unsupported
efforts, unknown models, and cost-consideration weight sets / thresholds
(issue #198). Run with `python3 -m unittest test_model_router
test_model_router_yaml` from `issue_worker/` (not pytest — see
`test_swarm_issue_worker.py`'s module docstring for why). End-to-end wiring
from the UI `routing_optimization` toggle is covered in
`test_dynamic_router.py`, `test_swarm_issue_worker.py`, and
`test_adversarial_uat.py`.

## Future tuning

`_score_candidate` in `model_router.py` has no historical-performance term
yet. Adding one (success rate / cost / runtime by model and task type, from
`issue_worker/ai_execution_history.py`) means adding a `historical_performance`
component to `_score_candidate`'s `components` dict and a matching weight in
`routing-rules.yaml`, the same shape as every other term — this file's
`weights` are meant to absorb that without a new abstraction.
