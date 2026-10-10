"""Read-only cost calibration and prospective next-bar paper observations.

No broker/API client, no mutations of entry policy, no inference calls. Price R
includes bid/ask once, excludes slippage, financing and currency conversion fees.
Independent paper candidates are not a backtest of the one-position portfolio.
"""
import json
import math
import sqlite3
import statistics
from collections import Counter
from pathlib import Path

from .candles import numeric, utc_stamp
from .universe import MARKETS, SPREAD_ATR_LIMIT, spread_metrics

THRESHOLDS=(.1,.2,.3,.5)  # sensitivity comparisons only; never execution settings
BAR_SECONDS=900
HORIZON_BARS=16
RETENTION_SECONDS=7*86400
COMPLETE={'stop','target','horizon'}


def candidate_costs(context,recommendation):
    action=recommendation['action']
    if action not in {'BUY','SELL'}:
        raise ValueError('Paper study requires BUY or SELL')
    bid,ask=context['bid'],context['ask']
    stop,target=recommendation['stop_level'],recommendation['target_level']
    for value in (bid,ask,stop,target):
        numeric(value)
    if ask<bid:
        raise ValueError('Crossed candidate quote')
    if action=='BUY':
        entry=ask
        risk,reward=ask-stop,target-ask
        valid=stop<bid<=ask<target
    else:
        entry=bid
        risk,reward=stop-bid,bid-target
        valid=target<bid<=ask<stop
    if not valid or risk<=0 or reward<=0:
        raise ValueError('Invalid candidate protection')
    spread=ask-bid
    # The executable entry side already includes spread. Do not subtract it again.
    return dict(action=action,entry_price=entry,stop_level=stop,target_level=target,
                spread=spread,risk_distance=risk,reward_distance=reward,
                reward_risk=reward/risk,spread_to_risk=spread/risk,
                spread_to_reward=spread/reward,
                quote_currency=context.get('quote_currency','USD'),
                cost_basis='executable_entry_sides_spread_included_once')


def paper_outcome(candidate,bars,now):
    """Use only full future bid/ask bars; never the partially elapsed entry bar.

    This tests a fixed next-bar-open paper entry, not the actual rejected fill.
    Both levels in one bar are ambiguous, not assigned to the profitable level.
    Missing bars censor the candidate rather than silently bridging a gap.
    """
    start=candidate['paper_start']
    duration=HORIZON_BARS*BAR_SECONDS
    by_time={b['t']:b for b in bars if b['t']>=start and b['t']+BAR_SECONDS<=now}
    if not by_time:
        return dict(status='missing_bars' if now>=start+duration else 'pending')
    if start not in by_time:
        return dict(status='missing_bars')
    action=candidate['action']
    side='bid' if action=='BUY' else 'ask'
    entry=by_time[start]['ask' if action=='BUY' else 'bid']['o']
    stop,target=candidate['stop_level'],candidate['target_level']
    risk=entry-stop if action=='BUY' else stop-entry
    reward=target-entry if action=='BUY' else entry-target
    first=by_time[start]
    valid=(stop<first['bid']['o']<=first['ask']['o']<target if action=='BUY' else
           target<first['bid']['o']<=first['ask']['o']<stop)
    if not valid or risk<=0 or reward<2*risk:
        return dict(status='invalid_next_open',paper_entry=entry)
    for index in range(HORIZON_BARS):
        stamp=start+index*BAR_SECONDS
        if stamp not in by_time:
            status='missing_bars' if now>=stamp+BAR_SECONDS else 'pending'
            return dict(status=status,paper_entry=entry)
        prices=by_time[stamp][side]
        stopped=prices['l']<=stop if action=='BUY' else prices['h']>=stop
        targeted=prices['h']>=target if action=='BUY' else prices['l']<=target
        stop_gap=prices['o']<=stop if action=='BUY' else prices['o']>=stop
        target_gap=prices['o']>=target if action=='BUY' else prices['o']<=target
        if stop_gap:
            status,exit_price='stop',prices['o']  # gap loss may exceed -1R
        elif target_gap:
            status,exit_price='target',target  # no assumed favourable gap fill
        elif stopped and targeted:
            return dict(status='ambiguous',paper_entry=entry,bar=stamp)
        elif stopped:
            status,exit_price='stop',stop
        elif targeted:
            status,exit_price='target',target
        elif index==HORIZON_BARS-1:
            status,exit_price='horizon',prices['c']
        else:
            continue
        pnl=exit_price-entry if action=='BUY' else entry-exit_price
        return dict(status=status,paper_entry=entry,paper_exit=exit_price,
                    price_R=pnl/risk,bar=stamp)
    return dict(status='pending',paper_entry=entry)


