import json,math
from freqtrade.exchange import Exchange
from freqtrade.enums import TradingMode
from freqtrade.wallets import Wallets

def encode(value):
    return '+infinity' if math.isinf(value) else value

maxima=[]
for mode in ('spot','futures'):
 for amount in (None,10.0):
  for cost in (None,1000.0):
   for size in (1.0,0.01):
    for rate in (10.0,100.0,1000.0):
     for leverage in (1.0,2.0,5.0,25.0):
      for tiered in (False,True):
       exchange=Exchange.__new__(Exchange);exchange.close=lambda:None;exchange.trading_mode=TradingMode(mode)
       exchange._config={};exchange._markets={'AAA/USDT':{'contractSize':size,'limits':{'amount':{'min':None,'max':amount},'cost':{'min':None,'max':cost}}}}
       tiers=[{'minNotional':0.0,'maxNotional':1000.0,'maxLeverage':20.0},{'minNotional':1000.0,'maxNotional':5000.0,'maxLeverage':10.0},{'minNotional':5000.0,'maxNotional':25000.0,'maxLeverage':2.0}]
       exchange._leverage_tiers={'AAA/USDT':tiers} if tiered else {}
       value=exchange.get_max_pair_stake_amount('AAA/USDT',rate,leverage=leverage)
       maxima.append({'mode':mode,'amount_max':amount,'cost_max':cost,'contract_size':size,'rate':rate,'leverage':leverage,'tiered':tiered,'maximum':encode(value)})
wallets=[]
for available in (2.0,50.0,500.0):
 for minimum in (0.0,5.0,10.0):
  for maximum in (0.0,8.0,100.0):
   for existing in (None,0.0,20.0,100.0):
    for requested in (0.0,5.0,10.0,50.0,110.0,200.0):
     wallet=Wallets.__new__(Wallets);wallet.get_available_stake_amount=lambda:available;wallet._local_log=lambda *args,**kwargs:None
     value=wallet.validate_stake_amount('AAA/USDT',requested,minimum,maximum,existing)
     wallets.append({'available':available,'minimum':minimum,'maximum':maximum,'existing':existing,'requested':requested,'validated':value})
print(json.dumps({'maxima':maxima,'wallets':wallets},indent=2,allow_nan=False))
