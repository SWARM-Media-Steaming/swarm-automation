//! Whether a GitHub event should wake a worker container.
//!
//! The worker still posts and honors the HTML lifecycle markers
//! (`.claude/rules/issue-lifecycle-comments.md`). This function only stops the
//! orchestrator from starting a container for the worker's own comments, and
//! from treating an unhandled event as work. `force` (Run now) skips those
//! checks; the worker's own marker still keeps a second start idempotent.

use std::collections::BTreeSet;

use serde_json::Value;

const MARKER: &str = "<!-- swarm-issue-worker:";
const NEEDS_INPUT: &str = "swarm-issue-worker:needs-input:";
const COMMIT_MARKER: &str = "swarm-issue-worker:commit:";

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Decision {
    Start,
    Skip(&'static str),
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ParsedEvent {
    pub owner: String,
    pub repo: String,
    pub issue: u64,
    pub sender: String,
    pub trusted: bool,
    pub decision: Decision,
}

pub fn parse(
    event: &str,
    payload: &Value,
    trusted: &BTreeSet<String>,
    force: bool,
) -> Option<ParsedEvent> {
    let (owner, repo) = repository(payload)?;
    let sender = payload["sender"]["login"]
        .as_str()
        .unwrap_or("")
        .to_string();
    let is_trusted = trusted
        .iter()
        .any(|login| login.eq_ignore_ascii_case(&sender));
    let action = payload["action"].as_str().unwrap_or("");
    let decision = if force {
        Decision::Start
    } else {
        decide(event, action, payload, &sender, trusted)
    };
    let issue = issue_number(event, payload)?;
    Some(ParsedEvent {
        owner,
        repo,
        issue,
        sender,
        trusted: is_trusted,
        decision,
    })
}

fn decide(
    event: &str,
    action: &str,
    payload: &Value,
    sender: &str,
    trusted: &BTreeSet<String>,
) -> Decision {
    match event {
        "issues" if matches!(action, "opened" | "reopened" | "assigned" | "labeled") => {
            if payload["issue"].get("pull_request").is_some() {
                Decision::Skip("skipped_pull_request")
            } else {
                Decision::Start
            }
        }
        "issue_comment" if action == "created" => comment_decision(payload, sender, trusted),
        "pull_request" if matches!(action, "opened" | "synchronize" | "closed" | "labeled") => {
            Decision::Start
        }
        "poll" => poll_decision(payload, trusted),
        _ => Decision::Skip("skipped_unhandled"),
    }
}

fn author_trusted(login: &str, trusted: &BTreeSet<String>) -> bool {
    trusted.iter().any(|item| item.eq_ignore_ascii_case(login))
}

fn comment_decision(payload: &Value, sender: &str, trusted: &BTreeSet<String>) -> Decision {
    let body = payload["comment"]["body"].as_str().unwrap_or("");
    if body.contains(NEEDS_INPUT) {
        return if author_trusted(sender, trusted) {
            Decision::Start
        } else {
            Decision::Skip("skipped_needs_input")
        };
    }
    if body.contains(MARKER) || body.contains(COMMIT_MARKER) {
        return Decision::Skip("skipped_own_comment");
    }
    if sender.ends_with("[bot]") {
        return Decision::Skip("skipped_bot");
    }
    Decision::Start
}

/// A poll snapshot: the newest human comment wins. A commit marker with no
/// newer human comment does not start another container.
fn poll_decision(payload: &Value, trusted: &BTreeSet<String>) -> Decision {
    let Some(comments) = payload["comments"].as_array() else {
        return Decision::Skip("skipped_unhandled");
    };
    for comment in comments.iter().rev() {
        let body = comment["body"].as_str().unwrap_or("");
        let login = comment["user"]["login"].as_str().unwrap_or("");
        if body.contains(NEEDS_INPUT) {
            return if author_trusted(login, trusted) {
                Decision::Start
            } else {
                Decision::Skip("skipped_needs_input")
            };
        }
        // Newest first: an older human comment does not outrank a later marker.
        if body.contains(COMMIT_MARKER) {
            return Decision::Skip("skipped_commit");
        }
        if body.contains(MARKER) {
            return Decision::Skip("skipped_own_comment");
        }
        if login.ends_with("[bot]") {
            continue;
        }
        if !login.is_empty() {
            return Decision::Start;
        }
    }
    Decision::Skip("skipped_unhandled")
}

fn repository(payload: &Value) -> Option<(String, String)> {
    let full = payload["repository"]["full_name"].as_str()?;
    let (owner, name) = full.split_once('/')?;
    if !github_name(owner) || !github_name(name) {
        return None;
    }
    Some((owner.to_string(), name.to_string()))
}

pub fn github_name(value: &str) -> bool {
    !value.is_empty()
        && value.len() <= 100
        && !value.starts_with('.')
        && !value.contains("..")
        && value
            .chars()
            .all(|c| c.is_ascii_alphanumeric() || c == '-' || c == '_' || c == '.')
}

fn issue_number(event: &str, payload: &Value) -> Option<u64> {
    if event == "pull_request" {
        let reference = payload["pull_request"]["head"]["ref"]
            .as_str()
            .unwrap_or("");
        return issue_from_ref(reference);
    }
    payload["issue"]["number"].as_u64().filter(|n| *n > 0)
}

/// `ai/<provider>/issue-N`, the branch shape from `issue-branch-delivery.md`.
pub fn issue_from_ref(name: &str) -> Option<u64> {
    let mut parts = name.split('/');
    if parts.next() != Some("ai") {
        return None;
    }
    parts.next()?;
    let issue = parts.next()?;
    if parts.next().is_some() {
        return None;
    }
    issue.strip_prefix("issue-")?.parse().ok()
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn trusted() -> BTreeSet<String> {
        BTreeSet::from(["octocat".into()])
    }

    fn issue(action: &str) -> Value {
        json!({
            "action": action,
            "issue": {"number": 418},
            "repository": {"full_name": "acme/demo"},
            "sender": {"login": "octocat"}
        })
    }

    #[test]
    fn issue_events_start_and_our_comments_do_not() {
        let parsed = parse("issues", &issue("opened"), &trusted(), false).unwrap();
        assert_eq!(parsed.decision, Decision::Start);
        assert_eq!(
            (parsed.owner.as_str(), parsed.repo.as_str(), parsed.issue),
            ("acme", "demo", 418)
        );

        let mut comment = issue("created");
        comment["comment"] = json!({"body": "please look again"});
        assert_eq!(
            parse("issue_comment", &comment, &trusted(), false)
                .unwrap()
                .decision,
            Decision::Start
        );

        comment["comment"] = json!({"body": "<!-- swarm-issue-worker:started:issue:418 -->"});
        assert_eq!(
            parse("issue_comment", &comment, &trusted(), false)
                .unwrap()
                .decision,
            Decision::Skip("skipped_own_comment")
        );

        comment["comment"] = json!({"body": "<!-- swarm-issue-worker:commit:abc -->"});
        assert_eq!(
            parse("issue_comment", &comment, &trusted(), false)
                .unwrap()
                .decision,
            Decision::Skip("skipped_own_comment")
        );

        comment["comment"] =
            json!({"body": "<!-- swarm-issue-worker:needs-input:issue:418;provider:claude -->"});
        comment["sender"] = json!({"login": "stranger"});
        assert_eq!(
            parse("issue_comment", &comment, &trusted(), false)
                .unwrap()
                .decision,
            Decision::Skip("skipped_needs_input")
        );
        comment["sender"] = json!({"login": "octocat"});
        assert_eq!(
            parse("issue_comment", &comment, &trusted(), false)
                .unwrap()
                .decision,
            Decision::Start
        );
    }

    #[test]
    fn a_commit_marker_without_a_newer_human_comment_does_not_start() {
        let payload = json!({
            "action": "poll",
            "repository": {"full_name": "acme/demo"},
            "issue": {"number": 418},
            "comments": [
                {"user": {"login": "dev"}, "body": "ship it"},
                {"user": {"login": "swarm[bot]"}, "body": "<!-- swarm-issue-worker:commit:abc -->"}
            ]
        });
        assert_eq!(
            parse("poll", &payload, &trusted(), false).unwrap().decision,
            Decision::Skip("skipped_commit")
        );
        let followed = json!({
            "action": "poll",
            "repository": {"full_name": "acme/demo"},
            "issue": {"number": 418},
            "comments": [
                {"user": {"login": "swarm[bot]"}, "body": "<!-- swarm-issue-worker:commit:abc -->"},
                {"user": {"login": "dev"}, "body": "one more thing"}
            ]
        });
        assert_eq!(
            parse("poll", &followed, &trusted(), false)
                .unwrap()
                .decision,
            Decision::Start
        );
    }

    #[test]
    fn pull_requests_use_the_issue_branch_and_unhandled_events_wait() {
        let payload = json!({
            "action": "synchronize",
            "repository": {"full_name": "acme/demo"},
            "pull_request": {"number": 9, "head": {"ref": "ai/claude/issue-418"}},
            "sender": {"login": "octocat"}
        });
        let parsed = parse("pull_request", &payload, &trusted(), false).unwrap();
        assert_eq!(parsed.issue, 418);
        assert_eq!(parsed.decision, Decision::Start);

        let edited = issue("edited");
        assert_eq!(
            parse("issues", &edited, &trusted(), false)
                .unwrap()
                .decision,
            Decision::Skip("skipped_unhandled")
        );
        assert!(
            parse("issues", &issue("opened"), &trusted(), false)
                .unwrap()
                .decision
                == Decision::Start
        );
        let forced = issue("edited");
        assert_eq!(
            parse("issues", &forced, &trusted(), true).unwrap().decision,
            Decision::Start
        );
    }

    #[test]
    fn branch_names_only_match_the_delivery_shape() {
        assert_eq!(issue_from_ref("ai/claude/issue-418"), Some(418));
        assert_eq!(issue_from_ref("ai/xai/issue-7"), Some(7));
        assert_eq!(issue_from_ref("feature/issue-7"), None);
        assert_eq!(issue_from_ref("ai/claude/issue-7/extra"), None);
        assert!(!github_name(".."));
        assert!(!github_name(""));
        assert!(github_name("demo"));
    }
}
