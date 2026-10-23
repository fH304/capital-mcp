"""Conservative contract-currency costs in the existing USD demo account.

Rates come from authenticated Capital quotes, never from the model. Buying the
contract currency costs ask on XXXUSD and 1/bid on USDXXX. The loss allowance
includes Capital's published retail 0.7% conversion mark-up; margin uses the
spot cost rate, as margin itself is not a realised conversion charge.
Sources: https://open-api.capital.com/
https://capital.com/en-ae/ways-to-trade/fees-and-charges
"""
import math
from decimal import Decimal

ROUTES = {'EUR': 'EURUSD', 'GBP': 'GBPUSD', 'AUD': 'AUDUSD',
          'NZD': 'NZDUSD', 'JPY': 'USDJPY', 'CHF': 'USDCHF', 'CAD': 'USDCAD'}
LOSS_MARKUP = Decimal('0.007')


def conversion(currency, quote, now):
    """Validate the FX route and return USD-per-contract-currency cost rates."""
    if type(now) not in (int,float) or not math.isfinite(now):
        raise ValueError('Invalid currency conversion time')
    if currency == 'USD':
        return dict(account_currency='USD', quote_currency='USD', fx_epic=None,
                    fx_rate=1.0, quote_to_account=1.0, conversion_time=now,
                    conversion_fee_buffer=0.0)
    epic = ROUTES.get(currency)
    if not epic or not isinstance(quote, dict):
        raise ValueError('Unsupported currency conversion')
    if (quote.get('epic') != epic or quote.get('instrument_type') != 'CURRENCIES'
            or quote.get('quote_currency') != epic[3:]):
        raise ValueError('Currency conversion contract mismatch')
    bid, ask, stamp = (quote.get(k) for k in ('bid', 'ask', 'quote_time'))
    if (any(type(v) not in (int, float) or not math.isfinite(v) for v in (bid, ask, stamp, now))
            or bid <= 0 or ask < bid or not 0 <= now-stamp <= 30):
        raise ValueError('Stale or invalid currency conversion quote')
    rate = Decimal(str(ask)) if epic[:3] == currency else 1/Decimal(str(bid))
    if not math.isfinite(float(rate*(1+LOSS_MARKUP))) or float(rate)<=0:
        raise ValueError('Invalid currency conversion rate')
    return dict(account_currency='USD', quote_currency=currency, fx_epic=epic,
                fx_rate=float(rate), quote_to_account=float(rate*(1+LOSS_MARKUP)),
                conversion_time=stamp, conversion_fee_buffer=float(LOSS_MARKUP))
