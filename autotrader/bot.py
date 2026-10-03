"""Demo-only Capital worker; run python -m autotrader.bot. No third-party dependencies."""
import argparse
import json
import math
import os
import re
import sqlite3
import time
from datetime import datetime, timezone
from decimal import Decimal, ROUND_DOWN, ROUND_UP
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

BASE = "https://demo-api-capital.backend-capital.com/api/v1"
UNIVERSE = "GOLD,SILVER,US500,US100,US30,EURUSD,GBPUSD,AUDUSD,BTCUSD,ETHUSD"
TYPES = {"COMMODITIES", "INDICES", "CURRENCIES", "CRYPTOCURRENCIES"}


class Halt(RuntimeError):
    """Stop automatically; require investigation before resuming."""


class Skip(ValueError):
    """Market or signal is unsuitable; do not submit an order."""


def number(x):
    x = float(x)
    if not math.isfinite(x):
        raise Skip("Non-finite value")
    return x


def stamp(s):
    d = datetime.fromisoformat(s.replace("Z", "+00:00"))
    return d.replace(tzinfo=timezone.utc).timestamp() if d.tzinfo is None else d.timestamp()


def normalize_market(market, timezone_offset):
    """Single-market snapshot uses account-local time; offset comes from login."""
    snapshot = market["snapshot"]
    if "updateTimeUTC" not in snapshot:
        offset = number(timezone_offset)
        if not -14 <= offset <= 14:
            raise Halt("Invalid session timezone offset")
        local = stamp(snapshot["updateTime"])
        snapshot["updateTimeUTC"] = datetime.fromtimestamp(local - offset * 3600, timezone.utc).isoformat()
    return market


def log(event, **fields):
    print(json.dumps({"utc": datetime.now(timezone.utc).isoformat(), "event": event, **fields}), flush=True)


def stepped(x, step, up=False):
    step = Decimal(str(step))
    if step <= 0:
        raise Skip("Invalid broker increment")
    return float((Decimal(str(x)) / step).to_integral_value(rounding=ROUND_UP if up else ROUND_DOWN) * step)


def signal(prices, now):
    """EMA20/50 crossover; last 14-bar mean true range; closed UTC bars only."""
    rows = sorted(prices, key=lambda r: stamp(r["snapshotTimeUTC"]))
    rows = [r for r in rows if stamp(r["snapshotTimeUTC"]) + 900 <= now]
    if len(rows) < 100 or len({r["snapshotTimeUTC"] for r in rows}) != len(rows):
        raise Skip("Need 100 distinct closed bars")
    last = stamp(rows[-1]["snapshotTimeUTC"])
    if not 0 <= now - (last + 900) <= 1000:
        raise Skip("Stale or future candles")
    recent = [stamp(r["snapshotTimeUTC"]) for r in rows[-15:]]
    if any(b - a != 900 for a, b in zip(recent, recent[1:])):
        raise Skip("Gap in recent candles; wait for continuous bars")
    closes, trs = [], []
    def mid(r, key):
        b, a = number(r[key]["bid"]), number(r[key]["ask"])
        if b <= 0 or a < b:
            raise Skip("Invalid OHLC bid/ask")
        return (a + b) / 2
    for row in rows:
        h, l, c = mid(row, "highPrice"), mid(row, "lowPrice"), mid(row, "closePrice")
        if h < l or not l <= c <= h:
            raise Skip("Invalid OHLC range")
        trs.append(max(h - l, abs(h - closes[-1]), abs(l - closes[-1])) if closes else h - l)
        closes.append(c)
    fast = slow = closes[0]
    previous = None
    for c in closes[1:]:
        previous = fast - slow
        fast += 2 / 21 * (c - fast)
        slow += 2 / 51 * (c - slow)
    atr = sum(trs[-14:]) / 14
    if atr <= 0:
        raise Skip("No volatility")
    direction = "BUY" if previous <= 0 < fast - slow else "SELL" if previous >= 0 > fast - slow else None
    return {"direction": direction, "bar": rows[-1]["snapshotTimeUTC"], "atr": atr}


