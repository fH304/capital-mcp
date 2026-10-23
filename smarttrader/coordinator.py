"""Connect analysis to bounded entry plans and a durable demo order journal.

No network adapter is supplied here. A broker adapter must independently collect
account/contract data and confirm positions; model output cannot supply them.
"""
import hashlib
import json
import math
import sqlite3
import time
from pathlib import Path

from .analysis import validate_recommendation
from .policy import assessed_size
from .sizing import size_position, SizingError
from .exposure import entry_limits, portfolio_entry_limits, ExposureError
from .trial import TrialEntryBlocked, is_approved_account


class CoordinationError(RuntimeError):
    pass


class EntryPreflightBlocked(RuntimeError):
    """Adapter proof that a pre-HTTP input check prevented submission."""


class PairEntryBlocked(CoordinationError):
    """A known exposure veto before any broker order is submitted."""


def fresh(value, now, age, label):
    if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= now-value <= age:
        raise CoordinationError('Stale or invalid '+label)


def plan_entry(recommendation, context, snapshot, now, *, owned=None, max_positions=1,
               allow_technical_entry=False, allow_newsless_technical=False):
    """Revalidate analysis with current executable quotes and broker metadata.

    snapshot is trusted adapter data, never part of the model recommendation.
    Two-entry mode accepts only independently verified journal-owned legs.
    It never treats the analysis assessment as a calibrated probability.
    """
    if snapshot.get('environment') != 'demo' or not snapshot.get('account_id'):
        raise CoordinationError('Only an identified demo account is supported')
    if allow_technical_entry and not is_approved_account(str(snapshot['account_id'])):
        raise CoordinationError('Technical entry policy is not authorized for this account')
    if snapshot.get('epic') != context.get('epic'):
        raise CoordinationError('Market mismatch')
    for key in ('account_time', 'positions_time', 'orders_time', 'contract_time'):
        fresh(snapshot.get(key), now, 30, key)
    if 'conversion_time' in snapshot or snapshot.get('quote_currency','USD')!='USD':
        fresh(snapshot.get('conversion_time'),now,30,'currency conversion')
    if snapshot.get('tradeable') is not True or snapshot.get('daily_stopped') is not False:
        raise CoordinationError('Market or daily entry gate is closed')
    if max_positions not in (0, 1, 2):
        raise CoordinationError('Invalid position policy')
    if max_positions == 1 and (snapshot.get('positions') != [] or snapshot.get('working_orders') != []):
        raise CoordinationError('Account must be reconciled and empty before entry')
    # Replace old analysis prices after the model call; recheck reward/risk.
    current = dict(context, bid=snapshot.get('bid'), ask=snapshot.get('ask'),
                   quote_time=snapshot.get('quote_time'))
    if current.get('strategy_policy') == 'independent_hour_day_trends' \
            and recommendation['action'] in ('BUY','SELL'):
        from .strategy import active_setup
        refreshed = active_setup(current,now)
        if not refreshed['ready'] or refreshed['direction'] != recommendation['action'] \
                or refreshed.get('signal_at') != context.get('trend_signal_at'):
            raise CoordinationError('Trend signal changed before submission')
        # Refresh the broker stop from the original signal's candles and route.
        recommendation = dict(recommendation,stop_level=refreshed['stop_level'],
                              target_level=refreshed['target_level'])
    result = validate_recommendation(recommendation, current, now,
                                    allow_technical_entry=allow_technical_entry,
                                    allow_newsless_technical=allow_newsless_technical)
    if result['action'] == 'WAIT':
        return None
    if result['action'] not in ('BUY', 'SELL'):
        raise CoordinationError('Exit reconciliation is not connected')
    bid, ask = current['bid'], current['ask']
    # sizing adds spread itself: measure from the executable closing side.
    distance = bid-result['stop_level'] if result['action'] == 'BUY' else result['stop_level']-ask
    setup=None
    if allow_technical_entry:
        from .strategy import active_setup
        setup=active_setup(current,now)
        distance+=setup['slippage_stress_distance']
    names = ('equity', 'free_margin', 'point_value', 'quote_to_account',
             'margin_per_unit', 'min_size', 'size_step', 'max_size',
             'open_risk', 'daily_remaining_risk')
    try:
        inputs = {name: snapshot[name] for name in names}
    except KeyError:
        raise CoordinationError('Incomplete broker risk metadata') from None
    if inputs['open_risk'] != 0:
        raise CoordinationError('Empty account has inconsistent open risk')
    limits = None
    if max_positions == 2:
        try:
            candle = context['timeframes']['MINUTE_15']['candles'][-1]['t']
            fresh(candle+900, now, 900, 'closed entry candle')
            limits = entry_limits(snapshot, owned, direction=result['action'],
                                  assessment=result['assessment'], candle=candle)
        except (KeyError, IndexError, TypeError, ExposureError, ValueError) as error:
            raise PairEntryBlocked(str(error)) from None
        inputs.update(open_risk=limits['existing_risk'],
                      daily_remaining_risk=limits['daily_remaining_risk'],
                      portfolio_fraction='0.0025', margin_fraction=limits['margin_fraction'])
    elif max_positions == 0:
        try:
            candle = context['timeframes']['MINUTE_15']['candles'][-1]['t']
            fresh(candle+900, now, 900, 'closed entry candle')
            limits = portfolio_entry_limits(snapshot,owned,candle=candle,now=now)
        except (KeyError, IndexError, TypeError, ExposureError, ValueError) as error:
            raise PairEntryBlocked(str(error)) from None
        inputs.update(open_risk=limits['existing_risk'],
                      daily_remaining_risk=limits['daily_remaining_risk'],
                      risk_fraction='0.0005',portfolio_fraction='0.0025',
                      margin_fraction=limits['margin_fraction'])
    if max_positions == 0:
        sizing = size_position(stop_distance=distance, spread=ask-bid, **inputs)
    else:
        sizing = assessed_size(assessment=result['assessment'], validation_passed=False,
                               stop_distance=distance, spread=ask-bid, **inputs)
    no_target = (current.get('strategy_policy') == 'independent_hour_day_trends'
                 and result['target_level'] == 0)
    plan = {'account_id': str(snapshot['account_id']), 'epic': current['epic'],
            'direction': result['action'], 'size': sizing['size'],
            'stop_level': result['stop_level'],
            'target_level': None if no_target else result['target_level'],
            'planned_risk': sizing['planned_risk'], 'required_margin': sizing['required_margin'],
            'reason': result['reason'], 'article_ids': list(result['article_ids'])}
    if setup:
        plan['slippage_stress_distance']=setup['slippage_stress_distance']
        plan['strategy']=setup['strategy']
        plan['setup_reason']=setup['reason']
        plan['invalidation_level']=setup['invalidation_level']
        plan['target_basis']=setup['target_basis']
        if no_target:
            plan['trend_route']=context['trend_route']
            plan['trend_signal_at']=context['trend_signal_at']
    plan['evidence_basis'] = 'news_articles' if context['articles'] else 'validated_closed_candles'
    for key in ('account_currency','quote_currency','fx_epic','fx_rate','quote_to_account',
                'conversion_time','conversion_fee_buffer'):
        if key in snapshot:
            plan[key]=snapshot[key]
    if limits is not None and max_positions == 2:
        plan.update(group_equity=limits['group_equity'], pair_risk_limit=limits['pair_risk_limit'],
                    existing_risk=limits['existing_risk'], existing_margin=limits['existing_margin'],
                    combined_planned_risk=limits['existing_risk']+plan['planned_risk'],
                    combined_required_margin=limits['existing_margin']+plan['required_margin'],
                    pair_position_limit=2, signal_candle=candle, assessment=result['assessment'],
                    leverage=snapshot['leverage'], margin_factor=snapshot['margin_factor'],
                    confidence_basis='assessment_not_probability')
    elif limits is not None:
        plan.update(group_equity=limits['group_equity'],
                    portfolio_risk_limit=limits['portfolio_risk_limit'],
                    existing_risk=limits['existing_risk'], existing_margin=limits['existing_margin'],
                    combined_planned_risk=limits['existing_risk']+plan['planned_risk'],
                    combined_required_margin=limits['existing_margin']+plan['required_margin'],
                    signal_candle=candle, assessment=result['assessment'],
                    leverage=snapshot['leverage'], margin_factor=snapshot['margin_factor'],
                    entry_policy='journal_owned_portfolio_no_count_limit')
    return plan


