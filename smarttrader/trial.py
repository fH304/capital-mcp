"""One authorized demo trial; durable spend reservations, never a daily reset.

OpenAI Docs (2026-10-08): gpt-4.1-mini input $0.40/M, cached
input $0.10/M, output $1.60/M; context limit 1,047,576 tokens.
https://developers.openai.com/api/docs/models/gpt-4.1-mini
Reserve the entire context limit plus the output cap before HTTP. This is
deliberately conservative, independent of tokenizer estimates. Reconcile only
with valid provider usage; lost responses keep their full reservation.
The limit covers this worker's token charges, not taxes, hosting or other apps.
"""
import math
import sqlite3
import uuid
from pathlib import Path

APPROVED_ACCOUNT = '330438437114294558'
TRIAL_ID = '2026-10-08-all21-72h-20usd'
MODEL = 'gpt-4.1-mini-2025-04-14'
DURATION = 72 * 3600
# Original deployed checkpoint, verified in the user's 2026-10-08 Render log.
# A later deployment is not authorization for another 72 hours or another $20.
AUTHORIZED_STARTED_AT = 1791483873.5271823
LIMIT_NANO = 20 * 1_000_000_000
MAX_INPUT = 1_047_576
MAX_OUTPUT = 2000
INPUT_RATE, CACHED_RATE, OUTPUT_RATE = 400, 100, 1600  # nano USD/token
MAX_CHARGE = MAX_INPUT * INPUT_RATE + MAX_OUTPUT * OUTPUT_RATE


class TrialStopped(RuntimeError):
    pass


class TrialEntryBlocked(TrialStopped):
    """Pre-HTTP veto only; proves the entry mutation was never sent."""


