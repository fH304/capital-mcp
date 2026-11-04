"""Persistent GOLD + EUR/USD paper ledger, independent of the demo GOLD trade.

All fills are local calculations at observed bid/ask quotes. This client cannot
submit, cancel or close broker orders. Funding, slippage and margin liquidation
are excluded; the broker account equity is an observed comparison only.
"""
import json
import sqlite3
import threading
import time
import uuid
from datetime import datetime, timezone
from decimal import Decimal, ROUND_CEILING

from .candles import utc_stamp
from .ema50_daily import Broker, Halt, Transport, Wait, daily_signal, emit, num, quote

STRATEGY = 'gold_eurusd_daily_ema50_paper_v1'


def eur_quote(data, now, offset):
    instrument, snapshot = data['instrument'], data['snapshot']
    if (instrument['epic'] != 'EURUSD' or instrument['type'] != 'CURRENCIES'
            or instrument['currency'] != 'USD' or num(instrument['lotSize']) != 1
            or num(snapshot['scalingFactor']) != 1):
        raise Halt('unsupported_eurusd_paper_contract')
    if snapshot['marketStatus'] != 'TRADEABLE' or 'REGULAR' not in snapshot['marketModes']:
        raise Wait('eurusd_market_closed')
    stamp = (utc_stamp(snapshot['updateTimeUTC']) if snapshot.get('updateTimeUTC') else
             utc_stamp(snapshot['updateTime']) - offset * 3600)
    if snapshot['delayTime'] != 0 or not 0 <= now - stamp <= 120:
        raise Wait('eurusd_quote_stale')
    bid, ask = num(snapshot['bid']), num(snapshot['offer'])
    if ask < bid:
        raise Halt('inverted_eurusd_quote')
    return dict(bid=bid, ask=ask)


def minimum_size(data, requested=None):
    rules = data['dealingRules']
    def size_rule(name):
        rule = rules[name]
        if rule['unit'] != 'POINTS':
            raise Halt('unsupported_paper_size_rule')
        return Decimal(str(num(rule['value'])))
    minimum, step, maximum = (size_rule(k) for k in
                              ('minDealSize', 'minSizeIncrement', 'maxDealSize'))
    size = (minimum / step).to_integral_value(rounding=ROUND_CEILING) * step
    if requested is not None:
        size = Decimal(str(num(requested)))
        if size < minimum or size % step:
            raise Halt('paper_requested_size_outside_broker_rules')
    if size > maximum:
        raise Halt('invalid_paper_minimum_size')
    return float(size)


def mark_position(position, bid_ask):
    """USD P&L of units, with the executable exit side including the spread."""
    if position['direction'] == 'BUY':
        mark = bid_ask['bid']
        pnl = (mark - position['entry']) * position['size']
    elif position['direction'] == 'SELL':
        mark = bid_ask['ask']
        pnl = (position['entry'] - mark) * position['size']
    else:
        raise Halt('invalid_paper_position_direction')
    return mark, pnl


