# X8 recognition and compatibility

The strategy catalog discovers `NostalgiaForInfinityX8.py` in the workspace root,
`strategies/`, or `user_data/strategies/`. Class selection comes from the source AST.
The NFI trade-manager compiler is selected by managed exit methods, independent of
the class name or generation. Source hashing and structural validation still apply.

## Supplied source

The local file inspected on 2026-09-07 declares class `NostalgiaForInfinityX8` and
version `v18.0.2`. Its SHA-256 is
`e9482adcd6ca29eb28d03e4589d51b5d8096e96ed8b35b9bdbadc91d14007b84`.
This identifies the supplied file; it is not an upstream provenance certificate.

The supplied source now passes managed Native compatibility checks and executes
through the complete indicator, signal, order, adjustment, and exit pipeline.
The original-source isolated-Futures capture for BTC/USDT:USDT, 2022-04-01 through
2022-04-20, matches pinned Freqtrade on its one short-rebuy trade and full portfolio
state. The original-source ADA Spot capture for November 2025 also matches its legacy
grind entry and additional buy. A separate January 2023 BTC Spot run has no trades
and qualifies preparation only.

These are bounded qualification results, not certification of every configuration
or entry mode. Rare lifecycle branches need the focused scenarios below, and
callback custom data needs checks beyond the existing portfolio projection.
Existing X7 certificates do not certify X8.

## Explicit short top-coins routing

The compiler selects the routing layout from `custom_exit` calls. When the source
dispatches `short_exit_top_coins`, its wrapper is compiled independently with the
`short_tc` mode, source tag matcher, decision order, stop policy, and profit-target
state updates. The explicit route runs before scalp and accepts compound tags
according to the source's `any(...)` predicate. Sources with a dormant top-coins
wrapper retain the existing short-normal fallback behavior.

A final normal fallback is omitted only when its source predicate proves that it
cannot match any admitted short tag, including legacy grind tags. A changed
fallback predicate, an unbound tag alias, or a changed route order is rejected.
Rust requires the explicit layout to use the source program as its primary evaluator.

The [captured fixture](../benchmarks/fixtures/captured/explicit-short-top-coins-futures-r1/manifest.json)
uses the supplied X8 dispatch and wrapper in the archived managed strategy, with
volume-positive short entries tagged `641`. Its
[sealed recipe](../benchmarks/fixtures/captured/explicit-short-top-coins-futures-r1/inputs/auxiliary/001-recipe.json)
records the source hashes and probe transformations. This isolates the new callback; full-source qualification is recorded below.

Pinned Freqtrade 2026.5.1 and Native agree at zero tolerance on all three trades and
288 portfolio snapshots for BTC/USDT:USDT, 2022-04-01, using isolated Futures.
Two trades exit through `exit_short_tc_d_1_130` and `exit_short_tc_w_0_1`; the third
uses the end-of-run force exit.
The [verification evidence](../benchmarks/evidence/explicit-short-top-coins-2026-09-07.json)
records the fixture identity and matching state-stream hashes.

```bash
nfi-bte engine fixture \
  benchmarks/fixtures/captured/explicit-short-top-coins-futures-r1/manifest.json \
  --output-dir artifacts/short-top-coins-parity --level full
```

## Grind-entry diagnostic writes

`long_grind_entry_v3` records a diagnostic `_grind_entry_tag` before returning its
decision. X8 also writes that field in its short and v4 predicates. The compiler
now inspects every writer and reader in the selected class, including methods
outside the requested dependency closure, instead of allowing only two v3 writer
names. The decision programs retain the original conditions, first-match order,
and protection guards, including the late recovery conditions through `g27`.

Only ordinary assignments to this diagnostic field are eligible for ephemeral
VM writes. Reads must feed supported standalone logging or notification calls.
Semantic reads, augmented assignment, deletion, strategy methods named like
loggers, and logger results used in control flow remain unsupported. Assignment
expressions still execute in the scalar VM; their evaluation is not discarded.

The [captured grind-entry fixture](../benchmarks/fixtures/captured/x8-grind-entry-futures-r1/manifest.json)
copies the four donor predicates unchanged into the archived managed strategy.
Long entry signals use positive volume and `RSI_14 > 60`, so the grind callback
also evaluates deeper predicates when the entry signal is off. Pinned Freqtrade
2026.5.1 records two `grind_1_entry` orders, through `g0` and `g24`, during the
BTC/USDT:USDT isolated-Futures run from 2022-04-04 through 2022-04-11.
The [sealed recipe](../benchmarks/fixtures/captured/x8-grind-entry-futures-r1/inputs/auxiliary/001-recipe.json)
records the source identities and probe transformations.
The full Native replay matches all two trades, six orders, and 2,304 portfolio
snapshots at zero tolerance. The
[verification evidence](../benchmarks/evidence/x8-grind-entry-2026-09-07.json)
records the matching state hashes, unchanged donor-method hashes, and 27 passing
Python tests, including the existing X7 manager goldens.

