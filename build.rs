fn main() {
    // The release workflow computes the real published version (with its
    // `-beta.<run>` / `+main.<run>` suffix) and passes it through this env
    // var so the running app can report exactly what it was published as,
    // independent of whatever plain semver is hand-written in Cargo.toml.
    let version =
        std::env::var("SWARM_APP_VERSION").unwrap_or_else(|_| env!("CARGO_PKG_VERSION").into());
    println!("cargo:rustc-env=SWARM_APP_VERSION={version}");
    println!("cargo:rerun-if-env-changed=SWARM_APP_VERSION");

    tauri_build::build()
}
