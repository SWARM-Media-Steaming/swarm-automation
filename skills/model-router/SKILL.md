# Dynamic Model Router (issue #195)

A reusable, provider-agnostic component whose only job is choosing the least
expensive, least token-intensive provider/model/reasoning-effort combination
reasonably expected to complete a given task — never simply the strongest
model available. It does not decide *what* a task is; that classification
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
  + context_fit              # token efficiency + SWE-Atlas-QnA blend
  + cost_efficiency           \
  + token_efficiency           > weighted by relative_cost / _token_efficiency /
  + latency                   / _latency; the matching weight is multiplied by
  |                         # sensitivity_boost when the request flags that
  |                         # dimension (cost_sensitive/token_sensitive/
  |                         # latency_sensitive)
  + reliability              # coding_capability, discounted when its data is
  |                         # HEURISTIC rather than MEASURED — only weighted
  |                         # for task types that need it (deep_reasoning group)
  - overqualification_penalty        # relative_capability beyond what the band needs
  - unnecessary_reasoning_penalty    # effort levels beyond the band's floor
```

Weights live in `routing-rules.yaml`'s `weights` (the issue's initial BALANCED
preset) plus each task type group's `extra_weights`. The highest-scoring
candidate wins; candidates within `tie_break_margin` of the top score are
treated as equivalent and the cheapest (then lowest-effort) one is chosen —
"if two adjacent options perform similarly, pick the less expensive one."
`preferred_provider` in `RoutingAvailability` only breaks ties among those
near-equal candidates; it never overrides a clearly better-scoring one.

Output matches the issue's documented shape (`RoutingDecision.as_dict()`):
`provider`, `agent`, `model`, `effort`, `complexity`, `task_type`,
`confidence`, `reason`, plus up to two `alternatives`.

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
efforts, and unknown models. Run with `python3 -m unittest test_model_router
test_model_router_yaml` from `issue_worker/` (not pytest — see
`test_swarm_issue_worker.py`'s module docstring for why).

## Future tuning

`_score_candidate` in `model_router.py` has no historical-performance term
yet. Adding one (success rate / cost / runtime by model and task type, from
`issue_worker/ai_execution_history.py`) means adding a `historical_performance`
component to `_score_candidate`'s `components` dict and a matching weight in
`routing-rules.yaml`, the same shape as every other term — this file's
`weights` are meant to absorb that without a new abstraction.
