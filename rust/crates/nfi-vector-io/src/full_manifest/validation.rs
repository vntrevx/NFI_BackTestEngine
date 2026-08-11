use std::collections::BTreeSet;
use std::path::Component;

use nfi_sim_core::PortfolioConfig;
use nfi_vector_core::alignment::{FrameIdentity, Timeframe};
use nfi_vector_core::mutation::MutationProgram;
use nfi_vector_core::program::IndicatorProgram;
use sha2::{Digest, Sha256};

use super::model::{
    ArtifactDocument, CompileContext, FeatureRetention, FrameDocument, HistoricPriceStep,
    IdentityDocument, ManifestDocument, NativeContractError, PairContract, PairLimits, PairOptions,
    PairPrecision, RunContract, SourceSeal, TradingMode, ValidatedDocument, ValidatedFrame,
    ValidatedFutures,
};
use super::FULL_NATIVE_VECTOR_MANIFEST_VERSION;

pub(super) fn validate_document(
    document: ManifestDocument,
) -> Result<ValidatedDocument, NativeContractError> {
    let (source, config, compile_context, run) = validate_header(&document)?;
    validate_program_descriptors(&document)?;
    let retained_features = validate_features(document.retained_features)?;
    let (pairs, pair_names) = validate_pairs(document.pairs, &run)?;
    let frames = validate_base_frames(document.frames, &pairs)?;
    let futures = validate_futures(
        document.futures.unwrap_or_default(),
        run.trading_mode,
        &pair_names,
    )?;
    Ok(ValidatedDocument {
        source,
        config,
        compile_context,
        programs: document.programs,
        run,
        retained_features,
        pairs,
        frames,
        futures,
    })
}

fn validate_header(
    document: &ManifestDocument,
) -> Result<(SourceSeal, PortfolioConfig, CompileContext, RunContract), NativeContractError> {
    if document.schema_version != FULL_NATIVE_VECTOR_MANIFEST_VERSION {
        return Err(invalid(format!(
            "unsupported schema_version {:?}",
            document.schema_version
        )));
    }
    let source = SourceSeal {
        strategy_sha256: checked_digest(
            "strategy_sha256",
            document.source.strategy_sha256.clone(),
        )?,
        config_sha256: checked_digest("config_sha256", document.source.config_sha256.clone())?,
        compiler_source_fingerprint: checked_digest(
            "compiler_source_fingerprint",
            document.source.compiler_source_fingerprint.clone(),
        )?,
        selected_class: checked_name("selected_class", document.source.selected_class.clone())?,
    };
    let config_identity = serde_json::to_vec(&document.config)
        .map_err(|error| invalid(format!("cannot serialize config identity: {error}")))?;
    let actual_config_sha = format!("{:x}", Sha256::digest(config_identity));
    if actual_config_sha != source.config_sha256 {
        return Err(invalid(
            "config_sha256 differs from the embedded simulator config",
        ));
    }
    let config: PortfolioConfig = serde_json::from_value(document.config.clone())
        .map_err(|error| invalid(format!("embedded simulator config is invalid: {error}")))?;
    if document.compile_context.run_mode != "backtest" {
        return Err(invalid("compile_context.run_mode must be backtest"));
    }
    if document.run.trading_mode != document.compile_context.trading_mode {
        return Err(invalid(
            "run trading_mode differs from the compiled trading_mode",
        ));
    }
    if config.is_futures != (document.run.trading_mode == TradingMode::Futures) {
        return Err(invalid(
            "simulator config is_futures differs from run trading_mode",
        ));
    }
    if document.run.timerange.start_ms < 0
        || document.run.timerange.stop_ms < document.run.timerange.start_ms
    {
        return Err(invalid("run timerange millisecond bounds are invalid"));
    }
    let base_timeframe = Timeframe::parse(document.run.base_timeframe.clone())
        .map_err(|error| invalid(format!("run base_timeframe is invalid: {error}")))?;
    Ok((
        source,
        config,
        CompileContext {
            run_mode: document.compile_context.run_mode.clone(),
            trading_mode: document.compile_context.trading_mode,
        },
        RunContract {
            trading_mode: document.run.trading_mode,
            timerange_start_ms: document.run.timerange.start_ms,
            timerange_stop_ms: document.run.timerange.stop_ms,
            startup_candles: document.run.startup_candles,
            base_timeframe,
            source_row_shift: document.run.source_row_shift,
        },
    ))
}

