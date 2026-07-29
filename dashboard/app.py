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

# Fetching full history is a GitHub API call per file (day tabs + the symbol
# matrix need every entry, not just the latest) — cache it too, both to keep
# each dashboard poll fast and to stay well under GitHub's rate limit.
CONTENT_CACHE_TTL = 45
MAX_JOURNAL_ENTRIES = 20
MAX_REVIEWS = 12
_content_cache = {"data": None, "fetched_at": 0}


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


def get_content():
    """Full journal + review history (capped), cached server-side so a 30s
    dashboard poll doesn't re-fetch every file on every request."""
    now = time.time()
    if _content_cache["data"] is not None and (now - _content_cache["fetched_at"]) < CONTENT_CACHE_TTL:
        return _content_cache["data"]

    journal_files = list_dir("journal")[:MAX_JOURNAL_ENTRIES]
    review_files = list_dir("reviews")[:MAX_REVIEWS]

    data = {
        "journal_entries": [
            {"date": name.replace(".md", ""), "raw": get_file(f"journal/{name}")}
            for name in journal_files
        ],
        "reviews": [
            {"date": name.replace(".md", ""), "raw": get_file(f"reviews/{name}")}
            for name in review_files
        ],
    }
    _content_cache["data"] = data
    _content_cache["fetched_at"] = now
    return data


@app.route("/api/data")
def api_data():
    content = get_content()
    return jsonify({
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "routines": get_routine_status(),
        "journal_entries": content["journal_entries"],
        "reviews": content["reviews"],
    })


@app.route("/")
def index():
    return send_from_directory(".", "index.html")


if __name__ == "__main__":
    print("Trader live dashboard — http://localhost:5050")
    app.run(port=5050, debug=False)
