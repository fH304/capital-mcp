"""One minimum-size GOLD position, closed D1 EMA50 signals, demo endpoint only.

This opt-in strategy has no price stop or take-profit. Its exit is a confirmed
opposite daily close. It never calls an AI provider or renews a paid trial.
"""
import argparse
import fcntl
import json
import math
import os
import re
import signal
import sqlite3
import threading
import time
from datetime import datetime, timezone
from decimal import Decimal, ROUND_CEILING
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, build_opener

from .candles import utc_stamp
from .news import NoRedirect

BASE = 'https://demo-api-capital.backend-capital.com/api/v1'
STRATEGY = 'gold_daily_ema50_v1'


class Halt(RuntimeError):
    """A persistent ownership/order problem; never resubmit automatically."""


class Wait(RuntimeError):
    """No executable signal or quote yet."""


class Transport(RuntimeError):
    def __init__(self, status=None):
        self.status = status
        super().__init__('broker_transport_failure')


def emit(event, **fields):
    print(json.dumps(dict(utc=datetime.now(timezone.utc).isoformat(),
                          event=event, **fields), allow_nan=False), flush=True)


def num(value, positive=True):
    if type(value) not in (int, float) or not math.isfinite(value) or (positive and value <= 0):
        raise Halt('invalid_broker_number')
    return value


def ident(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,150}', value):
        raise Halt('invalid_broker_identifier')
    return value


def daily_signal(rows, now):
    """SMA-seeded EMA50 of BID closes, matching the comparison backtest."""
    bars, seen = [], set()
    if not isinstance(rows, list):
        raise Halt('invalid_daily_history')
    for row in rows:
        t = utc_stamp(row['snapshotTimeUTC'])
        if t in seen or t > now or t % 86400:
            raise Halt('duplicate_or_invalid_daily_timestamp')
        seen.add(t)
        if t + 86400 > now:
            continue
        bid, ask = num(row['closePrice']['bid']), num(row['closePrice']['ask'])
        if ask < bid:
            raise Halt('inverted_daily_close')
        bars.append((t, bid))
    bars.sort()
    if len(bars) < 250:
        raise Wait('need_250_completed_daily_bars')
    if not 0 <= now - bars[-1][0] - 86400 <= 4 * 86400:
        raise Wait('daily_history_stale')
    ema = sum(c for _, c in bars[:50]) / 50
    for _, close in bars[50:]:
        ema += 2 / 51 * (close - ema)
    t, close = bars[-1]
    return dict(bar=t, close=close, ema50=ema,
                direction='BUY' if close > ema else 'SELL' if close < ema else None)


def quote(data, now, offset):
    ins, s = data['instrument'], data['snapshot']
    if (ins['epic'] != 'GOLD' or ins['type'] != 'COMMODITIES'
            or ins['currency'] != 'USD' or num(ins['lotSize']) != 1
            or num(s['scalingFactor']) != 1):
        raise Halt('unsupported_gold_contract')
    if s['marketStatus'] != 'TRADEABLE' or 'REGULAR' not in s['marketModes']:
        raise Wait('gold_market_closed')
    t = (utc_stamp(s['updateTimeUTC']) if s.get('updateTimeUTC') else
         utc_stamp(s['updateTime']) - offset * 3600)
    if s['delayTime'] != 0 or not 0 <= now - t <= 120:
        raise Wait('gold_quote_stale')
    bid, ask = num(s['bid']), num(s['offer'])
    if ask < bid:
        raise Halt('inverted_gold_quote')
    return dict(bid=bid, ask=ask)


