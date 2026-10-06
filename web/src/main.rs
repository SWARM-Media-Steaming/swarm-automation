use std::sync::Arc;

use swarm_web::clock::SystemClock;
use swarm_web::config::Config;
use swarm_web::github::HttpGitHub;
use swarm_web::memory::MemoryStore;
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
