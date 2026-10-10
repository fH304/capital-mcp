"""Restricted Capital demo REST adapter. Source: https://open-api.capital.com/.

USD CFD account, contracts with verified USD FX routes and lot/scaling 1 only.
The runner supplies persistent ownership and order reservations.
Daily risk is supplied by a persistent controller, not by the AI model.
"""
import json
import math
import re
import threading
import time
from datetime import datetime, timezone
from decimal import Decimal
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, build_opener

from .coordinator import CoordinationError, EntryPreflightBlocked, fresh
from .fx import ROUTES, conversion
from .news import NoRedirect
from .exposure import entry_limits, ExposureError

BASE = 'https://demo-api-capital.backend-capital.com/api/v1'
TYPES = {'CURRENCIES', 'COMMODITIES', 'INDICES', 'CRYPTOCURRENCIES'}


class CapitalError(RuntimeError):
    pass


def number(value, positive=False):
    if type(value) not in (int, float) or not math.isfinite(value) or (positive and value <= 0):
        raise CapitalError('Invalid broker numeric data')
    return value


def identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,150}', value):
        raise CapitalError('Invalid broker identifier')
    return quote(value, safe='')


class CapitalDemo:
    environment = 'demo'

    def __init__(self, *, key, user, password, account_id, daily_controller,
                 environment='demo', armed=False, opener=None, clock=time.time,
                 sleep=time.sleep, entry_guard=None):
        if environment != 'demo':
            raise CapitalError('Live endpoint is unavailable in this adapter')
        if not all(isinstance(x, str) and x for x in (key, user, password, account_id)):
            raise CapitalError('Missing Capital credentials or account ID')
        identifier(account_id)
        self.expected_account = account_id
        self.account_id = None  # only populated after authenticated verification
        self.key, self.user, self.password = key, user, password
        self.daily_controller, self.armed = daily_controller, armed is True
        self.opener = opener or build_opener(NoRedirect()).open
        self.clock, self.sleep = clock, sleep
        self.entry_guard = entry_guard
        self.headers, self.offset = {}, None
        self.lock = threading.RLock()
        self.last_request = self.last_login = float('-inf')
        self.prepared, self.pending = None, {}
        self._close_path = None
        self.fx_quotes = {}
        self.max_positions, self.ownership = 1, None

    def configure_entries(self, max_positions, ownership):
        if type(max_positions) is not int or max_positions not in (1, 2) or not callable(ownership):
            raise CapitalError('Invalid journal-backed entry policy')
        self.max_positions, self.ownership = max_positions, ownership

    def _request(self, method, path, payload=None, preflight=None):
        # Fixed host; DELETE is enabled only inside verified submit_close.
        allowed = (method == 'GET' and (path in {'/session', '/accounts', '/positions', '/workingorders', '/accounts/preferences'}
                    or re.fullmatch(r'/(markets|positions|confirms)/[A-Za-z0-9_.:%-]+', path)))
        allowed = allowed or (method == 'POST' and path in {'/session', '/positions'})
        allowed = allowed or (method=='GET' and re.fullmatch(
            r'/prices/[A-Za-z0-9_.:%-]+\?resolution=(MINUTE_15|HOUR|HOUR_4)&max=([1-9]|[1-3][0-9]|40)',path))
        allowed = allowed or (method == 'DELETE' and self.armed and path == self._close_path)
        if not allowed:
            raise CapitalError('Endpoint outside adapter scope')
        with self.lock:
            delay = .15 - (self.clock()-self.last_request)
            if path == '/session' and method == 'POST':
                delay = max(delay, 1.1-(self.clock()-self.last_login))
            if delay > 0:
                self.sleep(delay)
            self.last_request = self.clock()
            if path == '/session' and method == 'POST':
                self.last_login = self.last_request
            if method == 'POST' and path == '/positions' and self.entry_guard:
                self.entry_guard()  # before HTTP, after any local pacing delay
            if preflight:
                preflight()
            request = Request(BASE+path, method=method,
                              data=json.dumps(payload, allow_nan=False).encode() if payload is not None else None,
                              headers={'Content-Type':'application/json', 'X-CAP-API-KEY':self.key, **self.headers})
            # The worker repeats this check after its shared request pacing.
            request.capital_preflight = preflight
            try:
                with self.opener(request, timeout=20) as response:
                    raw, headers = response.read(2000001), response.headers
                if len(raw) > 2000000:
                    raise CapitalError('Oversized broker response')
                data = json.loads(raw)
                if not isinstance(data, dict):
                    raise CapitalError('Invalid broker response')
                return data, headers
            except HTTPError as error:
                # Expose status only. In particular POST requests are never retried.
                raise CapitalError('Capital HTTP '+str(error.code)) from None
            except (URLError, OSError, TimeoutError, ValueError, UnicodeError):
                raise CapitalError('Capital transport or response failure') from None

    def login(self):
        with self.lock:
            self.headers, self.account_id, self.offset = {}, None, None
            self.fx_quotes.clear()
            data, headers = self._request('POST', '/session',
                                         {'identifier':self.user, 'password':self.password, 'encryptedPassword':False})
            try:
                offset = number(data['timezoneOffset'])
                tokens = {'CST':headers['CST'], 'X-SECURITY-TOKEN':headers['X-SECURITY-TOKEN']}
                if str(data['currentAccountId']) != self.expected_account or not all(tokens.values()) or not -14 <= offset <= 14:
                    raise CapitalError('Authenticated account or session mismatch')
            except (KeyError, TypeError):
                raise CapitalError('Incomplete login response') from None
            self.headers, self.account_id, self.offset = tokens, self.expected_account, offset

    def get(self, path):
        if not self.account_id:
            raise CapitalError('Authenticated session required')
        # No implicit relogin or account change inside an order sequence.
        return self._request('GET', path)[0]

    def verify_session(self):
        session = self.get('/session')
        if str(session.get('accountId')) != self.expected_account:
            self.account_id, self.headers = None, {}
            raise CapitalError('Active account changed')

    def prices(self,epic,resolution,count=40):
        from .candles import RESOLUTIONS
        if resolution not in RESOLUTIONS or type(count) is not int or not 1<=count<=40:
            raise CapitalError('Invalid historical price request')
        self.verify_session()
        data=self.get('/prices/'+identifier(epic)+'?resolution='+resolution+'&max='+str(count))
        rows=data.get('prices')
        if not isinstance(rows,list) or len(rows)>count:
            raise CapitalError('Invalid historical prices response')
        return rows

    def observation_quote(self,epic):
        return self._market_quote(epic, observation_only=True)

    def market_quote(self,epic):
        return self._market_quote(epic, observation_only=False)

    def _market_quote(self,epic,observation_only):
        self.verify_session()
        data=self.get('/markets/'+identifier(epic))
        result=self._quote(data,epic)
        currency=result['quote_currency']
        supported=currency=='USD' or currency in ROUTES
        if not observation_only and not supported:
            raise CapitalError('Unsupported market currency')
        if observation_only and supported:
            try:
                self._conversion(currency,seed=result,refresh=False)
            except (CapitalError,CoordinationError):
                supported=False
        # Monitoring/closure needs only a fresh price for the owned contract;
        # a failing FX feed must not prevent an otherwise valid protective exit.
        return dict(result,execution_supported=supported)

    def _quote(self,data,epic):
        try:
            ins,snap=data['instrument'],data['snapshot']
            if (ins['epic']!=epic or ins['type'] not in TYPES
                    or not isinstance(ins['currency'],str) or not re.fullmatch('[A-Z]{3}',ins['currency'])
                    or number(ins['lotSize'])!=1 or number(snap['scalingFactor'])!=1):
                raise CapitalError('Unsupported market data contract')
            if snap['marketStatus']!='TRADEABLE' or 'REGULAR' not in snap['marketModes'] or number(snap['delayTime'])!=0:
                raise CapitalError('Closed, restricted or delayed market')
            bid,ask=number(snap['bid'],True),number(snap['offer'],True)
            if ask<bid:
                raise CapitalError('Invalid executable spread')
            if 'updateTimeUTC' in snap:
                from .candles import utc_stamp
                stamp=utc_stamp(snap['updateTimeUTC'])
            else:
                dt=datetime.fromisoformat(snap['updateTime'])
                if dt.tzinfo is not None:
                    raise CapitalError('Unexpected local quote timestamp')
                stamp=dt.replace(tzinfo=timezone.utc).timestamp()-self.offset*3600
            fresh(stamp,self.clock(),30,'market quote')
            return dict(epic=epic,bid=bid,ask=ask,quote_time=stamp,
                        instrument_type=ins['type'],quote_currency=ins['currency'])
        except (KeyError,TypeError,ValueError,OverflowError):
            raise CapitalError('Invalid market quote response') from None

    def _conversion(self,currency,seed=None,refresh=True):
        if currency=='USD':
            return conversion(currency,None,self.clock())
        epic=ROUTES.get(currency)
        if not epic:
            raise CapitalError('Unsupported currency conversion')
        quote=seed if seed and seed.get('epic')==epic else None
        if quote is None and not refresh:
            cached=self.fx_quotes.get(epic)
            if cached:
                try:
                    conversion(currency,cached,self.clock())
                    quote=cached
                except ValueError:
                    pass
        if quote is None:
            quote=self._quote(self.get('/markets/'+identifier(epic)),epic)
        try:
            result=conversion(currency,quote,self.clock())
        except ValueError:
            raise CapitalError('Unverified currency conversion') from None
        self.fx_quotes[epic]=dict(quote)
        return result

    def _entry_fresh(self,data):
        try:
            for key in ('quote_time','account_time','positions_time','orders_time',
                        'contract_time','conversion_time'):
                fresh(data[key],self.clock(),30,key)
        except CoordinationError:
            # No HTTP was sent when this preflight rejects an expired snapshot.
            raise EntryPreflightBlocked('stale_entry_or_fx_quote') from None

    def snapshot(self, epic):
        with self.lock:
            self.prepared = None
            self.verify_session()
            accounts = self.get('/accounts').get('accounts')
            if not isinstance(accounts, list):
                raise CapitalError('Missing account list')
            matches = [a for a in accounts if str(a.get('accountId')) == self.expected_account]
            if len(matches) != 1 or matches[0].get('currency') != 'USD' or matches[0].get('status') != 'ENABLED' or matches[0].get('accountType') != 'CFD':
                raise CapitalError('Need an enabled USD CFD demo account')
            try:
                balance = matches[0]['balance']
                # Capital balance includes floating P/L; do not add profitLoss twice.
                equity = number(balance['balance'], True)
                available = number(balance['available'], True)
                account_time = self.clock()
                positions = self.get('/positions')['positions']
                positions_time = self.clock()
                orders = self.get('/workingorders')['workingOrders']
                orders_time = self.clock()
                market = self.get('/markets/'+identifier(epic))
                contract_time = self.clock()
                ins, snap, rules = market['instrument'], market['snapshot'], market['dealingRules']
                if (ins['epic'] != epic or ins['type'] not in TYPES
                        or ins['currency'] not in {'USD',*ROUTES}
                        or number(ins['lotSize']) != 1 or number(snap['scalingFactor']) != 1
                        or ins['marginFactorUnit'] != 'PERCENTAGE'):
                    raise CapitalError('Unsupported contract convention')
                if not isinstance(positions, list) or not isinstance(orders, list):
                    raise CapitalError('Incomplete position or order list')
                bid, ask = number(snap['bid'], True), number(snap['offer'], True)
                if ask < bid:
                    raise CapitalError('Invalid spread')
                if 'updateTimeUTC' in snap:
                    dt = datetime.fromisoformat(snap['updateTimeUTC'].replace('Z', '+00:00'))
                    quote_time = (dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt).timestamp()
                else:
                    dt = datetime.fromisoformat(snap['updateTime'])
                    if dt.tzinfo is not None:
                        raise CapitalError('Unexpected local quote timestamp')
                    quote_time = dt.replace(tzinfo=timezone.utc).timestamp()-self.offset*3600
                fresh(quote_time, self.clock(), 30, 'quote')
                fx=self._conversion(ins['currency'],seed=dict(epic=epic,bid=bid,ask=ask,
                                    quote_time=quote_time,quote_currency=ins['currency'],instrument_type=ins['type']))
                margin = number(ins['marginFactor'], True)/100
                if margin > 1:
                    raise CapitalError('Invalid margin factor')
                preferences = {}
                if self.max_positions == 2:
                    prefs = self.get('/accounts/preferences')
                    settings = prefs['leverages'][ins['type']]
                    leverage = number(settings['current'], True)
                    choices = settings['available']
                    if (prefs.get('hedgingMode') is not True or leverage < 1
                            or not isinstance(choices, list) or leverage not in choices
                            or any(number(x, True) < 1 for x in choices)
                            or not math.isclose(margin, 1/leverage, rel_tol=1e-6, abs_tol=1e-9)):
                        raise CapitalError('Unverified hedging or leverage/margin convention')
                    preferences = dict(hedging_mode=True, leverage=leverage)
                def size_rule(name):
                    if rules[name]['unit'] != 'POINTS':
                        raise CapitalError('Unverified size rule unit')
                    return number(rules[name]['value'], True)
                daily = self.daily_controller(equity, self.clock())
                if type(daily.get('stopped')) is not bool:
                    raise CapitalError('Invalid daily risk controller')
                remaining = number(daily['remaining'])
                if remaining < 0:
                    raise CapitalError('Invalid remaining daily risk')
                data = dict(environment='demo', account_id=self.expected_account, epic=epic,
                            equity=equity, free_margin=available, account_time=account_time,
                            positions=positions, positions_time=positions_time, working_orders=orders,
                            orders_time=orders_time, contract_time=contract_time, bid=bid, ask=ask,
                            quote_time=quote_time, daily_stopped=daily['stopped'], daily_remaining_risk=remaining,
                            tradeable=(daily.get('ready',True) is True and snap['marketStatus']=='TRADEABLE' and 'REGULAR' in snap['marketModes']
                                       and number(snap['delayTime'])==0),
                            point_value=1, margin_factor=margin, margin_per_unit=ask*margin*fx['fx_rate'],
                            min_size=size_rule('minDealSize'), size_step=size_rule('minSizeIncrement'),
                            max_size=size_rule('maxDealSize'), open_risk=0, **fx)
                data.update(preferences)
            except (KeyError, TypeError, ValueError, OverflowError):
                raise CapitalError('Incomplete or invalid broker snapshot') from None
            self.prepared = (data, rules)
            return dict(data)

    def submit_entry(self, plan):
        with self.lock:
            if not self.armed or not self.prepared or self.pending:
                raise CapitalError('Adapter is unarmed or order reconciliation required')
            self.verify_session()
            if self.max_positions == 2:
                # Refresh account, actual legs, FX, contract and preferences
                # after the journal reservation and before the order mutation.
                self.snapshot(plan['epic'])
            data, rules = self.prepared
            self._entry_fresh(data)
            if plan['account_id'] != self.expected_account or plan['epic'] != data['epic']:
                raise CapitalError('Prepared account or market mismatch')
            if (not data['tradeable'] or data['daily_stopped'] or data['working_orders']
                    or (self.max_positions == 1 and data['positions'])):
                if self.max_positions == 2:
                    raise EntryPreflightBlocked('account_gate_changed_before_submission')
                raise CapitalError('Prepared account entry gate is closed')
            limits = None
            if self.max_positions == 2:
                limits = self._pair_limits(data, plan)
                if plan.get('leverage') != data['leverage']:
                    raise EntryPreflightBlocked('leverage_changed_before_submission')
            size = number(plan['size'], True)
            if not data['min_size'] <= size <= data['max_size'] or Decimal(str(size)) % Decimal(str(data['size_step'])):
                raise CapitalError('Invalid broker size increment')
            direction = plan['direction']
            if direction not in ('BUY', 'SELL'):
                raise CapitalError('Invalid direction')
            entry = data['ask'] if direction=='BUY' else data['bid']
            stop, target = number(plan['stop_level'], True), number(plan['target_level'], True)
            loss = entry-stop if direction=='BUY' else stop-entry
            reward = target-entry if direction=='BUY' else entry-target
            def distance_rule(name):
                rule = rules[name]
                v = number(rule['value'], True)
                if rule['unit'] == 'POINTS':
                    return v
                if rule['unit'] == 'PERCENTAGE':
                    return entry*v/100
                raise CapitalError('Unknown distance rule')
            low, high = distance_rule('minStopOrProfitDistance'), distance_rule('maxStopOrProfitDistance')
            if not low <= loss <= high or not low <= reward <= high or reward < 2*loss:
                if self.max_positions == 2:
                    raise EntryPreflightBlocked('protection_or_reward_risk_changed_before_submission')
                raise CapitalError('Broker protection or reward/risk limits failed')
            budget = min(data['equity']*.00125, data['daily_remaining_risk'])
            margin_budget = data['free_margin']*.1
            if limits is not None:
                budget, margin_budget = limits['risk_budget'], limits['margin_budget']
            if (size*loss*data['quote_to_account'] > budget+1e-9
                    or size*data['margin_per_unit'] > margin_budget+1e-9):
                if self.max_positions == 2:
                    raise EntryPreflightBlocked('combined_risk_or_margin_changed_before_submission')
                raise CapitalError('Risk or margin budget exceeded')
            payload = dict(epic=plan['epic'], direction=direction, size=size,
                           stopLevel=stop, profitLevel=target, guaranteedStop=False)
            self.prepared = None  # consume even when the transport fails
            result = self._request('POST', '/positions', payload,
                                   preflight=lambda:self._submission_fresh(data, plan))[0]
            reference = result.get('dealReference')
            identifier(reference)
            self.pending[reference] = dict(plan, _risk_cap=budget,
                                          _quote_currency=data['quote_currency'],
                                          _conversion=data['quote_to_account'],_fx_rate=data['fx_rate'],
                                          _margin_cap=margin_budget,_margin_factor=data['margin_factor'])
            if limits is not None:
                self.pending[reference].update(_existing_risk=limits['existing_risk'],
                                               _existing_margin=limits['existing_margin'],
                                               _pair_risk_cap=min(limits['pair_risk_limit'],data['daily_remaining_risk']),
                                               _group_margin_cap=limits['group_margin_limit'])
            return reference

    def _pair_limits(self, data, plan):
        try:
            return entry_limits(data, self.ownership(), direction=plan.get('direction'),
                                assessment=plan.get('assessment'), candle=plan.get('signal_candle'))
        except (ExposureError, ValueError, KeyError, TypeError):
            raise EntryPreflightBlocked('combined_exposure_or_ownership_changed') from None

    def _submission_fresh(self, data, plan):
        self._entry_fresh(data)
        if self.max_positions == 2:
            self._pair_limits(data, plan)

    def confirm_entry(self, reference):
        with self.lock:
            if reference not in self.pending:
                raise CapitalError('Unknown local order reference')
            self.verify_session()
            confirmation = None
            for _ in range(5):
                confirmation = self.get('/confirms/'+identifier(reference))
                if confirmation.get('dealStatus') in {'ACCEPTED', 'REJECTED'}:
                    break
                self.sleep(1)
            deals = confirmation.get('affectedDeals', [])
            if confirmation.get('dealStatus') != 'ACCEPTED' or len(deals)!=1 or deals[0].get('status')!='OPENED':
                raise CapitalError('Order did not produce one confirmed open position')
            deal = deals[0]['dealId']
            actual = self.get('/positions/'+identifier(deal))
            try:
                pos, market = actual['position'], actual['market']
                result = dict(status='confirmed', account_id=self.expected_account, deal_id=deal,
                              epic=market['epic'], direction=pos['direction'], size=number(pos['size'], True),
                              stop_level=number(pos['stopLevel'], True), target_level=number(pos['profitLevel'], True))
                expected = self.pending[reference]
                fx=self._conversion(expected['_quote_currency'])
                fill = number(pos['level'], True)
                fill_risk = fill-result['stop_level'] if result['direction']=='BUY' else result['stop_level']-fill
                fill_reward = result['target_level']-fill if result['direction']=='BUY' else fill-result['target_level']
                risk=fill_risk*result['size']*max(fx['quote_to_account'],expected['_conversion'])
                margin=fill*result['size']*expected['_margin_factor']*max(fx['fx_rate'],expected['_fx_rate'])
                if (pos['dealId'] != deal or pos['currency']!=expected['_quote_currency'] or number(pos['contractSize'])!=1
                        or any(result[k] != expected[k] for k in ('account_id','epic','direction','size','stop_level','target_level'))
                        or fill_risk<=0 or fill_reward<2*fill_risk
                        or risk>expected['_risk_cap']+1e-9 or margin>expected['_margin_cap']+1e-9):
                    raise CapitalError('Actual position differs from reserved order')
                if self.max_positions == 2:
                    if number(pos.get('leverage'), True) != expected['leverage']:
                        raise CapitalError('Actual leverage differs from reserved entry')
                    result['leverage'] = pos['leverage']
                    existing_risk=expected['_existing_risk']*max(1,fx['quote_to_account']/expected['_conversion'])
                    existing_margin=expected['_existing_margin']*max(1,fx['fx_rate']/expected['_fx_rate'])
                    if (existing_risk+risk>expected['_pair_risk_cap']+1e-9
                            or existing_margin+margin>expected['_group_margin_cap']+1e-9):
                        raise CapitalError('Actual combined risk or margin exceeded')
                    result.update(existing_risk=existing_risk,existing_margin=existing_margin)
                result.update(actual_risk=risk,actual_margin=margin,**fx)
            except (KeyError, TypeError):
                raise CapitalError('Incomplete position confirmation') from None
            del self.pending[reference]
            return result

    def account_state(self):
        """Complete authenticated lists; never infer closure from a failed GET."""
        self.verify_session()
        accounts = self.get('/accounts').get('accounts')
        if not isinstance(accounts, list):
            raise CapitalError('Missing account list')
        matches = [a for a in accounts if str(a.get('accountId')) == self.expected_account]
        if len(matches) != 1 or any(matches[0].get(k) != v for k,v in
                dict(currency='USD',status='ENABLED',accountType='CFD').items()):
            raise CapitalError('Unsupported demo account')
        try:
            equity = number(matches[0]['balance']['balance'], True)
            positions = self.get('/positions')['positions']
            orders = self.get('/workingorders')['workingOrders']
            if not isinstance(positions,list) or not isinstance(orders,list):
                raise CapitalError('Incomplete account lists')
            seen = set()
            for item in positions:
                pos, market = item['position'], item['market']
                deal = pos['dealId']
                identifier(deal)
                identifier(market['epic'])
                if deal in seen or pos['direction'] not in ('BUY','SELL'):
                    raise CapitalError('Invalid position list')
                number(pos['size'],True)
                seen.add(deal)
            daily = self.daily_controller(equity,self.clock())
            if type(daily.get('stopped')) is not bool:
                raise CapitalError('Invalid daily gate')
            return dict(account_id=self.expected_account,positions=positions,
                        working_orders=orders,equity=equity,daily=daily,observed_at=self.clock())
        except (KeyError,TypeError):
            raise CapitalError('Invalid account state') from None

    def submit_close(self, deal_id, expected):
        with self.lock:
            if not self.armed or expected.get('account_id') != self.expected_account:
                raise CapitalError('Unarmed or mismatched close')
            state = self.account_state()
            matches = [x for x in state['positions'] if x['position']['dealId']==deal_id]
            if len(matches)!=1:
                raise CapitalError('Expected position is not open')
            pos, market = matches[0]['position'], matches[0]['market']
            if (market['epic']!=expected['epic'] or pos['direction']!=expected['direction']
                    or pos['size']!=expected['size'] or pos.get('currency')!=expected.get('quote_currency','USD')
                    or number(pos.get('contractSize'))!=1):
                raise CapitalError('Close ownership mismatch')
            # A protective stop can have been removed: closure remains allowed.
            quote=self.market_quote(expected['epic'])
            if quote['quote_currency']!=expected.get('quote_currency','USD'):
                raise CapitalError('Close contract currency mismatch')
            self._close_path = '/positions/'+identifier(deal_id)
            try:
                reference = self._request('DELETE',self._close_path)[0].get('dealReference')
                identifier(reference)
                return reference
            finally:
                self._close_path = None

    def confirm_close(self, reference, deal_id):
        self.verify_session()
        confirmation = self.get('/confirms/'+identifier(reference))
        if confirmation.get('dealStatus') != 'ACCEPTED':
            raise CapitalError('Close is not confirmed')
        deals = confirmation.get('affectedDeals')
        if not isinstance(deals,list) or len(deals)!=1 or deals[0].get('dealId')!=deal_id or deals[0].get('status')!='CLOSED':
            raise CapitalError('Close confirmation mismatch')
        state = self.account_state()
        if any(x['position']['dealId']==deal_id for x in state['positions']):
            raise CapitalError('Position remains open')
        return dict(status='closed',account_id=self.expected_account,deal_id=deal_id)