fn validate_program_descriptors(document: &ManifestDocument) -> Result<(), NativeContractError> {
    for (role, program) in [
        ("indicator", &document.programs.indicator),
        ("signal", &document.programs.signal),
        ("tag", &document.programs.tag),
    ] {
        checked_digest(
            &format!("{role} program fingerprint"),
            program.fingerprint.clone(),
        )?;
        checked_artifact(role, &program.artifact)?;
    }
    Ok(())
}

fn validate_features(
    document: super::model::FeatureDocument,
) -> Result<FeatureRetention, NativeContractError> {
    validate_unique_names(&document.columns, "retained feature columns")?;
    let fingerprint = checked_digest("retained feature fingerprint", document.fingerprint)?;
    if fingerprint != retained_feature_fingerprint(&document.columns) {
        return Err(invalid(
            "retained feature fingerprint differs from the ordered column list",
        ));
    }
    Ok(FeatureRetention {
        columns: document.columns,
        fingerprint,
    })
}

fn validate_pairs(
    documents: Vec<super::model::PairDocument>,
    run: &RunContract,
) -> Result<(Vec<PairContract>, BTreeSet<String>), NativeContractError> {
    if documents.is_empty() {
        return Err(invalid("pairs must be non-empty"));
    }
    let mut names = BTreeSet::new();
    let mut pairs = Vec::with_capacity(documents.len());
    for document in documents {
        let identity = frame_identity(document.identity)?;
        if identity.timeframe != run.base_timeframe {
            return Err(invalid(format!(
                "pair {} base timeframe differs from run base_timeframe",
                identity.pair
            )));
        }
        if !names.insert(identity.pair.clone()) {
            return Err(invalid(format!(
                "duplicate pair contract {}",
                identity.pair
            )));
        }
        match document.metadata.get("pair") {
            Some(metadata_pair) if metadata_pair == &identity.pair => {}
            _ => {
                return Err(invalid(format!(
                    "metadata pair differs from pair contract {}",
                    identity.pair
                )));
            }
        }
        if document.metadata.keys().any(String::is_empty) {
            return Err(invalid(format!(
                "pair {} metadata contains an empty key",
                identity.pair
            )));
        }
        validate_optional_positive(document.precision.amount_step, "amount_step")?;
        validate_optional_positive(document.precision.price_step, "price_step")?;
        validate_optional_nonnegative(document.limits.minimum_stake, "minimum_stake")?;
        validate_optional_nonnegative(document.limits.minimum_amount, "minimum_amount")?;
        validate_optional_nonnegative(document.limits.minimum_cost, "minimum_cost")?;
        if run.trading_mode == TradingMode::Spot
            && (document.options.can_short || document.options.include_funding)
        {
            return Err(invalid(format!(
                "Spot pair {} cannot enable short or funding options",
                identity.pair
            )));
        }
        if run.trading_mode == TradingMode::Futures && !document.options.include_funding {
            return Err(invalid(format!(
                "Futures pair {} must enable funding input",
                identity.pair
            )));
        }
        let price_steps = validate_price_steps(document.price_steps)?;
        pairs.push(PairContract {
            identity,
            metadata: document.metadata,
            precision: PairPrecision {
                amount_step: document.precision.amount_step,
                price_step: document.precision.price_step,
            },
            limits: PairLimits {
                minimum_stake: document.limits.minimum_stake,
                minimum_amount: document.limits.minimum_amount,
                minimum_cost: document.limits.minimum_cost,
            },
            price_steps,
            options: PairOptions {
                can_short: document.options.can_short,
                include_funding: document.options.include_funding,
                use_exit_signal: document.options.use_exit_signal,
                include_previous_close: document.options.include_previous_close,
            },
        });
    }
    Ok((pairs, names))
}

