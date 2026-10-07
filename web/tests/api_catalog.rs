//! The command catalog cannot drift (issue #419): the desktop's Tauri commands,
//! the router's routes, `ui/api.js`'s tables and `docs/web-architecture.md`
//! must all say the same thing.

use std::collections::{BTreeMap, BTreeSet};
use std::path::PathBuf;

use regex::Regex;
use swarm_web::catalog::{self, Scope};

fn repo(path: &str) -> String {
    let root = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("..");
    std::fs::read_to_string(root.join(path)).unwrap_or_else(|e| panic!("{path}: {e}"))
}

/// Every `#[tauri::command]` function name in the desktop's `src/main.rs`.
fn desktop_commands() -> BTreeSet<String> {
    let source = repo("src/main.rs");
    let command = Regex::new(
        r"(?m)^#\[tauri::command\]\s*\n(?:\s*#\[[^\n]*\]\s*\n)*\s*(?:pub\s+)?(?:async\s+)?fn\s+(\w+)",
    )
    .unwrap();
    command
        .captures_iter(&source)
        .map(|m| m[1].to_string())
        .collect()
}

fn catalog_commands() -> BTreeSet<String> {
    catalog::ROUTES
        .iter()
        .flat_map(|route| route.commands.iter())
        .filter(|name| !name.starts_with("web_"))
        .map(|name| name.to_string())
        .collect()
}

fn removed() -> BTreeSet<String> {
    catalog::REMOVED
        .iter()
        .map(|r| r.command.to_string())
        .collect()
}

