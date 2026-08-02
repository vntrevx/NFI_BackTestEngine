//! Derived X7 tag, route, and scalar-program dispatch indexes.

use std::collections::{BTreeSet, HashMap};

use crate::domain::{
    ManagedExitTagMatcher, ManagedExitTagOperator, NfiDispatchPlan, NfiInternedTagMatcher,
    NfiLongDispatchStep, NfiManagedDispatchStep, NfiManagedLongProfile, NfiProgramHandle, NfiTagId,
    NfiX7TradeManager, ScalarDecisionProgram,
};
use crate::portfolio::{OpenTrade, TradeSide};

use super::exit::{
    NFI_LONG_EXIT_PROGRAMS, NFI_LONG_EXIT_PROGRAMS_WITHOUT_DESCENDING, NFI_SHORT_EXIT_PROGRAMS,
};

pub(crate) struct NfiInternedEntryTags<'a> {
    pub words: &'a [String],
    pub ids: &'a [Option<NfiTagId>],
}

impl NfiX7TradeManager {
    pub(crate) fn runtime_dispatch(&self) -> Option<&NfiDispatchPlan> {
        self.dispatch_plan
            .get_or_init(|| build_dispatch_plan(self))
            .as_ref()
    }
}

impl NfiDispatchPlan {
    pub(crate) fn intern_trade_tags<'a>(&self, trade: &'a OpenTrade) -> NfiInternedEntryTags<'a> {
        let cache = trade.entry_tag_cache();
        let ids = cache.nfi_ids.get_or_init(|| {
            cache
                .words
                .iter()
                .map(|word| self.tag_ids.get(word).copied())
                .collect()
        });
        NfiInternedEntryTags {
            words: &cache.words,
            ids,
        }
    }

    pub(crate) fn intern_tag_ids(&self, entry_tag: &str) -> Vec<Option<NfiTagId>> {
        entry_tag
            .split_whitespace()
            .map(|word| self.tag_ids.get(word).copied())
            .collect()
    }

    pub(crate) fn program(&self, handle: NfiProgramHandle) -> Option<&ScalarDecisionProgram> {
        self.programs.get(handle)
    }
}

pub(crate) fn interned_matcher_matches(
    matcher: &NfiInternedTagMatcher,
    enter_tags: &NfiInternedEntryTags<'_>,
    side: TradeSide,
) -> bool {
    match matcher.operator {
        ManagedExitTagOperator::Any => enter_tags
            .ids
            .iter()
            .flatten()
            .any(|tag| matcher.entry_tags.contains(tag)),
        ManagedExitTagOperator::All => enter_tags
            .ids
            .iter()
            .all(|tag| tag.is_some_and(|tag| matcher.entry_tags.contains(&tag))),
        ManagedExitTagOperator::AnyOf => matcher
            .operands
            .iter()
            .any(|operand| interned_matcher_matches(operand, enter_tags, side)),
        ManagedExitTagOperator::AllOf => matcher
            .operands
            .iter()
            .all(|operand| interned_matcher_matches(operand, enter_tags, side)),
        ManagedExitTagOperator::Not => matcher
            .operands
            .first()
            .is_some_and(|operand| !interned_matcher_matches(operand, enter_tags, side)),
        ManagedExitTagOperator::IsShort => side == TradeSide::Short,
    }
}

pub(crate) fn any_in_scope(tags: &NfiInternedEntryTags<'_>, scope: &[NfiTagId]) -> bool {
    tags.ids.iter().flatten().any(|tag| scope.contains(tag))
}

pub(crate) fn all_in_scope(tags: &NfiInternedEntryTags<'_>, scope: &[NfiTagId]) -> bool {
    !tags.ids.is_empty()
        && tags
            .ids
            .iter()
            .all(|tag| tag.is_some_and(|tag| scope.contains(&tag)))
}

