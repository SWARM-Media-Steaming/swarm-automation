//! The `Store` contract (issue #439): one body of assertions, run against
//! `MemoryStore` always and against `PostgresStore` when
//! `SWARM_TEST_POSTGRES_DSN` names a database (CI's `storage-live` workflow
//! provides one). Every id is random, so the Postgres runs share a database
//! without cleaning it up and never see each other's rows.

use serde_json::json;
use swarm_web::crypto::{random_hex, SealedSecret};
use swarm_web::memory::MemoryStore;
use swarm_web::model::*;
use swarm_web::postgres::PostgresStore;
use swarm_web::store::Store;

fn installation() -> u64 {
    u64::from_str_radix(&random_hex(6), 16).expect("hex") + 1
}

fn session(user: &User, ttl_from: u64, expires_at: u64) -> Session {
    Session {
        token_hash: random_hex(16),
        user_id: user.id.clone(),
        csrf_token: format!("csrf-{ttl_from}"),
        expires_at,
    }
}

async fn user(store: &dyn Store) -> User {
    store
        .upsert_user(installation(), &format!("login-{}", random_hex(4)))
        .await
        .expect("user")
}

async fn tenant(store: &dyn Store) -> Tenant {
    store
        .upsert_installation_tenant(installation(), "acme", "Organization")
        .await
        .expect("tenant")
}

fn sealed(tag: u8) -> SealedSecret {
    SealedSecret {
        version: 1,
        wrapper_key_id: "local-test".into(),
        wrapped_data_key: vec![tag, 1, 2, 3],
        ciphertext: vec![tag, 9, 8, 7, 6],
    }
}

async fn identity_and_sessions(store: &dyn Store) {
    let github_id = installation();
    let first = store.upsert_user(github_id, "first-login").await.unwrap();
    let again = store.upsert_user(github_id, "renamed").await.unwrap();
    assert_eq!(first.id, again.id, "one person per GitHub id");
    assert_eq!(again.login, "renamed");
    assert_eq!(again.github_id, github_id);
    let stored = store.user(&first.id).await.unwrap().expect("stored user");
    assert_eq!(
        (stored.github_id, stored.login.as_str()),
        (github_id, "renamed")
    );
    assert!(store.user("u-missing").await.unwrap().is_none());

    let live = session(&first, 1, u64::MAX / 4);
    let old = session(&first, 2, 100);
    store.create_session(live.clone()).await.unwrap();
    store.create_session(old.clone()).await.unwrap();
    let read = store
        .session(&live.token_hash)
        .await
        .unwrap()
        .expect("session");
    assert_eq!(read.user_id, first.id);
    assert_eq!(read.csrf_token, live.csrf_token);
    assert_eq!(read.expires_at, live.expires_at);
    assert!(store.session("no-such-hash").await.unwrap().is_none());

    assert!(store.delete_expired_sessions(1_000).await.unwrap() >= 1);
    assert!(store.session(&old.token_hash).await.unwrap().is_none());
    assert!(store.session(&live.token_hash).await.unwrap().is_some());
    store.delete_session(&live.token_hash).await.unwrap();
    assert!(store.session(&live.token_hash).await.unwrap().is_none());
    // Deleting what is gone is not an error.
    store.delete_session(&live.token_hash).await.unwrap();
}

