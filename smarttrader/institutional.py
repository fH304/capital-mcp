"""Public-data desk analysis and an isolated, unvalidated paper strategy.

This is not a bank's proprietary strategy or a calibrated probability model.
No API client, broker method, account preference, or risk setting lives here.
The paper strategy cannot authorize an entry or be enabled through an env flag.
"""
import json
import math
import sqlite3
import statistics
from collections import Counter
from pathlib import Path

from .candles import MarketDataError, numeric, validate_frames
from .spreadstudy import BAR_SECONDS, COMPLETE, HORIZON_BARS, paper_outcome
from .universe import SPREAD_ATR_REFERENCE, SPREAD_POLICY

FRAMEWORK = 'institutional_discipline_v1'
STRATEGY = 'shadow_breakout_retest_v1'
RETENTION = 7*86400


def true_ranges(bars):
    return [max(b['h']-b['l'], abs(b['h']-p['c']), abs(b['l']-p['c']))
            for p, b in zip(bars, bars[1:])]


def frame_metrics(frame):
    bars = frame['candles'][-20:]
    ranges = true_ranges(bars)
    atr = statistics.mean(ranges[-14:])
    changes = [b['c']-p['c'] for p, b in zip(bars, bars[1:])]
    net, path = bars[-1]['c']-bars[0]['c'], sum(abs(x) for x in changes)
    efficiency = abs(net)/path if path else 0.0
    bias = ('up' if net > 0 else 'down') if atr > 0 and abs(net) >= atr and efficiency >= .35 else 'neutral'
    return dict(bias=bias, atr_14=atr, efficiency_ratio=efficiency,
                net_move_atr=net/atr if atr > 0 else None,
                closed_bars=len(bars), first_start=bars[0]['t'], last_start=bars[-1]['t'],
                history_contiguous=all(b['t']-a['t'] == frame['seconds'] for a, b in zip(bars, bars[1:])),
                prior_support=min(b['l'] for b in bars[:-1]),
                prior_resistance=max(b['h'] for b in bars[:-1]))


def market_brief(context, now):
    """Deterministic evidence for the analyst; never include a paper order."""
    bid, ask, quote_time = (numeric(context[k]) for k in ('bid', 'ask', 'quote_time'))
    if ask < bid or not 0 <= now-quote_time <= 30:
        raise MarketDataError('Stale or crossed desk quote')
    validate_frames(context['timeframes'], now)
    frames = {name: frame_metrics(frame) for name, frame in context['timeframes'].items()}
    higher = [frames[name]['bias'] for name in ('HOUR_4', 'HOUR')]
    regime = ('trend_'+higher[0] if higher[0] == higher[1] and higher[0] != 'neutral' else
              'range' if higher == ['neutral', 'neutral'] else 'mixed')
    bars = context['timeframes']['MINUTE_15']['candles']
    baseline_atr = statistics.mean(true_ranges(bars[:-1])[-14:])
    last_range = true_ranges(bars[-2:])[0]
    spread = ask-bid
    atr = frames['MINUTE_15']['atr_14']
    return dict(framework=FRAMEWORK, observed_at=now, quote_time=quote_time,
                regime=regime, frames=frames,
                spread=spread, spread_to_atr=spread/atr if atr > 0 else None,
                spread_atr_limit=None, spread_policy=SPREAD_POLICY,
                spread_atr_reference=SPREAD_ATR_REFERENCE, baseline_atr_15=baseline_atr,
                last_bar_range_atr=last_range/baseline_atr if baseline_atr > 0 else None,
                price_basis='closed bid/ask midpoint candles; executable quote sides',
                indicator_basis='20 closed bars; heuristic thresholds, not win probabilities',
                missing_data=['scheduled_macro_calendar', 'global_order_book', 'bank_client_flow',
                              'global_traded_volume', 'verified_cross_market_confirmation',
                              'forward_slippage_and_financing_costs'],
                news_basis='supplied recent articles only; absence is not a quiet event calendar')


