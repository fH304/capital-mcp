"""Per-market monitoring reservations; legacy account spacing is untouched."""
import math
import sqlite3
from pathlib import Path


class MarketSchedule:
    def __init__(self, path, account, interval=0):
        if type(interval) is not int or not 0 <= interval <= 86400:
            raise ValueError('Invalid per-market analysis interval')
        self.account, self.interval = account, interval
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, timeout=10)
        self.db.execute('PRAGMA synchronous=FULL')
        self.db.execute('CREATE TABLE IF NOT EXISTS market_analysis('
                        'account TEXT,epic TEXT,candle REAL,attempted_at REAL,'
                        'status TEXT NOT NULL,PRIMARY KEY(account,epic,candle))')
        self.db.commit()

    def reserve(self, epic, candle, now):
        if any(type(v) not in (int, float) or not math.isfinite(v) for v in (candle, now)):
            raise ValueError('Invalid analysis timestamps')
        self.db.execute('BEGIN IMMEDIATE')
        try:
            latest = self.db.execute('SELECT candle,attempted_at FROM market_analysis '
                                     'WHERE account=? AND epic=? ORDER BY candle DESC LIMIT 1',
                                     (self.account, epic)).fetchone()
            if latest and (candle <= latest[0] or now < latest[1]+self.interval):
                self.db.rollback()
                return dict(reserved=False, reason='same_or_older_candle' if candle <= latest[0] else 'market_interval',
                            next_at=max(latest[0]+1800, latest[1]+self.interval))
            # Reserve before HTTP. A timeout or restart never repeats this
            # candle automatically, and never blocks another market's candle.
            self.db.execute('INSERT INTO market_analysis VALUES(?,?,?,?,?)',
                            (self.account, epic, candle, now, 'pending'))
            self.db.commit()
            return dict(reserved=True)
        except Exception:
            self.db.rollback()
            raise

    def finish(self, epic, candle, status):
        if status not in {'done', 'failed'}:
            raise ValueError('Invalid analysis status')
        self.db.execute('UPDATE market_analysis SET status=? WHERE account=? AND epic=? AND candle=?',
                        (status, self.account, epic, candle))
        self.db.commit()

    def close(self):
        self.db.close()