```bash
nfi-bte engine fixture \
  benchmarks/fixtures/captured/x8-grind-entry-futures-r1/manifest.json \
  --output-dir artifacts/x8-grind-entry/parity-full --level full
```

## Active system-v4 lifecycle

The assembled manager now compiles the source's actual adjustment dispatcher.
It proves the system marker written on the first entry, specializes only the
reachable backtest/version branches, and validates the live trade marker before
executing the selected program. Long/short grind, rebuy, and reachable legacy
routes preserve source order and compound-tag predicates. The legacy BTC branch
calls the source's legacy grind callback directly, without inserting an extra
regular-rebuy call.

Grind and de-risk constants come from their selected source families. Missing v4
constants do not fall back to v3 values. Static conditional expressions evaluate
only the proven active arm, while maximum-stake comparisons remain dynamic.
The retained [adjustment contract](../benchmarks/fixtures/source-contracts/x8-system-v4/strategy.source)
and [dispatch contract](../benchmarks/fixtures/source-contracts/x8-system-v4/dispatch.source)
contain unchanged donor methods and source hashes. These are compiler inputs,
not official backtest captures. Existing X7 manager envelopes and fingerprints
remain unchanged.

Common stop helpers and the complete active cached-profit-target helper execute
source-compiled scalar programs. The bad-trade controller and compound-tag doom
rule execute before managed exit dispatch. Source defaults for missing daily
EMA/range columns, finite-value checks, directional adverse movement, trade age,
exit priority, and zero-decimal reason formatting are retained. Indicator mapping
literals can contribute computed columns through DataFrame expansion; optional
callback columns are retained only when present in the final indicator frame.

A [pinned official callback probe](../benchmarks/evidence/x8-system-v4-2026-09-07/official_exit_probe.py)
uses the exact retained methods with Freqtrade Trade/Order objects. Its
[337 captured cases](../benchmarks/evidence/x8-system-v4-2026-09-07/official-exit-cases-v2.json)
cover long/short and Spot/Futures common stops, all trailing-profit buckets,
protected targets, de-risk order tags and amount boundaries, controller switches,
missing/non-finite indicator values, rounding, and early-exit priority. Rust matches
all cases exactly. These are direct callback comparisons, with documented plumbing
stubs for the early-exit router, and supplement the portfolio captures.

The vectors exposed a JSON float parsing error at a trailing-profit boundary:
`0.013000000000000001` was rounded to a neighboring binary float before evaluation.
Rust JSON readers now enable exact float round trips. This transport correction
applies generally and does not introduce a strategy-specific numeric tolerance.

## Source configuration

The source constructor's safe-parameter list and update loops are structurally
validated before configuration is frozen. Nested `nfi_parameters` updates run
first; supported top-level legacy parameters take precedence afterward. Long/short
entry-signal dictionaries update only keys present in the source, with the same
nested-then-top-level precedence. Indicator, Signal, Tag, and callback compilers
consume the same effective values. The source bytes remain unchanged.

A [constructor probe](../benchmarks/evidence/x8-system-v4-2026-09-07/official_configuration_probe.py)
retains five [official results](../benchmarks/evidence/x8-system-v4-2026-09-07/official-configuration-cases.json)
for precedence, partial signal updates, ignored unknown keys, and disabled settings.
The source's base initializer and cache plumbing are stubbed in this direct probe;
the parameter-update method bodies are unchanged. A separate complete lifecycle
capture disables `system_v4_bad_trade_exit_enable` through `nfi_parameters`: daily
controller columns disappear and the position survives the stale-exit boundary,
as it does in official Freqtrade.

Standard Freqtrade resolver settings `position_adjustment_enable` and
`max_entry_position_adjustment` also override the class defaults after constructor
updates. Disabling adjustments retains the v4 exit contract while making adjustment
dispatch return immediately. A finite additional-entry limit is applied after the
callback, preserving its custom-data changes and permitting partial exits.

