#!/usr/bin/env python3
"""Deterministic, offline benchmark of the per-meal image pipeline costs.

Measures (locally, no network, synthetic images only):
  (a) PIL full decode vs Image.draft('RGB', box) decode (+thumbnail)
  (b) JPEG re-encode cost at echo settings (1280px q82) and normalize
      settings (1568px q85)
  (c) md5 of original bytes (dedup hash)
  (d) temp/staging file write of original bytes vs normalized bytes
  (e) replicas of the exact pipeline stages in telegram_bot.py:
        _prepare_image_for_gemini  (full decode + thumbnail 1024 + RGB)
        + google-genai pil_to_blob re-encode (JPEG default quality)
        _compress_photo_for_echo   (full decode + thumbnail 1280 + q82)
      and the PROPOSED single-decode normalize
        (draft decode -> exif_transpose -> thumbnail 1568 -> q85 JPEG)

Synthetic images are camera-realistic 12MP JPEGs (~3-6MB) plus one ~25MB
monster, generated with a fixed numpy seed -- byte-identical across runs on
the same PIL/numpy versions.

Usage:
    python scripts/bench_image_pipeline.py [--repeats N] [--quick]

--quick uses a 1600x1200 image so the smoke test stays fast; results are
not representative, it only proves the harness runs.
"""

import argparse
import base64
import hashlib
import io
import json
import statistics
import sys
import tempfile
import time
from pathlib import Path

import numpy as np
from PIL import Image, ImageOps

SEED = 20260716


# ── synthetic photo generation ─────────────────────────────────────

def make_photo_bytes(width: int, height: int, noise_sigma: float, quality: int) -> bytes:
    """Camera-ish content: smooth gradients + blobs + sensor noise.

    Pure gradients compress too well; pure noise compresses too badly.
    This mix lands 4000x3000 in the 3-6MB band at q88 like a real phone
    JPEG. Deterministic via fixed seed.
    """
    rng = np.random.default_rng(SEED)
    y = np.linspace(0, 1, height, dtype=np.float32)[:, None]
    x = np.linspace(0, 1, width, dtype=np.float32)[None, :]
    base = 60 + 120 * (0.6 * x + 0.4 * y)

    # A few large soft "objects" (plate / food blobs)
    xx, yy = np.meshgrid(np.linspace(-1, 1, width, dtype=np.float32),
                         np.linspace(-1, 1, height, dtype=np.float32))
    blobs = (
        70 * np.exp(-((xx - 0.2) ** 2 + (yy + 0.1) ** 2) * 6)
        + 50 * np.exp(-((xx + 0.4) ** 2 + (yy - 0.3) ** 2) * 12)
    )

    img = np.empty((height, width, 3), dtype=np.float32)
    for c, phase in enumerate((0.0, 0.33, 0.66)):
        img[:, :, c] = base + blobs * (0.7 + 0.3 * phase)
    img += rng.normal(0, noise_sigma, size=img.shape).astype(np.float32)
    arr = np.clip(img, 0, 255).astype(np.uint8)
    buf = io.BytesIO()
    Image.fromarray(arr).save(buf, format="JPEG", quality=quality)
    return buf.getvalue()


# ── timing helper ──────────────────────────────────────────────────

def timeit(fn, repeats: int) -> dict:
    """Median/min wall time in ms over `repeats` runs (1 warmup)."""
    fn()  # warmup
    samples = []
    for _ in range(repeats):
        t0 = time.perf_counter()
        fn()
        samples.append((time.perf_counter() - t0) * 1000)
    return {"median_ms": round(statistics.median(samples), 1),
            "min_ms": round(min(samples), 1)}


# ── pipeline stage replicas (mirror telegram_bot.py exactly) ───────

def full_decode(data: bytes) -> Image.Image:
    img = Image.open(io.BytesIO(data))
    img.load()
    return img


def prepare_for_gemini(data: bytes) -> Image.Image:
    """Replica of _prepare_image_for_gemini (full decode, no draft)."""
    img = Image.open(io.BytesIO(data))
    img = ImageOps.exif_transpose(img)
    img.thumbnail((1024, 1024))
    return img.convert("RGB")


def genai_pil_to_blob(img: Image.Image) -> bytes:
    """Replica of google-genai 0.3.0 _transformers.pil_to_blob for RGB."""
    buf = io.BytesIO()
    img.save(buf, format="JPEG")  # SDK uses default quality (75)
    return buf.getvalue()


def compress_for_echo(data: bytes) -> bytes:
    """Replica of _compress_photo_for_echo (full decode, no draft)."""
    img = Image.open(io.BytesIO(data))
    img = ImageOps.exif_transpose(img)
    img.thumbnail((1280, 1280))
    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="JPEG", quality=82)
    return buf.getvalue()


def _aspect_box(img: Image.Image, box: int) -> tuple:
    """Aspect-corrected draft target: PIL only engages a draft scale when
    BOTH dims stay >= the requested box, so a square (1568,1568) request
    on a 4:3 image never scales. Ask for the thumbnail-equivalent box."""
    w, h = img.size
    scale = box / max(w, h)
    if scale >= 1:
        return (w, h)
    return (max(1, round(w * scale)), max(1, round(h * scale)))


