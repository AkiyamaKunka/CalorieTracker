#!/usr/bin/env python3
"""
Daily Calorie Tracker
Scans today's photos from macOS Photos library (via SQLite),
detects food using Gemini vision, estimates calories, and generates
a daily markdown report.

Usage:
    export GEMINI_API_KEY='your-key'
    python3 calorie_tracker.py
"""

import json
import logging
import os
import shutil
import sqlite3
import sys
import tempfile
from datetime import datetime, date, timezone, timedelta
from pathlib import Path
from typing import Dict, List, Optional

from google import genai
from PIL import Image

from config import (
    GEMINI_API_KEY,
    GEMINI_MODEL,
    REPORTS_DIR,
    SUPPORTED_EXTENSIONS,
    FOOD_DETECTION_PROMPT,
)

# ─── Logging ───────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("calorie_tracker")


# ─── macOS Photos Constants ───────────────────────────────────────
# macOS Photos stores dates as seconds since 2001-01-01 (Core Data epoch)
CORE_DATA_EPOCH = datetime(2001, 1, 1, tzinfo=timezone.utc)

# Default Photos library path
DEFAULT_PHOTOS_LIB = (
    Path.home() / "Pictures" / "Photos Library.photoslibrary"
)


# ─── Photo Scanner (Direct SQLite) ────────────────────────────────
def find_photos_library() -> Optional[Path]:
    """Find the macOS Photos library path."""
    # Check default location
    if DEFAULT_PHOTOS_LIB.exists():
        return DEFAULT_PHOTOS_LIB

    # Search common locations
    search_paths = [
        Path.home() / "Pictures",
        Path.home() / "Library" / "Pictures",
        Path.home(),
    ]
    for search_dir in search_paths:
        if not search_dir.exists():
            continue
        for lib in search_dir.glob("*.photoslibrary"):
            return lib

    # Try Spotlight search
    try:
        import subprocess
        result = subprocess.run(
            ["mdfind", "kMDItemKind == 'Photos Library'"],
            capture_output=True, text=True, timeout=10,
        )
        for line in result.stdout.strip().split("\n"):
            if line and Path(line).exists():
                return Path(line)
    except Exception:
        pass

    return None


def get_todays_photos(photos_lib: Path) -> List[Dict]:
    """
    Query the macOS Photos SQLite database for photos taken today.
    Returns a list of dicts with 'filename', 'filepath', 'date_taken'.
    """
    db_path = photos_lib / "database" / "Photos.sqlite"
    if not db_path.exists():
        log.error(f"Photos database not found at: {db_path}")
        return []

    # Copy database to temp to avoid locking issues
    with tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False) as tmp:
        tmp_db = tmp.name
    shutil.copy2(str(db_path), tmp_db)

    # Also copy WAL and SHM files if they exist
    for ext in ["-wal", "-shm"]:
        wal = Path(str(db_path) + ext)
        if wal.exists():
            shutil.copy2(str(wal), tmp_db + ext)

    try:
        conn = sqlite3.connect(tmp_db)
        conn.row_factory = sqlite3.Row

        # Calculate today's date range in Core Data epoch
        # Use LOCAL timezone so "today" matches the user's actual day
        local_tz = datetime.now(timezone.utc).astimezone().tzinfo
        today = date.today()
        today_start = datetime(today.year, today.month, today.day, tzinfo=local_tz)
        today_end = datetime(today.year, today.month, today.day, 23, 59, 59, tzinfo=local_tz)

        epoch_start = (today_start - CORE_DATA_EPOCH).total_seconds()
        epoch_end = (today_end - CORE_DATA_EPOCH).total_seconds()

        log.info(f"Scanning Photos library for {today.isoformat()}...")

        # Query for photos taken today
        # ZASSET is the main table for photos in macOS Photos
        query = """
            SELECT
                ZASSET.Z_PK as pk,
                ZASSET.ZFILENAME as filename,
                ZASSET.ZDATECREATED as date_created,
                ZASSET.ZDIRECTORY as directory,
                ZASSET.ZUNIFORMTYPEIDENTIFIER as uti
            FROM ZASSET
            WHERE ZASSET.ZDATECREATED BETWEEN ? AND ?
              AND ZASSET.ZTRASHEDSTATE = 0
              AND ZASSET.ZKIND = 0
            ORDER BY ZASSET.ZDATECREATED ASC
        """

        cursor = conn.execute(query, (epoch_start, epoch_end))
        rows = cursor.fetchall()
        conn.close()

        photos = []
        originals_dir = photos_lib / "originals"

        for row in rows:
            filename = row["filename"]
            if not filename:
                continue

            ext = Path(filename).suffix.lower()
            if ext not in SUPPORTED_EXTENSIONS:
                continue

            # Find the actual file in the originals directory
            directory = row["directory"]
            if directory:
                filepath = originals_dir / directory / filename
            else:
                filepath = None

            # If direct path doesn't work, search for the file
            if not filepath or not filepath.exists():
                # Search in originals subdirectories
                matches = list(originals_dir.rglob(filename))
                if matches:
                    filepath = matches[0]
                else:
                    log.debug(f"  Could not find file for: {filename}")
                    continue

            # Convert Core Data date to datetime
            date_taken = CORE_DATA_EPOCH + timedelta(
                seconds=row["date_created"]
            )

            photos.append({
                "filename": filename,
                "filepath": str(filepath),
                "date_taken": date_taken.strftime("%I:%M %p"),
            })

        log.info(f"Found {len(photos)} photos from today.")
        return photos

    except sqlite3.OperationalError as e:
        log.error(f"Database query failed: {e}")
        log.info("Tip: Make sure Photos.app has been opened at least once.")
        return []
    finally:
        # Clean up temp files
        for ext in ["", "-wal", "-shm"]:
            try:
                os.unlink(tmp_db + ext)
            except OSError:
                pass