Strategy-only `custom_fee_open_rate` and `custom_fee_close_rate` overrides are
supported by a separate source-proven profit contract. Each accepts a finite number
in `[0, 1)` or `null`. Zero means zero decision fee; `null` independently falls back
to that side's exchange fee. Constructor precedence remains nested-then-top-level.
These rates affect source profit snapshots, exit confirmation, adjustment
thresholds, and rebuy decisions. Grind-cluster profit amounts and their maxima
retain the actual close fee, as the source requires. Exchange fills, wallet
accounting, funding, and Freqtrade's callback `current_profit` also keep actual fees.

The profit contract also preserves the source's summation order: all filled
entries, then all filled exits. Newly compiled source-system descriptors use this
contract even when both rates fall back to exchange fees. Historical descriptors
without the contract retain their original arithmetic. The compiler validates the
complete profit helper and each custom-fee selection before admitting the contract.
The legacy short-grind contract separately records its additive actual close fee
(`1 + fee_close`); system-v3/v4 cluster formulas retain their source expressions.

Unreviewed advanced parameters still fail before simulation. They require
additional contracts; qualification covers the configurations actually captured.

For example, this configuration uses 0.1% and 0.2% in strategy decisions:

```json
{
  "nfi_parameters": {
    "custom_fee_open_rate": 0.001,
    "custom_fee_close_rate": 0.002
  }
}
```

The order-filled compiler also preserves the new first-entry condition:
`nr_of_successful_entries == 1 and nr_of_successful_exits == 0`. Initial custom-data
writes stop after an exit; order-tag writes still run. The optional native policy
defaults to the previous behavior when absent. Static `and`/`or` system selectors
are evaluated with source short-circuit order.

The [captured order-state fixture](../benchmarks/fixtures/captured/order-filled-no-exits-futures-r1/manifest.json)
matches pinned Freqtrade on three trades and 288 portfolio snapshots at zero
tolerance. That existing full-state projection does not include arbitrary
`custom_data` fields. A separate
[official callback probe](../benchmarks/evidence/x8-order-state-2026-09-07/official_callback_probe.py)
therefore runs the exact donor method with official Freqtrade `Trade`/`Order`
objects. Its [retained result](../benchmarks/evidence/x8-order-state-2026-09-07/official-callback-probe.json)
confirms that initial state is preserved across two partial exits in both
directions while order-tag cluster resets still execute. This direct callback
probe supplements the portfolio capture; it is not an end-to-end v4 backtest.
The [verification record](../benchmarks/evidence/x8-order-state-2026-09-07/verification.json)
binds both results, the source contract, replay command, and regression checks.

## Supplemental callback-state parity

The original portfolio projection does not include arbitrary `custom_data` fields.
The [callback-state comparison](../benchmarks/evidence/x8-system-v4-2026-09-07/compare_callback_state.py)
therefore compares independently captured official callback state with Native's
open-position state at matching scheduler boundaries, using exact canonical values.
It verifies all version, de-risk, and five-cluster maximum fields emitted by the
captured scenarios; unexpected Native keys and ambiguous callback boundaries fail.
Terminal force-close fills are covered by the separate trade-surface comparison.

The [long result](../benchmarks/evidence/x8-system-v4-2026-09-07/long-custom-state/comparison.json)
contains 5,389 equal snapshots; the
[short result](../benchmarks/evidence/x8-system-v4-2026-09-07/short-custom-state/comparison.json)
contains 3,759. Both retain compressed official audit inputs, Native event inputs,
and matching projected streams. Together these establish exact callback-state
parity at 9,148 observed open-position boundaries, beyond portfolio-only equality.

## Captured qualification scope

All eleven captures pass zero-tolerance trade-surface and full portfolio-state replay:
27 trades, 152 orders, and 32,832 portfolio snapshots. The separate callback-state
streams above also verify custom data at 9,148 observed open-position boundaries.

The [verification record](../benchmarks/evidence/x8-system-v4-2026-09-07/verification.json)
binds the sealed fixtures, reference identity, replay receipts, source hashes,
callback-state evidence, and validation commands. Earlier incremental evidence is
preserved with its original scope; this record supersedes its integration blockers.

