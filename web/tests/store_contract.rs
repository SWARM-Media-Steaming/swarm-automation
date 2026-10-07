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

fn profile(subject: u64, login: &str) -> IdentityProfile {
    IdentityProfile {
        provider: "github".into(),
        subject: subject.to_string(),
        login: login.into(),
        display_name: Some(format!("{login} Display")),
        avatar_url: Some(format!("https://avatars.test/u/{subject}")),
    }
}

/// A login no other test (or earlier run, against a shared database) used, so
/// its personal tenant id is exactly the slug.
fn fresh_login() -> String {
    format!("user-{}", random_hex(6))
}

async fn register(store: &dyn Store, profile: &IdentityProfile) -> Registration {
    store.register_identity(profile).await.expect("register")
}

async fn user(store: &dyn Store) -> User {
    register(store, &profile(installation(), &fresh_login()))
        .await
        .user
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
    let login = fresh_login();
    let first = register(store, &profile(github_id, &login)).await.user;
    let again = register(store, &profile(github_id, "renamed")).await.user;
    assert_eq!(first.id, again.id, "one person per (provider, subject)");
    assert_eq!(again.login, "renamed");
    let stored = store.user(&first.id).await.unwrap().expect("stored user");
    assert_eq!(stored, again);
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

    let personal = store
        .tenants_for_user(&alice.id)
        .await
        .unwrap()
        .into_iter()
        .find(|(t, _)| t.installation_id.is_none())
        .expect("alice's personal tenant")
        .0;
    let mut expected = vec![created.id.clone(), other.id.clone(), personal.id.clone()];
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

fn personal_of(registration: &Registration) -> &Tenant {
    assert_eq!(registration.tenant.installation_id, None);
    assert_eq!(registration.tenant.account_type, PERSONAL_ACCOUNT_TYPE);
    assert_eq!(registration.tenant.status, TenantStatus::Active);
    &registration.tenant
}

async fn first_sign_in_registers_user_identity_tenant_and_owner(store: &dyn Store) {
    let login = fresh_login();
    let subject = installation();
    let first = register(store, &profile(subject, &login)).await;
    assert!(first.first_sign_in);
    let tenant = personal_of(&first);
    assert_eq!(
        tenant.id.as_str(),
        login,
        "the tenant id is the slugified login"
    );
    assert_eq!(tenant.account_login, login);
    assert_eq!(first.user.login, login);
    assert_eq!(
        first.user.display_name.as_deref(),
        Some(format!("{login} Display").as_str())
    );
    assert_eq!(
        first.user.avatar_url.as_deref(),
        Some(format!("https://avatars.test/u/{subject}").as_str())
    );
    assert!(first.user.last_login_at.is_some());
    assert_eq!(
        store.user(&first.user.id).await.unwrap().unwrap(),
        first.user
    );
    assert_eq!(store.tenant(&tenant.id).await.unwrap().unwrap(), *tenant);
    assert_eq!(
        store.role_in(&tenant.id, &first.user.id).await.unwrap(),
        Some(Role::Owner)
    );
    let listed = store.tenants_for_user(&first.user.id).await.unwrap();
    assert_eq!(listed.len(), 1);
    assert_eq!(
        (listed[0].0.id.clone(), listed[0].1),
        (tenant.id.clone(), Role::Owner)
    );
    let members = store.members(&tenant.id).await.unwrap();
    assert_eq!(members.len(), 1);
    assert_eq!(
        (members[0].login.as_str(), members[0].role),
        (login.as_str(), Role::Owner)
    );
}

async fn repeat_sign_in_matches_on_provider_and_subject(store: &dyn Store) {
    let login = fresh_login();
    let subject = installation();
    let first = register(store, &profile(subject, &login)).await;
    let mut changed = profile(subject, &login);
    changed.display_name = Some("New Name".into());
    changed.avatar_url = None;
    let again = register(store, &changed).await;
    assert!(!again.first_sign_in);
    assert_eq!(again.user.id, first.user.id);
    assert_eq!(again.tenant, first.tenant, "no second tenant");
    assert_eq!(again.user.display_name.as_deref(), Some("New Name"));
    assert_eq!(again.user.avatar_url, None);
    assert!(again.user.last_login_at >= first.user.last_login_at);
    assert_eq!(
        store.user(&again.user.id).await.unwrap().unwrap(),
        again.user
    );
    assert_eq!(
        store.tenants_for_user(&first.user.id).await.unwrap().len(),
        1
    );
    // The same subject under another provider is another person.
    let mut elsewhere = profile(subject, &login);
    elsewhere.provider = "oidc-example".into();
    let other = register(store, &elsewhere).await;
    assert!(other.first_sign_in);
    assert_ne!(other.user.id, first.user.id);
    assert_ne!(other.tenant.id, first.tenant.id);
}

async fn a_renamed_login_keeps_the_tenant_id(store: &dyn Store) {
    let login = fresh_login();
    let renamed = fresh_login();
    let subject = installation();
    let first = register(store, &profile(subject, &login)).await;
    let again = register(store, &profile(subject, &renamed)).await;
    assert_eq!(again.user.id, first.user.id);
    assert_eq!(
        again.tenant.id, first.tenant.id,
        "the tenant id never follows a rename"
    );
    assert_eq!(again.tenant.id.as_str(), login);
    assert_eq!(again.user.login, renamed);
    assert_eq!(
        again.tenant.account_login, renamed,
        "the displayed account does"
    );
    assert_eq!(
        store.user(&first.user.id).await.unwrap().unwrap().login,
        renamed
    );
    assert_eq!(
        store.members(&first.tenant.id).await.unwrap()[0].login,
        renamed
    );
    // A different person who now holds the old login gets a different tenant.
    let newcomer = register(store, &profile(installation(), &login)).await;
    assert_ne!(newcomer.tenant.id, first.tenant.id);
    assert_eq!(newcomer.tenant.id.as_str(), format!("{login}-2"));
}

async fn a_taken_tenant_id_is_deduplicated(store: &dyn Store) {
    let login = fresh_login();
    let one = register(store, &profile(installation(), &login)).await;
    let two = register(store, &profile(installation(), &login)).await;
    let three = register(store, &profile(installation(), &login)).await;
    assert_eq!(one.tenant.id.as_str(), login);
    assert_eq!(two.tenant.id.as_str(), format!("{login}-2"));
    assert_eq!(three.tenant.id.as_str(), format!("{login}-3"));
    // An id some other kind of tenant already holds is taken too.
    let installed = tenant(store).await;
    let squatter = register(store, &profile(installation(), installed.id.as_str())).await;
    assert_eq!(squatter.tenant.id.as_str(), format!("{}-2", installed.id));
    // Logins that slugify to the same id collide the same way.
    let base = fresh_login();
    let shouty = base.to_uppercase();
    let a = register(store, &profile(installation(), &base)).await;
    let b = register(store, &profile(installation(), &shouty)).await;
    assert_eq!(a.tenant.id.as_str(), base);
    assert_eq!(b.tenant.id.as_str(), format!("{base}-2"));
    // An id that cannot be a slug at all still gets a valid one.
    let odd = register(store, &profile(installation(), "日本語")).await;
    assert!(odd.tenant.id.as_str().starts_with("user"));
    // Each person owns only their own tenant.
    for registration in [&one, &two, &three] {
        let listed = store.tenants_for_user(&registration.user.id).await.unwrap();
        assert_eq!(listed.len(), 1);
        assert_eq!(listed[0].0.id, registration.tenant.id);
    }
}

async fn concurrent_first_sign_ins_register_one_user_and_one_tenant(store: &dyn Store) {
    let login = fresh_login();
    let subject = installation();
    let who = profile(subject, &login);
    let (a, b, c, d, e, f) = tokio::join!(
        store.register_identity(&who),
        store.register_identity(&who),
        store.register_identity(&who),
        store.register_identity(&who),
        store.register_identity(&who),
        store.register_identity(&who),
    );
    let all: Vec<Registration> = [a, b, c, d, e, f]
        .into_iter()
        .map(|r| r.expect("a racing sign-in must not fail"))
        .collect();
    assert_eq!(
        all.iter().filter(|r| r.first_sign_in).count(),
        1,
        "exactly one creates the user"
    );
    for r in &all {
        assert_eq!(r.user.id, all[0].user.id);
        assert_eq!(r.tenant.id, all[0].tenant.id);
    }
    assert_eq!(all[0].tenant.id.as_str(), login);
    let listed = store.tenants_for_user(&all[0].user.id).await.unwrap();
    assert_eq!(listed.len(), 1, "one personal tenant");
    assert_eq!(store.members(&all[0].tenant.id).await.unwrap().len(), 1);

    // Different people whose logins collide, at the same moment: every one gets
    // a tenant of their own.
    let shared = fresh_login();
    let people: Vec<IdentityProfile> = (0..4).map(|_| profile(installation(), &shared)).collect();
    let (a, b, c, d) = tokio::join!(
        store.register_identity(&people[0]),
        store.register_identity(&people[1]),
        store.register_identity(&people[2]),
        store.register_identity(&people[3]),
    );
    let mut ids: Vec<String> = [a, b, c, d]
        .into_iter()
        .map(|r| r.expect("racing registrations").tenant.id.to_string())
        .collect();
    ids.sort();
    ids.dedup();
    assert_eq!(ids.len(), 4, "four people, four tenants: {ids:?}");
    assert!(ids.iter().all(|id| id.starts_with(&shared)));
}

async fn a_failed_registration_leaves_nothing_behind(store: &dyn Store) {
    let login = fresh_login();
    // An empty subject violates the identity's own constraint, after the user,
    // tenant and membership statements have run in Postgres.
    let mut broken = profile(installation(), &login);
    broken.subject = String::new();
    assert!(store.register_identity(&broken).await.is_err());
    let mut broken = profile(installation(), &login);
    broken.provider = String::new();
    assert!(store.register_identity(&broken).await.is_err());
    // Nothing remains: the next registration of that login gets the plain id.
    let next = register(store, &profile(installation(), &login)).await;
    assert!(next.first_sign_in);
    assert_eq!(
        next.tenant.id.as_str(),
        login,
        "the failed attempt kept no tenant"
    );
    assert_eq!(
        store.tenants_for_user(&next.user.id).await.unwrap().len(),
        1
    );
    // The error says what failed, not who was signing in.
    let mut broken = profile(installation(), &login);
    broken.subject = String::new();
    let message = store
        .register_identity(&broken)
        .await
        .unwrap_err()
        .to_string();
    assert!(!message.contains(&login), "{message}");
}

async fn identities_list_provider_and_login_only(store: &dyn Store) {
    let login = fresh_login();
    let user = register(store, &profile(installation(), &login)).await.user;
    let other = register(store, &profile(installation(), &fresh_login()))
        .await
        .user;
    assert_eq!(
        store.identities_for_user(&user.id).await.unwrap(),
        vec![("github".to_string(), login)]
    );
    assert_ne!(
        store.identities_for_user(&other.id).await.unwrap(),
        store.identities_for_user(&user.id).await.unwrap()
    );
    assert!(store
        .identities_for_user("u-missing")
        .await
        .unwrap()
        .is_empty());
}

async fn contract(store: std::sync::Arc<dyn Store>) {
    first_sign_in_registers_user_identity_tenant_and_owner(store.as_ref()).await;
    repeat_sign_in_matches_on_provider_and_subject(store.as_ref()).await;
    a_renamed_login_keeps_the_tenant_id(store.as_ref()).await;
    a_taken_tenant_id_is_deduplicated(store.as_ref()).await;
    concurrent_first_sign_ins_register_one_user_and_one_tenant(store.as_ref()).await;
    a_failed_registration_leaves_nothing_behind(store.as_ref()).await;
    identity_and_sessions(store.as_ref()).await;
    identities_list_provider_and_login_only(store.as_ref()).await;
    tenants_and_membership(store.as_ref()).await;
    provider_keys_are_sealed_and_isolated(store.as_ref()).await;
    quotas_budgets_usage_and_reports(store.as_ref()).await;
    job_slots(store.as_ref()).await;
    concurrent_reservations_respect_the_limit(store.clone()).await;
    documents(store.as_ref()).await;
    webhook_deliveries(store.as_ref()).await;
}

/// Platform admins (issue #441). "No admin exists" is a property of the whole
/// database, so this runs against an *empty* store: a fresh `MemoryStore`, and
/// for Postgres a schema of its own (`isolated_postgres`).
async fn platform_admins(store: std::sync::Arc<dyn Store>) {
    let tag = random_hex(4);
    let mut people = Vec::new();
    let mut subjects = Vec::new();
    for (n, name) in ["aaa", "Bbb", "ccc", "ddd", "eee"].iter().enumerate() {
        let subject = installation();
        subjects.push(subject);
        people.push(
            register(
                store.as_ref(),
                &profile(subject, &format!("{name}-{tag}-{n}")),
            )
            .await
            .user,
        );
    }
    let [a, b, c, d, e] = &people[..] else {
        unreachable!()
    };
    assert!(
        people.iter().all(|u| !u.is_platform_admin),
        "nobody is an admin by default"
    );
    assert!(store.admin_audit_log(10).await.unwrap().is_empty());
    assert!(!store.bootstrap_platform_admin("u-missing").await.unwrap());

    // Racing bootstraps (two listed accounts signing in at once): one wins.
    let mut tasks = Vec::new();
    for person in [a, b, c, d, e] {
        let store = store.clone();
        let id = person.id.clone();
        tasks.push(tokio::spawn(async move {
            store.bootstrap_platform_admin(&id).await.unwrap()
        }));
    }
    let mut winners = 0;
    for task in tasks {
        winners += usize::from(task.await.unwrap());
    }
    assert_eq!(winners, 1, "the bootstrap happens once");
    let admins: Vec<User> = store
        .platform_users()
        .await
        .unwrap()
        .into_iter()
        .filter(|u| u.is_platform_admin)
        .collect();
    assert_eq!(admins.len(), 1);
    let first = admins[0].clone();
    assert!(
        !store.bootstrap_platform_admin(&first.id).await.unwrap(),
        "idempotent: the admin is not bootstrapped again"
    );
    let rows = store.admin_audit_log(10).await.unwrap();
    assert_eq!(rows.len(), 1, "one audit row for one bootstrap");
    assert_eq!(rows[0].action, AUDIT_ADMIN_BOOTSTRAP);
    assert_eq!(rows[0].actor_user_id.as_deref(), Some(first.id.as_str()));
    assert_eq!(rows[0].target_user_id.as_deref(), Some(first.id.as_str()));
    assert_eq!(rows[0].detail["target_login"], first.login.as_str());

    // A sign-in never changes the flag, whoever has it.
    for (person, subject) in people.iter().zip(&subjects) {
        let signed_in = register(store.as_ref(), &profile(*subject, &person.login)).await;
        assert_eq!(signed_in.user.id, person.id);
        assert_eq!(signed_in.user.is_platform_admin, person.id == first.id);
    }
    assert!(
        store
            .user(&first.id)
            .await
            .unwrap()
            .unwrap()
            .is_platform_admin
    );

    // The list is every user, by login without regard to case.
    let listed: Vec<String> = store
        .platform_users()
        .await
        .unwrap()
        .into_iter()
        .filter(|u| people.iter().any(|p| p.id == u.id))
        .map(|u| u.login)
        .collect();
    let mut expected = listed.clone();
    expected.sort_by_key(|login| login.to_lowercase());
    assert_eq!(listed, expected);
    assert_eq!(listed.len(), 5);

    // Promote and demote: a real change writes one row, a repeat writes none.
    let other = people.iter().find(|u| u.id != first.id).unwrap().clone();
    match store
        .set_platform_admin(&first.id, &other.id, true)
        .await
        .unwrap()
    {
        AdminChange::Changed(user) => assert!(user.is_platform_admin && user.id == other.id),
        unexpected => panic!("{unexpected:?}"),
    }
    assert!(matches!(
        store
            .set_platform_admin(&first.id, &other.id, true)
            .await
            .unwrap(),
        AdminChange::Unchanged(_)
    ));
    assert_eq!(store.admin_audit_log(10).await.unwrap().len(), 2);
    assert!(matches!(
        store
            .set_platform_admin(&first.id, "u-missing", true)
            .await
            .unwrap(),
        AdminChange::UnknownUser
    ));
    assert!(
        store
            .set_platform_admin("u-missing", &other.id, false)
            .await
            .is_err(),
        "an unknown actor cannot write an audit row"
    );
    assert!(
        store
            .user(&other.id)
            .await
            .unwrap()
            .unwrap()
            .is_platform_admin
    );

    // Racing demotions of the two admins leave exactly one, never zero.
    let (x, y) = (first.id.clone(), other.id.clone());
    let (s1, s2) = (store.clone(), store.clone());
    let (x1, y1, x2, y2) = (x.clone(), y.clone(), x.clone(), y.clone());
    let one = tokio::spawn(async move { s1.set_platform_admin(&x1, &y1, false).await.unwrap() });
    let two = tokio::spawn(async move { s2.set_platform_admin(&y2, &x2, false).await.unwrap() });
    let results = [one.await.unwrap(), two.await.unwrap()];
    let changed = results
        .iter()
        .filter(|r| matches!(r, AdminChange::Changed(_)))
        .count();
    let last = results
        .iter()
        .filter(|r| matches!(r, AdminChange::LastAdmin))
        .count();
    assert_eq!((changed, last), (1, 1), "{results:?}");
    let remaining: Vec<User> = store
        .platform_users()
        .await
        .unwrap()
        .into_iter()
        .filter(|u| u.is_platform_admin)
        .collect();
    assert_eq!(remaining.len(), 1);

    // The last admin cannot be demoted, by anyone, and a refusal writes nothing.
    let rows_before = store.admin_audit_log(100).await.unwrap().len();
    let sole = remaining[0].id.clone();
    assert_eq!(
        store.set_platform_admin(&sole, &sole, false).await.unwrap(),
        AdminChange::LastAdmin
    );
    assert!(store.user(&sole).await.unwrap().unwrap().is_platform_admin);
    assert_eq!(store.admin_audit_log(100).await.unwrap().len(), rows_before);

    // Audit rows, newest first, name actor, target and the action.
    let rows = store.admin_audit_log(100).await.unwrap();
    assert_eq!(
        rows.len(),
        3,
        "bootstrap, promote and the one demotion that won"
    );
    assert!(rows.windows(2).all(|pair| pair[0].id > pair[1].id));
    assert_eq!(rows[0].action, AUDIT_ADMIN_DEMOTE);
    assert_eq!(rows[rows.len() - 1].action, AUDIT_ADMIN_BOOTSTRAP);
    assert_eq!(store.admin_audit_log(1).await.unwrap().len(), 1);
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
async fn memory_store_meets_the_platform_admin_contract() {
    platform_admins(std::sync::Arc::new(MemoryStore::new())).await;
}

/// A store over an empty schema of its own, so "no admin exists yet" holds
/// however many other tests share the database. The schema is left behind like
/// the random ids the other tests leave.
async fn isolated_postgres(dsn: &str) -> PostgresStore {
    let (client, connection) = tokio_postgres::connect(dsn, tokio_postgres::NoTls)
        .await
        .expect("plain connection to the test database");
    tokio::spawn(connection);
    let schema = format!("contract_{}", random_hex(6));
    client
        .batch_execute(&format!("CREATE SCHEMA {schema}"))
        .await
        .expect("create schema");
    let options = format!("-c search_path={schema}");
    let isolated = if dsn.contains("://") {
        let joiner = if dsn.contains('?') { '&' } else { '?' };
        format!(
            "{dsn}{joiner}options={}",
            options.replace(' ', "%20").replace('=', "%3D")
        )
    } else {
        format!("{dsn} options='{options}'")
    };
    PostgresStore::connect(&isolated).await.expect("connect")
}

#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn postgres_store_meets_the_platform_admin_contract() {
    let Some(dsn) = postgres_dsn() else {
        eprintln!("SWARM_TEST_POSTGRES_DSN is not set: skipping the Postgres admin contract");
        return;
    };
    platform_admins(std::sync::Arc::new(isolated_postgres(&dsn).await)).await;
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
