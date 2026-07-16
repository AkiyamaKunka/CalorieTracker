#!/usr/bin/env python3
"""Compute production latency distributions from a journalctl dump.

Deterministic, stdlib-only, no network. Feed it the output of:

    sudo journalctl -u caloriebot.service --since '...' --no-pager -o short-precise

(saved to a file). It pairs log lines into per-photo / per-message chains and
prints count / min / p50 / p95 / max for each segment, plus a per-sample table.

Pairing rules
-------------
Chains are tracked per service PID (a restart closes all open chains).
When more than one chain of the same kind is in flight, completion lines
cannot be attributed with certainty (the log lines carry no photo id), so
samples that overlapped another chain are flagged "concurrent" — their
FIFO pairing is a best guess and they are excluded from the headline
distribution (reported separately).

Usage:
    python3 scripts/journal_latency_report.py JOURNAL_FILE [--year 2026]
"""

import argparse
import re
from datetime import datetime

MONTHS = {m: i + 1 for i, m in enumerate(
    "Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split())}

# short-precise: "Jul 04 17:51:46.457580 host unit[pid]: message"
LINE_RE = re.compile(
    r"^(?P<mon>[A-Z][a-z]{2}) (?P<day>[ \d]\d) "
    r"(?P<time>\d\d:\d\d:\d\d\.\d{6}) \S+ "
    r"(?P<unit>[^\[]+)\[(?P<pid>\d+)\]: (?P<msg>.*)$"
)

UPLOAD_START = "Analyzing food from"
UPLOAD_FOOD = "✅ API Food:"
UPLOAD_NOTFOOD = "API Upload: Not food"
UPLOAD_FAILED = "API Upload: Analysis failed"
TG_START = "Received photo, analyzing"
TG_FOOD = "✅ Food:"
TG_NOTFOOD = "⏭️ Not food"
TG_ERROR = "Error processing photo"
ANALYZED_CLAUDE = "analyzed by Claude"
ANALYZED_GEMINI = "analyzed by Gemini"
BOOT_STARTED = "Started Calorie Tracker"
BOOT_RUNNING = "Bot is running"
TEXT_START_RE = re.compile(r"\[[^\]]+\] Text: ")
TEXT_ENDS = (
    "✅ Manual Food:", "✏️ Corrected meal", "💬 Chat response sent",
    "⚖️ NL weight logged", "🏃 NL activity logged", "🗑️ NL delete confirmed",
    "Deterministic run query", "Deterministic macro query",
    "Deterministic plan query", "NL response had no usable actions",
)


def parse_ts(m, year):
    return datetime(year, MONTHS[m.group("mon")], int(m.group("day")),
                    *map(int, m.group("time")[:8].split(":")),
                    int(m.group("time")[9:]))


def pctl(sorted_vals, p):
    """Nearest-rank percentile."""
    if not sorted_vals:
        return None
    k = max(0, min(len(sorted_vals) - 1,
                   int(round(p / 100.0 * len(sorted_vals) + 0.5)) - 1))
    return sorted_vals[k]


class Chain:
    def __init__(self, kind, start, concurrent):
        self.kind = kind
        self.start = start
        self.analyzed = None      # (ts, engine)
        self.end = None           # (ts, outcome)
        self.concurrent = concurrent


def analyze(path, year):
    boots = []            # (started_ts or None, running_ts, pid)
    chains = []           # completed Chain objects
    texts = []            # (start, end, marker, concurrent_flag)
    open_by_pid = {}      # pid -> list of open Chain (upload+telegram mixed FIFO)
    open_text = {}        # pid -> start ts
    pending_boot = None   # ts of last systemd Started
    last_pid_seen = None

    with open(path, encoding="utf-8", errors="replace") as f:
        for raw in f:
            m = LINE_RE.match(raw.rstrip("\n"))
            if not m:
                continue
            ts, pid, msg = parse_ts(m, year), m.group("pid"), m.group("msg")

            if "systemd" in m.group("unit"):
                if BOOT_STARTED in msg:
                    pending_boot = ts
                continue

            if pid != last_pid_seen:
                # new process: every chain left open in older pids is dead
                for stale in list(open_by_pid):
                    if stale != pid:
                        open_by_pid.pop(stale)
                        open_text.pop(stale, None)
                last_pid_seen = pid

            inflight = open_by_pid.setdefault(pid, [])

            if BOOT_RUNNING in msg:
                boots.append((pending_boot, ts, pid))
                pending_boot = None
            elif UPLOAD_START in msg or TG_START in msg:
                kind = "upload" if UPLOAD_START in msg else "telegram"
                for c in inflight:
                    c.concurrent = True
                inflight.append(Chain(kind, ts, bool(inflight)))
            elif ANALYZED_CLAUDE in msg or ANALYZED_GEMINI in msg:
                engine = "claude" if ANALYZED_CLAUDE in msg else "gemini"
                for c in inflight:
                    if c.analyzed is None:
                        c.analyzed = (ts, engine)
                        break
            elif any(k in msg for k in (UPLOAD_FOOD, UPLOAD_NOTFOOD, UPLOAD_FAILED,
                                        TG_FOOD, TG_NOTFOOD, TG_ERROR)):
                outcome = ("food" if (UPLOAD_FOOD in msg or TG_FOOD in msg)
                           else "not_food" if "Not food" in msg or TG_NOTFOOD in msg
                           else "failed")
                if inflight:
                    c = inflight.pop(0)   # FIFO best guess
                    c.end = (ts, outcome)
                    chains.append(c)
            elif TEXT_START_RE.search(msg):
                open_text[pid] = ts       # a newer Text abandons the older one
            elif any(k in msg for k in TEXT_ENDS):
                if pid in open_text:
                    texts.append((open_text.pop(pid), ts, msg.strip()[:60]))

    return boots, chains, texts


def fmt_dist(vals):
    if not vals:
        return "n=0"
    s = sorted(vals)
    return (f"n={len(s)}  min={s[0]:6.2f}  p50={pctl(s, 50):6.2f}  "
            f"p95={pctl(s, 95):6.2f}  max={s[-1]:6.2f}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("journal")
    ap.add_argument("--year", type=int, default=2026)
    ap.add_argument("--samples", action="store_true",
                    help="print every sample, not just distributions")
    args = ap.parse_args()

    boots, chains, texts = analyze(args.journal, args.year)

    print("== Boot: systemd Started -> 'Bot is running' ==")
    bvals = [(r - s).total_seconds() for s, r, _ in boots if s]
    print("  " + fmt_dist(bvals))
    if args.samples:
        for s, r, pid in boots:
            if s:
                print(f"    {s}  pid={pid}  {(r - s).total_seconds():6.2f}s")

    def dur(c):
        return (c.end[0] - c.start).total_seconds()

    for kind in ("upload", "telegram"):
        done = [c for c in chains if c.kind == kind and c.end]
        clean = [c for c in done if not c.concurrent]
        conc = [c for c in done if c.concurrent]
        print(f"\n== {kind} photo chain: worker start -> terminal log ==")
        for label, subset in (("single-flight", clean), ("concurrent(FIFO-guess)", conc)):
            for eng in ("claude", "gemini", None):
                sel = [c for c in subset
                       if (c.analyzed[1] if c.analyzed else None) == eng]
                if not sel:
                    continue
                total = fmt_dist([dur(c) for c in sel])
                print(f"  {label:24s} analyzer={eng or 'unlogged(pre-deploy)'}"
                      f"  total: {total}")
                asel = [c for c in sel if c.analyzed]
                if asel:
                    print(f"  {'':24s}   start->analyzed: "
                          + fmt_dist([(c.analyzed[0] - c.start).total_seconds()
                                      for c in asel]))
                    print(f"  {'':24s}   analyzed->card:  "
                          + fmt_dist([(c.end[0] - c.analyzed[0]).total_seconds()
                                      for c in asel]))
                if args.samples:
                    for c in sel:
                        print(f"    {c.start}  {dur(c):6.2f}s  {c.end[1]}"
                              f"{'  [concurrent]' if c.concurrent else ''}")

    print("\n== NL text: received -> handled (Gemini NL path + deterministic) ==")
    det = [(s, e, mk) for s, e, mk in texts if "Deterministic" in mk]
    nl = [(s, e, mk) for s, e, mk in texts if "Deterministic" not in mk]
    print("  gemini-NL:      " + fmt_dist([(e - s).total_seconds() for s, e, _ in nl]))
    print("  deterministic:  " + fmt_dist([(e - s).total_seconds() for s, e, _ in det]))
    if args.samples:
        for s, e, mk in texts:
            print(f"    {s}  {(e - s).total_seconds():6.2f}s  {mk}")


if __name__ == "__main__":
    main()