def draft_decode(data: bytes, box: int) -> Image.Image:
    img = Image.open(io.BytesIO(data))
    img.draft("RGB", _aspect_box(img, box))  # DCT-domain downscale in decode
    img.load()
    return img


def normalize_single_decode(data: bytes, box: int = 1568, quality: int = 85) -> bytes:
    """PROPOSED: one draft decode -> ~1568px q85 JPEG, reused everywhere."""
    img = Image.open(io.BytesIO(data))
    img.draft("RGB", _aspect_box(img, box))
    img = ImageOps.exif_transpose(img)
    img.thumbnail((box, box))
    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="JPEG", quality=quality)
    return buf.getvalue()


# ── benchmark ──────────────────────────────────────────────────────

def bench_one(name: str, data: bytes, repeats: int, tmpdir: Path) -> dict:
    r: dict = {"image": name, "bytes": len(data), "mb": round(len(data) / 1e6, 2)}
    img = Image.open(io.BytesIO(data))
    r["dims"] = f"{img.width}x{img.height}"

    # (c) md5 of the original bytes
    r["md5"] = timeit(lambda: hashlib.md5(data).hexdigest(), repeats)

    # base64 (what the CLI must do to the temp file before upload)
    r["base64_encode"] = timeit(lambda: base64.b64encode(data), repeats)

    # (d) staging write of ORIGINAL bytes + read-back (the /upload path
    # does write -> read_bytes across the request/thread boundary)
    p = tmpdir / f"stage_{name}.jpg"

    def write_read():
        p.write_bytes(data)
        p.read_bytes()

    r["stage_write_plus_readback"] = timeit(write_read, repeats)

    # (a) full decode vs draft decode
    r["full_decode"] = timeit(lambda: full_decode(data), repeats)
    d = draft_decode(data, 1280)
    r["draft1280_decode"] = timeit(lambda: draft_decode(data, 1280), repeats)
    r["draft1280_dims"] = f"{d.width}x{d.height}"
    d = draft_decode(data, 1568)
    r["draft1568_decode"] = timeit(lambda: draft_decode(data, 1568), repeats)
    r["draft1568_dims"] = f"{d.width}x{d.height}"

    # (e) current pipeline stages, exactly as coded today
    r["gemini_prepare_full"] = timeit(lambda: prepare_for_gemini(data), repeats)
    gimg = prepare_for_gemini(data)
    blob = genai_pil_to_blob(gimg)
    r["gemini_sdk_reencode"] = timeit(lambda: genai_pil_to_blob(gimg), repeats)
    r["gemini_sdk_blob_kb"] = round(len(blob) / 1024)
    echo = compress_for_echo(data)
    r["echo_compress_full"] = timeit(lambda: compress_for_echo(data), repeats)
    r["echo_kb"] = round(len(echo) / 1024)

    # (b)/(e) proposed single-decode normalize
    norm = normalize_single_decode(data)
    r["normalize_draft_1568_q85"] = timeit(lambda: normalize_single_decode(data), repeats)
    r["normalized_kb"] = round(len(norm) / 1024)
    nimg = Image.open(io.BytesIO(norm))
    r["normalized_dims"] = f"{nimg.width}x{nimg.height}"

    # (d) temp write of normalized bytes (CLI temp file, proposed)
    p2 = tmpdir / f"norm_{name}.jpg"
    r["temp_write_normalized"] = timeit(lambda: p2.write_bytes(norm), repeats)

    # downstream reuse costs on the normalized bytes
    r["echo_from_normalized"] = timeit(lambda: compress_for_echo(norm), repeats)
    r["gemini_from_normalized"] = timeit(lambda: prepare_for_gemini(norm), repeats)
    return r


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repeats", type=int, default=5)
    ap.add_argument("--quick", action="store_true",
                    help="tiny image, smoke-test only")
    ap.add_argument("--json", action="store_true", help="raw JSON output")
    args = ap.parse_args()

    if args.quick:
        specs = [("quick_2mp", 1600, 1200, 12.0, 88)]
    else:
        specs = [
            ("phone_12mp_typical", 4000, 3000, 12.0, 88),   # ~3-6MB target
            ("phone_12mp_noisy", 4000, 3000, 28.0, 92),     # low-light, bigger
            ("monster_48mp", 8000, 6000, 30.0, 95),         # ~25MB target
        ]

    results = []
    with tempfile.TemporaryDirectory() as td:
        tmpdir = Path(td)
        for name, w, h, sigma, q in specs:
            data = make_photo_bytes(w, h, sigma, q)
            results.append(bench_one(name, data, args.repeats, tmpdir))

    if args.json:
        print(json.dumps(results, indent=2))
        return 0

    for r in results:
        print(f"\n=== {r['image']}  {r['dims']}  {r['mb']}MB ===")
        for k, v in r.items():
            if isinstance(v, dict):
                print(f"  {k:32s} {v['median_ms']:8.1f} ms  (min {v['min_ms']}ms)")
            elif k not in ("image", "dims", "mb", "bytes"):
                print(f"  {k:32s} {v}")

    print("\nNOTE: local numbers. The GCP e2-micro (shared vCPU) is roughly "
          "4-8x slower single-threaded than an Apple-silicon core; scale "
          "accordingly when projecting VM latency.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
