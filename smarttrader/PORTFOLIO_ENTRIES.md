# Portfolio entries on the approved demo trial

`SMART_PAIR_ENTRIES=0` removes the software count limit and the single-market
restriction. A fresh H1 or D1 trend signal may enter while other journal-owned
positions are open, including positions in different markets. The same market
requires a later closed M15 candle for an additional entry. One account
reconciliation and one order reservation run at a time; an unknown position,
working order or uncertain submission blocks further entries.

Every candidate is sized from independently refreshed executable quotes,
contract factors and currency conversion for every existing position. The
position's current equity-to-stop downside is reserved alongside its original
planned and actual fill loss, taking the larger amount. The portfolio shares
the previous **0.25% of equity** loss allowance, while each new entry uses at
most **0.05%**; combined estimated margin is bounded by **10% of current free
margin**. Existing daily loss controls, broker stop checks and broker minimum
sizes remain in effect. There is no guarantee that a third trade can fit: the
broker's minimum size or the remaining portfolio allowance can prevent it.
New H1/D1 trades have no fixed take-profit order; the watcher closes them on
confirmed reversal of their original timeframe while retaining the broker stop.

The demo trial was explicitly extended on 2026-10-20 for seven days from its
prior deadline. It ends at 2026-10-27 00:00 Asia/Dubai (exclusive). Its
original spend ledger and **$30 total analysis budget** remain; the extension
does not authorize additional spend. AI calls remain disabled by the earlier
rules-only renewal. There is no live-account order path.

These are planning limits based on stops and quotes. A gap, slippage or forced
liquidation can exceed the planned loss, especially with multiple positions.
