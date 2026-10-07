"""Persistent equity loss gate; UTC days, baseline at first observation.

This gates entries. It does not close positions, track transfers, or guarantee a
loss ceiling during gaps. Account cash flows require separate reconciliation.
"""
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .sizing import decimal


class DailyRisk:
    def __init__(self, path, account_id, loss_limit):
        self.account = str(account_id)
        self.limit = decimal(loss_limit, 'daily loss limit')
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, timeout=10)
        self.db.execute('PRAGMA synchronous=FULL')
        self.db.execute('CREATE TABLE IF NOT EXISTS smart_daily ('
                        'account TEXT PRIMARY KEY, config TEXT NOT NULL, day TEXT NOT NULL, '
                        'baseline TEXT NOT NULL, stopped INTEGER NOT NULL)')
        self.db.commit()

    def close(self):
        self.db.close()

    def __call__(self, equity, now):
        equity = decimal(equity, 'equity')
        day = datetime.fromtimestamp(now, timezone.utc).date().isoformat()
        config = json.dumps({'limit':str(self.limit)}, sort_keys=True)
        self.db.execute('BEGIN IMMEDIATE')
        try:
            exists=self.db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='adaptive_daily'").fetchone()
            adaptive=self.db.execute('SELECT day FROM adaptive_daily WHERE account=?',(self.account,)).fetchone() if exists else None
            if adaptive and day<=adaptive[0]:
                raise ValueError('Cannot switch from adaptive risk within its active day; review required')
            row = self.db.execute('SELECT config,day,baseline,stopped FROM smart_daily WHERE account=?',
                                  (self.account,)).fetchone()
            if row and row[0] != config:
                raise ValueError('Daily limit changed; explicit state review required')
            if row and day < row[1]:
                raise ValueError('Daily clock moved backwards')
            if row and row[1] == day:
                baseline, stopped = decimal(row[2], 'baseline'), bool(row[3])
            else:
                baseline, stopped = equity, False
            # Intraday profits do not increase the configured loss allowance.
            remaining = max(0, self.limit-max(0, baseline-equity))
            stopped = stopped or remaining == 0
            self.db.execute('INSERT OR REPLACE INTO smart_daily VALUES (?,?,?,?,?)',
                            (self.account, config, day, str(baseline), int(stopped)))
            self.db.commit()
            return dict(stopped=stopped, remaining=0.0 if stopped else float(remaining),
                        day=day, baseline=float(baseline), limit=float(self.limit))
        except Exception:
            self.db.rollback()
            raise


class AdaptiveDailyRisk:
    """Deterministic experimental demo policy, not an optimized trading model.

    At most 1% of first observed daily equity, reduced to 0.5% when recent
    normalized true range is >=1.5 times its preceding median. Intraday limits
    can only decrease. Missing/stale market observations block new entries.
    """
    def __init__(self, path, account_id, epics):
        self.account=str(account_id)
        self.epics=tuple(sorted(epics))
        if not self.epics:
            raise ValueError('Adaptive policy requires markets')
        Path(path).parent.mkdir(parents=True,exist_ok=True)
        self.db=sqlite3.connect(path,timeout=10)
        self.db.execute('PRAGMA synchronous=FULL')
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS adaptive_daily (
                account TEXT PRIMARY KEY, policy TEXT NOT NULL, day TEXT NOT NULL,
                baseline TEXT NOT NULL, ceiling TEXT NOT NULL, stopped INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS risk_market (
                account TEXT NOT NULL, epic TEXT NOT NULL, observed REAL NOT NULL,
                stressed INTEGER NOT NULL, ratio REAL NOT NULL, PRIMARY KEY(account,epic));
        ''')
        self.policy=json.dumps(dict(version=1,epics=self.epics),sort_keys=True)

    def close(self):
        self.db.close()

    def update_market(self, epic, frame, now):
        from statistics import median
        from .candles import validate_frame
        if epic not in self.epics or frame.get('resolution')!='MINUTE_15':
            raise ValueError('Unexpected volatility market or timeframe')
        validate_frame(frame,now)
        bars=frame['candles']
        ranges=[max(b['h']-b['l'],abs(b['h']-previous['c']),abs(b['l']-previous['c']))/previous['c']
                for previous,b in zip(bars,bars[1:])]
        reference=median(ranges[-15:-5])
        recent=sum(ranges[-5:])/5
        # Flat old data followed by activity is treated as high volatility.
        ratio=recent/reference if reference>0 else (2.0 if recent>0 else 1.0)
        with self.db:
            self.db.execute('INSERT OR REPLACE INTO risk_market VALUES(?,?,?,?,?)',
                            (self.account,epic,now,int(ratio>=1.5),ratio))
        return dict(stressed=ratio>=1.5,ratio=ratio)

    def __call__(self, equity, now):
        from decimal import Decimal
        equity=decimal(equity,'equity')
        day=datetime.fromtimestamp(now,timezone.utc).date().isoformat()
        self.db.execute('BEGIN IMMEDIATE')
        try:
            row=self.db.execute('SELECT policy,day,baseline,ceiling,stopped FROM adaptive_daily WHERE account=?',
                                (self.account,)).fetchone()
            if row and (row[0]!=self.policy or day<row[1]):
                raise ValueError('Adaptive policy changed or clock moved backwards; review required')
            if row and row[1]==day:
                baseline,ceiling,stopped=decimal(row[2],'baseline'),decimal(row[3],'ceiling'),bool(row[4])
            else:
                baseline,ceiling,stopped=equity,equity*Decimal('.01'),False
                # Transition preserves an existing fixed day's baseline and latch.
                exists=self.db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='smart_daily'").fetchone()
                old=self.db.execute('SELECT config,day,baseline,stopped FROM smart_daily WHERE account=?',
                                    (self.account,)).fetchone() if exists else None
                if old and day<old[1]:
                    raise ValueError('Clock moved backwards')
                if old and old[1]==day:
                    baseline=decimal(old[2],'baseline')
                    ceiling=min(baseline*Decimal('.01'),decimal(json.loads(old[0])['limit'],'old limit'))
                    stopped=bool(old[3])
            observations={epic:(observed,stressed) for epic,observed,stressed in self.db.execute(
                'SELECT epic,observed,stressed FROM risk_market WHERE account=?',(self.account,))}
            ready=all(epic in observations and 0<=now-observations[epic][0]<=300 for epic in self.epics)
            stressed=any(0<=now-t<=300 and high for t,high in observations.values())
            rate=Decimal('.005') if stressed else Decimal('.01')
            ceiling=min(ceiling,min(baseline,equity)*rate)
            loss=max(Decimal(0),baseline-equity)
            remaining=max(Decimal(0),ceiling-loss)
            stopped=stopped or remaining==0
            self.db.execute('INSERT OR REPLACE INTO adaptive_daily VALUES(?,?,?,?,?,?)',
                            (self.account,self.policy,day,str(baseline),str(ceiling),int(stopped)))
            self.db.commit()
            return dict(stopped=stopped,remaining=0.0 if stopped else float(remaining),day=day,
                        baseline=float(baseline),limit=float(ceiling),ready=ready,mode='auto',
                        stressed=bool(stressed))
        except Exception:
            self.db.rollback()
            raise
