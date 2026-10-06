"""EODHD news ingestion. Secret-safe errors, persistent budget, no trading calls."""
import hashlib
import json
import os
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen, HTTPRedirectHandler, build_opener


class NewsError(RuntimeError):
    pass


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Never forward a credential-bearing query to another endpoint.
        return None


def timestamp(value):
    value = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    if value.tzinfo is None:
        raise ValueError('Timezone required')
    return value.timestamp()


def normalize(rows, now, max_age=21600):
    """Preserve article ids and UTC times; never convert sentiment into orders."""
    if not isinstance(rows, list):
        raise NewsError('News response is not an article list')
    accepted, rejected, seen = [], [], set()
    for row in rows:
        try:
            if not isinstance(row, dict):
                raise ValueError('Invalid article')
            published = timestamp(row['date'])
            title = str(row.get('title', '')).strip()
            link = str(row.get('link', '')).strip()
            if not title or not link.startswith('https://'):
                raise ValueError('Missing title or secure source link')
            identity = hashlib.sha256(link.encode()).hexdigest()
            if identity in seen:
                rejected.append('duplicate')
                continue
            seen.add(identity)
            age = now - published
            if age < -60:
                rejected.append('future_publication')
                continue
            if age > max_age:
                rejected.append('stale_article')
                continue
            accepted.append({'id': identity, 'published_utc': row['date'],
                             'received_utc': datetime.fromtimestamp(now, timezone.utc).isoformat(),
                             'age_seconds': max(0, age), 'title': title[:1000],
                             'content': str(row.get('content', ''))[:12000], 'link': link,
                             'symbols': [s for s in row.get('symbols', []) if isinstance(s, str)]
                             if isinstance(row.get('symbols', []), list) else []})
        except (KeyError, ValueError, TypeError, OverflowError):
            rejected.append('malformed_article')
    return {'articles': accepted, 'rejected': rejected}


class NewsClient:
    COST = 5  # News calls consume 5 provider units, independent of article limit.

    def __init__(self, token, path, daily_units=20, interval=21600, opener=None):
        if not token or token == 'YOUR_KEY':
            raise NewsError('EODHD_API_KEY is missing')
        if daily_units < self.COST or interval < 60:
            raise NewsError('Invalid news budget or refresh interval')
        self.token, self.daily_units, self.interval = token, daily_units, interval
        self.opener = opener or build_opener(NoRedirect()).open
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, timeout=30)
        self.db.execute('PRAGMA synchronous=FULL')
        self.db.executescript('''CREATE TABLE IF NOT EXISTS budget(day TEXT PRIMARY KEY, used INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS feed(symbol TEXT PRIMARY KEY, attempted REAL NOT NULL, fetched REAL, payload TEXT);''')

    def fetch(self, symbol='EURUSD.FOREX', now=None):
        now = time.time() if now is None else now
        day = datetime.fromtimestamp(now, timezone.utc).strftime('%Y-%m-%d')
        # Reserve before network I/O, including failed requests; lock across processes.
        self.db.execute('BEGIN IMMEDIATE')
        try:
            cached = self.db.execute('SELECT attempted,fetched,payload FROM feed WHERE symbol=?', (symbol,)).fetchone()
            used = self.db.execute('SELECT used FROM budget WHERE day=?', (day,)).fetchone()
            status = None
            if cached and now - cached[0] < self.interval:
                status = 'cached' if cached[2] is not None else 'retry_wait'
            elif (used[0] if used else 0) + self.COST > self.daily_units:
                status = 'daily_budget_exhausted'
            if status:
                self.db.commit()
                data = normalize(json.loads(cached[2]), now) if cached and cached[2] else {'articles': [], 'rejected': []}
                return {'status': status, 'symbol': symbol, **data}
            self.db.execute('INSERT INTO budget VALUES (?,?) ON CONFLICT(day) DO UPDATE SET used=used+excluded.used', (day,self.COST))
            self.db.execute('INSERT INTO feed(symbol,attempted) VALUES (?,?) ON CONFLICT(symbol) DO UPDATE SET attempted=excluded.attempted', (symbol, now))
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise
        query = urlencode({'s': symbol, 'limit': 20, 'api_token': self.token, 'fmt': 'json'})
        request = Request('https://eodhd.com/api/news?' + query, headers={'Accept': 'application/json'})
        try:
            with self.opener(request, timeout=20) as response:
                raw = response.read(2_000_001)
            if len(raw) > 2_000_000:
                raise NewsError('News response exceeds size limit')
            rows = json.loads(raw)
            result = normalize(rows, now)
        except HTTPError as exc:
            # Error URLs include api_token. Never propagate original exceptions or bodies.
            raise NewsError('EODHD HTTP status ' + str(exc.code)) from None
        except (URLError, TimeoutError, OSError):
            raise NewsError('EODHD network request failed') from None
        except (ValueError, UnicodeError):
            raise NewsError('EODHD response could not be decoded') from None
        with self.db:
            self.db.execute('UPDATE feed SET fetched=?,payload=? WHERE symbol=?', (now, json.dumps(rows), symbol))
        return {'status': 'fetched', 'symbol': symbol, **result}


def main():
    """One diagnostic GET; deliberately does not import the execution worker."""
    try:
        client = NewsClient(os.environ.get('EODHD_API_KEY'),
                            os.environ.get('NEWS_STATE_PATH', '/var/data/smart-news.sqlite'))
        result = client.fetch()
        print(json.dumps({'event':'news_diagnostic', 'status': result['status'],
                          'symbol': result['symbol'], 'usable_articles': len(result['articles']),
                          'rejection_reasons': result['rejected'],
                          'published_utc': [r['published_utc'] for r in result['articles']] }), flush=True)
    except NewsError as exc:
        print(json.dumps({'event': 'news_diagnostic_error', 'reason': str(exc)}), flush=True)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
