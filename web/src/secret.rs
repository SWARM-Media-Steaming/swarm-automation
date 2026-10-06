//! A string that must never be printed, serialized or logged.

use serde::{Deserialize, Deserializer};
use zeroize::Zeroizing;

/// Holds a credential. `Debug` and `Display` print a placeholder, there is no
/// `Serialize`, and the memory is zeroed on drop. The only way to read it is
/// the explicit [`Secret::expose`], which makes every use greppable.
#[derive(Clone)]
pub struct Secret(Zeroizing<String>);

impl Secret {
    pub fn new(value: impl Into<String>) -> Self {
        Secret(Zeroizing::new(value.into()))
    }

    pub fn expose(&self) -> &str {
        &self.0
    }

    pub fn is_empty(&self) -> bool {
        self.0.is_empty()
    }
}

/// Deserializing is allowed (request bodies carry keys in); serializing is not.
impl<'de> Deserialize<'de> for Secret {
    fn deserialize<D: Deserializer<'de>>(deserializer: D) -> Result<Self, D::Error> {
        String::deserialize(deserializer).map(Secret::new)
    }
}

impl std::fmt::Debug for Secret {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str("Secret(<redacted>)")
    }
}

impl std::fmt::Display for Secret {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str("<redacted>")
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn formatting_never_reveals_the_value() {
        let secret = Secret::new("sk-ant-CANARY-0123456789abcdef");
        assert_eq!(format!("{secret:?}"), "Secret(<redacted>)");
        assert_eq!(format!("{secret}"), "<redacted>");
        assert_eq!(secret.expose(), "sk-ant-CANARY-0123456789abcdef");
    }
}
