//! SWARM Automation web backend (issue #416, part of #413).
//!
//! A tenant-isolated REST API under `/api/v1` that also serves the shared
//! `ui/` assets. See `docs/web-architecture.md` ("Web backend") and
//! `.claude/rules/web-backend.md`.

pub mod api;
pub mod auth;
pub mod bridge;
pub mod catalog;
pub mod clock;
pub mod config;
pub mod crypto;
pub mod error;
pub mod events;
pub mod github;
pub mod identity;
pub mod jobs_http;
pub mod lifecycle;
pub mod logging;
pub mod memory;
pub mod model;
pub mod orchestrator;
pub mod postgres;
pub mod redact;
pub mod routes;
pub mod runner;
pub mod schema;
pub mod secret;
pub mod settings;
pub mod state;
pub mod store;
pub mod usage;
pub mod vault;
pub mod webhook;