class DemoCoordinator:
    """Reserve before submission; unresolved requests block subsequent entries.

    broker.environment and broker.account_id must come from authenticated session
    verification. submit_entry returns a reference only; confirm_entry must fetch
    the actual position and return its account, epic, direction, size, stop and
    target in addition to deal_id and status='confirmed'. No automatic retry.
    """
    def __init__(self, path, account_id, max_positions=1, *, allow_technical_entry=False,
                 allow_newsless_technical=False):
        if type(max_positions) is not int or max_positions not in (0, 1, 2):
            raise CoordinationError('Invalid position policy')
        self.max_positions = max_positions
        self.account_id = str(account_id)
        self.allow_technical_entry = allow_technical_entry is True
        self.allow_newsless_technical = allow_newsless_technical is True
        if self.allow_newsless_technical and not self.allow_technical_entry:
            raise CoordinationError('News-independent rules require approved technical entries')
        if self.allow_technical_entry and not is_approved_account(self.account_id):
            raise CoordinationError('Technical entry policy is not authorized for this account')
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, timeout=10)
        self.db.execute('PRAGMA synchronous=FULL')
        self.db.execute('CREATE TABLE IF NOT EXISTS smart_entries ('
                        'signal TEXT PRIMARY KEY, account TEXT NOT NULL, '
                        'status TEXT NOT NULL, plan TEXT NOT NULL, deal TEXT, reference TEXT)')
        if 'reference' not in {row[1] for row in self.db.execute('PRAGMA table_info(smart_entries)')}:
            self.db.execute('ALTER TABLE smart_entries ADD COLUMN reference TEXT')
        self.db.execute('CREATE TABLE IF NOT EXISTS smart_exits ('
                        'deal TEXT PRIMARY KEY, account TEXT NOT NULL, status TEXT NOT NULL, '
                        'reason TEXT NOT NULL, reference TEXT)')
        self.db.execute('CREATE TABLE IF NOT EXISTS smart_uncertain_reviews ('
                        'signal TEXT PRIMARY KEY, account TEXT NOT NULL, first_seen REAL NOT NULL)')
        self.db.commit()

    def close(self):
        self.db.close()

    def process(self, signal_id, recommendation, context, broker, now=None, armed=False):
        if not isinstance(signal_id, str) or not signal_id.strip() or len(signal_id) > 256:
            raise CoordinationError('A stable market/candle signal ID is required')
        if broker.environment != 'demo' or str(broker.account_id) != self.account_id:
            raise CoordinationError('Authenticated demo account mismatch')
        # snapshot collection happens after analysis, immediately before entry.
        snapshot = broker.snapshot(context['epic'])
        now = time.time() if now is None else now
        if str(snapshot.get('account_id')) != self.account_id:
            raise CoordinationError('Snapshot account mismatch')
        if self.max_positions != 1:
            if self.unresolved():
                raise CoordinationError('Account ownership or order review required')
            # Monitoring reconciles closures separately. Do not mark a newly
            # confirmed leg closed using an older in-flight entry snapshot.
        try:
            plan = plan_entry(recommendation, context, snapshot, now,
                              owned=self.owned(), max_positions=self.max_positions,
                              allow_technical_entry=self.allow_technical_entry,
                              allow_newsless_technical=self.allow_newsless_technical)
        except PairEntryBlocked as error:
            return dict(status='blocked',reason=str(error))
        if plan is None:
            return {'status': 'wait'}
        return self._submit_planned(signal_id,plan,context,broker,armed,
                                    recommendation,snapshot,now)

    def process_hourly(self, epic, direction, broker, now=None, armed=False):
        """One idempotent BUY or SELL attempt per UTC hour and instrument."""
        from .hourly_hedge import hour_start, plan_entry as hourly_plan
        if self.max_positions != 0 or not self.allow_technical_entry:
            raise CoordinationError('Hourly hedging needs approved demo portfolio policy')
        if broker.environment != 'demo' or str(broker.account_id) != self.account_id:
            raise CoordinationError('Authenticated demo account mismatch')
        if self.unresolved():
            raise CoordinationError('Account ownership or order review required')
        snapshot=broker.snapshot(epic)
        # A broker snapshot can be newer than the scheduler timestamp, especially
        # its USD conversion stamp created at the end of the HTTP sequence.
        # Compare quote freshness against a clock read after that sequence.
        now=max(now,broker.clock()) if now is not None and callable(getattr(broker,'clock',None)) else (time.time() if now is None else now)
        try:
            stop=broker.hourly_stop(snapshot,direction)
            plan=hourly_plan(snapshot,self.owned(),direction,now,stop)
        except (ValueError,KeyError,ExposureError,SizingError) as error:
            return dict(status='blocked',reason=str(error))
        signal_id='hourly:%d:%s' % (hour_start(now),direction)
        return self._submit_planned(signal_id,plan,{'epic':epic},broker,armed,
                                    None,snapshot,now)

    def _submit_planned(self,signal_id,plan,context,broker,armed,
                        recommendation,snapshot,now):
        if self.unresolved():
            raise CoordinationError('Unresolved order; review required')
        if armed is not True:
            return {'status': 'preview', 'plan': plan}
        signal = hashlib.sha256((self.account_id+'\0'+context['epic']+'\0'+signal_id).encode()).hexdigest()
        self.db.execute('BEGIN IMMEDIATE')
        try:
            if self.db.execute('SELECT 1 FROM smart_entries WHERE signal=?', (signal,)).fetchone():
                self.db.rollback()
                return {'status': 'duplicate'}
            if self.db.execute("SELECT 1 FROM smart_entries WHERE account=? AND status IN ('pending','uncertain')",
                               (self.account_id,)).fetchone():
                raise CoordinationError('Unresolved order; broker reconciliation required')
            if self.max_positions != 1 and recommendation is not None:
                # Another client must not use a pre-reservation snapshot after
                # an order has been confirmed on the shared journal.
                plan = plan_entry(recommendation, context, snapshot, now,
                                  owned=self.owned(), max_positions=self.max_positions,
                                  allow_technical_entry=self.allow_technical_entry,
                                  allow_newsless_technical=self.allow_newsless_technical)
            self.db.execute('INSERT INTO smart_entries (signal,account,status,plan) VALUES (?,?,?,?)',
                            (signal, self.account_id, 'pending', json.dumps(plan, allow_nan=False)))
            self.db.commit()
        except PairEntryBlocked as error:
            self.db.rollback()
            return dict(status='blocked',reason=str(error))
        except Exception:
            self.db.rollback()
            raise
        # No error/timeout is classified as rejection without broker evidence.
        phase='submit'
        try:
            reference = broker.submit_entry(dict(plan))
            if not isinstance(reference, str) or not reference or len(reference)>256:
                raise CoordinationError('Invalid order reference')
            self.db.execute('UPDATE smart_entries SET reference=? WHERE signal=?', (reference, signal))
            self.db.commit()
            phase='confirm'
            confirmation = broker.confirm_entry(reference)
            keys = ('account_id', 'epic', 'direction', 'size', 'stop_level', 'target_level')
            if (not isinstance(confirmation, dict) or confirmation.get('status') != 'confirmed'
                    or not confirmation.get('deal_id')
                    or any(confirmation.get(k) != plan[k] for k in keys)):
                raise CoordinationError('Position confirmation mismatch')
            # Persist the independently verified fill-cost estimates for review.
            for key in ('actual_risk','actual_margin'):
                if key in confirmation:
                    plan[key]=confirmation[key]
            if self.max_positions != 1:
                actual_leverage=confirmation.get('leverage')
                if (type(actual_leverage) not in (int,float) or not math.isfinite(actual_leverage)
                        or actual_leverage<=0 or actual_leverage>plan['leverage']
                        or plan['margin_factor']+1e-9<1/actual_leverage):
                    raise CoordinationError('Confirmed leverage exceeds reserved protection')
                plan['leverage']=actual_leverage
                for key in ('existing_risk','existing_margin'):
                    plan[key]=confirmation[key]
                plan['combined_actual_risk']=plan['existing_risk']+plan['actual_risk']
                plan['combined_actual_margin']=plan['existing_margin']+plan['actual_margin']
            if 'conversion_time' in confirmation:
                plan['confirmed_conversion_time']=confirmation['conversion_time']
                plan['confirmed_fx_rate']=confirmation['fx_rate']
            self.db.execute('UPDATE smart_entries SET status=?, deal=?, plan=? WHERE signal=?',
                            ('confirmed', str(confirmation['deal_id']),json.dumps(plan,allow_nan=False),signal))
            self.db.commit()
            return {'status': 'confirmed', 'deal_id': str(confirmation['deal_id']), 'plan': plan}
        except (TrialEntryBlocked,EntryPreflightBlocked) as error:
            # These exceptions are raised only by pre-HTTP entry checks.
            # It proves no POST was sent; it is not a broker rejection.
            self.db.execute('UPDATE smart_entries SET status=? WHERE signal=?', ('blocked',signal))
            self.db.commit()
            return dict(status='blocked',reason=str(error))
        except Exception as error:
            self.db.execute('UPDATE smart_entries SET status=? WHERE signal=?', ('uncertain', signal))
            self.db.commit()
            detail=(str(error) if type(error).__name__ in ('CapitalError','CoordinationError')
                    else type(error).__name__)
            raise CoordinationError('Order outcome uncertain at '+phase+': '+detail) from None

    def owned(self):
        return {deal:json.loads(plan) for deal,plan in self.db.execute(
            "SELECT deal,plan FROM smart_entries WHERE account=? AND status='confirmed'",(self.account_id,))}

    def unresolved(self):
        return bool(self.db.execute("SELECT 1 FROM smart_entries WHERE account=? AND status IN ('pending','uncertain')",
                    (self.account_id,)).fetchone() or self.db.execute(
                    "SELECT 1 FROM smart_exits WHERE account=? AND status IN ('pending','uncertain')",(self.account_id,)).fetchone())

    def review_summary(self):
        """Counts only; log no order references or broker credentials."""
        result={}
        for table,category in (('smart_entries','entries'),('smart_exits','exits')):
            rows=self.db.execute('SELECT status,count(*) FROM '+table+
                                 ' WHERE account=? AND status IN (\'pending\',\'uncertain\') GROUP BY status',
                                 (self.account_id,))
            result.update({category+'_'+status:count for status,count in rows})
        return result

    def reconcile(self, state, now=None, broker=None):
        if str(state.get('account_id')) != self.account_id or not isinstance(state.get('positions'),list):
            raise CoordinationError('Account reconciliation mismatch')
        owned = self.owned()
        actual = {x['position']['dealId']:x for x in state['positions']}
        if len(actual)!=len(state['positions']):
            raise CoordinationError('Duplicate position identifiers')
        released=0
        recovered=0
        recovery_review=[]
        with self.db:
            for deal in owned.keys()-actual.keys():
                # Broker stop/target/external close: do not invent a realized P/L.
                self.db.execute("UPDATE smart_entries SET status='closed' WHERE account=? AND deal=?",
                                (self.account_id,deal))
            if state.get('working_orders') == []:
                # A disputed close needs no resend once the complete broker
                # list independently proves the named deal is absent.
                for (deal,) in self.db.execute("SELECT deal FROM smart_exits WHERE account=? "
                                               "AND status IN ('pending','uncertain')",(self.account_id,)):
                    if deal not in actual:
                        self.db.execute("UPDATE smart_exits SET status='closed' WHERE account=? AND deal=?",
                                        (self.account_id,deal))
            # Old order transports can time out with no reference. Preserve the
            # signal's primary key and the uncertain history, but release the
            # *entry gate* only after two complete, spaced broker snapshots
            # prove no unowned position or pending working order remains.
            if (type(now) in (int,float) and math.isfinite(now)
                    and type(state.get('observed_at')) in (int,float)
                    and 0<=now-state['observed_at']<=30
                    and state.get('working_orders')==[]):
                # A confirmation can fail after the broker has opened a deal.
                # A unique exact broker reference and conservative fill checks
                # recover ownership so the hourly exit continues to run.
                for signal,raw,reference in self.db.execute(
                    "SELECT signal,plan,reference FROM smart_entries WHERE account=? AND status='uncertain'",
                    (self.account_id,)):
                    if not reference:
                        continue
                    matches=[item for deal,item in actual.items()
                             if deal not in owned and item['position'].get('dealReference')==reference]
                    if not matches and broker is not None:
                        # Capital's position dealReference may differ from the
                        # order reference. Its authenticated confirmation is
                        # authoritative for the opened deal ID.
                        try:
                            from .capital import identifier
                            confirmation=broker.get('/confirms/'+identifier(reference))
                            deals=confirmation.get('affectedDeals')
                            if confirmation.get('dealStatus')=='ACCEPTED' and isinstance(deals,list) \
                                    and len(deals)==1 and deals[0].get('status')=='OPENED':
                                deal=deals[0].get('dealId')
                                if deal in actual and deal not in owned:
                                    matches=[actual[deal]]
                        except Exception:
                            recovery_review.append('confirmation_unavailable')
                    if len(matches)!=1:
                        recovery_review.append('reference_not_matched')
                        continue
                    try:
                        plan=json.loads(raw)
                        item=matches[0]
                        pos,market=item['position'],item['market']
                        leverage=pos['leverage']
                        risk=(pos['level']-pos['stopLevel'] if pos['direction']=='BUY'
                              else pos['stopLevel']-pos['level'])*pos['size']*plan['quote_to_account']
                        margin=pos['level']*pos['size']*plan['margin_factor']*plan['fx_rate']
                        valid=(plan.get('strategy')=='hourly_hedge_v1'
                               and market['epic']==plan['epic']
                               and pos['direction']==plan['direction']
                               and pos['size']==plan['size']
                               and pos['stopLevel']==plan['stop_level']
                               and pos.get('profitLevel')==plan['target_level']
                               and pos['currency']==plan['quote_currency']
                               and pos['contractSize']==1
                               and type(leverage) in (int,float) and math.isfinite(leverage)
                               and 0<leverage<=plan['leverage']
                               and plan['margin_factor']+1e-9>=1/leverage
                               and 0<risk<=plan['portfolio_risk_limit']-plan['existing_risk']+1e-9
                               and 0<margin<=plan['required_margin']*1.05+1)
                        if not valid:
                            recovery_review.append('fill_or_margin_mismatch')
                            continue
                        plan.update(leverage=leverage,actual_risk=risk,actual_margin=margin,
                                    combined_actual_risk=plan['existing_risk']+risk,
                                    combined_actual_margin=plan['existing_margin']+margin,
                                    recovered_from_broker_snapshot=True)
                        self.db.execute('UPDATE smart_entries SET status=?,deal=?,plan=? WHERE signal=?',
                                        ('confirmed',pos['dealId'],json.dumps(plan,allow_nan=False),signal))
                        owned[pos['dealId']]=plan
                        recovered+=1
                    except (KeyError,TypeError,ValueError,ZeroDivisionError,OverflowError):
                        recovery_review.append('incomplete_fill_evidence')
                        continue
                if set(actual)==set(owned)&set(actual):
                    for signal,raw in self.db.execute(
                        "SELECT signal,plan FROM smart_entries WHERE account=? AND status='uncertain'",
                        (self.account_id,)):
                        try:
                            plan=json.loads(raw)
                            candle=plan['signal_candle']
                            minimum_age=(0 if plan.get('strategy')=='hourly_hedge_v1' else 3600)
                            if type(candle) not in (int,float) or not math.isfinite(candle) \
                                    or not minimum_age < now-candle < 30*86400:
                                continue
                        except (ValueError,KeyError,TypeError):
                            continue
                        row=self.db.execute('SELECT first_seen FROM smart_uncertain_reviews '
                                            'WHERE signal=? AND account=?',(signal,self.account_id)).fetchone()
                        if row is None:
                            self.db.execute('INSERT INTO smart_uncertain_reviews VALUES (?,?,?)',
                                            (signal,self.account_id,now))
                        elif 120<=now-row[0]<=7*86400:
                            self.db.execute("UPDATE smart_entries SET status='inactive_unverified' "
                                            "WHERE signal=? AND account=? AND status='uncertain'",
                                            (signal,self.account_id))
                            released+=1
        changed = []
        for deal in owned.keys() & actual.keys():
            pos, market = actual[deal]['position'],actual[deal]['market']
            plan=owned[deal]
            if (pos['size']!=plan['size'] or pos['direction']!=plan['direction'] or market['epic']!=plan['epic']
                    or ('quote_currency' in plan and pos.get('currency')!=plan['quote_currency'])):
                changed.append(deal)
        return dict(owned={k:v for k,v in owned.items() if k in actual and k not in changed},
                    unknown=sorted(actual.keys()-owned.keys()),changed=changed,
                    unresolved=self.unresolved(),historical_review_released=released,
                    broker_confirmation_recovered=recovered,
                    broker_recovery_review=sorted(set(recovery_review)))

    def close_position(self, deal, reason, broker, armed=False):
        plan=self.owned().get(deal)
        if not plan or broker.environment!='demo' or str(broker.account_id)!=self.account_id:
            raise CoordinationError('Close ownership or account mismatch')
        if armed is not True:
            return dict(status='close_preview',deal_id=deal,reason=reason)
        if reason not in {'missing_broker_stop','daily_loss_limit','stop_breached',
                          'confirmed_trend_reversal','hourly_profit_target','hourly_window_end'}:
            raise CoordinationError('Exit evidence is not independently validated')
        if reason in {'hourly_profit_target','hourly_window_end'} and (
                plan.get('strategy')!='hourly_hedge_v1' or type(plan.get('hour_start')) is not int):
            raise CoordinationError('No recorded hourly exit policy for owned position')
        if reason == 'confirmed_trend_reversal' and (
                plan.get('target_level') is not None or plan.get('trend_route') not in ('HOUR','DAY')
                or type(plan.get('trend_signal_at')) not in (int,float)):
            raise CoordinationError('No recorded trend exit policy for owned position')
        self.db.execute('BEGIN IMMEDIATE')
        try:
            if self.db.execute('SELECT 1 FROM smart_exits WHERE deal=?',(deal,)).fetchone():
                self.db.rollback()
                return dict(status='close_already_reserved',deal_id=deal)
            self.db.execute('INSERT INTO smart_exits(deal,account,status,reason) VALUES (?,?,?,?)',
                            (deal,self.account_id,'pending',reason))
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise
        try:
            reference=broker.submit_close(deal,plan)
            if not isinstance(reference,str) or not reference or len(reference)>256:
                raise CoordinationError('Invalid close reference')
            with self.db:
                self.db.execute('UPDATE smart_exits SET reference=? WHERE deal=?',(reference,deal))
            result=broker.confirm_close(reference,deal)
            if result != dict(status='closed',account_id=self.account_id,deal_id=deal):
                raise CoordinationError('Close outcome mismatch')
            with self.db:
                self.db.execute("UPDATE smart_exits SET status='closed' WHERE deal=?",(deal,))
                self.db.execute("UPDATE smart_entries SET status='closed' WHERE account=? AND deal=?",(self.account_id,deal))
            return result
        except Exception:
            with self.db:
                self.db.execute("UPDATE smart_exits SET status='uncertain' WHERE deal=?",(deal,))
            raise CoordinationError('Close outcome uncertain; no automatic resend') from None