| Scenario | Source and exercised behavior |
| --- | --- |
| Original BTC isolated Futures, April 2022 | Unmodified supplied source; short rebuy entry, funding, force closure. |
| Original BTC Spot, January 2023 | Unmodified supplied source; indicators and signals, zero trades. |
| Original ADA Spot, November 2025 | Unmodified supplied source; legacy grind tag 120 and additional buy. |
| Long v4 isolated Futures | Synthetic entry only; grind levels, partial exits, de-risk levels 1/2, normal and 14-day stale exits. |
| Short v4 isolated Futures | Synthetic entry only; grind entry/exit, ordinary and trailing-profit exits. |
| Controller disabled | Same long scenario with a safe configuration override; position survives the stale-exit boundary. |
| Long v4 ADA Spot | Synthetic entry only; grind levels 1/2 and level-1 de-risk. |
| Adjustments disabled | Standard resolver override; all position adjustment routes return without changing the position, while normal exits remain active. |
| Additional entries limited | Standard resolver sets the additional-entry limit to zero; callback state still updates, but further buys are suppressed. |
| Long/short rebuy lifecycle | Synthetic entries and indicator guards, with explicitly reduced rebuy/de-risk thresholds; additional rebuy orders, level-3 de-risk, transfer to all five grind levels, and partial exits. |

The rebuy scenarios retain unchanged callback bodies. Their sealed recipes disclose
all indicator and constant changes used to trigger rare transitions. They qualify
those transitions, not their frequency under default entry conditions.

These captures cover Binance Spot and isolated Futures with the recorded fees,
funding, market precision, and data seals. They do not certify every exchange,
configuration, concurrent multi-pair portfolio, or liquidation path for X8.
Unreviewed advanced overrides remain explicit admission errors. Later strategy
revisions must pass source checks and receive their own
qualification evidence.

The rebuy transition tests exposed a minimum-stake boundary inherited from archived
programs: the active source preserves the corrected minimum when delegating into
grind, while dividing the maximum by leverage. New source-proven transfer metadata
retains those separate rules; archived programs keep their existing contracts.
The fixture runner also now distinguishes its half-open data-validation interval
from the Native simulation's inclusive final candle, preventing an extra candle
when retained data extends past the official run. Archived raw-proof replay
explicitly selects the historical producer's next-slot boundary so existing
published bytes remain reproducible. New fixture qualification uses the corrected
consumed-candle default; retained certificates and golden files are unchanged.

### Profit ordering and virtual-fee qualification

The [additional verification record](../benchmarks/evidence/x8-virtual-fees-2026-09-07/verification.json)
retains fresh full-state replays of the eleven cases above and five new official
captures. Long/short Futures rebuy scenarios cover nonzero and zero overrides,
level-3 de-risk, transfer to all five grind levels, and subsequent exits. ADA Spot
covers an open-only override in v4 and a close-only zero override in the unchanged
supplied source's legacy grind route. Each omitted side retains its exchange fee.
A separate tag-620 Futures capture exercises the legacy short-grind fee rule.
Its entry decision is bound to the helper actually called by the source, with
the four arguments and constant `True` flag validated. It does not inherit the
system-v3 entry helper through a default route alias.

All 16 cases match without tolerance: 38 trades, 244 orders, and 40,608 full
portfolio snapshots. The three Futures callback audits additionally match 5,758
open-position custom-data snapshots. Validation includes 98 focused Python tests,
the complete Rust workspace suite (434 passed, two ignored), and retained archive
and golden regressions. The full Python suite was not repeated for this increment;
its prior run is recorded in the baseline qualification above.

The [official profit probe](../benchmarks/evidence/x8-virtual-fees-2026-09-07/official_profit_probe.py)
retains 84 exact cases using the unchanged source helper with official Freqtrade
Trade/Order objects. Cases include both sides, Spot/Futures, funding, independent
fee fallbacks, zero rates, and partial exits followed by additional entries. All
four returned profit values are compared without tolerance. Separate callback
audits cover open-position grind maxima and de-risk markers, beyond the portfolio
state projection. The retained [source scope](../benchmarks/evidence/x8-virtual-fees-2026-09-07/source-scope.json)
discloses scenario changes and proves all other method ASTs match the supplied donor.

## Expanded configuration qualification (2026-09-08)

The source compiler now follows the advanced-parameter constructor path, inherited
strategy defaults, and the later Freqtrade resolver overrides. The retained
[verification record](../benchmarks/evidence/x8-settings-2026-09-08/verification.json)
separates constructor admission from runtime qualification: 600 static overrides
match the complete official constructor, while 21 resolver cases cover exchange
startup values, override precedence, container settings, and three expected
rejections of deprecated `sell_profit_only` configuration. Runtime initialization
objects and unproved constructor mutations still fail before simulation.

