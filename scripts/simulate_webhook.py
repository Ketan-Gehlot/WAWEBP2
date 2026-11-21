"""
Utility script to send signed sample Meta WhatsApp webhook payloads to a local KAP server.
Usage:
    python scripts/simulate_webhook.py --event incoming_greeting
"""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx
from app.config import settings
from app.core.security import compute_webhook_signature


def main() -> None:
    parser = argparse.ArgumentParser(description="Send signed sample Meta webhook events locally.")
    parser.add_argument(
        "--url",
        default="http://127.0.0.1:8000/webhook",
        help="Target webhook URL",
    )
    parser.add_argument(
        "--event",
        default="incoming_greeting",
        choices=[
            "incoming_greeting",
            "incoming_brake_pads_inquiry",
            "incoming_location_inquiry",
            "incoming_opt_out_stop",
            "delivery_and_read_status_update",
        ],
        help="Sample event key from scripts/sample_webhook_payloads.json",
    )
    parser.add_argument(
        "--secret",
        default=settings.META_APP_SECRET or "local_dev_app_secret",
        help="Meta App Secret used to sign X-Hub-Signature-256",
    )
    args = parser.parse_args()

    payload_file = Path(__file__).resolve().parent / "sample_webhook_payloads.json"
    all_payloads = json.loads(payload_file.read_text(encoding="utf-8"))
    selected = all_payloads[args.event]

    raw_bytes = json.dumps(selected).encode("utf-8")
    signature = compute_webhook_signature(raw_bytes, args.secret)

    response = httpx.post(
        args.url,
        content=raw_bytes,
        headers={
            "Content-Type": "application/json",
            "X-Hub-Signature-256": signature,
        },
        timeout=10.0,
    )
    print(f"POST {args.url} [{args.event}] -> HTTP {response.status_code}: {response.text}")


if __name__ == "__main__":
    main()
