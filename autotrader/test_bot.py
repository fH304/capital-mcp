import copy
import os
import tempfile
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from autotrader.bot import API, Halt, Ledger, Skip, Worker, normalize_market, plan, signal, stamp, stepped

NOW = 1800000000


def utc(t):
    return datetime.fromtimestamp(t, timezone.utc).isoformat()


def market():
    return {
        "instrument": {"epic": "GOLD", "type": "COMMODITIES", "currency": "USD", "lotSize": 1,
                       "marginFactor": 10, "marginFactorUnit": "PERCENTAGE"},
        "snapshot": {"marketStatus": "TRADEABLE", "marketModes": ["REGULAR"], "delayTime": 0,
                     "updateTimeUTC": utc(NOW), "bid": 100, "offer": 100.01, "scalingFactor": 1},
        "dealingRules": {name: {"value": value, "unit": unit} for name, value, unit in [
            ("minStepDistance", .01, "POINTS"), ("minDealSize", .01, "POINTS"),
            ("minSizeIncrement", .01, "POINTS"), ("maxDealSize", 1000, "POINTS"),
            ("minStopOrProfitDistance", .01, "PERCENTAGE"), ("maxStopOrProfitDistance", 60, "PERCENTAGE")]} }


def bars():
    result = []
    for i in range(120):
        p = 100 - .1 * i if i < 119 else 140
        result.append({"snapshotTimeUTC": utc(NOW - (120 - i) * 900),
                       "closePrice": {"bid": p, "ask": p + .02},
                       "highPrice": {"bid": p + 1, "ask": p + 1.02},
                       "lowPrice": {"bid": p - 1, "ask": p - .98}})
    return result


class StrategyTests(unittest.TestCase):
    def test_closed_bar_crossover_and_exclude_forming_bar(self):
        data = bars()
        idea = signal(data, NOW)
        self.assertEqual(idea["direction"], "BUY")
        unfinished = copy.deepcopy(data[-1])
        unfinished["snapshotTimeUTC"] = utc(NOW)
        self.assertEqual(signal(data + [unfinished], NOW), idea)

    def test_stale_duplicate_and_gap_rejected(self):
        for rows, clock in [(bars(), NOW + 3600), (bars() + [bars()[-1]], NOW), (bars()[:110] + bars()[111:], NOW)]:
            with self.assertRaises(Skip):
                signal(rows, clock)

    def test_risk_size_and_increment(self):
        p = plan(market(), {"direction": "BUY", "atr": 1}, 1000, 1000, NOW)
        self.assertLessEqual(p["risk"], 2.5)
        self.assertLessEqual(p["size"] * p["entry"], 500)
        self.assertEqual(p["profitDistance"], 2 * p["stopDistance"])
        self.assertEqual(stepped(.029, .01), .02)

    def test_bad_market_conditions_and_minimum_size(self):
        mutations = [lambda m: m["snapshot"].update(marketStatus="CLOSED"),
                     lambda m: m["snapshot"].update(updateTimeUTC=utc(NOW - 121)),
                     lambda m: m["snapshot"].update(delayTime=15),
                     lambda m: m["instrument"].update(currency="JPY"),
                     lambda m: m["instrument"].update(lotSize=100),
                     lambda m: m["snapshot"].update(offer=105),
                     lambda m: m["dealingRules"]["minDealSize"].update(value=10)]
        for mutate in mutations:
            m = market()
            mutate(m)
            with self.assertRaises(Skip):
                plan(m, {"direction": "BUY", "atr": 1}, 1000, 1000, NOW)

    def test_live_configuration_refused(self):
        with patch.dict(os.environ, {"CAP_ENV": "live"}):
            with self.assertRaises(Halt):
                API("123")

    def test_account_timezone_used_for_single_market_snapshot(self):
        m = market()
        del m["snapshot"]["updateTimeUTC"]
        m["snapshot"]["updateTime"] = utc(NOW + 4 * 3600).replace("+00:00", "")
        self.assertEqual(stamp(normalize_market(m, 4)["snapshot"]["updateTimeUTC"]), NOW)


