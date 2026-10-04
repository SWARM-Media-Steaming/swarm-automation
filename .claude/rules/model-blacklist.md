# Model retirement rules

`skills/model-router/model-blacklist.json` records explicit predecessor/successor
relationships. An entry is dormant until its successor is both offered by that
provider CLI and priced by the static catalog or active feed calibration.
This rule applies to every entry, including GPT-6 Sol → GPT-6.1 Sol. While
it is dormant, the predecessor stays routable and saved selections stay intact.

Python (`available_models.py`) and Rust (`src/tools.rs`) read the same JSON.
Rust consumes the price and derived-retirement snapshot published atomically
with the active calibration; both readers check current CLI availability.
Keep price and availability evidence keyed by provider. A model name returned
by a different provider's CLI cannot activate a retirement, including a
derived one. Older snapshots recover price ownership from their model rows.
Keep the cross-language dormancy test passing when changing this contract.

`model_lifecycle.py` also derives retirements within the same provider and
family: a newer CLI-offered, priced release must cost no more than
`UPGRADE_PRICE_TOLERANCE` and cannot lose more than `UPGRADE_SCORE_MARGIN`
at any measured common effort. These thresholds are shared with
`dynamic_router.latest_release`; do not duplicate them.

Retired rows remain in `models.yaml`, `_MODEL_CATALOG`, and calibration history
as inactive, deprecated peers with `superseded_by`. They supply inference
metadata and historical provenance; never delete them to retire a model.
A dormant row is restored when the CLI still offers it. Models with no price
are excluded from fresh routing and option lists.

An active retirement removes the predecessor from fresh routing, upgrades,
Jev's advisory list, and option lists, and repairs saved selections into the
successor. Started sessions finish on their pinned model. Every retirement
transition is logged once and included in the calibration diff/notification.

A new feed model needs no static catalog or pricing edit: validation, CLI
availability and feed prices suffice. The explicit JSON list is an override
for known successor relationships, not the only retirement mechanism.
A dormant explicit entry does not prevent a derived retirement to a qualifying
later release when the CLI skips the named successor.
