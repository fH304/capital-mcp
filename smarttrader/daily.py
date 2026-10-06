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