def plan(market, idea, equity, available, now):
    """Fail closed for currency/contract conventions not verified by this worker."""
    ins, snap, rules = market["instrument"], market["snapshot"], market["dealingRules"]
    if ins["type"] not in TYPES or ins["currency"] != "USD":
        raise Skip("Only the four asset classes with USD quote currency are supported")
    if number(ins["lotSize"]) != 1 or number(snap["scalingFactor"]) != 1:
        raise Skip("Unverified lot/scaling convention")
    if snap["marketStatus"] != "TRADEABLE" or "REGULAR" not in snap["marketModes"]:
        raise Skip("Market is closed or restricted")
    if number(snap["delayTime"]) != 0:
        raise Skip("Delayed quote")
    # updateTime may be in platform time. Never guess its offset.
    if not 0 <= now - stamp(snap["updateTimeUTC"]) <= 120:
        raise Skip("Quote older than 120 seconds")
    bid, ask = number(snap["bid"]), number(snap["offer"])
    if bid <= 0 or ask < bid:
        raise Skip("Invalid quote")
    entry = ask if idea["direction"] == "BUY" else bid
    def rule(name):
        item = rules[name]
        v = number(item["value"])
        if v <= 0:
            raise Skip("Invalid broker rule")
        if item["unit"] == "PERCENTAGE":
            return entry * v / 100
        if item["unit"] == "POINTS":
            return v
        raise Skip("Unknown broker rule unit")
    step = rule("minStepDistance")
    stop = stepped(max(2 * idea["atr"], rule("minStopOrProfitDistance") + step), step, up=True)
    profit = stepped(2 * stop, step, up=True)
    if profit > rule("maxStopOrProfitDistance") or (ask - bid) > stop * 0.1:
        raise Skip("Excessive stop distance or spread")
    if equity <= 0 or available <= 0:
        raise Skip("No funds")
    if ins["marginFactorUnit"] != "PERCENTAGE":
        raise Skip("Unknown margin convention")
    margin = number(ins["marginFactor"]) / 100
    if not 0 < margin <= 1:
        raise Skip("Invalid margin")
    risk_budget = equity * 0.0025
    # Spread allowance, at most 50% equity notional and 10% available as margin.
    size = min(risk_budget / (stop + ask - bid), equity * 0.5 / entry, available * 0.1 / (entry * margin), rule("maxDealSize"))
    size = stepped(size, rule("minSizeIncrement"))
    if size < rule("minDealSize"):
        raise Skip("Minimum broker size exceeds risk or margin budget")
    return {"epic": ins["epic"], "direction": idea["direction"], "size": size,
            "stopDistance": stop, "profitDistance": profit, "guaranteedStop": False,
            "entry": entry, "risk": size * (stop + ask - bid), "type": ins["type"]}


class API:
    def __init__(self, account):
        if os.environ.get("CAP_ENV", "demo").lower() != "demo":
            raise Halt("CAP_ENV must be demo; live trading is not implemented")
        self.account, self.headers = account, {}
        self.timezone_offset = None
        self.credentials = {k: os.environ[k] for k in ("CAP_API_KEY", "CAP_IDENTIFIER", "CAP_API_PASSWORD")}

    def raw(self, method, path, data=None):
        time.sleep(0.15)  # below global 10 requests/second, single worker
        headers = {"Content-Type": "application/json", "X-CAP-API-KEY": self.credentials["CAP_API_KEY"], **self.headers}
        req = Request(BASE + path, data=json.dumps(data).encode() if data is not None else None, headers=headers, method=method)
        try:
            with urlopen(req, timeout=20) as r:
                return json.loads(r.read()), r.headers
        except HTTPError as e:
            # Never log headers, response body or credentials.
            raise HTTPError(e.url, e.code, "Capital request failed", {}, None) from None

    def login(self):
        d, h = self.raw("POST", "/session", {"identifier": self.credentials["CAP_IDENTIFIER"], "password": self.credentials["CAP_API_PASSWORD"], "encryptedPassword": False})
        self.headers = {"CST": h["CST"], "X-SECURITY-TOKEN": h["X-SECURITY-TOKEN"]}
        if not all(self.headers.values()) or str(d["currentAccountId"]) != self.account:
            raise Halt("Unexpected account after login; no automatic account switching")
        self.timezone_offset = number(d["timezoneOffset"])

    def get(self, path):
        try:
            data = self.raw("GET", path)[0]
        except HTTPError as e:
            if e.code != 401:
                raise
            self.login()
            data = self.raw("GET", path)[0]
        if path.startswith("/markets/"):
            data = normalize_market(data, self.timezone_offset)
        return data

    def post_position(self, p):
        # Deliberately no retries: transport failure can mean an order was submitted.
        payload = {k: p[k] for k in ("epic", "direction", "size", "stopDistance", "profitDistance", "guaranteedStop")}
        return self.raw("POST", "/positions", payload)[0]


