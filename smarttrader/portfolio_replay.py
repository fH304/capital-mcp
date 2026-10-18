"""Read-only, single-account historical replay of frozen demo rules.

This is a research approximation. It never connects to an account or submits
orders. The alternate range confirmation is intentionally not an entry policy.
"""
import argparse
import bisect
import glob
import json
import math
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

from .candles import normalize_candles, utc_stamp
from .evidence import technical_evidence

SECONDS = {'MINUTE_15': 900, 'HOUR': 3600, 'HOUR_4': 14400}
METALS = {'GOLD': {'margin': .05, 'min_size': .01, 'size_step': .01},
          'SILVER': {'margin': .10, 'min_size': 1, 'size_step': .1}}


def timestamp(raw):
    dt=datetime.fromisoformat(raw.replace('Z', '+00:00'))
    return (dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt).timestamp()


def load_history(directory, epics=('GOLD', 'SILVER')):
    """Read Capital OHLC exports; timestamps are UTC and duplicates are removed."""
    result = {}
    for epic in epics:
        result[epic] = {}
        for resolution in SECONDS:
            rows = {}
            pattern = str(Path(directory)/f'market_hist_{epic}_{resolution}_20*.json')
            for name in glob.glob(pattern):
                with open(name) as source:
                    for row in json.load(source):
                        stamp = utc_stamp(row['snapshotTimeUTC'])
                        if stamp in rows and rows[stamp] != row:
                            raise ValueError('Conflicting historical candle')
                        rows[stamp] = row
            if not rows:
                raise ValueError('Missing history: '+epic+'/'+resolution)
            result[epic][resolution] = dict(rows=rows, times=sorted(rows))
    return result


def history_context(data, epic, stamp):
    frames = {}
    for resolution, seconds in SECONDS.items():
        feed = data[epic][resolution]
        end = bisect.bisect_right(feed['times'], stamp-seconds)
        if end < 40:
            raise ValueError('Insufficient closed history')
        raw = [feed['rows'][t] for t in feed['times'][end-40:end]]
        frames[resolution] = normalize_candles(raw, resolution, stamp)
    close = data[epic]['MINUTE_15']['rows'][stamp-900]['closePrice']
    return dict(epic=epic, bid=close['bid'], ask=close['ask'], quote_time=stamp,
                collected_at=stamp, execution_supported=True, timeframes=frames)


def permitted_entry(signal, opening, balance, spec, slippage_bps):
    direction=signal['direction']
    side='ask' if direction=='BUY' else 'bid'
    raw=opening[side]
    entry=raw*(1+(slippage_bps/10000 if direction=='BUY' else -slippage_bps/10000))
    stop,target=signal['stop_level'],signal['target_level']
    risk=entry-stop if direction=='BUY' else stop-entry
    reward=target-entry if direction=='BUY' else entry-target
    stress=.1*signal['atr']+entry*slippage_bps/10000
    if (risk<=0 or reward-stress<2*(risk+stress) or risk<entry*.0001
            or reward<entry*.0001):
        return None, 'next_open_invalid_levels'
    step=spec['size_step']
    size=round(math.floor((min(balance*.00125/(risk+stress),
                               balance*.1/(entry*spec['margin']))+1e-12)/step)*step,8)
    if size+1e-10<spec['min_size']:
        return None, 'size_below_broker_minimum'
    return dict(direction=direction,entry=entry,size=size,stop=stop,target=target,
                atr=signal['atr'],strategy=signal['strategy'],
                expected_risk=size*risk),None


def confirmation(signal, row):
    """Check only the bar after the signal; no future bar informs the decision."""
    sign=1 if signal['direction']=='BUY' else -1
    close=(row['closePrice']['bid']+row['closePrice']['ask'])/2
    signal_close=signal['signal_close']
    if sign*(close-signal_close)<=0:
        return False,'confirmation_not_continuing'
    if sign==1:
        touched=(row['lowPrice']['bid']<=signal['stop_level'] or
                 row['highPrice']['bid']>=signal['target_level'])
    else:
        touched=(row['highPrice']['ask']>=signal['stop_level'] or
                 row['lowPrice']['ask']<=signal['target_level'])
    if touched:
        return False,'confirmation_levels_touched'
    return True,None


