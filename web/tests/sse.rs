//! Server-Sent Events (issue #419): tenant scoping, `Last-Event-ID` resume,
//! heartbeat, bounded backpressure and redaction.

mod common;

use std::time::Duration;

use axum::body::Body;
use axum::http::{header, Request, StatusCode};
use common::*;
use http_body_util::BodyExt;
use serde_json::Value;
use swarm_web::auth::session_cookie_name;
use swarm_web::model::Role;
use tower::ServiceExt;

const CANARY: &str = "sk-ant-api03-SSE-CANARY-0123456789";

fn world(extra: &[(&str, &str)]) -> TestApp {
    let app = TestApp::with_env(extra);
    app.github.add_user(
        "code-alice",
        1,
        "alice",
        vec![installation(100, "alice", "User", Role::Owner)],
    );
    app.github.add_user(
        "code-bob",
        2,
        "bob",
        vec![installation(200, "bob", "User", Role::Owner)],
    );
    app.github.add_user(
        "code-dana",
        4,
        "dana",
        vec![
            installation(100, "alice", "User", Role::Member),
            installation(200, "bob", "User", Role::Member),
        ],
    );
    app
}

struct Stream {
    body: Body,
    pub status: StatusCode,
    pub content_type: String,
}

async fn open(
    app: &TestApp,
    client: &Client<'_>,
    path: &str,
    last_event_id: Option<&str>,
) -> Stream {
    let mut builder = Request::builder().uri(path).header(
        header::COOKIE,
        format!("{}={}", session_cookie_name(app.secure), client.session),
    );
    if let Some(id) = last_event_id {
        builder = builder.header("last-event-id", id);
    }
    let response = app
        .router
        .clone()
        .oneshot(builder.body(Body::empty()).unwrap())
        .await
        .unwrap();
    Stream {
        status: response.status(),
        content_type: response
            .headers()
            .get(header::CONTENT_TYPE)
            .map(|v| v.to_str().unwrap().to_string())
            .unwrap_or_default(),
        body: response.into_body(),
    }
}

/// One SSE event: `id`, `event`, `data` (or a comment).
#[derive(Debug, Clone)]
struct Sse {
    id: Option<u64>,
    event: Option<String>,
    data: Option<Value>,
    comment: Option<String>,
}

impl Stream {
    /// Read events until `done` says so (or `max` events), failing on silence.
    async fn read_until(
        &mut self,
        wait: Duration,
        mut done: impl FnMut(&[Sse]) -> bool,
    ) -> Vec<Sse> {
        let mut events = Vec::new();
        let mut buffer = String::new();
        while !done(&events) {
            let frame = tokio::time::timeout(wait, self.body.frame())
                .await
                .unwrap_or_else(|_| panic!("the stream went quiet; got {events:?}"))
                .expect("the stream ended")
                .expect("a frame");
            buffer.push_str(&String::from_utf8_lossy(
                &frame.into_data().unwrap_or_default(),
            ));
            while let Some(end) = buffer.find("\n\n") {
                let block: String = buffer.drain(..end + 2).collect();
                events.push(parse(&block));
            }
        }
        events
    }
}

fn parse(block: &str) -> Sse {
    let mut event = Sse {
        id: None,
        event: None,
        data: None,
        comment: None,
    };
    for line in block.lines() {
        if let Some(value) = line.strip_prefix("id:") {
            event.id = value.trim().parse().ok();
        } else if let Some(value) = line.strip_prefix("event:") {
            event.event = Some(value.trim().to_string());
        } else if let Some(value) = line.strip_prefix("data:") {
            event.data = serde_json::from_str(value.trim()).ok();
        } else if let Some(value) = line.strip_prefix(':') {
            event.comment = Some(value.trim().to_string());
        }
    }
    event
}

fn lines(events: &[Sse]) -> Vec<String> {
    events
        .iter()
        .filter(|e| e.event.as_deref() == Some("automation-log"))
        .map(|e| {
            e.data.as_ref().unwrap()["line"]
                .as_str()
                .unwrap()
                .to_string()
        })
        .collect()
}

const WAIT: Duration = Duration::from_secs(3);

#[tokio::test]
async fn a_stream_needs_a_session_and_is_event_stream() {
    let app = world(&[]);
    let anonymous = app.get("/api/v1/events/automation-log").await;
    assert_eq!(anonymous.status, StatusCode::UNAUTHORIZED);
    let alice = app.sign_in("code-alice").await;
    for path in [
        "/api/v1/events/automation-log",
        "/api/v1/events/jobs",
        "/api/v1/events/model-calibration",
    ] {
        let stream = open(&app, &alice, path, None).await;
        assert_eq!(stream.status, StatusCode::OK, "{path}");
        assert!(
            stream.content_type.starts_with("text/event-stream"),
            "{path}: {}",
            stream.content_type
        );
    }
}

