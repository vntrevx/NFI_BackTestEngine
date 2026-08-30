//! Schema-versioned legacy grind program orchestration.

use super::{
    legacy_clusters, legacy_policy, CompiledLegacyComparison, CompiledLegacyGrindSide,
    NfiLongGrindRoute,
};

pub(crate) fn valid_versioned_legacy_grind_program(
    schema_version: &str,
    route: &NfiLongGrindRoute,
) -> bool {
    let required_program_version = match schema_version {
        "0.25.0" => Some("grind-transition-program-v1"),
        "0.26.0" => Some("grind-transition-program-v2"),
        "0.27.0" | "0.28.0" | "0.29.0" | "0.30.0" | "0.31.0" => Some("grind-transition-program-v3"),
        _ => None,
    };
    let Some(program) = route.program.as_ref() else {
        return required_program_version.is_none();
    };
    if required_program_version.is_some_and(|required| program.schema_version != required) {
        return false;
    }
    let is_v1 = program.schema_version == "grind-transition-program-v1";
    let is_v2 = program.schema_version == "grind-transition-program-v2";
    let is_v3 = program.schema_version == "grind-transition-program-v3";
    if !is_v1 && !is_v2 && !is_v3 {
        return false;
    }
    let expected_comparison = match program.side {
        CompiledLegacyGrindSide::Long => CompiledLegacyComparison::LessThan,
        CompiledLegacyGrindSide::Short => CompiledLegacyComparison::GreaterThan,
    };
    legacy_policy::program_is_valid(schema_version, program, route)
        && legacy_clusters::clusters_are_valid(program, route, is_v1, expected_comparison)
        && legacy_policy::derisk_buyback_is_valid(program, route, is_v3)
}