fn build_dispatch_plan(manager: &NfiX7TradeManager) -> Option<NfiDispatchPlan> {
    let mut tag_ids = HashMap::new();
    let mut next_tag_id = 0;
    let mut long_scope = Vec::new();
    let mut short_scope = Vec::new();
    let mut long_regular_scope = Vec::new();
    let mut short_regular_scope = Vec::new();

    let mut intern = |tag: &str| {
        *tag_ids.entry(tag.to_owned()).or_insert_with(|| {
            let id = next_tag_id;
            next_tag_id += 1;
            id
        })
    };
    for route in &manager.managed_long_routes {
        for tag in &route.entry_tags {
            let id = intern(tag);
            push_unique(&mut long_scope, id);
            if route.profile != NfiManagedLongProfile::Rebuy {
                push_unique(&mut long_regular_scope, id);
            }
        }
    }
    for route in manager.long_grind.iter().chain(&manager.long_btc) {
        for tag in &route.entry_tags {
            let id = intern(tag);
            push_unique(&mut long_scope, id);
        }
    }
    for route in &manager.managed_short_routes {
        for tag in &route.entry_tags {
            let id = intern(tag);
            push_unique(&mut short_scope, id);
        }
    }
    if let Some(adjustment) = &manager.short_position_adjustment {
        for tag in &adjustment.entry_tags {
            let id = intern(tag);
            push_unique(&mut short_regular_scope, id);
        }
    }

    let (programs, program_handles) = build_program_arena(manager)?;

    let long_rebuy_route = manager
        .managed_long_routes
        .iter()
        .position(|route| route.profile == NfiManagedLongProfile::Rebuy);
    let short_rebuy_route = manager
        .managed_short_routes
        .iter()
        .position(|route| route.key == "short_rebuy");

    let long_steps = build_long_steps(manager, &program_handles, &mut intern)?;
    let short_steps = build_short_steps(manager, &program_handles, &mut intern)?;

    let long_grind_tags = manager
        .long_grind
        .iter()
        .flat_map(|route| &route.entry_tags)
        .map(|tag| intern(tag))
        .collect();
    let long_btc_tags = manager
        .long_btc
        .iter()
        .flat_map(|route| &route.entry_tags)
        .map(|tag| intern(tag))
        .collect();

    Some(NfiDispatchPlan {
        tag_ids,
        long_scope,
        short_scope,
        long_regular_scope,
        short_regular_scope,
        long_steps,
        short_steps,
        long_rebuy_route,
        short_rebuy_route,
        long_grind_tags,
        long_btc_tags,
        programs,
    })
}

fn build_program_arena(
    manager: &NfiX7TradeManager,
) -> Option<(
    Vec<ScalarDecisionProgram>,
    HashMap<String, NfiProgramHandle>,
)> {
    let mut required = BTreeSet::new();
    for route in &manager.managed_long_routes {
        required.extend(legacy_long_programs(route.profile));
    }
    if !manager.managed_short_routes.is_empty() {
        required.extend(NFI_SHORT_EXIT_PROGRAMS);
    }
    for program in [
        manager.managed_exit_program.as_ref(),
        manager.managed_short_exit_program.as_ref(),
    ]
    .into_iter()
    .flatten()
    {
        for route in &program.routes {
            required.extend(route.decision_program_order.iter().map(String::as_str));
        }
    }

    let programs = required
        .iter()
        .map(|name| manager.programs.get(*name).cloned())
        .collect::<Option<Vec<_>>>()?;
    let handles = required
        .into_iter()
        .enumerate()
        .map(|(handle, name)| (name.to_owned(), handle))
        .collect();
    Some((programs, handles))
}

