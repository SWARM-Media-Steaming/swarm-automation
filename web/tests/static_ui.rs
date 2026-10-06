//! The shared `ui/` is served with a strict CSP; the API never falls through to it.

mod common;

use axum::http::StatusCode;
use common::*;

#[tokio::test]
async fn health_reports_ok_with_security_headers() {
    let app = TestApp::new();
    let reply = app.get("/api/v1/health").await;
    assert_eq!(reply.status, StatusCode::OK);
    assert_eq!(reply.json()["status"], "ok");
    assert_eq!(reply.headers["cache-control"], "no-store");
}

#[tokio::test]
async fn the_ui_is_served_with_a_strict_content_security_policy() {
    let app = TestApp::new();
    for path in ["/", "/index.html", "/app.js"] {
        let reply = app.get(path).await;
        assert_eq!(reply.status, StatusCode::OK, "{path}");
        let csp = reply.headers["content-security-policy"].to_str().unwrap();
        for directive in [
            "default-src 'none'",
            "script-src 'self'",
            "style-src 'self'",
            "frame-ancestors 'none'",
            "object-src 'none'",
            "base-uri 'none'",
            "connect-src 'self'",
        ] {
            assert!(csp.contains(directive), "{path}: {directive}");
        }
        assert!(
            !csp.contains("unsafe-inline") && !csp.contains("unsafe-eval") && !csp.contains('*'),
            "{csp}"
        );
        assert_eq!(reply.headers["x-content-type-options"], "nosniff");
        assert_eq!(reply.headers["x-frame-options"], "DENY");
        assert_eq!(reply.headers["referrer-policy"], "no-referrer");
    }
    assert!(app.get("/").await.text().contains("<title>SWARM</title>"));
}

#[tokio::test]
async fn the_shipped_ui_works_under_the_policy() {
    // The served assets must not need anything the CSP forbids: no inline
    // script or style, and no third-party origin to load code or data from.
    let ui = std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("../ui");
    let html = std::fs::read_to_string(ui.join("index.html")).expect("ui/index.html");
    assert!(!html.contains(" style=\""), "no inline style attributes");
    let scripts = regex::Regex::new(r"(?is)<script([^>]*)>(.*?)</script>").unwrap();
    for script in scripts.captures_iter(&html) {
        assert!(
            script[1].contains("src="),
            "an inline <script> block: {}",
            &script[0]
        );
        assert!(
            script[2].trim().is_empty(),
            "a <script src> with a body: {}",
            &script[0]
        );
    }
    assert!(
        !regex_find(
            &html,
            r#"<(?:script|link|img|iframe)[^>]+(?:src|href)="https?://"#
        ),
        "no third-party subresources"
    );
    let css = std::fs::read_to_string(ui.join("style.css")).expect("ui/style.css");
    assert!(
        !regex_find(&css, r#"@import|url\(\s*["']?https?:"#),
        "no remote CSS"
    );
}

fn regex_find(text: &str, pattern: &str) -> bool {
    regex::Regex::new(pattern).unwrap().is_match(text)
}

#[tokio::test]
async fn tests_notes_and_dotfiles_are_not_served() {
    let app = TestApp::new();
    for path in ["/app.test.js", "/notes.md", "/.secret", "/api.test.js"] {
        assert_eq!(app.get(path).await.status, StatusCode::NOT_FOUND, "{path}");
    }
}

#[tokio::test]
async fn path_traversal_cannot_leave_the_ui_directory() {
    let app = TestApp::new();
    for path in [
        "/../Cargo.toml",
        "/%2e%2e/Cargo.toml",
        "/..%2fCargo.toml",
        "/%2e%2e%2f%2e%2e%2fetc/passwd",
        "/static/../../Cargo.toml",
    ] {
        let reply = app.get(path).await;
        assert!(
            reply.status == StatusCode::NOT_FOUND || reply.status == StatusCode::BAD_REQUEST,
            "{path} -> {}",
            reply.status
        );
        assert!(!reply.text().contains("swarm-web"), "{path} leaked a file");
    }
}

#[tokio::test]
async fn unknown_api_paths_are_json_404s_not_the_page() {
    let app = TestApp::new();
    for path in ["/api/v1/nope", "/api/v1/tenants/x/y/z/w", "/api/v1/"] {
        let reply = app.get(path).await;
        assert_eq!(reply.status, StatusCode::NOT_FOUND, "{path}");
        assert_eq!(reply.json()["code"], "not_found");
        assert!(reply.headers["content-security-policy"]
            .to_str()
            .unwrap()
            .contains("default-src 'none'"));
    }
}