class Ledger:
    def __init__(self, path, mode, account):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript("CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY,value TEXT NOT NULL); CREATE TABLE IF NOT EXISTS trades (id TEXT PRIMARY KEY,status TEXT NOT NULL,body TEXT NOT NULL);")
        scope = f"{mode}:{account}:{UNIVERSE}:ema20-50-15m-v1"
        old = self.get("scope")
        if old is not None and old != scope:
            raise Halt("Ledger belongs to another configuration; use a different state file")
        self.put("scope", scope)

    def get(self, key):
        row = self.db.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def put(self, key, value):
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO state VALUES (?,?)", (key, json.dumps(value)))

    def trades(self, status):
        return [(r[0], json.loads(r[1])) for r in self.db.execute("SELECT id,body FROM trades WHERE status=?", (status,))]

    def reserve(self, ident, body):
        with self.db:
            return self.db.execute("INSERT OR IGNORE INTO trades VALUES (?, 'pending', ?)", (ident, json.dumps(body))).rowcount == 1

    def finish(self, ident, status, body):
        with self.db:
            self.db.execute("UPDATE trades SET status=?,body=? WHERE id=?", (status, json.dumps(body), ident))

    def day(self, equity, now):
        day = datetime.fromtimestamp(now, timezone.utc).date().isoformat()
        saved = self.get("day")
        if saved is None or saved["date"] != day:
            self.put("day", {"date": day, "start": equity, "orders": 0, "stopped": False})
        saved = self.get("day")
        if equity <= saved["start"] * 0.98:
            saved["stopped"] = True
            self.put("day", saved)
        return saved