fn validate_price_steps(
    documents: Vec<super::model::PriceStepDocument>,
) -> Result<Vec<HistoricPriceStep>, NativeContractError> {
    let mut prior = None;
    documents
        .into_iter()
        .map(|step| {
            if step.timestamp_ms < 0
                || !step.step.is_finite()
                || step.step <= 0.0
                || prior.is_some_and(|value| step.timestamp_ms <= value)
            {
                return Err(invalid("price_steps must be positive and strictly ordered"));
            }
            prior = Some(step.timestamp_ms);
            Ok(HistoricPriceStep {
                timestamp_ms: step.timestamp_ms,
                step: step.step,
            })
        })
        .collect()
}

fn validate_base_frames(
    documents: Vec<FrameDocument>,
    pairs: &[PairContract],
) -> Result<Vec<ValidatedFrame>, NativeContractError> {
    if documents.is_empty() {
        return Err(invalid("raw frames must be non-empty"));
    }
    let mut identities = BTreeSet::new();
    let frames = documents
        .into_iter()
        .map(|frame| validate_frame(frame, &mut identities))
        .collect::<Result<Vec<_>, _>>()?;
    for pair in pairs {
        if !identities.contains(&pair.identity) {
            return Err(invalid(format!(
                "pair {} has no exact base raw frame",
                pair.identity.pair
            )));
        }
    }
    Ok(frames)
}

fn validate_futures(
    documents: Vec<super::model::FuturesDocument>,
    mode: TradingMode,
    pair_names: &BTreeSet<String>,
) -> Result<Vec<ValidatedFutures>, NativeContractError> {
    if mode == TradingMode::Spot && !documents.is_empty() {
        return Err(invalid("Spot manifest cannot declare Futures sources"));
    }
    let mut seen = BTreeSet::new();
    documents
        .into_iter()
        .map(|document| {
            if !pair_names.contains(&document.pair) || !seen.insert(document.pair.clone()) {
                return Err(invalid(format!(
                    "invalid or duplicate Futures descriptor for {}",
                    document.pair
                )));
            }
            // Funding and mark commonly share one pair/timeframe but remain
            // separate roles and artifacts.
            let funding_rate = validate_frame(document.funding_rate, &mut BTreeSet::new())?;
            let mark = validate_frame(document.mark, &mut BTreeSet::new())?;
            if funding_rate.identity.pair != document.pair || mark.identity.pair != document.pair {
                return Err(invalid(format!(
                    "Futures descriptor identity differs from pair {}",
                    document.pair
                )));
            }
            Ok(ValidatedFutures {
                pair: document.pair,
                funding_rate,
                mark,
            })
        })
        .collect()
}

fn validate_frame(
    frame: FrameDocument,
    identities: &mut BTreeSet<FrameIdentity>,
) -> Result<ValidatedFrame, NativeContractError> {
    let identity = frame_identity(frame.identity)?;
    if !identities.insert(identity.clone()) {
        return Err(invalid(format!(
            "duplicate raw frame identity {} {}",
            identity.pair,
            identity.timeframe.as_str()
        )));
    }
    checked_artifact("raw frame", &frame.artifact)?;
    Ok(ValidatedFrame {
        identity,
        rows: frame.rows,
        artifact: frame.artifact,
    })
}

fn frame_identity(document: IdentityDocument) -> Result<FrameIdentity, NativeContractError> {
    let timeframe = Timeframe::parse(document.timeframe)
        .map_err(|error| invalid(format!("frame timeframe is invalid: {error}")))?;
    FrameIdentity::new(document.pair, timeframe)
        .map_err(|error| invalid(format!("frame identity is invalid: {error}")))
}

fn checked_artifact(role: &str, artifact: &ArtifactDocument) -> Result<(), NativeContractError> {
    checked_digest(&format!("{role} artifact SHA-256"), artifact.sha256.clone())?;
    if artifact.path.as_os_str().is_empty() || artifact.path.is_absolute() {
        return Err(invalid(format!(
            "{role} artifact path must be a non-empty relative path"
        )));
    }
    if artifact
        .path
        .components()
        .any(|component| !matches!(component, Component::Normal(_)))
    {
        return Err(invalid(format!(
            "{role} artifact path contains a non-contained component"
        )));
    }
    Ok(())
}

