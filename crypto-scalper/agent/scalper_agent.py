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


def _invalidation_price(entry_price, stop_pct):
    if not entry_price or stop_pct is None:
        return None
    return round(float(entry_price) * (1 - stop_pct / 100), 8)


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

    # Pass 1: handle exits (independent per pair, no shared budget) and sort
    # not-held pairs into immediate holds vs. buy candidates. Candidates are
    # NOT sized or placed yet — that happens in the even-split pass below, so
    # a pair earlier in watchlist order (BTC/USD is always first) can't eat
    # the whole remaining budget before later pairs are even evaluated.
    results = []  # one dict per pair, in watchlist order
    candidates = []  # indices into `results` that still need sizing/placement

    for pair in _load_watchlist():
        try:
            signal = research.get_signal(pair)
        except Exception as exc:
            signal = {"pair": pair, "error": str(exc)}

        held_position = positions_by_symbol.get(pair)
        row = {"pair": pair, "signal": signal, "invalidation_price": None, "risk_sizing": None}

        if held_position:
            avg_entry = held_position.get("avg_entry_price")
            try:
                held_stop_pct, held_tp_pct = research.get_position_trade_params(pair)
            except Exception:
                held_stop_pct, held_tp_pct = None, None
            row["invalidation_price"] = _invalidation_price(avg_entry, held_stop_pct)
            flag = exit_flags_by_symbol.get(pair)

            if not flag:
                plpc = float(held_position.get("unrealized_plpc", 0)) * 100
                row["action"] = "HOLD"
                row["reason"] = f"holding, unrealized {plpc:.2f}% (stop {-held_stop_pct if held_stop_pct is not None else '?'}%/target +{held_tp_pct}%), no exit trigger"
                row["log_line"] = f"- {pair}: {row['reason']}."
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
                    row["action"] = "CLOSE"
                    row["reason"] = f"{flag['action']} ({flag['reason']}) -> sell {flag['qty']} @ {price_desc}: {json.dumps(result)}"
                    row["log_line"] = f"- {pair}: EXIT {row['reason']}"
                except Exception as exc:
                    row["action"] = "HOLD"
                    row["reason"] = f"exit triggered ({flag['reason']}) but could not fetch a reference price/place the order ({exc}) — HOLDING, not guessing a sell price."
                    row["log_line"] = f"- {pair}: {row['reason']}"

            results.append(row)
            continue

        if signal.get("error"):
            row["action"], row["reason"] = "HOLD", f"signal unavailable ({signal['error']})"
        elif buy_block_reason:
            row["action"], row["reason"] = "HOLD", buy_block_reason
        elif not signal["buy_signal"]:
            row["action"], row["reason"] = "HOLD", (
                f"rsi={signal['rsi']} (recently_oversold={signal['rsi_recently_oversold']}, "
                f"min_in_lookback={signal['rsi_min_in_lookback']}), "
                f"crossed_above_ema20={signal['crossed_above_ema']}, "
                f"ema_alignment_bullish={signal['ema_alignment_bullish']}, "
                f"atr_volatility_spike={signal['atr_volatility_spike']}"
            )
        else:
            results.append(row)  # action/reason/log_line filled in below
            candidates.append(len(results) - 1)
            continue

        row["log_line"] = f"- {pair}: hold — {row['reason']}"
        results.append(row)

    # Pass 2: RSI-depth-weighted allocation across every pair that qualified
    # THIS run (added 2026-08-05, at the operator's request that stronger-
    # looking setups get more capital than weaker ones, not an even split).
    # Weight = (30 - rsi_min_in_lookback), i.e. how far below the oversold
    # threshold RSI got during the dip that qualified this pair — a deeper
    # oversold reading is the conventional read on a "stronger" mean-
    # reversion setup. This is a signal-STRENGTH proxy, not a profit
    # prediction — nothing here or anywhere can actually know which trade
    # will make more money; this only changes which qualifying setup looks
    # more textbook by the numbers already computed.
    #
    # Uses rsi_min_in_lookback, NOT the current-bar rsi (changed 2026-08-05
    # alongside the RSI lookback-window fix): buy_signal now only requires
    # RSI to have touched <30 within the lookback window, not on the
    # current bar — and a diagnostic found RSI is reliably back in the
    # 50-65 range by the time the EMA20 cross actually fires. Weighting by
    # current rsi would make (30 - rsi) negative or near-zero for nearly
    # every real qualifying candidate, collapsing the weighting into a flat
    # floor value regardless of actual dip depth — silently degenerating
    # into an even split without erroring. rsi_min_in_lookback is always
    # < 30 for a qualifying candidate (it's the gating condition), so this
    # stays a meaningful, always-positive signal-strength measure.
    #
    # Each candidate still respects its own normal per-trade cap
    # (MAX_ORDER_NOTIONAL / PER_TRADE_PCT_CAP) — weighting only changes how
    # the shared daily/weekly headroom divides when more than one pair
    # qualifies at once. A single qualifying pair still gets the full normal
    # per-trade cap, same as before. Unused headroom from a pair whose
    # weighted share exceeds its own per-trade cap is NOT redistributed to
    # the other candidates — a known simplification, same as the earlier
    # even-split version had for the realistic-notional floor.
    weights = {}
    if candidates:
        weights = {i: max(30 - results[i]["signal"]["rsi_min_in_lookback"], 0.01) for i in candidates}
        total_weight = sum(weights.values())
        qualifying_desc = ", ".join(
            f"{results[i]['pair']} (rsi_min={results[i]['signal']['rsi_min_in_lookback']}, weight {weights[i] / total_weight * 100:.0f}%)"
            for i in candidates
        )
        log.append(
            f"- {len(candidates)} pair(s) qualified for a buy this run — weighting ${headroom:,.2f} "
            f"headroom by RSI dip depth (deeper oversold = more capital), not evenly: {qualifying_desc}."
        )

    for idx in candidates:
        row = results[idx]
        pair, signal = row["pair"], row["signal"]
        weighted_headroom = headroom * (weights[idx] / total_weight)
        notional_cap = min(MAX_ORDER_NOTIONAL, per_trade_cap, weighted_headroom)

        if notional_cap < MIN_REALISTIC_NOTIONAL:
            row["action"] = "HOLD"
            row["reason"] = (
                f"buy signal true but this pair's RSI-weighted share (${notional_cap:,.2f} of "
                f"${headroom:,.2f}, weight {weights[idx] / total_weight * 100:.0f}% across "
                f"{len(candidates)} qualifying pairs) is below the ${MIN_REALISTIC_NOTIONAL:.0f} realistic-trade floor"
            )
            row["log_line"] = f"- {pair}: hold — {row['reason']}"
            continue

        limit_price = round(signal["curr_close"] * (1 + LIMIT_SLIPPAGE_BUFFER), 8)

        # ATR-based stop/TP for THIS trade, not the flat global default —
        # changed 2026-08-06 after an out-of-sample backtest showed the
        # flat 0.75%/1.5% design blowing up on whichever pair happened to
        # be most volatile in a given window (AVAX in one, DOT in another).
        # See research.compute_atr_based_stop_tp_pct's docstring. Falls
        # back to the flat defaults if ATR is unavailable for some reason.
        stop_pct, tp_pct = research.compute_atr_based_stop_tp_pct(limit_price, signal.get("atr14"))
        if stop_pct is None or tp_pct is None:
            stop_pct, tp_pct = research.STOP_LOSS_PCT, research.PROFIT_TARGET_PCT
        client_order_id = research.encode_trade_params(stop_pct, tp_pct)

        qty_from_caps = notional_cap / limit_price
        # Risk-based sizing is informational/a ceiling, not the operative
        # cap — min() with the existing notional caps below. Sized against
        # THIS trade's own ATR-based stop, not the flat global one, so the
        # risk-sizing math matches whatever stop actually gets set. With a
        # tight stop this can still imply a much larger position than the
        # notional caps allow, so in practice the caps usually bind; both
        # target and actual risk % are logged so that's visible rather
        # than silently overridden. See CLAUDE.md.
        risk_based_qty = research.compute_risk_based_qty(equity, limit_price, stop_pct, research.TARGET_RISK_PCT)
        qty = round(min(qty_from_caps, risk_based_qty) if risk_based_qty > 0 else qty_from_caps, 6)
        notional = qty * limit_price
        actual_risk_dollar = round(qty * limit_price * (stop_pct / 100), 2)
        risk_sizing = {
            "stop_pct": stop_pct,
            "tp_pct": tp_pct,
            "target_risk_pct": research.TARGET_RISK_PCT,
            "target_risk_dollar": round(research.TARGET_RISK_PCT / 100 * equity, 2) if equity else None,
            "risk_based_qty": round(risk_based_qty, 6) if risk_based_qty else None,
            "notional_cap_qty": round(qty_from_caps, 6),
            "final_qty": qty,
            "binding_constraint": "risk_target" if (risk_based_qty and risk_based_qty < qty_from_caps) else "notional_cap",
            "actual_risk_dollar": actual_risk_dollar,
            "actual_risk_pct": round(actual_risk_dollar / equity * 100, 4) if equity else None,
        }
        result = trade.place_order(pair, qty, "buy", limit_price, client_order_id=client_order_id)
        row["action"] = "NEW_TRADE"
        row["invalidation_price"] = _invalidation_price(limit_price, stop_pct)
        row["risk_sizing"] = risk_sizing
        row["reason"] = (
            f"rsi={signal['rsi']} (touched {signal['rsi_min_in_lookback']} within last {research.RSI_LOOKBACK_BARS} bars), "
            f"crossed above EMA20, EMA20>EMA50 (1h), no ATR spike — "
            f"order {qty} @ limit ${limit_price} (${notional:,.2f}, ATR-based stop -{stop_pct}%/target +{tp_pct}%, "
            f"RSI-weighted share {weights[idx] / total_weight * 100:.0f}% "
            f"across {len(candidates)} qualifying pair(s), actual risk {risk_sizing['actual_risk_pct']}% vs {research.TARGET_RISK_PCT}% target): {json.dumps(result)}"
        )
        row["log_line"] = f"- {pair}: BUY — {row['reason']}"

    for row in results:
        log.append(row["log_line"])
        envelopes.append(research.build_decision_envelope(
            row["pair"], row["action"], row["signal"], account, positions,
            row["invalidation_price"], row["reason"], row["risk_sizing"],
        ))

    log.append("```json\n" + json.dumps(envelopes, indent=2) + "\n```")

    path = _append_journal(log)
    print("\n".join(log))
    return path


if __name__ == "__main__":
    run()