Native execution now applies `minimal_roi`, the trailing-stop controls,
`use_exit_signal`, `exit_profit_only`, `exit_profit_offset`, and
`ignore_roi_if_entry_signal` in the official order. Enabling `use_custom_stoploss`
on the supplied source invokes the inherited Freqtrade stoploss behavior, including
refresh after additional entries and partial exits. Leverage, actual fees,
cumulative funding, candle bounds, and exchange precision participate in the exit
calculations. Direct official probes provide 96 stop/trailing cases, 192 inherited
stop refresh cases, and 864 ROI cases. Some direct-helper combinations are invalid
as full configurations; the admission validator rejects those combinations.

The unchanged X8 `confirm_trade_exit` rejects ordinary and trailing stop orders in
the captured stop scenarios. These full runs qualify stop state and rejection
behavior; accepted close-rate arithmetic is additionally checked by the direct
helper probes. Custom exits remain independent of `exit_profit_only`, as in
Freqtrade. Long-held short positions exposed an additional-buy error: source short
cluster-distance negation had been applied twice. Source-proven programs now bind
the raw distance and retain the source's own sign operation.

Source-selected system-v3 and system-v3_2 adjustment families are also compiled
and have separate full lifecycle captures. Unavailable level-4 constants are only
represented as inert values when the source proves that level disabled. The
additional system-v3_1 rebuy action has subsequent qualification below.

Wallet allocation now respects `amend_last_stake_amount`,
`last_stake_amount_min_ratio`, and `available_capital`. Insufficient fixed stake
rejects before the stake callback unless amendment is enabled; a zero amended
proposal still reaches the callback. Capital limits include closed-trade profit
and exclude partial profit from positions still open. Currency-mapped
`dry_run_wallet` selects the configured stake currency for Spot and isolated
Futures. Spot state also retains pre-existing base-asset balances across entries
and closes. Cross-margin currency conversion remains unsupported. The already
supported unlimited stake and tradable-balance ratio have additional single-pair
and multi-pair captures. Simultaneously unlimited stake and trade slots are rejected.
Nonpositive or fractional trade-slot limits also fail before simulation until their
distinct source semantics are implemented.
An official wallet probe supplies 1,620 allocation and amendment boundary cases.

Full-state fixture authentication now validates the official order of open-trade
pairs followed by remaining configured pairs. It continues to reject missing,
duplicate, or reordered candle observations. Per-pair trade equality alone does
not establish portfolio parity.

The captured settings matrix retains unchanged donor callback bodies and class
constants. Entry and indicator transformations used to exercise branches are
recorded in each sealed recipe. Original captures, failed preliminary candidate
comparisons, and earlier qualification records remain preserved.

All 15 scenarios match at zero tolerance: 39 trades, 195 orders, and 25,632
portfolio snapshots. Seven supplemental callback audits also match 13,339 custom-data
snapshots. Validation includes 185 focused Python tests, a 17-test wallet/adapter
follow-up, and the full Rust workspace suite (446 passed, two ignored). Engine Ruff,
Basedpyright, Rustfmt, and Clippy pass. The unmodified donor is excluded from engine
Ruff; the full Python suite and cross-platform CI were not repeated for this increment.

Complete support is still open. Fractional trade-slot limits, position stacking,
and broader exchange/order-policy and routing-setting combinations need further work.
This matrix does not certify every exchange or every combination of settings.

## System-v3_1 additional entries

The [extra-action verification record](../benchmarks/evidence/x8-extra-actions-2026-09-08/verification.json)
qualifies the source-selected `system_v3_1` final `rebuy_entry` action in Futures
long, Futures short, and Spot. The compiler retains the original action order,
helper call with `is_derisk=True`, stake cap, minimum stake floor, and the
callback's terminating `return None` behavior.

Source-described order groups count active additional entries, exclude the initial
fill, and reset independently at the group's exits or global de-risk. Both system
families include these counts in their open-grind inputs. The compiler rejects
changed traversal, tag extraction, shadowing guards, increments, initialization,
mode assignments, and helper-call assumptions.

Four new captures contain 10 trades, 40 orders, and 6,912 portfolio snapshots,
including 20 `rebuy_entry` orders. The Spot case uses two stake values and four
thresholds, exercises the minimum stake floor, and stops at the stake-vector
length. A second long case retains enabled default grind settings; no numbered
grind orders occur in that bounded scenario.

Together with seven existing settings regressions, 29 trades, 145 orders, and
20,448 portfolio snapshots match at zero tolerance. Four supplemental callback
audits match 6,571 custom-data snapshots. The Spot supplemental audit is a separate
official run; its trade surface also matches. All donor callbacks and class
constants remain unchanged in the captures; signal and indicator transformations
are disclosed in their recipes.

