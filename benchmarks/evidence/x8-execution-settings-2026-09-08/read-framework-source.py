import inspect,json
from freqtrade.optimize.backtesting import Backtesting
from freqtrade.exchange import Exchange
from freqtrade.wallets import Wallets
names=[(Backtesting,'get_valid_entry_price_and_stake'),(Backtesting,'_check_adjust_trade_for_candle'),(Exchange,'get_max_pair_stake_amount'),(Exchange,'_get_max_notional_from_tiers'),(Wallets,'validate_stake_amount')]
print(json.dumps({cls.__name__+'.'+name:inspect.getsource(getattr(cls,name)) for cls,name in names},indent=2))
