//! Live updates: the hub behind every Server-Sent Events stream.
//!
//! One [`EventHub`] holds a bounded replay ring per tenant and a bounded
//! broadcast channel. Every frame carries the tenant it belongs to and a
//! process-wide increasing id, so:
//!
//! * a stream only ever carries tenants the signed-in user belongs to;
//! * `Last-Event-ID` resumes from the ring (a gap past the ring is announced
//!   with a `resync` event so the client refetches `/logs`);
//! * a slow client cannot grow memory: the broadcast channel and the
//!   per-connection queue are bounded, a lagging reader is told to `resync`
//!   and caught up from the ring;
//! * everything is scrubbed by [`redact_value`] **before** it is stored or sent.
//!
//! A log line is one frame that several streams project: `automation-log`
//! (the desktop's `LogEvent`: `source`, `stream`, `line`, `timestamp`) and
//! `job-log` (the per-issue stream). The lines are passed through verbatim, so
//! the `Adversarial UAT for issue #...` / `Adversarial Cybersecurity for issue
//! #...` boundary logs the Overview replays keep their format.

use std::collections::{HashMap, HashSet, VecDeque};
use std::convert::Infallible;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::Mutex;
use std::time::Duration;

use axum::http::HeaderMap;
use axum::response::sse::{Event, KeepAlive, Sse};
use axum::response::IntoResponse;
use serde_json::{json, Value};
use tokio::sync::{broadcast, mpsc};
use tokio_stream::wrappers::ReceiverStream;

use crate::auth::Authed;
use crate::error::ApiError;
use crate::redact::{redact_text, redact_value};
use crate::state::AppState;

/// Frames kept per tenant for replay and `get_recent_logs` (the UI asks for 5000).
pub const RING_CAPACITY: usize = 5000;
/// Frames the broadcast channel buffers before a reader is told it lagged.
pub const LIVE_CAPACITY: usize = 512;
/// Frames queued for one connection before the sender waits.
const CONNECTION_QUEUE: usize = 64;
const RECHECK_EVERY: Duration = Duration::from_secs(30);
pub const DEFAULT_HEARTBEAT_SECS: u64 = 15;

/// The SSE event names the web serves (`ui/api.js` `EVENTS`).
pub const STREAMS: &[&str] = &["automation-log", "job-log", "model-calibration-refreshed"];

#[derive(Clone, Debug)]
pub enum Topic {
    /// One worker/orchestrator log line.
    Log,
    /// The model calibration was refreshed.
    Calibration,
}

#[derive(Clone, Debug)]
pub struct Frame {
    pub id: u64,
    pub tenant: String,
    pub topic: Topic,
    pub payload: Value,
}

impl Frame {
    /// The `(event name, JSON data)` this frame is on `stream`, if it is on it.
    fn project(&self, stream: &str) -> Option<(&'static str, String)> {
        match (&self.topic, stream) {
            (Topic::Log, "automation-log") => Some(("automation-log", self.payload.to_string())),
            (Topic::Log, "job-log") => {
                let field = |name: &str| self.payload.get(name).cloned().unwrap_or(Value::Null);
                let data = json!({
                    "tenant": field("tenant"),
                    "repository": field("repository"),
                    "issue": field("issue"),
                    "line": field("line"),
                });
                Some(("job-log", data.to_string()))
            }
            (Topic::Calibration, "model-calibration-refreshed") => {
                Some(("model-calibration-refreshed", self.payload.to_string()))
            }
            _ => None,
        }
    }
}

pub struct EventHub {
    next: AtomicU64,
    rings: Mutex<HashMap<String, VecDeque<Frame>>>,
    tx: broadcast::Sender<Frame>,
    ring_capacity: usize,
    heartbeat: Duration,
}

impl EventHub {
    pub fn new(heartbeat_secs: u64) -> Self {
        Self::with_capacity(heartbeat_secs, RING_CAPACITY, LIVE_CAPACITY)
    }

    pub fn with_capacity(heartbeat_secs: u64, ring: usize, live: usize) -> Self {
        let (tx, _) = broadcast::channel(live.max(1));
        EventHub {
            next: AtomicU64::new(1),
            rings: Mutex::new(HashMap::new()),
            tx,
            ring_capacity: ring.max(1),
            heartbeat: Duration::from_secs(heartbeat_secs.max(1)),
        }
    }