Validation includes 161 focused Python tests, a final 20-test source-proof run,
and the full Rust workspace suite (447 passed, two ignored). Engine Ruff,
Basedpyright, Rustfmt, and Clippy pass. This does not claim a full Python suite run,
cross-platform CI, or complete support for the remaining settings listed above.

## Initialization parameters

The [initialization verification record](../benchmarks/evidence/x8-initialization-2026-09-08/verification.json)
qualifies advanced overrides of `is_futures_mode`, `can_short`,
`target_profit_cache`, and `hold_trades_cache` under the source's fresh-instance
initialization contract. Futures construction sets both mode flags to `True`
after the parameter loop, even when the configuration supplies `False`. Spot
admits `False`; forcing a different source mode remains rejected.

Cache overrides admit only `None`, preserving the source's initial cache creation.
Arbitrary cache objects, changed base construction, earlier state access,
unproven prefix calls, and additional mode writes fail before simulation.
Repeated configuration application by callback compilers preserves the same result.

Twelve pinned official constructor cases establish ten admitted outcomes and two
rejected source/exchange mode combinations. Three full Futures long, Futures
short, and Spot captures match nine trades, 78 orders, and 4,896 portfolio
snapshots at zero tolerance. Supplemental audits match 4,601 custom-data
snapshots. All captured callback bodies and class constants remain donor-identical.
The 84 affected Python tests, Basedpyright, and engine Ruff pass; this increment
uses the previously qualified Rust build without simulator changes.

## Zero and unlimited trade slots

The [slot verification record](../benchmarks/evidence/x8-slot-settings-2026-09-08/verification.json)
qualifies integer `max_open_trades=0` and `-1`. The scheduler's finite capacity
comes from the available pairs, while source callbacks retain zero or infinity.
This distinction also applies to scalp free-slot guards and compiled indicators.
The report preserves zero for a zero limit and pair count for an unlimited limit,
including runs with no trades.

With zero slots, `stake_amount="unlimited"` proposes zero and still invokes
`custom_stake_amount`. Both slots and stake cannot be unlimited simultaneously.
The official capture exposed an initial minimum-stake mismatch: the pinned
backtester uses a fixed `-5%` reserve independently of strategy stoploss. Native
now supplies that minimum to both stake callbacks and initial-order validation.
Additional-order validation keeps its zero reserve, and position-adjustment
callbacks keep their separate `-10%` reserve. Archived simulator inputs without
the new policy field retain their earlier behavior; the failed comparison and
its successful official exports are preserved in the evidence.

Five new captures cover two-pair fixed stakes, the zero/unlimited-stake lifecycle,
and scalp acceptance/rejection. Together with five existing regressions, the
same Native build matches 30 trades, 125 orders, and 21,600 portfolio snapshots
at zero tolerance. Three supplemental audits match 7,488 custom-data snapshots.
Continuous custom-state coverage is not claimed for the two scalp cases whose
trade-state callbacks are disabled or never invoked.

The pinned framework probe contains 456 cases; Native unit tests compare 96
wallet vectors and 180 integer-slot scalp decisions. Validation includes 141
focused Python tests and the full Rust workspace suite (451 passed, two ignored),
plus engine Ruff, Basedpyright, Rustfmt, and Clippy. A subsequent
[full Python verification](../benchmarks/evidence/x8-slot-settings-2026-09-08/full-python-verification.json)
passes 2,489 tests with 55 platform or environment exclusions. That source snapshot
precedes buyback integration; cross-platform CI remains outside this qualification.

Fractional limits remain rejected because the current exact report contract
requires an integer. `position_stacking=True` is explicitly rejected while the
engine allows one open trade per pair. The captures qualify the established
portfolio state projection; additional scheduler envelopes containing raw slot
values are not separately certified.

## Buyback entries and group de-risk

The [buyback verification record](../benchmarks/evidence/x8-buyback-settings-2026-09-08/verification.json)
qualifies enabled `system_v3_buyback_1_enable` with the source-selected
`system_v3_2` long lifecycle in Spot and isolated Futures. Buyback stake,
positive/negative distance thresholds, de-risk switch, and de-risk threshold
remain source-compiled settings. The fourth de-risk level is the source
prerequisite; its settings retain their selected system family.

