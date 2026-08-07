# daytrader/agent/daytrader_agent.py
#
# Main run loop for the intraday equities day-trading agent. Scheduled every
# 5 minutes during market hours (see .github/workflows/daytrader.yml).
# Checks exits first, force-flattens near market close, then evaluates new
# entries — same order of operations as the crypto scalper, so a mandatory
# exit is never skipped in favor of evaluating a new buy.

import os
import sys
import json
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import research
import trade

WATCHLIST_PATH = os.path.join(os.path.dirname(__file__), "..", "watchlist.json")
JOURNAL_DIR = os.path.join(os.path.dirname(__file__), "..", "journal")

LIMIT_SLIPPAGE_BUFFER = 0.001  # 0.1%, same convention as the other two bots

# Force-flatten every open position once the market clock reports fewer than
# this many minutes to close — a "day trading" bot that can carry a position
# overnight isn't day trading, it's accidentally swing trading with gap risk
# nobody signed up for. Not in the original spec; added deliberately (see
# CLAUDE.md "Forced end-of-day flatten").
EOD_FLATTEN_MINUTES = int(os.getenv("DAYTRADER_EOD_FLATTEN_MINUTES", "10"))


def _load_watchlist():
    with open(WATCHLIST_PATH) as f:
        return json.load(f)["symbols"]


def _minutes_to_close(clock):
    if not clock.get("is_open"):
        return None
    now = datetime.strptime(clock["timestamp"][:19], "%Y-%m-%dT%H:%M:%S")
    close = datetime.strptime(clock["next_close"][:19], "%Y-%m-%dT%H:%M:%S")
    return (close - now).total_seconds() / 60


def _journal_path():
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    os.makedirs(JOURNAL_DIR, exist_ok=True)
    return os.path.join(JOURNAL_DIR, f"{day}.md")


def _append_journal(lines, envelopes):
    path = _journal_path()
    is_new = not os.path.exists(path)
    with open(path, "a") as f:
        if is_new:
            f.write(f"# {datetime.now(timezone.utc).strftime('%Y-%m-%d')}\n\n")
        f.write(f"## Run {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')} UTC\n\n")
        for line in lines:
            f.write(f"- {line}\n")
        f.write("\n```json\n")
        f.write(json.dumps(envelopes, indent=2))
        f.write("\n```\n\n")


