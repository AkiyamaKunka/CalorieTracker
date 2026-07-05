#!/usr/bin/env python3
"""
CalorieTracker Meal Relay Server

Simple HTTP server that receives meal data from Android (Termux)
and saves it to the meals log. Runs on the Mac alongside the
Telegram bot.

Usage:
    python3 meal_relay.py              # Default port 8765
    python3 meal_relay.py --port 9000  # Custom port
"""

import json
import hmac
import os
import sys
import logging
from datetime import datetime, date
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path

import database
from config import ANDROID_API_KEY, TELEGRAM_CHAT_ID

# ─── Logging ──────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("meal_relay")

PORT = 8765
HOST = os.environ.get("MEAL_RELAY_HOST", "127.0.0.1")
RELAY_API_KEY = os.environ.get("MEAL_RELAY_API_KEY") or ANDROID_API_KEY


class MealHandler(BaseHTTPRequestHandler):
    """Handle incoming meal data from Android."""

    def _authorized(self) -> bool:
        if not RELAY_API_KEY:
            return False
        provided = self.headers.get("X-API-Key", "")
        return hmac.compare_digest(str(provided), str(RELAY_API_KEY))

    def do_POST(self, *args, **kwargs):
        try:
            if not self._authorized():
                self.send_response(401)
                self.end_headers()
                self.wfile.write(b'{"error": "Unauthorized"}')
                return

            if not TELEGRAM_CHAT_ID:
                self.send_response(500)
                self.end_headers()
                self.wfile.write(b'{"error": "TELEGRAM_CHAT_ID is not configured"}')
                return

            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length)
            data = json.loads(body)

            analysis = data.get("analysis", {})
            filename = data.get("filename", "android_photo")
            source = data.get("source", "android")
            meal_time = data.get("time", datetime.now().strftime("%I:%M %p"))

            if not analysis.get("is_food"):
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'{"status": "skipped", "reason": "not food"}')
                return

            # Save to SQLite database
            chat_id = int(TELEGRAM_CHAT_ID)
            date_str = date.today().isoformat()
            timestamp_str = datetime.now().isoformat()
            image_hash = data.get("image_hash", "")

            if image_hash and not database.reserve_photo_hash(chat_id, image_hash, source, timestamp_str):
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'{"status": "duplicate"}')
                return

            meal_id = database.save_meal(chat_id, date_str, meal_time, timestamp_str, source, image_hash, filename, analysis)
            if image_hash:
                database.mark_photo_hash_status(chat_id, image_hash, "saved", meal_id, source=source)

            desc = analysis.get("meal_description", "?")
            cals = analysis.get("total_calories", "?")
            log.info(f"✅ Saved Android meal: {desc} (~{cals} kcal)")

            self.send_response(200)
            self.end_headers()
            self.wfile.write(json.dumps({"status": "saved"}).encode())

        except Exception as e:
            log.error(f"Error processing meal: {e}")
            self.send_response(500)
            self.end_headers()
            self.wfile.write(json.dumps({"error": str(e)}).encode())

    def do_GET(self, *args, **kwargs):
        """Health check endpoint."""
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b'{"status": "ok", "service": "CalorieTracker Meal Relay"}')

    def log_message(self, format, *args):
        """Suppress default request logging."""
        pass


def main():
    port = PORT
    if "--port" in sys.argv:
        idx = sys.argv.index("--port")
        port = int(sys.argv[idx + 1])

    server = HTTPServer((HOST, port), MealHandler)
    log.info(f"🔗 Meal Relay Server running on port {port}")
    log.info(f"   Listening on: http://{HOST}:{port}")
    log.info(f"   Press Ctrl+C to stop.\n")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("\n👋 Relay stopped.")
        server.server_close()


if __name__ == "__main__":
    main()
