"""Connect analysis to bounded entry plans and a durable demo order journal.

No network adapter is supplied here. A broker adapter must independently collect
account/contract data and confirm positions; model output cannot supply them.
"""
import hashlib
import json
import math
import sqlite3
import time
from pathlib import Path

from .analysis import validate_recommendation
from .policy import assessed_size


class CoordinationError(RuntimeError):
    pass


def fresh(value, now, age, label):
    if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= now-value <= age:
        raise CoordinationError('Stale or invalid '+label)


def plan_entry(recommendation, context, snapshot, now):
    """Revalidate analysis with current executable quotes and broker metadata.

    snapshot is trusted adapter data, never part of the model recommendation.
    This initial integration refuses occupied accounts until ownership and exit
    reconciliation are connected. It never proposes changing leverage.
    """
    if snapshot.get('environment') != 'demo' or not snapshot.get('account_id'):
        raise CoordinationError('Only an identified demo account is supported')
    if snapshot.get('epic') != context.get('epic'):
        raise CoordinationError('Market mismatch')
    for key in ('account_time', 'positions_time', 'orders_time', 'contract_time'):
        fresh(snapshot.get(key), now, 30, key)
    if snapshot.get('tradeable') is not True or snapshot.get('daily_stopped') is not False:
        raise CoordinationError('Market or daily entry gate is closed')
    if snapshot.get('positions') != [] or snapshot.get('working_orders') != []:
        raise CoordinationError('Account must be reconciled and empty before entry')
    # Replace old analysis prices after the model call; recheck reward/risk.
    current = dict(context, bid=snapshot.get('bid'), ask=snapshot.get('ask'),
                   quote_time=snapshot.get('quote_time'))
    result = validate_recommendation(recommendation, current, now)
    if result['action'] == 'WAIT':
        return None
    if result['action'] not in ('BUY', 'SELL'):
        raise CoordinationError('Exit reconciliation is not connected')
    bid, ask = current['bid'], current['ask']
    # sizing adds spread itself: measure from the executable closing side.
    distance = bid-result['stop_level'] if result['action'] == 'BUY' else result['stop_level']-ask
    names = ('equity', 'free_margin', 'point_value', 'quote_to_account',
             'margin_per_unit', 'min_size', 'size_step', 'max_size',
             'open_risk', 'daily_remaining_risk')
    try:
        inputs = {name: snapshot[name] for name in names}
    except KeyError:
        raise CoordinationError('Incomplete broker risk metadata') from None
    if inputs['open_risk'] != 0:
        raise CoordinationError('Empty account has inconsistent open risk')
    sizing = assessed_size(assessment=result['assessment'], validation_passed=False,
                           stop_distance=distance, spread=ask-bid, **inputs)
    return {'account_id': str(snapshot['account_id']), 'epic': current['epic'],
            'direction': result['action'], 'size': sizing['size'],
            'stop_level': result['stop_level'], 'target_level': result['target_level'],
            'planned_risk': sizing['planned_risk'], 'required_margin': sizing['required_margin'],
            'reason': result['reason'], 'article_ids': list(result['article_ids'])}