async fn tenants_and_membership(store: &dyn Store) {
    let installation_id = installation();
    let created = store
        .upsert_installation_tenant(installation_id, "acme", "Organization")
        .await
        .unwrap();
    assert_eq!(created.status, TenantStatus::Active);
    let again = store
        .upsert_installation_tenant(installation_id, "acme-renamed", "User")
        .await
        .unwrap();
    assert_eq!(created.id, again.id, "one tenant per installation");
    assert_eq!(again.account_login, "acme-renamed");
    assert_eq!(again.account_type, "User");
    assert_eq!(
        store.tenant(&created.id).await.unwrap().unwrap(),
        again.clone()
    );
    assert_eq!(
        store
            .tenant_by_installation(installation_id)
            .await
            .unwrap()
            .unwrap()
            .id,
        created.id
    );
    assert!(store
        .tenant_by_installation(installation())
        .await
        .unwrap()
        .is_none());
    assert!(store
        .tenant(&TenantId::parse("tnotthere").unwrap())
        .await
        .unwrap()
        .is_none());

    store
        .set_tenant_status(&created.id, TenantStatus::Suspended)
        .await
        .unwrap();
    assert_eq!(
        store.tenant(&created.id).await.unwrap().unwrap().status,
        TenantStatus::Suspended
    );
    // An upsert of a suspended tenant does not reactivate it.
    let kept = store
        .upsert_installation_tenant(installation_id, "acme-renamed", "User")
        .await
        .unwrap();
    assert_eq!(kept.status, TenantStatus::Suspended);
    assert!(store
        .set_tenant_status(&TenantId::parse("tnotthere").unwrap(), TenantStatus::Active)
        .await
        .is_err());

    let other = tenant(store).await;
    let third = tenant(store).await;
    let alice = user(store).await;
    let bob = user(store).await;
    store
        .set_membership(&created.id, &alice.id, Role::Owner)
        .await
        .unwrap();
    store
        .set_membership(&other.id, &alice.id, Role::Member)
        .await
        .unwrap();
    store
        .set_membership(&created.id, &bob.id, Role::Member)
        .await
        .unwrap();
    assert_eq!(
        store.role_in(&created.id, &alice.id).await.unwrap(),
        Some(Role::Owner)
    );
    assert_eq!(store.role_in(&third.id, &alice.id).await.unwrap(), None);
    // Changing a role replaces it.
    store
        .set_membership(&other.id, &alice.id, Role::Owner)
        .await
        .unwrap();
    assert_eq!(
        store.role_in(&other.id, &alice.id).await.unwrap(),
        Some(Role::Owner)
    );

    let mut expected = vec![created.id.clone(), other.id.clone()];
    expected.sort();
    let listed = store.tenants_for_user(&alice.id).await.unwrap();
    assert_eq!(
        listed.iter().map(|(t, _)| t.id.clone()).collect::<Vec<_>>(),
        expected
    );
    let members = store.members(&created.id).await.unwrap();
    let mut logins = vec![alice.login.clone(), bob.login.clone()];
    logins.sort();
    assert_eq!(
        members.iter().map(|m| m.login.clone()).collect::<Vec<_>>(),
        logins
    );
    assert!(store.members(&third.id).await.unwrap().is_empty());

    store
        .retain_memberships(&alice.id, std::slice::from_ref(&created.id))
        .await
        .unwrap();
    assert_eq!(
        store.role_in(&created.id, &alice.id).await.unwrap(),
        Some(Role::Owner)
    );
    assert_eq!(store.role_in(&other.id, &alice.id).await.unwrap(), None);
    assert_eq!(
        store.role_in(&created.id, &bob.id).await.unwrap(),
        Some(Role::Member),
        "another user's membership is untouched"
    );
    store.retain_memberships(&alice.id, &[]).await.unwrap();
    assert!(store.tenants_for_user(&alice.id).await.unwrap().is_empty());
}

async fn provider_keys_are_sealed_and_isolated(store: &dyn Store) {
    let a = tenant(store).await;
    let b = tenant(store).await;
    store
        .put_provider_key(&a.id, Provider::Claude, sealed(1), "alice", 111)
        .await
        .unwrap();
    store
        .put_provider_key(&a.id, Provider::Claude, sealed(2), "bob", 222)
        .await
        .unwrap();
    let stored = store
        .provider_key(&a.id, Provider::Claude)
        .await
        .unwrap()
        .expect("key");
    assert_eq!(stored.sealed, sealed(2), "a put replaces the key");
    assert_eq!(
        (stored.updated_at, stored.updated_by.as_str()),
        (222, "bob")
    );
    assert!(store
        .provider_key(&b.id, Provider::Claude)
        .await
        .unwrap()
        .is_none());
    assert!(store
        .provider_key(&a.id, Provider::Codex)
        .await
        .unwrap()
        .is_none());

    let meta = store.provider_key_meta(&a.id).await.unwrap();
    assert_eq!(meta.len(), Provider::ALL.len());
    let claude = meta
        .iter()
        .find(|m| m.provider == Provider::Claude)
        .unwrap();
    assert!(claude.configured);
    assert_eq!(claude.updated_by.as_deref(), Some("bob"));
    assert!(meta
        .iter()
        .filter(|m| m.provider != Provider::Claude)
        .all(|m| !m.configured && m.updated_at.is_none()));
    assert!(store
        .provider_key_meta(&b.id)
        .await
        .unwrap()
        .iter()
        .all(|m| !m.configured));

    assert!(store
        .delete_provider_key(&a.id, Provider::Claude)
        .await
        .unwrap());
    assert!(!store
        .delete_provider_key(&a.id, Provider::Claude)
        .await
        .unwrap());
}

