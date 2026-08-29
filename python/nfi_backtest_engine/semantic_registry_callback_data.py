"""Immutable callback matrix and authenticated Freqtrade source identities."""

from __future__ import annotations

FREQTRADE_VERSION = "2026.5.1"
FREQTRADE_COMMIT = "6fa470939cc74bf0672e0e348a4d9b293072e43c"
FREQTRADE_METHOD_MERKLE = "54e428105e8b2108b76a5ae1fbdf4d948e1a27a853b1c0bcdee6f1ac5d1b0192"
SOURCE_METHODS = {
    ("freqtrade.optimize.backtesting.Backtesting", "_check_adjust_trade_for_candle"): (
        "900e3ec7f067fb67b56f190babed637712e26f5c08cb9e90ee5fd0df9849af8d"
    ),
    ("freqtrade.optimize.backtesting.Backtesting", "_check_trade_exit"): (
        "397f4c7be265c58fd9d596ae7421c8493379b9ef1ac5e20e175f05cd4643d1f8"
    ),
    ("freqtrade.optimize.backtesting.Backtesting", "_enter_trade"): (
        "e04325821a53f56ac22e1ee6a1ee13355f2eca7b88c193837364febf6c8bd952"
    ),
    ("freqtrade.optimize.backtesting.Backtesting", "_exit_trade"): (
        "ab4bfae7ee432cfc740b9a71a301ce16fe261ba5545a0d58676184a5f3c1cbb7"
    ),
    ("freqtrade.optimize.backtesting.Backtesting", "backtest_loop"): (
        "e6db4c1d0a841366eafd78d3ec2cc4d71d7a50b9331d45a5ebb9d3fd6558bc77"
    ),
    ("freqtrade.optimize.backtesting.Backtesting", "backtest_one_strategy"): (
        "d1e700943a7aa952fffe42ab68449d19525847d000077df377470d0aa47e024a"
    ),
}
ROW_FIELDS = frozenset(
    {
        "callback",
        "interaction",
        "rule",
        "source_owner",
        "source_method",
        "source_sha256",
        "boundary_row",
        "fixture_requirement",
    }
)
CALLBACK_INTERACTIONS = (
    "order",
    "predicate",
    "return",
    "rollback",
    "state-delta",
    "visibility",
)

