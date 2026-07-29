# dashboard/app.py
#
# Local live dashboard for the Trader repo. Pulls journal/review/scout content
# straight from GitHub via the `gh` CLI (already authenticated on this
# machine), so it always reflects whatever the cloud routines have actually
# committed — no claude.ai connector needed, this just runs on your machine.
#
# Run: python dashboard/app.py
# View: http://localhost:5050

import base64
import json
import os
import subprocess
import time
from datetime import datetime, timezone

from flask import Flask, jsonify, send_from_directory

app = Flask(__name__)

REPO = "fabjon14-cmd/Jarvis-trader"
BRANCH = "claude/trading-journal"
CLAUDE_BIN = os.path.expanduser("~/.local/bin/claude")

# Routine status comes from an actual `claude -p "/schedule list"` call, which
# takes several seconds (it's a real agent turn, not a plain API call), so we
# cache it server-side rather than re-running it on every 30s dashboard poll.
STATUS_CACHE_TTL = 120
_status_cache = {"data": None, "fetched_at": 0}


def gh_api(path):
    """Call the GitHub API via the gh CLI's existing auth. Returns None on failure."""
    result = subprocess.run(
        ["gh", "api", f"repos/{REPO}/{path}"],
        capture_output=True, text=True, timeout=15,
    )
    if result.returncode != 0:
        return None
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        return None


def list_dir(path):
    """List filenames in a repo directory on BRANCH, newest-first by name (dates sort lexically)."""
    data = gh_api(f"contents/{path}?ref={BRANCH}")
    if not isinstance(data, list):
        return []
    return sorted((item["name"] for item in data), reverse=True)


def get_file(path):
    """Fetch and decode a file's content from BRANCH."""
    data = gh_api(f"contents/{path}?ref={BRANCH}")
    if not data or "content" not in data:
        return None
    return base64.b64decode(data["content"]).decode("utf-8")


def get_routine_status():
    """Live agent status via the local `claude` CLI's own login — a real
    /schedule list turn, not a cached API response. Cached server-side since
    each call takes several seconds."""
    now = time.time()
    if _status_cache["data"] is not None and (now - _status_cache["fetched_at"]) < STATUS_CACHE_TTL:
        return _status_cache["data"]

    try:
        result = subprocess.run(
            [
                CLAUDE_BIN, "-p",
                "Run /schedule list, then output ONLY a raw JSON array (no markdown, "
                "no code fences, no commentary) of objects with keys: name, cron, "
                "next_run_utc, enabled, last_fired_utc (null if never/unknown).",
            ],
            capture_output=True, text=True, timeout=45,
        )
        if result.returncode == 0:
            data = json.loads(result.stdout.strip())
            _status_cache["data"] = data
            _status_cache["fetched_at"] = now
            return data
    except Exception:
        pass

    return _status_cache["data"]  # stale (or None) rather than blocking the page on failure


@app.route("/api/data")
def api_data():
    journal_files = list_dir("journal")
    review_files = list_dir("reviews")
    scout_files = list_dir("scout")

    latest_journal = journal_files[0] if journal_files else None
    latest_review = review_files[0] if review_files else None
    latest_scout = scout_files[0] if scout_files else None

    return jsonify({
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "routines": get_routine_status(),
        "journal": {
            "date": latest_journal,
            "content": get_file(f"journal/{latest_journal}") if latest_journal else None,
        },
        "review": {
            "date": latest_review,
            "content": get_file(f"reviews/{latest_review}") if latest_review else None,
        },
        "scout": {
            "date": latest_scout,
            "content": get_file(f"scout/{latest_scout}") if latest_scout else None,
        },
    })


@app.route("/")
def index():
    return send_from_directory(".", "index.html")


if __name__ == "__main__":
    print("Trader live dashboard — http://localhost:5050")
    app.run(port=5050, debug=False)
