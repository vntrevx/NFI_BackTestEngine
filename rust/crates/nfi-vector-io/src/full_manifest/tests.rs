use std::fs::{self, File};
use std::path::{Path, PathBuf};

use arrow2::array::{Array, PrimitiveArray};
use arrow2::chunk::Chunk;
use arrow2::datatypes::{DataType, Field, Schema, TimeUnit};
use arrow2::io::ipc::write::{FileWriter, WriteOptions};
use nfi_vector_core::alignment::{FrameIdentity, SourceLocation, Timeframe};
use serde_json::{json, Value};
use sha2::{Digest, Sha256};

use super::*;

const INDICATOR: &str =
    include_str!("../../../../../benchmarks/reference/vector-shadow/indicator-program.json");
const SIGNAL: &str =
    include_str!("../../../../../benchmarks/reference/vector-shadow/signal-program.json");
const TAG: &str =
    include_str!("../../../../../benchmarks/reference/vector-shadow/tag-program.json");
const STRATEGY_SHA: &str = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";
const CLASS_NAME: &str = "NativeManifestContract";

struct Fixture {
    temporary: tempfile::TempDir,
    path: PathBuf,
    document: Value,
}

impl Fixture {
    fn write_manifest(&self) {
        fs::write(
            &self.path,
            serde_json::to_vec(&self.document).expect("manifest JSON"),
        )
        .expect("write manifest");
    }
}

fn fixture(mode: TradingMode) -> Fixture {
    let temporary = tempfile::tempdir().expect("temporary");
    let root = temporary.path();
    let indicator = write_program(root, "indicator.json", INDICATOR, mode);
    let signal = write_program(root, "signal.json", SIGNAL, mode);
    let tag = write_program(root, "tag.json", TAG, mode);
    let frame_path = root.join("data/BTC_USDT-5m.feather");
    write_ohlcv(&frame_path, 100);
    let features = vec!["delta".to_owned()];
    let config = json!({
        "starting_balance": 1_000.0,
        "max_open_trades": 2,
        "stake_amount": 100.0,
        "fee_rate": 0.001,
        "stoploss_ratio": -0.2,
        "amount_step": 0.001,
        "price_step": 0.01,
        "is_futures": mode == TradingMode::Futures
    });
    let config_sha = format!(
        "{:x}",
        Sha256::digest(serde_json::to_vec(&config).expect("config identity"))
    );
    let document = json!({
        "schema_version": FULL_NATIVE_VECTOR_MANIFEST_VERSION,
        "source": {
            "strategy_sha256": STRATEGY_SHA,
            "config_sha256": config_sha,
            "compiler_source_fingerprint":
                "cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc",
            "selected_class": CLASS_NAME
        },
        "config": config,
        "compile_context": {"run_mode": "backtest", "trading_mode": mode.as_str()},
        "programs": {"indicator": indicator, "signal": signal, "tag": tag},
        "run": {
            "trading_mode": mode.as_str(),
            "timerange": {"start_ms": 0, "stop_ms": 1_000},
            "startup_candles": 17,
            "base_timeframe": "5m",
            "source_row_shift": 3
        },
        "retained_features": {
            "columns": features,
            "fingerprint": retained_feature_fingerprint(&features)
        },
        "pairs": [{
            "identity": {"pair": "BTC/USDT", "timeframe": "5m"},
            "metadata": {"pair": "BTC/USDT", "market": "test"},
            "precision": {"amount_step": 0.001, "price_step": 0.01},
            "limits": {
                "minimum_stake": 10.0,
                "minimum_amount": 0.001,
                "minimum_cost": 5.0
            },
            "price_steps": [
                {"timestamp_ms": 0, "step": 0.01},
                {"timestamp_ms": 500, "step": 0.1}
            ],
            "options": {
                "can_short": mode == TradingMode::Futures,
                "include_funding": mode == TradingMode::Futures,
                "use_exit_signal": true,
                "include_previous_close": true
            }
        }],
        "frames": [{
            "identity": {"pair": "BTC/USDT", "timeframe": "5m"},
            "rows": 1,
            "artifact": artifact(root, &frame_path)
        }],
        "futures": null
    });
    let path = root.join("manifest.json");
    let fixture = Fixture {
        temporary,
        path,
        document,
    };
    fixture.write_manifest();
    fixture
}