class DemoCoordinator:
    """Reserve before submission; unresolved requests block subsequent entries.

    broker.environment and broker.account_id must come from authenticated session
    verification. submit_entry returns a reference only; confirm_entry must fetch
    the actual position and return its account, epic, direction, size, stop and
    target in addition to deal_id and status='confirmed'. No automatic retry.
    """
    def __init__(self, path, account_id):
        self.account_id = str(account_id)
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, timeout=10)
        self.db.execute('PRAGMA synchronous=FULL')
        self.db.execute('CREATE TABLE IF NOT EXISTS smart_entries ('
                        'signal TEXT PRIMARY KEY, account TEXT NOT NULL, '
                        'status TEXT NOT NULL, plan TEXT NOT NULL, deal TEXT, reference TEXT)')
        if 'reference' not in {row[1] for row in self.db.execute('PRAGMA table_info(smart_entries)')}:
            self.db.execute('ALTER TABLE smart_entries ADD COLUMN reference TEXT')
        self.db.execute('CREATE TABLE IF NOT EXISTS smart_exits ('
                        'deal TEXT PRIMARY KEY, account TEXT NOT NULL, status TEXT NOT NULL, '
                        'reason TEXT NOT NULL, reference TEXT)')
        self.db.commit()

    def close(self):
        self.db.close()

    def process(self, signal_id, recommendation, context, broker, now=None, armed=False):
        if not isinstance(signal_id, str) or not signal_id.strip() or len(signal_id) > 256:
            raise CoordinationError('A stable market/candle signal ID is required')
        if broker.environment != 'demo' or str(broker.account_id) != self.account_id:
            raise CoordinationError('Authenticated demo account mismatch')
        # snapshot collection happens after analysis, immediately before entry.
        snapshot = broker.snapshot(context['epic'])
        now = time.time() if now is None else now
        if str(snapshot.get('account_id')) != self.account_id:
            raise CoordinationError('Snapshot account mismatch')
        plan = plan_entry(recommendation, context, snapshot, now)
        if plan is None:
            return {'status': 'wait'}
        if self.unresolved():
            raise CoordinationError('Unresolved order; review required')
        if armed is not True:
            return {'status': 'preview', 'plan': plan}
        signal = hashlib.sha256((self.account_id+'\0'+context['epic']+'\0'+signal_id).encode()).hexdigest()
        self.db.execute('BEGIN IMMEDIATE')
        try:
            if self.db.execute('SELECT 1 FROM smart_entries WHERE signal=?', (signal,)).fetchone():
                self.db.rollback()
                return {'status': 'duplicate'}
            if self.db.execute("SELECT 1 FROM smart_entries WHERE account=? AND status IN ('pending','uncertain')",
                               (self.account_id,)).fetchone():
                raise CoordinationError('Unresolved order; broker reconciliation required')
            self.db.execute('INSERT INTO smart_entries (signal,account,status,plan) VALUES (?,?,?,?)',
                            (signal, self.account_id, 'pending', json.dumps(plan, allow_nan=False)))
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise
        # No error/timeout is classified as rejection without broker evidence.
        try:
            reference = broker.submit_entry(dict(plan))
            if not isinstance(reference, str) or not reference or len(reference)>256:
                raise CoordinationError('Invalid order reference')
            self.db.execute('UPDATE smart_entries SET reference=? WHERE signal=?', (reference, signal))
            self.db.commit()
            confirmation = broker.confirm_entry(reference)
            keys = ('account_id', 'epic', 'direction', 'size', 'stop_level', 'target_level')
            if (not isinstance(confirmation, dict) or confirmation.get('status') != 'confirmed'
                    or not confirmation.get('deal_id')
                    or any(confirmation.get(k) != plan[k] for k in keys)):
                raise CoordinationError('Position confirmation mismatch')
            self.db.execute('UPDATE smart_entries SET status=?, deal=? WHERE signal=?',
                            ('confirmed', str(confirmation['deal_id']), signal))
            self.db.commit()
            return {'status': 'confirmed', 'deal_id': str(confirmation['deal_id']), 'plan': plan}
        except Exception:
            self.db.execute('UPDATE smart_entries SET status=? WHERE signal=?', ('uncertain', signal))
            self.db.commit()
            raise CoordinationError('Order outcome uncertain; reconcile before further entries') from None

    def owned(self):
        return {deal:json.loads(plan) for deal,plan in self.db.execute(
            "SELECT deal,plan FROM smart_entries WHERE account=? AND status='confirmed'",(self.account_id,))}

    def unresolved(self):
        return bool(self.db.execute("SELECT 1 FROM smart_entries WHERE account=? AND status IN ('pending','uncertain')",
                    (self.account_id,)).fetchone() or self.db.execute(
                    "SELECT 1 FROM smart_exits WHERE account=? AND status IN ('pending','uncertain')",(self.account_id,)).fetchone())

    def reconcile(self, state):
        if str(state.get('account_id')) != self.account_id or not isinstance(state.get('positions'),list):
            raise CoordinationError('Account reconciliation mismatch')
        owned = self.owned()
        actual = {x['position']['dealId']:x for x in state['positions']}
        if len(actual)!=len(state['positions']):
            raise CoordinationError('Duplicate position identifiers')
        with self.db:
            for deal in owned.keys()-actual.keys():
                # Broker stop/target/external close: do not invent a realized P/L.
                self.db.execute("UPDATE smart_entries SET status='closed' WHERE account=? AND deal=?",
                                (self.account_id,deal))
        changed = []
        for deal in owned.keys() & actual.keys():
            pos, market = actual[deal]['position'],actual[deal]['market']
            plan=owned[deal]
            if pos['size']!=plan['size'] or pos['direction']!=plan['direction'] or market['epic']!=plan['epic']:
                changed.append(deal)
        return dict(owned={k:v for k,v in owned.items() if k in actual and k not in changed},
                    unknown=sorted(actual.keys()-owned.keys()),changed=changed,
                    unresolved=self.unresolved())

    def close_position(self, deal, reason, broker, armed=False):
        plan=self.owned().get(deal)
        if not plan or broker.environment!='demo' or str(broker.account_id)!=self.account_id:
            raise CoordinationError('Close ownership or account mismatch')
        if armed is not True:
            return dict(status='close_preview',deal_id=deal,reason=reason)
        if reason not in {'missing_broker_stop','daily_loss_limit','stop_breached'}:
            raise CoordinationError('Exit evidence is not independently validated')
        self.db.execute('BEGIN IMMEDIATE')
        try:
            if self.db.execute('SELECT 1 FROM smart_exits WHERE deal=?',(deal,)).fetchone():
                self.db.rollback()
                return dict(status='close_already_reserved',deal_id=deal)
            self.db.execute('INSERT INTO smart_exits(deal,account,status,reason) VALUES (?,?,?,?)',
                            (deal,self.account_id,'pending',reason))
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise
        try:
            reference=broker.submit_close(deal,plan)
            if not isinstance(reference,str) or not reference or len(reference)>256:
                raise CoordinationError('Invalid close reference')
            with self.db:
                self.db.execute('UPDATE smart_exits SET reference=? WHERE deal=?',(reference,deal))
            result=broker.confirm_close(reference,deal)
            if result != dict(status='closed',account_id=self.account_id,deal_id=deal):
                raise CoordinationError('Close outcome mismatch')
            with self.db:
                self.db.execute("UPDATE smart_exits SET status='closed' WHERE deal=?",(deal,))
                self.db.execute("UPDATE smart_entries SET status='closed' WHERE account=? AND deal=?",(self.account_id,deal))
            return result
        except Exception:
            with self.db:
                self.db.execute("UPDATE smart_exits SET status='uncertain' WHERE deal=?",(deal,))
            raise CoordinationError('Close outcome uncertain; no automatic resend') from None