def ratio_summary(values):
    values=sorted(v for v in values if v is not None)
    return dict(samples=len(values),median=statistics.median(values) if values else None,
                p90=values[max(0,math.ceil(len(values)*.9)-1)] if values else None,
                maximum=max(values) if values else None)


def threshold_comparison(observations,candidates=()):
    result=[]
    for limit in THRESHOLDS:
        accepted=[c for c in candidates if c['metrics']['spread_to_atr'] is not None
                  and c['metrics']['spread_to_atr']<=limit and c['metrics'].get('timeframes_agree',False)]
        completed=[c['outcome']['price_R'] for c in accepted if c['outcome']['status'] in COMPLETE]
        result.append(dict(limit=limit,spread_passed=sum(o['spread_to_atr'] is not None
                           and o['spread_to_atr']<=limit for o in observations),
                           paper_candidates_accepted=len(accepted),paper_completed=len(completed),
                           mean_price_R=statistics.mean(completed) if completed else None,
                           paper_status_counts=dict(Counter(c['outcome']['status'] for c in accepted))))
    return result


class SpreadStudy:
    def __init__(self,path,account):
        self.account=account
        self.db=sqlite3.connect(path,timeout=10)
        self.db.execute('PRAGMA synchronous=FULL')
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS spread_observations(
                account TEXT,epic TEXT,candle REAL,observed_at REAL,payload TEXT,
                PRIMARY KEY(account,epic,candle));
            CREATE TABLE IF NOT EXISTS spread_candidates(
                account TEXT,epic TEXT,candle REAL,observed_at REAL,payload TEXT,outcome TEXT,
                PRIMARY KEY(account,epic,candle));
        ''')
        self.db.commit()

    def observe(self,context,scoring,now,bars=()):
        candle=context['timeframes']['MINUTE_15']['candles'][-1]['t']
        metrics={k:scoring[k] for k in ('spread','atr_15','spread_to_atr','spread_atr_limit',
                                      'spread_gate_reason','timeframes_agree')}
        metrics.update(quote_time=context['quote_time'],quote_currency=context.get('quote_currency','USD'))
        with self.db:
            self.db.execute('INSERT OR REPLACE INTO spread_observations VALUES(?,?,?,?,?)',
                            (self.account,context['epic'],candle,now,json.dumps(metrics,allow_nan=False)))
            cutoff=now-RETENTION_SECONDS
            for table in ('spread_observations','spread_candidates'):
                self.db.execute('DELETE FROM '+table+' WHERE account=? AND observed_at<?',(self.account,cutoff))
            if bars:
                rows=self.db.execute('SELECT candle,payload FROM spread_candidates '
                                     'WHERE account=? AND epic=? AND json_extract(outcome,"$.status")="pending"',
                                     (self.account,context['epic'])).fetchall()
                for saved_candle,payload in rows:
                    candidate=json.loads(payload)
                    outcome=paper_outcome(candidate,bars,now)
                    self.db.execute('UPDATE spread_candidates SET outcome=? '
                                    'WHERE account=? AND epic=? AND candle=?',
                                    (json.dumps(outcome,allow_nan=False),self.account,context['epic'],saved_candle))
        return metrics

    def candidate(self,context,scoring,recommendation,now):
        costs=candidate_costs(context,recommendation)
        candle=context['timeframes']['MINUTE_15']['candles'][-1]['t']
        payload=dict(costs,metrics={k:scoring[k] for k in ('spread_to_atr','timeframes_agree')},
                     technical_reason=scoring['reason'],technical_passed=scoring['score']>0,
                     paper_start=(math.floor(now/BAR_SECONDS)+1)*BAR_SECONDS,
                     observed_at=now)
        with self.db:
            saved=self.db.execute('INSERT OR IGNORE INTO spread_candidates VALUES(?,?,?,?,?,?)',
                                 (self.account,context['epic'],candle,now,json.dumps(payload,allow_nan=False),
                                  '{"status":"pending"}')).rowcount
        return dict(costs,recorded=bool(saved),paper_start=payload['paper_start'])

    @classmethod
    def report(cls,path,account,now):
        """mode=ro cannot create a study, extend a trial, or change a threshold."""
        db=sqlite3.connect(Path(path).resolve().as_uri()+'?mode=ro',uri=True,timeout=10)
        try:
            observations={epic:[] for epic in MARKETS}
            candidates={epic:[] for epic in MARKETS}
            cutoff=now-RETENTION_SECONDS
            for epic,payload in db.execute('SELECT epic,payload FROM spread_observations '
                                          'WHERE account=? AND observed_at>=?',(account,cutoff)):
                observations.setdefault(epic,[]).append(json.loads(payload))
            for epic,payload,outcome in db.execute('SELECT epic,payload,outcome FROM spread_candidates '
                                                  'WHERE account=? AND observed_at>=?',(account,cutoff)):
                item=json.loads(payload)
                item['outcome']=json.loads(outcome)
                if (item['outcome']['status']=='pending'
                        and now>=item['paper_start']+HORIZON_BARS*BAR_SECONDS):
                    item['outcome']=dict(status='missing_bars')
                candidates.setdefault(epic,[]).append(item)
            markets={}
            for epic,values in observations.items():
                plans=candidates.get(epic,[])
                markets[epic]=dict(observations=len(values),spread_to_atr=ratio_summary(
                    [v['spread_to_atr'] for v in values]),spread_gate_counts=dict(Counter(
                    v['spread_gate_reason'] for v in values)),paper_candidates=len(plans),
                    paper_status_counts=dict(Counter(c['outcome']['status'] for c in plans)),
                    comparison=threshold_comparison(values,plans))
            return dict(current_spread_atr_limit=SPREAD_ATR_LIMIT,threshold_changed=False,
                        retention_days=7,markets=markets,
                        method='prospective_next_full_M15_open_exit_sides_16_bars',
                        limitations=['independent paper plans, not portfolio performance',
                                     'no slippage, financing or FX fees; spread counted once',
                                     'same-bar stop and target censored as ambiguous',
                                     'missing bars censored; invalid next-open entries excluded',
                                     'requires independent out-of-sample validation before policy changes'])
        finally:
            db.close()

    def close(self):
        self.db.close()


def historical_cost_study(markets,now):
    """Historical close-side spread/ATR sensitivity, not trade profitability."""
    result={}
    for epic,feed in markets.items():
        bars=[]
        error=None
        try:
            for row in feed['prices']:
                stamp=utc_stamp(row['snapshotTimeUTC'])
                if stamp+BAR_SECONDS>now:
                    continue
                values={}
                for source,key in (('openPrice','o'),('highPrice','h'),('lowPrice','l'),('closePrice','c')):
                    bid,ask=(numeric(row[source][side]) for side in ('bid','ask'))
                    if ask<bid:
                        raise ValueError('Crossed historical spread')
                    values[key]=(bid+ask)/2
                for side in ('bid','ask'):
                    if not (row['lowPrice'][side]<=row['openPrice'][side]<=row['highPrice'][side]
                            and row['lowPrice'][side]<=row['closePrice'][side]<=row['highPrice'][side]):
                        raise ValueError('Invalid historical range')
                bars.append(dict(t=stamp,bid=row['closePrice']['bid'],ask=row['closePrice']['ask'],**values))
            bars.sort(key=lambda b:b['t'])
            if len({b['t'] for b in bars})!=len(bars):
                raise ValueError('Duplicate historical bar')
            observations=[]
            gaps=0
            for index in range(14,len(bars)):
                history=bars[index-14:index+1]
                if any(b['t']-a['t']!=BAR_SECONDS for a,b in zip(history[-5:-1],history[-4:])):
                    gaps+=1
                    continue
                observations.append(spread_metrics(dict(bid=bars[index]['bid'],ask=bars[index]['ask'],
                    timeframes={'MINUTE_15':{'candles':history}})))
            result[epic]=dict(closed_bars=len(bars),observations=len(observations),gap_skips=gaps,
                first_bar=bars[0]['t'] if bars else None,last_bar=bars[-1]['t'] if bars else None,
                spread_to_atr=ratio_summary([o['spread_to_atr'] for o in observations]),
                spread_gate_counts=dict(Counter(o['spread_gate_reason'] for o in observations)),
                comparison=threshold_comparison(observations))
        except (ValueError,TypeError,KeyError,ZeroDivisionError) as exc:
            error=type(exc).__name__
        if error:
            result[epic]=dict(error_type=error,observations=0)
    return dict(as_of=now,current_spread_atr_limit=SPREAD_ATR_LIMIT,threshold_changed=False,
                source='Capital historical M15 bid/ask close prices',markets=result,
                limitations=['historical close spread is a proxy, not the rejected order quote',
                             'cost-screen pass rates do not establish profitable entry thresholds',
                             'no historical AI stop/target plans or portfolio outcomes are fabricated'])


def main():
    import argparse
    import os
    import time
    parser=argparse.ArgumentParser(description='Read-only spread calibration; no APIs or orders')
    mode=parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--report',action='store_true')
    mode.add_argument('--history',type=Path,help='JSON containing previously retrieved markets/prices')
    args=parser.parse_args()
    if args.history:
        data=json.loads(args.history.read_text())
        result=historical_cost_study(data['markets'],data.get('as_of',time.time()))
    else:
        path=Path(os.environ.get('SMART_STATE_DIR','/var/data/smarttrader'))/'spread.sqlite'
        result=SpreadStudy.report(path,os.environ.get('BOT_ACCOUNT_ID',''),time.time())
    print(json.dumps(result,allow_nan=False,indent=2))


if __name__=='__main__':
    main()
