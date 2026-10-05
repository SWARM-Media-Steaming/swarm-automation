# Dynamic Model Router (issue #195)

A reusable, provider-agnostic component that chooses a
provider/model/reasoning-effort combination reasonably expected to complete a
given task. Automatic routing is always cost-first after capability,
expected-success, safety, and context-fit gates: it prefers the least
expensive combination that still clears a minimum success bar — never simply
the strongest model, never a cheaper model that is not expected to succeed,
and never a faster model solely because it is faster. Isolated scoring tests
may still pass `cost_consideration_enabled=False` to exercise the non-cost
weight set. It does not decide *what* a task is; that classification
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
  `_MODEL_CATALOG`); feed and CLI discovery can add models without a row here. Metadata is inferred
  from the closest same-family peer and refined by feed measurements.
- `model-blacklist.json` — explicit retirements that take effect when their successor is CLI offered and
  priced. Same-family retirements can also be derived from measurements. Read by
  `issue_worker/available_models.py` and embedded in `src/tools.rs`; see
  "Model blacklist" below.
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
scoring engine fit together, and `.claude/skills/swarm-automation-dev/SKILL.md`'s
"Dynamic model routing" section for the surrounding feature.

### Jev interaction

When Jev is enabled, it supplies bounded typed signals such as task type,
complexity, security risk, and expected success to the existing Swarm router.
Jev is also asked which available model is the least expensive one that can do
the task well, but that `recommended_model` answer is advisory metadata and is
not directly applied as the final route. Swarm reruns its own scoring and
continues to own the provider/model/effort decision.

Automatic Jev/Swarm routing always uses the cost-on policy. Candidates below
the default `minimum_expected_success` of `0.80` are not eligible for the
cost-first choice, and candidates more than the configured
`cost_optimization_quality_tolerance` (`0.03` by default) behind the strongest
expected-success candidate are excluded before cost decides among the capable
remaining candidates. A blocking security finding, failed test, required UAT
or Cyber review, or another deterministic Swarm gate overrides Jev regardless
of confidence. See `docs/jev-decision-engine.md` for the decision-layer
confidence bands and safety contract.

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

## Automatic calibration (issues #205 and #374)

`issue_worker/model_calibration.py` provides the shared refresh service for
AI Configuration, startup, and scheduled/AI callers. Artificial Analysis overlays
the local catalog. Every validated refresh activates automatically; CLI-offered,
priced models become ACTIVE or CANDIDATE without approval. Regressions are
recorded in the diff and notification and never block activation. Missing keys
report not configured; failures keep the last good data and retry with backoff.
Contradictory observations for the same provider/model (including API aliases)
fail the entire refresh without publishing a proposal or changing active data.
Equal normalized observations and complementary fields can be combined.

Changes to supported efforts, strengths, weaknesses, and eligibility remain
visible in the diff even when representative routing examples do not change. These
inputs affect requests outside the simulation sample. Calibration version IDs
are never recycled after history pruning or rollback, and refresh summaries
retain their originating version independently of which version is active.

Starting worker and router models are derived the same way, not configured:
`dynamic_router.suggested_defaults` (surfaced as `routing_calculator.py
defaults`) returns the scoring router's pick for a simple and a trivial task. An
unset model means "auto" to the worker, and the desktop fills only empty
settings from it.

A model with no static or feed input/output price is excluded from routing,
the router prompt, derived tiers, Jev's model list and upgrades. Static prices
win; feed rates also record spend with calibration provenance.

Refresh cadence: the app re-checks every 15 minutes, and the service refreshes
once `model_data_min_refresh_interval_hours` has passed since the last success.
Data built by an older `ALGORITHM_VERSION` (before per-effort Intelligence Index
scores were folded into one row per model) is rebuilt regardless of the
interval, because it cannot rank a new release.

Every fresh adversarial tester or fixer session, like the primary run, passes
through `latest_release`: a model routed from a fallback tier or the
configured default moves to the newest release of its family when that is no
dearer and not measurably weaker. A session already started keeps its model.
This is not a separate scoring bonus; measured capability and cost still decide
which candidate wins. The upgrade only lands on a CLI-offered model with a static or feed price,
so its spend is recorded.