# ─── Image Loading ─────────────────────────────────────────────────
def load_image_for_gemini(image_path: str) -> Image.Image:
    """
    Load an image file as a PIL Image for Gemini.
    Handles HEIC/HEIF conversion automatically.
    """
    ext = Path(image_path).suffix.lower()

    # Register HEIF opener if needed
    if ext in {".heic", ".heif"}:
        try:
            import pillow_heif
            pillow_heif.register_heif_opener()
        except ImportError:
            log.warning("pillow-heif not installed, HEIC support may be limited")

    return Image.open(image_path)


# ─── Gemini Vision ─────────────────────────────────────────────────
def analyze_photo(client: genai.Client, image_path: str) -> Optional[Dict]:
    """Send a photo to Gemini for food detection and calorie estimation."""
    try:
        img = load_image_for_gemini(image_path)

        response = client.models.generate_content(
            model=GEMINI_MODEL,
            contents=[FOOD_DETECTION_PROMPT, img],
        )

        content = response.text.strip()

        # Strip markdown code fences if present
        if content.startswith("```"):
            lines = content.split("\n")
            lines = [l for l in lines if not l.strip().startswith("```")]
            content = "\n".join(lines)

        return json.loads(content)

    except json.JSONDecodeError as e:
        log.warning(f"Could not parse API response as JSON: {e}")
        return None
    except Exception as e:
        log.error(f"Error analyzing photo: {e}")
        return None


# ─── Report Generator ──────────────────────────────────────────────
def generate_report(
    today: date,
    results: List[Dict],
    total_scanned: int,
) -> str:
    """Generate a daily calorie report as markdown."""
    date_str = today.strftime("%B %-d, %Y")
    lines = [f"# 🍽️ Daily Calorie Report — {date_str}\n"]

    food_results = [r for r in results if r["analysis"].get("is_food")]
    non_food_count = total_scanned - len(food_results)

    if not food_results:
        lines.append("No food photos detected today.\n")
    else:
        lines.append("## Meals\n")
        for r in food_results:
            analysis = r["analysis"]
            filename = r["filename"]
            time_taken = r.get("time", "")
            time_label = f" ({time_taken})" if time_taken else ""

            lines.append(f"### 📸 {filename}{time_label}\n")
            lines.append(f"- **Meal**: {analysis.get('meal_description', 'Unknown')}")

            items = analysis.get("food_items", [])
            if items:
                lines.append("- **Items**:")
                for item in items:
                    name = item.get("name", "Unknown")
                    cals = item.get("estimated_calories", "?")
                    lines.append(f"  - {name}: ~{cals} kcal")

            total = analysis.get("total_calories", "?")
            lines.append(f"- **Subtotal**: ~{total} kcal")

            note = analysis.get("confidence_note", "")
            if note:
                lines.append(f"- **Note**: {note}")
            lines.append("")

    # Summary table
    grand_total = sum(
        r["analysis"].get("total_calories", 0)
        for r in food_results
    )

    lines.append("---\n")
    lines.append("## 📊 Daily Summary\n")
    lines.append("| Metric | Value |")
    lines.append("|--------|-------|")
    lines.append(f"| Photos scanned | {total_scanned} |")
    lines.append(f"| Food photos found | {len(food_results)} |")
    lines.append(f"| Non-food photos skipped | {non_food_count} |")
    lines.append(f"| **Total estimated calories** | **{grand_total} kcal** |")
    lines.append("")
    lines.append(
        "> ⚠️ These are AI estimates based on visual analysis "
        "and may not be perfectly accurate."
    )
    lines.append(
        f"\n---\n*Generated at {datetime.now().strftime('%I:%M %p')} "
        f"by CalorieTracker*\n"
    )

    return "\n".join(lines)