#[tokio::test]
async fn live_lines_carry_only_the_signed_in_users_tenants_and_are_redacted() {
    let app = world(&[]);
    let alice = app.sign_in("code-alice").await;
    let (a, b) = (
        alice.tenant_for("alice").await,
        app.sign_in("code-bob").await.tenant_for("bob").await,
    );
    let mut stream = open(&app, &alice, "/api/v1/events/automation-log", None).await;
    let events = stream.read_until(WAIT, |e| !e.is_empty()).await;
    assert_eq!(events[0].comment.as_deref(), Some("ready"));

    app.state
        .events
        .publish_log(&b, "bob/secret", 7, "bob only line", 10);
    app.state.events.publish_log(
        &a,
        "acme/demo",
        419,
        "Adversarial UAT for issue #419: tester Codex model m with effort high.",
        11,
    );
    app.state
        .events
        .publish_log(&a, "acme/demo", 419, &format!("calling with {CANARY}"), 12);
    let got = stream.read_until(WAIT, |e| lines(e).len() == 2).await;
    let text = format!("{got:?}");
    assert!(
        !text.contains("bob only"),
        "another tenant's line reached alice: {text}"
    );
    assert!(!text.contains(CANARY), "{text}");
    let lines = lines(&got);
    assert_eq!(
        lines[0],
        "Adversarial UAT for issue #419: tester Codex model m with effort high."
    );
    assert!(lines[1].contains("[REDACTED]"));
    let payload = got
        .iter()
        .find(|e| e.event.is_some())
        .unwrap()
        .data
        .as_ref()
        .unwrap()
        .clone();
    assert_eq!(payload["tenant"], a.as_str());
    assert_eq!(payload["source"], "Issue worker");
    assert_eq!(payload["stream"], "stdout");
    assert_eq!(payload["timestamp"], 11);

    // The per-issue stream projects the same frames in the job-log shape.
    let mut jobs = open(&app, &alice, "/api/v1/events/jobs", Some("0")).await;
    let job_events = jobs
        .read_until(WAIT, |e| {
            e.iter()
                .filter(|x| x.event.as_deref() == Some("job-log"))
                .count()
                == 2
        })
        .await;
    let job = job_events
        .iter()
        .find(|e| e.event.is_some())
        .unwrap()
        .data
        .as_ref()
        .unwrap()
        .clone();
    assert_eq!(job["repository"], "acme/demo");
    assert_eq!(job["issue"], 419);
    assert!(job.get("source").is_none());
}

#[tokio::test]
async fn last_event_id_resumes_without_gaps_or_duplicates() {
    let app = world(&[]);
    let alice = app.sign_in("code-alice").await;
    let a = alice.tenant_for("alice").await;
    let ids: Vec<u64> = (1..=5)
        .map(|n| {
            app.state
                .events
                .publish_log(&a, "acme/demo", 1, &format!("line {n}"), 100 + n)
        })
        .collect();

    let mut stream = open(
        &app,
        &alice,
        "/api/v1/events/automation-log",
        Some(&ids[1].to_string()),
    )
    .await;
    let got = stream.read_until(WAIT, |e| lines(e).len() == 3).await;
    assert_eq!(lines(&got), ["line 3", "line 4", "line 5"]);
    assert!(
        got.iter().filter_map(|e| e.id).eq(ids[2..].iter().copied()),
        "frames carry their ids"
    );

    // Lines published while connected continue the same id sequence.
    let next = app
        .state
        .events
        .publish_log(&a, "acme/demo", 1, "line 6", 106);
    let more = stream.read_until(WAIT, |e| lines(e).len() == 1).await;
    assert_eq!(lines(&more), ["line 6"]);
    assert_eq!(more.last().unwrap().id, Some(next));
    assert!(next > ids[4]);

    // Resuming from the last id replays nothing; a garbage id replays everything.
    let mut fresh = open(
        &app,
        &alice,
        "/api/v1/events/automation-log",
        Some(&next.to_string()),
    )
    .await;
    let events = fresh.read_until(WAIT, |e| !e.is_empty()).await;
    assert!(lines(&events).is_empty());
    let mut all = open(
        &app,
        &alice,
        "/api/v1/events/automation-log",
        Some("not-a-number"),
    )
    .await;
    let everything = all.read_until(WAIT, |e| lines(e).len() == 6).await;
    assert_eq!(lines(&everything).len(), 6);
}

#[tokio::test]
async fn resume_never_replays_another_tenants_history() {
    let app = world(&[]);
    let alice = app.sign_in("code-alice").await;
    let bob = app.sign_in("code-bob").await;
    let (a, b) = (alice.tenant_for("alice").await, bob.tenant_for("bob").await);
    app.state
        .events
        .publish_log(&b, "bob/secret", 7, "bob line 1", 1);
    app.state
        .events
        .publish_log(&a, "acme/demo", 1, "alice line", 2);
    app.state
        .events
        .publish_log(&b, "bob/secret", 7, "bob line 2", 3);

    let mut as_alice = open(&app, &alice, "/api/v1/events/automation-log", Some("0")).await;
    let got = as_alice.read_until(WAIT, |e| lines(e).len() == 1).await;
    assert_eq!(lines(&got), ["alice line"]);

    // A user in both tenants gets both, in id order.
    let dana = app.sign_in("code-dana").await;
    let mut both = open(&app, &dana, "/api/v1/events/automation-log", Some("0")).await;
    let all = both.read_until(WAIT, |e| lines(e).len() == 3).await;
    assert_eq!(lines(&all), ["bob line 1", "alice line", "bob line 2"]);
}

