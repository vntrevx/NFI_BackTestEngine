//! Durable, integrity-checked aggregate publication shared by native frontends.

use std::collections::BTreeSet;
use std::error::Error;
use std::fmt::{self, Write as _};
use std::fs::{self, File, OpenOptions};
use std::io::{self, Read, Seek, SeekFrom, Write};
use std::path::{Path, PathBuf};

use fs2::FileExt;
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};

const RESULT_NAME: &str = "result.json";
const PROFILE_NAME: &str = "profile.json";
const EVENTS_NAME: &str = "events.jsonl";
const MANIFEST_NAME: &str = "publication.json";
const OWNER_NAME: &str = ".nfi-owner.json";
const COMMIT_SCHEMA: &str = "nfi-artifact-publication-v1";
const OWNER_SCHEMA: &str = "nfi-artifact-owner-v1";
const PUBLISHER: &str = "nfi-native";
const UNPROFILED_PROFILE: &[u8] = b"{\"schema_version\":\"1.0.0\",\"measurement\":\"unprofiled\"}";
const MAX_MANIFEST_BYTES: u64 = 64 * 1024;
const MAX_OWNER_BYTES: u64 = 8 * 1024;

#[derive(Debug)]
pub enum PublicationError {
    SameDestination(PathBuf),
    Identity { path: PathBuf, source: io::Error },
    Stage { path: PathBuf, source: io::Error },
    Lock { path: PathBuf, source: io::Error },
    DestinationExists(PathBuf),
    InvalidBundle { path: PathBuf, message: String },
    Commit { path: PathBuf, source: io::Error },
    Durability { path: PathBuf, source: io::Error },
    Export { path: PathBuf, source: io::Error },
}

impl fmt::Display for PublicationError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::SameDestination(path) => write!(
                formatter,
                "publication destinations are not distinct: {}",
                path.display()
            ),
            Self::Identity { path, source } => write!(
                formatter,
                "cannot identify destination {}: {source}",
                path.display()
            ),
            Self::Stage { path, source } => {
                write!(formatter, "cannot stage {}: {source}", path.display())
            }
            Self::Lock { path, source } => {
                write!(formatter, "cannot lock {}: {source}", path.display())
            }
            Self::DestinationExists(path) => {
                write!(formatter, "destination already exists: {}", path.display())
            }
            Self::InvalidBundle { path, message } => write!(
                formatter,
                "invalid publication bundle {}: {message}",
                path.display()
            ),
            Self::Commit { path, source } => write!(
                formatter,
                "cannot commit publication bundle {}: {source}",
                path.display()
            ),
            Self::Durability { path, source } => write!(
                formatter,
                "cannot sync publication boundary {}: {source}",
                path.display()
            ),
            Self::Export { path, source } => write!(
                formatter,
                "cannot export compatibility artifact {}: {source}",
                path.display()
            ),
        }
    }
}

impl Error for PublicationError {
    fn source(&self) -> Option<&(dyn Error + 'static)> {
        match self {
            Self::SameDestination(_) | Self::DestinationExists(_) | Self::InvalidBundle { .. } => {
                None
            }
            Self::Identity { source, .. }
            | Self::Stage { source, .. }
            | Self::Lock { source, .. }
            | Self::Commit { source, .. }
            | Self::Durability { source, .. }
            | Self::Export { source, .. } => Some(source),
        }
    }
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
struct OwnerMarker {
    schema: String,
    attempt_id: String,
    destination_id: String,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
struct DestinationIdentity {
    result: String,
    profile: Option<String>,
    events: Option<String>,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
struct ArtifactIdentity {
    role: String,
    name: String,
    size: u64,
    sha256: String,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
struct PublicationManifest {
    commit_schema: String,
    publisher: String,
    publication_id: String,
    bundle_id: String,
    integrity_sha256: String,
    attempt_id: String,
    destination_id: String,
    destinations: DestinationIdentity,
    artifacts: Vec<ArtifactIdentity>,
}

#[derive(Serialize)]
struct BundleIdentity<'a> {
    commit_schema: &'a str,
    publisher: &'a str,
    publication_id: &'a str,
    bundle_id: &'a str,
    attempt_id: &'a str,
    destination_id: &'a str,
    destinations: &'a DestinationIdentity,
    artifacts: &'a [ArtifactIdentity],
}

struct ExpectedDestinations<'a> {
    result: &'a Path,
    profile: Option<&'a Path>,
    events: Option<&'a Path>,
}

struct OwnedDirectory {
    path: PathBuf,
    remove_on_drop: bool,
}

impl OwnedDirectory {
    fn create(path: PathBuf) -> io::Result<Self> {
        fs::create_dir(&path)?;
        Ok(Self {
            path,
            remove_on_drop: true,
        })
    }