async fn quotas_budgets_usage_and_reports(store: &dyn Store) {
    let a = tenant(store).await;
    let b = tenant(store).await;
    assert!(store.plan_quotas(&a.id).await.unwrap().is_none());
    let plan = PlanQuotas {
        max_concurrent_jobs: 3,
        monthly_spend_cap_usd: Some(12.5),
    };
    store.set_plan_quotas(&a.id, plan).await.unwrap();
    assert_eq!(store.plan_quotas(&a.id).await.unwrap(), Some(plan));
    let uncapped = PlanQuotas {
        max_concurrent_jobs: 1,
        monthly_spend_cap_usd: None,
    };
    store.set_plan_quotas(&a.id, uncapped).await.unwrap();
    assert_eq!(store.plan_quotas(&a.id).await.unwrap(), Some(uncapped));
    assert!(store.plan_quotas(&b.id).await.unwrap().is_none());

    assert!(store.budgets(&a.id).await.unwrap().is_none());
    let mut budgets = Budgets {
        minimum_remaining_percent: 25.0,
        ..Budgets::default()
    };
    budgets.provider_budgets_usd.insert(Provider::Claude, 40.0);
    budgets.provider_budgets_usd.insert(Provider::Grok, 5.5);
    store.set_budgets(&a.id, budgets.clone()).await.unwrap();
    assert_eq!(store.budgets(&a.id).await.unwrap(), Some(budgets));
    assert!(store.budgets(&b.id).await.unwrap().is_none());

    let entries = vec![
        LedgerEntry {
            id: "e1".into(),
            provider: Provider::Claude,
            cost_usd: Some(1.25),
        },
        LedgerEntry {
            id: "e2".into(),
            provider: Provider::Claude,
            cost_usd: Some(0.5),
        },
        LedgerEntry {
            id: "e3".into(),
            provider: Provider::Claude,
            cost_usd: None,
        },
        LedgerEntry {
            id: "e4".into(),
            provider: Provider::Codex,
            cost_usd: Some(2.0),
        },
    ];
    assert_eq!(
        store
            .record_usage(&a.id, "2026-05", &entries)
            .await
            .unwrap(),
        4
    );
    assert_eq!(
        store
            .record_usage(&a.id, "2026-05", &entries)
            .await
            .unwrap(),
        0,
        "re-recording a batch changes nothing"
    );
    // The same entry id in another tenant is its own entry.
    assert_eq!(
        store
            .record_usage(&b.id, "2026-05", &entries[..1])
            .await
            .unwrap(),
        1
    );
    let spend = store.spend(&a.id, "2026-05").await.unwrap();
    let claude = spend.for_provider(Provider::Claude);
    assert!((claude.spend_usd - 1.75).abs() < 1e-9);
    assert_eq!(claude.priced_invocations, 2);
    assert_eq!(claude.unpriced_invocations, 1, "unpriced is never zero");
    assert!((spend.total_usd() - 3.75).abs() < 1e-9);
    assert_eq!(
        store.spend(&a.id, "2026-06").await.unwrap(),
        Spend::default()
    );
    assert!((store.spend(&b.id, "2026-05").await.unwrap().total_usd() - 1.25).abs() < 1e-9);

    assert!(store
        .provider_report(&a.id, Provider::Claude)
        .await
        .unwrap()
        .is_none());
    let report = ProviderReport {
        remaining_percent: 42.5,
        detail: Some("weekly".into()),
        reported_at: 1_700_000_000,
    };
    store
        .set_provider_report(&a.id, Provider::Claude, report.clone())
        .await
        .unwrap();
    assert_eq!(
        store
            .provider_report(&a.id, Provider::Claude)
            .await
            .unwrap(),
        Some(report)
    );
    let bare = ProviderReport {
        remaining_percent: 7.0,
        detail: None,
        reported_at: 1_700_000_100,
    };
    store
        .set_provider_report(&a.id, Provider::Claude, bare.clone())
        .await
        .unwrap();
    assert_eq!(
        store
            .provider_report(&a.id, Provider::Claude)
            .await
            .unwrap(),
        Some(bare)
    );
    assert!(store
        .provider_report(&b.id, Provider::Claude)
        .await
        .unwrap()
        .is_none());
}

