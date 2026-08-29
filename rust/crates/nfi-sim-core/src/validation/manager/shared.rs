//! Focused NFI trade-manager validation.

use super::BTreeSet;

pub(super) fn lists_are_unique_and_non_empty<const N: usize>(lists: [&Vec<String>; N]) -> bool {
    lists.iter().all(|values| {
        !values.is_empty()
            && values.iter().all(|value| !value.is_empty())
            && values.iter().collect::<BTreeSet<_>>().len() == values.len()
    })
}

pub(super) fn valid_sha256(value: &str) -> bool {
    value.len() == 64
        && value
            .bytes()
            .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
}
