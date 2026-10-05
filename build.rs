fn main() {
    // Export the Python catalog instead of maintaining a second price list or
    // scraping Python source in Rust. The desktop needs the same effective
    // windows before its first calibration has been published.
    let pricing = std::process::Command::new("python3")
        .args([
            "-c",
            "import dataclasses,json,sys; sys.path.insert(0,sys.argv[1]); import model_pricing as p; errors=p.validate_catalog(); assert not errors, errors; print(json.dumps([dict(dataclasses.asdict(row), starts_at=p.parse_timestamp(row.effective_from).timestamp(), ends_at=p.parse_timestamp(row.effective_to).timestamp() if row.effective_to else None) for row in p.PRICING_CATALOG]))",
        ])
        .arg(std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("issue_worker"))
        .output()
        .expect("Python 3 is required to export the shared model pricing catalog");
    assert!(
        pricing.status.success(),
        "Could not export model prices: {}",
        String::from_utf8_lossy(&pricing.stderr)
    );
    std::fs::write(
        std::path::Path::new(&std::env::var_os("OUT_DIR").unwrap()).join("model-prices.json"),
        pricing.stdout,
    )
    .expect("Could not write the shared pricing catalog");
    println!("cargo:rerun-if-changed=issue_worker/model_pricing.py");

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
