//! Envelope encryption for tenant provider keys.
//!
//! Every secret gets its own random 256-bit data key (DEK). The secret is
//! sealed with AES-256-GCM under the DEK, and the DEK is wrapped by a
//! [`KeyWrapper`]: a local master key in development, KMS on AWS (a KMS
//! `KeyWrapper` is the one piece that needs the AWS SDK and lands with the
//! deployment work; the trait is the seam). Only the wrapped DEK and the
//! ciphertext are stored.
//!
//! Both layers authenticate the same associated data (`tenant`, `provider`),
//! so a sealed key copied to another tenant's row, or to another provider's,
//! fails to open instead of decrypting.

use std::fmt;

use aes_gcm::aead::rand_core::{OsRng, RngCore};
use aes_gcm::aead::{Aead, KeyInit, Payload};
use aes_gcm::{Aes256Gcm, Nonce};
use async_trait::async_trait;
use sha2::{Digest, Sha256};
use zeroize::Zeroizing;

const NONCE_LEN: usize = 12;
const KEY_LEN: usize = 32;
const FORMAT_VERSION: u8 = 1;

#[derive(Debug)]
pub struct CryptoError(pub &'static str);

impl fmt::Display for CryptoError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(self.0)
    }
}

impl std::error::Error for CryptoError {}

/// Wraps and unwraps data keys. Implementations must authenticate `aad`.
#[async_trait]
pub trait KeyWrapper: Send + Sync {
    /// Identifies the wrapping key, stored with each secret so a rotation can
    /// tell which master key sealed it.
    fn key_id(&self) -> String;
    async fn wrap(&self, data_key: &[u8], aad: &[u8]) -> Result<Vec<u8>, CryptoError>;
    async fn unwrap(&self, wrapped: &[u8], aad: &[u8]) -> Result<Zeroizing<Vec<u8>>, CryptoError>;
}

/// The development key wrapper: AES-256-GCM under a locally configured key.
pub struct LocalKeyWrapper {
    key: Zeroizing<[u8; KEY_LEN]>,
    id: String,
}

impl LocalKeyWrapper {
    pub fn new(key: [u8; KEY_LEN]) -> Self {
        let digest = Sha256::digest(key);
        LocalKeyWrapper {
            key: Zeroizing::new(key),
            id: format!("local:{}", hex::encode(&digest[..4])),
        }
    }

    /// Parse a base64 (standard or URL-safe) 32-byte key.
    pub fn from_base64(value: &str) -> Result<Self, CryptoError> {
        use base64::Engine;
        let trimmed = value.trim();
        let bytes = base64::engine::general_purpose::STANDARD
            .decode(trimmed)
            .or_else(|_| base64::engine::general_purpose::URL_SAFE_NO_PAD.decode(trimmed))
            .map_err(|_| CryptoError("the local key is not valid base64"))?;
        let key: [u8; KEY_LEN] = bytes
            .try_into()
            .map_err(|_| CryptoError("the local key must be exactly 32 bytes"))?;
        Ok(Self::new(key))
    }
}

pub fn random_bytes<const N: usize>() -> [u8; N] {
    let mut out = [0u8; N];
    OsRng.fill_bytes(&mut out);
    out
}

/// `2 * bytes` lowercase hex characters from the OS random source.
pub fn random_hex(bytes: usize) -> String {
    let mut buffer = vec![0u8; bytes];
    OsRng.fill_bytes(&mut buffer);
    hex::encode(buffer)
}

/// 256 bits of OS randomness as URL-safe base64: session ids, CSRF tokens,
/// OAuth state.
pub fn random_token() -> String {
    use base64::Engine;
    base64::engine::general_purpose::URL_SAFE_NO_PAD.encode(random_bytes::<32>())
}

pub fn sha256_hex(data: &[u8]) -> String {
    hex::encode(Sha256::digest(data))
}

fn encrypt(key: &[u8], aad: &[u8], plaintext: &[u8]) -> Result<Vec<u8>, CryptoError> {
    let cipher = Aes256Gcm::new_from_slice(key).map_err(|_| CryptoError("invalid key length"))?;
    let nonce_bytes = random_bytes::<NONCE_LEN>();
    let sealed = cipher
        .encrypt(
            Nonce::from_slice(&nonce_bytes),
            Payload {
                msg: plaintext,
                aad,
            },
        )
        .map_err(|_| CryptoError("encryption failed"))?;
    let mut out = nonce_bytes.to_vec();
    out.extend_from_slice(&sealed);
    Ok(out)
}

fn decrypt(key: &[u8], aad: &[u8], sealed: &[u8]) -> Result<Zeroizing<Vec<u8>>, CryptoError> {
    if sealed.len() < NONCE_LEN + 16 {
        return Err(CryptoError("sealed data is truncated"));
    }
    let cipher = Aes256Gcm::new_from_slice(key).map_err(|_| CryptoError("invalid key length"))?;
    let (nonce, body) = sealed.split_at(NONCE_LEN);
    cipher
        .decrypt(Nonce::from_slice(nonce), Payload { msg: body, aad })
        .map(Zeroizing::new)
        .map_err(|_| CryptoError("the secret could not be authenticated"))
}

