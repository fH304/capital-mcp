"""Validate broker UTC candles without inventing missing bars or volume."""
import math
from datetime import datetime, timezone

RESOLUTIONS = {'MINUTE_15':900, 'HOUR':3600, 'HOUR_4':14400}


class MarketDataError(ValueError):
    pass


def numeric(value, positive=True):
    if type(value) not in (float,int) or not math.isfinite(value) or (value<=0 if positive else value<0):
        raise MarketDataError('Invalid candle value')
    return value


def utc_stamp(value):
    try:
        if not isinstance(value,str):
            raise ValueError()
        dt=datetime.fromisoformat(value.replace('Z','+00:00'))
        # Capital explicitly names this field snapshotTimeUTC, including when
        # its ISO representation omits a suffix. Never use snapshotTime.
        if dt.tzinfo is None:
            dt=dt.replace(tzinfo=timezone.utc)
        if dt.utcoffset().total_seconds()!=0:
            raise ValueError()
        return dt.timestamp()
    except (ValueError,TypeError,OverflowError):
        raise MarketDataError('Invalid broker UTC candle timestamp') from None


def normalize_candles(rows, resolution, now, count=32):
    if resolution not in RESOLUTIONS or not isinstance(rows,list) or type(count) is not int or not 20<=count<=40:
        raise MarketDataError('Invalid candle collection request')
    duration=RESOLUTIONS[resolution]
    closed,seen=[],set()
    for row in rows:
        try:
            stamp=utc_stamp(row['snapshotTimeUTC'])
            if stamp in seen or stamp>now:
                raise MarketDataError('Duplicate or future candle')
            seen.add(stamp)
            if stamp+duration>now:
                continue  # current bar may still change; never send it to AI
            prices={}
            for source,key in (('openPrice','o'),('highPrice','h'),('lowPrice','l'),('closePrice','c')):
                bid=numeric(row[source]['bid'])
                ask=numeric(row[source]['ask'])
                if ask<bid:
                    raise MarketDataError('Inverted candle spread')
                prices[key]=(bid+ask)/2
            for side in ('bid','ask'):
                if not (row['lowPrice'][side]<=row['openPrice'][side]<=row['highPrice'][side]
                        and row['lowPrice'][side]<=row['closePrice'][side]<=row['highPrice'][side]):
                    raise MarketDataError('Invalid candle OHLC range')
            candle=dict(t=stamp,**prices)
            volume=row.get('lastTradedVolume')
            if volume is not None:
                candle['broker_volume']=numeric(volume,positive=False)
            closed.append(candle)
        except (KeyError,TypeError):
            raise MarketDataError('Incomplete broker candle') from None
    closed=sorted(closed,key=lambda x:x['t'])[-count:]
    frame={'resolution':resolution,'seconds':duration,'candles':closed}
    validate_frame(frame,now,count)
    return frame


def validate_frame(frame, now, minimum=20):
    try:
        resolution=frame['resolution']
        duration=RESOLUTIONS[resolution]
        if frame['seconds']!=duration:
            raise MarketDataError('Candle duration mismatch')
        candles=frame['candles']
        if not isinstance(candles,list) or not minimum<=len(candles)<=40:
            raise MarketDataError('Insufficient closed candle history')
        times=[]
        for candle in candles:
            t=numeric(candle['t'])
            if t+duration>now:
                raise MarketDataError('Unclosed candle in analysis context')
            o,h,l,c=(numeric(candle[k]) for k in ('o','h','l','c'))
            if not l<=o<=h or not l<=c<=h:
                raise MarketDataError('Invalid normalized OHLC')
            if 'broker_volume' in candle:
                numeric(candle['broker_volume'],positive=False)
            times.append(t)
        if times!=sorted(set(times)):
            raise MarketDataError('Candle order or uniqueness failed')
        if not 0<=now-(times[-1]+duration)<=duration+120:
            raise MarketDataError('Stale closed candle history')
        # Older weekend/session gaps are visible in timestamps, never filled.
        # The five latest bars must be continuous before this stage can enter.
        if any(b-a!=duration for a,b in zip(times[-5:-1],times[-4:])):
            raise MarketDataError('Gap in recent candle history')
    except (KeyError,TypeError):
        raise MarketDataError('Invalid timeframe context') from None


def validate_frames(frames,now):
    if not isinstance(frames,dict) or set(frames)!=set(RESOLUTIONS):
        raise MarketDataError('All three configured timeframes are required')
    for resolution,frame in frames.items():
        if not isinstance(frame,dict) or frame.get('resolution')!=resolution:
            raise MarketDataError('Timeframe label mismatch')
        validate_frame(frame,now)
