"""Connect Capital market observations to the OpenAI analysis input."""
import time
from .candles import RESOLUTIONS, MarketDataError, normalize_candles, validate_frames
from .analysis import validate_context
from .coordinator import fresh


class MarketCollector:
    def __init__(self,broker,clock=time.time):
        self.broker,self.clock=broker,clock

    def collect_market(self,epic):
        # Initial quote avoids historical requests for closed/restricted markets.
        quote_method=getattr(self.broker,'observation_quote',self.broker.market_quote)
        quote_method(epic)
        rules=getattr(self,'rules_only',False)
        if rules:
            histories={'MINUTE_15':self.broker.prices(epic,'MINUTE_15',40)}
            from .capital import CapitalError
            try:
                histories['HOUR']=self.broker.prices(epic,'HOUR',40)
            except CapitalError:
                pass  # The daily route can still proceed.
        else:
            histories={resolution:self.broker.prices(epic,resolution,40) for resolution in RESOLUTIONS}
        # Refresh executable quote AFTER all historical requests.
        quote=quote_method(epic)
        now=self.clock()
        frames={'MINUTE_15':normalize_candles(histories['MINUTE_15'],'MINUTE_15',now)}
        for resolution,rows in histories.items():
            if resolution=='MINUTE_15':
                continue
            try:
                frames[resolution]=normalize_candles(rows,resolution,now)
            except MarketDataError:
                if not rules or resolution!='HOUR':
                    raise
        validate_frames(frames,now,required=frames.keys() if rules else RESOLUTIONS)
        fresh(quote['quote_time'],now,30,'collected quote')
        # Reuse already-fetched executable sides for the read-only spread study.
        # They are kept outside the AI input, so its token/cost policy is unchanged.
        self.execution_candles=normalize_candles(histories['MINUTE_15'],'MINUTE_15',now,
                                                include_sides=True)['candles']
        return dict(epic=epic,bid=quote['bid'],ask=quote['ask'],quote_time=quote['quote_time'],
                     collected_at=now,timeframes=frames,
                     execution_supported=quote.get('execution_supported',True),
                     quote_currency=quote.get('quote_currency','USD'),
                     price_basis='candles are bid/ask midpoints; quote bid/ask are executable',
                     volume_basis='optional Capital lastTradedVolume; not asserted as global exchange volume')

    def attach_trend_daily(self,context):
        """Daily data is optional: H1 can execute if D1 is unavailable."""
        from .trend_following import daily_candles
        from .capital import CapitalError
        try:
            context['trend_daily']=daily_candles(self.broker.prices(context['epic'],'DAY',40),self.clock())
        except (CapitalError,MarketDataError) as error:
            context['trend_daily']=[]
            context['trend_daily_issue']=str(error)
        return context

    def collect(self,epic,articles):
        context=self.collect_market(epic)
        context['articles']=articles
        validate_context(context,self.clock())
        return context

    def analyze(self,epic,articles,client):
        context=self.collect(epic,articles)
        recommendation=client.analyze(context)
        # Provider latency may cross a freshness boundary even with fake clients.
        validate_context(context,self.clock())
        return context,recommendation


def main():
    """Read-only diagnostic: no OpenAI call and no order submission."""
    import argparse
    import json
    import os
    from datetime import datetime,timezone
    from .capital import CapitalDemo,CapitalError
    parser=argparse.ArgumentParser(description='Validate Capital demo market data only')
    parser.add_argument('--epic',default='GOLD')
    args=parser.parse_args()
    try:
        broker=CapitalDemo(key=os.environ.get('CAP_API_KEY',''),user=os.environ.get('CAP_IDENTIFIER',''),
                           password=os.environ.get('CAP_API_PASSWORD',''),account_id=os.environ.get('BOT_ACCOUNT_ID',''),
                           environment=os.environ.get('CAP_ENV','demo'),armed=False,
                           daily_controller=lambda equity,now:dict(stopped=True,remaining=0))
        broker.login()
        data=MarketCollector(broker).collect_market(args.epic)
        print(json.dumps(dict(event='market_data_ready',mode='demo',epic=data['epic'],
                              bid=data['bid'],ask=data['ask'],quote_time=data['quote_time'],
                              timeframes={k:dict(closed_bars=len(v['candles']),
                                                 last_closed_start_utc=datetime.fromtimestamp(v['candles'][-1]['t'],timezone.utc).isoformat())
                                          for k,v in data['timeframes'].items()})))
    except Exception as error:
        # Never print transport bodies, credentials, or the caller's environment.
        print(json.dumps(dict(event='market_data_unavailable',error_type=type(error).__name__)))
        raise SystemExit(1) from None


if __name__=='__main__':
    main()
