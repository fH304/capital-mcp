import io
import json
import tempfile
import unittest
from urllib.error import HTTPError
from smarttrader.news import NewsClient, NewsError, normalize

NOW = 1791100000

def article(age=0):
    from datetime import datetime, timezone
    return {'date': datetime.fromtimestamp(NOW-age, timezone.utc).isoformat(),
            'title': 'Fixture headline', 'link': 'https://example.org/article', 'content': 'Untrusted text'}

class NewsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = self.tmp.name + '/news.sqlite'
        self.calls = 0
    def tearDown(self):
        self.tmp.cleanup()
    def opener(self, req, timeout):
        self.calls += 1
        return io.BytesIO(json.dumps([article()]).encode())
    def client(self, **kwargs):
        return NewsClient('secret-fixture', self.path, opener=self.opener, **kwargs)
    def test_stale_and_future(self):
        for age, reason in [(21601,'stale_article'),(-61,'future_publication')]:
            self.assertEqual(normalize([article(age)], NOW)['rejected'], [reason])
    def test_no_timezone_rejected(self):
        row = article(); row['date'] = '2026-10-04T12:00:00'
        self.assertEqual(normalize([row],NOW)['rejected'], ['malformed_article'])
    def test_deduplicate(self):
        self.assertEqual(len(normalize([article(),article()],NOW)['articles']),1)
    def test_cache_rechecks_age(self):
        c = self.client(interval=30000)
        c.fetch(now=NOW)
        self.assertEqual(c.fetch(now=NOW+21601)['articles'], [])
        self.assertEqual(self.calls,1)
    def test_budget_persists_across_restart(self):
        c = self.client()
        for i in range(4):
            self.assertEqual(c.fetch(symbol=str(i),now=NOW)['status'],'fetched')
        c.db.close()
        c = self.client()
        self.assertEqual(c.fetch(symbol='fifth',now=NOW)['status'],'daily_budget_exhausted')
        self.assertEqual(self.calls,4)
    def test_errors_hide_credential_and_consume_budget(self):
        def fail(req,timeout):
            raise HTTPError(req.full_url,401,'secret-fixture',{},None)
        c = NewsClient('secret-fixture',self.path,opener=fail)
        with self.assertRaises(NewsError) as caught:
            c.fetch(now=NOW)
        self.assertNotIn('secret-fixture',str(caught.exception))
        self.assertEqual(c.fetch(now=NOW+1)['status'],'retry_wait')
        self.assertEqual(c.db.execute('SELECT used FROM budget').fetchone()[0],5)
    def test_new_day_refreshes_budget(self):
        c = self.client()
        for i in range(4): c.fetch(symbol=str(i),now=NOW)
        self.assertEqual(c.fetch(symbol='new',now=NOW+86400)['status'],'fetched')
    def test_error_response_not_accepted(self):
        with self.assertRaises(NewsError): normalize({'error':'unauthorized'},NOW)

if __name__ == '__main__': unittest.main()