pub(super) fn validate_program_identity(
    document: &ValidatedDocument,
    indicator: &IndicatorProgram,
    signal: &MutationProgram,
    tag: &MutationProgram,
) -> Result<(), NativeContractError> {
    if signal.is_tag_program() || !tag.is_tag_program() {
        return Err(invalid("Signal and Tag program roles are swapped"));
    }
    for (role, source_sha, selected_class, fingerprint, expected_fingerprint) in [
        (
            "indicator",
            indicator.source.sha256.as_str(),
            indicator.selected_class.as_str(),
            indicator.fingerprint.as_str(),
            document.programs.indicator.fingerprint.as_str(),
        ),
        (
            "signal",
            signal.source.sha256.as_str(),
            signal.selected_class.as_str(),
            signal.fingerprint.as_str(),
            document.programs.signal.fingerprint.as_str(),
        ),
        (
            "tag",
            tag.source.sha256.as_str(),
            tag.selected_class.as_str(),
            tag.fingerprint.as_str(),
            document.programs.tag.fingerprint.as_str(),
        ),
    ] {
        if source_sha != document.source.strategy_sha256
            || selected_class != document.source.selected_class
            || fingerprint != expected_fingerprint
        {
            return Err(invalid(format!(
                "{role} program source, class, or fingerprint differs from the manifest"
            )));
        }
    }
    for (role, program) in [("signal", signal), ("tag", tag)] {
        if program.compile_context.run_mode != document.compile_context.run_mode
            || program.compile_context.trading_mode
                != document.compile_context.trading_mode.as_str()
        {
            return Err(invalid(format!(
                "{role} program compile context differs from the manifest"
            )));
        }
    }
    Ok(())
}

/// Canonical ordered-list fingerprint for retained feature columns.
#[must_use]
pub fn retained_feature_fingerprint(columns: &[String]) -> String {
    let mut hasher = Sha256::new();
    hasher.update(b"full-native-retained-features-v1\0");
    for column in columns {
        hasher.update(
            u64::try_from(column.len())
                .unwrap_or(u64::MAX)
                .to_be_bytes(),
        );
        hasher.update(column.as_bytes());
    }
    format!("{:x}", hasher.finalize())
}

fn validate_unique_names(values: &[String], label: &str) -> Result<(), NativeContractError> {
    let mut seen = BTreeSet::new();
    if values
        .iter()
        .any(|value| value.is_empty() || !seen.insert(value))
    {
        return Err(invalid(format!(
            "{label} contain an empty or duplicate value"
        )));
    }
    Ok(())
}

fn validate_optional_positive(value: Option<f64>, label: &str) -> Result<(), NativeContractError> {
    if value.is_some_and(|value| !value.is_finite() || value <= 0.0) {
        return Err(invalid(format!("{label} must be finite and positive")));
    }
    Ok(())
}

fn validate_optional_nonnegative(
    value: Option<f64>,
    label: &str,
) -> Result<(), NativeContractError> {
    if value.is_some_and(|value| !value.is_finite() || value < 0.0) {
        return Err(invalid(format!("{label} must be finite and non-negative")));
    }
    Ok(())
}

fn checked_name(label: &str, value: String) -> Result<String, NativeContractError> {
    if value.is_empty() {
        return Err(invalid(format!("{label} must be non-empty")));
    }
    Ok(value)
}

fn checked_digest(label: &str, value: String) -> Result<String, NativeContractError> {
    if value.len() != 64
        || !value
            .bytes()
            .all(|byte| byte.is_ascii_digit() || matches!(byte, b'a'..=b'f'))
    {
        return Err(invalid(format!(
            "{label} must be a lowercase SHA-256 digest"
        )));
    }
    Ok(value)
}

pub(super) fn invalid(message: impl Into<String>) -> NativeContractError {
    NativeContractError::Invalid(message.into())
}