# Each tuple is callback, observed Freqtrade owner, observed method, then the six
# interaction rules in CALLBACK_INTERACTIONS order. Rules deliberately include
# accepted classes and failure effects rather than merely naming the callback.
CALLBACK_SPECS = (
    (
        "custom_stoploss",
        "freqtrade.optimize.backtesting.Backtesting",
        "_check_trade_exit",
        (
            "after-liquidation-before-roi-and-exit-signal",
            "use-custom-stoploss-and-open-trade-and-candle-high-low-profit",
            "none|nan|inf=>keep-current;finite-float=>absolute-stop-price",
            "exception=>retain-prior-stoploss-and-continue-exit-evaluation",
            "finite-tightening=>stoploss-price-delta;invalid-or-loosening=>zero-delta",
            "updated-stoploss-visible-to-same-candle-exit-checks-and-next-loop",
        ),
    ),
    (
        "custom_exit",
        "freqtrade.optimize.backtesting.Backtesting",
        "_check_trade_exit",
        (
            "after-stoploss-and-roi-evaluation-before-exit-order-confirmation",
            "open-trade-and-not-exit-signal-suppressed-and-current-profit-context",
            "none|false=>no-exit;string=>exit-tag;true=>custom-exit-tag",
            "exception=>no-custom-exit-and-no-trade-or-wallet-delta",
            "return-only=>zero-custom-state-delta;explicit-trade-custom-data-write=>typed-delta",
            "entry-tag-trade-custom-data-and-analyzed-row-visible-at-callback-entry",
        ),
    ),
    (
        "confirm_trade_entry",
        "freqtrade.optimize.backtesting.Backtesting",
        "_enter_trade",
        (
            "after-price-stake-leverage-precision-and-order-id-reservation-before-fill",
            "entry-signal-and-pair-lock-and-open-trade-limit-and-wallet-capacity",
            "bool-true=>accept;bool-false=>reject;unknown-return-shape=>reject",
            "exception=>reject-entry-keep-reserved-order-id-and-rollback-wallet-trade-deltas",
            "accept=>order-pending-delta;reject=>zero-wallet-and-trade-delta",
            "entry-tag-side-rate-stake-leverage-and-precision-adjusted-amount-visible",
        ),
    ),
    (
        "confirm_trade_exit",
        "freqtrade.optimize.backtesting.Backtesting",
        "_exit_trade",
        (
            "after-exit-reason-rate-amount-and-fee-resolution-before-exit-fill",
            "open-trade-and-exit-reason-including-stoploss-roi-signal-or-liquidation",
            "bool-true=>accept;bool-false=>reject-except-liquidation;unknown-shape=>reject",
            "exception=>reject-nonliquidation-exit-and-preserve-trade-wallet-orders",
            "accept=>pending-exit-order-delta;reject=>zero-trade-wallet-delta",
            "exit-tag-entry-tag-reason-side-rate-profit-and-current-trade-state-visible",
        ),
    ),
    (
        "leverage",
        "freqtrade.optimize.backtesting.Backtesting",
        "_enter_trade",
        (
            "after-entry-rate-resolution-before-stake-sizing-and-amount-precision",
            "futures-mode-and-entry-side-and-market-max-leverage",
            "finite-float=>clamp-to-[1,max];none-nan-inf-or-nonnumeric=>fail-closed",
            "exception=>reject-entry-with-zero-wallet-trade-and-order-fill-delta",
            "selection-only=>zero-custom-state-delta",
            "pair-time-side-entry-tag-proposed-leverage-and-max-leverage-visible",
        ),
    ),
    (
        "custom_stake_amount",
        "freqtrade.optimize.backtesting.Backtesting",
        "_enter_trade",
        (
            "after-leverage-and-wallet-tradable-balance-before-min-max-and-precision",
            "entry-accepted-and-wallet-capacity-and-pair-min-max-stake",
            "finite-positive-float=>clamp-min-max;zero-or-none=>reject;unknown=>reject",
            "exception=>reject-entry-and-rollback-wallet-trade-order-fill-deltas",
            "selection-only=>zero-custom-state-delta",
            "pair-time-rate-proposed-stake-min-max-leverage-entry-tag-and-side-visible",
        ),
    ),
    (
        "adjust_trade_position",
        "freqtrade.optimize.backtesting.Backtesting",
        "_check_adjust_trade_for_candle",
        (
            "after-open-order-management-before-adjustment-fill-and-order-filled-callback",
            "position-adjustment-enabled-and-open-trade-and-no-blocking-open-order",
            "none|zero=>no-action;positive-float-or-(float,tag)=>entry;negative=>partial-exit",
            "exception-or-invalid-shape=>no-adjustment-and-rollback-wallet-trade-order-deltas",
            "accepted-fill=>stake-amount-order-wallet-and-trade-custom-data-deltas",
            "filled-orders-source-order-entry-tag-custom-data-profit-min-max-stake-visible",
        ),
    ),
    (
        "bot_loop_start",
        "freqtrade.optimize.backtesting.Backtesting",
        "backtest_loop",
        (
            "once-per-candle-before-pair-entry-exit-and-position-adjustment-dispatch",
            "startup-window-satisfied-and-current-backtest-candle-is-executable",
            "none-only;any-value-ignored-with-side-effects-retained",
            "exception=>abort-candle-and-rollback-unpublished-loop-custom-state",
            "normal-completion=>publish-loop-custom-state-delta-once",
            "published-loop-state-visible-to-all-pairs-at-same-timestamp-in-pair-order",
        ),
    ),
    (
        "order_filled",
        "freqtrade.optimize.backtesting.Backtesting",
        "_check_adjust_trade_for_candle",
        (
            "after-each-fill-commit-and-before-next-adjustment-replay-or-candle-phase",
            "newly-filled-order-and-source-ordered-fill-replay",
            "none-only;any-value-ignored-with-side-effects-retained",
            "exception=>fill-remains-committed-and-callback-custom-state-delta-rolled-back",
            "normal-completion=>trade-custom-data-delta-associated-with-filled-order",
            "committed-order-trade-wallet-and-prior-fill-custom-state-visible",
        ),
    ),
    (
        "loop_cadence_startup_lookback",
        "freqtrade.optimize.backtesting.Backtesting",
        "backtest_one_strategy",
        (
            "load-lookback-then-startup-trim-then-one-loop-per-detail-candle",
            "timerange-start-minus-startup-candles-and-required-data-availability",
            "executable-window=>ordered-candle-stream;insufficient-lookback=>no-execution",
            "preparation-failure=>no-loop-pair-wallet-trade-or-custom-state-publication",
            "each-completed-loop=>single-published-cadence-state-delta",
            "startup-context-readable-but-not-tradable;lookback-row-offset-visible-to-callbacks",
        ),
    ),
)
REQUIRED_CALLBACKS = tuple(spec[0] for spec in CALLBACK_SPECS)
