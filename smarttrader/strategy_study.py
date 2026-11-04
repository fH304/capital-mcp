"""Forward evaluation of frozen hypotheses, isolated from broker execution.

Price R includes spread; stressed R additionally models .05 ATR slippage at each
end. Financing and fees are unknown, so neither is labelled actual net profit.
"""
import json
import math
import sqlite3
from collections import Counter
from pathlib import Path
from .strategy import trend_setup, range_setup
from .short_study import short_setup
from .spreadstudy import paper_outcome, COMPLETE, BAR_SECONDS, HORIZON_BARS


def metrics(rows):
    complete=[(p,o) for p,o in rows if o['status'] in COMPLETE]
    values=[]
    for p,o in complete:
        risk=abs(o['paper_entry']-p['stop_level'])
        values.append(o['price_R']-p['slippage_stress_distance']/risk)
    total=peak=drawdown=0.
    for value in values:
        total+=value; peak=max(peak,total); drawdown=max(drawdown,peak-total)
    return dict(completed=len(values),mean_stressed_R=sum(values)/len(values) if values else None,
                cumulative_stressed_R=total if values else None,max_closed_trade_drawdown_R=drawdown if values else None,
                winning_fraction=sum(v>0 for v in values)/len(values) if values else None,
                outcome_counts=dict(Counter(o['status'] for _,o in rows)),
                profitability_validated=False)


class StrategyStudy:
    def __init__(self,path,account):
        self.account=account
        self.db=sqlite3.connect(path,timeout=10)
        self.db.execute('PRAGMA synchronous=FULL')
        self.db.execute('CREATE TABLE IF NOT EXISTS setup_forward('
                        'account TEXT,epic TEXT,candle REAL,version TEXT,observed_at REAL,payload TEXT,outcome TEXT,'
                        'PRIMARY KEY(account,epic,candle,version))')
        self.db.commit()

    def observe(self,context,now,bars=()):
        setups=[trend_setup(context,now),range_setup(context,now),short_setup(context,now)]
        candle=context['timeframes']['MINUTE_15']['candles'][-1]['t']
        results=[]
        with self.db:
            for setup in setups:
                version=setup['strategy']
                # Resolve earlier observations first. No entry bar can precede observation.
                pending=self.db.execute('SELECT candle,payload FROM setup_forward WHERE account=? AND epic=? '
                        'AND version=? AND json_extract(outcome,"$.status")="pending"',
                        (self.account,context['epic'],version)).fetchall()
                for saved,raw in pending:
                    payload=json.loads(raw)
                    outcome=paper_outcome(payload,bars,now)
                    self.db.execute('UPDATE setup_forward SET outcome=? WHERE account=? AND epic=? AND candle=? AND version=?',
                                    (json.dumps(outcome),self.account,context['epic'],saved,version))
                payload=dict(setup,action=setup['direction'] if setup['ready'] else 'WAIT',
                             paper_start=(math.floor(now/BAR_SECONDS)+1)*BAR_SECONDS)
                last=self.db.execute('SELECT payload FROM setup_forward WHERE account=? AND epic=? AND version=? '
                                     'AND json_extract(payload,"$.action")!="WAIT" ORDER BY candle DESC LIMIT 1',
                                     (self.account,context['epic'],version)).fetchone()
                # Fixed holding horizon excludes overlapping same-market paper plans.
                if setup['ready'] and last and now<json.loads(last[0])['paper_start']+HORIZON_BARS*BAR_SECONDS:
                    payload.update(action='WAIT',reason='paper_horizon_overlap',ready=False)
                outcome={'status':'pending' if payload['action']!='WAIT' else 'not_candidate'}
                saved=self.db.execute('INSERT OR IGNORE INTO setup_forward VALUES(?,?,?,?,?,?,?)',
                        (self.account,context['epic'],candle,version,now,json.dumps(payload),json.dumps(outcome))).rowcount
                results.append(dict(strategy=version,ready=payload['ready'],reason=payload['reason'],
                                    recorded=bool(saved),execution_authorized=False))
        return results

    @classmethod
    def report(cls,path,account,now):
        db=sqlite3.connect(Path(path).resolve().as_uri()+'?mode=ro',uri=True,timeout=10)
        try:
            groups={}
            for epic,version,stamp,raw,done in db.execute('SELECT epic,version,observed_at,payload,outcome '
                       'FROM setup_forward WHERE account=? ORDER BY observed_at,candle',(account,)):
                p,o=json.loads(raw),json.loads(done)
                if o['status']=='pending' and now>=p['paper_start']+BAR_SECONDS*HORIZON_BARS:
                    o={'status':'missing_bars'}
                groups.setdefault((epic,version),[]).append((stamp,p,o))
            markets={}
            for (epic,version),rows in groups.items():
                candidates=[(p,o) for _,p,o in rows if p['action']!='WAIT']
                # Freeze 70/30 chronological split by candidate observation, not outcome.
                cut=int(len(candidates)*.7)
                report=dict(observations=len(rows),candidates=len(candidates),
                            setup_counts=dict(Counter(p['reason'] for _,p,_ in rows)),
                            all_forward=metrics(candidates),earlier_segment=metrics(candidates[:cut]),
                            later_holdout_segment=metrics(candidates[cut:]),
                            comparison_ready=(len(candidates)>=100 and all(o['status'] in COMPLETE for _,o in candidates)),
                            execution_authorized=False)
                markets.setdefault(epic,{})[version]=report
            return dict(mode='forward_research_only',markets=markets,split='chronological_70_30_no_parameter_fitting',
                        assumptions={'slippage_per_side_atr':.05,'spread':'included_once_in_bid_ask_execution'},
                        limitations=['financing, commissions and FX fees not modelled; no actual net-profit claim',
                                     'closed-trade drawdown in R, not intratrade or account-equity drawdown',
                                     'independent market series; correlated exposure is not portfolio performance',
                                     'ambiguous and missing bars censored; compare their counts, not only winners',
                                     '100 candidates is a reporting floor, not statistical proof or automatic promotion'],
                        profitability_validated=False)
        finally: db.close()

    def close(self): self.db.close()


def main():
    import argparse,os,time
    parser=argparse.ArgumentParser(description='Read-only prospective setup evaluation')
    parser.add_argument('--report',action='store_true',required=True); parser.parse_args()
    path=Path(os.environ.get('SMART_STATE_DIR','/var/data/smarttrader'))/'strategy-study.sqlite'
    print(json.dumps(StrategyStudy.report(path,os.environ['BOT_ACCOUNT_ID'],time.time()),allow_nan=False))


if __name__=='__main__': main()