class Worker:
    def __init__(self, api, ledger, mode):
        self.api, self.store, self.mode = api, ledger, mode

    def cycle(self, now=None):
        fixed_clock = now is not None
        now = now or time.time()
        if self.store.get("halt") or self.store.trades("pending"):
            raise Halt("Persisted halt or unresolved order; inspect ledger and Capital before restart")
        session = self.api.get("/session")
        if str(session["accountId"] if "accountId" in session else session["currentAccountId"]) != self.api.account:
            raise Halt("Active account changed")
        accounts = self.api.get("/accounts")["accounts"]
        account = next(a for a in accounts if str(a["accountId"]) == self.api.account)
        if account["currency"] != "USD" or account["status"] != "ENABLED":
            raise Halt("Need an enabled USD demo account")
        equity = number(account["balance"]["balance"])
        available = number(account["balance"]["available"])
        positions = self.api.get("/positions")["positions"]
        orders = self.api.get("/workingorders")["workingOrders"]
        if orders:
            raise Halt("Existing working orders require review")
        opened = self.store.trades("open")
        if self.mode == "demo":
            known = {t["deal_id"] for _, t in opened}
            actual = {p["position"]["dealId"] for p in positions}
            if actual - known:
                raise Halt("Manual/unrecognized position; do not mix this worker with manual trading")
            for ident, t in opened:
                if t["deal_id"] not in actual:
                    self.store.finish(ident, "closed", t)
            opened = self.store.trades("open")
            for item in positions:
                pos = item["position"]
                if pos.get("stopLevel") is None or pos.get("profitLevel") is None:
                    raise Halt("Position missing broker protection; review immediately in Capital")
        elif positions:
            raise Halt("Paper testing requires no existing broker positions")
        if self.mode == "paper":
            if self.store.get("paper_cash") is None:
                self.store.put("paper_cash", equity)
            cash = self.store.get("paper_cash")
            unrealized = 0
            for ident, t in opened:
                m = self.api.get("/markets/" + t["epic"])
                s = m["snapshot"]
                if s["marketStatus"] != "TRADEABLE" or number(s["delayTime"]) != 0:
                    unrealized += t.get("mark_pnl", 0)
                    continue
                if not 0 <= now - stamp(s["updateTimeUTC"]) <= 120:
                    raise Skip("Stale paper exit quote")
                exit_price = number(s["bid"] if t["direction"] == "BUY" else s["offer"])
                move = (exit_price - t["entry"]) * (1 if t["direction"] == "BUY" else -1)
                pnl = move * t["size"]
                if move <= -t["stopDistance"] or move >= t["profitDistance"]:
                    t["exit"], t["pnl"] = exit_price, pnl
                    # Closure and realized balance update must commit together.
                    with self.store.db:
                        self.store.db.execute("UPDATE trades SET status='closed',body=? WHERE id=?", (json.dumps(t), ident))
                        cash += pnl
                        self.store.db.execute("INSERT OR REPLACE INTO state VALUES ('paper_cash',?)", (json.dumps(cash),))
                    log("paper_closed", epic=t["epic"], pnl=pnl)
                else:
                    unrealized += pnl
                    t["mark_pnl"] = pnl
                    self.store.finish(ident, "open", t)
            equity, available = cash + unrealized, max(0, cash + unrealized)
            opened = self.store.trades("open")
        day = self.store.day(equity, now)
        log("heartbeat", mode=self.mode, equity=round(equity, 2), open_positions=len(opened), daily_stopped=day["stopped"])
        if day["stopped"] or day["orders"] >= 4 or equity <= 0 or len(opened) >= 2:
            return
        for epic in UNIVERSE.split(","):
            if len(opened) >= 2 or day["orders"] >= 4:
                break
            if any(t["epic"] == epic for _, t in opened):
                continue
            try:
                market = self.api.get("/markets/" + epic)
                if market["snapshot"]["marketStatus"] != "TRADEABLE":
                    continue
                prices = self.api.get("/prices/" + epic + "?resolution=MINUTE_15&max=200")["prices"]
                idea = signal(prices, now)
                if idea["direction"] is None:
                    continue
                # Refresh quote after historical request before constructing the order.
                market = self.api.get("/markets/" + epic)
                p = plan(market, idea, equity, available, now if fixed_clock else time.time())
                if p["epic"] != epic or any(t["type"] == p["type"] for _, t in opened):
                    raise Skip("Epic mismatch or exposure to the same asset class")
                if sum(t["risk"] for _, t in opened) + p["risk"] > equity * 0.005:
                    raise Skip("Portfolio planned risk exceeds 0.5%")
                ident = epic + ":" + idea["bar"]
                if not self.store.reserve(ident, p):
                    continue
                day["orders"] += 1
                self.store.put("day", day)
                if self.mode == "demo":
                    result = self.api.post_position(p)
                    p["deal_reference"] = result["dealReference"]
                    self.store.finish(ident, "pending", p)
                    confirm = None
                    for _ in range(5):
                        time.sleep(1)
                        confirm = self.api.get("/confirms/" + p["deal_reference"])
                        if confirm.get("dealStatus") in {"ACCEPTED", "REJECTED"}:
                            break
                    if confirm.get("dealStatus") == "REJECTED":
                        self.store.finish(ident, "rejected", p)
                        log("order_rejected", epic=epic)
                        continue
                    deals = confirm.get("affectedDeals", [])
                    if confirm.get("dealStatus") != "ACCEPTED" or len(deals) != 1 or deals[0].get("status") != "OPENED":
                        raise Halt("Uncertain confirmation; do not resubmit")
                    p["deal_id"] = deals[0]["dealId"]
                    actual = self.api.get("/positions/" + p["deal_id"])["position"]
                    if actual.get("stopLevel") is None or actual.get("profitLevel") is None or number(actual["size"]) != p["size"]:
                        raise Halt("Confirmed position differs or lacks protection; inspect immediately")
                self.store.finish(ident, "open", p)
                opened.append((ident, p))
                log("position_opened", mode=self.mode, epic=epic, direction=p["direction"], size=p["size"], planned_risk=round(p["risk"], 2))
            except (Skip, KeyError, StopIteration, ValueError) as e:
                if self.store.trades("pending"):
                    raise Halt("Failure after reservation; inspect before continuing") from e
                log("market_skipped", epic=epic, reason=str(e)[:140])


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    mode = os.environ.get("BOT_MODE", "paper")
    if mode not in {"paper", "demo"}:
        raise Halt("BOT_MODE must be paper or demo; live mode is unavailable")
    if mode == "demo" and os.environ.get("BOT_ARMED") != "DEMO_ONLY":
        raise Halt("Demo execution requires BOT_ARMED=DEMO_ONLY after paper review")
    account = os.environ.get("BOT_ACCOUNT_ID", "")
    if not re.fullmatch(r"\d+", account):
        raise Halt("Set exact BOT_ACCOUNT_ID; the worker will not pick an account")
    state = os.environ.get("BOT_STATE_PATH", "")
    if not state:
        raise Halt("Set BOT_STATE_PATH on a persistent disk")
    Path(state).parent.mkdir(parents=True, exist_ok=True)
    import fcntl  # Render/Linux worker; refuse concurrent processes on this disk
    lock = open(state + ".lock", "a")
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    api, store = API(account), Ledger(state, mode, account)
    worker = Worker(api, store, mode)
    api.login()
    while True:
        try:
            worker.cycle()
        except Exception as e:
            # Do not restart trades after any API, persistence or ambiguous-order error.
            store.put("halt", {"error_type": type(e).__name__, "utc": datetime.now(timezone.utc).isoformat()})
            log("HALTED_REVIEW_REQUIRED", error_type=type(e).__name__)
            return 1
        if args.once:
            return 0
        time.sleep(60)


if __name__ == "__main__":
    raise SystemExit(main())