Each adversarial stage's router prompt also carries the graded complexity and
diff size of the change under test, and the stage logs the complexity it was
graded, so a small change is not tested by a top-tier model without a reason.
That grade never lowers a tester below a floor: the model the scoring router
itself picks for a STANDARD task (complexity 4, `dynamic_router.scored_floor`,
computed from the live catalog, never from a stored table) at medium effort or better, and, on the
same provider, no weaker than the first assessment's tester
(`apply_tester_floor`, recorded as `tester_reference`). A
tester that breaks an edit rule is rejected, its edits restored, and the next
attempt is told which rule it broke and what to do instead
(`rejection_remedy`, with `attempts` counted in `retry_rejection`).

The over-qualification penalty is waived for any model that costs no more than
the cheapest model *capable of the task*, so a newer release at its
predecessor's price is not penalized for being stronger.

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
`RouteRequest.cost_consideration_enabled` (always on for automatic Swarm/Jev
routing after issue #299; never inferred from the prompt):

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

## Model blacklist

A retirement in `model-blacklist.json` is dormant until its successor is offered
by the provider CLI and priced. `model_lifecycle.py` additionally derives
same-provider, same-family retirements under the shared price tolerance and
score margin. Retired rows stay as inactive, deprecated peers with
`superseded_by`; dormant predecessors remain usable. Fresh selections are
repaired only after retirement takes effect. Started sessions finish pinned.
See `.claude/rules/model-blacklist.md` and `docs/model-pricing.md`.

## Routing cap (per repository, per provider)

After the router's pick (and before any release upgrade) `routing_cap.clamp`
compares the pick's estimated cost with the cap's model at the cap's effort for
the same provider. Over the cap: the cap's exact pair is used, the raw pick stays
in `routing_decision.routing_cap`. At or under: the pick stands. Release upgrades
(`latest_release(max_cost=...)`), the tester floor and escalation are clamped the
same way and never loop past it. Manual pins and Dynamic Routing off are uncapped.
Retired cap models repair to their successor; unpriced ones are ignored with a log.
Jev's `recommended_model` stays advisory and is never capped directly.

## Repository-aware complexity (always on)

Before routing, every issue gets a complexity vector from the cached repository
and component profiles, interpreted by Jev (or the configured AI fallback, or a
deterministic fallback). The router applies the vector's capability, context and
security floors first and only then optimizes cost; it consumes the whole vector,
not one score. See `docs/complexity-scoring.md` and
`.claude/rules/repository-complexity-scoring.md`. There is no toggle.

## Measured native-cache evidence (issue #381)

When the repository's history holds enough recent measurements for the exact
provider, model, effort and agent role (`usage_report.cache_routing_evidence`:
at least 20 calls over five issues in 30 days, 95% success, complete priced
cache observations, one pricing rate), `build_router_prompt` includes that
evidence and saved-attempt cost comparisons apply it through
`dynamic_router.cache_adjusted_cost` (discount capped at 50%). It only adjusts
the API-equivalent cost estimate of the measured route. Cache evidence never
overrides the capability, expected-success, reasoning-effort, safety,
context-fit or reviewer-independence gates, never applies to an unmeasured
candidate, and is not subscription billing. Insufficient evidence leaves routing
unchanged. See `docs/prompt-caching.md`.

## Tests

`issue_worker/test_model_router.py` and `issue_worker/test_model_router_yaml.py`
— deterministic, no live API calls, covering complexity/task-type routing,
sensitivity flags, overqualification, disabled providers/models, unsupported
efforts, unknown models, and cost-consideration weight sets / thresholds
(issue #198). Run with `python3 -m unittest test_model_router
test_model_router_yaml` from `issue_worker/` (not pytest — see
`test_swarm_issue_worker.py`'s module docstring for why). End-to-end wiring
from automatic cost-first routing is covered in
`test_dynamic_router.py`, `test_swarm_issue_worker.py`, and
`test_adversarial_uat.py`.

## Future tuning

`_score_candidate` in `model_router.py` has no historical-performance term
yet. Adding one (success rate / cost / runtime by model and task type, from
`issue_worker/ai_execution_history.py`) means adding a `historical_performance`
component to `_score_candidate`'s `components` dict and a matching weight in
`routing-rules.yaml`, the same shape as every other term — this file's
`weights` are meant to absorb that without a new abstraction.
