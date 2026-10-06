//! SWARM Automation web backend (issue #416, part of #413).
//!
//! A tenant-isolated REST API under `/api/v1` that also serves the shared
//! `ui/` assets. See `docs/web-architecture.md` ("Web backend") and
//! `.claude/rules/web-backend.md`.

pub mod auth;
pub mod clock;
pub mod config;
pub mod crypto;
pub mod error;
pub mod github;
pub mod logging;
pub mod memory;
pub mod model;
pub mod redact;
pub mod routes;
pub mod secret;
pub mod state;
pub mod store;
pub mod usage;
pub mod vault;
pub mod webhook;