fn build_long_steps(
    manager: &NfiX7TradeManager,
    program_handles: &HashMap<String, NfiProgramHandle>,
    intern: &mut impl FnMut(&str) -> NfiTagId,
) -> Option<Vec<NfiLongDispatchStep>> {
    let mut steps = Vec::with_capacity(manager.route_order.len());
    for key in &manager.route_order {
        if let Some(route_index) = manager
            .managed_long_routes
            .iter()
            .position(|route| &route.key == key)
        {
            let source_route_index = manager
                .managed_exit_program
                .as_ref()
                .and_then(|program| program.routes.iter().position(|route| &route.id == key));
            let source_route = source_route_index
                .and_then(|index| manager.managed_exit_program.as_ref()?.routes.get(index));
            let source_matcher = source_route.map(|route| compile_matcher(&route.matcher, intern));
            let source_program_handles = source_route.map_or_else(
                || Some(Vec::new()),
                |route| handles(&route.decision_program_order, program_handles),
            )?;
            let legacy_program_handles = handles(
                legacy_long_programs(manager.managed_long_routes[route_index].profile),
                program_handles,
            )?;
            steps.push(NfiLongDispatchStep::Managed(NfiManagedDispatchStep {
                route_index,
                source_route_index,
                source_matcher,
                source_program_handles,
                legacy_program_handles,
            }));
        } else if manager.long_grind.is_some() && key == "long_grind" {
            steps.push(NfiLongDispatchStep::LongGrind);
        } else if manager.long_btc.is_some() && key == "long_btc" {
            steps.push(NfiLongDispatchStep::LongBtc);
        } else {
            return None;
        }
    }
    Some(steps)
}

fn build_short_steps(
    manager: &NfiX7TradeManager,
    program_handles: &HashMap<String, NfiProgramHandle>,
    intern: &mut impl FnMut(&str) -> NfiTagId,
) -> Option<Vec<NfiManagedDispatchStep>> {
    manager
        .short_route_order
        .iter()
        .map(|key| {
            let route_index = manager
                .managed_short_routes
                .iter()
                .position(|route| &route.key == key)?;
            let source_route_index = manager
                .managed_short_exit_program
                .as_ref()
                .and_then(|program| program.routes.iter().position(|route| &route.id == key));
            let source_route = source_route_index.and_then(|index| {
                manager
                    .managed_short_exit_program
                    .as_ref()?
                    .routes
                    .get(index)
            });
            Some(NfiManagedDispatchStep {
                route_index,
                source_route_index,
                source_matcher: source_route.map(|route| compile_matcher(&route.matcher, intern)),
                source_program_handles: source_route.map_or_else(
                    || Some(Vec::new()),
                    |route| handles(&route.decision_program_order, program_handles),
                )?,
                legacy_program_handles: handles(NFI_SHORT_EXIT_PROGRAMS, program_handles)?,
            })
        })
        .collect()
}

fn compile_matcher(
    matcher: &ManagedExitTagMatcher,
    intern: &mut impl FnMut(&str) -> NfiTagId,
) -> NfiInternedTagMatcher {
    NfiInternedTagMatcher {
        operator: matcher.operator,
        entry_tags: matcher.entry_tags.iter().map(|tag| intern(tag)).collect(),
        operands: matcher
            .operands
            .iter()
            .map(|operand| compile_matcher(operand, intern))
            .collect(),
    }
}

fn push_unique(values: &mut Vec<NfiTagId>, id: NfiTagId) {
    if !values.contains(&id) {
        values.push(id);
    }
}

fn handles<T: AsRef<str>>(
    names: &[T],
    program_handles: &HashMap<String, NfiProgramHandle>,
) -> Option<Vec<NfiProgramHandle>> {
    names
        .iter()
        .map(|name| program_handles.get(name.as_ref()).copied())
        .collect()
}

fn legacy_long_programs(profile: NfiManagedLongProfile) -> &'static [&'static str] {
    match profile {
        NfiManagedLongProfile::HighProfit => NFI_LONG_EXIT_PROGRAMS_WITHOUT_DESCENDING,
        _ => NFI_LONG_EXIT_PROGRAMS,
    }
}