async fn job_slots(store: &dyn Store) {
    let a = tenant(store).await;
    let b = tenant(store).await;
    assert!(store
        .reserve_job(&a.id, "j1", Provider::Claude, 2)
        .await
        .unwrap());
    assert!(
        store
            .reserve_job(&a.id, "j1", Provider::Claude, 2)
            .await
            .unwrap(),
        "reserving a held job id takes no second slot"
    );
    assert_eq!(store.active_jobs(&a.id).await.unwrap(), 1);
    assert!(store
        .reserve_job(&a.id, "j2", Provider::Codex, 2)
        .await
        .unwrap());
    assert!(!store
        .reserve_job(&a.id, "j3", Provider::Codex, 2)
        .await
        .unwrap());
    assert_eq!(store.active_jobs(&a.id).await.unwrap(), 2);
    assert_eq!(
        store.active_jobs(&b.id).await.unwrap(),
        0,
        "slots are per tenant"
    );
    assert!(store
        .reserve_job(&b.id, "j1", Provider::Claude, 1)
        .await
        .unwrap());

    assert!(store.release_job(&a.id, "j1").await.unwrap());
    assert!(!store.release_job(&a.id, "j1").await.unwrap());
    assert!(store
        .reserve_job(&a.id, "j3", Provider::Codex, 2)
        .await
        .unwrap());
    // A released job id can hold a slot again.
    assert!(store.release_job(&a.id, "j3").await.unwrap());
    assert!(store
        .reserve_job(&a.id, "j1", Provider::Claude, 2)
        .await
        .unwrap());
    assert_eq!(store.active_jobs(&a.id).await.unwrap(), 2);
}

async fn concurrent_reservations_respect_the_limit(store: std::sync::Arc<dyn Store>) {
    let t = tenant(store.as_ref()).await;
    let mut tasks = Vec::new();
    for n in 0..8 {
        let store = store.clone();
        let id = t.id.clone();
        tasks.push(tokio::spawn(async move {
            store
                .reserve_job(&id, &format!("race-{n}"), Provider::Claude, 3)
                .await
                .unwrap()
        }));
    }
    let mut won = 0;
    for task in tasks {
        if task.await.unwrap() {
            won += 1;
        }
    }
    assert_eq!(won, 3);
    assert_eq!(store.active_jobs(&t.id).await.unwrap(), 3);
}

async fn documents(store: &dyn Store) {
    let a = tenant(store).await;
    let b = tenant(store).await;
    assert!(store
        .document(&a.id, "tenant_config", "app")
        .await
        .unwrap()
        .is_none());
    store
        .put_document(&a.id, "tenant_config", "repo-2", json!({"n": 2}))
        .await
        .unwrap();
    store
        .put_document(
            &a.id,
            "tenant_config",
            "app",
            json!({"theme": "dark", "list": [1, 2]}),
        )
        .await
        .unwrap();
    store
        .put_document(&a.id, "tenant_config", "app", json!({"theme": "light"}))
        .await
        .unwrap();
    store
        .put_document(&a.id, "prefs", "app", json!("other collection"))
        .await
        .unwrap();
    assert_eq!(
        store.document(&a.id, "tenant_config", "app").await.unwrap(),
        Some(json!({"theme": "light"})),
        "a put replaces the document"
    );
    assert_eq!(
        store.documents(&a.id, "tenant_config").await.unwrap(),
        vec![
            ("app".to_string(), json!({"theme": "light"})),
            ("repo-2".to_string(), json!({"n": 2})),
        ]
    );
    assert!(store
        .documents(&b.id, "tenant_config")
        .await
        .unwrap()
        .is_empty());
    assert!(store
        .document(&b.id, "tenant_config", "app")
        .await
        .unwrap()
        .is_none());
    assert!(store
        .delete_document(&a.id, "tenant_config", "app")
        .await
        .unwrap());
    assert!(!store
        .delete_document(&a.id, "tenant_config", "app")
        .await
        .unwrap());
    assert_eq!(
        store.document(&a.id, "prefs", "app").await.unwrap(),
        Some(json!("other collection"))
    );
}

async fn webhook_deliveries(store: &dyn Store) {
    let id = format!("delivery-{}", random_hex(6));
    let hash = random_hex(32);
    assert_eq!(
        store.claim_delivery(&id, &hash).await.unwrap(),
        DeliveryClaim::New
    );
    assert_eq!(
        store.claim_delivery(&id, &hash).await.unwrap(),
        DeliveryClaim::Duplicate
    );
    let other = format!("delivery-{}", random_hex(6));
    assert_eq!(
        store.claim_delivery(&other, &hash).await.unwrap(),
        DeliveryClaim::Replay
    );
    store.release_delivery(&id).await.unwrap();
    assert_eq!(
        store.claim_delivery(&other, &hash).await.unwrap(),
        DeliveryClaim::New
    );
    store.release_delivery("never-claimed").await.unwrap();
}

