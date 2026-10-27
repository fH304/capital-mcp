"""Persistent demo worker. Preview is default; never selects a live endpoint."""
import argparse
import fcntl
import json
import math
import os
import signal
import sqlite3
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from .analysis import AnalysisClient, AnalysisError, validate_context, validate_recommendation
from .capital import CapitalDemo, CapitalError, identifier
from .coordinator import DemoCoordinator, EntryPreflightBlocked, CoordinationError
from .daily import DailyRisk, AdaptiveDailyRisk, DemoHourlyNoDailyGate, DemoFastAINoDailyGate
from .exposure import FAST_AI_ENTRY_FRACTION, FAST_AI_PORTFOLIO_FRACTION
from .fx import ROUTES
from .marketdata import MarketCollector
from .monitor import EntryGate, Monitor, PositionView, exit_decision
from .news import NewsClient, timestamp
from .universe import MARKETS, SPREAD_POLICY, relevant_articles, rank
from .schedule import MarketSchedule
from .spreadstudy import SpreadStudy
from .strategy import active_setup
from .candles import MarketDataError
from .trend_following import trend_candles, reversal_exit
from .hourly_hedge import hour_start, should_close
from .strategy_study import StrategyStudy
from .institutional import FRAMEWORK, STRATEGY, InstitutionalStudy, market_brief
from .trial import (is_approved_account, MODEL as TRIAL_MODEL, TrialBudget, TrialStopped,
                    RULES_RENEWAL_AUTHORIZED_AT)
from .evidence import NEWS_REQUIRED, TECHNICAL_OR_NEWS, technical_evidence


def log(event, **fields):
    print(json.dumps(dict(utc=datetime.now(timezone.utc).isoformat(),event=event,**fields),allow_nan=False),flush=True)


class Config:
    def __init__(self, env):
        self.mode=env.get('SMART_MODE','preview')
        if self.mode not in {'preview','demo'} or env.get('CAP_ENV','demo')!='demo':
            raise ValueError('Only preview and demo are available')
        self.account=env.get('BOT_ACCOUNT_ID','')
        identifier(self.account)
        self.armed=self.mode=='demo'
        if self.armed and (env.get('SMART_ARMED')!='DEMO_ONLY'
                or env.get('SMART_EXCLUSIVE_ACCOUNT')!=self.account):
            raise ValueError('Explicit demo arming and exclusive account required')
        self.directory=Path(env.get('SMART_STATE_DIR','/var/data/smarttrader'))
        if not self.directory.is_absolute():
            raise ValueError('Persistent state path must be absolute')
        self.legacy=env.get('BOT_STATE_PATH','/var/data/demo.sqlite')
        if not Path(self.legacy).is_absolute():
            raise ValueError('Legacy worker state path must be absolute')
        self.risk_mode=env.get('SMART_DAILY_RISK_MODE','fixed' if env.get('SMART_DAILY_LOSS_LIMIT') else 'auto')
        if self.risk_mode not in {'auto','fixed'}:
            raise ValueError('Invalid daily risk mode')
        self.limit=None
        if self.risk_mode=='fixed':
            self.limit=float(env['SMART_DAILY_LOSS_LIMIT'])
            if not math.isfinite(self.limit) or self.limit<=0:
                raise ValueError('Daily loss limit must be positive')
        self.key,self.user,self.password=(env.get(k,'') for k in ('CAP_API_KEY','CAP_IDENTIFIER','CAP_API_PASSWORD'))
        self.ai_key,self.model,self.news_key=(env.get(k,'') for k in ('OPENAI_API_KEY','OPENAI_MODEL','EODHD_API_KEY'))
        if not all((self.key,self.user,self.password)):
            raise ValueError('Missing broker configuration')
        self.broad=env.get('SMART_MARKETS','single')=='broad'
        if env.get('SMART_MARKETS','single') not in {'single','broad'}:
            raise ValueError('SMART_MARKETS must be single or broad')
        # This account's 72h/$20 trial was explicitly authorized on 2026-10-08.
        # Other accounts keep the previous defaults. Restart never renews it.
        trial_flag=env.get('SMART_TRIAL_ENABLED','1' if is_approved_account(self.account) and self.broad else '0')
        if trial_flag not in {'0','1'}:
            raise ValueError('SMART_TRIAL_ENABLED must be 0 or 1')
        self.trial=trial_flag=='1'
        if self.trial and (not is_approved_account(self.account) or not self.broad):
            raise ValueError('Trial is authorized for the approved broad demo account only')
        self.fast_ai=env.get('SMART_AI_FAST','0')=='1'
        if env.get('SMART_AI_FAST','0') not in {'0','1'}:
            raise ValueError('SMART_AI_FAST must be 0 or 1')
        if self.fast_ai and not (self.trial and is_approved_account(self.account)):
            raise ValueError('Fast AI is restricted to the approved demo trial')
        if self.fast_ai:
            self.risk_mode='fast_ai_no_daily_cap'
            self.limit=None
        self.decision_engine=env.get('SMART_DECISION_ENGINE',
                                     'ai' if self.fast_ai else 'rules' if self.trial else 'ai')
        if self.fast_ai:
            self.decision_engine='ai'  # opt in overrides the previous Render rules setting
        if self.decision_engine not in {'rules','ai'} or (self.decision_engine=='rules' and not self.trial):
            raise ValueError('Rules engine requires the approved demo trial')
        if self.decision_engine=='ai' and not all((self.ai_key,self.model)):
            raise ValueError('Missing AI configuration')
        if self.decision_engine=='ai' and not self.fast_ai and not self.news_key:
            raise ValueError('Missing AI news configuration')
        # Zero removes the numerical cap only for the approved demo trial.
        # Broker minimums and portfolio risk/margin ceilings still apply.
        self.max_positions=int(env.get('SMART_PAIR_ENTRIES',
                                       '0' if self.trial and is_approved_account(self.account) else '1'))
        if self.max_positions not in (0,1,2) or (self.max_positions==0 and not self.trial):
            raise ValueError('SMART_PAIR_ENTRIES must be 1, 2, or 0 for the approved demo trial')
        if self.fast_ai:
            self.max_positions=0  # shared portfolio risk/margin cap, no count cap
        self.analysis_mode=env.get('SMART_ANALYSIS_MODE','budgeted')
        if self.analysis_mode not in {'continuous','budgeted'}:
            raise ValueError('Invalid analysis mode')
        if self.trial:
            self.analysis_mode='rules' if self.decision_engine=='rules' else 'trial'
            if self.decision_engine=='ai':
                self.model=TRIAL_MODEL  # fixed model and verified token tariff
        self.continuous=self.analysis_mode in {'continuous','trial','rules'}
        # Continuous monitoring is explicitly independent of the old account
        # quota, including an existing SMART_AI_CALLS_PER_DAY=4 deployment.
        self.calls=None if self.continuous else int(env.get('SMART_AI_CALLS_PER_DAY','4'))
        if self.calls is not None and not 1<=self.calls<=24:
            raise ValueError('Invalid API request cap')
        self.analysis_interval=int(env.get('SMART_ANALYSIS_INTERVAL_SECONDS','0'))
        if not 0<=self.analysis_interval<=86400:
            raise ValueError('Invalid per-market analysis interval')
        if self.trial:
            self.analysis_interval=0
        default_evidence=TECHNICAL_OR_NEWS if self.trial and is_approved_account(self.account) else NEWS_REQUIRED
        self.entry_evidence=env.get('SMART_ENTRY_EVIDENCE',default_evidence)
        if self.entry_evidence not in {NEWS_REQUIRED,TECHNICAL_OR_NEWS}:
            raise ValueError('Invalid entry evidence policy')
        self.technical_entries=self.entry_evidence==TECHNICAL_OR_NEWS
        if self.technical_entries and not (self.trial and is_approved_account(self.account)):
            raise ValueError('Technical entry policy is restricted to the approved demo trial')
        if self.fast_ai and not self.technical_entries:
            raise ValueError('Fast AI requires technical entry evidence')
        if self.decision_engine=='rules' and not self.technical_entries:
            raise ValueError('Rules engine requires the approved technical entry policy')
        self.core_rules_only=self.decision_engine=='rules'
        self.strategy=('ai_fast' if self.fast_ai else env.get('SMART_RULES_STRATEGY',
                              'hourly_hedge' if (self.trial and self.decision_engine=='rules'
                                                 and is_approved_account(self.account)) else 'trend'))
        if self.strategy=='hourly_hedge' and self.trial and self.decision_engine=='rules':
            self.max_positions=0  # user replaced the old position-count policy
        if self.strategy not in {'trend','hourly_hedge','ai_fast'} or (self.strategy=='hourly_hedge' and
                (not self.trial or self.decision_engine!='rules' or self.max_positions!=0)):
            raise ValueError('Hourly hedging requires the approved rules-only demo portfolio')
        self.symbols=({epic:definition[1][0] for epic,definition in MARKETS.items()} if self.broad else
                      json.loads(env.get('SMART_NEWS_SYMBOLS','{"EURUSD":"EURUSD.FOREX"}')))
        if self.strategy=='hourly_hedge':
            self.symbols={epic:epic for epic,(kind,_,_) in MARKETS.items()
                          if kind in {'forex','metals'}}
        if not isinstance(self.symbols,dict) or not 1<=len(self.symbols) or (not self.broad and len(self.symbols)>32):
            raise ValueError('Invalid news mappings')
        for epic,symbol in self.symbols.items():
            identifier(epic)
            identifier(symbol)

    def add_crypto_markets(self,epics):
        if not (self.broad and self.trial and self.decision_engine=='rules'):
            return 0
        added=0
        for epic in epics:
            identifier(epic)
            if epic not in self.symbols:
                self.symbols[epic]=epic
                added+=1
        return added