class FakeAPI:
    account = "123"
    posts = 0

    def get(self, path):
        if path == "/session":
            return {"accountId": self.account}
        if path == "/accounts":
            return {"accounts": [{"accountId": self.account, "currency": "USD", "status": "ENABLED",
                                  "balance": {"balance": 1000, "available": 1000}}]}
        if path == "/positions":
            return {"positions": []}
        if path == "/workingorders":
            return {"workingOrders": []}
        if path.startswith("/prices/"):
            return {"prices": bars()}
        if path.startswith("/markets/"):
            return market()
        if path.startswith("/confirms/"):
            return {"dealStatus": "REJECTED"}
        raise AssertionError(path)

    def post_position(self, p):
        self.posts += 1
        raise TimeoutError("Simulated lost response after sending order")


class StateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = self.temp.name + "/state.sqlite"

    def tearDown(self):
        self.temp.cleanup()

    def test_reservation_survives_restart(self):
        s = Ledger(self.path, "demo", "123")
        self.assertTrue(s.reserve("GOLD:bar", {"epic": "GOLD"}))
        s.db.close()
        s = Ledger(self.path, "demo", "123")
        self.assertFalse(s.reserve("GOLD:bar", {}))
        self.assertEqual(len(s.trades("pending")), 1)
        with self.assertRaises(Halt):
            Worker(FakeAPI(), s, "demo").cycle(NOW)

    def test_daily_loss_latch_survives_rebound_and_restart(self):
        s = Ledger(self.path, "paper", "123")
        self.assertFalse(s.day(1000, NOW)["stopped"])
        self.assertTrue(s.day(979, NOW + 1)["stopped"])
        s.db.close()
        s = Ledger(self.path, "paper", "123")
        self.assertTrue(s.day(1100, NOW + 2)["stopped"])
        self.assertFalse(s.day(1100, NOW + 86400)["stopped"])

    def test_scope_change_refused(self):
        Ledger(self.path, "paper", "123").db.close()
        with self.assertRaises(Halt):
            Ledger(self.path, "demo", "123")

    def test_paper_never_submits_and_no_duplicate_after_restart(self):
        s, api = Ledger(self.path, "paper", "123"), FakeAPI()
        Worker(api, s, "paper").cycle(NOW)
        self.assertEqual(api.posts, 0)
        self.assertEqual(len(s.trades("open")), 1)
        s.db.close()
        s = Ledger(self.path, "paper", "123")
        Worker(api, s, "paper").cycle(NOW)
        self.assertEqual(len(s.trades("open")), 1)
        self.assertEqual(api.posts, 0)

    def test_timeout_is_not_retried_after_restart(self):
        s, api = Ledger(self.path, "demo", "123"), FakeAPI()
        with self.assertRaises(TimeoutError):
            Worker(api, s, "demo").cycle(NOW)
        self.assertEqual(api.posts, 1)
        s.db.close()
        s = Ledger(self.path, "demo", "123")
        with self.assertRaises(Halt):
            Worker(api, s, "demo").cycle(NOW)
        self.assertEqual(api.posts, 1)

    def test_demo_accepted_and_protection_checked(self):
        class Accepted(FakeAPI):
            def post_position(self, p):
                self.posts += 1
                return {"dealReference": "o_test"}

            def get(self, path):
                if path.startswith("/confirms/"):
                    return {"dealStatus": "ACCEPTED", "affectedDeals": [{"dealId": "d_test", "status": "OPENED"}]}
                if path == "/positions/d_test":
                    return {"position": {"size": .22, "stopLevel": 90, "profitLevel": 120}}
                return super().get(path)
        s, api = Ledger(self.path, "demo", "123"), Accepted()
        with patch("autotrader.bot.time.sleep"):
            Worker(api, s, "demo").cycle(NOW)
        self.assertEqual(api.posts, 1)
        self.assertEqual(s.trades("open")[0][1]["deal_id"], "d_test")

    def test_wrong_active_account_blocks_before_any_order(self):
        class Wrong(FakeAPI):
            def get(self, path):
                return {"accountId": "999"} if path == "/session" else super().get(path)
        s, api = Ledger(self.path, "demo", "123"), Wrong()
        with self.assertRaises(Halt):
            Worker(api, s, "demo").cycle(NOW)
        self.assertEqual(api.posts, 0)


if __name__ == "__main__":
    unittest.main()