def shadow_setup(context, brief):
    """Trend breakout / next-bar retest hypotheses, for future paper bars only.

    Thresholds are research assumptions. A 2R target is a fixed exit rule, not
    a forecast or a demonstrated edge. These outputs never go to the analyst
    as an executable recommendation or to the order coordinator.
    """
    base = dict(strategy=STRATEGY, action='WAIT', reason='no_qualifying_setup',
                validated=False, execution_authorized=False)
    def wait(reason):
        return dict(base, reason=reason)
    if context.get('execution_supported') is not True:
        return wait('observation_only_contract')
    if not all(f['history_contiguous'] for f in brief['frames'].values()):
        return wait('lookback_history_gap')
    atr = brief['baseline_atr_15']
    if atr <= 0 or brief['spread_to_atr'] is None:
        return wait('zero_volatility')
    if brief['last_bar_range_atr'] > 3:
        return wait('volatility_shock')
    if brief['regime'] not in ('trend_up', 'trend_down'):
        return wait('higher_timeframes_range_or_mixed')
    bars = context['timeframes']['MINUTE_15']['candles']
    last, previous = bars[-1], bars[-2]
    buying = brief['regime'] == 'trend_up'
    sign = 1 if buying else -1
    edge = 'h' if buying else 'l'
    extreme = max if buying else min
    level = extreme(b[edge] for b in bars[-21:-1])
    retest_level = extreme(b[edge] for b in bars[-22:-2])
    # The reference window ends before the signal bar(s), avoiding lookahead.
    breakout = sign*(last['c']-level) > .1*atr
    retest = (sign*(previous['c']-retest_level) > .1*atr
              and sign*(last['c']-retest_level) > .1*atr
              and sign*(last['c']-last['o']) > 0
              and (last['l'] <= retest_level+.2*atr if buying else last['h'] >= retest_level-.2*atr))
    if retest:
        setup, level = 'breakout_retest', retest_level
    elif breakout:
        setup = 'closed_bar_breakout'
    else:
        return wait('no_closed_breakout_or_retest')
    exit_side = context['bid'] if buying else context['ask']
    entry = context['ask'] if buying else context['bid']
    if sign*(exit_side-level) <= 0:
        return wait('breakout_reversed_before_observation')
    # Chasing measures price movement, not the width of the spread. The spread
    # is counted separately in executable risk/reward, without a fixed cutoff.
    mid = (context['bid']+context['ask'])/2
    if abs(mid-last['c']) > .5*atr or sign*(last['c']-level) > atr:
        return wait('price_extended_no_chasing')
    stop = (min(last['l'], level)-.1*atr if buying else max(last['h'], level)+.1*atr)
    risk = sign*(entry-stop)
    target = math.nextafter(entry+sign*2*risk, math.inf if buying else -math.inf)
    if stop <= 0 or target <= 0 or risk <= 0 or sign*(exit_side-stop) <= 0:
        return wait('invalid_structure_protection')
    return dict(base, action='BUY' if buying else 'SELL', reason=setup, reference_level=level,
                entry_price=entry, stop_level=stop, target_level=target,
                reward_risk=2.0, risk_distance=risk, spread_to_risk=brief['spread']/risk,
                target_basis='fixed_2R_paper_exit_rule_not_forecast',
                news_confirmed=False)


