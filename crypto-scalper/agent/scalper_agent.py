# crypto-scalper/agent/scalper_agent.py
#
# Decision loop for the crypto scalper — RSI(14)/EMA(20) cross-above buy
# trigger, +1.5% take-profit / -0.75% stop-loss exit, run on a schedule (not
# a bare `while True: sleep(120)` loop — see crypto-scalper/CLAUDE.md for why).
# Mirrors the equities Trader's audit-trail discipline: every decision this
# run makes gets logged, not just the trades that went through.

import os
import sys
import json
import importlib.util
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(__file__))

_scripts_dir = os.path.join(os.path.dirname(__file__), "..", "scripts")


def _load(name, filename):
    spec = importlib.util.spec_from_file_location(name, os.path.join(_scripts_dir, filename))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


research = _load("crypto_scalper_research_top", "research.py")
trade = _load("crypto_scalper_trade_top", "trade.py")

WATCHLIST_PATH = os.path.join(os.path.dirname(__file__), "..", "watchlist.json")
JOURNAL_DIR = os.path.join(os.path.dirname(__file__), "..", "journal")

DAILY_NOTIONAL_CAP = trade.DAILY_NOTIONAL_CAP
WEEKLY_NOTIONAL_CAP = trade.WEEKLY_NOTIONAL_CAP
MAX_ORDER_NOTIONAL = trade.MAX_ORDER_NOTIONAL
PER_TRADE_PCT_CAP = trade.PER_TRADE_PCT_CAP
MIN_REALISTIC_NOTIONAL = float(os.getenv("CRYPTO_MIN_REALISTIC_NOTIONAL", "10"))
LIMIT_SLIPPAGE_BUFFER = float(os.getenv("CRYPTO_LIMIT_SLIPPAGE_BUFFER", "0.001"))  # 0.1%


def _load_watchlist():
    with open(WATCHLIST_PATH) as f:
        return json.load(f)["pairs"]


def _journal_path():
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return os.path.join(JOURNAL_DIR, f"{today}.md")


def _append_journal(lines):
    os.makedirs(JOURNAL_DIR, exist_ok=True)
    path = _journal_path()
    header_needed = not os.path.exists(path)
    with open(path, "a") as f:
        if header_needed:
            f.write(f"# {datetime.now(timezone.utc).strftime('%Y-%m-%d')} — Crypto Scalper\n\n")
        f.write("\n".join(lines) + "\n\n")
    return path


def run():
    now = datetime.now(timezone.utc)
    log = [f"## Run {now.strftime('%Y-%m-%d %H:%M UTC')}"]

    try:
        cb = research.get_circuit_breaker_status()
    except Exception as exc:
        log.append(f"- Circuit breaker check FAILED ({exc}) — holding all new buys this run, exits still evaluated.")
        cb = {"halted": True, "reasons": ["circuit-breaker check failed"]}
    else:
        log.append(f"- Circuit breaker: halted={cb['halted']}" + (f" ({'; '.join(cb['reasons'])})" if cb["halted"] else ""))

    # Exits always run in full, regardless of budget or circuit-breaker state
    # — same principle as the equities stop-loss: a mandatory exit is never
    # blocked by a buy-side gate.
    try:
        exit_flags = research.get_exit_flags()
    except Exception as exc:
        log.append(f"- Exit-flag check FAILED ({exc}) — holding all positions this run rather than guessing.")
        exit_flags = []

    for flag in exit_flags:
        symbol = flag["symbol"]
        try:
            bars = research.get_crypto_bars(symbol, limit=2)
            ref_price = bars[-1]["c"]
        except Exception as exc:
            log.append(f"- {symbol}: exit triggered ({flag['reason']}) but could not fetch a reference price ({exc}) — HOLDING, not guessing a sell price.")
            continue
        limit_price = round(ref_price * (1 - LIMIT_SLIPPAGE_BUFFER), 8)
        result = trade.place_order(symbol, flag["qty"], "sell", limit_price)
        log.append(f"- {symbol}: EXIT {flag['action']} ({flag['reason']}) -> sell {flag['qty']} @ limit ${limit_price}: {json.dumps(result)}")

    # Budget-aware evaluation: skip the full new-buy sweep if there's no
    # realistic headroom left, same efficiency principle as the equities bot.
    try:
        deployed = research.get_crypto_deployed_notional()
        daily_headroom = DAILY_NOTIONAL_CAP - deployed["daily_deployed"]
        weekly_headroom = WEEKLY_NOTIONAL_CAP - deployed["weekly_deployed"]
        headroom = max(0, min(daily_headroom, weekly_headroom))
    except Exception as exc:
        log.append(f"- Could not compute deployed notional ({exc}) — skipping new-buy evaluation this run, holding.")
        headroom = 0

    if cb.get("halted"):
        log.append("- New-buy evaluation skipped: circuit breaker halted.")
    elif headroom < MIN_REALISTIC_NOTIONAL:
        log.append(f"- Daily/weekly headroom ${headroom:,.2f} remaining — below the ${MIN_REALISTIC_NOTIONAL:.0f} realistic-trade floor, skipping new-buy evaluation this run.")
    else:
        try:
            account = research.get_account()
            buying_power = float(account.get("buying_power", 0))
        except Exception as exc:
            log.append(f"- Could not fetch buying power ({exc}) — skipping new-buy evaluation this run, holding.")
            buying_power = 0

        if buying_power > 0:
            per_trade_cap = buying_power * PER_TRADE_PCT_CAP
            pairs = _load_watchlist()
            held = {p["symbol"] for p in research.get_crypto_positions()}

            for pair in pairs:
                if pair in held:
                    log.append(f"- {pair}: already holding a position — no add/re-entry, skipped new-buy evaluation (exit handled above if triggered).")
                    continue

                try:
                    signal = research.get_signal(pair)
                except Exception as exc:
                    log.append(f"- {pair}: signal check FAILED ({exc}) — holding, not inferring from anything else.")
                    continue
                if signal.get("error"):
                    log.append(f"- {pair}: signal unavailable ({signal['error']}) — holding.")
                    continue
                if not signal["buy_signal"]:
                    log.append(
                        f"- {pair}: hold — rsi={signal['rsi']} (oversold={signal['rsi_oversold']}), "
                        f"crossed_above_ema20={signal['crossed_above_ema']} (close {signal['curr_close']} vs ema {signal['ema20']})."
                    )
                    continue

                notional = min(MAX_ORDER_NOTIONAL, per_trade_cap, headroom)
                if notional < MIN_REALISTIC_NOTIONAL:
                    log.append(f"- {pair}: buy signal true (rsi={signal['rsi']}, crossed above EMA20) but sizeable notional ${notional:,.2f} is below the realistic-trade floor — holding.")
                    continue

                limit_price = round(signal["curr_close"] * (1 + LIMIT_SLIPPAGE_BUFFER), 8)
                qty = round(notional / limit_price, 6)
                result = trade.place_order(pair, qty, "buy", limit_price)
                log.append(
                    f"- {pair}: BUY signal — rsi={signal['rsi']} < 30, crossed above EMA20 "
                    f"(close {signal['curr_close']} vs ema {signal['ema20']}). "
                    f"Order: {qty} @ limit ${limit_price} (${notional:,.2f} notional): {json.dumps(result)}"
                )
                if result.get("placed", True) and not result.get("duplicate"):
                    headroom -= notional

    path = _append_journal(log)
    print("\n".join(log))
    return path


if __name__ == "__main__":
    run()