def run():
    lines = []
    envelopes = []

    try:
        clock = research.get_market_clock()
    except Exception as e:
        _append_journal([f"Market clock check failed ({e}) — holding entire run."], [])
        return

    if not clock.get("is_open"):
        lines.append(f"Market closed (next_open: {clock.get('next_open')}) — no action taken.")
        _append_journal(lines, [])
        return

    minutes_left = _minutes_to_close(clock)
    force_flatten = minutes_left is not None and minutes_left <= EOD_FLATTEN_MINUTES

    try:
        positions = {p["symbol"]: p for p in research.get_positions()}
    except Exception as e:
        _append_journal([f"Could not fetch positions ({e}) — holding entire run."], [])
        return

    # --- Exits first: forced flatten, then stop-loss / take-profit / opposite crossover ---
    for symbol, pos in positions.items():
        entry_price = float(pos["avg_entry_price"])
        qty = pos["qty"]
        last_price = float(pos.get("current_price") or entry_price)
        unrealized_plpc = float(pos.get("unrealized_plpc", 0)) * 100

        exit_reason = None
        if force_flatten:
            exit_reason = "eod_flatten"
        elif unrealized_plpc <= -research.STOP_LOSS_PCT:
            exit_reason = "stop_loss"
        elif unrealized_plpc >= research.TAKE_PROFIT_PCT:
            exit_reason = "take_profit"
        else:
            try:
                if research.get_exit_crossunder(symbol):
                    exit_reason = "opposite_crossover"
            except Exception as e:
                lines.append(f"{symbol}: exit-crossover check failed ({e}) — holding position, will retry next run.")
                continue

        if exit_reason:
            use_market = exit_reason in ("stop_loss", "eod_flatten")
            limit_price = None if use_market else round(last_price * (1 - LIMIT_SLIPPAGE_BUFFER), 2)
            result = trade.place_order(symbol, qty, "sell", limit_price=limit_price, market=use_market)
            lines.append(f"{symbol}: CLOSE ({exit_reason}, unrealized_plpc={unrealized_plpc:.2f}%) -> {result}")
        else:
            lines.append(f"{symbol}: hold open position (unrealized_plpc={unrealized_plpc:.2f}%)")

    if force_flatten:
        lines.append(f"Within {EOD_FLATTEN_MINUTES} min of close — new-entry evaluation skipped this run.")
        _append_journal(lines, envelopes)
        return

    # --- Circuit breaker check, once, before evaluating any new entries ---
    cb = research.get_circuit_breaker_status()
    buy_block_reason = None
    if cb.get("halted"):
        buy_block_reason = f"circuit breaker halted: {cb.get('reason')}"
        lines.append(f"New-entry evaluation blocked this run: {buy_block_reason}.")

    # --- New entries ---
    watchlist = _load_watchlist()
    try:
        account = research.get_account()
        equity = float(account["equity"])
        buying_power = float(account["buying_power"])
    except Exception as e:
        lines.append(f"Could not fetch account ({e}) — holding all new-entry evaluation this run.")
        _append_journal(lines, envelopes)
        return

    open_count = len(positions)

    for symbol in watchlist:
        if symbol in positions:
            lines.append(f"{symbol}: hold — already have an open position (no averaging in).")
            continue

        try:
            signal = research.get_signal(symbol)
        except Exception as e:
            lines.append(f"{symbol}: signal check failed ({e}) — hold (API/data failure -> hold, never inferred).")
            continue

        envelope = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "symbol": symbol,
            "technical_validation": signal,
            "action": "HOLD",
        }

        if signal.get("error"):
            lines.append(f"{symbol}: hold — {signal['error']}.")
            envelopes.append(envelope)
            continue

        if not signal["buy_signal"]:
            reason = []
            if not signal["crossed_above"]:
                reason.append("no fresh EMA9>EMA21 cross")
            if not signal["rsi_in_band"]:
                reason.append(f"rsi14={signal['rsi14']} outside [{research.RSI_ENTRY_MIN},{research.RSI_ENTRY_MAX}]")
            lines.append(f"{symbol}: hold — {', '.join(reason)}.")
            envelopes.append(envelope)
            continue

        if buy_block_reason:
            envelope["action"] = "BLOCKED"
            envelope["reason"] = buy_block_reason
            envelopes.append(envelope)
            continue

        if open_count >= trade.MAX_POSITIONS:
            lines.append(f"{symbol}: signal qualified but max positions ({trade.MAX_POSITIONS}) already open — hold.")
            envelope["action"] = "BLOCKED"
            envelope["reason"] = "max positions"
            envelopes.append(envelope)
            continue

        entry_price = signal["last_price"]
        # PER_TRADE_PCT_CAP (default 1%) of total account balance — the
        # operator's spec, taken literally as a position-size cap. Backed
        # by MAX_ORDER_NOTIONAL as a secondary dollar ceiling (matters more
        # on a large account, where 1% could still be a large dollar
        # amount) — whichever is smaller actually binds, logged below.
        pct_cap_qty = research.compute_position_qty(entry_price, equity)
        notional_cap_qty = trade.MAX_ORDER_NOTIONAL / entry_price
        qty = round(min(pct_cap_qty, notional_cap_qty), 4)
        binding = "per_trade_pct_cap" if pct_cap_qty <= notional_cap_qty else "max_order_notional"

        if qty <= 0:
            lines.append(f"{symbol}: signal qualified but computed qty was 0 — hold.")
            envelopes.append(envelope)
            continue

        limit_price = round(entry_price * (1 + LIMIT_SLIPPAGE_BUFFER), 2)
        result = trade.place_order(symbol, qty, "buy", limit_price=limit_price)
        envelope["action"] = "NEW_TRADE"
        envelope["position_sizing"] = {
            "per_trade_pct_cap": research.PER_TRADE_PCT_CAP,
            "pct_cap_qty": round(pct_cap_qty, 4),
            "notional_cap_qty": round(notional_cap_qty, 4),
            "binding_constraint": binding,
            "qty": qty,
        }
        envelope["order_result"] = result
        envelopes.append(envelope)
        lines.append(f"{symbol}: NEW_TRADE qty={qty} @ ~{limit_price} (rsi14={signal['rsi14']}, binding={binding}) -> {result}")

        if result.get("placed"):
            open_count += 1

    _append_journal(lines, envelopes)


if __name__ == "__main__":
    run()