    fn committed(&mut self) {
        self.remove_on_drop = false;
    }
}

impl Drop for OwnedDirectory {
    fn drop(&mut self) {
        if self.remove_on_drop {
            let _ = fs::remove_dir_all(&self.path);
        }
    }
}

/// Return the sole certified completion surface for an output path.
#[must_use]
pub fn bundle_path(result_path: &Path) -> PathBuf {
    let parent = result_path.parent().unwrap_or_else(|| Path::new("."));
    let name = result_path
        .file_name()
        .and_then(|name| name.to_str())
        .unwrap_or("result");
    parent.join(format!("{name}.nfi-bundle"))
}

/// Publish one result through an aggregate commit boundary.
///
/// # Errors
/// Returns a typed staging, locking, identity, durability, conflict, or export error.
pub fn publish_result(result_path: &Path, result: &[u8]) -> Result<(), PublicationError> {
    publish(result_path, result, None, None, |_| Ok(()))
}

/// Publish a result and staged event trace through one aggregate commit.
///
/// # Errors
/// Returns a typed staging, locking, identity, durability, conflict, or export error.
pub fn publish_result_events(
    result_path: &Path,
    result: &[u8],
    events_path: &Path,
    staged_events: &File,
) -> Result<(), PublicationError> {
    publish(
        result_path,
        result,
        None,
        Some((events_path, staged_events)),
        |_| Ok(()),
    )
}

/// Publish result and profile through one aggregate commit boundary.
///
/// # Errors
/// Returns a typed staging, locking, identity, durability, conflict, or export error.
pub fn publish_result_profile(
    result_path: &Path,
    result: &[u8],
    profile_path: &Path,
    profile: &[u8],
) -> Result<(), PublicationError> {
    distinct(result_path, profile_path)?;
    publish(
        result_path,
        result,
        Some((profile_path, profile)),
        None,
        |_| Ok(()),
    )
}

/// Publish result, profile, and staged event trace through one aggregate commit.
///
/// # Errors
/// Returns a typed staging, locking, identity, durability, conflict, or export error.
pub fn publish_result_profile_events(
    result_path: &Path,
    result: &[u8],
    profile_path: &Path,
    profile: &[u8],
    events_path: &Path,
    staged_events: &File,
) -> Result<(), PublicationError> {
    distinct(result_path, profile_path)?;
    distinct(result_path, events_path)?;
    distinct(profile_path, events_path)?;
    publish(
        result_path,
        result,
        Some((profile_path, profile)),
        Some((events_path, staged_events)),
        |_| Ok(()),
    )
}

/// Validate a committed aggregate without creating compatibility exports.
///
/// # Errors
/// Returns [`PublicationError::InvalidBundle`] for malformed, stale, tampered,
/// symlink-backed, or structurally unexpected bundles.
pub fn validate_publication(
    result_path: &Path,
    profile_path: Option<&Path>,
    events_path: Option<&Path>,
) -> Result<(), PublicationError> {
    let expected = ExpectedDestinations {
        result: result_path,
        profile: profile_path,
        events: events_path,
    };
    validate_bundle(&bundle_path(result_path), &expected).map(|_| ())
}

/// Recover owned attempts and compatibility exports under the destination lock.
///
/// Returns whether a valid committed aggregate exists.
///
/// # Errors
/// Invalid bundles fail before any export is created.
pub fn recover_publication(
    result_path: &Path,
    profile_path: Option<&Path>,
) -> Result<bool, PublicationError> {
    recover_publication_with_events(result_path, profile_path, None)
}

/// Recover a publication that may include an event trace.
///
/// # Errors
/// Invalid bundles fail before any export is created.
pub fn recover_publication_with_events(
    result_path: &Path,
    profile_path: Option<&Path>,
    events_path: Option<&Path>,
) -> Result<bool, PublicationError> {
    let _lock = lock_file(result_path)?;
    let expected = ExpectedDestinations {
        result: result_path,
        profile: profile_path,
        events: events_path,
    };
    let destination_id = destination_id(&expected)?;
    recover_owned_attempts(&expected, &destination_id)?;
    let bundle = bundle_path(result_path);
    if !path_entry_exists(&bundle)? {
        return Ok(false);
    }
    let manifest = validate_bundle(&bundle, &expected)?;
    recover_exports(&bundle, &manifest, &expected)?;
    Ok(true)
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum PublishPoint {
    Staged,
    Committed,
}

fn publish<Hook>(
    result_path: &Path,
    result: &[u8],
    profile: Option<(&Path, &[u8])>,
    events: Option<(&Path, &File)>,
    mut hook: Hook,
) -> Result<(), PublicationError>
where
    Hook: FnMut(PublishPoint) -> io::Result<()>,
{
    let profile_path = profile.map(|(path, _)| path);
    let events_path = events.map(|(path, _)| path);
    let expected = ExpectedDestinations {
        result: result_path,
        profile: profile_path,
        events: events_path,
    };
    let destination_id = destination_id(&expected)?;
    let _lock = lock_file(result_path)?;
    recover_owned_attempts(&expected, &destination_id)?;
    let bundle = bundle_path(result_path);
    if path_entry_exists(&bundle)? {
        return Err(PublicationError::DestinationExists(bundle));
    }
    for destination in [Some(result_path), profile_path, events_path]
        .into_iter()
        .flatten()
    {
        if path_entry_exists(destination)? {
            return Err(PublicationError::DestinationExists(
                destination.to_path_buf(),
            ));
        }
    }

    let parent = result_path.parent().unwrap_or_else(|| Path::new("."));
    let (mut staging, manifest) = stage_bundle(
        &expected,
        &destination_id,
        result,
        profile.map_or(UNPROFILED_PROFILE, |(_, contents)| contents),
        events.map(|(_, source)| source),
    )?;
    let staging_path = staging.path.clone();
    hook(PublishPoint::Staged).map_err(|source| PublicationError::Stage {
        path: staging_path.clone(),
        source,
    })?;
    for destination in [
        Some(result_path),
        profile_path,
        events_path,
        Some(bundle.as_path()),
    ]
    .into_iter()
    .flatten()
    {
        if path_entry_exists(destination)? {
            return Err(PublicationError::DestinationExists(
                destination.to_path_buf(),
            ));
        }
    }
    commit_noreplace(&staging_path, &bundle)?;
    staging.committed();
    sync_directory(parent)?;
    hook(PublishPoint::Committed).map_err(|source| PublicationError::Commit {
        path: bundle.clone(),
        source,
    })?;

    if let Some(path) = profile_path {
        export_artifact(&bundle, &manifest, "profile", path)?;
    }
    if let Some(path) = events_path {
        export_artifact(&bundle, &manifest, "events", path)?;
    }
    export_artifact(&bundle, &manifest, "result", result_path)
}

fn stage_bundle(
    expected: &ExpectedDestinations<'_>,
    destination_id: &str,
    result: &[u8],
    profile: &[u8],
    events: Option<&File>,
) -> Result<(OwnedDirectory, PublicationManifest), PublicationError> {
    let attempt_id = random_id()?;
    let parent = expected.result.parent().unwrap_or_else(|| Path::new("."));
    let staging_path = parent.join(format!("{}{}", staging_prefix(expected.result), attempt_id));
    let staging =
        OwnedDirectory::create(staging_path.clone()).map_err(|source| PublicationError::Stage {
            path: staging_path.clone(),
            source,
        })?;
    let marker = OwnerMarker {
        schema: OWNER_SCHEMA.to_owned(),
        attempt_id: attempt_id.clone(),
        destination_id: destination_id.to_owned(),
    };
    write_json_synced(&staging_path.join(OWNER_NAME), &marker)?;
    sync_directory(&staging_path)?;
    let mut artifacts = vec![write_bytes_synced(
        &staging_path.join(RESULT_NAME),
        "result",
        result,
    )?];
    artifacts.push(write_bytes_synced(
        &staging_path.join(PROFILE_NAME),
        "profile",
        profile,
    )?);
    if let Some(source) = events {
        artifacts.push(copy_file_synced(
            &staging_path.join(EVENTS_NAME),
            "events",
            source,
        )?);
    }
    let destinations = destination_identity(expected)?;
    let publication_id = random_id()?;
    let bundle_id = random_id()?;
    let identity = BundleIdentity {
        commit_schema: COMMIT_SCHEMA,
        publisher: PUBLISHER,
        publication_id: &publication_id,
        bundle_id: &bundle_id,
        attempt_id: &attempt_id,
        destination_id,
        destinations: &destinations,
        artifacts: &artifacts,
    };
    let integrity_sha256 = digest_bytes(
        &serde_json::to_vec(&identity)
            .map_err(|error| invalid(&staging_path, error.to_string()))?,
    );
    let manifest = PublicationManifest {
        commit_schema: COMMIT_SCHEMA.to_owned(),
        publisher: PUBLISHER.to_owned(),
        publication_id,
        bundle_id,
        integrity_sha256,
        attempt_id,
        destination_id: destination_id.to_owned(),
        destinations,
        artifacts,
    };
    write_json_synced(&staging_path.join(MANIFEST_NAME), &manifest)?;
    sync_directory(&staging_path)?;
    Ok((staging, manifest))
}

fn validate_bundle(
    bundle: &Path,
    expected: &ExpectedDestinations<'_>,
) -> Result<PublicationManifest, PublicationError> {
    let metadata =
        fs::symlink_metadata(bundle).map_err(|source| invalid(bundle, source.to_string()))?;
    if metadata.file_type().is_symlink() || !metadata.is_dir() {
        return Err(invalid(
            bundle,
            "completion surface is not a regular directory",
        ));
    }
    let manifest_bytes = read_bounded_regular(&bundle.join(MANIFEST_NAME), MAX_MANIFEST_BYTES)?;
    let manifest: PublicationManifest = serde_json::from_slice(&manifest_bytes)
        .map_err(|error| invalid(bundle, format!("manifest schema: {error}")))?;
    let owner_bytes = read_bounded_regular(&bundle.join(OWNER_NAME), MAX_OWNER_BYTES)?;
    let owner: OwnerMarker = serde_json::from_slice(&owner_bytes)
        .map_err(|error| invalid(bundle, format!("owner schema: {error}")))?;
    validate_manifest_identity(bundle, &manifest, &owner, expected)?;

    let expected_entries = manifest
        .artifacts
        .iter()
        .map(|artifact| artifact.name.as_str())
        .chain([MANIFEST_NAME, OWNER_NAME])
        .collect::<BTreeSet<_>>();
    let actual_entries = fs::read_dir(bundle)
        .map_err(|source| invalid(bundle, source.to_string()))?
        .map(|entry| {
            entry
                .map(|entry| entry.file_name().to_string_lossy().into_owned())
                .map_err(|source| invalid(bundle, source.to_string()))
        })
        .collect::<Result<BTreeSet<_>, _>>()?;
    let expected_owned = expected_entries
        .into_iter()
        .map(str::to_owned)
        .collect::<BTreeSet<_>>();
    if actual_entries != expected_owned {
        return Err(invalid(
            bundle,
            "bundle contains missing or unexpected entries",
        ));
    }
    for artifact in &manifest.artifacts {
        validate_artifact(bundle, artifact)?;
    }
    Ok(manifest)
}

fn validate_manifest_identity(
    bundle: &Path,
    manifest: &PublicationManifest,
    owner: &OwnerMarker,
    expected: &ExpectedDestinations<'_>,
) -> Result<(), PublicationError> {
    if manifest.commit_schema != COMMIT_SCHEMA
        || manifest.publisher != PUBLISHER
        || owner.schema != OWNER_SCHEMA
    {
        return Err(invalid(bundle, "unsupported commit or owner schema"));
    }
    if !valid_id(&manifest.attempt_id)
        || !valid_id(&manifest.publication_id)
        || !valid_id(&manifest.bundle_id)
        || !valid_id(&manifest.integrity_sha256)
        || !valid_id(&manifest.destination_id)
    {
        return Err(invalid(bundle, "identity is not lowercase SHA-256"));
    }
    let destinations = destination_identity(expected)?;
    let destination_id = destination_id(expected)?;
    if manifest.destinations.result != destinations.result
        || manifest.destinations.profile != destinations.profile
        || manifest.destinations.events != destinations.events
        || manifest.destination_id != destination_id
    {
        return Err(invalid(bundle, "stale destination identity"));
    }
    if owner.attempt_id != manifest.attempt_id || owner.destination_id != destination_id {
        return Err(invalid(bundle, "owner marker identity mismatch"));
    }
    let identity = BundleIdentity {
        commit_schema: &manifest.commit_schema,
        publisher: &manifest.publisher,
        publication_id: &manifest.publication_id,
        bundle_id: &manifest.bundle_id,
        attempt_id: &manifest.attempt_id,
        destination_id: &manifest.destination_id,
        destinations: &manifest.destinations,
        artifacts: &manifest.artifacts,
    };
    let integrity_sha256 = digest_bytes(
        &serde_json::to_vec(&identity).map_err(|error| invalid(bundle, error.to_string()))?,
    );
    if manifest.integrity_sha256 != integrity_sha256 {
        return Err(invalid(bundle, "bundle integrity mismatch"));
    }
    let expected_roles = [
        Some("result"),
        Some("profile"),
        expected.events.map(|_| "events"),
    ]
    .into_iter()
    .flatten()
    .collect::<Vec<_>>();
    if manifest.artifacts.len() != expected_roles.len()
        || manifest
            .artifacts
            .iter()
            .zip(expected_roles)
            .any(|(artifact, role)| artifact.role != role || artifact.name != role_name(role))
    {
        return Err(invalid(
            bundle,
            "artifact roles or names do not match the destination contract",
        ));
    }
    Ok(())
}

fn validate_artifact(bundle: &Path, artifact: &ArtifactIdentity) -> Result<(), PublicationError> {
    let path = bundle.join(&artifact.name);
    let mut file = open_regular_nofollow(&path)?;
    let metadata = file
        .metadata()
        .map_err(|source| invalid(&path, source.to_string()))?;
    if metadata.len() != artifact.size {
        return Err(invalid(&path, "artifact size mismatch"));
    }
    let actual = digest_reader(&mut file).map_err(|source| invalid(&path, source.to_string()))?;
    if actual != artifact.sha256 {
        return Err(invalid(&path, "artifact SHA-256 mismatch"));
    }
    Ok(())
}

fn recover_exports(
    bundle: &Path,
    manifest: &PublicationManifest,
    expected: &ExpectedDestinations<'_>,
) -> Result<(), PublicationError> {
    if let Some(path) = expected.profile {
        if !path_entry_exists(path)? {
            export_artifact(bundle, manifest, "profile", path)?;
        }
    }
    if let Some(path) = expected.events {
        if !path_entry_exists(path)? {
            export_artifact(bundle, manifest, "events", path)?;
        }
    }
    if !path_entry_exists(expected.result)? {
        export_artifact(bundle, manifest, "result", expected.result)?;
    }
    Ok(())
}

fn export_artifact(
    bundle: &Path,
    manifest: &PublicationManifest,
    role: &str,
    destination: &Path,
) -> Result<(), PublicationError> {
    let artifact = manifest
        .artifacts
        .iter()
        .find(|artifact| artifact.role == role)
        .ok_or_else(|| invalid(bundle, format!("missing {role} artifact")))?;
    let destination_id = manifest.destination_id.as_str();
    let attempt_id = &manifest.attempt_id;
    let parent = destination.parent().unwrap_or_else(|| Path::new("."));
    let export_path = parent.join(format!("{}{}", export_prefix(destination), attempt_id));
    let mut owned_directory =
        OwnedDirectory::create(export_path.clone()).map_err(|source| PublicationError::Export {
            path: export_path.clone(),
            source,
        })?;
    let marker = OwnerMarker {
        schema: OWNER_SCHEMA.to_owned(),
        attempt_id: attempt_id.clone(),
        destination_id: destination_id.to_owned(),
    };
    write_json_synced(&export_path.join(OWNER_NAME), &marker)?;
    sync_directory(&export_path)?;
    let staged = export_path.join("artifact");
    let mut source = open_regular_nofollow(&bundle.join(&artifact.name))?;
    let mut output = OpenOptions::new()
        .write(true)
        .create_new(true)
        .open(&staged)
        .map_err(|source| PublicationError::Export {
            path: staged.clone(),
            source,
        })?;
    let mut hasher = Sha256::new();
    let mut size = 0_u64;
    let mut buffer = vec![0_u8; 64 * 1024].into_boxed_slice();
    loop {
        let count = source
            .read(&mut buffer)
            .map_err(|source| PublicationError::Export {
                path: bundle.join(&artifact.name),
                source,
            })?;
        if count == 0 {
            break;
        }
        output
            .write_all(&buffer[..count])
            .map_err(|source| PublicationError::Export {
                path: staged.clone(),
                source,
            })?;
        hasher.update(&buffer[..count]);
        size = size.saturating_add(u64::try_from(count).unwrap_or(u64::MAX));
    }
    output
        .sync_all()
        .map_err(|source| PublicationError::Export {
            path: staged.clone(),
            source,
        })?;
    if size != artifact.size || format!("{:x}", hasher.finalize()) != artifact.sha256 {
        return Err(invalid(bundle, "artifact changed during export"));
    }
    sync_directory(&export_path)?;
    fs::hard_link(&staged, destination).map_err(|source| PublicationError::Export {
        path: destination.to_path_buf(),
        source,
    })?;
    sync_directory(parent)?;
    fs::remove_dir_all(&export_path).map_err(|source| PublicationError::Export {
        path: export_path.clone(),
        source,
    })?;
    owned_directory.committed();
    Ok(())
}

fn recover_owned_attempts(
    expected: &ExpectedDestinations<'_>,
    destination_id: &str,
) -> Result<(), PublicationError> {
    recover_directories(
        expected.result.parent().unwrap_or_else(|| Path::new(".")),
        &staging_prefix(expected.result),
        destination_id,
    )?;
    for destination in [Some(expected.result), expected.profile, expected.events]
        .into_iter()
        .flatten()
    {
        recover_directories(
            destination.parent().unwrap_or_else(|| Path::new(".")),
            &export_prefix(destination),
            destination_id,
        )?;
    }
    Ok(())
}

fn recover_directories(
    parent: &Path,
    prefix: &str,
    destination_id: &str,
) -> Result<(), PublicationError> {
    for entry in fs::read_dir(parent).map_err(|source| PublicationError::Stage {
        path: parent.to_path_buf(),
        source,
    })? {
        let entry = entry.map_err(|source| PublicationError::Stage {
            path: parent.to_path_buf(),
            source,
        })?;
        if !entry.file_name().to_string_lossy().starts_with(prefix) {
            continue;
        }
        let path = entry.path();
        let metadata = fs::symlink_metadata(&path).map_err(|source| PublicationError::Stage {
            path: path.clone(),
            source,
        })?;
        if metadata.file_type().is_symlink() || !metadata.is_dir() {
            continue;
        }
        let Some(marker) = read_bounded_regular(&path.join(OWNER_NAME), MAX_OWNER_BYTES)
            .ok()
            .and_then(|bytes| serde_json::from_slice::<OwnerMarker>(&bytes).ok())
        else {
            continue;
        };
        if marker.schema == OWNER_SCHEMA
            && marker.destination_id == destination_id
            && valid_id(&marker.attempt_id)
            && entry
                .file_name()
                .to_string_lossy()
                .ends_with(&marker.attempt_id)
        {
            fs::remove_dir_all(&path).map_err(|source| PublicationError::Stage { path, source })?;
        }
    }
    Ok(())
}

fn write_bytes_synced(
    path: &Path,
    role: &str,
    contents: &[u8],
) -> Result<ArtifactIdentity, PublicationError> {
    let mut file = OpenOptions::new()
        .write(true)
        .create_new(true)
        .open(path)
        .map_err(|source| PublicationError::Stage {
            path: path.to_path_buf(),
            source,
        })?;
    file.write_all(contents)
        .and_then(|()| file.sync_all())
        .map_err(|source| PublicationError::Stage {
            path: path.to_path_buf(),
            source,
        })?;
    Ok(ArtifactIdentity {
        role: role.to_owned(),
        name: role_name(role).to_owned(),
        size: u64::try_from(contents.len()).unwrap_or(u64::MAX),
        sha256: digest_bytes(contents),
    })
}

fn copy_file_synced(
    path: &Path,
    role: &str,
    source_file: &File,
) -> Result<ArtifactIdentity, PublicationError> {
    let mut source = source_file
        .try_clone()
        .map_err(|source| PublicationError::Stage {
            path: path.to_path_buf(),
            source,
        })?;
    source
        .seek(SeekFrom::Start(0))
        .map_err(|source| PublicationError::Stage {
            path: path.to_path_buf(),
            source,
        })?;
    let mut output = OpenOptions::new()
        .write(true)
        .create_new(true)
        .open(path)
        .map_err(|source| PublicationError::Stage {
            path: path.to_path_buf(),
            source,
        })?;
    let mut hasher = Sha256::new();
    let mut size = 0_u64;
    let mut buffer = vec![0_u8; 64 * 1024].into_boxed_slice();
    loop {
        let count = source
            .read(&mut buffer)
            .map_err(|source| PublicationError::Stage {
                path: path.to_path_buf(),
                source,
            })?;
        if count == 0 {
            break;
        }
        output
            .write_all(&buffer[..count])
            .map_err(|source| PublicationError::Stage {
                path: path.to_path_buf(),
                source,
            })?;
        hasher.update(&buffer[..count]);
        size = size.saturating_add(u64::try_from(count).unwrap_or(u64::MAX));
    }
    output
        .sync_all()
        .map_err(|source| PublicationError::Stage {
            path: path.to_path_buf(),
            source,
        })?;
    Ok(ArtifactIdentity {
        role: role.to_owned(),
        name: role_name(role).to_owned(),
        size,
        sha256: format!("{:x}", hasher.finalize()),
    })
}

fn write_json_synced(path: &Path, value: &impl Serialize) -> Result<(), PublicationError> {
    let encoded = serde_json::to_vec(value).map_err(|error| invalid(path, error.to_string()))?;
    write_raw_synced(path, &encoded)
}

fn write_raw_synced(path: &Path, contents: &[u8]) -> Result<(), PublicationError> {
    let mut file = OpenOptions::new()
        .write(true)
        .create_new(true)
        .open(path)
        .map_err(|source| PublicationError::Stage {
            path: path.to_path_buf(),
            source,
        })?;
    file.write_all(contents)
        .and_then(|()| file.sync_all())
        .map_err(|source| PublicationError::Stage {
            path: path.to_path_buf(),
            source,
        })
}

fn read_bounded_regular(path: &Path, limit: u64) -> Result<Vec<u8>, PublicationError> {
    let file = open_regular_nofollow(path)?;
    let length = file
        .metadata()
        .map_err(|source| invalid(path, source.to_string()))?
        .len();
    if length > limit {
        return Err(invalid(path, "document exceeds size limit"));
    }
    let mut bytes = Vec::with_capacity(usize::try_from(length).unwrap_or(0));
    file.take(limit + 1)
        .read_to_end(&mut bytes)
        .map_err(|source| invalid(path, source.to_string()))?;
    if u64::try_from(bytes.len()).unwrap_or(u64::MAX) > limit {
        return Err(invalid(path, "document exceeds size limit"));
    }
    Ok(bytes)
}

fn open_regular_nofollow(path: &Path) -> Result<File, PublicationError> {
    let metadata =
        fs::symlink_metadata(path).map_err(|source| invalid(path, source.to_string()))?;
    if metadata.file_type().is_symlink() || !metadata.is_file() {
        return Err(invalid(path, "entry is not a regular no-follow file"));
    }
    let mut options = OpenOptions::new();
    options.read(true);
    #[cfg(unix)]
    {
        use std::os::unix::fs::OpenOptionsExt;
        options.custom_flags(libc::O_NOFOLLOW);
    }
    #[cfg(windows)]
    {
        use std::os::windows::fs::OpenOptionsExt;
        options.custom_flags(0x0020_0000);
    }
    let file = options
        .open(path)
        .map_err(|source| invalid(path, source.to_string()))?;
    if !file
        .metadata()
        .map_err(|source| invalid(path, source.to_string()))?
        .is_file()
    {
        return Err(invalid(path, "opened entry is not regular"));
    }
    Ok(file)
}

#[cfg(unix)]
fn commit_noreplace(source: &Path, destination: &Path) -> Result<(), PublicationError> {
    use rustix::fs::{renameat_with, RenameFlags, CWD};
    renameat_with(CWD, source, CWD, destination, RenameFlags::NOREPLACE).map_err(|error| {
        let source = io::Error::from_raw_os_error(error.raw_os_error());
        if source.kind() == io::ErrorKind::AlreadyExists {
            PublicationError::DestinationExists(destination.to_path_buf())
        } else {
            PublicationError::Commit {
                path: destination.to_path_buf(),
                source,
            }
        }
    })
}

#[cfg(windows)]
fn commit_noreplace(source: &Path, destination: &Path) -> Result<(), PublicationError> {
    fs::rename(source, destination).map_err(|source| {
        if source.kind() == io::ErrorKind::AlreadyExists
            || source.kind() == io::ErrorKind::PermissionDenied
        {
            PublicationError::DestinationExists(destination.to_path_buf())
        } else {
            PublicationError::Commit {
                path: destination.to_path_buf(),
                source,
            }
        }
    })
}

#[cfg(unix)]
fn sync_directory(path: &Path) -> Result<(), PublicationError> {
    File::open(path)
        .and_then(|directory| directory.sync_all())
        .map_err(|source| PublicationError::Durability {
            path: path.to_path_buf(),
            source,
        })
}

#[cfg(windows)]
fn sync_directory(path: &Path) -> Result<(), PublicationError> {
    use std::os::windows::fs::OpenOptionsExt;
    let mut options = OpenOptions::new();
    options
        .read(true)
        // BACKUP_SEMANTICS opens directories; OPEN_REPARSE_POINT prevents
        // following a substituted junction while establishing durability.
        .custom_flags(0x0200_0000 | 0x0020_0000);
    let directory = options
        .open(path)
        .map_err(|source| PublicationError::Durability {
            path: path.to_path_buf(),
            source,
        })?;
    match directory.sync_all() {
        Ok(()) => Ok(()),
        // Windows filesystems may reject directory FlushFileBuffers with
        // ERROR_ACCESS_DENIED, ERROR_INVALID_FUNCTION, or ERROR_NOT_SUPPORTED.
        // Payload handles remain synced and the rename remains atomic.
        Err(source) if matches!(source.raw_os_error(), Some(1 | 5 | 50)) => Ok(()),
        Err(source) => Err(PublicationError::Durability {
            path: path.to_path_buf(),
            source,
        }),
    }
}

fn destination_identity(
    expected: &ExpectedDestinations<'_>,
) -> Result<DestinationIdentity, PublicationError> {
    Ok(DestinationIdentity {
        result: path_identity(expected.result)?,
        profile: expected.profile.map(path_identity).transpose()?,
        events: expected.events.map(path_identity).transpose()?,
    })
}

fn destination_id(expected: &ExpectedDestinations<'_>) -> Result<String, PublicationError> {
    let identity = destination_identity(expected)?;
    let encoded = serde_json::to_vec(&identity)
        .map_err(|error| invalid(expected.result, error.to_string()))?;
    Ok(digest_bytes(&encoded))
}

fn path_identity(path: &Path) -> Result<String, PublicationError> {
    let parent = path.parent().unwrap_or_else(|| Path::new("."));
    let canonical_parent = parent
        .canonicalize()
        .map_err(|source| PublicationError::Identity {
            path: path.to_path_buf(),
            source,
        })?;
    let name = path.file_name().ok_or_else(|| PublicationError::Identity {
        path: path.to_path_buf(),
        source: io::Error::new(io::ErrorKind::InvalidInput, "destination has no file name"),
    })?;
    Ok(digest_bytes(
        canonical_parent.join(name).to_string_lossy().as_bytes(),
    ))
}

fn random_id() -> Result<String, PublicationError> {
    let mut bytes = [0_u8; 32];
    getrandom::fill(&mut bytes).map_err(|error| PublicationError::Stage {
        path: PathBuf::from("<random-attempt-id>"),
        source: io::Error::other(error.to_string()),
    })?;
    let mut encoded = String::with_capacity(bytes.len() * 2);
    for byte in bytes {
        write!(encoded, "{byte:02x}").map_err(|error| PublicationError::Stage {
            path: PathBuf::from("<random-attempt-id>"),
            source: io::Error::other(error.to_string()),
        })?;
    }
    Ok(encoded)
}

fn digest_reader(reader: &mut impl Read) -> io::Result<String> {
    let mut hasher = Sha256::new();
    let mut buffer = vec![0_u8; 64 * 1024].into_boxed_slice();
    loop {
        let count = reader.read(&mut buffer)?;
        if count == 0 {
            break;
        }
        hasher.update(&buffer[..count]);
    }
    Ok(format!("{:x}", hasher.finalize()))
}

fn digest_bytes(bytes: &[u8]) -> String {
    format!("{:x}", Sha256::digest(bytes))
}
fn valid_id(value: &str) -> bool {
    value.len() == 64
        && value
            .bytes()
            .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
}
fn role_name(role: &str) -> &'static str {
    match role {
        "result" => RESULT_NAME,
        "profile" => PROFILE_NAME,
        "events" => EVENTS_NAME,
        _ => "invalid",
    }
}
fn path_entry_exists(path: &Path) -> Result<bool, PublicationError> {
    match fs::symlink_metadata(path) {
        Ok(_) => Ok(true),
        Err(source) if source.kind() == io::ErrorKind::NotFound => Ok(false),
        Err(source) => Err(PublicationError::Identity {
            path: path.to_path_buf(),
            source,
        }),
    }
}

fn distinct(first: &Path, second: &Path) -> Result<(), PublicationError> {
    if first == second {
        Err(PublicationError::SameDestination(first.to_path_buf()))
    } else {
        Ok(())
    }
}
fn invalid(path: &Path, message: impl Into<String>) -> PublicationError {
    PublicationError::InvalidBundle {
        path: path.to_path_buf(),
        message: message.into(),
    }
}
fn export_prefix(path: &Path) -> String {
    format!(
        ".{}.nfi-export-",
        path.file_name()
            .and_then(|name| name.to_str())
            .unwrap_or("artifact")
    )
}
fn staging_prefix(path: &Path) -> String {
    format!(
        ".{}.nfi-stage-",
        path.file_name()
            .and_then(|name| name.to_str())
            .unwrap_or("result")
    )
}

fn lock_file(path: &Path) -> Result<File, PublicationError> {
    let parent = path.parent().unwrap_or_else(|| Path::new("."));
    let name = path
        .file_name()
        .and_then(|name| name.to_str())
        .unwrap_or("result");
    let lock_path = parent.join(format!(".{name}.nfi-publication.lock"));
    let lock = OpenOptions::new()
        .read(true)
        .write(true)
        .create(true)
        .truncate(false)
        .open(&lock_path)
        .map_err(|source| PublicationError::Lock {
            path: lock_path.clone(),
            source,
        })?;
    FileExt::lock_exclusive(&lock).map_err(|source| PublicationError::Lock {
        path: lock_path,
        source,
    })?;
    Ok(lock)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::mpsc;
    use std::thread;
    use std::time::Duration;

    fn pair() -> Result<(tempfile::TempDir, PathBuf, PathBuf), Box<dyn Error>> {
        let directory = tempfile::tempdir()?;
        let result = directory.path().join("result.json");
        let profile = directory.path().join("profile.json");
        Ok((directory, result, profile))
    }

    #[test]
    fn manifest_binds_identity_sizes_and_hashes() -> Result<(), Box<dyn Error>> {
        let (_directory, result, profile) = pair()?;
        publish_result_profile(&result, b"result", &profile, b"profile")?;
        validate_publication(&result, Some(&profile), None)?;
        let manifest: PublicationManifest =
            serde_json::from_slice(&fs::read(bundle_path(&result).join(MANIFEST_NAME))?)?;
        assert_eq!(manifest.commit_schema, COMMIT_SCHEMA);
        assert!(
            valid_id(&manifest.publication_id)
                && valid_id(&manifest.bundle_id)
                && valid_id(&manifest.destination_id)
        );
        assert_eq!(
            manifest
                .artifacts
                .iter()
                .map(|artifact| (artifact.name.as_str(), artifact.size))
                .collect::<Vec<_>>(),
            vec![(RESULT_NAME, 6), (PROFILE_NAME, 7)]
        );
        assert_eq!(manifest.artifacts[0].sha256, digest_bytes(b"result"));
        assert_ne!(manifest.attempt_id, manifest.publication_id);
        assert_ne!(manifest.attempt_id, manifest.bundle_id);
        assert_ne!(manifest.publication_id, manifest.bundle_id);
        Ok(())
    }

    #[test]
    fn invalid_bundles_never_recover_or_export() -> Result<(), Box<dyn Error>> {
        for mutation in ["payload", "manifest", "identity", "extra", "missing"] {
            let (_directory, result, profile) = pair()?;
            publish_result_profile(&result, b"result", &profile, b"profile")?;
            fs::remove_file(&result)?;
            fs::remove_file(&profile)?;
            let bundle = bundle_path(&result);
            match mutation {
                "payload" => fs::write(bundle.join(RESULT_NAME), b"tampered")?,
                "manifest" => {
                    let mut bytes = fs::read(bundle.join(MANIFEST_NAME))?;
                    bytes[1] = b'X';
                    fs::write(bundle.join(MANIFEST_NAME), bytes)?;
                }
                "identity" => {
                    let mut manifest: PublicationManifest =
                        serde_json::from_slice(&fs::read(bundle.join(MANIFEST_NAME))?)?;
                    manifest.destinations.result = "0".repeat(64);
                    fs::write(bundle.join(MANIFEST_NAME), serde_json::to_vec(&manifest)?)?;
                }
                "extra" => fs::write(bundle.join("unexpected"), b"x")?,
                "missing" => fs::remove_file(bundle.join(PROFILE_NAME))?,
                _ => unreachable!(),
            }
            assert!(matches!(
                recover_publication(&result, Some(&profile)),
                Err(PublicationError::InvalidBundle { .. })
            ));
            assert!(!result.exists() && !profile.exists());
        }
        Ok(())
    }

    #[cfg(unix)]
    #[test]
    fn symlink_backed_payload_is_rejected_without_export() -> Result<(), Box<dyn Error>> {
        use std::os::unix::fs::symlink;
        let (_directory, result, profile) = pair()?;
        publish_result_profile(&result, b"result", &profile, b"profile")?;
        fs::remove_file(&result)?;
        fs::remove_file(&profile)?;
        let payload = bundle_path(&result).join(RESULT_NAME);
        fs::remove_file(&payload)?;
        symlink("profile.json", payload)?;
        assert!(recover_publication(&result, Some(&profile)).is_err());
        assert!(!result.exists() && !profile.exists());
        Ok(())
    }

    #[test]
    fn unmarked_and_wrong_owner_prefix_matches_are_preserved() -> Result<(), Box<dyn Error>> {
        let (directory, result, profile) = pair()?;
        let unmarked =
            directory
                .path()
                .join(format!("{}{}", staging_prefix(&result), "a".repeat(64)));
        fs::create_dir(&unmarked)?;
        let wrong = directory
            .path()
            .join(format!("{}{}", staging_prefix(&result), "b".repeat(64)));
        fs::create_dir(&wrong)?;
        write_json_synced(
            &wrong.join(OWNER_NAME),
            &OwnerMarker {
                schema: OWNER_SCHEMA.to_owned(),
                attempt_id: "b".repeat(64),
                destination_id: "c".repeat(64),
            },
        )?;
        publish_result_profile(&result, b"result", &profile, b"profile")?;
        assert!(unmarked.exists() && wrong.exists());
        Ok(())
    }

    #[test]
    fn observer_sees_only_synced_aggregate_at_commit() -> Result<(), Box<dyn Error>> {
        let (_directory, result, profile) = pair()?;
        let bundle = bundle_path(&result);
        publish(
            &result,
            b"result",
            Some((&profile, b"profile")),
            None,
            |point| {
                if point == PublishPoint::Staged {
                    assert!(!bundle.exists() && !result.exists() && !profile.exists());
                }
                Ok(())
            },
        )?;
        validate_publication(&result, Some(&profile), None)?;
        Ok(())
    }

    #[test]
    fn destination_inserted_at_commit_is_preserved() -> Result<(), Box<dyn Error>> {
        let (_directory, result, profile) = pair()?;
        let bundle = bundle_path(&result);
        let outcome = publish(
            &result,
            b"result",
            Some((&profile, b"profile")),
            None,
            |point| {
                if point == PublishPoint::Staged {
                    fs::create_dir(&bundle)?;
                }
                Ok(())
            },
        );
        assert!(
            matches!(outcome, Err(PublicationError::DestinationExists(path)) if path == bundle)
        );
        assert!(bundle.is_dir());
        assert_eq!(fs::read_dir(bundle)?.count(), 0);
        assert!(!result.exists() && !profile.exists());
        Ok(())
    }

    #[test]
    fn concurrent_writers_commit_one_valid_bundle() -> Result<(), Box<dyn Error>> {
        let (_directory, result, profile) = pair()?;
        let (ready_tx, ready_rx) = mpsc::sync_channel(2);
        let (done_tx, done_rx) = mpsc::sync_channel(2);
        let mut children = Vec::new();
        let mut triggers = Vec::new();
        for identity in *b"ab" {
            let result = result.clone();
            let profile = profile.clone();
            let ready_tx = ready_tx.clone();
            let done_tx = done_tx.clone();
            let (trigger_tx, trigger_rx) = mpsc::sync_channel(1);
            triggers.push(trigger_tx);
            children.push(thread::spawn(move || {
                ready_tx.send(())?;
                trigger_rx.recv_timeout(Duration::from_secs(5))?;
                let outcome = publish_result_profile(&result, &[identity], &profile, &[identity]);
                done_tx.send(outcome)?;
                Ok::<_, Box<dyn Error + Send + Sync>>(())
            }));
        }
        ready_rx.recv_timeout(Duration::from_secs(5))?;
        ready_rx.recv_timeout(Duration::from_secs(5))?;
        for trigger in triggers {
            trigger.send(())?;
        }
        let outcomes = [
            done_rx.recv_timeout(Duration::from_secs(5))?,
            done_rx.recv_timeout(Duration::from_secs(5))?,
        ];
        for child in children {
            child
                .join()
                .map_err(|_| "thread panicked")?
                .map_err(|error| error.to_string())?;
        }
        assert_eq!(outcomes.iter().filter(|outcome| outcome.is_ok()).count(), 1);
        validate_publication(&result, Some(&profile), None)?;
        Ok(())
    }

    #[test]
    fn preexisting_destinations_are_preserved() -> Result<(), Box<dyn Error>> {
        let (_directory, result, profile) = pair()?;
        fs::write(&result, b"old-result")?;
        fs::write(&profile, b"old-profile")?;
        assert!(publish_result_profile(&result, b"new", &profile, b"new").is_err());
        assert_eq!(fs::read(result)?, b"old-result");
        assert_eq!(fs::read(profile)?, b"old-profile");
        Ok(())
    }

    #[cfg(unix)]
    #[test]
    fn dangling_configured_destinations_are_conflicts_before_staging() -> Result<(), Box<dyn Error>>
    {
        use std::os::unix::fs::symlink;
        for conflict in ["result", "profile", "events", "bundle"] {
            let directory = tempfile::tempdir()?;
            let result = directory.path().join("result.json");
            let profile = directory.path().join("profile.json");
            let events = directory.path().join("events.jsonl");
            let bundle = bundle_path(&result);
            let conflict_path = match conflict {
                "result" => &result,
                "profile" => &profile,
                "events" => &events,
                "bundle" => &bundle,
                _ => unreachable!(),
            };
            symlink("missing-target", conflict_path)?;
            let staged_path = directory.path().join("staged-events");
            fs::write(&staged_path, b"event\n")?;
            let staged = File::open(staged_path)?;

            let outcome = publish_result_profile_events(
                &result, b"result", &profile, b"profile", &events, &staged,
            );

            assert!(
                matches!(outcome, Err(PublicationError::DestinationExists(path)) if path == *conflict_path)
            );
            assert_eq!(fs::read_link(conflict_path)?, Path::new("missing-target"));
            for destination in [&result, &profile, &events, &bundle] {
                if destination != conflict_path {
                    assert!(fs::symlink_metadata(destination).is_err());
                }
            }
            assert!(
                !fs::read_dir(directory.path())?.any(|entry| entry.is_ok_and(|entry| {
                    entry.file_name().to_string_lossy().contains(".nfi-stage-")
                }))
            );
        }
        Ok(())
    }

    #[test]
    fn event_trace_is_manifest_bound() -> Result<(), Box<dyn Error>> {
        let directory = tempfile::tempdir()?;
        let result = directory.path().join("result.json");
        let events = directory.path().join("events.jsonl");
        let staged = directory.path().join("staged-events");
        fs::write(&staged, b"event\n")?;
        let staged = File::open(staged)?;
        publish_result_events(&result, b"result", &events, &staged)?;
        validate_publication(&result, None, Some(&events))?;
        let bundle = bundle_path(&result);
        let profile: serde_json::Value =
            serde_json::from_slice(&fs::read(bundle.join(PROFILE_NAME))?)?;
        assert_eq!(profile["measurement"], "unprofiled");
        let manifest: PublicationManifest =
            serde_json::from_slice(&fs::read(bundle.join(MANIFEST_NAME))?)?;
        assert_eq!(
            manifest
                .artifacts
                .iter()
                .map(|artifact| artifact.role.as_str())
                .collect::<Vec<_>>(),
            vec!["result", "profile", "events"]
        );
        assert_eq!(fs::read(events)?, b"event\n");
        Ok(())
    }
}