def minimum_order(data, direction, balance, available, now, offset):
    q = quote(data, now, offset)
    rules, ins = data['dealingRules'], data['instrument']
    def quantity(name):
        if rules[name]['unit'] != 'POINTS':
            raise Halt('unsupported_size_rule')
        return Decimal(str(num(rules[name]['value'])))
    minimum, step, maximum = (quantity(k) for k in
                              ('minDealSize', 'minSizeIncrement', 'maxDealSize'))
    size = (minimum / step).to_integral_value(rounding=ROUND_CEILING) * step
    if size > maximum or direction not in ('BUY', 'SELL'):
        raise Halt('invalid_minimum_order')
    if ins['marginFactorUnit'] != 'PERCENTAGE':
        raise Halt('unsupported_margin_rule')
    price = q['ask'] if direction == 'BUY' else q['bid']
    margin = num(ins['marginFactor']) / 100
    if margin > 1 or balance <= 0 or available <= 0:
        raise Wait('insufficient_demo_funds')
    # Select the broker minimum only; never increase leverage or top up funds.
    # These entry exposure limits are not stop-loss guarantees.
    if float(size) * price > balance * .1 or float(size) * price * margin > available * .1:
        raise Wait('minimum_size_exceeds_test_exposure_limit')
    return dict(epic='GOLD', direction=direction, size=float(size), guaranteedStop=False)


class LoginGate:
    """Share the API-key login rate limit across otherwise separate sessions."""
    def __init__(self, *, clock=time.monotonic, sleep=time.sleep):
        self.lock, self.last = threading.Lock(), float('-inf')
        self.clock, self.sleep = clock, sleep

    def wait(self):
        with self.lock:
            self.sleep(max(0, 1.1 - (self.clock() - self.last)))
            self.last = self.clock()


class Broker:
    def __init__(self, env, *, opener=None, clock=time.time, sleep=time.sleep,
                 paper_market_data=False, login_gate=None):
        if env.get('CAP_ENV') != 'demo':
            raise Halt('demo_environment_required')
        self.account = ident(env['BOT_ACCOUNT_ID'])
        self.armed = env.get('SMART_MODE') == 'demo'
        self.paper_market_data, self.login_gate = paper_market_data, login_gate
        if paper_market_data and self.armed:
            raise Halt('paper_client_must_be_unarmed')
        if self.armed and (env.get('SMART_ARMED') != 'DEMO_ONLY'
                           or env.get('SMART_EXCLUSIVE_ACCOUNT') != self.account):
            raise Halt('exclusive_demo_arming_required')
        self.key, self.user, self.password = (env[k] for k in
                                              ('CAP_API_KEY', 'CAP_IDENTIFIER', 'CAP_API_PASSWORD'))
        if not all((self.key, self.user, self.password)):
            raise Halt('broker_credentials_missing')
        self.opener = opener or build_opener(NoRedirect()).open
        self.clock, self.sleep = clock, sleep
        self.headers, self.offset = {}, 0
        self.last_request = self.last_login = float('-inf')

    def request(self, method, path, payload=None):
        allowed = method == 'GET' and (path in {'/session', '/accounts', '/positions', '/workingorders',
                                               '/markets/GOLD', '/prices/GOLD?resolution=DAY&max=500'}
                   or re.fullmatch(r'/(confirms|positions)/[A-Za-z0-9_.:-]{1,150}', path))
        allowed = allowed or (self.paper_market_data and method == 'GET' and path in {
            '/markets/EURUSD', '/prices/EURUSD?resolution=DAY&max=500'})
        allowed = allowed or method == 'POST' and path == '/session'
        allowed = allowed or self.armed and (method == 'POST' and path == '/positions'
                    or method == 'DELETE' and re.fullmatch(r'/positions/[A-Za-z0-9_.:-]{1,150}', path))
        if not allowed:
            raise Halt('endpoint_outside_daily_demo_scope')
        delay = max(0, .2 - (self.clock() - self.last_request))
        if method == 'POST' and path == '/session':
            delay = max(delay, 1.1 - (self.clock() - self.last_login))
        self.sleep(delay)
        if method == 'POST' and path == '/session' and self.login_gate:
            self.login_gate.wait()
        self.last_request = self.clock()
        if method == 'POST' and path == '/session':
            self.last_login = self.last_request
        req = Request(BASE + path, method=method,
                      data=json.dumps(payload, allow_nan=False).encode() if payload is not None else None,
                      headers={'Content-Type': 'application/json', 'X-CAP-API-KEY': self.key, **self.headers})
        try:
            with self.opener(req, timeout=20) as response:
                raw, headers = response.read(2000001), response.headers
            if len(raw) > 2000000:
                raise Transport()
            result = json.loads(raw)
            if not isinstance(result, dict):
                raise Transport()
            return result, headers
        except HTTPError as error:
            raise Transport(error.code) from None
        except (URLError, OSError, TimeoutError, ValueError, UnicodeError):
            raise Transport() from None

    def login(self):
        self.headers = {}
        d, h = self.request('POST', '/session', dict(identifier=self.user,
                            password=self.password, encryptedPassword=False))
        if str(d['currentAccountId']) != self.account:
            raise Halt('authenticated_account_mismatch')
        offset = num(d['timezoneOffset'], False)
        if not -14 <= offset <= 14 or not h.get('CST') or not h.get('X-SECURITY-TOKEN'):
            raise Halt('invalid_broker_session')
        self.offset = offset
        self.headers = {'CST': h['CST'], 'X-SECURITY-TOKEN': h['X-SECURITY-TOKEN']}

    def read(self, path):
        if not self.headers:
            self.login()
        try:
            return self.request('GET', path)[0]
        except Transport as error:
            if error.status != 401:
                raise
            self.login()
            return self.request('GET', path)[0]

    def write(self, method, path, payload=None):
        # A mutation is deliberately never retried, including after a 401.
        return self.request(method, path, payload)[0]