class TrialBudget:
    def __init__(self, path, account, now):
        self.account = account
        self._time(now)
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, timeout=10)
        self.db.execute('PRAGMA synchronous=FULL')
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS trial_runs(
                trial_id TEXT, account TEXT, started_at REAL NOT NULL,
                ends_at REAL NOT NULL, last_seen REAL NOT NULL,
                limit_nano INTEGER NOT NULL, stop_reason TEXT,
                PRIMARY KEY(trial_id,account));
            CREATE TABLE IF NOT EXISTS trial_calls(
                id TEXT PRIMARY KEY, trial_id TEXT NOT NULL, account TEXT NOT NULL,
                epic TEXT NOT NULL, created_at REAL NOT NULL,
                reserved_nano INTEGER NOT NULL, charged_nano INTEGER,
                input_tokens INTEGER, cached_tokens INTEGER, output_tokens INTEGER);
            CREATE TABLE IF NOT EXISTS trial_metrics(
                trial_id TEXT, account TEXT, event TEXT, epic TEXT, detail TEXT,
                count INTEGER NOT NULL, PRIMARY KEY(trial_id,account,event,epic,detail));
            CREATE TABLE IF NOT EXISTS trial_equity(
                trial_id TEXT, account TEXT, initial_equity REAL NOT NULL,
                latest_equity REAL NOT NULL, observed_at REAL NOT NULL,
                open_positions INTEGER NOT NULL, PRIMARY KEY(trial_id,account));
        ''')
        # Missing state is a blocked placeholder, never a fresh funded run.
        # Preserve any surviving calls and original/replaced row for review.
        self.db.execute('INSERT OR IGNORE INTO trial_runs VALUES(?,?,?,?,?,?,?)',
                        (TRIAL_ID,account,AUTHORIZED_STARTED_AT,
                         AUTHORIZED_STARTED_AT+DURATION,now,LIMIT_NANO,'trial_state_missing'))
        self.db.commit()

    @staticmethod
    def _time(now):
        if type(now) not in (int,float) or not math.isfinite(now) or now < 0:
            raise ValueError('Invalid trial clock')

    def _status(self, now, reserve=False, mutate=True):
        row = self.db.execute('SELECT started_at,ends_at,last_seen,limit_nano,stop_reason '
                              'FROM trial_runs WHERE trial_id=? AND account=?',
                              (TRIAL_ID,self.account)).fetchone()
        if row is None:
            raise TrialStopped('trial_state_missing')
        start,end,last,limit,reason = row
        identity_ok = (self.account == APPROVED_ACCOUNT and start == AUTHORIZED_STARTED_AT
                       and end == AUTHORIZED_STARTED_AT+DURATION and limit == LIMIT_NANO)
        ledger_verified = identity_ok and reason not in {
            'trial_state_missing','trial_ledger_replaced','unauthorized_trial_account'}
        used,pending,requests = self.db.execute(
            'SELECT COALESCE(SUM(COALESCE(charged_nano,reserved_nano)),0),'
            'COALESCE(SUM(CASE WHEN charged_nano IS NULL THEN reserved_nano ELSE 0 END),0),COUNT(*) '
            'FROM trial_calls WHERE trial_id=? AND account=?', (TRIAL_ID,self.account)).fetchone()
        if not reason:
            if self.account != APPROVED_ACCOUNT:
                reason = 'unauthorized_trial_account'
            elif not identity_ok:
                reason = 'trial_ledger_replaced'
            elif now+1 < last or now < start:
                reason = 'clock_moved_backwards'
            elif now >= end:
                reason = 'duration_elapsed'
            elif used > limit or (reserve and used+MAX_CHARGE > limit):
                reason = 'budget_exhausted'
        if mutate:
            self.db.execute('UPDATE trial_runs SET last_seen=?,stop_reason=? '
                            'WHERE trial_id=? AND account=?', (max(last,now),reason,TRIAL_ID,self.account))
        return dict(trial_id=TRIAL_ID,trial_active=not bool(reason),stop_reason=reason,
                    started_at=start,ends_at=end,budget_usd=limit/1e9,
                    authorized_started_at=AUTHORIZED_STARTED_AT,
                    authorized_ends_at=AUTHORIZED_STARTED_AT+DURATION,
                    authorized_budget_usd=LIMIT_NANO/1e9,ledger_verified=ledger_verified,
                    accounted_usd_scope='original_trial' if ledger_verified else 'available_local_fragment',
                    accounted_usd=used/1e9,unreconciled_usd=pending/1e9,
                    remaining_usd=max(0,limit-used)/1e9 if ledger_verified else None,requests=requests)

    def status(self, now):
        self._time(now)
        self.db.execute('BEGIN IMMEDIATE')
        try:
            result = self._status(now)
            self.db.commit()
            return result
        except Exception:
            self.db.rollback()
            raise

    def require_active(self, now):
        status = self.status(now)
        if not status['trial_active']:
            raise TrialStopped(status['stop_reason'])
        return status

    def require_entry(self, now):
        try:
            return self.require_active(now)
        except TrialStopped as error:
            raise TrialEntryBlocked(str(error)) from None

    def reserve(self, epic, model, now):
        self._time(now)
        if model != MODEL:
            raise TrialStopped('unpriced_trial_model')
        self.db.execute('BEGIN IMMEDIATE')
        try:
            status = self._status(now,reserve=True)
            if not status['trial_active']:
                self.db.commit()  # a stop is permanent across restart/redeploy
                raise TrialStopped(status['stop_reason'])
            charge_id = uuid.uuid4().hex
            self.db.execute('INSERT INTO trial_calls VALUES(?,?,?,?,?,?,NULL,NULL,NULL,NULL)',
                            (charge_id,TRIAL_ID,self.account,epic,now,MAX_CHARGE))
            self.db.commit()
            return charge_id
        except Exception:
            self.db.rollback()
            raise

    def settle(self, charge_id, response):
        """Account usage before inspecting the model's trading recommendation."""
        usage = response.get('usage') if isinstance(response,dict) else None
        valid_model = isinstance(response,dict) and response.get('model') == MODEL
        inp = usage.get('input_tokens') if isinstance(usage,dict) else None
        out = usage.get('output_tokens') if isinstance(usage,dict) else None
        details = usage.get('input_tokens_details',{}) if isinstance(usage,dict) else None
        cached = details.get('cached_tokens',0) if isinstance(details,dict) else None
        valid = (valid_model and type(inp) is int and 0 <= inp <= MAX_INPUT
                 and type(out) is int and 0 <= out <= MAX_OUTPUT
                 and type(cached) is int and 0 <= cached <= inp)
        self.db.execute('BEGIN IMMEDIATE')
        try:
            row = self.db.execute('SELECT reserved_nano,charged_nano FROM trial_calls '
                                  'WHERE id=? AND trial_id=? AND account=?',
                                  (charge_id,TRIAL_ID,self.account)).fetchone()
            if row is None or row[1] is not None:
                raise TrialStopped('invalid_usage_reservation')
            if valid:
                cost = (inp-cached)*INPUT_RATE + cached*CACHED_RATE + out*OUTPUT_RATE
                if cost > row[0]:
                    valid = False
            if valid:
                self.db.execute('UPDATE trial_calls SET charged_nano=?,input_tokens=?,cached_tokens=?,output_tokens=? '
                                'WHERE id=?', (cost,inp,cached,out,charge_id))
            else:
                self.db.execute('UPDATE trial_runs SET stop_reason=COALESCE(stop_reason,?) '
                                'WHERE trial_id=? AND account=?',
                                ('usage_unverified',TRIAL_ID,self.account))
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise
        if not valid:
            raise TrialStopped('usage_unverified')

    def record(self, event, fields):
        if event not in {'analysis_ready','analysis_failed','entry_wait','entry_blocked',
                         'entry_preview','entry_confirmed','entry_rejected','entry_uncertain',
                         'closed','close_preview','position_closed_observed'}:
            return
        detail = str(fields.get('action') if event == 'analysis_ready' else fields.get('reason',''))[:120]
        epic = str(fields.get('epic',''))[:50]
        self.db.execute('INSERT INTO trial_metrics VALUES(?,?,?,?,?,1) '
                        'ON CONFLICT(trial_id,account,event,epic,detail) DO UPDATE SET count=count+1',
                        (TRIAL_ID,self.account,event,epic,detail))
        self.db.commit()

    def observe_account(self, now, equity, positions):
        self._time(now)
        if type(equity) not in (int,float) or not math.isfinite(equity):
            raise ValueError('Invalid observed equity')
        self.db.execute('INSERT INTO trial_equity VALUES(?,?,?,?,?,?) '
                        'ON CONFLICT(trial_id,account) DO UPDATE SET '
                        'latest_equity=excluded.latest_equity,observed_at=excluded.observed_at,'
                        'open_positions=excluded.open_positions',
                        (TRIAL_ID,self.account,equity,equity,now,positions))
        self.db.commit()

    def summary(self, now):
        return self._summary(self.status(now))

    def _summary(self, status):
        status['metrics'] = [dict(event=e,epic=p,detail=d,count=n) for e,p,d,n in self.db.execute(
            'SELECT event,epic,detail,count FROM trial_metrics WHERE trial_id=? AND account=? '
            'ORDER BY event,epic,detail', (TRIAL_ID,self.account))]
        equity=self.db.execute('SELECT initial_equity,latest_equity,observed_at,open_positions '
                               'FROM trial_equity WHERE trial_id=? AND account=?',
                               (TRIAL_ID,self.account)).fetchone()
        if equity:
            status['equity_observation']=dict(initial_equity=equity[0],latest_equity=equity[1],
                                             equity_change=equity[1]-equity[0],observed_at=equity[2],
                                             open_positions=equity[3])
        return status

    @classmethod
    def report(cls, path, account, now):
        """Reading a report cannot start, renew, or modify the trial."""
        cls._time(now)
        obj=object.__new__(cls)
        obj.account=account
        obj.db=sqlite3.connect(Path(path).resolve().as_uri()+'?mode=ro',uri=True,timeout=10)
        try:
            return obj._summary(obj._status(now,mutate=False))
        finally:
            obj.close()

    def close(self):
        self.db.close()


if __name__=='__main__':
    import argparse
    import json
    import os
    import time
    parser=argparse.ArgumentParser(description='Read the existing demo trial report without API calls')
    parser.add_argument('--report',action='store_true',required=True)
    args=parser.parse_args()
    path=Path(os.environ.get('SMART_STATE_DIR','/var/data/smarttrader'))/'trial.sqlite'
    print(json.dumps(TrialBudget.report(path,os.environ.get('BOT_ACCOUNT_ID',APPROVED_ACCOUNT),time.time()),
                     allow_nan=False,indent=2))
