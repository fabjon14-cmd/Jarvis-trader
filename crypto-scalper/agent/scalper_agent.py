# crypto-scalper/agent/scalper_agent.py
#
# Decision loop for the crypto scalper — RSI(14)/EMA(20) cross-above buy
# trigger with EMA(20)/EMA(50) trend-alignment and ATR(14) volatility-spike
# filters, +1.5% take-profit / -0.75% stop-loss exit, run on a schedule (not
# a bare `while True: sleep(120)` loop — see crypto-scalper/CLAUDE.md for why).
# Mirrors the equities Trader's audit-trail discipline: every decision this
# run makes gets logged, not just the trades that went through — as both a
# plain-English journal line and a strict JSON decision envelope (portfolio
# cash/NLV, positions & category exposure, technical validation, invalidation
# price) for every pair, every run, per the operator's decision-workflow spec.

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


def _invalidation_price(entry_price):
    if not entry_price:
        return None
    return round(float(entry_price) * (1 - research.STOP_LOSS_PCT / 100), 8)


def run():
    now = datetime.now(timezone.utc)
    log = [f"## Run {now.strftime('%Y-%m-%d %H:%M UTC')}"]
    envelopes = []

    try:
        account = research.get_account()
    except Exception as exc:
        log.append(f"- Could not fetch account ({exc}) — envelopes this run will show a null portfolio snapshot.")
        account = {}

    try:
        positions = research.get_crypto_positions()
    except Exception as exc:
        log.append(f"- Could not fetch positions ({exc}) — treating all pairs as unheld for this run (exit checks below will also reflect this).")
        positions = []
    positions_by_symbol = {p["symbol"]: p for p in positions}

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
        exit_flags_by_symbol = {f["symbol"]: f for f in research.get_exit_flags()}
    except Exception as exc:
        log.append(f"- Exit-flag check FAILED ({exc}) — holding all positions this run rather than guessing.")
        exit_flags_by_symbol = {}

    # Budget-aware evaluation: compute once whether new-buy evaluation is
    # blocked at all (circuit breaker or exhausted daily/weekly headroom),
    # same efficiency principle as the equities bot's budget-aware sweep.
    try:
        deployed = research.get_crypto_deployed_notional()
        daily_headroom = DAILY_NOTIONAL_CAP - deployed["daily_deployed"]
        weekly_headroom = WEEKLY_NOTIONAL_CAP - deployed["weekly_deployed"]
        headroom = max(0, min(daily_headroom, weekly_headroom))
    except Exception as exc:
        log.append(f"- Could not compute deployed notional ({exc}) — new-buy evaluation blocked this run, holding.")
        headroom = 0

    buy_block_reason = None
    if cb.get("halted"):
        buy_block_reason = f"circuit breaker halted: {'; '.join(cb.get('reasons', []))}"
    elif headroom < MIN_REALISTIC_NOTIONAL:
        buy_block_reason = f"daily/weekly headroom ${headroom:,.2f} below ${MIN_REALISTIC_NOTIONAL:.0f} realistic-trade floor"
    if buy_block_reason:
        log.append(f"- New-buy evaluation blocked this run: {buy_block_reason}.")

    buying_power = float(account.get("buying_power", 0)) if account else 0
    equity = float(account.get("equity", 0)) if account else 0
    per_trade_cap = buying_power * PER_TRADE_PCT_CAP

    for pair in _load_watchlist():
        try:
            signal = research.get_signal(pair)
        except Exception as exc:
            signal = {"pair": pair, "error": str(exc)}

        held_position = positions_by_symbol.get(pair)
        invalidation_price = None
        risk_sizing = None  # only populated for NEW_TRADE decisions below

        if held_position:
            avg_entry = held_position.get("avg_entry_price")
            invalidation_price = _invalidation_price(avg_entry)
            flag = exit_flags_by_symbol.get(pair)

            if not flag:
                action = "HOLD"
                plpc = float(held_position.get("unrealized_plpc", 0)) * 100
                reason = f"holding, unrealized {plpc:.2f}%, no exit trigger"
                log.append(f"- {pair}: {reason}.")
            else:
                try:
                    ref_price = research.get_crypto_bars(pair, limit=2)[-1]["c"]
                    if flag["action"] == "close_stop_loss":
                        # Market order, not limit — a resting limit sell can
                        # simply not fill during a sharp drop, letting the
                        # loss run past -0.75% with no backstop. This is the
                        # one exit where guaranteed execution matters more
                        # than price control. See CLAUDE.md "Stop-loss
                        # execution". ref_price is still passed as the
                        # reference for the notional-cap check and dedup.
                        result = trade.place_order(pair, flag["qty"], "sell", ref_price, market=True)
                        price_desc = f"MARKET (ref ${ref_price})"
                    else:
                        limit_price = round(ref_price * (1 - LIMIT_SLIPPAGE_BUFFER), 8)
                        result = trade.place_order(pair, flag["qty"], "sell", limit_price)
                        price_desc = f"limit ${limit_price}"
                    action = "CLOSE"
                    reason = f"{flag['action']} ({flag['reason']}) -> sell {flag['qty']} @ {price_desc}: {json.dumps(result)}"
                    log.append(f"- {pair}: EXIT {reason}")
                except Exception as exc:
                    action = "HOLD"
                    reason = f"exit triggered ({flag['reason']}) but could not fetch a reference price/place the order ({exc}) — HOLDING, not guessing a sell price."
                    log.append(f"- {pair}: {reason}")

        else:
            if signal.get("error"):
                action, reason = "HOLD", f"signal unavailable ({signal['error']})"
            elif buy_block_reason:
                action, reason = "HOLD", buy_block_reason
            elif not signal["buy_signal"]:
                action, reason = "HOLD", (
                    f"rsi={signal['rsi']} (oversold={signal['rsi_oversold']}), "
                    f"crossed_above_ema20={signal['crossed_above_ema']}, "
                    f"ema_alignment_bullish={signal['ema_alignment_bullish']}, "
                    f"atr_volatility_spike={signal['atr_volatility_spike']}"
                )
            else:
                notional_cap = min(MAX_ORDER_NOTIONAL, per_trade_cap, headroom)
                if notional_cap < MIN_REALISTIC_NOTIONAL:
                    action, reason = "HOLD", f"buy signal true but sizeable notional ${notional_cap:,.2f} below the ${MIN_REALISTIC_NOTIONAL:.0f} realistic-trade floor"
                else:
                    limit_price = round(signal["curr_close"] * (1 + LIMIT_SLIPPAGE_BUFFER), 8)
                    qty_from_caps = notional_cap / limit_price
                    # Risk-based sizing is informational/a ceiling, not the operative
                    # cap — min() with the existing notional caps below. With the
                    # current 0.75% stop, a 1% risk target implies a much larger
                    # position than the notional caps allow, so in practice the caps
                    # bind; both target and actual risk % are logged so that's
                    # visible rather than silently overridden. See CLAUDE.md.
                    risk_based_qty = research.compute_risk_based_qty(equity, limit_price, research.STOP_LOSS_PCT, research.TARGET_RISK_PCT)
                    qty = round(min(qty_from_caps, risk_based_qty) if risk_based_qty > 0 else qty_from_caps, 6)
                    notional = qty * limit_price
                    actual_risk_dollar = round(qty * limit_price * (research.STOP_LOSS_PCT / 100), 2)
                    risk_sizing = {
                        "target_risk_pct": research.TARGET_RISK_PCT,
                        "target_risk_dollar": round(research.TARGET_RISK_PCT / 100 * equity, 2) if equity else None,
                        "risk_based_qty": round(risk_based_qty, 6) if risk_based_qty else None,
                        "notional_cap_qty": round(qty_from_caps, 6),
                        "final_qty": qty,
                        "binding_constraint": "risk_target" if (risk_based_qty and risk_based_qty < qty_from_caps) else "notional_cap",
                        "actual_risk_dollar": actual_risk_dollar,
                        "actual_risk_pct": round(actual_risk_dollar / equity * 100, 4) if equity else None,
                    }
                    result = trade.place_order(pair, qty, "buy", limit_price)
                    action = "NEW_TRADE"
                    invalidation_price = _invalidation_price(limit_price)
                    reason = f"rsi={signal['rsi']}<30, crossed above EMA20, EMA20>EMA50, no ATR spike — order {qty} @ limit ${limit_price} (${notional:,.2f}, actual risk {risk_sizing['actual_risk_pct']}% vs {research.TARGET_RISK_PCT}% target): {json.dumps(result)}"
                    if result.get("placed", True) and not result.get("duplicate"):
                        headroom -= notional
            log.append(f"- {pair}: {'BUY' if action == 'NEW_TRADE' else 'hold'} — {reason}")

        envelopes.append(research.build_decision_envelope(pair, action, signal, account, positions, invalidation_price, reason, risk_sizing))

    log.append("```json\n" + json.dumps(envelopes, indent=2) + "\n```")

    path = _append_journal(log)
    print("\n".join(log))
    return path


if __name__ == "__main__":
    run()
