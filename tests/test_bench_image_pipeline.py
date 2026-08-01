"""Smoke tests for scripts/bench_image_pipeline.py.

Keeps the benchmark harness honest without paying benchmark cost: tiny
images, 1 repeat, no network, deterministic seed.
"""

import io
import subprocess
import sys
from pathlib import Path

import pytest

# The bench harness needs numpy; it is a dev tool, not a server
# dependency, so a machine without it SKIPS these smoke tests instead of
# failing collection (this took CI down on 2026-08-01).
np = pytest.importorskip("numpy")
from PIL import Image

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))

import bench_image_pipeline as bench  # noqa: E402


def test_photo_bytes_deterministic_and_jpeg():
    a = bench.make_photo_bytes(320, 240, 12.0, 88)
    b = bench.make_photo_bytes(320, 240, 12.0, 88)
    assert a == b, "synthetic photo generation must be deterministic"
    assert a[:3] == b"\xff\xd8\xff"


def test_aspect_box_engages_draft_scale():
    img = Image.new("RGB", (4000, 3000))
    # Square box on a 4:3 image must be aspect-corrected or PIL's draft
    # never engages (both dims must stay >= the request).
    assert bench._aspect_box(img, 1568) == (1568, 1176)
    small = Image.new("RGB", (800, 600))
    assert bench._aspect_box(small, 1568) == (800, 600)


def test_normalize_single_decode_dims_and_format():
    data = bench.make_photo_bytes(1600, 1200, 12.0, 88)
    out = bench.normalize_single_decode(data, box=800, quality=85)
    assert out[:3] == b"\xff\xd8\xff"
    img = Image.open(io.BytesIO(out))
    assert max(img.size) <= 800
    assert img.size == (800, 600)


def test_pipeline_replicas_run():
    data = bench.make_photo_bytes(640, 480, 12.0, 88)
    g = bench.prepare_for_gemini(data)
    assert max(g.size) <= 1024 and g.mode == "RGB"
    blob = bench.genai_pil_to_blob(g)
    assert blob[:3] == b"\xff\xd8\xff"
    echo = bench.compress_for_echo(data)
    assert echo[:3] == b"\xff\xd8\xff"


@pytest.mark.parametrize("flag", ["--quick"])
def test_cli_quick_run(flag, tmp_path):
    proc = subprocess.run(
        [sys.executable, str(REPO / "scripts" / "bench_image_pipeline.py"),
         flag, "--repeats", "1", "--json"],
        capture_output=True, text=True, timeout=120,
    )
    assert proc.returncode == 0, proc.stderr
    assert '"image": "quick_2mp"' in proc.stdout
