import unittest
import hashlib
from unittest.mock import patch

from .capital import CapitalDemo, CapitalError
from .runner import Config
from .trial import APPROVED_ACCOUNT


class CryptoUniverseTests(unittest.TestCase):
    def test_catalogue_filters_type_and_deduplicates_without_guessing_epics(self):
        broker=CapitalDemo(key='k',user='u',password='p',account_id='demo-1',daily_controller=None)
        broker.account_id='demo-1'
        broker.verify_session=lambda: None
        broker.get=lambda path: {'markets':[
            {'epic':'BTCUSD','instrumentType':'CRYPTOCURRENCIES'},
            {'epic':'ABCUSD','instrumentType':'CRYPTOCURRENCIES','marketStatus':'CLOSED'},
            {'epic':'ABCUSD','instrumentType':'CRYPTOCURRENCIES'},
            {'epic':'GOLD','instrumentType':'COMMODITIES'}]}
        self.assertEqual(broker.available_crypto_epics(),['ABCUSD','BTCUSD'])
        broker.get=lambda path: {'markets':[{'epic':'bad/epic','instrumentType':'CRYPTOCURRENCIES'}]}
        with self.assertRaises(CapitalError):
            broker.available_crypto_epics()

    def test_trial_broad_market_expansion_preserves_existing_markets(self):
        with patch('smarttrader.trial.ACCOUNT_SHA256',hashlib.sha256(APPROVED_ACCOUNT.encode()).hexdigest()):
            config=Config(dict(BOT_ACCOUNT_ID=APPROVED_ACCOUNT,CAP_API_KEY='k',
                               CAP_IDENTIFIER='u',CAP_API_PASSWORD='p',SMART_MARKETS='broad',
                               SMART_DECISION_ENGINE='rules',SMART_RULES_STRATEGY='trend'))
        original=set(config.symbols)
        added=config.add_crypto_markets(['BTCUSD']+[f'COIN{i}USD' for i in range(50)])
        self.assertEqual(added,50)
        self.assertEqual(len(config.symbols),len(original)+50)
        self.assertTrue(original.issubset(config.symbols))
