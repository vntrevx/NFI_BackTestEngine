//! Central IEEE-754 policy for deterministic vector kernels.

/// One quiet NaN payload used at every Rust vector-kernel boundary.
pub const CANONICAL_NAN_BITS: u64 = 0x7ff8_0000_0000_0000;

/// Arithmetic implemented by the M20-03 execution substrate.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum BinaryFloatOp {
    Add,
    Subtract,
    Multiply,
    Divide,
    Remainder,
}

/// IEEE comparisons with explicit NaN behavior.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum FloatComparison {
    Equal,
    NotEqual,
    Less,
    LessEqual,
    Greater,
    GreaterEqual,
}

/// Replace every NaN payload with the engine's single quiet NaN token.
///
/// Finite values, infinities, and the sign bit of zero remain byte-exact.
#[must_use]
pub fn canonicalize(value: f64) -> f64 {
    if value.is_nan() {
        f64::from_bits(CANONICAL_NAN_BITS)
    } else {
        value
    }
}

/// Canonicalize a nullable scalar without conflating Arrow null and NaN.
#[must_use]
pub fn canonicalize_optional(value: Option<f64>) -> Option<f64> {
    value.map(canonicalize)
}

/// Evaluate one ordered scalar operation and canonicalize its result.
///
/// Keeping this as a scalar match prevents future kernels from silently
/// reassociating operations or opting into a fast-math implementation.
#[must_use]
pub fn binary(lhs: f64, rhs: f64, op: BinaryFloatOp) -> f64 {
    canonicalize(match op {
        BinaryFloatOp::Add => lhs + rhs,
        BinaryFloatOp::Subtract => lhs - rhs,
        BinaryFloatOp::Multiply => lhs * rhs,
        BinaryFloatOp::Divide => lhs / rhs,
        BinaryFloatOp::Remainder => lhs % rhs,
    })
}

/// Compare with Rust/Python IEEE scalar semantics.
///
/// A NaN is unequal to every value, including itself; ordered comparisons
/// involving NaN are false. Arrow nulls are handled before this function.
#[must_use]
#[allow(clippy::float_cmp)] // Exact IEEE equality is the execution contract, not an approximation.
pub fn compare(lhs: f64, rhs: f64, comparison: FloatComparison) -> bool {
    match comparison {
        FloatComparison::Equal => lhs == rhs,
        FloatComparison::NotEqual => lhs != rhs,
        FloatComparison::Less => lhs < rhs,
        FloatComparison::LessEqual => lhs <= rhs,
        FloatComparison::Greater => lhs > rhs,
        FloatComparison::GreaterEqual => lhs >= rhs,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn every_nan_payload_becomes_one_quiet_nan() {
        let negative_nan = f64::from_bits(0xfff8_1234_5678_9abc);
        assert_eq!(canonicalize(f64::NAN).to_bits(), CANONICAL_NAN_BITS);
        assert_eq!(canonicalize(negative_nan).to_bits(), CANONICAL_NAN_BITS);
        assert_eq!(
            binary(f64::INFINITY, f64::INFINITY, BinaryFloatOp::Subtract).to_bits(),
            CANONICAL_NAN_BITS
        );
    }

    #[test]
    fn null_nan_signed_zero_and_infinity_remain_distinct() {
        assert_eq!(canonicalize_optional(None), None);
        assert!(canonicalize_optional(Some(f64::NAN))
            .expect("NaN value is not null")
            .is_nan());
        assert_eq!(canonicalize(-0.0).to_bits(), (-0.0_f64).to_bits());
        assert_eq!(
            canonicalize(f64::INFINITY).to_bits(),
            f64::INFINITY.to_bits()
        );
        assert_eq!(
            canonicalize(f64::NEG_INFINITY).to_bits(),
            f64::NEG_INFINITY.to_bits()
        );
    }

    #[test]
    fn comparisons_use_explicit_ieee_nan_rules() {
        for comparison in [
            FloatComparison::Equal,
            FloatComparison::Less,
            FloatComparison::LessEqual,
            FloatComparison::Greater,
            FloatComparison::GreaterEqual,
        ] {
            assert!(!compare(f64::NAN, 1.0, comparison));
        }
        assert!(compare(f64::NAN, f64::NAN, FloatComparison::NotEqual));
        assert!(compare(-0.0, 0.0, FloatComparison::Equal));
    }

    #[test]
    fn division_preserves_ieee_infinity_and_zero_sign() {
        assert_eq!(
            binary(1.0, 0.0, BinaryFloatOp::Divide).to_bits(),
            f64::INFINITY.to_bits()
        );
        assert_eq!(
            binary(1.0, -0.0, BinaryFloatOp::Divide).to_bits(),
            f64::NEG_INFINITY.to_bits()
        );
    }
}
