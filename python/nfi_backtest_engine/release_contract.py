"""Versioned release-mode contracts shared by input sealing and certification.

Release-critical mode rules belong here instead of being repeated as scattered
``if trading_mode == ...`` branches.  Strategy behavior is still compiled from
the supplied source; these contracts describe only the exchange/data/evidence
boundary a public certificate is allowed to claim.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from .errors import SpecValidationError

SPOT_RELEASE_CONTRACT_ID = "binance-spot"
FUTURES_RELEASE_CONTRACT_ID = "binance-usdtm-isolated"

ALL_PROTECTION_METHODS = frozenset(
    {
        "CooldownPeriod",
        "StoplossGuard",
        "MaxDrawdown",
        "LowProfitPairs",
    }
)
SPOT_PROTECTION_METHODS = ALL_PROTECTION_METHODS - {"StoplossGuard"}


@dataclass(frozen=True, slots=True)
class ProbeEvidenceContract:
    """Minimum official behavior one named probe must actually reach."""

    probe_kind: str
    required_entry_tags: frozenset[str] = frozenset()
    required_exit_reasons: frozenset[str] = frozenset()
    required_sides: frozenset[str] = frozenset()
    minimum_compound_tags: int = 0
    minimum_protection_methods: int = 0
    minimum_lock_count: int = 0
    minimum_distinct_leverages: int = 0
    minimum_funded_trades: int = 0

    def missing_from(self, observed: dict[str, Any]) -> list[str]:
        """Describe evidence absent from an aggregate of immutable probe outputs."""

        missing: list[str] = []
        for field, required in (
            ("entry_tags", self.required_entry_tags),
            ("exit_reasons", self.required_exit_reasons),
            ("sides", self.required_sides),
        ):
            absent = sorted(required - set(observed.get(field, [])))
            missing.extend(f"{field}:{value}" for value in absent)
        counts = (
            (
                "compound_tags",
                len(set(observed.get("compound_tags", []))),
                self.minimum_compound_tags,
            ),
            (
                "protection_methods",
                len(set(observed.get("protection_methods", []))),
                self.minimum_protection_methods,
            ),
            (
                "lock_count",
                observed.get("lock_count", 0),
                self.minimum_lock_count,
            ),
            (
                "distinct_leverages",
                len(set(observed.get("leverages", []))),
                self.minimum_distinct_leverages,
            ),
            (
                "funded_trades",
                observed.get("funded_trades", 0),
                self.minimum_funded_trades,
            ),
        )
        for name, actual, minimum in counts:
            if actual < minimum:
                missing.append(f"{name}:{actual}<{minimum}")
        return missing


@dataclass(frozen=True, slots=True)
class ReleaseModeContract:
    """Immutable product boundary for one independently certified trading mode."""

    contract_id: str
    trading_mode: str
    margin_mode: str | None
    exchange: str
    settlement_currency: str
    pair_pattern: re.Pattern[str]
    required_data_roles: tuple[str, ...]
    side_channel_intervals_ms: tuple[tuple[str, int], ...]
    probe_evidence: tuple[ProbeEvidenceContract, ...]
    required_protection_methods: frozenset[str]
    require_rejected_locked_entry: bool

    @property
    def required_probe_kinds(self) -> frozenset[str]:
        return frozenset(requirement.probe_kind for requirement in self.probe_evidence)

    def validate_pair(self, pair: str) -> None:
        if self.pair_pattern.fullmatch(pair) is None:
            raise SpecValidationError(
                f"{self.contract_id} release pair has an invalid form: {pair!r}"
            )

    def validate_pairs(self, pairs: list[str]) -> None:
        if len(pairs) != len(set(pairs)):
            raise SpecValidationError("release pairs must be unique")
        for pair in pairs:
            self.validate_pair(pair)

    def scope_fields(self) -> dict[str, Any]:
        return {
            "mode_contract": self.contract_id,
            "trading_mode": self.trading_mode,
            "margin_mode": self.margin_mode,
            "exchange": self.exchange,
            "settlement_currency": self.settlement_currency,
            "required_data_roles": list(self.required_data_roles),
        }


SPOT_RELEASE_CONTRACT = ReleaseModeContract(
    contract_id=SPOT_RELEASE_CONTRACT_ID,
    trading_mode="spot",
    margin_mode=None,
    exchange="binance",
    settlement_currency="USDT",
    pair_pattern=re.compile(r"^[^/:\s]+/USDT$"),
    required_data_roles=("candles",),
    side_channel_intervals_ms=(),
    probe_evidence=(
        ProbeEvidenceContract(
            probe_kind="tag-121",
            required_entry_tags=frozenset({"121"}),
        ),
        ProbeEvidenceContract(
            probe_kind="protections-locks",
            minimum_protection_methods=1,
            minimum_lock_count=1,
        ),
    ),
    required_protection_methods=SPOT_PROTECTION_METHODS,
    require_rejected_locked_entry=True,
)

FUTURES_RELEASE_CONTRACT = ReleaseModeContract(
    contract_id=FUTURES_RELEASE_CONTRACT_ID,
    trading_mode="futures",
    margin_mode="isolated",
    exchange="binance",
    settlement_currency="USDT",
    pair_pattern=re.compile(r"^[^/:\s]+/USDT:USDT$"),
    required_data_roles=("candles", "funding_rate", "mark"),
    side_channel_intervals_ms=(
        ("funding_rate", 8 * 60 * 60 * 1000),
        ("mark", 60 * 60 * 1000),
    ),
    probe_evidence=(
        ProbeEvidenceContract(
            probe_kind="tag-121",
            required_entry_tags=frozenset({"121"}),
        ),
        ProbeEvidenceContract(
            probe_kind="futures-lifecycle",
            required_sides=frozenset({"long", "short"}),
            minimum_funded_trades=1,
        ),
        ProbeEvidenceContract(
            probe_kind="protections-locks",
            minimum_protection_methods=1,
            minimum_lock_count=1,
        ),
        ProbeEvidenceContract(
            probe_kind="liquidation",
            required_exit_reasons=frozenset({"liquidation"}),
        ),
        ProbeEvidenceContract(
            probe_kind="compound-tags",
            minimum_compound_tags=1,
        ),
        ProbeEvidenceContract(
            probe_kind="variable-leverage",
            minimum_distinct_leverages=2,
        ),
    ),
    required_protection_methods=ALL_PROTECTION_METHODS,
    require_rejected_locked_entry=True,
)

_CONTRACTS_BY_ID = {
    contract.contract_id: contract
    for contract in (SPOT_RELEASE_CONTRACT, FUTURES_RELEASE_CONTRACT)
}


def release_contract_for_config(config: dict[str, Any]) -> ReleaseModeContract:
    """Resolve and validate the release contract represented by a Freqtrade config."""
    exchange = config.get("exchange")
    if not isinstance(exchange, dict):
        raise SpecValidationError("effective release config exchange must be an object")
    exchange_name = str(exchange.get("name", "")).lower()
    trading_mode = config.get("trading_mode", "spot")
    margin_mode = config.get("margin_mode")
    stake_currency = config.get("stake_currency")

    if trading_mode == "spot":
        contract = SPOT_RELEASE_CONTRACT
        if margin_mode not in {None, ""}:
            raise SpecValidationError("spot release config cannot select a margin mode")
    elif trading_mode == "futures":
        contract = FUTURES_RELEASE_CONTRACT
        if margin_mode != contract.margin_mode:
            raise SpecValidationError(
                "futures release config must use Binance USDT-M isolated margin"
            )
    else:
        raise SpecValidationError(
            "release certification supports spot or Binance isolated futures"
        )

    if exchange_name != contract.exchange:
        raise SpecValidationError(
            f"{contract.contract_id} release config requires exchange "
            f"{contract.exchange!r}"
        )
    if stake_currency != contract.settlement_currency:
        raise SpecValidationError(
            f"{contract.contract_id} release config requires stake currency "
            f"{contract.settlement_currency!r}"
        )
    pairs = exchange.get("pair_whitelist")
    if not isinstance(pairs, list) or not all(isinstance(pair, str) for pair in pairs):
        raise SpecValidationError(
            "effective release config pair_whitelist must contain strings"
        )
    contract.validate_pairs(pairs)
    return contract


def release_contract_for_scope(
    scope: dict[str, Any],
    *,
    legacy_spot: bool = False,
) -> ReleaseModeContract:
    """Resolve a sealed lock scope without guessing unsupported mode details."""
    if legacy_spot:
        if scope.get("trading_mode") != "spot":
            raise SpecValidationError("legacy Full X7 release lock must use spot mode")
        return SPOT_RELEASE_CONTRACT

    contract_id = scope.get("mode_contract")
    if not isinstance(contract_id, str):
        raise SpecValidationError("release input lock has no mode contract")
    contract = _CONTRACTS_BY_ID.get(contract_id)
    if contract is None:
        raise SpecValidationError("release input lock has an unsupported mode contract")
    expected = contract.scope_fields()
    actual = {key: scope.get(key) for key in expected}
    if actual != expected:
        raise SpecValidationError(
            f"release input lock contradicts {contract.contract_id}"
        )
    return contract


def data_role_for_path(
    path: str,
    *,
    pair: str,
    timeframes: list[str],
    contract: ReleaseModeContract,
) -> str | None:
    """Classify one sealed data path for a pair under a release contract."""
    stem = path.rsplit("/", 1)[-1].rsplit(".", 1)[0]
    normalized = pair.replace("/", "_").replace(":", "_")
    if stem == f"{normalized}-1h-funding_rate":
        return "funding_rate" if contract.trading_mode == "futures" else None
    if stem == f"{normalized}-1h-mark":
        return "mark" if contract.trading_mode == "futures" else None
    if contract.trading_mode == "futures":
        expected = {f"{normalized}-{timeframe}-futures" for timeframe in timeframes}
    else:
        expected = {
            value
            for timeframe in timeframes
            for value in (
                f"{normalized}-{timeframe}",
                f"{normalized}-{timeframe}-spot",
            )
        }
    return "candles" if stem in expected else None