Auxiliary order groups accumulate costs and amounts in reverse source order,
exclude the initial fill, and retain the weighted entry price. The group's own
exit anchor takes precedence over the level-four de-risk anchor. Partial-exit
tags append the active group's entry IDs in source order, and subsequent entries
use the reset group and new exit anchor. Group profit uses the actual trade close
fee; the Futures capture supplies a distinct virtual close fee to check that
separation. Changed price accumulation, initialization, fee source, anchor rules,
or ID rendering fail source validation.

The two active captures contain 42 buyback entries and 40 buyback de-risk orders,
with repeated exits and re-entry. A third capture enables
`system_v4_buyback_1_enable` and retains the source's fixed
`derisk_4_enable=False`; no buyback order occurs. That case qualifies the enabled
but inactive configuration, not an active v4 buyback lifecycle.

Together with six X7/X8 regressions, the same Native build matches 21 trades,
159 orders, and 26,208 portfolio snapshots at zero tolerance. Three supplemental
audits match 4,813 custom-data snapshots. Validation includes 92 focused Python
tests, 78 Python regressions, and the full Rust workspace suite (452 passed,
two ignored), plus engine Ruff, Basedpyright, Rustfmt, and Clippy. The earlier
2,489-test full Python result precedes this increment; a full Python rerun and
cross-platform CI are not claimed here.

## Exchange stake bounds, leverage ordering, and entry-candle exits

The execution settings qualification binds maximum amount and cost limits to the frozen
market snapshot. Both limits use the source contract-size conversion in Futures. Unbounded
limits must be explicit `null` values; missing, negative, nonfinite, or overflowing limits
fail closed in the new contract.

Native execution selects leverage from the wallet's proposed stake before calling
`custom_stake_amount`. The selected leverage determines entry minimum and maximum stakes.
Adjustment callbacks receive the unleveraged exchange maximum capped by wallet allocation,
while an additional order uses the trade's actual leverage and subtracts its existing stake
from the maximum position capacity. Callback wallet observations retain the actual
allocation separately from this order limit.

Six full-source configuration captures use unchanged frozen exchange metadata. Spot and
Futures initial orders reach the respective 900,000 ADA and 1,000 BTC amount limits. A
separate BTC Spot case caps pre-fee notional at 9,000,000 USDT and then applies amount
precision; fees are accounted for separately. The leverage case uses a 50,000 wallet
proposal and selects 50x, while the source custom stake reduces that proposal afterward. Two
Spot rebuy cases cover normal allocation and three actual remaining-capacity clamps. The
latter requests approximately 200,000 per additional entry and records the exchange's
remaining position capacity before amount precision is applied.

The high-leverage capture exposed a source doom exit on the entry candle. Native execution
now evaluates source exits immediately after entry under its new exit policy. The original
failed comparison receipt is preserved, and the independently sealed successful official
export is replayed with the corrected engine. Optional contract fields keep archived
simulator inputs on their previous behavior.

The official maximum-stake and wallet probes supply 384 and 648 boundary vectors. Some
helper-only vectors intentionally use combinations that the full strategy admission path
rejects; these are arithmetic probes, not additional lifecycle certificates. The sealed
recipes disclose the signal and indicator transformations; all donor callbacks and class
constants remain unchanged.

The [verification
record](../benchmarks/evidence/x8-execution-settings-2026-09-08/verification.json) covers
all 53 retained X8-named captures and one X7 regression on the same Native build: 136
trades, 743 orders, and 103,968 portfolio snapshots match at zero tolerance. Six
supplemental audits match 6,416 callback custom-data snapshots. Every replay records
`rust-full-native` with Python populate methods unexecuted.

Validation includes the complete Linux Python suite (2,535 passed, 55 skipped), with its
source file hashes unchanged throughout the run; 146 focused tests; and the full Rust
workspace (458 passed, 2 ignored). Engine Ruff, Basedpyright, Rustfmt, and Clippy also pass.
The earlier failed comparison and interrupted preliminary test runs are retained; their
results are not counted as successful validation.

This remains bounded backtest qualification. Every possible configuration combination,
cross-platform CI, broader exchange/order policies, position stacking, and cross-margin
collateral are not certified by these captures. Unsupported exact behavior continues to fail
closed.

## Inspect a local X8 revision

Run these commands from the workspace containing the supplied strategy:

```bash
nfi-bte strategy list --workspace . --show-unsupported
nfi-bte strategy check NostalgiaForInfinityX8.py --trading-mode spot \
  --output artifacts/x8-recognition/spot.json
nfi-bte strategy check NostalgiaForInfinityX8.py --trading-mode futures \
  --output artifacts/x8-recognition/futures.json
```