class InstitutionalStudy:
    """Prospective observations, isolated from order ownership and trial budget."""
    def __init__(self, path, account):
        self.account = account
        self.db = sqlite3.connect(path, timeout=10)
        self.db.execute('PRAGMA synchronous=FULL')
        self.db.execute('CREATE TABLE IF NOT EXISTS desk_observations('
                        'account TEXT,epic TEXT,candle REAL,version TEXT,observed_at REAL,payload TEXT,outcome TEXT,'
                        'PRIMARY KEY(account,epic,candle,version))')
        self.db.commit()

    def observe(self, context, now, bars=()):
        brief = market_brief(context, now)
        setup = shadow_setup(context, brief)
        payload = dict(setup, regime=brief['regime'], observed_at=now,
                       signal_candle=context['timeframes']['MINUTE_15']['candles'][-1]['t'],
                       paper_start=(math.floor(now/BAR_SECONDS)+1)*BAR_SECONDS,
                       quote_currency=context.get('quote_currency', 'USD'))
        outcome = dict(status='pending' if setup['action'] != 'WAIT' else 'not_candidate')
        with self.db:
            saved = self.db.execute('INSERT OR IGNORE INTO desk_observations VALUES(?,?,?,?,?,?,?)',
                                   (self.account, context['epic'], payload['signal_candle'], STRATEGY, now,
                                    json.dumps(payload, allow_nan=False), json.dumps(outcome))).rowcount
            self.db.execute('DELETE FROM desk_observations WHERE account=? AND observed_at<?',
                            (self.account, now-RETENTION))
            if bars:
                pending = self.db.execute('SELECT candle,version,payload FROM desk_observations '
                                          'WHERE account=? AND epic=? AND json_extract(outcome,"$.status")="pending"',
                                          (self.account, context['epic'])).fetchall()
                for candle, version, data in pending:
                    result = paper_outcome(json.loads(data), bars, now)
                    self.db.execute('UPDATE desk_observations SET outcome=? '
                                    'WHERE account=? AND epic=? AND candle=? AND version=?',
                                    (json.dumps(result, allow_nan=False), self.account, context['epic'], candle, version))
        return dict(recorded=bool(saved), regime=brief['regime'], shadow_action=setup['action'],
                    shadow_reason=setup['reason'], strategy=STRATEGY, validated=False,
                    execution_authorized=False, execution_policy_changed=False)

    @classmethod
    def report(cls, path, account, now):
        db = sqlite3.connect(Path(path).resolve().as_uri()+'?mode=ro', uri=True, timeout=10)
        try:
            grouped = {}
            for epic, payload, raw in db.execute('SELECT epic,payload,outcome FROM desk_observations '
                                                'WHERE account=? AND version=? AND observed_at>=?',
                                                (account, STRATEGY, now-RETENTION)):
                data, outcome = json.loads(payload), json.loads(raw)
                if outcome['status'] == 'pending' and now >= data['paper_start']+HORIZON_BARS*BAR_SECONDS:
                    outcome = dict(status='missing_bars')
                grouped.setdefault(epic, []).append((data, outcome))
            markets = {}
            for epic, values in grouped.items():
                completed = [outcome['price_R'] for _, outcome in values if outcome['status'] in COMPLETE]
                markets[epic] = dict(observations=len(values),
                                     candidates=sum(data['action'] != 'WAIT' for data, _ in values),
                                     setup_counts=dict(Counter(data['reason'] for data, _ in values)),
                                     outcome_counts=dict(Counter(outcome['status'] for _, outcome in values)),
                                     completed=len(completed), mean_price_R=statistics.mean(completed) if completed else None)
            return dict(framework=FRAMEWORK, strategy=STRATEGY, mode='shadow_only', validated=False,
                        execution_authorized=False, markets=markets,
                        method='prospective_next_full_M15_open_exit_sides_16_bars',
                        limitations=['independent candidates; not a simulated two-leg portfolio',
                                     'no slippage, financing or currency conversion fees; spread counted once',
                                     'same-bar stop and target ambiguous; gaps censored or priced conservatively',
                                     'no macro calendar, global order flow, or demonstrated profitable edge',
                                     'requires independent out-of-sample and forward evaluation before promotion'])
        finally:
            db.close()

    def close(self):
        self.db.close()


def main():
    import argparse
    import os
    import time
    parser = argparse.ArgumentParser(description='Read-only institutional paper strategy report')
    parser.add_argument('--report', action='store_true', required=True)
    parser.parse_args()
    path = Path(os.environ.get('SMART_STATE_DIR', '/var/data/smarttrader'))/'institutional.sqlite'
    print(json.dumps(InstitutionalStudy.report(path, os.environ['BOT_ACCOUNT_ID'], time.time()), allow_nan=False))


if __name__ == '__main__':
    main()