#[tokio::test]
async fn a_heartbeat_keeps_an_idle_stream_alive() {
    let app = world(&[("SWARM_WEB_SSE_HEARTBEAT_SECS", "1")]);
    let alice = app.sign_in("code-alice").await;
    let mut stream = open(&app, &alice, "/api/v1/events/automation-log", None).await;
    let events = stream
        .read_until(Duration::from_secs(4), |e| {
            e.iter().any(|x| x.comment.as_deref() == Some("heartbeat"))
        })
        .await;
    assert!(
        events
            .iter()
            .any(|e| e.comment.as_deref() == Some("heartbeat")),
        "{events:?}"
    );
}

#[tokio::test]
async fn a_slow_reader_is_told_to_resync_and_memory_stays_bounded() {
    let app = world(&[]);
    let alice = app.sign_in("code-alice").await;
    let a = alice.tenant_for("alice").await;
    let mut stream = open(&app, &alice, "/api/v1/events/automation-log", None).await;

    // Far more than the live channel holds, published while the client reads nothing.
    let total = swarm_web::events::LIVE_CAPACITY * 4;
    for n in 0..total {
        app.state
            .events
            .publish_log(&a, "acme/demo", 1, &format!("burst {n}"), 1);
    }
    let got = stream
        .read_until(Duration::from_secs(10), |e| {
            e.iter().any(|x| x.event.as_deref() == Some("resync"))
        })
        .await;
    let resync = got
        .iter()
        .find(|e| e.event.as_deref() == Some("resync"))
        .unwrap();
    assert_eq!(resync.data.as_ref().unwrap()["reason"], "lagged");
    // Everything delivered is in order with no duplicates.
    let ids: Vec<u64> = got
        .iter()
        .filter(|e| e.event.as_deref() == Some("automation-log"))
        .filter_map(|e| e.id)
        .collect();
    assert!(ids.windows(2).all(|w| w[0] < w[1]), "ids strictly increase");

    // The replay ring is bounded: older lines are dropped, never buffered forever.
    for n in 0..swarm_web::events::RING_CAPACITY + 50 {
        app.state
            .events
            .publish_log(&a, "acme/demo", 1, &format!("more {n}"), 1);
    }
    let kept = app.state.events.recent_logs(&a, usize::MAX);
    assert_eq!(kept.len(), swarm_web::events::RING_CAPACITY);
    assert!(kept
        .last()
        .unwrap()
        .ends_with(&format!("more {}", swarm_web::events::RING_CAPACITY + 49)));
}

#[tokio::test]
async fn resuming_past_the_ring_says_history_was_truncated() {
    let app = world(&[]);
    let alice = app.sign_in("code-alice").await;
    let a = alice.tenant_for("alice").await;
    let first = app
        .state
        .events
        .publish_log(&a, "acme/demo", 1, "ancient", 1);
    for n in 0..swarm_web::events::RING_CAPACITY + 10 {
        app.state
            .events
            .publish_log(&a, "acme/demo", 1, &format!("recent {n}"), 2);
    }
    let mut stream = open(
        &app,
        &alice,
        "/api/v1/events/automation-log",
        Some(&first.to_string()),
    )
    .await;
    let got = stream
        .read_until(WAIT, |e| {
            e.iter().any(|x| x.event.as_deref() == Some("resync"))
        })
        .await;
    let resync = got
        .iter()
        .find(|e| e.event.as_deref() == Some("resync"))
        .unwrap();
    assert_eq!(resync.data.as_ref().unwrap()["reason"], "history_truncated");
}

#[tokio::test]
async fn calibration_refreshes_stream_to_the_tenants_members_only() {
    let app = world(&[]);
    let alice = app.sign_in("code-alice").await;
    let bob = app.sign_in("code-bob").await;
    let (a, b) = (alice.tenant_for("alice").await, bob.tenant_for("bob").await);
    let mut stream = open(&app, &alice, "/api/v1/events/model-calibration", None).await;
    stream.read_until(WAIT, |e| !e.is_empty()).await;
    app.state
        .events
        .publish_calibration(&b, serde_json::json!({ "status": "bob" }));
    app.state.events.publish_calibration(
        &a,
        serde_json::json!({ "status": "refreshed", "note": CANARY }),
    );
    let got = stream
        .read_until(WAIT, |e| e.iter().any(|x| x.event.is_some()))
        .await;
    let event = got.iter().find(|e| e.event.is_some()).unwrap();
    assert_eq!(event.event.as_deref(), Some("model-calibration-refreshed"));
    let data = event.data.as_ref().unwrap();
    assert_eq!(data["status"], "refreshed");
    assert!(!data.to_string().contains(CANARY));
}
