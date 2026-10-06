//! Structured (JSON) logging with redaction applied to every line.

use tracing_subscriber::fmt::MakeWriter;
use tracing_subscriber::EnvFilter;

use crate::redact::RedactingMakeWriter;

/// The subscriber the service runs with: JSON lines through the redacting
/// writer. Tests build the same thing around a capture buffer.
pub fn subscriber<W>(writer: W) -> impl tracing::Subscriber + Send + Sync
where
    W: for<'a> MakeWriter<'a> + Send + Sync + 'static,
{
    tracing_subscriber::fmt()
        .json()
        .with_env_filter(
            EnvFilter::try_from_default_env().unwrap_or_else(|_| EnvFilter::new("info")),
        )
        .with_writer(RedactingMakeWriter(writer))
        .with_current_span(false)
        .finish()
}

pub fn init() {
    let _ = tracing::subscriber::set_global_default(subscriber(std::io::stdout));
}