class WorkerLock:
    """Shares the old worker's lock on the SAME persistent disk/service."""
    def __init__(self,path):
        Path(path).parent.mkdir(parents=True,exist_ok=True)
        self.file=open(path,'a')
        try:
            fcntl.flock(self.file.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
        except Exception:
            self.file.close()
            raise RuntimeError('Another worker owns this account disk lock') from None
    def close(self):
        self.file.close()


class Worker:
    def __init__(self,config,broker_factory=None,ai_factory=None,news_factory=None,clock=time.time,emit=log):
        self.config,self.clock,self.emit=config,clock,emit
        config.directory.mkdir(parents=True,exist_ok=True)
        self.local=threading.local()
        self.gate=EntryGate({'positions':30,'opportunities':3600 if config.strategy=='hourly_hedge' else 120}
                            if config.decision_engine=='rules' else None)
        self.feed_lock=threading.Lock()
        self.order_lock=threading.RLock()
        self.articles={}
        self.news_checks={}
        # Scheduling is shared across clients but slow I/O does not hold the lock.
        self.rate_lock=threading.Lock()
        self.next_request=self.next_login=0
        self.broker_factory=broker_factory
        self.ai_factory,self.news_factory=ai_factory,news_factory
        self.rotation=0
        self.hourly_scan_hour=None
        self.candidates={}
        self.trend_exit_probe={}
        self.trial_stop_logged=False
        if config.trial:
            original_emit=self.emit
            def trial_emit(event,**fields):
                original_emit(event,**fields)
                try:
                    self.trial_budget().record(event,fields)
                except sqlite3.Error:
                    original_emit('trial_metrics_failed')
            self.emit=trial_emit
            self.emit('trial_started',**self.trial_budget().status(self.clock()))

    def trial_budget(self):
        if not hasattr(self.local,'trial'):
            self.local.trial=TrialBudget(str(self.config.directory/'trial.sqlite'),self.config.account,self.clock())
        return self.local.trial

    def trial_active(self):
        if not self.config.trial:
            return True
        if (self.clock() >= RULES_RENEWAL_AUTHORIZED_AT
                and self.config.decision_engine != 'rules' and not self.config.fast_ai):
            return False
        status=self.trial_budget().status(self.clock())
        if not status['trial_active'] and not self.trial_stop_logged:
            self.trial_stop_logged=True
            self.emit('trial_stopped',**self.trial_budget().summary(self.clock()))
        return status['trial_active']

    def spread_study(self):
        if not hasattr(self.local,'spread_study'):
            self.local.spread_study=SpreadStudy(str(self.config.directory/'spread.sqlite'),self.config.account)
        return self.local.spread_study

    def require_entry(self):
        now=self.clock()
        if self.config.trial:
            self.trial_budget().require_entry(now)
        if not self.gate.may_enter(now):
            raise EntryPreflightBlocked('feed_or_daily_gate_changed_before_submission')

    def evidence_context(self,context,articles):
        with self.feed_lock:
            feed=dict(self.news_checks.get(context['epic'],{}))
        return dict(context,articles=articles,news_feed_healthy=feed.get('healthy',False),
                    news_checked_at=feed.get('checked_at'),entry_evidence_policy=self.config.entry_evidence,
                    strategy_policy=('fast_ai_m15' if self.config.fast_ai else
                                     'independent_hour_day_trends' if self.config.decision_engine=='rules'
                                     else 'legacy_ai'))

    def decision_fields(self,context,scoring):
        checked=(technical_evidence(context,self.clock(),
                                   require_news_health=not self.config.fast_ai)
                 if self.config.technical_entries else None)
        reason=('missing_news' if not context['articles'] and not self.config.technical_entries else
                checked['reason'] if not context['articles'] and checked and not checked['ready'] else
                active_setup(context,self.clock())['reason'] if self.config.technical_entries and not active_setup(context,self.clock())['ready'] else
                scoring['reason'] if scoring['score']<=0 else 'analyst_wait')
        return dict(decision_time=self.clock(),news_articles=len(context['articles']),
                    news_feed_healthy=context.get('news_feed_healthy',False),
                    technical_score=scoring['score'],technical_reason=scoring['reason'],
                    entry_evidence_policy=self.config.entry_evidence,wait_reason=reason,
                    structure_setup=active_setup(context,self.clock()) if self.config.technical_entries else None)

    def institutional_study(self):
        if not hasattr(self.local,'institutional_study'):
            self.local.institutional_study=InstitutionalStudy(
                str(self.config.directory/'institutional.sqlite'),self.config.account)
        return self.local.institutional_study

    @staticmethod
    def spread_fields(scoring):
        return {k:scoring[k] for k in ('spread','atr_15','spread_to_atr','spread_atr_limit',
                                      'spread_gate_reason','timeframes_agree','spread_policy','spread_atr_reference')}

    def observe_spread(self,context,scoring,bars=()):
        # New technical hypotheses are observational only. Their action is
        # never passed to the AI, the coordinator, or the broker.
        if self.config.decision_engine=='rules' or self.config.fast_ai:
            self.emit('spread_metrics',epic=context['epic'],study_recorded=False,
                      **self.spread_fields(scoring))
            return
        context['desk_brief']=market_brief(context,self.clock())
        try:
            observed=self.institutional_study().observe(context,self.clock(),bars)
            if observed['recorded']:
                self.emit('institutional_observation',epic=context['epic'],**observed)
        except Exception as error:
            self.emit('institutional_study_failed',epic=context['epic'],error_type=type(error).__name__)
        try:
            if not hasattr(self.local,'strategy_study'):
                self.local.strategy_study=StrategyStudy(str(self.config.directory/'strategy-study.sqlite'),self.config.account)
            for observation in self.local.strategy_study.observe(context,self.clock(),bars):
                if observation['recorded']:
                    self.emit('setup_forward_observation',epic=context['epic'],**observation)
        except Exception as error:
            self.emit('setup_forward_study_failed',epic=context['epic'],error_type=type(error).__name__)
        recorded=False
        try:
            self.spread_study().observe(context,scoring,self.clock(),bars)
            recorded=True
        except Exception as error:
            # Study failures are observational; they cannot weaken or replace entry gates.
            self.emit('spread_study_failed',epic=context['epic'],error_type=type(error).__name__)
        self.emit('spread_metrics',epic=context['epic'],study_recorded=recorded,
                  candle=context['timeframes']['MINUTE_15']['candles'][-1]['t'],
                  quote_time=context['quote_time'],
                  quote_currency=context.get('quote_currency','USD'),**self.spread_fields(scoring))

    def study_candidate(self,context,scoring,result):
        try:
            costs=self.spread_study().candidate(context,scoring,result,self.clock())
            if costs['recorded']:
                self.emit('entry_candidate_costs',epic=context['epic'],
                          technical_passed=scoring['score']>0,
                          **dict(costs,**self.spread_fields(scoring)))
        except Exception as error:
            self.emit('spread_study_failed',epic=context['epic'],error_type=type(error).__name__)

    def paced_open(self,request,timeout):
        from urllib.request import build_opener
        from .news import NoRedirect
        with self.rate_lock:
            now=time.monotonic()
            scheduled=max(now,self.next_request)
            login=request.get_method()=='POST' and request.full_url.endswith('/session')
            if login:
                scheduled=max(scheduled,self.next_login)
                self.next_login=scheduled+1.1
            self.next_request=scheduled+.15
        time.sleep(max(0,scheduled-time.monotonic()))
        preflight=getattr(request,'capital_preflight',None)
        if preflight:
            preflight()
        if (self.config.trial and request.get_method()=='POST'
                and request.full_url.endswith('/positions')):
            self.require_entry()
        return build_opener(NoRedirect()).open(request,timeout=timeout)

    def resources(self):
        if not hasattr(self.local,'journal'):
            c=self.config
            self.local.daily=(DemoFastAINoDailyGate() if getattr(c,'fast_ai',False) else
                              DemoHourlyNoDailyGate() if c.strategy=='hourly_hedge' else
                              AdaptiveDailyRisk(str(c.directory/'daily.sqlite'),c.account,c.symbols,
                                                selected_market_only=c.broad)
                              if c.risk_mode=='auto' else DailyRisk(str(c.directory/'daily.sqlite'),c.account,c.limit))
            self.local.journal=DemoCoordinator(str(c.directory/'orders.sqlite'),c.account,c.max_positions,
                                              allow_technical_entry=c.technical_entries,
                                              allow_newsless_technical=c.decision_engine=='rules' or getattr(c,'fast_ai',False))
        if not getattr(self.local,'broker',None):
            c=self.config
            broker=(self.broker_factory(self.local.daily) if self.broker_factory else
                CapitalDemo(key=c.key,user=c.user,password=c.password,account_id=c.account,
                            daily_controller=self.local.daily,armed=c.armed,opener=self.paced_open,
                            entry_guard=self.require_entry))
            broker.configure_entries(c.max_positions,self.local.journal.owned)
            broker.login()
            self.local.broker=broker
        return self.local.broker,self.local.journal

    def positions(self):
        # Serialize reconciliation/closures with entry reservations and their
        # confirmations. News and paid analysis do not hold this lock.
        with self.order_lock:
            return self._positions()

    def _positions(self):
        broker,journal=self.resources()
        state=broker.account_state()
        previously_owned=journal.owned() if self.config.trial else {}
        reconciled=journal.reconcile(state,self.clock(),broker)
        if reconciled['broker_confirmation_recovered']:
            self.emit('broker_confirmation_recovered',
                      count=reconciled['broker_confirmation_recovered'])
        if reconciled['historical_review_released']:
            self.emit('historical_order_review_released',
                      count=reconciled['historical_review_released'],
                      outcome='not_verified_no_open_exposure')
        for deal in set(self.trend_exit_probe)-set(reconciled['owned']):
            self.trend_exit_probe.pop(deal,None)
        if self.config.trial:
            actual_ids={item['position']['dealId'] for item in state['positions']}
            for deal in previously_owned.keys()-actual_ids:
                self.emit('position_closed_observed',deal_id=deal,epic=previously_owned[deal]['epic'])
            try:
                self.trial_budget().observe_account(self.clock(),state['equity'],len(state['positions']))
            except sqlite3.Error:
                self.emit('trial_metrics_failed')
        self.gate.stopped(state['daily']['stopped'])
        usable=not (reconciled['unknown'] or reconciled['changed'] or reconciled['unresolved'] or state['working_orders'])
        if not usable:
            self.emit('account_review_required',unknown_positions=len(reconciled['unknown']),
                      changed_positions=len(reconciled['changed']),unresolved=reconciled['unresolved'],
                      broker_recovery_review=reconciled['broker_recovery_review'],
                      **journal.review_summary())
        for item in state['positions']:
            pos=item['position']
            deal=pos['dealId']
            plan=reconciled['owned'].get(deal)
            if not plan:
                continue
            protected=type(pos.get('stopLevel')) in (int,float) and math.isfinite(pos['stopLevel']) and pos['stopLevel']>0
            if (self.config.strategy=='hourly_hedge'
                    and plan.get('strategy')!='hourly_hedge_v1'):
                decision=dict(action='close_intent',reason='superseded_trend_position')
            elif plan.get('strategy')=='hourly_hedge_v1' and plan.get('risk_basis')=='hourly_reference_unbounded':
                # The time exit must still be attempted after a restart or a
                # stale price feed. A profit exit needs a fresh quote.
                now=self.clock()
                if state['daily']['stopped']:
                    decision=dict(action='close_intent',reason='daily_loss_limit')
                elif now>=plan['hour_start']+3600:
                    decision=dict(action='close_intent',reason='hourly_window_end')
                else:
                    quote=broker.market_quote(plan['epic'])
                    if not 0<=now-quote['quote_time']<=30:
                        decision=dict(action='alert',reason='stale_quote_block_new_entries')
                    else:
                        reason=should_close(plan,pos,now)
                        decision=(dict(action='close_intent',reason=reason) if reason
                                  else dict(action='hold',reason=None))
            elif (plan.get('strategy')=='ai_fast_m15_v1'
                  and self.clock() >= plan['signal_candle']+900+3600):
                # The broker stop/target remain in force even if this time
                # exit cannot be sent (market closure, transport failure).
                try:
                    broker.market_quote(plan['epic'])
                except CapitalError:
                    decision=dict(action='alert',reason='fast_exit_market_unavailable')
                else:
                    decision=dict(action='close_intent',reason='fast_window_end')
            else:
                quote=broker.market_quote(plan['epic'])
                view=PositionView(deal,True,pos['direction'],pos['stopLevel'] if protected else plan['stop_level'],
                                  quote['bid'],quote['ask'],quote['quote_time'],True,protected)
                decision=exit_decision(view,self.clock(),state['daily']['stopped'])
                if decision['action']=='hold' and plan.get('strategy')=='hourly_hedge_v1':
                    reason=should_close(plan,pos,self.clock())
                    if reason:
                        decision=dict(action='close_intent',reason=reason)
            if (decision['action']=='hold' and plan.get('target_level') is None
                    and plan.get('strategy')!='hourly_hedge_v1'):
                try:
                    if self.trend_reversed(broker,deal,plan,pos['direction']):
                        decision=dict(action='close_intent',reason='confirmed_trend_reversal')
                except (CapitalError,MarketDataError,ValueError,KeyError,TypeError) as error:
                    self.trend_exit_probe[deal]=self.clock()+60
                    self.emit('trend_exit_unavailable',deal_id=deal,epic=plan['epic'],
                              error_type=type(error).__name__)
            if decision['action']=='close_intent':
                result=journal.close_position(deal,decision['reason'],broker,armed=self.config.armed)
                self.emit(result['status'],deal_id=deal,reason=decision['reason'])
            elif decision['action']=='alert':
                usable=False
                self.emit('position_alert',deal_id=deal,reason=decision['reason'])
        self.emit('smart_heartbeat',mode=self.config.mode,equity=state['equity'],
                  open_positions=len(state['positions']),daily_stopped=state['daily']['stopped'],
                  analysis_mode=self.config.analysis_mode,markets=len(self.config.symbols),
                  daily_analysis_cap=self.config.calls,armed=self.config.armed,
                  entry_evidence_policy=self.config.entry_evidence)
        if self.config.trial:
            self.emit('trial_status',**self.trial_budget().status(self.clock()))
            self.trial_active()
        return usable

    def trend_reversed(self,broker,deal,plan,direction):
        """Read the owned entry route only, after its next closed candle."""
        now=self.clock()
        if now < self.trend_exit_probe.get(deal,0):
            return False
        route=plan['trend_route']
        duration={'HOUR':3600,'DAY':86400}[route]
        rows=broker.prices(plan['epic'],route,40)
        bars=trend_candles(rows,route,now)
        if not bars:
            raise MarketDataError('No completed trend exit candle')
        latest=bars[-1]['t']
        reversed_=reversal_exit(bars,route,direction,plan['trend_signal_at'],now)
        self.trend_exit_probe[deal]=max(now+30,latest+2*duration+2)
        self.emit('trend_exit_checked',deal_id=deal,epic=plan['epic'],
                  route=route,candle=latest,reversed=reversed_)
        return reversed_

    def entry_state(self,broker,journal):
        with self.order_lock:
            state=broker.account_state()
            return state,journal.reconcile(state)

    def capacity_reason(self,epic,state,reconciled):
        if (state['working_orders'] or reconciled['unresolved']
                or reconciled['unknown'] or reconciled['changed']):
            return 'account_ownership_or_order_review'
        if self.config.max_positions and len(state['positions'])>=self.config.max_positions:
            return 'pair_position_limit'
        if self.config.max_positions==2 and any(plan['epic']!=epic for plan in reconciled['owned'].values()):
            return 'other_pair_open'
        return None

    def news(self):
        if self.config.decision_engine=='rules' or self.config.fast_ai:
            return True  # Price-only and fast-AI modes do not depend on a news provider.
        if not hasattr(self.local,'news'):
            self.local.news=(self.news_factory() if self.news_factory else
                NewsClient(self.config.news_key,str(self.config.directory/'news.sqlite')))
        general=self.local.news.fetch('__GENERAL__') if self.config.broad else None
        healthy=False
        for epic,symbol in self.config.symbols.items():
            feed=general if general is not None else self.local.news.fetch(symbol)
            # A symbol-specific request alone is insufficient if article tags disagree.
            articles=(relevant_articles(epic,feed['articles'],self.clock()) if self.config.broad else
                      [a for a in feed['articles'] if symbol in a.get('symbols',[]) and
                       0<=self.clock()-timestamp(a['published_utc'])<=21600])
            with self.feed_lock:
                self.articles[epic]=articles
                verified=feed.get('healthy',feed['status'] in {'fetched','cached'}) is True
                self.news_checks[epic]=dict(healthy=verified,checked_at=feed.get('checked_at',self.clock()))
                healthy=healthy or verified
            self.emit('news_ready',epic=epic,status=feed['status'],articles=len(articles),
                      feed_mode='general' if self.config.broad else 'symbol',feed_healthy=verified,
                      entry_evidence_policy=self.config.entry_evidence,checked_at=feed.get('checked_at'),
                      rejection_counts={r:feed.get('rejected',[]).count(r) for r in set(feed.get('rejected',[]))})
        with self.feed_lock:
            return healthy if self.config.technical_entries else any(self.articles.values())

    def opportunities(self):
        if not self.trial_active():
            return True  # protection runs independently; no automatic fallback
        if self.config.strategy=='hourly_hedge':
            return self.hourly_opportunities()
        if self.config.broad:
            return self.broad_opportunities()
        broker,journal=self.resources()
        state,reconciled=self.entry_state(broker,journal)
        self.gate.stopped(state['daily']['stopped'])
        epics=list(self.config.symbols)
        epic=epics[self.rotation%len(epics)]
        self.rotation+=1
        with self.feed_lock:
            articles=[dict(a,title=a['title'][:500],content=a['content'][:1000])
                      for a in sorted(self.articles.get(epic,[]),key=lambda x:timestamp(x['published_utc']),reverse=True)
                      if 0<=self.clock()-timestamp(a['published_utc'])<=21600][:5]
        collector=MarketCollector(broker,self.clock)
        collector.rules_only=self.config.decision_engine=='rules'
        collector.fast_ai=self.config.fast_ai
        context=collector.collect_market(epic)
        if collector.rules_only:
            context=collector.attach_trend_daily(context)
        scoring=rank(context)
        self.observe_spread(context,scoring,getattr(collector,'execution_candles',()))
        if self.config.continuous:
            return self.monitor_market(epic,context,scoring,articles)
        return self.evaluate(epic,context,articles,state,reconciled,broker,journal)

    def hourly_opportunities(self):
        """Attempt both directions on every eligible market once each UTC hour."""
        epics=list(self.config.symbols)
        sweep_hour=hour_start(self.clock())
        if self.hourly_scan_hour==sweep_hour:
            return True
        # Close the previous candle's owned positions before reserving any
        # entries for the new candle. Retry the sweep if reconciliation fails.
        if not self.positions():
            self.emit('hourly_sweep_deferred',hour_start=sweep_hour,reason='account_review_required')
            return False
        self.gate.success('positions',self.clock())
        self.gate.success('opportunities',self.clock())
        broker,journal=self.resources()
        state,_=self.entry_state(broker,journal)
        self.hourly_scan_hour=sweep_hour
        if state['daily']['stopped']:
            self.emit('hourly_sweep_deferred',hour_start=sweep_hour,reason='daily_loss_limit')
            return True
        self.emit('hourly_sweep_started',hour_start=sweep_hour,markets=len(epics))
        attempted=0
        for epic in epics:
            if hour_start(self.clock())!=sweep_hour or not self.trial_active():
                break
            attempted+=1
            try:
                if self.config.risk_mode=='auto' and self.config.strategy!='hourly_hedge':
                    # Adaptive daily-loss policy needs current closed candles.
                    from .candles import normalize_candles
                    rows=broker.prices(epic,'MINUTE_15',40)
                    frame=normalize_candles(rows,'MINUTE_15',self.clock())
                    self.local.daily.update_market(epic,frame,self.clock())
                    self.local.daily.activate(epic)
                state,reconciled=self.entry_state(broker,journal)
                self.gate.stopped(state['daily']['stopped'])
                if state['daily']['stopped']:
                    self.emit('hourly_sweep_deferred',hour_start=sweep_hour,reason='daily_loss_limit')
                    break
                reason=self.capacity_reason(epic,state,reconciled)
                if reason or not state['daily'].get('ready',True) or not self.gate.may_enter(self.clock()):
                    self.emit('hourly_entry_blocked',epic=epic,
                              reason=reason or 'daily_or_feed_gate')
                    continue
                # Each broker order must be confirmed separately. A shared
                # account or broker limit may reject the second direction.
                for direction in ('BUY','SELL'):
                    if hour_start(self.clock())!=sweep_hour or not self.trial_active():
                        break
                    with self.order_lock:
                        result=journal.process_hourly(epic,direction,broker,
                                                      now=self.clock(),armed=self.config.armed)
                    self.emit('hourly_entry_'+result['status'],epic=epic,direction=direction,
                              **{k:v for k,v in result.items() if k not in ('status','plan')})
            except (CapitalError,MarketDataError,ValueError,EntryPreflightBlocked) as error:
                self.emit('hourly_market_unavailable',epic=epic,
                          error_type=type(error).__name__,reason=str(error))
            except CoordinationError as error:
                self.emit('hourly_order_review',epic=epic,reason=str(error))
        self.emit('hourly_sweep_finished',hour_start=sweep_hour,
                  markets=len(epics),attempted=attempted)
        return True

    def broad_opportunities(self):
        broker,journal=self.resources()
        epics=([epic for epic in self.config.symbols if epic in MARKETS]
               if self.config.core_rules_only else list(self.config.symbols))
        epic=epics[self.rotation%len(epics)]
        self.rotation+=1
        observed=None
        try:
            collector=MarketCollector(broker,self.clock)
            collector.rules_only=self.config.decision_engine=='rules'
            collector.fast_ai=self.config.fast_ai
            context=collector.collect_market(epic)
            if (self.config.decision_engine=='rules' and epic in MARKETS
                    and hasattr(collector,'attach_trend_daily')):
                context=collector.attach_trend_daily(context)
            scoring=rank(context)
            self.observe_spread(context,scoring,getattr(collector,'execution_candles',()))
            self.candidates[epic]=dict(context=context,**scoring)
            if self.config.risk_mode=='auto':
                self.local.daily.update_market(epic,context['timeframes']['MINUTE_15'],self.clock())
            self.emit('market_scanned',epic=epic,asset_class=MARKETS[epic][0] if epic in MARKETS else 'crypto',
                      score=scoring['score'],reason=scoring['reason'],
                      execution_supported=context['execution_supported'],quote_currency=context['quote_currency'],
                      trend_daily_bars=len(context.get('trend_daily',[])),
                      trend_daily_issue=context.get('trend_daily_issue'),
                      **self.spread_fields(scoring))
            observed=(context,scoring)
        except Exception as error:
            self.candidates.pop(epic,None)
            self.emit('market_unavailable',epic=epic,error_type=type(error).__name__,
                      reason=str(error) if isinstance(error,CapitalError) else 'invalid_or_unavailable_observations')
        if self.config.continuous:
            # Every observed market gets its own AI reservation, even when it
            # ranks zero, has no news, or the account cannot accept a new entry.
            if observed is not None:
                self.monitor_market(epic,*observed)
            if self.rotation%len(epics)==0:
                values=sorted(self.candidates.items(),key=lambda item:(-item[1]['score'],item[0]))
                self.emit('universe_ranked',scanned=len(epics),available=len(values),
                          analysis_mode=self.config.analysis_mode,
                          top=[dict(epic=e,score=v['score']) for e,v in values[:5]])
            return True
        # Complete a sweep before selecting. One unavailable/closed contract
        # must not prevent observation of the remaining markets.
        if self.rotation%len(epics):
            return True
        state,reconciled=self.entry_state(broker,journal)
        self.gate.stopped(state['daily']['stopped'])
        if state['working_orders'] or reconciled['unresolved'] or reconciled['unknown'] or reconciled['changed']:
            self.emit('entry_blocked',reason='account_not_flat_or_unresolved')
            return True
        eligible=[]
        with self.feed_lock:
            for candidate,value in self.candidates.items():
                ctx=value['context']
                articles=relevant_articles(candidate,self.articles.get(candidate,[]),self.clock())
                if (value['score']>0 and ctx['execution_supported'] and articles
                        and 0<=self.clock()-ctx['collected_at']<=300
                        and self.capacity_reason(candidate,state,reconciled) is None):
                    eligible.append((value['score'],candidate,articles))
        eligible.sort(key=lambda item:(-item[0],item[1]))
        self.emit('universe_ranked',scanned=len(epics),available=len(self.candidates),
                  eligible=len(eligible),top=[dict(epic=e,score=s) for s,e,_ in eligible[:5]])
        if not eligible:
            self.emit('entry_blocked',reason='no_eligible_market_with_fresh_news')
            return True
        _,epic,articles=eligible[0]
        # Cached scans rank candidates; fresh quotes and candles build the
        # actual AI input and the broker independently checks the order again.
        context=MarketCollector(broker,self.clock).collect_market(epic)
        if self.config.risk_mode=='auto':
            self.local.daily.activate(epic)
        articles=[dict(a,title=a['title'][:500],content=a['content'][:1000])
                  for a in sorted(articles,key=lambda a:timestamp(a['published_utc']),reverse=True)[:5]]
        return self.evaluate(epic,context,articles,state,reconciled,broker,journal)

    def monitor_market(self,epic,context,scoring,articles=None):
        if not self.trial_active():
            return True
        if self.config.decision_engine=='rules':
            return self.rules_market(epic,context,scoring)
        if articles is None:
            with self.feed_lock:
                articles=relevant_articles(epic,self.articles.get(epic,[]),self.clock())
        articles=[dict(a,title=a.get('title','')[:500],content=a.get('content','')[:1000])
                  for a in sorted(articles,key=lambda a:timestamp(a['published_utc']),reverse=True)[:5]]
        context=dict(self.evidence_context(context,articles),purpose='market_monitoring')
        validate_context(context,self.clock(),allow_missing_news=True)
        if not hasattr(self.local,'ai'):
            c=self.config
            self.local.ai=(self.ai_factory() if self.ai_factory else
                           AnalysisClient(c.ai_key,c.model,str(c.directory/'analysis.sqlite'),
                                          daily_calls=24 if c.fast_ai else None,allow_missing_news=True,
                                          allow_technical_entry=c.technical_entries,
                                          allow_newsless_technical=c.fast_ai,
                                          fast_ai=c.fast_ai,
                                          trial=self.trial_budget() if c.trial else None))
        if not hasattr(self.local,'schedule'):
            self.local.schedule=MarketSchedule(str(self.config.directory/'scans.sqlite'),
                                               self.config.account,self.config.analysis_interval)
        candle=context['timeframes']['MINUTE_15']['candles'][-1]['t']
        reservation=self.local.schedule.reserve(epic,candle,self.clock())
        if not reservation['reserved']:
            self.emit('analysis_scheduled',epic=epic,analysis_mode=self.config.analysis_mode,
                      scheduling='per_market',next_at=reservation['next_at'],reason=reservation['reason'])
            return True
        if self.config.fast_ai:
            candidate=active_setup(context,self.clock())
            if not candidate['ready']:
                self.local.schedule.finish(epic,candle,'done')
                self.emit('fast_candidate_skipped',epic=epic,candle=candle,
                          reason=candidate['reason'])
                return True
        self.emit('analysis_started',epic=epic,analysis_mode=self.config.analysis_mode,candle=candle,
                  news_articles=len(articles),execution_supported=context.get('execution_supported',True))
        try:
            result=self.local.ai.analyze(context)
            validate_recommendation(result,context,self.clock(),allow_missing_news=True,
                                    allow_technical_entry=self.config.technical_entries,
                                    allow_newsless_technical=self.config.fast_ai)
        except TrialStopped:
            self.local.schedule.finish(epic,candle,'failed')
            self.trial_active()
            return True
        except Exception as error:
            self.local.schedule.finish(epic,candle,'failed')
            fields=dict(epic=epic,analysis_mode=self.config.analysis_mode,error_type=type(error).__name__)
            # Only the fixed HTTP status label is exposed, never request URLs,
            # credentials, model bodies, or arbitrary exception text.
            if isinstance(error,AnalysisError) and str(error).startswith('OpenAI HTTP status '):
                fields['http_status']=int(str(error).rsplit(' ',1)[1])
            self.emit('analysis_failed',**fields)
            return True
        self.local.schedule.finish(epic,candle,'done')
        diagnostics=self.decision_fields(context,scoring)
        self.emit('analysis_ready',epic=epic,analysis_mode=self.config.analysis_mode,action=result['action'],
                  assessment=result['assessment'],reason=result['reason'],**diagnostics)
        if result['action']=='WAIT':
            self.emit('entry_wait',epic=epic,reason=diagnostics['wait_reason'],rationale=result['reason'])
            return True
        if context.get('execution_supported',True) and articles:
            self.study_candidate(context,scoring,result)
        range_ready=(self.config.technical_entries and active_setup(context,self.clock())['ready']
                     and active_setup(context,self.clock())['strategy']=='range_rejection_v1')
        if not context.get('execution_supported',True) or (not articles and not self.config.technical_entries) or (scoring['score']<=0 and not range_ready and not self.config.fast_ai):
            blocked_filters=[]
            if not context.get('execution_supported',True):
                blocked_filters.append('execution_unsupported')
            if not articles and not self.config.technical_entries:
                blocked_filters.append('missing_news')
            if scoring['score']<=0 and not range_ready:
                blocked_filters.append('technical_filter')
            self.emit('entry_blocked',epic=epic,reason='execution_news_or_technical_filter',
                      blocked_filters=blocked_filters,analysis_action=result['action'],
                      execution_supported=context.get('execution_supported',True),
                      news_articles=len(articles),technical_score=scoring['score'],
                      technical_reason=scoring['reason'],candle=candle,**self.spread_fields(scoring))
            return True
        broker,journal=self.resources()
        if self.config.risk_mode=='auto':
            self.local.daily.activate(epic)
        state,reconciled=self.entry_state(broker,journal)
        return self.evaluate(epic,context,articles,state,reconciled,broker,journal,recommendation=result)

    def rules_market(self,epic,context,scoring):
        """Independent hourly and daily closed-candle routes, without paid AI."""
        now=self.clock()
        context=self.evidence_context(context,[])
        validate_context(context,now,allow_missing_news=True)
        if not hasattr(self.local,'schedule'):
            self.local.schedule=MarketSchedule(str(self.config.directory/'scans.sqlite'),
                                               self.config.account,self.config.analysis_interval)
        from .trend_following import frame_setup
        candidates=[frame_setup(context,route,now) for route in ('HOUR','DAY')]
        selected=None
        for candidate in candidates:
            if not candidate['ready']:
                continue
            candle=candidate['signal_at']
            key=epic+':'+candidate['route']
            if self.local.schedule.reserve(key,candle,now)['reserved']:
                selected=candidate
                break
        if selected is None:
            candle=context['timeframes']['MINUTE_15']['candles'][-1]['t']
            key=epic+':monitor'
            if not self.local.schedule.reserve(key,candle,now)['reserved']:
                return True
            self.emit('rules_evaluated',epic=epic,candle=candle,ready=False,
                      reason='no_new_trend_signal',hour_reason=candidates[0]['reason'],
                      day_reason=candidates[1]['reason'],news_independent=True)
            self.emit('entry_wait',epic=epic,reason='no_new_trend_signal')
            self.local.schedule.finish(key,candle,'done')
            return True
        context=dict(context,trend_route=selected['route'],trend_signal_at=candle)
        completed=False
        try:
            checked=technical_evidence(context,now,require_news_health=False)
            setup=active_setup(context,now)
            self.emit('rules_evaluated',epic=epic,candle=candle,ready=checked['ready'],
                      reason=checked['reason'],strategy=setup['strategy'],
                      timeframe=selected['route'],
                      setup_reason=setup['reason'],rank_reason=scoring['reason'],
                      news_independent=True,technical_score=scoring['score'])
            if not checked['ready']:
                self.emit('entry_wait',epic=epic,reason=checked['reason'])
                completed=True
                return True
            result=dict(action=checked['direction'],assessment='acceptable',
                        stop_level=setup['stop_level'],target_level=setup['target_level'],
                        reason=setup['reason'],article_ids=[])
            validate_recommendation(result,context,now,allow_technical_entry=True,
                                    allow_newsless_technical=True)
            broker,journal=self.resources()
            if self.config.risk_mode=='auto':
                self.local.daily.activate(epic)
            state,reconciled=self.entry_state(broker,journal)
            outcome=self.evaluate(epic,context,[],state,reconciled,broker,journal,recommendation=result)
            completed=True
            return outcome
        finally:
            self.local.schedule.finish(key,candle,'done' if completed else 'failed')

    def evaluate(self,epic,context,articles,state,reconciled,broker,journal,recommendation=None):
        if not self.trial_active():
            return True
        context=self.evidence_context(context,articles)
        if self.config.risk_mode=='auto':
            self.local.daily.update_market(epic,context['timeframes']['MINUTE_15'],self.clock())
            state['daily']=self.local.daily(state['equity'],self.clock())
            self.gate.stopped(state['daily']['stopped'])
            self.emit('daily_risk_ready',mode='auto',limit=state['daily']['limit'],
                      remaining=state['daily']['remaining'],stressed=state['daily']['stressed'],
                      market_data_ready=state['daily']['ready'])
        self.emit('market_data_ready',epic=epic,closed_bars={k:len(v['candles']) for k,v in context['timeframes'].items()})
        capacity=self.capacity_reason(epic,state,reconciled)
        if (not articles and not self.config.technical_entries) or capacity or not state['daily'].get('ready',True):
            self.emit('entry_blocked',epic=epic,reason=capacity or 'news_or_daily_gate')
            return True  # observations usable; no entry performed
        if not articles and self.config.technical_entries:
            checked=technical_evidence(context,self.clock(),
                                       require_news_health=self.config.decision_engine!='rules' and not self.config.fast_ai)
            if not checked['ready']:
                self.emit('entry_blocked',epic=epic,reason=checked['reason'])
                return True
        self.gate.success('opportunities',self.clock())
        if not self.gate.may_enter(self.clock()):
            self.emit('entry_blocked',epic=epic,reason='feed_or_daily_gate')
            return True
        context['articles']=articles
        if self.config.decision_engine!='rules':
            context['desk_brief']=market_brief(context,self.clock())
        validate_context(context,self.clock(),allow_missing_news=self.config.technical_entries)
        if recommendation is not None:
            if not self.trial_active():
                return True
            with self.order_lock:
                signal_id=(str(context['trend_route'])+':'+str(context['trend_signal_at'])
                           if self.config.decision_engine=='rules' else
                           str(context['timeframes']['MINUTE_15']['candles'][-1]['t']))
                outcome=journal.process(signal_id,
                                        recommendation,context,broker,armed=self.config.armed)
            self.emit('entry_'+outcome['status'],epic=epic,**{k:v for k,v in outcome.items() if k!='status'})
            return True
        if not hasattr(self.local,'ai'):
            c=self.config
            self.local.ai=(self.ai_factory() if self.ai_factory else
                          AnalysisClient(c.ai_key,c.model,str(c.directory/'analysis.sqlite'),daily_calls=c.calls,
                                          allow_technical_entry=c.technical_entries,
                                          allow_newsless_technical=c.fast_ai,fast_ai=c.fast_ai,
                                          trial=self.trial_budget() if c.trial else None))
            self.local.scans=sqlite3.connect(c.directory/'scans.sqlite',timeout=10)
            self.local.scans.execute('PRAGMA synchronous=FULL')
            self.local.scans.execute('CREATE TABLE IF NOT EXISTS scans(account TEXT,epic TEXT,candle REAL,PRIMARY KEY(account,epic,candle))')
            self.local.scans.execute('CREATE TABLE IF NOT EXISTS analysis_schedule(account TEXT PRIMARY KEY,next_at REAL NOT NULL)')
            self.local.scans.commit()
        candle=context['timeframes']['MINUTE_15']['candles'][-1]['t']
        self.local.scans.execute('BEGIN IMMEDIATE')
        try:
            scheduled=self.local.scans.execute('SELECT next_at FROM analysis_schedule WHERE account=?',(self.config.account,)).fetchone()
            if scheduled and self.clock()<scheduled[0]:
                self.local.scans.rollback()
                self.emit('analysis_scheduled',epic=epic,next_at=scheduled[0])
                return True
            inserted=self.local.scans.execute('INSERT OR IGNORE INTO scans VALUES(?,?,?)',(self.config.account,epic,candle)).rowcount
            if inserted:
                self.local.scans.execute('INSERT OR REPLACE INTO analysis_schedule VALUES(?,?)',
                                        (self.config.account,self.clock()+86400/self.config.calls))
            self.local.scans.commit()
        except Exception:
            self.local.scans.rollback()
            raise
        if not inserted:
            return True
        result=self.local.ai.analyze(context)
        validate_context(context,self.clock(),allow_missing_news=self.config.technical_entries)
        self.emit('analysis_ready',epic=epic,action=result['action'],assessment=result['assessment'],reason=result['reason'])
        if result['action'] in {'BUY','SELL'}:
            self.study_candidate(context,rank(context),result)
        with self.order_lock:
            outcome=journal.process(str(candle),result,context,broker,armed=self.config.armed)
        self.emit('entry_'+outcome['status'],epic=epic,**{k:v for k,v in outcome.items() if k!='status'})
        return True

    def job(self,name):
        def run():
            try:
                return getattr(self,name)()
            except Exception as error:
                if name=='news':
                    from .news import NewsError
                    text=str(error) if isinstance(error,NewsError) else ''
                    fields={'reason':'news_provider_failure','error_type':type(error).__name__}
                    if text.startswith('EODHD HTTP status ') and text.rsplit(' ',1)[1].isdigit():
                        fields.update(reason='news_http_error',http_status=int(text.rsplit(' ',1)[1]))
                    elif text=='EODHD network request failed': fields['reason']='news_network_error'
                    elif text=='EODHD_API_KEY is missing': fields['reason']='news_key_missing'
                    elif isinstance(error,NewsError): fields['reason']='news_response_invalid'
                    self.emit('news_failed',**fields)
                # Refresh authentication only on the NEXT observation cycle;
                # never repeat or relogin inside an order mutation sequence.
                self.local.broker=None
                raise
        return run


def main():
    parser=argparse.ArgumentParser(description='Capital AI demo worker')
    parser.add_argument('--once',action='store_true',help='one preview pass, no orders')
    args=parser.parse_args()
    lock=None
    try:
        config=Config(os.environ)
        if args.once and config.armed:
            raise ValueError('--once is available in preview only')
        lock=WorkerLock(config.legacy+'.lock')
        if config.strategy=='hourly_hedge':
            try:
                catalogue=CapitalDemo(key=config.key,user=config.user,password=config.password,
                                      account_id=config.account,daily_controller=None,armed=False)
                catalogue.login()
                epics=catalogue.available_hourly_epics()
                config.symbols={epic:epic for epic in epics}
                log('hourly_universe_discovered',markets=len(epics))
            except CapitalError as error:
                log('hourly_universe_fallback',markets=len(config.symbols),reason=str(error))
        if config.broad and config.trial and config.decision_engine=='rules' and not config.core_rules_only:
            # Discovery precedes risk-controller creation so its policy includes
            # every account-visible crypto market. Existing markets survive a
            # temporary catalogue failure; the failure is visible in logs.
            try:
                catalogue=CapitalDemo(key=config.key,user=config.user,password=config.password,
                                      account_id=config.account,daily_controller=None,armed=False)
                catalogue.login()
                crypto=catalogue.available_crypto_epics()
                added=config.add_crypto_markets(crypto)
                log('crypto_universe_discovered',available=len(crypto),added=added,
                    total_markets=len(config.symbols))
            except CapitalError as error:
                log('crypto_universe_unavailable',reason=str(error))
        worker=Worker(config)
        log('smart_worker_started',mode=config.mode,account_id=config.account,
            market_mode='broad' if config.broad else 'single',markets=len(config.symbols),
            analysis_mode=config.analysis_mode,decision_engine=config.decision_engine,
            daily_analysis_cap=config.calls,
            analysis_interval_seconds=config.analysis_interval,armed=config.armed,
            entry_evidence_policy=config.entry_evidence,
            currency_conversion='USD_broker_quotes',conversion_currencies=sorted(ROUTES),
            spread_study='observe_only_historical_threshold_comparison',spread_policy=SPREAD_POLICY,
            pair_entries=config.max_positions if config.max_positions else None,
            pair_risk_fraction=(float(FAST_AI_PORTFOLIO_FRACTION) if config.fast_ai else
                                .0025 if config.max_positions!=1 else .00125),
            entry_risk_fraction=float(FAST_AI_ENTRY_FRACTION) if config.fast_ai else None,
            entry_policy=('journal_owned_portfolio_no_count_limit' if config.max_positions==0 else
                          'staged_owned_same_pair' if config.max_positions==2 else 'flat_only'),
            leverage_policy='broker_current_no_probability_increase',
            analysis_framework=FRAMEWORK,
            strategy_research='disabled' if config.decision_engine=='rules' else STRATEGY,
            strategy_execution=('ai_fast_m15_demo' if config.fast_ai else
                                'hourly_two_sided_demo' if config.strategy=='hourly_hedge'
                                else 'independent_hour_day_trends_demo'),
            daily_loss_policy=('disabled_fast_ai_demo' if config.fast_ai else
                               'disabled_hourly_demo' if config.strategy=='hourly_hedge'
                               else config.risk_mode),
            trial_ledger_guard='original_or_reviewed_provider_checkpoint',state_directory=str(config.directory))
        if args.once:
            for name in ('positions','news','opportunities'):
                repeats=(len(config.symbols) if config.broad and config.strategy!='hourly_hedge'
                         and name=='opportunities' else 1)
                for i in range(repeats):
                    if repeats>1 and i==repeats-1:
                        if worker.job('positions')():
                            worker.gate.success('positions',time.time())
                        else:
                            worker.gate.failure('positions')
                    usable=worker.job(name)()
                    if usable:
                        worker.gate.success(name,time.time())
                    else:
                        worker.gate.failure(name)
            return
        stop=threading.Event()
        for sig in (signal.SIGTERM,signal.SIGINT):
            signal.signal(sig,lambda *_:stop.set())
        monitor=Monitor(worker.job('positions'),worker.job('opportunities'),worker.job('news'),log,
                        gate=worker.gate,intervals=dict(positions=10,
                        opportunities=5 if config.broad or config.strategy=='hourly_hedge' else 60,news=60))
        monitor.start()
        stop.wait()
        monitor.stop()
    except Exception as error:
        log('smart_worker_failed',error_type=type(error).__name__)
        raise SystemExit(1) from None
    finally:
        if lock:
            lock.close()


if __name__=='__main__':
    main()
