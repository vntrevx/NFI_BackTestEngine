use std::fs;
use std::path::{Path, PathBuf};

use sha2::{Digest, Sha256};

fn main() {
    let manifest_dir = PathBuf::from(
        std::env::var_os("CARGO_MANIFEST_DIR").expect("CARGO_MANIFEST_DIR is set by Cargo"),
    );
    let rust_root = manifest_dir
        .parent()
        .and_then(Path::parent)
        .expect("nfi-py must remain under rust/crates")
        .to_path_buf();
    let files = source_files(&rust_root);
    let mut hasher = Sha256::new();

    for path in files {
        println!("cargo:rerun-if-changed={}", path.display());
        let relative = path
            .strip_prefix(&rust_root)
            .expect("source file belongs to Rust workspace")
            .to_string_lossy()
            .replace('\\', "/");
        let encoded = relative.as_bytes();
        let length = u32::try_from(encoded.len()).expect("source path fits into u32");
        hasher.update(length.to_be_bytes());
        hasher.update(encoded);
        hasher.update(fs::read(&path).expect("Rust source remains readable during build"));
    }

    println!(
        "cargo:rustc-env=NFI_RUST_SOURCE_FINGERPRINT={:x}",
        hasher.finalize()
    );
}

fn source_files(root: &Path) -> Vec<PathBuf> {
    let mut pending = vec![root.to_path_buf()];
    let mut files = Vec::new();
    while let Some(directory) = pending.pop() {
        for entry in fs::read_dir(&directory).expect("Rust source directory is readable") {
            let path = entry.expect("Rust source entry is readable").path();
            if path.is_dir() {
                if path.file_name().is_some_and(|name| name == "target") {
                    continue;
                }
                pending.push(path);
                continue;
            }
            let is_manifest = path.file_name().is_some_and(|name| {
                name == "Cargo.toml" || name == "Cargo.lock" || name == "rust-toolchain.toml"
            });
            if is_manifest || path.extension().is_some_and(|extension| extension == "rs") {
                files.push(path);
            }
        }
    }
    files.sort_by(|left, right| {
        left.strip_prefix(root)
            .expect("left source belongs to workspace")
            .cmp(
                right
                    .strip_prefix(root)
                    .expect("right source belongs to workspace"),
            )
    });
    files
}