async fn contract(store: std::sync::Arc<dyn Store>) {
    identity_and_sessions(store.as_ref()).await;
    tenants_and_membership(store.as_ref()).await;
    provider_keys_are_sealed_and_isolated(store.as_ref()).await;
    quotas_budgets_usage_and_reports(store.as_ref()).await;
    job_slots(store.as_ref()).await;
    concurrent_reservations_respect_the_limit(store.clone()).await;
    documents(store.as_ref()).await;
    webhook_deliveries(store.as_ref()).await;
}

fn postgres_dsn() -> Option<String> {
    std::env::var("SWARM_TEST_POSTGRES_DSN")
        .ok()
        .filter(|dsn| !dsn.trim().is_empty())
}

#[tokio::test]
async fn memory_store_meets_the_contract() {
    contract(std::sync::Arc::new(MemoryStore::new())).await;
}

#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn postgres_store_meets_the_contract() {
    let Some(dsn) = postgres_dsn() else {
        eprintln!("SWARM_TEST_POSTGRES_DSN is not set: skipping the Postgres contract");
        return;
    };
    let store = PostgresStore::connect(&dsn).await.expect("connect");
    contract(std::sync::Arc::new(store)).await;
}

#[tokio::test]
async fn postgres_sessions_and_identity_survive_a_restart() {
    let Some(dsn) = postgres_dsn() else {
        eprintln!("SWARM_TEST_POSTGRES_DSN is not set: skipping the Postgres restart test");
        return;
    };
    let before = PostgresStore::connect(&dsn).await.expect("connect");
    let person = user(&before).await;
    let owned = tenant(&before).await;
    before
        .set_membership(&owned.id, &person.id, Role::Owner)
        .await
        .unwrap();
    let live = session(&person, 1, u64::MAX / 4);
    before.create_session(live.clone()).await.unwrap();
    before
        .put_provider_key(&owned.id, Provider::Grok, sealed(5), "me", 5)
        .await
        .unwrap();
    drop(before);

    // A new process: a new pool over the same database; migrations run again.
    let after = PostgresStore::connect(&dsn).await.expect("reconnect");
    let read = after
        .session(&live.token_hash)
        .await
        .unwrap()
        .expect("the session survived");
    assert_eq!(read.user_id, person.id);
    assert_eq!(read.csrf_token, live.csrf_token);
    assert_eq!(after.user(&person.id).await.unwrap().unwrap(), person);
    assert_eq!(
        after.role_in(&owned.id, &person.id).await.unwrap(),
        Some(Role::Owner)
    );
    assert_eq!(
        after
            .provider_key(&owned.id, Provider::Grok)
            .await
            .unwrap()
            .unwrap()
            .sealed,
        sealed(5)
    );
}

#[tokio::test]
async fn postgres_migrations_run_once_and_twice() {
    let Some(dsn) = postgres_dsn() else {
        return;
    };
    let store = PostgresStore::connect(&dsn).await.expect("connect");
    store.migrate().await.expect("a second run changes nothing");
    let (client, connection) = tokio_postgres::connect(&dsn, tokio_postgres::NoTls)
        .await
        .expect("plain connection to the test database");
    tokio::spawn(connection);
    let rows = client
        .query(
            "SELECT version FROM platform_migrations ORDER BY version",
            &[],
        )
        .await
        .unwrap();
    let applied: Vec<String> = rows.iter().map(|row| row.get(0)).collect();
    for (version, _) in swarm_web::schema::MIGRATIONS {
        assert!(
            applied.iter().any(|v| v == version),
            "{version} is recorded"
        );
    }
}

#[tokio::test]
async fn a_bad_connection_string_is_reported_without_echoing_it() {
    let secret = "postgresql://user:hunter2-not-a-real-password@/%%%bad";
    let error = match PostgresStore::connect(secret).await {
        Ok(_) => panic!("must not connect"),
        Err(error) => error.to_string(),
    };
    assert!(!error.contains("hunter2"), "{error}");
    let unreachable = "postgresql://u:hunter2-not-a-real-password@127.0.0.1:1/db?sslmode=disable&connect_timeout=1";
    let error = match PostgresStore::connect(unreachable).await {
        Ok(_) => panic!("must not connect"),
        Err(error) => error.to_string(),
    };
    assert!(!error.contains("hunter2"), "{error}");
}
