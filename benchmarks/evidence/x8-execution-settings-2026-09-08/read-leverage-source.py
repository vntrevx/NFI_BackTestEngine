import inspect,json
from freqtrade.exchange import Exchange
print(json.dumps({'get_max_leverage':inspect.getsource(Exchange.get_max_leverage)},indent=2))
