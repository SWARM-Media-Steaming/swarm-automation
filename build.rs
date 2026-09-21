fn main() {
    // The release workflow computes the real published version (from the
    // VERSION file and git history, plus a `-beta.<run>` suffix on ai-main)
    // and passes it through this env var so the running app can report
    // exactly what it was published as. Cargo.toml's version is only a
    // placeholder for local builds.
    let version =
        std::env::var("SWARM_APP_VERSION").unwrap_or_else(|_| env!("CARGO_PKG_VERSION").into());
    println!("cargo:rustc-env=SWARM_APP_VERSION={version}");
    println!("cargo:rerun-if-env-changed=SWARM_APP_VERSION");

    tauri_build::build()
}
