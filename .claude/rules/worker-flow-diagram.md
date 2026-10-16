# Worker flow diagram rules

`docs/worker-flow.md` is the one reference for how the worker picks up, works,
attacks (adversarial UAT and cybersecurity), delivers and holds an issue. The
owner reads it to understand the system, so it must always describe what the
code does today.

**Update it in the same change** whenever you alter any of this:

- run order in `Worker.run()` or how `select_issue()` chooses (priority,
  dependencies, follow-ups, parking, CI monitoring);
- provider choice, routing, complexity analysis, handoff, quota pause/resume;
- the implementation pass outcomes (needs input, question, environment-only,
  no change, protected-test block) or the version policy;
- the adversarial loop in `adversarial_core.py`: round or epoch counts, strict vs
  best effort, escalation, stall holds or budgets, fixer skipping, tester
  rejection handling, stage order or ownership;
- how findings are handled: in-scope fixes, out-of-scope filing, duplicate
  detection, caps, overflow issues, rediscovery notes, protected-suite routing;
- delivery (sync with the integration branch, push, PR, approve, merge, promote,
  comments, labels, architecture docs);
- an exit code, a hold, a label, a lifecycle comment or a state file;
- any constant listed in the limits table.

How:

1. Edit the matching diagram and table in `docs/worker-flow.md`. Diagrams are
   Mermaid `flowchart` blocks; quote every label, use `<br/>` for line breaks,
   and never use a bare `end` as a node id. Name functions in backticks with
   `()` or as the first line of a node label so the test can check them.
2. Rebuild the visual page: `python3 scripts/build_worker_flow_html.py`. Commit
   `docs/worker-flow.html` and `docs/index.html` with it. They are generated;
   never edit them by hand.
3. Run `python3 -m unittest test_worker_flow_docs` from `issue_worker/`. It fails
   when a documented limit, exit code or function drifts from the code, when a
   diagram is malformed, or when the generated pages are stale.

Do not describe the flow in a second place that can drift; link to the diagram
instead. A new limit goes in the limits table (the test then keeps it honest).
A new top-level step gets a diagram section, a contents entry and a row in the
code map. The GitHub Pages site serves `docs/` (see `docs/index.html`).
