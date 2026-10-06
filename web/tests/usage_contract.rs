//! The worker's `UsageRecord` is the wire format of usage accounting.
//! `tests/fixtures/usage_record.json` is real `UsageRecord.to_dict()` output from
//! `issue_worker/token_usage.py`; `issue_worker/test_web_usage_contract.py`
//! checks the same file against the Python dataclass, so a renamed or dropped
//! field fails in both languages.

use swarm_web::model::Provider;
use swarm_web::usage::IngestBody;

#[test]
fn the_python_usage_records_deserialize_and_price_as_usage_report_would() {
    let raw = include_str!("fixtures/usage_record.json");
    let body: IngestBody = serde_json::from_str(raw).expect("the worker's records parse");
    assert_eq!(body.records.len(), 2);

    let priced = body.records[0].to_entry().unwrap();
    assert_eq!(
        (priced.provider, priced.cost_usd),
        (Provider::Claude, Some(0.0421))
    );
    // A model with no price: tokens reported, cost unknown. Counted as unpriced, never as zero.
    let unpriced = body.records[1].to_entry().unwrap();
    assert_eq!(
        (unpriced.provider, unpriced.cost_usd),
        (Provider::Codex, None)
    );
}