fn write_program(root: &Path, name: &str, encoded: &str, mode: TradingMode) -> Value {
    let mut program: Value = serde_json::from_str(encoded).expect("reference program");
    program["source"]["path"] = json!("strategy.py");
    program["source"]["sha256"] = json!(STRATEGY_SHA);
    program["selected_class"] = json!(CLASS_NAME);
    if program.get("compile_context").is_some() {
        program["compile_context"] = json!({"run_mode": "backtest", "trading_mode": mode.as_str()});
    }
    reseal_program(&mut program);
    let path = root.join(name);
    fs::write(&path, serde_json::to_vec(&program).expect("program JSON")).expect("write program");
    json!({
        "artifact": artifact(root, &path),
        "fingerprint": program["fingerprint"]
    })
}

fn reseal_program(program: &mut Value) {
    let mut identity = program.clone();
    let object = identity.as_object_mut().expect("program object");
    object.remove("fingerprint");
    object["source"]
        .as_object_mut()
        .expect("source object")
        .remove("path");
    program["fingerprint"] = json!(format!(
        "{:x}",
        Sha256::digest(serde_json::to_vec(&identity).expect("program identity"))
    ));
}

fn artifact(root: &Path, path: &Path) -> Value {
    json!({
        "path": path.strip_prefix(root).expect("contained path"),
        "sha256": format!("{:x}", Sha256::digest(fs::read(path).expect("artifact bytes")))
    })
}

fn reseal_config(document: &mut Value) {
    document["source"]["config_sha256"] = json!(format!(
        "{:x}",
        Sha256::digest(serde_json::to_vec(&document["config"]).expect("embedded config identity"))
    ));
}

fn write_ohlcv(path: &Path, timestamp_ms: i64) {
    fs::create_dir_all(path.parent().expect("parent")).expect("data directory");
    let fields = [Field::new(
        "date",
        DataType::Timestamp(TimeUnit::Millisecond, Some("UTC".to_owned())),
        false,
    )]
    .into_iter()
    .chain(
        ["open", "high", "low", "close", "volume"]
            .map(|name| Field::new(name, DataType::Float64, false)),
    )
    .collect::<Vec<_>>();
    let mut arrays: Vec<Box<dyn Array>> =
        vec![Box::new(PrimitiveArray::from_vec(vec![timestamp_ms]).to(
            DataType::Timestamp(TimeUnit::Millisecond, Some("UTC".to_owned())),
        ))];
    arrays.extend((0..5).map(|index| {
        Box::new(PrimitiveArray::from_vec(vec![f64::from(index + 1)])) as Box<dyn Array>
    }));
    let mut writer = FileWriter::try_new(
        File::create(path).expect("Feather file"),
        Schema::from(fields),
        None,
        WriteOptions { compression: None },
    )
    .expect("Feather writer");
    writer
        .write(&Chunk::new(arrays), None)
        .expect("Feather batch");
    writer.finish().expect("finish Feather");
}

#[test]
fn loads_strict_spot_contract_and_dynamic_runtime_fields() {
    let fixture = fixture(TradingMode::Spot);
    let loaded = load_full_native_vector_manifest(&fixture.path).expect("complete bundle");
    assert_eq!(loaded.run.source_row_shift, 3);
    assert_eq!(loaded.run.startup_candles, 17);
    assert_eq!(loaded.retained_features.columns, ["delta"]);
    assert_eq!(loaded.pairs[0].metadata["market"], "test");
    assert_eq!(loaded.pairs[0].price_steps.len(), 2);
    assert!(loaded.futures.is_empty());
    let identity = FrameIdentity::new("BTC/USDT", Timeframe::parse("5m").expect("timeframe"))
        .expect("identity");
    assert_eq!(
        loaded
            .frames
            .lookup(&identity, &SourceLocation::new("test", "test.rs", 1, 0),)
            .expect("base frame")
            .timestamps_ms,
        [100]
    );
}

