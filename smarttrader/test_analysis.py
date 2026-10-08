import json
import tempfile
import unittest
from datetime import datetime, timezone
from urllib.error import HTTPError

from .analysis import AnalysisClient, AnalysisError, validate_recommendation

NOW = 1800000000


def context():
    return {'epic': 'EURUSD', 'bid': 1.10, 'ask': 1.101, 'quote_time': NOW,
            'articles': [{'id': 'a1', 'published_utc': datetime.fromtimestamp(NOW-60, timezone.utc).isoformat()}]}


def recommendation():
    return {'action': 'BUY', 'assessment': 'positive', 'stop_level': 1.09,
            'target_level': 1.13, 'reason': 'Evidence supports a candidate.', 'article_ids': ['a1']}


class Response:
    def __enter__(self):
        return self
    def __exit__(self, *args):
        pass
    def read(self, size):
        return json.dumps({'status': 'completed', 'output': [{'type': 'message', 'content': [
            {'type': 'output_text', 'text': json.dumps(recommendation())}]}]}).encode()


class AnalysisTests(unittest.TestCase):
    def test_valid_candidate(self):
        self.assertEqual(validate_recommendation(recommendation(), context(), NOW)['action'], 'BUY')

    def test_untrusted_output_cannot_change_risk(self):
        r = recommendation()
        r['risk_fraction'] = 1
        with self.assertRaises(AnalysisError):
            validate_recommendation(r, context(), NOW)

    def test_stale_quote_and_news_rejected(self):
        for change in ('quote', 'news'):
            c = context()
            if change == 'quote':
                c['quote_time'] -= 31
            else:
                c['articles'][0]['published_utc'] = datetime.fromtimestamp(NOW-21601, timezone.utc).isoformat()
            with self.assertRaises(AnalysisError):
                validate_recommendation(recommendation(), c, NOW)

    def test_unknown_evidence_and_unowned_close(self):
        for update in ({'article_ids': ['fabricated']}, {'action': 'CLOSE'}):
            r = recommendation()
            r.update(update)
            with self.assertRaises(AnalysisError):
                validate_recommendation(r, context(), NOW)

    def test_levels_and_bool_rejected(self):
        for value in (True, float('nan'), 1.12):
            r = recommendation()
            r['stop_level'] = value
            with self.assertRaises(AnalysisError):
                validate_recommendation(r, context(), NOW)

    def test_budget_persists_and_payload_has_no_tools(self):
        calls = []
        def opener(request, timeout):
            p = json.loads(request.data)
            self.assertNotIn('tools', p)
            self.assertFalse(p['store'])
            self.assertEqual(timeout, 25)
            calls.append(p)
            return Response()
        with tempfile.TemporaryDirectory() as d:
            path = d+'/ai.sqlite'
            client = AnalysisClient('secret', 'configured-model', path, daily_calls=1, opener=opener)
            client.analyze(context(), NOW)
            client.db.close()
            restarted = AnalysisClient('secret', 'configured-model', path, daily_calls=1, opener=opener)
            with self.assertRaisesRegex(AnalysisError, 'cap exhausted'):
                restarted.analyze(context(), NOW)
            restarted.db.close()
        self.assertEqual(len(calls), 1)

    def test_failed_request_reserved_and_secret_redacted(self):
        def opener(request, timeout):
            raise HTTPError('https://example.com/secret', 429, 'secret', {}, None)
        with tempfile.TemporaryDirectory() as d:
            client = AnalysisClient('secret', 'model', d+'/ai.sqlite', 1, opener)
            with self.assertRaises(AnalysisError) as exc:
                client.analyze(context(), NOW)
            self.assertEqual(str(exc.exception), 'OpenAI HTTP status 429')
            with self.assertRaisesRegex(AnalysisError, 'cap exhausted'):
                client.analyze(context(), NOW)
            client.db.close()

    def test_real_clock_rechecks_after_api_latency(self):
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as d:
            client = AnalysisClient('secret', 'model', d+'/ai.sqlite', opener=lambda *a, **kw: Response())
            with patch('smarttrader.analysis.time.time', side_effect=[NOW, NOW+31]):
                with self.assertRaisesRegex(AnalysisError, 'Stale market'):
                    client.analyze(context())
            client.db.close()

    def test_continuous_client_counts_usage_without_legacy_daily_cap(self):
        with tempfile.TemporaryDirectory() as d:
            client=AnalysisClient('secret','model',d+'/ai.sqlite',daily_calls=None,
                                  opener=lambda *a,**kw:Response())
            day=datetime.fromtimestamp(NOW,timezone.utc).date().isoformat()
            client.db.execute('INSERT INTO ai_budget VALUES(?,1000)',(day,))
            client.db.commit()
            try:
                self.assertEqual(client.analyze(context(),NOW)['action'],'BUY')
                self.assertEqual(client.db.execute('SELECT used FROM ai_budget').fetchone()[0],1001)
            finally:
                client.db.close()

    def test_missing_news_monitoring_cannot_authorize_entry_or_fabricate_news(self):
        from .test_universe import trending
        ctx=dict(trending(),articles=[])
        wait=dict(action='WAIT',assessment='reject',stop_level=0,target_level=0,
                  reason='Technical trend observed; recent news is missing',article_ids=[])
        self.assertEqual(validate_recommendation(wait,ctx,NOW,allow_missing_news=True)['action'],'WAIT')
        with self.assertRaises(AnalysisError):
            validate_recommendation(wait,ctx,NOW)
        for result in (recommendation(),dict(wait,article_ids=['fabricated'])):
            with self.assertRaises(AnalysisError):
                validate_recommendation(result,ctx,NOW,allow_missing_news=True)


if __name__ == '__main__':
    unittest.main()