class PaperLedger:
    def __init__(self, path, account, gold_size=.05):
        self.gold_size = num(gold_size)
        self.db = sqlite3.connect(path)
        self.db.execute('PRAGMA synchronous=FULL')
        self.db.execute('CREATE TABLE IF NOT EXISTS state (id INTEGER PRIMARY KEY, body TEXT NOT NULL)')
        self.db.execute('CREATE TABLE IF NOT EXISTS trades (id INTEGER PRIMARY KEY, body TEXT NOT NULL)')
        self.db.execute('CREATE TABLE IF NOT EXISTS snapshots (minute INTEGER PRIMARY KEY, body TEXT NOT NULL)')
        scope = [STRATEGY, account, 'DAY', ['EURUSD', 'broker_minimum'], ['GOLD', self.gold_size]]
        row = self.db.execute('SELECT body FROM state WHERE id=1').fetchone()
        self.state = json.loads(row[0]) if row else dict(scope=scope, started_at=None,
            initial_broker_equity=None, position=None, realized=0.0, last_signal_bar=None,
            gold_position=None, gold_realized=0.0, gold_last_signal_bar=None)
        if self.state['scope'] != scope:
            self.db.close()
            raise Halt('paper_ledger_scope_mismatch')
        num(self.state['realized'], False)
        num(self.state['gold_realized'], False)
        for position in (self.state['position'], self.state['gold_position']):
            if position:
                num(position['entry'])
                num(position['size'])
                num(position['opened_at'], False)
                num(position['signal_bar'], False)
                if position['direction'] not in ('BUY', 'SELL'):
                    self.db.close()
                    raise Halt('invalid_paper_position_direction')

    def save_cycle(self, state, events, snapshot, now):
        # Position, realized P&L, fills and observation commit together. A restart
        # cannot repeat a fill or reset the baseline after a partial cycle.
        encode = lambda value: json.dumps(value, allow_nan=False)
        with self.db:
            self.db.execute('INSERT OR REPLACE INTO state VALUES (1,?)', (encode(state),))
            self.db.executemany('INSERT INTO trades(body) VALUES (?)',
                                [(encode(event),) for event in events])
            self.db.execute('INSERT OR REPLACE INTO snapshots VALUES (?,?)',
                            (int(now // 60), encode(snapshot)))
            self.db.execute('DELETE FROM snapshots WHERE minute < ?', (int(now // 60) - 90*1440,))
        self.state = state


class PaperWorker:
    def __init__(self, api, ledger, *, clock=time.time, log=emit):
        if api.armed or not api.paper_market_data:
            raise Halt('paper_client_must_be_unarmed')
        self.api, self.ledger, self.clock, self.log = api, ledger, clock, log
        self.idea, self.gold_idea, self.idea_day, self.idea_at = None, None, None, 0

    @staticmethod
    def fills(epic, position, realized, idea, q, size, now):
        events = []
        prefix = 'gold' if epic == 'GOLD' else 'eurusd'
        if idea['direction'] and (not position or position['direction'] != idea['direction']):
            if position:
                if idea['bar'] <= position['signal_bar']:
                    raise Wait('paper_reversal_requires_new_completed_day')
                exit_price, pnl = mark_position(position, q)
                realized += pnl
                events.append(dict(event=prefix + '_paper_closed', epic=epic,
                    trade_id=position['trade_id'], direction=position['direction'],
                    size=position['size'], entry=position['entry'], exit=exit_price,
                    realized_usd=pnl, signal_bar=idea['bar'], at=now))
            position = dict(trade_id=uuid.uuid4().hex, direction=idea['direction'], size=size,
                entry=q['ask'] if idea['direction'] == 'BUY' else q['bid'],
                opened_at=now, signal_bar=idea['bar'])
            events.append(dict(event=prefix + '_paper_opened', epic=epic, **position))
        return position, realized, events

    def reference_account(self):
        session = self.api.read('/session')
        if str(session.get('accountId', session.get('currentAccountId'))) != self.api.account:
            raise Halt('paper_reference_account_changed')
        matches = [a for a in self.api.read('/accounts')['accounts']
                   if str(a['accountId']) == self.api.account]
        if len(matches) != 1 or any(matches[0].get(k) != v for k, v in
                                   dict(currency='USD', accountType='CFD', status='ENABLED').items()):
            raise Halt('unsupported_paper_reference_account')
        balance = matches[0]['balance']
        equity = num(balance['balance'], False)
        positions = self.api.read('/positions')['positions']
        if not isinstance(positions, list):
            raise Halt('incomplete_paper_reference_positions')
        gold = [p for p in positions if p['market']['epic'] == 'GOLD']
        if len(gold) > 1:
            raise Wait('multiple_gold_reference_positions')
        reference = None
        if gold:
            p = gold[0]['position']
            if p['currency'] != 'USD' or num(p['contractSize']) != 1:
                raise Halt('unsupported_gold_reference_contract')
            q = quote(self.api.read('/markets/GOLD'), self.clock(), self.api.offset)
            mark, pnl = mark_position(dict(direction=p['direction'], entry=num(p['level']),
                                           size=num(p['size'])), q)
            reference = dict(deal_id=p['dealId'], direction=p['direction'], size=p['size'],
                             entry=p['level'], mark=mark, unrealized_usd=pnl)
        return equity, reference, len(positions) - len(gold)

    def cycle(self):
        equity, gold, other_count = self.reference_account()
        now = self.clock()
        day = int(now // 86400)
        if self.idea is None or self.idea_day != day or now - self.idea_at >= 300:
            history = self.api.read('/prices/EURUSD?resolution=DAY&max=500')['prices']
            idea = daily_signal(history, self.clock())
            gold_history = self.api.read('/prices/GOLD?resolution=DAY&max=500')['prices']
            gold_idea = daily_signal(gold_history, self.clock())
            self.idea, self.gold_idea = idea, gold_idea
            self.idea_at, self.idea_day = self.clock(), day
        data = self.api.read('/markets/EURUSD')
        gold_data = self.api.read('/markets/GOLD')
        now = self.clock()
        if int(now // 86400) != day:
            raise Wait('paper_daily_boundary_changed')
        q = eur_quote(data, now, self.api.offset)
        gold_q = quote(gold_data, now, self.api.offset)
        idea = self.idea
        state = dict(self.ledger.state)
        if state['started_at'] is None:
            state['started_at'], state['initial_broker_equity'] = now, equity
        position, realized, events = self.fills('EURUSD', state['position'], state['realized'],
            idea, q, minimum_size(data), now)
        gold_position, gold_realized, gold_events = self.fills('GOLD',
            state['gold_position'], state['gold_realized'], self.gold_idea, gold_q,
            minimum_size(gold_data, self.ledger.gold_size), now)
        state.update(position=position, realized=realized,
                     gold_position=gold_position, gold_realized=gold_realized)
        events.extend(gold_events)
        state['last_signal_bar'] = idea['bar']
        state['gold_last_signal_bar'] = self.gold_idea['bar']
        mark, unrealized = mark_position(position, q) if position else (None, 0.0)
        gold_mark, gold_unrealized = mark_position(gold_position, gold_q) if gold_position else (None, 0.0)
        eur_total = state['realized'] + unrealized
        gold_total = gold_realized + gold_unrealized
        combined = state['initial_broker_equity'] + eur_total + gold_total
        snapshot = dict(strategy=STRATEGY, virtual_only=True, epics=['GOLD', 'EURUSD'], timeframe='DAY',
            observed_at=now,
            account_ending=self.api.account[-4:], started_at=state['started_at'],
            last_closed_bar=datetime.fromtimestamp(idea['bar'], timezone.utc).isoformat(),
            last_closed_bid=idea['close'], ema50=idea['ema50'], signal=idea['direction'],
            eur_position=position, eur_mark=mark, eur_realized_usd=state['realized'],
            eur_unrealized_usd=unrealized, eur_total_usd=eur_total, gold_reference=gold,
            gold_paper_position=gold_position, gold_paper_mark=gold_mark,
            gold_paper_realized_usd=gold_realized, gold_paper_unrealized_usd=gold_unrealized,
            gold_paper_total_usd=gold_total, gold_signal=self.gold_idea['direction'],
            gold_ema50=self.gold_idea['ema50'],
            gold_last_closed_bar=datetime.fromtimestamp(self.gold_idea['bar'], timezone.utc).isoformat(),
            broker_equity_usd=equity, initial_broker_equity_usd=state['initial_broker_equity'],
            observed_account_plus_eur_equity_usd=equity + eur_total,
            combined_virtual_equity_usd=combined,
            combined_change_since_start_usd=combined - state['initial_broker_equity'],
            other_broker_positions=other_count, broker_cash_flows_not_adjusted=True,
            spread_included=True, funding_excluded=True, slippage_excluded=True,
            margin_closeout_excluded=True, stop_loss=None, take_profit=None,
            gold_fits_analogous_10pct_notional_cap=(
                gold_position['size'] * gold_position['entry'] <= equity * .1) if gold_position else None,
            fits_analogous_gold_10pct_notional_cap=(
                position['size'] * position['entry'] <= equity * .1) if position else None)
        self.ledger.save_cycle(state, events, snapshot, now)
        for event in events:
            fields = dict(event)
            name = fields.pop('event')
            self.log(name, virtual_only=True, timeframe='DAY', **fields)
        self.log('paper_portfolio_heartbeat', **snapshot)


def observer_loop(env, directory, stop, login_gate, *, log=emit):
    # Stagger session creation. Share only login pacing with the GOLD client;
    # sessions, SQLite connections and every error path are independent.
    if stop.wait(3):
        return
    ledger, worker = None, None
    try:
        while not stop.is_set():
            try:
                if worker is None:
                    paper_env = dict(env, SMART_MODE='preview')
                    api = Broker(paper_env, paper_market_data=True, login_gate=login_gate)
                    gold_size = float(env.get('SMART_PAPER_GOLD_SIZE', '0.05'))
                    ledger = PaperLedger(str(directory / 'gold-eurusd-daily-paper.sqlite'), api.account, gold_size)
                    worker = PaperWorker(api, ledger, log=log)
                    log('eurusd_paper_started', strategy=STRATEGY, virtual_only=True,
                        timeframe='DAY', account_ending=api.account[-4:],
                        resuming=ledger.state['started_at'] is not None, broker_orders_enabled=False,
                        eur_size_policy='broker_minimum', gold_size=gold_size, funding_excluded=True)
                worker.cycle()
            except Wait as error:
                log('eurusd_paper_wait', reason=str(error))
            except Transport as error:
                log('eurusd_paper_transport_wait', status=error.status)
            except Exception as error:
                log('eurusd_paper_error', error_type=type(error).__name__,
                    reason=str(error) if isinstance(error, Halt) else 'invalid_paper_runtime_data')
            stop.wait(60)
    finally:
        if ledger:
            ledger.db.close()


def start_observer(env, directory, stop, login_gate):
    thread = threading.Thread(target=observer_loop, args=(env, directory, stop, login_gate),
                              name='eurusd-paper-readonly', daemon=True)
    thread.start()
    return thread