class Journal:
    def __init__(self, path, account, mode):
        self.db = sqlite3.connect(path)
        self.db.execute('PRAGMA synchronous=FULL')
        self.db.execute('CREATE TABLE IF NOT EXISTS journal (id INTEGER PRIMARY KEY, body TEXT NOT NULL)')
        row = self.db.execute('SELECT body FROM journal WHERE id=1').fetchone()
        scope = [STRATEGY, account, mode, 'GOLD', 'broker_minimum']
        self.state = json.loads(row[0]) if row else dict(scope=scope, position=None, pending=None, halt=None)
        if self.state['scope'] != scope:
            raise Halt('journal_scope_mismatch')
        self.save()

    def save(self):
        with self.db:
            self.db.execute('INSERT OR REPLACE INTO journal VALUES (1,?)',
                            (json.dumps(self.state, allow_nan=False),))


class Worker:
    def __init__(self, broker, journal, mode, *, clock=time.time, sleep=time.sleep, log=emit):
        self.api, self.journal, self.mode = broker, journal, mode
        self.clock, self.sleep, self.log = clock, sleep, log
        self.idea, self.idea_at, self.idea_day = None, 0, None

    def halt(self, reason):
        self.journal.state['halt'] = reason
        self.journal.save()
        raise Halt(reason)

    def recover(self):
        state = self.journal.state
        pending = state['pending']
        if not pending:
            return
        if not pending.get('reference'):
            self.halt('uncertain_submission_no_automatic_resend')
        confirmation = self.api.read('/confirms/' + ident(pending['reference']))
        if confirmation.get('dealStatus') == 'REJECTED':
            self.halt('broker_rejected_order')
        affected = confirmation.get('affectedDeals', [])
        expected_status = 'OPENED' if pending['kind'] == 'open' else 'CLOSED'
        if (confirmation.get('dealStatus') != 'ACCEPTED' or len(affected) != 1
                or affected[0].get('status') != expected_status):
            raise Wait('awaiting_broker_confirmation')
        deal = ident(affected[0]['dealId'])
        if pending['kind'] == 'open':
            actual = self.api.read('/positions/' + deal)
            pos, market, order = actual['position'], actual['market'], pending['order']
            if (pos['dealId'] != deal or market['epic'] != 'GOLD' or pos['currency'] != 'USD'
                    or num(pos['contractSize']) != 1 or pos['direction'] != order['direction']
                    or num(pos['size']) != order['size'] or pos.get('stopLevel') is not None
                    or pos.get('profitLevel') is not None):
                self.halt('confirmed_position_mismatch')
            state['position'] = dict(deal_id=deal, direction=order['direction'],
                                     size=order['size'], signal_bar=pending['bar'])
        else:
            if deal != pending['deal_id']:
                self.halt('close_confirmation_mismatch')
            if any(x['position']['dealId'] == deal for x in self.api.read('/positions')['positions']):
                raise Wait('closed_position_still_visible')
            state['position'] = None
        state['pending'] = None
        self.journal.save()
        self.log('ema50_position_opened' if pending['kind'] == 'open' else 'ema50_position_closed',
                 mode=self.mode, deal_id=deal, direction=pending.get('order', {}).get('direction'),
                 size=pending.get('order', {}).get('size'), signal_bar=pending['bar'])

    def mutate(self, pending, method, path, payload=None):
        self.journal.state['pending'] = pending
        self.journal.save()  # Reservation is durable before sending any order.
        result = self.api.write(method, path, payload)
        pending['reference'] = ident(result['dealReference'])
        self.journal.save()
        for attempt in range(5):
            try:
                self.recover()
                return
            except Wait:
                if attempt == 4:
                    raise
                self.sleep(1)

    def account(self):
        session = self.api.read('/session')
        if str(session.get('accountId', session.get('currentAccountId'))) != self.api.account:
            self.halt('active_account_changed')
        accounts = self.api.read('/accounts')['accounts']
        matching = [a for a in accounts if str(a['accountId']) == self.api.account]
        if len(matching) != 1 or any(matching[0].get(k) != v for k, v in
                                  dict(currency='USD', accountType='CFD', status='ENABLED').items()):
            self.halt('unsupported_demo_account')
        return matching[0]['balance']

    def positions(self):
        positions = self.api.read('/positions')['positions']
        orders = self.api.read('/workingorders')['workingOrders']
        if not isinstance(positions, list) or not isinstance(orders, list):
            self.halt('incomplete_account_lists')
        owned = self.journal.state['position']
        if orders or len(positions) != (1 if owned else 0):
            self.halt('foreign_or_missing_account_exposure')
        if owned:
            pos, market = positions[0]['position'], positions[0]['market']
            if (pos['dealId'] != owned['deal_id'] or market['epic'] != 'GOLD'
                    or pos['direction'] != owned['direction'] or pos['size'] != owned['size']
                    or pos.get('currency') != 'USD' or num(pos.get('contractSize')) != 1
                    or pos.get('stopLevel') is not None or pos.get('profitLevel') is not None):
                self.halt('owned_position_changed')

    def cycle(self):
        state = self.journal.state
        if state['halt']:
            raise Halt(state['halt'])
        balance = self.account()
        self.recover()
        self.positions()
        data = self.api.read('/markets/GOLD')
        now = self.clock()
        quote(data, now, self.api.offset)
        day = int(now // 86400)
        if self.idea is None or self.idea_day != day or now - self.idea_at >= 300:
            rows = self.api.read('/prices/GOLD?resolution=DAY&max=500')['prices']
            self.idea = daily_signal(rows, self.clock())
            self.idea_at, self.idea_day = self.clock(), day
        idea, owned = self.idea, state['position']
        self.log('ema50_heartbeat', mode=self.mode, epic='GOLD', timeframe='DAY',
                 last_closed_bar=datetime.fromtimestamp(idea['bar'], timezone.utc).isoformat(),
                 last_closed_bid=idea['close'], ema50=round(idea['ema50'], 6),
                 signal=idea['direction'], open_positions=1 if owned else 0,
                 balance=num(balance['balance'], False))
        if idea['direction'] is None or owned and owned['direction'] == idea['direction']:
            return
        if self.mode == 'preview':
            order = minimum_order(data, idea['direction'], num(balance['balance'], False),
                                  num(balance['available'], False), self.clock(), self.api.offset)
            self.log('ema50_preview', **order, signal_bar=idea['bar'], stop_loss=None,
                     take_profit=None, exit_policy='opposite_completed_daily_close')
            return
        if owned:
            if idea['bar'] <= owned['signal_bar']:
                self.halt('non_new_reversal_signal')
            self.mutate(dict(kind='close', deal_id=owned['deal_id'], bar=idea['bar']),
                        'DELETE', '/positions/' + ident(owned['deal_id']))
        # Close must be accepted and absent from the broker list before reversal.
        balance = self.account()
        self.positions()
        data = self.api.read('/markets/GOLD')
        if int(self.clock() // 86400) != self.idea_day:
            raise Wait('daily_boundary_changed_before_entry')
        order = minimum_order(data, idea['direction'], num(balance['balance'], False),
                              num(balance['available'], False), self.clock(), self.api.offset)
        self.mutate(dict(kind='open', order=order, bar=idea['bar']), 'POST', '/positions', order)


def main():
    parser = argparse.ArgumentParser(description='Daily GOLD EMA50 demo-only worker')
    parser.add_argument('--once', action='store_true')
    args = parser.parse_args()
    env, mode = os.environ, os.environ.get('SMART_MODE', 'preview')
    if mode not in {'preview', 'demo'} or args.once and mode != 'preview':
        raise Halt('preview_or_continuous_demo_only')
    os.umask(0o077)
    directory = Path(env.get('SMART_STATE_DIR', '/var/data/smarttrader'))
    lock_path = Path(env.get('BOT_STATE_PATH', '/var/data/demo.sqlite') + '.lock')
    if not directory.is_absolute() or not lock_path.is_absolute():
        raise Halt('absolute_persistent_paths_required')
    directory.mkdir(parents=True, exist_ok=True)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock = open(lock_path, 'a')
    try:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        login_gate = LoginGate()
        api = Broker(env, login_gate=login_gate)
        journal = Journal(str(directory / ('gold-daily-ema50-' + mode + '.sqlite')), api.account, mode)
        worker = Worker(api, journal, mode)
        stop = threading.Event()
        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, lambda *_: stop.set())
        emit('ema50_started', strategy=STRATEGY, mode=mode, epic='GOLD', timeframe='DAY',
             account_ending=api.account[-4:], size_policy='broker_minimum',
             stop_loss=None, take_profit=None, ai_calls=False)
        if env.get('SMART_EURUSD_PAPER') == '1' and not args.once:
            # The observer owns a separate unarmed client and ledger. Its failure
            # must never stop the existing GOLD manager or change its ownership.
            try:
                from .eurusd_paper import start_observer
                start_observer(dict(env), directory, stop, login_gate)
            except Exception as error:
                emit('eurusd_paper_error', error_type=type(error).__name__,
                     reason='observer_start_failed_gold_continues')
        while not stop.is_set():
            try:
                worker.cycle()
            except Wait as error:
                emit('ema50_wait', reason=str(error))
            except Transport as error:
                if journal.state['pending'] and not journal.state['pending'].get('reference'):
                    journal.state['halt'] = 'uncertain_submission_no_automatic_resend'
                    journal.save()
                emit('ema50_transport_wait', status=error.status,
                     pending=bool(journal.state['pending']))
            except Exception as error:
                journal.state['halt'] = journal.state['halt'] or (
                    str(error) if isinstance(error, Halt) else 'invalid_runtime_data')
                journal.save()
                emit('ema50_halted', reason=journal.state['halt'], error_type=type(error).__name__)
            if args.once:
                return
            stop.wait(60)
    finally:
        lock.close()


if __name__ == '__main__':
    main()
