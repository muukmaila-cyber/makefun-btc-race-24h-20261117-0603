"""Selective dip-buy bot for make.fun btc-race-24h-20261117-0603.

Strategy (from the top-10 FINAL winners of the three earlier BTC races, NOT from
any dev rank — dev rank was shown to be anti-predictive: the race-3 winner came
from dev #2624/2840, our dev #59 entry finished #2523):

- Buy a dip: the close falls >= 1.2% below the 30-bar high AND the bar closes
  up on the previous close (the bounce has started). One position, full cash.
- Exit on the first of: take-profit +0.5%, stop-loss -1.0%, trailing stop -0.8%
  from the peak since entry, or 240 bars held.
- Loss halt: once closed PnL reaches -$12 the bot stops trading for the rest of
  the window. This is what keeps a crash window from bleeding the account
  (an uncapped version of this bot lost -$72 on the 14-15 Nov crash window;
  with the halt the worst loss over seven test windows is -$17.88).
- Late fallback: if no fill has happened by bar 1200, the dip threshold relaxes
  to 0.5% so the bot still makes the >= 1 trade the race bar requires.

Protocol: agent-fun/bars-replay/v1. The bot reads one hello, then one batch per
minute bar, and answers each batch with exactly one reply carrying its orders.
All decisions use only the bars the harness has sent (the close time is the
clock); there is no wall clock, no network, no file outside the repo, no
unseeded random, so two executions on the same data give identical orders.

Amounts are integers throughout: closes are price units (price_scale 5),
cash/equity are quote units (1 USDC = 1,000,000). Thresholds are compared by
integer cross-multiplication, never floats.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "vendor"))
import makefun_bars

SYMBOL = None  # set from hello: the first (only) symbol of the universe

# --- Strategy parameters (basis points where a percent applies) ---
DIP_BPS = 120          # 1.2%: buy when close is this far below the 30-bar high
LOOKBACK = 30          # bars of history the dip is measured against
TP_BPS = 50            # 0.5% take-profit from the entry fill price
SL_BPS = 100           # 1.0% stop-loss from the entry fill price
TRAIL_BPS = 80         # 0.8% trailing stop from the peak since entry
MAX_HOLD = 240         # bars; exit when held this long
LOSS_HALT_UNITS = 12_000_000   # -$12 of closed PnL: stop trading for the window
LATE_FB_BAR = 1200     # if no fill yet by this batch, relax the dip threshold
LATE_DIP_BPS = 50      # 0.5% relaxed dip threshold
MAX_SLIPPAGE_BPS = 200  # 2%: wide enough to never reject on a normal bar

# --- Per-run state (rebuilt from the hello and each batch; nothing persists) ---
closes = []            # closes in price units, oldest first
entry_price = 0        # fill price of the open position, price units
entry_seq = -1         # batch seq in which the open position's fill arrived
peak = 0               # highest close since entry, price units
fills = 0              # buy fills so far (drives the late fallback)
halted = False         # True once the loss halt has tripped
pending_notional = 0   # notional of a buy sent on the previous batch, awaiting its fill


def on_hello(state):
    global SYMBOL
    SYMBOL = state.hello["symbols"][0]["symbol"]


def _fill_price(state, notional):
    """The exact price the harness paid for a buy of `notional` filled at the
    current bar's open, recomputed from the bar the harness sent (its open and
    open depth) with the harness's own formula:

        spend = notional * (10000 - fee_bps) // 10000
        price = open * (10000 + slippage_bps) * (D + spend) // (10000 * D)

    (BarsPool's buy factor is exactly 1 + spend/D for a one-hop pool, and the
    single integer division is the harness's.) Using this instead of deriving
    the price from the position's cost avoids the ~4 bps the cost figure adds
    (cost is the total spent, fees included).
    """
    bar = state.latest[SYMBOL]
    open_price = int(bar["o"])
    depth = int(bar["o_depth"][0])
    fee_bps = int(state.hello["fees"]["fee_bps"])
    slip_bps = int(state.hello["fees"]["slippage_bps"])
    spend = notional * (10_000 - fee_bps) // 10_000
    return open_price * (10_000 + slip_bps) * (depth + spend) // (10_000 * depth)


def _entry_price_from_cost(state):
    """Fallback fill price from the harness's cost figure (cost is the total
    spent, fees included, so this reads ~4 bps above the true fill price).
    Only used if a position appears without a buy this bot sent.
    value = qty * price * 10**quote_scale // 10**(qty_scale + price_scale),
    so price = cost * 10**(qty_scale + price_scale - quote_scale) // qty
    = cost * 10**7 // qty for this universe (8, 5, 6).
    """
    holding = state.positions.get(SYMBOL)
    if holding is None or holding.qty == 0:
        return 0
    # Holding carries (qty, cost): cost is cost_quote_units from the account.
    return holding.cost * 10 ** 7 // holding.qty


def on_batch(state, batch):
    global entry_price, entry_seq, peak, fills, halted, pending_notional
    if state.warming_up:
        return []
    # The last batch's orders are always rejected (after_end): never order on it.
    if state.seq >= 1439:
        return []

    close = state.close(SYMBOL)
    if close is None:
        return []
    closes.append(close)
    if len(closes) > 480:
        del closes[:-480]

    holding = state.positions.get(SYMBOL)
    if holding is not None and holding.qty > 0:
        # We hold a position: maintain the exit rules on this bar's close.
        if entry_seq < 0:
            # The fill arrived in this batch: anchor entry price and peak.
            if pending_notional > 0:
                entry_price = _fill_price(state, pending_notional)
            else:
                entry_price = _entry_price_from_cost(state)
            entry_seq = state.seq
            peak = entry_price
            fills += 1  # a buy fill actually landed
            pending_notional = 0
        if close > peak:
            peak = close
        held = state.seq - entry_seq
        sell = False
        if close * 10_000 >= entry_price * (10_000 + TP_BPS):
            sell = True  # take-profit
        if close * 10_000 <= entry_price * (10_000 - SL_BPS):
            sell = True  # stop-loss
        if close * 10_000 <= peak * (10_000 - TRAIL_BPS):
            sell = True  # trailing stop from the peak since entry
        if held >= MAX_HOLD:
            sell = True  # held too long
        if sell:
            return [{
                "side": "sell",
                "symbol": SYMBOL,
                "qty": holding.qty,
                "max_slippage_bps": MAX_SLIPPAGE_BPS,
            }]
        return []

    # Flat: reset the entry anchor and check the loss halt.
    entry_price = 0
    entry_seq = -1
    peak = 0
    if pending_notional > 0:
        # No position appeared: the buy sent last batch was rejected.
        pending_notional = 0
    if halted:
        return []
    if state.cash <= 1_000_000_000 - LOSS_HALT_UNITS:
        halted = True
        return []

    # Entry: dip from the trailing high, confirmed by an up close.
    if len(closes) < 2:
        return []
    lo = max(0, len(closes) - LOOKBACK)
    high = max(closes[lo:])
    dip_bps = DIP_BPS
    if fills == 0 and state.seq >= LATE_FB_BAR:
        dip_bps = min(dip_bps, LATE_DIP_BPS)
    # (high - close) / high >= dip_bps / 10000, in integers.
    if (high - close) * 10_000 < dip_bps * high:
        return []
    if closes[-1] <= closes[-2]:
        return []  # no bounce yet: the dip may still be falling
    notional = state.cash
    room = state.weight_room(SYMBOL)
    if room < notional:
        notional = room
    if notional < int(state.hello["fees"]["min_notional_quote_units"]):
        return []
    pending_notional = notional  # the fill lands at the next bar's open
    return [{
        "side": "buy",
        "symbol": SYMBOL,
        "notional_quote_units": notional,
        "max_slippage_bps": MAX_SLIPPAGE_BPS,
    }]


makefun_bars.run(on_batch, on_hello=on_hello)