#[test]
fn rejects_unknown_fields_paths_digests_and_duplicates() {
    let mut unknown = fixture(TradingMode::Spot);
    unknown.document["unknown"] = json!(true);
    unknown.write_manifest();
    assert!(load_full_native_vector_manifest(&unknown.path)
        .expect_err("unknown field")
        .to_string()
        .contains("unknown field"));

    let mut escaped = fixture(TradingMode::Spot);
    escaped.document["programs"]["indicator"]["artifact"]["path"] = json!("../x.json");
    escaped.write_manifest();
    assert!(load_full_native_vector_manifest(&escaped.path)
        .expect_err("parent path")
        .to_string()
        .contains("non-contained component"));

    let mut digest = fixture(TradingMode::Spot);
    digest.document["frames"][0]["artifact"]["sha256"] = json!("0".repeat(64));
    digest.write_manifest();
    assert!(matches!(
        load_full_native_vector_manifest(&digest.path),
        Err(NativeContractError::ArtifactDigest { .. })
    ));

    let mut duplicate = fixture(TradingMode::Spot);
    let second = duplicate.temporary.path().join("data/second.feather");
    write_ohlcv(&second, 200);
    let mut frame = duplicate.document["frames"][0].clone();
    frame["artifact"] = artifact(duplicate.temporary.path(), &second);
    duplicate.document["frames"]
        .as_array_mut()
        .expect("frames")
        .push(frame);
    duplicate.write_manifest();
    assert!(load_full_native_vector_manifest(&duplicate.path)
        .expect_err("duplicate identity")
        .to_string()
        .contains("duplicate raw frame identity"));
}

#[test]
fn rejects_context_and_feature_drift_before_raw_decode() {
    let mut context = fixture(TradingMode::Spot);
    let raw = context.temporary.path().join("data/BTC_USDT-5m.feather");
    fs::write(&raw, b"not Feather").expect("corrupt raw frame");
    context.document["frames"][0]["artifact"] = artifact(context.temporary.path(), &raw);
    context.document["compile_context"]["trading_mode"] = json!("futures");
    context.document["run"]["trading_mode"] = json!("futures");
    context.document["config"]["is_futures"] = json!(true);
    context.document["pairs"][0]["options"]["can_short"] = json!(true);
    context.document["pairs"][0]["options"]["include_funding"] = json!(true);
    reseal_config(&mut context.document);
    context.write_manifest();
    let error = load_full_native_vector_manifest(&context.path)
        .expect_err("compile context must fail before Arrow");
    assert!(error.to_string().contains("compile context differs"));
    assert!(!error.to_string().contains("cannot decode"));

    let mut features = fixture(TradingMode::Spot);
    features.document["retained_features"]["columns"] = json!(["open", "open"]);
    features.write_manifest();
    assert!(load_full_native_vector_manifest(&features.path)
        .expect_err("duplicate feature")
        .to_string()
        .contains("empty or duplicate"));
}

#[test]
fn loads_optional_futures_funding_and_mark_descriptors() {
    let mut fixture = fixture(TradingMode::Futures);
    let root = fixture.temporary.path();
    let funding = root.join("data/BTC_USDT-1h-funding.feather");
    let mark = root.join("data/BTC_USDT-1h-mark.feather");
    write_ohlcv(&funding, 300);
    write_ohlcv(&mark, 300);
    fixture.document["futures"] = json!([{
        "pair": "BTC/USDT",
        "funding_rate": {
            "identity": {"pair": "BTC/USDT", "timeframe": "1h"},
            "rows": 1,
            "artifact": artifact(root, &funding)
        },
        "mark": {
            "identity": {"pair": "BTC/USDT", "timeframe": "1h"},
            "rows": 1,
            "artifact": artifact(root, &mark)
        }
    }]);
    fixture.write_manifest();
    let loaded = load_full_native_vector_manifest(&fixture.path).expect("Futures bundle");
    assert_eq!(loaded.futures.len(), 1);
    assert_eq!(loaded.futures[0].pair, "BTC/USDT");
    assert_eq!(loaded.futures[0].funding_rate.timestamps_ms, [300]);
    assert_eq!(loaded.futures[0].mark.timestamps_ms, [300]);
}
