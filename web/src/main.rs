use std::sync::Arc;

use swarm_web::clock::SystemClock;
use swarm_web::config::{Config, JobBackend};
use swarm_web::github::HttpGitHub;
use swarm_web::memory::MemoryStore;
use swarm_web::orchestrator::{JobSettings, Orchestrator, OrchestratorDeps, PythonTokenMinter};
use swarm_web::runner::{DockerJobRunner, EcsFargateJobRunner, JobRunner};
use swarm_web::state::AppState;

#[tokio::main]
async fn main() {
    swarm_web::logging::init();
    let (config, wrapper) = match Config::from_lookup(&|name| std::env::var(name).ok()) {
        Ok(loaded) => loaded,
        Err(error) => {
            eprintln!("swarm-web: configuration error: {error}");
            std::process::exit(2);
        }
    };
    config.register_secrets();
    let github = match HttpGitHub::new(
        config.github.client_id.clone(),
        config.github.client_secret.clone(),
        config.github.web_base.clone(),
        config.github.api_base.clone(),
    ) {
        Ok(client) => client,
        Err(error) => {
            eprintln!("swarm-web: {error}");
            std::process::exit(2);
        }
    };
    // Development store. The Postgres implementation of `Store` replaces this
    // behind the same trait; until then nothing survives a restart.
    tracing::warn!("using the in-memory store: sign-ins, keys and usage are lost on restart");
    let bind = config.bind;
    let state = AppState::new(
        config,
        Arc::new(MemoryStore::new()),
        Arc::new(github),
        Arc::new(wrapper),
        Arc::new(SystemClock),
    );
    attach_bridge(&state);
    attach_jobs(&state);
    let listener = match tokio::net::TcpListener::bind(bind).await {
        Ok(listener) => listener,
        Err(error) => {
            eprintln!("swarm-web: cannot listen on {bind}: {error}");
            std::process::exit(2);
        }
    };
    tracing::info!(%bind, "listening");
    let app = swarm_web::routes::router(state);
    if let Err(error) = axum::serve(listener, app)
        .with_graceful_shutdown(async {
            let _ = tokio::signal::ctrl_c().await;
        })
        .await
    {
        eprintln!("swarm-web: server error: {error}");
        std::process::exit(1);
    }
}

fn attach_bridge(state: &AppState) {
    let Some(bridge) = state.config.bridge.as_ref() else {
        tracing::warn!("worker bridge: off (history, usage and the other worker-computed endpoints answer 503)");
        return;
    };
    tracing::info!("worker bridge: python");
    state.set_bridge(Arc::new(swarm_web::bridge::ProcessBridge::new(
        bridge.python.clone(),
        bridge.worker_dir.clone(),
        bridge.env.clone(),
    )));
}

fn attach_jobs(state: &AppState) {
    let Some(jobs) = state.config.jobs.as_ref() else {
        return;
    };
    let runner: Arc<dyn JobRunner> = match jobs.backend {
        JobBackend::Docker => {
            tracing::info!("job runner: docker");
            Arc::new(DockerJobRunner::new(jobs.docker_bin.clone()))
        }
        JobBackend::Fargate => {
            tracing::info!(cluster = %jobs.ecs_cluster, "job runner: fargate");
            let runner = match EcsFargateJobRunner::new(
                jobs.ecs_endpoint.clone(),
                jobs.ecs_region.clone(),
                jobs.ecs_cluster.clone(),
                jobs.ecs_task_definition.clone(),
                jobs.ecs_subnets.clone(),
                jobs.ecs_security_groups.clone(),
            ) {
                Ok(runner) => runner,
                Err(error) => {
                    eprintln!("swarm-web: {error}");
                    std::process::exit(2);
                }
            };
            let runner = if let (Some(access), Some(secret)) =
                (&jobs.ecs_access_key, &jobs.ecs_secret_key)
            {
                runner.with_static_credentials(access.clone(), secret.clone())
            } else if let Some(url) = role_credentials_url() {
                tracing::info!("fargate runner: signing with the API task role");
                runner.with_role_credentials(
                    url,
                    std::env::var("AWS_CONTAINER_AUTHORIZATION_TOKEN")
                        .ok()
                        .filter(|value| !value.is_empty())
                        .map(swarm_web::secret::Secret::new),
                )
            } else {
                runner
            };
            Arc::new(runner)
        }
    };
    let minter = Arc::new(PythonTokenMinter::new(
        jobs.python.clone(),
        jobs.worker_dir.join("github_app_auth.py"),
    ));
    let settings = JobSettings {
        image: jobs.image.clone(),
        provider: jobs.provider,
        cpu_millis: jobs.cpu_millis,
        memory_mib: jobs.memory_mib,
        max_runtime_secs: jobs.max_runtime_secs,
        quota_resume_secs: jobs.quota_resume_secs,
        poll_secs: jobs.poll_secs,
        network: jobs.docker_network.clone(),
        app_id: jobs.app_id,
        private_key: jobs.private_key.clone(),
        trusted_authors: jobs.trusted_authors.iter().cloned().collect(),
        plain_env: jobs.plain_env.clone(),
        secret_env: jobs.secret_env.clone(),
    };
    let poll_secs = settings.poll_secs.max(1);
    let orchestrator = Orchestrator::new(OrchestratorDeps {
        runner,
        minter,
        store: state.store.clone(),
        vault: state.vault.clone(),
        accounting: state.accounting.clone(),
        clock: state.clock.clone(),
        settings,
    });
    state.set_orchestrator(orchestrator.clone());
    tokio::spawn(async move {
        loop {
            tokio::time::sleep(std::time::Duration::from_secs(poll_secs)).await;
            if let Err(error) = orchestrator.tick().await {
                tracing::warn!(error = %error, "job scheduler tick failed");
            }
        }
    });
}

/// The ECS agent's credentials endpoint for this task's role, when running as
/// an ECS task. The agent injects one of these two variables.
fn role_credentials_url() -> Option<String> {
    if let Ok(full) = std::env::var("AWS_CONTAINER_CREDENTIALS_FULL_URI") {
        if !full.is_empty() {
            return Some(full);
        }
    }
    std::env::var("AWS_CONTAINER_CREDENTIALS_RELATIVE_URI")
        .ok()
        .filter(|relative| relative.starts_with('/'))
        .map(|relative| format!("http://169.254.170.2{relative}"))
}
