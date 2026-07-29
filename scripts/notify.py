# scripts/notify.py

import os
import sys
import json
import requests

from dotenv import load_dotenv

load_dotenv()

RESEND_API_KEY = os.getenv("RESEND_API_KEY")
REPORT_TO_EMAIL = os.getenv("REPORT_TO_EMAIL")
REQUEST_TIMEOUT = 15


def send_email(subject, body):
    """Send a plain-text email report via Resend."""
    url = "https://api.resend.com/emails"
    headers = {
        "Authorization": f"Bearer {RESEND_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "from": "Trader <onboarding@resend.dev>",
        "to": [REPORT_TO_EMAIL],
        "subject": subject,
        "text": body,
    }
    response = requests.post(url, headers=headers, json=payload, timeout=REQUEST_TIMEOUT)
    response.raise_for_status()
    return response.json()


if __name__ == "__main__":
    subject = sys.argv[1] if len(sys.argv) > 1 else "Trader report"

    if len(sys.argv) > 2:
        with open(sys.argv[2], "r") as f:
            body = f.read()
    else:
        body = sys.stdin.read()

    print(json.dumps(send_email(subject, body)))