def exit_on_bar(position,row,slippage_bps):
    direction=position['direction']
    side='bid' if direction=='BUY' else 'ask'
    price=row['openPrice'][side]
    high,low=row['highPrice'][side],row['lowPrice'][side]
    stop,target=position['stop'],position['target']
    if direction=='BUY':
        if price<stop: exit_price,reason=price,'gap_stop'
        elif low<=stop: exit_price,reason=stop,'stop'
        elif high>=target: exit_price,reason=target,'target'
        else: return None
        exit_price*=1-slippage_bps/10000
        pnl=(exit_price-position['entry'])*position['size']
    else:
        if price>stop: exit_price,reason=price,'gap_stop'
        elif high>=stop: exit_price,reason=stop,'stop'
        elif low<=target: exit_price,reason=target,'target'
        else: return None
        exit_price*=1+slippage_bps/10000
        pnl=(position['entry']-exit_price)*position['size']
    return dict(exit_price=exit_price,exit_reason=reason,gross_pnl=pnl)


def replay(data, *, start, end, variant='baseline', initial=1000,
           slippage_bps=0, overnight_rate=0, rollover_hour_utc=21,
           market_order=('GOLD','SILVER')):
    """At most one open/pending leg across markets; GOLD wins simultaneous ties.

    A pending order opens only at the next available *contiguous* M15 open.
    The confirmation variant waits one complete extra M15 candle for range
    setups, keeping the original structural stop and target.
    """
    if variant not in {'baseline','range_confirm'} or initial<=0 or slippage_bps<0 or overnight_rate<0:
        raise ValueError('Invalid replay policy')
    if not 0<=rollover_hour_utc<=23:
        raise ValueError('Invalid rollover hour')
    feeds={e:data[e]['MINUTE_15']['rows'] for e in market_order}
    stamps=sorted(set().union(*(set(feed) for feed in feeds.values())))
    balance=float(initial)
    position=pending=None
    trades=[]
    reasons=Counter()
    scan_reasons=defaultdict(Counter)
    marked=[]
    last_tick=None
    for tick in stamps:
        if tick+900<start or tick>end:
            continue
        rows={e:feed[tick] for e,feed in feeds.items() if tick in feed}
        # A recorded signal at previous close can only enter the next open.
        if pending:
            epic=pending['epic']
            if tick!=pending['next_tick'] or epic not in rows:
                reasons['pending_bar_gap']+=1;pending=None
            elif pending['phase']=='confirm':
                good,reason=confirmation(pending['signal'],rows[epic])
                if good:
                    pending['phase']='entry';pending['next_tick']=tick+900
                else:
                    reasons[reason]+=1;pending=None
            else:
                opened,reason=permitted_entry(pending['signal'],rows[epic]['openPrice'],
                                              balance,METALS[epic],slippage_bps)
                if opened:
                    position=dict(opened,epic=epic,opened_at=tick,
                                  signal_at=pending['signal_at'],funding=0.,
                                  last_rollover=None,entry_spread=pending['entry_spread'])
                    reasons['hypothetical_entry']+=1
                else: reasons[reason]+=1
                pending=None
        if position and position['epic'] in rows:
            rollover=tick//86400*86400+rollover_hour_utc*3600
            if (tick>=rollover and position['opened_at']<rollover
                    and position['last_rollover']!=tick//86400):
                position['funding']+=position['entry']*position['size']*overnight_rate
                position['last_rollover']=tick//86400
            # Conservative bar sequencing: a stop wins if both levels touched.
            result=exit_on_bar(position,rows[position['epic']],slippage_bps)
            if result:
                net=result['gross_pnl']-position['funding']
                balance+=net
                trades.append(dict(epic=position['epic'],strategy=position['strategy'],
                                   signal_at=position['signal_at'],opened_at=position['opened_at'],
                                   closed_at=tick,entry=position['entry'],size=position['size'],
                                   stop=position['stop'],target=position['target'],
                                   spread_at_signal=position['entry_spread'],
                                   funding=position['funding'],net_pnl=net,
                                   balance=balance,**result))
                position=None
        if position or pending or tick+900>end:
            last_tick=tick;continue
        # Select the first eligible market, preserving an explicit tie rule.
        for epic in market_order:
            if epic not in rows: continue
            stamp=tick+900
            try:
                context=history_context(data,epic,stamp)
                signal=technical_evidence(context,stamp,require_news_health=False)
            except (ValueError,KeyError) as error:
                scan_reasons[epic]['history_unavailable']+=1
                continue
            scan_reasons[epic][signal['reason']]+=1
            if not signal['ready']: continue
            signal=dict(signal,signal_close=context['timeframes']['MINUTE_15']['candles'][-1]['c'])
            phase='confirm' if variant=='range_confirm' and signal['strategy']=='range_rejection_v1' else 'entry'
            pending=dict(epic=epic,signal=signal,signal_at=stamp,phase=phase,
                         next_tick=tick+900,entry_spread=context['ask']-context['bid'])
            reasons['candidate_'+signal['strategy']]+=1
            break
        last_tick=tick
    marked_balance=balance
    if position:
        row=feeds[position['epic']][last_tick]
        mark=row['closePrice']['bid' if position['direction']=='BUY' else 'ask']
        gross=(mark-position['entry'])*position['size']*(1 if position['direction']=='BUY' else -1)
        marked_balance+=gross-position['funding']
    peak=initial;drawdown=0
    for t in trades:
        peak=max(peak,t['balance']);drawdown=max(drawdown,peak-t['balance'])
    return dict(variant=variant,initial=initial,realized_balance=balance,
                marked_balance=marked_balance,max_closed_trade_drawdown=drawdown,
                counts=dict(Counter(t['epic'] for t in trades)),
                wins=sum(t['net_pnl']>0 for t in trades),losses=sum(t['net_pnl']<=0 for t in trades),
                reasons=dict(reasons),scan_reasons={k:dict(v) for k,v in scan_reasons.items()},
                trades=trades,open_position=position,pending=pending,
                last_available_tick=last_tick)


def main():
    parser=argparse.ArgumentParser(description='Read-only two-metal demo research replay')
    parser.add_argument('--history-dir',required=True)
    parser.add_argument('--start',default='2026-09-01T00:00:00')
    parser.add_argument('--end',default='2026-10-18T12:00:00')
    parser.add_argument('--initial',type=float,default=1000)
    parser.add_argument('--slippage-bps',type=float,default=0)
    parser.add_argument('--overnight-rate',type=float,default=0,
                        help='Hypothetical fraction of entry notional per rollover; zero means excluded')
    args=parser.parse_args()
    data=load_history(args.history_dir)
    start,end=timestamp(args.start),timestamp(args.end)
    report=[replay(data,start=start,end=end,variant=v,initial=args.initial,
                   slippage_bps=args.slippage_bps,overnight_rate=args.overnight_rate)
            for v in ('baseline','range_confirm')]
    print(json.dumps(dict(method='one_hypothetical_position_shared_account_fixed_GOLD_first_tie',
                          start=args.start,end=args.end,slippage_bps=args.slippage_bps,
                          overnight_rate_assumed=args.overnight_rate,
                          variants=report,execution_authorized=False,
                          limitations=['Historical broker fills and exact scheduling of the deployed 287-market worker are unavailable',
                                       'Only GOLD and SILVER; no staged second leg or other-market opportunity cost',
                                       'Current contract margin and size rules are historical proxies',
                                       'Rollover charge is an explicit sensitivity assumption, not a verified historical broker fee',
                                       'September-October was already inspected and cannot be an untouched validation sample']),
                     allow_nan=False))


if __name__=='__main__':main()
