//! Redaction for everything that reaches a log or an error message.
//!
//! Mirrors the intent of `issue_worker/architecture_docs.redact` (secrets, URLs
//! with credentials, key blocks): a leak must be impossible by construction in
//! the common case and scrubbed by pattern in the rest. Provider keys are never
//! passed to a logger in the first place (they live in [`crate::secret::Secret`]);
//! this is the safety net.

use std::io::Write;
use std::sync::{OnceLock, RwLock};

use regex::Regex;
use tracing_subscriber::fmt::MakeWriter;

const PLACEHOLDER: &str = "[REDACTED]";

struct Patterns {
    whole: Vec<Regex>,
    keyed: Regex,
}

fn patterns() -> &'static Patterns {
    static PATTERNS: OnceLock<Patterns> = OnceLock::new();
    PATTERNS.get_or_init(|| Patterns {
        whole: [
            r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?(?:-----END [A-Z ]*PRIVATE KEY-----|$)",
            r"\bgh[pousr]_[A-Za-z0-9]{16,}",
            r"\bgithub_pat_[A-Za-z0-9_]{16,}",
            r"\bsk-[A-Za-z0-9_\-]{12,}",
            r"\bxai-[A-Za-z0-9_\-]{12,}",
            r"\bAKIA[0-9A-Z]{16}\b",
            r"(?i)\b(?:bearer|basic)\s+[A-Za-z0-9._~+/=\-]{8,}",
            r"sha256=[0-9a-fA-F]{32,}",
            r"://[^/\s:@]+:[^/\s@]+@",
        ]
        .iter()
        .map(|p| Regex::new(p).expect("redaction pattern compiles"))
        .collect(),
        keyed: Regex::new(
            r#"(?i)\b(client_secret|access_token|refresh_token|id_token|code_verifier|code|state|api[_-]?key|secret|token|password|passwd|authorization|x-csrf-token|cookie|set-cookie)(["']?\s*[:=]\s*["']?)[^\s"'&,;}]+"#,
        )
        .expect("redaction pattern compiles"),
    })
}

fn known() -> &'static RwLock<Vec<String>> {
    static KNOWN: OnceLock<RwLock<Vec<String>>> = OnceLock::new();
    KNOWN.get_or_init(|| RwLock::new(Vec::new()))
}

/// Register a configured secret (client secret, webhook secret, master key) so
/// it is scrubbed by exact value even when it matches no pattern.
pub fn register_secret(value: &str) {
    if value.len() < 8 {
        return;
    }
    if let Ok(mut list) = known().write() {
        if !list.iter().any(|existing| existing == value) {
            list.push(value.to_string());
        }
    }
}

pub fn redact_text(input: &str) -> String {
    let mut text = input.to_string();
    if let Ok(list) = known().read() {
        for secret in list.iter() {
            text = text.replace(secret.as_str(), PLACEHOLDER);
        }
    }
    let p = patterns();
    for pattern in &p.whole {
        text = pattern.replace_all(&text, PLACEHOLDER).into_owned();
    }
    p.keyed
        .replace_all(&text, format!("$1$2{PLACEHOLDER}"))
        .into_owned()
}

/// Like [`redact_text`] without the `name=value` rule: only values that look
/// like a credential (provider and GitHub tokens, key blocks, bearer headers,
/// credentialed URLs, registered secrets) are replaced. For stored settings,
/// where a prompt may legitimately say "state: draft".
pub fn scrub_tokens(input: &str) -> String {
    let mut text = input.to_string();
    if let Ok(list) = known().read() {
        for secret in list.iter() {
            text = text.replace(secret.as_str(), PLACEHOLDER);
        }
    }
    for pattern in &patterns().whole {
        text = pattern.replace_all(&text, PLACEHOLDER).into_owned();
    }
    text
}

/// [`redact_text`] over every string in a JSON value (keys are left alone).
pub fn redact_value(value: &mut serde_json::Value) {
    match value {
        serde_json::Value::String(text) => *text = redact_text(text),
        serde_json::Value::Array(items) => items.iter_mut().for_each(redact_value),
        serde_json::Value::Object(map) => map.values_mut().for_each(redact_value),
        _ => {}
    }
}

/// A `tracing` writer that scrubs each formatted event before it is written.
#[derive(Clone)]
pub struct RedactingMakeWriter<M>(pub M);

pub struct RedactingWriter<W: Write>(W);

impl<'a, M> MakeWriter<'a> for RedactingMakeWriter<M>
where
    M: MakeWriter<'a>,
{
    type Writer = RedactingWriter<M::Writer>;

    fn make_writer(&'a self) -> Self::Writer {
        RedactingWriter(self.0.make_writer())
    }
}

impl<W: Write> Write for RedactingWriter<W> {
    fn write(&mut self, buf: &[u8]) -> std::io::Result<usize> {
        let clean = redact_text(&String::from_utf8_lossy(buf));
        self.0.write_all(clean.as_bytes())?;
        Ok(buf.len())
    }

    fn flush(&mut self) -> std::io::Result<()> {
        self.0.flush()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn scrubs_provider_and_github_credentials() {
        for secret in [
            "sk-ant-api03-CANARY0123456789abcdef",
            "xai-CANARY0123456789abcdef",
            "ghs_CANARY0123456789abcdefghij",
            "github_pat_CANARY0123456789abcdef",
            "AKIAABCDEFGHIJKLMNOP",
        ] {
            let line = format!("calling upstream with {secret} now");
            let clean = redact_text(&line);
            assert!(!clean.contains(secret), "{secret} survived: {clean}");
            assert!(clean.contains(PLACEHOLDER));
        }
    }

    #[test]
    fn scrubs_key_blocks_headers_urls_and_keyed_values() {
        let block =
            "-----BEGIN RSA PRIVATE KEY-----\nMIIBOgIBAAJBAKj\n-----END RSA PRIVATE KEY-----";
        assert!(!redact_text(block).contains("MIIB"));
        assert!(!redact_text("Authorization: Bearer abcdef0123456789").contains("abcdef0123456789"));
        assert!(!redact_text("https://user:hunter2hunter2@example.com/x").contains("hunter2"));
        let query = redact_text("GET /cb?code=abc123&state=zzz987 client_secret=topsecretvalue");
        assert!(
            !query.contains("abc123")
                && !query.contains("zzz987")
                && !query.contains("topsecretvalue")
        );
        let sig = "sha256=".to_string() + &"a".repeat(64);
        assert!(!redact_text(&sig).contains(&"a".repeat(64)));
    }

    #[test]
    fn scrubs_registered_secrets_by_exact_value() {
        register_secret("plain-value-with-no-pattern-1");
        assert!(!redact_text("oops plain-value-with-no-pattern-1 leaked").contains("plain-value"));
    }

    #[test]
    fn leaves_ordinary_text_alone() {
        assert_eq!(
            redact_text("tenant t1 listed 3 keys"),
            "tenant t1 listed 3 keys"
        );
    }
}