An unsupported compatibility check writes its report and exits with status 1.
Reports are specific to the supplied source and configuration. Add `--config`
when checking an actual run configuration.

Use the configuration intended for the actual run. A compatibility pass establishes
source admission; the captured matrix above states the independently verified scope.

## Run the supplied strategy

Use the guided run from this checkout after building its Native extension:

```bash
uv run nfi-bte run NostalgiaForInfinityX8.py
```

For existing data and an explicit configuration, replace the example input paths
and interval with the intended run inputs:

```bash
uv run nfi-bte run NostalgiaForInfinityX8.py \
  --class NostalgiaForInfinityX8 \
  --config user_data/config.json \
  --datadir user_data/data/binance \
  --timerange 20260101-20260108 \
  --output-dir artifacts/x8-research \
  --no-download --yes
```

The configuration selects Spot or isolated Futures. The run checks source admission
and data coverage before simulation, and preserves its effective inputs and reports.
The example interval is not an additional qualification claim.

## Bounded execution profiles

Limit worker scheduling and official comparison CPU use when creating a profile:

```bash
uv run --no-sync nfi-bte system tune \
  --output .nfi/laptop-profile.json \
  --memory-cap-gib 4 --cpu-process-limit 2
```

Pass `--profile .nfi/laptop-profile.json --workers 1` to `nfi-bte backtest`.
The guided project uses its saved `runtime.profile_path`; set that field to this
profile if using `nfi-bte run`. Existing profile files require `--force` for an
intentional replacement.

The profile CPU limit caps Native worker scheduling and the official backtest
container CPU quota. The memory cap controls Native resource planning and sets the
official container's hard memory limit. Normal official comparisons disable swap.
Native source compilation is not placed in a memory cgroup by this profile;
its memory planning limit is not an operating-system RSS enforcement guarantee.

Automatic profile recalibration after a host or CPU-affinity change now preserves
memory, CPU and spool settings. Invalid profiles fail without being overwritten.
Official comparisons inherit the completed run's recorded caps. Historical runs
without a recorded profile retain the existing managed Docker policy.

## Bounded original X8 qualification — 2026-09-08

[Verification](../benchmarks/evidence/x8-bounded-readiness-2026-09-08/verification.json)
adds two captures using the unchanged supplied X8 source, frozen ETH candles from
2025-04-05 through 2025-04-07, a fixed stake of 100 USDT, and one open-trade slot.
One case is Spot and the other is isolated Futures. Both naturally generate long
entries tagged `62` and `144`, followed by strategy exits; neither forces signals
or uses terminal force-close to manufacture trade coverage. The Futures case also
includes a nonzero funding payment.

| Case | Trades | Orders | Exact portfolio states | Exact callback snapshots |
| --- | ---: | ---: | ---: | ---: |
| Spot | 2 | 4 | 864 | 75 |
| Isolated Futures | 2 | 4 | 864 | 72 |
| Total | 4 | 8 | 1,728 | 147 |

Both captured fixtures pass full trade-surface and state-projection equality at
zero tolerance. Supplemental callback custom data also matches exactly. Native
execution compiles the source AST and runs in Rust without calling Python populate
methods. This adds four original-source trades to the earlier two; broader natural
signal coverage, especially short entries, remains limited.

All six successful phases ran sequentially with one Native worker, a work-local
two-CPU affinity, a sampled 4 GiB process-tree watchdog, and a ten-minute per-phase
timeout. They took 339.36 seconds combined. The largest observed host process tree
was 524.80 MiB. Official containers enforced two CPUs and 4 GiB without swap; their
memory peaks were 334.72 MiB and 337.08 MiB, with no OOM kills. Host measurements
exclude Docker daemon processes, and container measurements exclude helper
containers and unrelated user jobs.

The first Spot attempt exposed the profile-recalibration limit loss described
above. Its official capture was interrupted; preliminary artifacts are retained
and excluded from successful counts. Resource changes pass 123 focused Python
tests, Ruff, and Basedpyright. The earlier full-suite qualification remains a
historical record; full suites and cross-platform CI were not rerun for this bounded
resource-orchestration change. This three-day, single-pair scope does not establish
complete configuration support or production certification.

Authenticate the retained evidence and fixture inputs without another backtest:

```bash
uv run --no-sync python \
  benchmarks/evidence/x8-bounded-readiness-2026-09-08/verify.py
```