#[async_trait]
impl KeyWrapper for LocalKeyWrapper {
    fn key_id(&self) -> String {
        self.id.clone()
    }

    async fn wrap(&self, data_key: &[u8], aad: &[u8]) -> Result<Vec<u8>, CryptoError> {
        encrypt(&self.key[..], aad, data_key)
    }

    async fn unwrap(&self, wrapped: &[u8], aad: &[u8]) -> Result<Zeroizing<Vec<u8>>, CryptoError> {
        decrypt(&self.key[..], aad, wrapped)
    }
}

/// What the store keeps for one secret. Contains no plaintext.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct SealedSecret {
    pub version: u8,
    pub wrapper_key_id: String,
    pub wrapped_data_key: Vec<u8>,
    pub ciphertext: Vec<u8>,
}

/// The authenticated context a secret is bound to.
pub fn secret_aad(tenant: &str, provider: &str) -> Vec<u8> {
    format!("swarm-web/v{FORMAT_VERSION}|tenant={tenant}|provider={provider}").into_bytes()
}

pub async fn seal(
    wrapper: &dyn KeyWrapper,
    plaintext: &[u8],
    aad: &[u8],
) -> Result<SealedSecret, CryptoError> {
    let data_key = Zeroizing::new(random_bytes::<KEY_LEN>());
    let ciphertext = encrypt(&data_key[..], aad, plaintext)?;
    let wrapped_data_key = wrapper.wrap(&data_key[..], aad).await?;
    Ok(SealedSecret {
        version: FORMAT_VERSION,
        wrapper_key_id: wrapper.key_id(),
        wrapped_data_key,
        ciphertext,
    })
}

pub async fn open(
    wrapper: &dyn KeyWrapper,
    sealed: &SealedSecret,
    aad: &[u8],
) -> Result<Zeroizing<Vec<u8>>, CryptoError> {
    if sealed.version != FORMAT_VERSION {
        return Err(CryptoError("unsupported sealed-secret version"));
    }
    let data_key = wrapper.unwrap(&sealed.wrapped_data_key, aad).await?;
    decrypt(&data_key[..], aad, &sealed.ciphertext)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn wrapper() -> LocalKeyWrapper {
        LocalKeyWrapper::new([7u8; 32])
    }

    #[tokio::test]
    async fn seals_and_opens_and_stores_no_plaintext() {
        let w = wrapper();
        let aad = secret_aad("t1", "claude");
        let sealed = seal(&w, b"sk-ant-CANARY-plaintext", &aad).await.unwrap();
        assert!(!String::from_utf8_lossy(&sealed.ciphertext).contains("CANARY"));
        let opened = open(&w, &sealed, &aad).await.unwrap();
        assert_eq!(&opened[..], b"sk-ant-CANARY-plaintext");
    }

    #[tokio::test]
    async fn each_secret_gets_its_own_data_key_and_nonce() {
        let w = wrapper();
        let aad = secret_aad("t1", "claude");
        let a = seal(&w, b"same", &aad).await.unwrap();
        let b = seal(&w, b"same", &aad).await.unwrap();
        assert_ne!(a.wrapped_data_key, b.wrapped_data_key);
        assert_ne!(a.ciphertext, b.ciphertext);
    }

    #[tokio::test]
    async fn a_sealed_secret_is_bound_to_its_tenant_and_provider() {
        let w = wrapper();
        let sealed = seal(&w, b"secret", &secret_aad("t1", "claude"))
            .await
            .unwrap();
        assert!(open(&w, &sealed, &secret_aad("t2", "claude"))
            .await
            .is_err());
        assert!(open(&w, &sealed, &secret_aad("t1", "codex")).await.is_err());
    }

    #[tokio::test]
    async fn tampering_and_the_wrong_master_key_are_rejected() {
        let w = wrapper();
        let aad = secret_aad("t1", "grok");
        let mut sealed = seal(&w, b"secret", &aad).await.unwrap();
        let other = LocalKeyWrapper::new([9u8; 32]);
        assert!(open(&other, &sealed, &aad).await.is_err());
        let last = sealed.ciphertext.len() - 1;
        sealed.ciphertext[last] ^= 1;
        assert!(open(&w, &sealed, &aad).await.is_err());
        sealed.ciphertext.truncate(4);
        assert!(open(&w, &sealed, &aad).await.is_err());
    }

    #[test]
    fn the_local_key_must_be_32_bytes_of_base64() {
        use base64::Engine;
        let good = base64::engine::general_purpose::STANDARD.encode([1u8; 32]);
        assert!(LocalKeyWrapper::from_base64(&good).is_ok());
        assert!(LocalKeyWrapper::from_base64("not base64!!").is_err());
        let short = base64::engine::general_purpose::STANDARD.encode([1u8; 16]);
        assert!(LocalKeyWrapper::from_base64(&short).is_err());
    }
}
