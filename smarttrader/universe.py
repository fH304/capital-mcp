"""Bounded cross-market observations and deterministic candidate ranking.

These are candidate EPICs, not an assertion of availability on an account.
Every contract and executable quote is checked by Capital at runtime.
"""
import math
import re

SPREAD_ATR_LIMIT = .2


def spread_metrics(context):
    """Price-unit diagnostics; a zero ATR has no finite spread/ATR ratio."""
    bars=context['timeframes']['MINUTE_15']['candles']
    ranges=[max(b['h']-b['l'],abs(b['h']-p['c']),abs(b['l']-p['c']))
            for p,b in zip(bars,bars[1:])][-14:]
    atr=sum(ranges)/len(ranges)
    spread=context['ask']-context['bid']
    if not all(math.isfinite(v) for v in (atr,spread)) or spread<0:
        raise ValueError('Invalid spread observations')
    ratio=spread/atr if atr>0 else None
    reason=('zero_atr' if atr<=0 else
            'spread_above_limit' if ratio>SPREAD_ATR_LIMIT else 'within_limit')
    return dict(spread=spread,atr_15=atr,spread_to_atr=ratio,
                spread_atr_limit=SPREAD_ATR_LIMIT,spread_gate_reason=reason)


MARKETS = {
    'EURUSD': ('forex', ('EURUSD.FOREX',), ('eur/usd', 'eurusd', 'euro dollar')),
    'GBPUSD': ('forex', ('GBPUSD.FOREX',), ('gbp/usd', 'gbpusd', 'sterling dollar')),
    'AUDUSD': ('forex', ('AUDUSD.FOREX',), ('aud/usd', 'audusd', 'australian dollar')),
    'NZDUSD': ('forex', ('NZDUSD.FOREX',), ('nzd/usd', 'nzdusd', 'new zealand dollar')),
    'USDJPY': ('forex', ('USDJPY.FOREX',), ('usd/jpy', 'usdjpy', 'japanese yen')),
    'USDCHF': ('forex', ('USDCHF.FOREX',), ('usd/chf', 'usdchf', 'swiss franc')),
    'USDCAD': ('forex', ('USDCAD.FOREX',), ('usd/cad', 'usdcad', 'canadian dollar')),
    'US100': ('indices', ('NDX.INDX',), ('nasdaq 100', 'nasdaq-100')),
    'US500': ('indices', ('GSPC.INDX', 'SPX.INDX'), ('s&p 500', 's&p500')),
    'US30': ('indices', ('DJI.INDX',), ('dow jones', 'dow industrial')),
    'DE40': ('indices', ('GDAXI.INDX',), ('dax', 'german dax')),
    'UK100': ('indices', ('FTSE.INDX',), ('ftse 100', 'ftse-100')),
    'J225': ('indices', ('N225.INDX',), ('nikkei 225', 'nikkei-225')),
    'BTCUSD': ('crypto', ('BTC-USD.CC',), ('bitcoin', 'btc/usd')),
    'ETHUSD': ('crypto', ('ETH-USD.CC',), ('ethereum', 'ether price')),
    'SOLUSD': ('crypto', ('SOL-USD.CC',), ('solana',)),
    'XRPUSD': ('crypto', ('XRP-USD.CC',), ('xrp',)),
    'LTCUSD': ('crypto', ('LTC-USD.CC',), ('litecoin',)),
    'GOLD': ('metals', ('XAUUSD.FOREX',), ('gold price', 'gold prices', 'spot gold', 'gold futures')),
    'SILVER': ('metals', ('XAGUSD.FOREX',), ('silver price', 'silver prices', 'spot silver', 'silver futures')),
    'PLATINUM': ('metals', ('XPTUSD.FOREX',), ('platinum price', 'platinum prices', 'platinum futures')),
}


def relevant_articles(epic, articles, now):
    from .news import timestamp
    category, tags, terms = MARKETS[epic]
    result=[]
    for article in articles:
        if not 0 <= now-timestamp(article['published_utc']) <= 21600:
            continue
        title=article['title'].casefold()
        tagged=bool(set(tags).intersection(article.get('symbols', [])))
        topical=any(re.search(r'(?<!\w)'+re.escape(term)+r'(?!\w)', title) for term in terms)
        if tagged or topical:
            # Topic evidence is labelled explicitly; never replace a broker price
            # with an ETF, futures, index or provider symbol from an article.
            result.append(dict(article, relevance='symbol_tag' if tagged else 'headline_topic',
                               relevance_epic=epic, asset_class=category))
    return result


def rank(context):
    """Heuristic trend agreement after spread, not a return/profit probability."""
    frames=context['timeframes']
    bars=frames['MINUTE_15']['candles']
    diagnostics=spread_metrics(context)
    atr,spread=diagnostics['atr_15'],diagnostics['spread']
    moves=[(v['candles'][-1]['c']-v['candles'][-6]['c'])/v['candles'][-6]['c']
           for v in frames.values()]
    diagnostics['timeframes_agree']=all(x>0 for x in moves) or all(x<0 for x in moves)
    if diagnostics['spread_gate_reason']!='within_limit':
        return dict(score=0.0,reason='spread_or_flat_market',**diagnostics)
    if not diagnostics['timeframes_agree']:
        return dict(score=0.0,reason='timeframes_disagree',**diagnostics)
    normalized=abs(moves[0])/(atr/bars[-1]['c'])
    # Avoid ranking runaway volatility as unbounded opportunity.
    score=min(normalized, 5)/(1+spread/atr)
    if not math.isfinite(score):
        raise ValueError('Invalid candidate score')
    return dict(score=score,reason='trend_agreement_after_spread',**diagnostics)
