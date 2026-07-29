# agent/trader_agent.py
#
# Claude-powered agent ("Trader") for a paper-trading Alpaca account.
# Wraps scripts/research.py and scripts/trade.py as tools Claude can call.
# Order placement/cancellation is gated in scripts/trade.py: interactively it
# pauses for a human y/N confirmation; under TRADER_UNATTENDED=1 (scheduled
# runs) it auto-approves but requires limit orders and caps size/count.

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

from anthropic import Anthropic
from research import get_account, get_positions, get_bars, get_news, get_orders, get_portfolio_history, get_earnings_date, get_movers
from trade import place_order, cancel_all_orders, get_market_status

client = Anthropic()  # reads ANTHROPIC_API_KEY from the environment

MODEL = "claude-sonnet-5"

TOOLS = [
    {
        "name": "get_account",
        "description": "Get current paper-trading account status: equity, cash, buying power, and day P&L.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_positions",
        "description": "Get all open positions in the paper-trading account.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_bars",
        "description": "Get historical price bars for a stock symbol.",
        "input_schema": {
            "type": "object",
            "properties": {
                "symbol": {"type": "string", "description": "Ticker symbol, e.g. NVDA"},
                "timeframe": {"type": "string", "description": "Bar size, e.g. 1Day, 1Hour", "default": "1Day"},
                "limit": {"type": "integer", "description": "Number of bars to fetch", "default": 60},
            },
            "required": ["symbol"],
        },
    },
    {
        "name": "get_news",
        "description": "Get recent news headlines for a stock symbol.",
        "input_schema": {
            "type": "object",
            "properties": {
                "symbol": {"type": "string", "description": "Ticker symbol, e.g. NVDA"},
            },
            "required": ["symbol"],
        },
    },
    {
        "name": "get_market_status",
        "description": "Check whether the market is currently open, and the next open/close times.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_orders",
        "description": "Get historical orders (filled, canceled, expired, etc.), most recent first — use for reviewing past trade outcomes.",
        "input_schema": {
            "type": "object",
            "properties": {
                "status": {"type": "string", "description": "Order status filter, e.g. 'all', 'closed', 'open'", "default": "all"},
                "limit": {"type": "integer", "description": "Max orders to return", "default": 100},
            },
        },
    },
    {
        "name": "get_portfolio_history",
        "description": "Get account equity history over a period — use for computing P&L/drawdown over a stretch of time.",
        "input_schema": {
            "type": "object",
            "properties": {
                "period": {"type": "string", "description": "e.g. '1W', '1M'", "default": "1W"},
                "timeframe": {"type": "string", "description": "e.g. '1D', '1H'", "default": "1D"},
            },
        },
    },
    {
        "name": "get_movers",
        "description": "Get today's top gaining/losing stocks market-wide (not limited to the watchlist) — use for finding watchlist candidates, not for trading them directly.",
        "input_schema": {
            "type": "object",
            "properties": {
                "top": {"type": "integer", "description": "How many gainers and losers to return each", "default": 10},
            },
        },
    },
    {
        "name": "get_earnings_date",
        "description": (
            "Get a symbol's next upcoming earnings date and whether it falls within 48 hours. "
            "Always check this before opening a new position — CLAUDE.md's earnings-blackout rule "
            "depends on this, not on inferring timing from news headlines."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "symbol": {"type": "string", "description": "Ticker symbol, e.g. NVDA"},
            },
            "required": ["symbol"],
        },
    },
    {
        "name": "place_order",
        "description": (
            "Place a buy or sell order on the paper-trading account. Always state the exact "
            "symbol, quantity, side, and order type (market or limit) in your reply before "
            "calling this. Interactively this pauses for a human y/N confirmation at the "
            "terminal; in unattended/scheduled runs (TRADER_UNATTENDED=1) it auto-approves but "
            "requires a limit_price and is capped in size and count — a rejection means you hit "
            "one of those caps or the human declined, not a transient error. Do not retry the "
            "same order after a rejection; report why it wasn't placed instead."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "symbol": {"type": "string", "description": "Ticker symbol, e.g. NVDA"},
                "qty": {"type": "number", "description": "Number of shares"},
                "side": {"type": "string", "enum": ["buy", "sell"]},
                "limit_price": {"type": "number", "description": "Omit for a market order (rejected in unattended mode)"},
            },
            "required": ["symbol", "qty", "side"],
        },
    },
    {
        "name": "cancel_all_orders",
        "description": (
            "Cancel all open orders on the paper-trading account. Requires human approval at the "
            "terminal interactively, or auto-approves under TRADER_UNATTENDED=1."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
]

SYSTEM_PROMPT = (
    "You are Trader, an equities agent operating on a PAPER-TRADING (fake money) "
    "Alpaca account. You can inspect account status, positions, price history, "
    "news, and market hours, and you can place or cancel orders on this paper "
    "account. Before calling place_order or cancel_all_orders, state the exact "
    "order details in plain text. Interactively, those two tools pause for a "
    "human y/N confirmation at the terminal. In unattended/scheduled runs "
    "(TRADER_UNATTENDED=1) they auto-approve instead, but place_order then "
    "requires a limit_price and is capped on notional and order count per run — "
    "if it's rejected, that's a deliberate guardrail, not a bug; report it and "
    "move on rather than retrying. Be concise and cite the specific numbers "
    "behind any claim you make."
)


def run_tool(name, tool_input):
    if name == "get_account":
        return get_account()
    if name == "get_positions":
        return get_positions()
    if name == "get_bars":
        return get_bars(tool_input["symbol"], tool_input.get("timeframe", "1Day"), tool_input.get("limit", 60))
    if name == "get_news":
        return get_news(tool_input["symbol"])
    if name == "get_market_status":
        return get_market_status()
    if name == "get_orders":
        return get_orders(tool_input.get("status", "all"), tool_input.get("limit", 100))
    if name == "get_portfolio_history":
        return get_portfolio_history(tool_input.get("period", "1W"), tool_input.get("timeframe", "1D"))
    if name == "get_earnings_date":
        return get_earnings_date(tool_input["symbol"])
    if name == "get_movers":
        return get_movers(tool_input.get("top", 10))

    if name == "place_order":
        return place_order(
            tool_input["symbol"],
            tool_input["qty"],
            tool_input["side"],
            tool_input.get("limit_price"),
        )

    if name == "cancel_all_orders":
        return cancel_all_orders()

    raise ValueError(f"Unknown tool: {name}")


def ask(question, max_turns=6):
    messages = [{"role": "user", "content": question}]

    for _ in range(max_turns):
        response = client.messages.create(
            model=MODEL,
            max_tokens=1024,
            system=SYSTEM_PROMPT,
            tools=TOOLS,
            messages=messages,
        )

        if response.stop_reason != "tool_use":
            return "".join(block.text for block in response.content if block.type == "text")

        messages.append({"role": "assistant", "content": response.content})

        tool_results = []
        for block in response.content:
            if block.type == "tool_use":
                try:
                    result = run_tool(block.name, block.input)
                except Exception as exc:
                    result = {"error": str(exc)}
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": json.dumps(result),
                })
        messages.append({"role": "user", "content": tool_results})

    return "Reached max tool-call turns without a final answer."


if __name__ == "__main__":
    question = " ".join(sys.argv[1:]) or "Give me a quick status update on the portfolio."
    print(ask(question))
