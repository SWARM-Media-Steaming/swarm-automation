//! API errors: a stable JSON body, and internal detail only ever in the
//! (redacted) log, never in the response.

use axum::http::StatusCode;
use axum::response::{IntoResponse, Response};
use axum::Json;
use serde_json::json;

use crate::crypto::CryptoError;
use crate::redact::redact_text;
use crate::store::StoreError;

#[derive(Debug)]
pub enum ApiError {
    BadRequest(String),
    /// Not signed in, or the session is gone or expired.
    Unauthorized,
    /// A webhook whose signature did not verify.
    InvalidSignature,
    /// Signed in but not allowed. The code distinguishes CSRF, role and state.
    Forbidden {
        code: &'static str,
        message: String,
    },
    /// Also what a tenant the caller does not belong to looks like, so ids
    /// cannot be probed.
    NotFound,
    Conflict(String),
    TooManyRequests {
        code: &'static str,
        message: String,
    },
    BadGateway(String),
    Internal(String),
    /// The endpoint exists but this deployment cannot serve it yet (501).
    NotAvailable {
        code: &'static str,
        message: String,
    },
    /// A dependency this endpoint needs is not configured (503).
    Unconfigured {
        code: &'static str,
        message: String,
    },
}

impl ApiError {
    pub fn forbidden(code: &'static str, message: impl Into<String>) -> Self {
        ApiError::Forbidden {
            code,
            message: message.into(),
        }
    }

    fn parts(&self) -> (StatusCode, &'static str, String) {
        match self {
            ApiError::BadRequest(message) => {
                (StatusCode::BAD_REQUEST, "bad_request", message.clone())
            }
            ApiError::Unauthorized => (
                StatusCode::UNAUTHORIZED,
                "unauthorized",
                "Sign in with GitHub to continue.".into(),
            ),
            ApiError::InvalidSignature => (
                StatusCode::UNAUTHORIZED,
                "invalid_signature",
                "The webhook signature is not valid.".into(),
            ),
            ApiError::Forbidden { code, message } => (StatusCode::FORBIDDEN, code, message.clone()),
            ApiError::NotFound => (StatusCode::NOT_FOUND, "not_found", "Not found.".into()),
            ApiError::Conflict(message) => (StatusCode::CONFLICT, "conflict", message.clone()),
            ApiError::TooManyRequests { code, message } => {
                (StatusCode::TOO_MANY_REQUESTS, code, message.clone())
            }
            ApiError::BadGateway(_) => (
                StatusCode::BAD_GATEWAY,
                "upstream_error",
                "GitHub could not be reached.".into(),
            ),
            ApiError::NotAvailable { code, message } => {
                (StatusCode::NOT_IMPLEMENTED, code, message.clone())
            }
            ApiError::Unconfigured { code, message } => {
                (StatusCode::SERVICE_UNAVAILABLE, code, message.clone())
            }
            ApiError::Internal(_) => (
                StatusCode::INTERNAL_SERVER_ERROR,
                "internal_error",
                "Something went wrong.".into(),
            ),
        }
    }
}

impl IntoResponse for ApiError {
    fn into_response(self) -> Response {
        let (status, code, message) = self.parts();
        match &self {
            ApiError::BadGateway(detail) | ApiError::Internal(detail) => {
                tracing::error!(status = status.as_u16(), detail = %redact_text(detail), "request failed");
            }
            _ => {}
        }
        // `error` is the field `ui/api.js` shows to the user.
        (status, Json(json!({ "error": message, "code": code }))).into_response()
    }
}

impl From<StoreError> for ApiError {
    fn from(error: StoreError) -> Self {
        ApiError::Internal(error.to_string())
    }
}

impl From<CryptoError> for ApiError {
    fn from(error: CryptoError) -> Self {
        ApiError::Internal(error.to_string())
    }
}
