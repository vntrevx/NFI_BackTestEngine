# Computed zero liquidation boundary

This bounded historical X7 regression reuses the variable-leverage fixture's
authenticated v17.4.435 source and candle/market inputs. The wallet is increased
and the three declared futures leverage settings are set to 1.0. The resulting
isolated long has a finite negative buffered liquidation price, which Official
Freqtrade clamps to numeric zero.

The fixture passes exact trade-surface and mandatory state-projection parity.
The projection contract does **not** include `liquidation_price`; the regression
test additionally compares the bound Official entry state with the Native entry
event, including liquidation price, stake and leverage. Missing/`None` values
cannot satisfy that comparison.

This is runtime regression evidence, not latest-X7 branch closure, a five-year
certificate, or performance evidence. It does not replace any release fixture.
