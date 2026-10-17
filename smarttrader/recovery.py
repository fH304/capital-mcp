"""Review provider exports offline; explicitly recover one missing demo ledger.

Cost data is an observed billing snapshot, not proof of invoice finalization.
Charge the whole exported organization period (including pretrial use), round
up to cents, and additionally retain every surviving local charge/reservation.
This recovers a budget checkpoint, never the missing trading/request history.
"""
import argparse
import base64
import csv
import hashlib
import io
import json
import os
import time
from decimal import Decimal, InvalidOperation, ROUND_CEILING
from pathlib import Path

from .trial import (APPROVED_ACCOUNT, AUTHORIZED_STARTED_AT, DURATION,
                    LIMIT_NANO, MODEL, INPUT_RATE, CACHED_RATE, OUTPUT_RATE,
                    MAX_INPUT, MAX_OUTPUT, TrialBudget, TrialStopped, is_approved_account)

FIELDS={'schema','trial_id','account','started_at','ends_at','limit_nano',
        'cost_nano','budget_debit_nano','export_requests','observed_at',
        'range_start','range_end','costs_sha256','usage_sha256','scope'}


def canonical(checkpoint):
    return json.dumps(checkpoint,sort_keys=True,separators=(',',':'),allow_nan=False).encode()


def validate_checkpoint(c):
    from .trial import TRIAL_ID
    if not isinstance(c,dict) or set(c)!=FIELDS:
        raise ValueError('Invalid recovery checkpoint fields')
    if (type(c['schema']) is not int or c['schema']!=1 or c['trial_id']!=TRIAL_ID
            or c['account']!=APPROVED_ACCOUNT or not is_approved_account(c['account'])
            or c['started_at']!=AUTHORIZED_STARTED_AT
            or c['ends_at']!=AUTHORIZED_STARTED_AT+DURATION
            or type(c['limit_nano']) is not int or c['limit_nano']!=LIMIT_NANO
            or c['scope']!='organization_total_including_pretrial'):
        raise ValueError('Recovery cannot change the original authorization')
    for key in ('cost_nano','budget_debit_nano','export_requests','range_start','range_end'):
        if type(c[key]) is not int or c[key]<0:
            raise ValueError('Invalid recovery integer')
    expected=((c['cost_nano']+9_999_999)//10_000_000)*10_000_000
    if not 0<c['cost_nano']<=c['budget_debit_nano']==expected<=LIMIT_NANO or c['export_requests']<=0:
        raise ValueError('Invalid historical debit')
    TrialBudget._time(c['observed_at'])
    if not (c['range_start']<=AUTHORIZED_STARTED_AT<=c['observed_at']<c['range_end']
            and c['observed_at']<AUTHORIZED_STARTED_AT+DURATION):
        raise ValueError('Export does not cover the original trial through observation')
    for key in ('costs_sha256','usage_sha256'):
        value=c[key]
        if not isinstance(value,str) or len(value)!=64 or any(x not in '0123456789abcdef' for x in value):
            raise ValueError('Invalid source digest')


def _decimal(value):
    try:
        number=Decimal(value)
    except (InvalidOperation,TypeError):
        raise ValueError('Invalid export number') from None
    if not number.is_finite() or number<0:
        raise ValueError('Invalid export number')
    return number


def _integer(value):
    number=_decimal(value)
    if number!=number.to_integral_value():
        raise ValueError('Fractional export count')
    return int(number)


def _rows(raw,required):
    if not isinstance(raw,bytes) or not 0<len(raw)<=2_000_000:
        raise ValueError('Invalid export size')
    reader=csv.DictReader(io.StringIO(raw.decode('utf-8-sig')))
    if (not reader.fieldnames or len(set(reader.fieldnames))!=len(reader.fieldnames)
            or not required.issubset(reader.fieldnames)):
        raise ValueError('Export columns missing or duplicated')
    rows=list(reader)
    if not rows or any(None in r or any(v is None for v in r.values()) for r in rows):
        raise ValueError('Malformed export rows')
    return rows


def _buckets(rows):
    buckets=set()
    for r in rows:
        start,end=_integer(r['start_time']),_integer(r['end_time'])
        if start%86400 or end-start!=86400:
            raise ValueError('Expected UTC daily export buckets')
        buckets.add((start,end))
    ordered=sorted(buckets)
    if any(a[1]!=b[0] for a,b in zip(ordered,ordered[1:])):
        raise ValueError('Export has missing days')
    return ordered


def review_exports(cost_raw,usage_raw,observed_at):
    """No DB, network, API secrets or authorizing side effects."""
    from .trial import TRIAL_ID
    TrialBudget._time(observed_at)
    costs=_rows(cost_raw,{'start_time','end_time','amount_value','amount_currency','organization_id'})
    usage=_rows(usage_raw,{'start_time','end_time','num_model_requests','model','batch','service_tier',
                         'input_tokens','output_tokens','input_cached_tokens','api_key_id','project_id'})
    buckets=_buckets(costs)
    if buckets!=_buckets(usage):
        raise ValueError('Cost and usage export date coverage differs')
    cost_daily={b:Decimal(0) for b in buckets}
    organizations=set()
    for r in costs:
        if r['amount_value']=='':
            continue
        if r['amount_currency'].lower()!='usd' or not r['organization_id']:
            raise ValueError('Expected identified USD cost data')
        organizations.add(r['organization_id'])
        cost_daily[(_integer(r['start_time']),_integer(r['end_time']))]+=_decimal(r['amount_value'])
    if len(organizations)!=1:
        raise ValueError('Expected one organization cost export')
    token_daily={b:0 for b in buckets}
    requests=0
    keys,projects=set(),set()
    for r in usage:
        if r['num_model_requests']=='':
            if any(r[k] for k in ('model','input_tokens','output_tokens','input_cached_tokens')):
                raise ValueError('Usage row has tokens without a request count')
            continue
        if r['model']!=MODEL or r['batch']!='False' or r['service_tier']!='default':
            raise ValueError('Unpriced model, batch or service tier in export')
        n,inp,out,cached=(_integer(r[k]) for k in
                         ('num_model_requests','input_tokens','output_tokens','input_cached_tokens'))
        if not n or cached>inp or inp>n*MAX_INPUT or out>n*MAX_OUTPUT:
            raise ValueError('Usage exceeds the trial request contract')
        for k in ('input_audio_tokens','input_cached_audio_tokens','output_audio_tokens',
                  'input_image_tokens','input_cached_image_tokens','output_image_tokens',
                  'input_cache_write_tokens','input_cache_write_12h_tokens'):
            if r.get(k) and _decimal(r[k])!=0:
                raise ValueError('Non-text usage cannot be reconciled at the trial tariff')
        if not r['api_key_id'] or not r['project_id']:
            raise ValueError('Usage export must identify its key and project')
        keys.add(r['api_key_id'])
        projects.add(r['project_id'])
        requests+=n
        token_daily[(_integer(r['start_time']),_integer(r['end_time']))]+=(
            (inp-cached)*INPUT_RATE+cached*CACHED_RATE+out*OUTPUT_RATE)
    if len(keys)!=1 or len(projects)!=1:
        raise ValueError('Expected one identified bot key and project')
    if any(cost_daily[b]*1_000_000_000!=token_daily[b] for b in buckets):
        raise ValueError('Provider cost and token usage disagree by day')
    nano=int((sum(cost_daily.values())*1_000_000_000).to_integral_value(rounding=ROUND_CEILING))
    c=dict(schema=1,trial_id=TRIAL_ID,account=APPROVED_ACCOUNT,
           started_at=AUTHORIZED_STARTED_AT,ends_at=AUTHORIZED_STARTED_AT+DURATION,
           limit_nano=LIMIT_NANO,cost_nano=nano,
           budget_debit_nano=((nano+9_999_999)//10_000_000)*10_000_000,
           export_requests=requests,observed_at=observed_at,
           range_start=buckets[0][0],range_end=buckets[-1][1],
           costs_sha256=hashlib.sha256(cost_raw).hexdigest(),
           usage_sha256=hashlib.sha256(usage_raw).hexdigest(),
           scope='organization_total_including_pretrial')
    validate_checkpoint(c)
    return c


def require_persistent_state(directory):
    mount=Path('/var/data')
    resolved=Path(directory).resolve(strict=True)
    if not os.path.ismount(mount) or not resolved.is_relative_to(mount) or resolved==mount:
        raise TrialStopped('recovery_requires_mounted_persistent_state')
    if not (resolved/'trial.sqlite').is_file():
        raise TrialStopped('recovery_requires_existing_blocked_database')
    return resolved


def apply_reviewed(checkpoint,env,clock=time.time,broker_factory=None):
    """Read-only demo broker check, then one explicit DB reconciliation."""
    validate_checkpoint(checkpoint)
    now=clock()
    TrialBudget._time(now)
    if not AUTHORIZED_STARTED_AT <= now < AUTHORIZED_STARTED_AT+DURATION:
        raise TrialStopped('recovery_outside_original_duration')
    if not 0 <= now-checkpoint['observed_at'] <= 6*3600:
        raise TrialStopped('recovery_export_not_recent')
    if (env.get('CAP_ENV')!='demo' or env.get('BOT_ACCOUNT_ID')!=APPROVED_ACCOUNT
            or env.get('SMART_MODE')!='demo' or env.get('SMART_ARMED')!='DEMO_ONLY'
            or env.get('SMART_EXCLUSIVE_ACCOUNT')!=APPROVED_ACCOUNT
            or env.get('SMART_MARKETS')!='broad' or env.get('SMART_TRIAL_ENABLED','1')!='1'):
        raise TrialStopped('recovery_requires_authorized_demo_configuration')
    directory=require_persistent_state(env.get('SMART_STATE_DIR','/var/data/smarttrader'))
    path=directory/'trial.sqlite'
    before=TrialBudget.report(path,APPROVED_ACCOUNT,clock())
    if before['stop_reason']!='trial_state_missing':
        raise TrialStopped('recovery_requires_original_missing_placeholder')
    from .capital import CapitalDemo
    broker=(broker_factory or CapitalDemo)(
        key=env.get('CAP_API_KEY',''),user=env.get('CAP_IDENTIFIER',''),
        password=env.get('CAP_API_PASSWORD',''),account_id=APPROVED_ACCOUNT,
        daily_controller=lambda equity,now:dict(stopped=False),armed=False)
    broker.login()
    state=broker.account_state()
    budget=TrialBudget(path,APPROVED_ACCOUNT,clock())
    try:
        result=budget.recover_missing(checkpoint,clock(),state)
        return dict(event='trial_budget_reconciled',**result)
    finally:
        budget.close()


def main():
    parser=argparse.ArgumentParser(description='Review exports offline or apply one reviewed budget checkpoint')
    parser.add_argument('--costs')
    parser.add_argument('--usage')
    parser.add_argument('--observed-at',type=float)
    parser.add_argument('--apply',help='Reviewed base64 JSON checkpoint; explicit one-time recovery')
    args=parser.parse_args()
    if args.apply:
        if len(args.apply)>6000 or args.costs or args.usage or args.observed_at is not None:
            parser.error('Apply requires only the reviewed checkpoint')
        c=json.loads(base64.b64decode(args.apply,validate=True))
        print(json.dumps(apply_reviewed(c,os.environ),allow_nan=False,indent=2))
    else:
        if not args.costs or not args.usage or args.observed_at is None:
            parser.error('Offline review requires costs, usage and observed-at')
        c=review_exports(Path(args.costs).read_bytes(),Path(args.usage).read_bytes(),args.observed_at)
        print(json.dumps(c,allow_nan=False,indent=2))
        print('Reviewed checkpoint: '+base64.b64encode(canonical(c)).decode())


if __name__=='__main__':
    main()