    pub fn subscribe(&self) -> broadcast::Receiver<Frame> {
        self.tx.subscribe()
    }

    /// Publish one log line for a tenant. Returns the frame id.
    pub fn publish_log(
        &self,
        tenant: &str,
        repository: &str,
        issue: u64,
        line: &str,
        timestamp: u64,
    ) -> u64 {
        self.publish(
            tenant,
            Topic::Log,
            json!({
                "tenant": tenant,
                "source": "Issue worker",
                "stream": "stdout",
                "line": line,
                "timestamp": timestamp,
                "repository": repository,
                "issue": issue,
            }),
        )
    }

    pub fn publish_calibration(&self, tenant: &str, payload: Value) -> u64 {
        let mut payload = payload;
        if let Value::Object(map) = &mut payload {
            map.insert("tenant".into(), Value::String(tenant.to_string()));
        } else {
            payload = json!({ "tenant": tenant, "result": payload });
        }
        self.publish(tenant, Topic::Calibration, payload)
    }

    fn publish(&self, tenant: &str, topic: Topic, mut payload: Value) -> u64 {
        // Nothing unredacted is ever stored or sent.
        redact_value(&mut payload);
        let id = self.next.fetch_add(1, Ordering::SeqCst);
        let frame = Frame {
            id,
            tenant: tenant.to_string(),
            topic,
            payload,
        };
        if let Ok(mut rings) = self.rings.lock() {
            let ring = rings.entry(tenant.to_string()).or_default();
            ring.push_back(frame.clone());
            while ring.len() > self.ring_capacity {
                ring.pop_front();
            }
        }
        let _ = self.tx.send(frame);
        id
    }

    /// Frames after `after` for the given tenants, oldest first.
    pub fn replay(&self, tenants: &HashSet<String>, after: u64) -> Vec<Frame> {
        let Ok(rings) = self.rings.lock() else {
            return Vec::new();
        };
        let mut frames: Vec<Frame> = tenants
            .iter()
            .filter_map(|tenant| rings.get(tenant))
            .flat_map(|ring| ring.iter().filter(|frame| frame.id > after).cloned())
            .collect();
        frames.sort_by_key(|frame| frame.id);
        frames
    }

    /// The oldest id still replayable for a tenant, `None` when it has no frames.
    fn oldest(&self, tenants: &HashSet<String>) -> Option<u64> {
        let rings = self.rings.lock().ok()?;
        tenants
            .iter()
            .filter_map(|tenant| rings.get(tenant)?.front().map(|frame| frame.id))
            .min()
    }

    /// The last `limit` log lines of one tenant in the desktop's log file
    /// format: `[<unix seconds>] [<source>/<stream>] <line>`.
    pub fn recent_logs(&self, tenant: &str, limit: usize) -> Vec<String> {
        let Ok(rings) = self.rings.lock() else {
            return Vec::new();
        };
        let Some(ring) = rings.get(tenant) else {
            return Vec::new();
        };
        let mut lines: Vec<String> = ring
            .iter()
            .rev()
            .filter(|frame| matches!(frame.topic, Topic::Log))
            .take(limit)
            .map(|frame| {
                let text = |name: &str| {
                    frame
                        .payload
                        .get(name)
                        .and_then(Value::as_str)
                        .unwrap_or("")
                        .to_string()
                };
                let timestamp = frame
                    .payload
                    .get("timestamp")
                    .and_then(Value::as_u64)
                    .unwrap_or(0);
                format!(
                    "[{timestamp}] [{}/{}] {}",
                    text("source"),
                    text("stream"),
                    text("line")
                )
            })
            .collect();
        lines.reverse();
        lines
    }

    pub fn heartbeat(&self) -> Duration {
        self.heartbeat
    }
}

fn last_event_id(headers: &HeaderMap) -> u64 {
    headers
        .get("last-event-id")
        .and_then(|value| value.to_str().ok())
        .and_then(|value| value.trim().parse().ok())
        .unwrap_or(0)
}