/// `name: { method: "GET", path: "/x" }` rows of `ui/api.js` `COMMANDS`.
fn js_commands() -> BTreeMap<String, (String, String)> {
    let source = repo("ui/api.js");
    let start = source.find("const COMMANDS = {").expect("COMMANDS table");
    let end = source[start..].find("\n  };").expect("end of COMMANDS") + start;
    let row = Regex::new(r#"(?m)^\s+(\w+): \{ method: "(\w+)", path: "([^"]+)" \},"#).unwrap();
    row.captures_iter(&source[start..end])
        .map(|c| (c[1].to_string(), (c[2].to_string(), c[3].to_string())))
        .collect()
}

fn js_events() -> BTreeMap<String, String> {
    let source = repo("ui/api.js");
    let start = source.find("const EVENTS = {").expect("EVENTS table");
    let end = source[start..].find("\n  };").expect("end of EVENTS") + start;
    let row = Regex::new(r#""?([\w\-]+)"?: \{ path: "([^"]+)""#).unwrap();
    row.captures_iter(&source[start..end])
        .map(|c| (c[1].to_string(), c[2].to_string()))
        .collect()
}

#[test]
fn every_desktop_command_is_an_endpoint_or_listed_as_removed() {
    let desktop = desktop_commands();
    assert!(
        desktop.len() > 60,
        "the command list was found: {}",
        desktop.len()
    );
    let served = catalog_commands();
    let gone = removed();
    for name in &desktop {
        assert!(
            served.contains(name) ^ gone.contains(name),
            "{name} must be exactly one of: an endpoint, intentionally removed"
        );
    }
    for name in served.union(&gone) {
        assert!(
            desktop.contains(name),
            "{name} is in the catalog but is not a desktop command"
        );
    }
}

#[test]
fn removed_commands_say_why_and_are_not_in_the_adapter() {
    let js = js_commands();
    for item in catalog::REMOVED {
        assert!(item.reason.len() > 20, "{} needs a reason", item.command);
        assert!(
            !js.contains_key(item.command),
            "{} is removed but ui/api.js maps it",
            item.command
        );
    }
}

#[test]
fn the_adapter_table_matches_the_catalog_exactly() {
    let js = js_commands();
    let mut expected: BTreeMap<String, (String, String)> = BTreeMap::new();
    for route in catalog::ROUTES {
        let path = match route.scope {
            Scope::Tenant => format!("/tenants/{{tenant}}{}", route.path),
            Scope::Public => route.path.to_string(),
        };
        for name in route.commands {
            expected.insert(name.to_string(), (route.method.to_string(), path.clone()));
        }
    }
    for (name, row) in &expected {
        assert_eq!(js.get(name), Some(row), "ui/api.js row for {name}");
    }
    // What is left in the adapter is the account/job commands of earlier issues.
    for name in js.keys().filter(|name| !expected.contains_key(*name)) {
        assert!(
            name.starts_with("web_"),
            "{name} is in ui/api.js but not in the catalog"
        );
    }
}

#[test]
fn the_admin_routes_are_in_the_adapter_and_the_docs() {
    let js = js_commands();
    let docs = repo("docs/web-architecture.md");
    for route in catalog::ADMIN_ROUTES {
        assert!(
            route.command.starts_with("web_admin_"),
            "{} is a web-only command",
            route.command
        );
        assert_eq!(
            js.get(route.command),
            Some(&(route.method.to_string(), route.path.to_string())),
            "ui/api.js row for {}",
            route.command
        );
        let row = format!("`{} {}`", route.method, catalog::admin_full_path(route));
        assert!(
            docs.contains(&row),
            "docs/web-architecture.md is missing {row}"
        );
        assert!(
            docs.contains(&format!("`{}`", route.command)),
            "docs do not name {}",
            route.command
        );
    }
    // Nothing in the adapter claims to be an admin command the backend lacks.
    for name in js.keys().filter(|name| name.starts_with("web_admin_")) {
        assert!(
            catalog::ADMIN_ROUTES.iter().any(|r| r.command == name),
            "{name} is in ui/api.js but not in catalog::ADMIN_ROUTES"
        );
    }
    // Admin routes are platform-wide: never under a tenant.
    for route in catalog::ADMIN_ROUTES {
        assert!(!route.path.contains("{tenant}"), "{}", route.path);
    }
}

#[test]
fn every_desktop_event_is_streamed_or_listed_as_removed() {
    let js = js_events();
    for event in catalog::EVENTS {
        assert_eq!(
            js.get(event.event).map(String::as_str),
            Some(event.path),
            "ui/api.js stream for {}",
            event.event
        );
    }
    let app = repo("ui/app.js");
    let listen = Regex::new(r#"\blisten\("([a-z\-]+)""#).unwrap();
    let streamed: BTreeSet<&str> = catalog::EVENTS.iter().map(|e| e.event).collect();
    let dropped: BTreeSet<&str> = catalog::REMOVED_EVENTS
        .iter()
        .map(|(name, _)| *name)
        .collect();
    for m in listen.captures_iter(&app) {
        let event = &m[1];
        assert!(
            streamed.contains(event) ^ dropped.contains(event),
            "{event} is neither streamed nor removed"
        );
    }
    let emitted = Regex::new(r#"emit\("([a-z\-]+)""#).unwrap();
    for source in ["src/main.rs", "src/processes.rs"] {
        for m in emitted.captures_iter(&repo(source)) {
            let event = &m[1];
            if event == "stderr" {
                continue; // a log stream name inside `emit_log`, not an event
            }
            assert!(
                streamed.contains(event) || dropped.contains(event),
                "desktop event {event} has no web answer"
            );
        }
    }
    assert!(
        streamed.contains("automation-log"),
        "the log event is emitted by emit_log"
    );
}

#[test]
fn the_docs_print_the_same_table() {
    let docs = repo("docs/web-architecture.md");
    for route in catalog::ROUTES {
        let path = catalog::full_path(route);
        let row = format!("`{} {}`", route.method, path);
        assert!(
            docs.contains(&row),
            "docs/web-architecture.md is missing {row}"
        );
        for name in route
            .commands
            .iter()
            .filter(|name| !name.starts_with("web_"))
        {
            assert!(
                docs.contains(&format!("`{name}`")),
                "docs do not mention {name}"
            );
        }
    }
    for item in catalog::REMOVED {
        assert!(
            docs.contains(&format!("`{}`", item.command)),
            "docs do not list {} as removed",
            item.command
        );
    }
    for event in catalog::EVENTS {
        assert!(
            docs.contains(&format!("`{}`", event.event)),
            "docs do not list the {} stream",
            event.event
        );
        assert!(docs.contains(event.path), "docs do not list {}", event.path);
    }
    for (event, _) in catalog::REMOVED_EVENTS {
        assert!(
            docs.contains(&format!("`{event}`")),
            "docs do not list {event} as removed"
        );
    }
}

#[test]
fn bridge_operations_have_a_worker_implementation_or_an_honest_unavailable() {
    let source = repo("issue_worker/web_bridge.py");
    for route in catalog::ROUTES {
        if let catalog::Handler::Bridge(op) = route.handler {
            assert!(
                source.contains(&format!("\"{op}\"")),
                "web_bridge.py neither implements nor declares unavailable the {op} operation"
            );
        }
    }
}
