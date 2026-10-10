"""Concurrent monitoring scaffolding and deterministic exit decisions.

No broker API adapter is wired here. News signals require independently
validated evidence; a model's fear/confidence is never an exit instruction.
"""
import math
import threading
import time
from dataclasses import dataclass


@dataclass(frozen=True)
class PositionView:
    deal_id: str
    owned: bool
    direction: str
    stop_level: float
    bid: float
    ask: float
    quote_time: float
    tradeable: bool
    protected: bool
    thesis_invalidated: bool = False
    adverse_news_confirmed: bool = False
    evidence_time: float | None = None
    evidence_validated: bool = False


def exit_decision(position, now, daily_stopped=False):
    """Intent only; execution must verify account, ownership and confirmations."""
    p = position
    if not p.owned:
        return {'action': 'alert', 'reason': 'unrecognized_position'}
    if p.direction not in {'BUY', 'SELL'} or not p.deal_id:
        return {'action': 'alert', 'reason': 'invalid_position'}
    values = (p.stop_level,p.bid,p.ask,p.quote_time,now)
    if any(not math.isfinite(v) for v in values) or p.stop_level <= 0 or p.bid <= 0 or p.ask < p.bid:
        return {'action': 'alert', 'reason': 'invalid_market_data'}
    if not 0 <= now-p.quote_time <= 30:
        return {'action': 'alert', 'reason': 'stale_quote_block_new_entries'}
    if not p.tradeable:
        return {'action': 'alert', 'reason': 'market_closed_exit_unavailable'}
    reason = None
    if not p.protected:
        reason = 'missing_broker_stop'
    elif daily_stopped:
        reason = 'daily_loss_limit'
    elif (p.direction == 'BUY' and p.bid <= p.stop_level) or (p.direction == 'SELL' and p.ask >= p.stop_level):
        reason = 'stop_breached'
    elif p.evidence_validated and p.evidence_time is not None and math.isfinite(p.evidence_time) and 0 <= now-p.evidence_time <= 300:
        if p.thesis_invalidated:
            reason = 'entry_thesis_invalidated'
        elif p.adverse_news_confirmed:
            reason = 'validated_adverse_news'
    return {'action': 'close_intent' if reason else 'hold', 'reason': reason,
            'deal_id': p.deal_id}


class EntryGate:
    """Fail closed until every required feed has completed a fresh update."""
    def __init__(self, limits=None):
        self.limits = limits or {'positions': 30, 'opportunities': 120, 'news': 21600}
        self.updated, self.errors = {}, set(self.limits)
        self.daily_stopped = False
        self.lock = threading.Lock()

    def success(self, name, now):
        with self.lock:
            self.updated[name] = now
            self.errors.discard(name)

    def failure(self, name):
        with self.lock:
            self.errors.add(name)

    def stopped(self, value):
        with self.lock:
            self.daily_stopped = bool(value)

    def may_enter(self, now):
        with self.lock:
            return not self.daily_stopped and not self.errors and all(
                name in self.updated and 0 <= now-self.updated[name] <= age
                for name, age in self.limits.items())


class Monitor:
    """Independent bounded I/O jobs; a slow news request cannot stall exits.

    Jobs must enforce network timeouts and own thread-safe clients. A successful
    job must return True only with fresh/usable data; False blocks new entries.
    Positions job must keep checking exits even when entry gate is closed.
    No price feed or news article is assumed valid merely because HTTP succeeded.
    """
    def __init__(self, positions, opportunities, news, log, gate=None, intervals=None):
        self.jobs = dict(positions=positions,opportunities=opportunities,news=news)
        self.intervals = intervals or dict(positions=5,opportunities=60,news=21600)
        if set(self.intervals) != set(self.jobs) or any(not math.isfinite(x) or x <= 0 for x in self.intervals.values()):
            raise ValueError('Invalid monitoring intervals')
        self.log, self.gate = log, gate or EntryGate()
        self.stop_event = threading.Event()
        self.threads = []

    def _loop(self, name):
        while not self.stop_event.is_set():
            try:
                usable = self.jobs[name]()
                if usable is True:
                    self.gate.success(name,time.time())
                else:
                    self.gate.failure(name)
                    self.log('feed_unusable', feed=name)
            except Exception as exc:
                self.gate.failure(name)
                # Exception text can contain credential-bearing URLs.
                self.log('monitor_error',feed=name,error_type=type(exc).__name__)
            self.stop_event.wait(self.intervals[name])

    def start(self):
        if self.threads:
            raise RuntimeError('Monitor already started')
        for name in self.jobs:
            thread = threading.Thread(target=self._loop,args=(name,),name='smart-'+name,daemon=True)
            self.threads.append(thread)
            thread.start()

    def stop(self, timeout=25):
        self.stop_event.set()
        deadline = time.monotonic()+timeout
        for thread in self.threads:
            thread.join(max(0,deadline-time.monotonic()))
        if any(t.is_alive() for t in self.threads):
            raise RuntimeError('A feed did not respect its timeout')
