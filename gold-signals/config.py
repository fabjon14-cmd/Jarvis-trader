"""Settings for the gold signal bot (Python port of GoldSignalsPro TV, 30m "Day" mode).

Defaults match the TradingView settings the operator settled on (Oct 2026):
confirmed candle-close entries, falling-knife filter, profit lock 50%/15%,
no A-grade sells, liquidity-sweep bonus, plus the "H4 zone + 30m trigger" rule
used for the email alerts.
"""

SYMBOL_LABEL = "XAUUSD"

# ---- levels / zones (price units, gold ~4000) ----
CLUSTER_DIST = 4.0        # merge levels closer than this into one zone
LEVEL_RANGE = 120.0       # only keep levels within this distance of price
ROUND_STEP = 10.0         # round-number spacing
MIN_LEVEL_STR = 4.0       # zone strength needed to trade it (Day mode)
MAJOR_MIN_STR = 8.0       # "major" zone for the final target
FRESH_BARS = 300          # candles looked back for zone tests

# ---- Day mode (30m chart): ATR from 1h, trend from 4h ----
SWEEP_MAX_ATR = 0.70      # wick may poke this far beyond the zone
SWEEP_MIN_ATR = 0.05      # minimum poke to count as a liquidity sweep
SL_BUF_ATR = 0.10         # stop buffer beyond the zone
MIN_RISK_ATR = 0.30
MAX_RISK_ATR = 1.50
MIN_RR = 1.5
COOLDOWN_MIN = 120        # minutes before the same level can signal again
STRUCT_LEN = 3            # swing size for BOS / CHoCH

# ---- targets / trade management ----
TP_FRONT_ATR = 0.25       # targets sit this far before the next zone
PARTIAL_PCT = 50.0        # close this % at TP1
TP2_MAX_ATR = 3.5
LOCK_ON = True
LOCK_TRIG = 50.0          # % of the way to TP1 that arms the lock
LOCK_AT = 15.0            # stop moves to this % of the way to TP1

# ---- filters / scoring ----
MIN_SCORE = 60.0
NO_A_SELLS = True         # skip sells scoring 75+ (they lost in every backtest)
KNIFE_ON = True
KNIFE_BARS = 4
KNIFE_ATR = 2.0
KNIFE_BIG = 1.5
SWEEP_BONUS = 8.0
RETEST_BARS = 12
RETEST_PTS = 5.0
MOM_PTS = 6.0
PUSH_PTS = 8.0
RSI_LEN = 14
RSI_OS = 35.0
RSI_OB = 65.0
CROSS_BARS = 8            # 1h candles used for the dollar / silver direction
CROSS_THRESH = 1.0
CROSS_PTS = 6.0
VOL_SPIKE = 1.5
VOL_PTS = 5.0

# ---- "H4 zone + 30m trigger" (email bot rule) ----
REQUIRE_HTF_ZONE = True   # zone must contain an H4 swing, yesterday's or last week's high/low
REQUIRE_30M_AGREE = True  # 30m structure must point the same way as the trade

# ---- position size shown in the email ----
ACCOUNT_SIZE = 1000.0
RISK_PCT = 1.0
CONTRACT_OZ = 100.0       # 1.00 lot = 100 oz
LOT_STEP = 0.01

# ---- sessions (UK / New York local time) ----
ASIA = ("00:00", "07:00", "Europe/London")
LONDON = ("08:00", "16:30", "Europe/London")
NEWYORK = ("08:00", "17:00", "America/New_York")

# ---- alerts ----
STALE_MIN = 45             # a 30m event older than this (after its candle closed) is summarised, not alerted

# ---- 5m entry helper ----
# When a 30m candle trades into an H4 zone in the H4 trend direction (and no
# trade is open) the bot emails a WATCH note, then checks 5m candles for a
# rejection / reversal inside the zone and emails a tighter "5m ENTRY".
HELPER_5M = True
WATCH_HOURS = 2.0          # stop watching 5m after this long
SL5_BUF_ATR = 0.10         # 5m stop: beyond the trigger candle's wick by this x 1h ATR
MIN_RISK5_ATR = 0.35       # 5m / 1m stop never tighter than this x 1h ATR (0.20 was inside 1m noise)
LIVE_WATCH = True          # between 30m closes, start a WATCH as soon as a 1m candle reaches an H4 zone
ARM_RANGE_ATR = 4.0        # ...only zones within this x ATR of the last 30m close
ARM_MINUTES = 60           # ...armed zones stay valid this long if the next 30m close is late
WATCH_DROP_ATR = 1.5       # end a watch once price runs this x ATR past the zone (the entry is gone)
HELPER_1M = True           # also check 1m candles while watching (first trigger, 1m or 5m, wins)
# Runs are every ~5 minutes, so a 1m / 5m trigger can be a few minutes old when the email
# goes out. If price has already reached the stop or TP1 by then, no entry email is sent.
