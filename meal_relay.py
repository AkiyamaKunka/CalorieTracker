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
import sys
import logging
from datetime import datetime, date
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path

import database
from config import TELEGRAM_CHAT_ID

# ─── Logging ──────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("meal_relay")

PORT = 8765


class MealHandler(BaseHTTPRequestHandler):
    """Handle incoming meal data from Android."""

    def do_POST(self, *args, **kwargs):
        try:
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

            database.save_meal(chat_id, date_str, meal_time, timestamp_str, source, image_hash, filename, analysis)

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

    server = HTTPServer(("0.0.0.0", port), MealHandler)
    log.info(f"🔗 Meal Relay Server running on port {port}")
    log.info(f"   Android can POST meals to: http://<mac-ip>:{port}")
    log.info(f"   Press Ctrl+C to stop.\n")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("\n👋 Relay stopped.")
        server.server_close()


if __name__ == "__main__":
    main()
