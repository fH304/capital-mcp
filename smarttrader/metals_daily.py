"""Coordinate the existing GOLD journal with one explicitly sized SILVER trade.

Uses the original durable confirmation engine, daily signal rules and demo-only
transport. No forex orders or size conversions are enabled by this profile.
"""
import argparse
import fcntl
import os
import signal
import threading
from pathlib import Path

from .ema50_daily import Broker, Halt, Journal, LoginGate, Transport, Wait, Worker, emit, num


class Metals:
    def __init__(self, api, workers, *, log=emit):
        self.api, self.workers, self.log = api, workers, log
        if len({w.epic for w in workers}) != len(workers):
            raise Halt('duplicate_metals_worker')
        if {w.epic for w in workers} != {'GOLD', 'SILVER'}:
            raise Halt('gold_and_silver_workers_required')
        for worker in self.workers:
            worker.exposure_check = self.positions

    def halt(self, reason):
        for worker in self.workers:
            worker.journal.state['halt'] = worker.journal.state['halt'] or reason
            worker.journal.save()
        raise Halt(reason)

    def positions(self):
        positions = self.api.read('/positions')['positions']
        orders = self.api.read('/workingorders')['workingOrders']
        if not isinstance(positions, list) or not isinstance(orders, list):
            self.halt('incomplete_metals_account_lists')
        owned = [(w.epic, w.journal.state['position']) for w in self.workers
                 if w.journal.state['position']]
        expected = {p['deal_id']:(epic,p) for epic,p in owned}
        if len(expected) != len(owned) or orders or len(positions) != len(expected):
            self.halt('foreign_or_missing_metals_exposure')
        seen = set()
        for actual in positions:
            p, market = actual['position'], actual['market']
            deal = p['dealId']
            if deal not in expected or deal in seen:
                self.halt('foreign_or_duplicate_metals_position')
            seen.add(deal)
            epic, record = expected[deal]
            if (market['epic'] != epic or p['direction'] != record['direction']
                    or num(p['size']) != record['size'] or p.get('currency') != 'USD'
                    or num(p.get('contractSize')) != 1 or p.get('stopLevel') is not None
                    or p.get('profitLevel') is not None):
                self.halt('owned_metals_position_changed')

    def cycle(self):
        for worker in self.workers:
            if worker.journal.state['halt']:
                self.halt(worker.journal.state['halt'])
        # Recover every pending order before treating any broker exposure as
        # foreign. A crash after a SILVER fill cannot make GOLD close or reopen.
        self.workers[0].account()
        for worker in self.workers:
            worker.recover()
        self.positions()
        for worker in self.workers:
            try:
                worker.cycle()
            except Wait as error:
                self.log('metals_wait', epic=worker.epic, reason=str(error))
            except Transport as error:
                pending = worker.journal.state['pending']
                if pending and not pending.get('reference'):
                    self.halt('uncertain_submission_no_automatic_resend')
                self.log('metals_transport_wait', epic=worker.epic, status=error.status,
                         pending=bool(pending))
            if worker.journal.state['pending']:
                return  # Resolve this mutation before another asset can trade.

    def transport_failure(self):
        if any(w.journal.state['pending'] and not w.journal.state['pending'].get('reference')
               for w in self.workers):
            self.halt('uncertain_submission_no_automatic_resend')


def main():
    parser = argparse.ArgumentParser(description='Daily GOLD and SILVER EMA50, demo only')
    parser.add_argument('--once', action='store_true')
    args = parser.parse_args()
    env, mode = os.environ, os.environ.get('SMART_MODE', 'preview')
    if mode not in {'preview', 'demo'} or args.once and mode != 'preview':
        raise Halt('preview_or_continuous_demo_only')
    silver_size = num(float(env['SMART_SILVER_SIZE']))
    gold_size = num(float(env['SMART_GOLD_SIZE'])) if env.get('SMART_GOLD_SIZE') else None
    os.umask(0o077)
    directory = Path(env.get('SMART_STATE_DIR', '/var/data/smarttrader'))
    lock_path = Path(env.get('BOT_STATE_PATH', '/var/data/demo.sqlite') + '.lock')
    if not directory.is_absolute() or not lock_path.is_absolute():
        raise Halt('absolute_persistent_paths_required')
    directory.mkdir(parents=True, exist_ok=True)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, 'a') as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        gate = LoginGate()
        api = Broker(env, login_gate=gate, market_epics=('GOLD', 'SILVER'))
        workers = []
        try:
            for epic, requested in [('GOLD', gold_size), ('SILVER', silver_size)]:
                journal = Journal(str(directory / (epic.lower() + '-daily-ema50-' + mode + '.sqlite')),
                                  api.account, mode, epic)
                workers.append(Worker(api, journal, mode, epic=epic, requested_size=requested))
            portfolio = Metals(api, workers)
            stop = threading.Event()
            for sig in (signal.SIGTERM, signal.SIGINT):
                signal.signal(sig, lambda *_: stop.set())
            emit('metals_started', mode=mode, account_ending=api.account[-4:], timeframe='DAY',
                 epics=['GOLD', 'SILVER'], gold_requested_size=gold_size,
                 silver_requested_size=silver_size, stop_loss=None, take_profit=None, ai_calls=False)
            if env.get('SMART_EURUSD_PAPER') == '1' and not args.once:
                try:
                    from .eurusd_paper import start_observer
                    start_observer(dict(env), directory, stop, gate)
                except Exception as error:
                    emit('eurusd_paper_error', error_type=type(error).__name__,
                         reason='observer_start_failed_metals_continue')
            while not stop.is_set():
                try:
                    portfolio.cycle()
                except Wait as error:
                    emit('metals_wait', reason=str(error))
                except Transport as error:
                    try:
                        portfolio.transport_failure()
                        emit('metals_transport_wait', status=error.status)
                    except Halt as halted:
                        emit('metals_halted', reason=str(halted))
                except Exception as error:
                    reason = str(error) if isinstance(error, Halt) else 'invalid_metals_runtime_data'
                    try:
                        portfolio.halt(reason)
                    except Halt:
                        pass
                    emit('metals_halted', reason=reason, error_type=type(error).__name__)
                if args.once:
                    return
                stop.wait(60)
        finally:
            for worker in workers:
                worker.journal.db.close()


if __name__ == '__main__':
    main()