async fn allowed_tenants(state: &AppState, authed: &Authed) -> Result<HashSet<String>, ApiError> {
    Ok(state
        .store
        .tenants_for_user(&authed.user.id)
        .await?
        .into_iter()
        .map(|(tenant, _)| tenant.id.to_string())
        .collect())
}

fn sse_event(id: u64, name: &str, data: &str) -> Event {
    Event::default().id(id.to_string()).event(name).data(data)
}

/// Serve `stream` (one of [`STREAMS`]) to the signed-in user: every tenant they
/// belong to, resumed after `Last-Event-ID`, with a heartbeat.
pub async fn serve(
    state: AppState,
    authed: Authed,
    headers: HeaderMap,
    stream: &'static str,
) -> Result<impl IntoResponse, ApiError> {
    let mut allowed = allowed_tenants(&state, &authed).await?;
    let hub = state.events.clone();
    let after = last_event_id(&headers);
    // Subscribe before reading the ring so nothing falls in the gap; the
    // `sent` watermark drops what both paths deliver.
    let mut live = hub.subscribe();
    let (tx, rx) = mpsc::channel::<Result<Event, Infallible>>(CONNECTION_QUEUE);
    let session_hash = authed.session.token_hash.clone();
    let user_id = authed.user.id.clone();
    let heartbeat = hub.heartbeat();

    tokio::spawn(async move {
        let mut sent = after;
        if tx
            .send(Ok(Event::default().comment("ready")))
            .await
            .is_err()
        {
            return;
        }
        // Resuming past what the ring still holds loses lines: say so.
        if after > 0 {
            if let Some(oldest) = hub.oldest(&allowed) {
                if oldest > after + 1 {
                    let data = json!({ "reason": "history_truncated", "after": after }).to_string();
                    let _ = tx
                        .send(Ok(Event::default().event("resync").data(data)))
                        .await;
                }
            }
        }
        for frame in hub.replay(&allowed, sent) {
            sent = sent.max(frame.id);
            if let Some((name, data)) = frame.project(stream) {
                if tx.send(Ok(sse_event(frame.id, name, &data))).await.is_err() {
                    return;
                }
            }
        }
        let mut recheck = tokio::time::interval(RECHECK_EVERY);
        recheck.tick().await;
        loop {
            tokio::select! {
                _ = tx.closed() => return,
                _ = recheck.tick() => {
                    // A logout, an expired session or a lost membership ends the stream.
                    let session = state.store.session(&session_hash).await;
                    match session {
                        Ok(Some(session)) if session.expires_at > state.clock.now_secs() => {}
                        _ => return,
                    }
                    if let Ok(tenants) = state.store.tenants_for_user(&user_id).await {
                        allowed = tenants.into_iter().map(|(t, _)| t.id.to_string()).collect();
                    }
                }
                received = live.recv() => match received {
                    Ok(frame) => {
                        if frame.id <= sent || !allowed.contains(&frame.tenant) {
                            continue;
                        }
                        sent = frame.id;
                        if let Some((name, data)) = frame.project(stream) {
                            if tx.send(Ok(sse_event(frame.id, name, &data))).await.is_err() {
                                return;
                            }
                        }
                    }
                    Err(broadcast::error::RecvError::Lagged(missed)) => {
                        // The client was too slow for the live channel. Tell it,
                        // then catch up from the ring (bounded) rather than
                        // buffering without limit.
                        let data = json!({ "reason": "lagged", "missed": missed }).to_string();
                        if tx.send(Ok(Event::default().event("resync").data(data))).await.is_err() {
                            return;
                        }
                        for frame in hub.replay(&allowed, sent) {
                            sent = sent.max(frame.id);
                            if let Some((name, data)) = frame.project(stream) {
                                if tx.send(Ok(sse_event(frame.id, name, &data))).await.is_err() {
                                    return;
                                }
                            }
                        }
                    }
                    Err(broadcast::error::RecvError::Closed) => return,
                },
            }
        }
    });

    Ok(Sse::new(ReceiverStream::new(rx))
        .keep_alive(KeepAlive::new().interval(heartbeat).text("heartbeat")))
}

/// Scrub a line the same way frames are scrubbed (for callers that format
/// their own text, such as the status endpoint's `detail`).
pub fn clean(text: &str) -> String {
    redact_text(text)
}
