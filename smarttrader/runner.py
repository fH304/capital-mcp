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
from .coordinator import DemoCoordinator
from .daily import DailyRisk, AdaptiveDailyRisk
from .fx import ROUTES
from .marketdata import MarketCollector
from .monitor import EntryGate, Monitor, PositionView, exit_decision
from .news import NewsClient, timestamp
from .universe import MARKETS, relevant_articles, rank
from .schedule import MarketSchedule
from .spreadstudy import SpreadStudy
from .trial import is_approved_account, MODEL as TRIAL_MODEL, TrialBudget, TrialStopped


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
        if not all((self.key,self.user,self.password,self.ai_key,self.model,self.news_key)):
            raise ValueError('Missing worker configuration')
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
        # Two staged legs were explicitly authorized for this demo account.
        self.max_positions=int(env.get('SMART_PAIR_ENTRIES',
                                       '2' if self.trial and is_approved_account(self.account) else '1'))
        if self.max_positions not in (1,2):
            raise ValueError('SMART_PAIR_ENTRIES must be 1 or 2')
        self.analysis_mode=env.get('SMART_ANALYSIS_MODE','budgeted')
        if self.analysis_mode not in {'continuous','budgeted'}:
            raise ValueError('Invalid analysis mode')
        if self.trial:
            self.analysis_mode='trial'
            self.model=TRIAL_MODEL  # fixed model and verified token tariff
        self.continuous=self.analysis_mode in {'continuous','trial'}
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
        self.symbols=({epic:definition[1][0] for epic,definition in MARKETS.items()} if self.broad else
                      json.loads(env.get('SMART_NEWS_SYMBOLS','{"EURUSD":"EURUSD.FOREX"}')))
        if not isinstance(self.symbols,dict) or not 1<=len(self.symbols)<=32:
            raise ValueError('One to 32 news mappings required')
        for epic,symbol in self.symbols.items():
            identifier(epic)
            identifier(symbol)


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
        self.gate=EntryGate()
        self.feed_lock=threading.Lock()
        self.order_lock=threading.RLock()
        self.articles={}
        # Scheduling is shared across clients but slow I/O does not hold the lock.
        self.rate_lock=threading.Lock()
        self.next_request=self.next_login=0
        self.broker_factory=broker_factory
        self.ai_factory,self.news_factory=ai_factory,news_factory
        self.rotation=0
        self.candidates={}
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
        status=self.trial_budget().status(self.clock())
        if not status['trial_active'] and not self.trial_stop_logged:
            self.trial_stop_logged=True
            self.emit('trial_stopped',**self.trial_budget().summary(self.clock()))
        return status['trial_active']

    def spread_study(self):
        if not hasattr(self.local,'spread_study'):
            self.local.spread_study=SpreadStudy(str(self.config.directory/'spread.sqlite'),self.config.account)
        return self.local.spread_study

    @staticmethod
    def spread_fields(scoring):
        return {k:scoring[k] for k in ('spread','atr_15','spread_to_atr','spread_atr_limit',
                                      'spread_gate_reason','timeframes_agree')}

    def observe_spread(self,context,scoring,bars=()):
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
            self.trial_budget().require_entry(self.clock())
        return build_opener(NoRedirect()).open(request,timeout=timeout)

    def resources(self):
        if not hasattr(self.local,'journal'):
            c=self.config
            self.local.daily=(AdaptiveDailyRisk(str(c.directory/'daily.sqlite'),c.account,c.symbols,
                                               selected_market_only=c.broad)
                              if c.risk_mode=='auto' else DailyRisk(str(c.directory/'daily.sqlite'),c.account,c.limit))
            self.local.journal=DemoCoordinator(str(c.directory/'orders.sqlite'),c.account,c.max_positions)
        if not getattr(self.local,'broker',None):
            c=self.config
            broker=(self.broker_factory(self.local.daily) if self.broker_factory else
                CapitalDemo(key=c.key,user=c.user,password=c.password,account_id=c.account,
                            daily_controller=self.local.daily,armed=c.armed,opener=self.paced_open,
                            entry_guard=(lambda:self.trial_budget().require_entry(self.clock())) if c.trial else None))
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
        reconciled=journal.reconcile(state)
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
                      changed_positions=len(reconciled['changed']),unresolved=reconciled['unresolved'])
        for item in state['positions']:
            pos=item['position']
            deal=pos['dealId']
            plan=reconciled['owned'].get(deal)
            if not plan:
                continue
            quote=broker.market_quote(plan['epic'])
            protected=type(pos.get('stopLevel')) in (int,float) and math.isfinite(pos['stopLevel']) and pos['stopLevel']>0
            view=PositionView(deal,True,pos['direction'],pos['stopLevel'] if protected else plan['stop_level'],
                              quote['bid'],quote['ask'],quote['quote_time'],True,protected)
            decision=exit_decision(view,self.clock(),state['daily']['stopped'])
            if decision['action']=='close_intent':
                result=journal.close_position(deal,decision['reason'],broker,armed=self.config.armed)
                self.emit(result['status'],deal_id=deal,reason=decision['reason'])
            elif decision['action']=='alert':
                usable=False
                self.emit('position_alert',deal_id=deal,reason=decision['reason'])
        self.emit('smart_heartbeat',mode=self.config.mode,equity=state['equity'],
                  open_positions=len(state['positions']),daily_stopped=state['daily']['stopped'],
                  analysis_mode=self.config.analysis_mode,markets=len(self.config.symbols),
                  daily_analysis_cap=self.config.calls)
        if self.config.trial:
            self.emit('trial_status',**self.trial_budget().status(self.clock()))
            self.trial_active()
        return usable

    def entry_state(self,broker,journal):
        with self.order_lock:
            state=broker.account_state()
            return state,journal.reconcile(state)

    def capacity_reason(self,epic,state,reconciled):
        if (state['working_orders'] or reconciled['unresolved']
                or reconciled['unknown'] or reconciled['changed']):
            return 'account_ownership_or_order_review'
        if len(state['positions'])>=self.config.max_positions:
            return 'pair_position_limit'
        if any(plan['epic']!=epic for plan in reconciled['owned'].values()):
            return 'other_pair_open'
        return None

    def news(self):
        if not hasattr(self.local,'news'):
            self.local.news=(self.news_factory() if self.news_factory else
                NewsClient(self.config.news_key,str(self.config.directory/'news.sqlite')))
        general=self.local.news.fetch('__GENERAL__') if self.config.broad else None
        for epic,symbol in self.config.symbols.items():
            feed=general if general is not None else self.local.news.fetch(symbol)
            # A symbol-specific request alone is insufficient if article tags disagree.
            articles=(relevant_articles(epic,feed['articles'],self.clock()) if self.config.broad else
                      [a for a in feed['articles'] if symbol in a.get('symbols',[]) and
                       0<=self.clock()-timestamp(a['published_utc'])<=21600])
            with self.feed_lock:
                self.articles[epic]=articles
            self.emit('news_ready',epic=epic,status=feed['status'],articles=len(articles),
                      feed_mode='general' if self.config.broad else 'symbol')
        with self.feed_lock:
            return any(self.articles.values())

    def opportunities(self):
        if not self.trial_active():
            return True  # protection runs independently; no automatic fallback
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
        context=collector.collect_market(epic)
        scoring=rank(context)
        self.observe_spread(context,scoring,getattr(collector,'execution_candles',()))
        if self.config.continuous:
            return self.monitor_market(epic,context,scoring,articles)
        return self.evaluate(epic,context,articles,state,reconciled,broker,journal)

    def broad_opportunities(self):
        broker,journal=self.resources()
        epics=list(self.config.symbols)
        epic=epics[self.rotation%len(epics)]
        self.rotation+=1
        observed=None
        try:
            collector=MarketCollector(broker,self.clock)
            context=collector.collect_market(epic)
            scoring=rank(context)
            self.observe_spread(context,scoring,getattr(collector,'execution_candles',()))
            self.candidates[epic]=dict(context=context,**scoring)
            if self.config.risk_mode=='auto':
                self.local.daily.update_market(epic,context['timeframes']['MINUTE_15'],self.clock())
            self.emit('market_scanned',epic=epic,asset_class=MARKETS[epic][0],
                      score=scoring['score'],reason=scoring['reason'],
                      execution_supported=context['execution_supported'],quote_currency=context['quote_currency'],
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
        if articles is None:
            with self.feed_lock:
                articles=relevant_articles(epic,self.articles.get(epic,[]),self.clock())
        articles=[dict(a,title=a.get('title','')[:500],content=a.get('content','')[:1000])
                  for a in sorted(articles,key=lambda a:timestamp(a['published_utc']),reverse=True)[:5]]
        context=dict(context,articles=articles,purpose='market_monitoring')
        validate_context(context,self.clock(),allow_missing_news=True)
        if not hasattr(self.local,'ai'):
            c=self.config
            self.local.ai=(self.ai_factory() if self.ai_factory else
                           AnalysisClient(c.ai_key,c.model,str(c.directory/'analysis.sqlite'),
                                          daily_calls=None,allow_missing_news=True,
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
        self.emit('analysis_started',epic=epic,analysis_mode=self.config.analysis_mode,candle=candle,
                  news_articles=len(articles),execution_supported=context.get('execution_supported',True))
        try:
            result=self.local.ai.analyze(context)
            validate_recommendation(result,context,self.clock(),allow_missing_news=True)
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
        self.emit('analysis_ready',epic=epic,analysis_mode=self.config.analysis_mode,action=result['action'],
                  assessment=result['assessment'],reason=result['reason'],news_articles=len(articles))
        if result['action']=='WAIT':
            self.emit('entry_wait',epic=epic)
            return True
        if context.get('execution_supported',True) and articles:
            self.study_candidate(context,scoring,result)
        if not context.get('execution_supported',True) or not articles or scoring['score']<=0:
            blocked_filters=[]
            if not context.get('execution_supported',True):
                blocked_filters.append('execution_unsupported')
            if not articles:
                blocked_filters.append('missing_news')
            if scoring['score']<=0:
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

    def evaluate(self,epic,context,articles,state,reconciled,broker,journal,recommendation=None):
        if not self.trial_active():
            return True
        if self.config.risk_mode=='auto':
            self.local.daily.update_market(epic,context['timeframes']['MINUTE_15'],self.clock())
            state['daily']=self.local.daily(state['equity'],self.clock())
            self.gate.stopped(state['daily']['stopped'])
            self.emit('daily_risk_ready',mode='auto',limit=state['daily']['limit'],
                      remaining=state['daily']['remaining'],stressed=state['daily']['stressed'],
                      market_data_ready=state['daily']['ready'])
        self.emit('market_data_ready',epic=epic,closed_bars={k:len(v['candles']) for k,v in context['timeframes'].items()})
        capacity=self.capacity_reason(epic,state,reconciled)
        if not articles or capacity or not state['daily'].get('ready',True):
            self.emit('entry_blocked',epic=epic,reason=capacity or 'news_or_daily_gate')
            return True  # observations usable; no entry performed
        self.gate.success('opportunities',self.clock())
        if not self.gate.may_enter(self.clock()):
            self.emit('entry_blocked',epic=epic,reason='feed_or_daily_gate')
            return True
        context['articles']=articles
        validate_context(context,self.clock())
        if recommendation is not None:
            if not self.trial_active():
                return True
            with self.order_lock:
                outcome=journal.process(str(context['timeframes']['MINUTE_15']['candles'][-1]['t']),
                                        recommendation,context,broker,armed=self.config.armed)
            self.emit('entry_'+outcome['status'],epic=epic,**{k:v for k,v in outcome.items() if k!='status'})
            return True
        if not hasattr(self.local,'ai'):
            c=self.config
            self.local.ai=(self.ai_factory() if self.ai_factory else
                          AnalysisClient(c.ai_key,c.model,str(c.directory/'analysis.sqlite'),daily_calls=c.calls))
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
        validate_context(context,self.clock())
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
            except Exception:
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
        worker=Worker(config)
        log('smart_worker_started',mode=config.mode,account_id=config.account,
            market_mode='broad' if config.broad else 'single',markets=len(config.symbols),
            analysis_mode=config.analysis_mode,daily_analysis_cap=config.calls,
            analysis_interval_seconds=config.analysis_interval,
            currency_conversion='USD_broker_quotes',conversion_currencies=sorted(ROUTES),
            spread_study='observe_only_20pct_unchanged',
            pair_entries=config.max_positions,
            pair_risk_fraction=.0025 if config.max_positions==2 else .00125,
            entry_policy='staged_owned_same_pair' if config.max_positions==2 else 'flat_only',
            leverage_policy='broker_current_no_probability_increase',
            trial_ledger_guard='original_or_reviewed_provider_checkpoint',state_directory=str(config.directory))
        if args.once:
            for name in ('positions','news','opportunities'):
                repeats=len(config.symbols) if config.broad and name=='opportunities' else 1
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
                        gate=worker.gate,intervals=dict(positions=10,opportunities=5 if config.broad else 60,news=60))
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
