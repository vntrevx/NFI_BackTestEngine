import inspect,json
from freqtrade.optimize.backtesting import Backtesting
from freqtrade.strategy.interface import IStrategy
names=[(Backtesting,n) for n in vars(Backtesting) if 'exit' in n or n=='backtest_loop']+[(IStrategy,'should_exit')]
print(json.dumps({cls.__name__+'.'+name:inspect.getsource(getattr(cls,name)) for cls,name in names if inspect.isfunction(getattr(cls,name))},indent=2))