def save_report(report: str, today: date) -> Path:
    """Save the markdown report to the reports directory."""
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    report_path = REPORTS_DIR / f"{today.isoformat()}.md"
    report_path.write_text(report, encoding="utf-8")
    return report_path


# ─── Main ──────────────────────────────────────────────────────────
def main():
    """Main entry point for the daily calorie tracker."""
    log.info("=" * 50)
    log.info("🍽️  Daily Calorie Tracker")
    log.info("=" * 50)

    # Validate API key
    if not GEMINI_API_KEY:
        log.error(
            "GEMINI_API_KEY not set. "
            "Please set it: export GEMINI_API_KEY='your-key-here'"
        )
        sys.exit(1)

    # Configure Gemini client (new google-genai SDK)
    client = genai.Client(api_key=GEMINI_API_KEY)

    today = date.today()

    # Step 1: Find Photos library
    photos_lib = find_photos_library()
    if not photos_lib:
        log.error(
            "Could not find macOS Photos library. "
            "Make sure Photos.app has been opened at least once "
            "and iCloud Photos is enabled."
        )
        sys.exit(1)

    log.info(f"Using Photos library: {photos_lib}")

    # Step 2: Get today's photos
    photos = get_todays_photos(photos_lib)
    if not photos:
        log.info("No photos found for today. Generating empty report.")
        report = generate_report(today, [], 0)
        path = save_report(report, today)
        log.info(f"Report saved to {path}")
        return

    # Step 3: Analyze each photo
    results = []
    for i, photo in enumerate(photos, 1):
        filename = photo["filename"]
        filepath = photo["filepath"]
        log.info(f"[{i}/{len(photos)}] Processing: {filename}")

        if not Path(filepath).exists():
            log.warning(f"  Skipping {filename} (file not found — may need iCloud download)")
            continue

        # Check file size (skip very small files that might be thumbnails)
        file_size = Path(filepath).stat().st_size
        if file_size < 10_000:  # Less than 10KB
            log.info(f"  Skipping {filename} (too small: {file_size} bytes)")
            continue

        # Analyze with Gemini
        log.info(f"  Sending to Gemini for analysis...")
        analysis = analyze_photo(client, filepath)

        if analysis is None:
            log.warning(f"  Skipping {filename} (analysis failed)")
            continue

        is_food = analysis.get("is_food", False)
        if is_food:
            total_cal = analysis.get("total_calories", "?")
            desc = analysis.get("meal_description", "?")
            log.info(f"  ✅ Food detected: {desc} (~{total_cal} kcal)")
        else:
            log.info(f"  ⏭️  Not food — skipping")

        results.append({
            "filename": filename,
            "analysis": analysis,
            "time": photo.get("date_taken", ""),
        })

    # Step 4: Generate and save report
    food_count = sum(1 for r in results if r["analysis"].get("is_food"))
    log.info(
        f"\nAnalysis complete: {food_count} food photos "
        f"out of {len(photos)} total"
    )

    report = generate_report(today, results, len(photos))
    path = save_report(report, today)

    log.info(f"📄 Report saved to: {path}")
    log.info("Done!")


if __name__ == "__main__":
    main()
