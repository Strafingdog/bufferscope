#!/usr/bin/env python3
"""Segment a passive bufferscope run by the router's throughput, then report
latency per load period.

bufferscope normally learns its phase boundaries from Ookla's event stream. With
any other load generator there is no such stream, so this derives the periods
from what the WAN actually carried - which is a better referee anyway, since
it depends on neither tool's claims.

Usage:
    python analyse_load.py runs/some-probe-run.json [--threshold MBPS]
"""
import argparse
import json
import sys

import bufferscope

# A period counts as "loaded" above this share of the run's peak throughput.
DEFAULT_SHARE = 0.35
MIN_PERIOD_SECONDS = 3.0


def rates(samples):
    """Per-interval (t_mid, down_mbps, up_mbps) from counter snapshots."""
    out = []
    for (t0, c0), (t1, c1) in zip(samples, samples[1:]):
        seconds = t1 - t0
        if seconds <= 0:
            continue
        best_rx = best_tx = 0
        for name, (rx1, tx1) in c1.items():
            if name not in c0:
                continue
            rx0, tx0 = c0[name]
            best_rx = max(best_rx, max(0, rx1 - rx0))
            best_tx = max(best_tx, max(0, tx1 - tx0))
        out.append(((t0 + t1) / 2.0,
                    best_rx * 8 / 1e6 / seconds,
                    best_tx * 8 / 1e6 / seconds))
    return out


def periods(points, index, threshold):
    """Contiguous spans where one direction exceeds the threshold."""
    spans, start, last = [], None, None
    for t, down, up in points:
        value = (down, up)[index]
        if value >= threshold:
            if start is None:
                start = t
            last = t
        elif start is not None:
            if last - start >= MIN_PERIOD_SECONDS:
                spans.append((start, last))
            start = None
    if start is not None and last - start >= MIN_PERIOD_SECONDS:
        spans.append((start, last))
    return spans


def stats_in(series, spans):
    picked = [(t, v) for t, v in series
              if any(a <= t <= b for a, b in spans)]
    return bufferscope.summarize(picked)


def outside(series, all_spans):
    picked = [(t, v) for t, v in series
              if not any(a <= t <= b for a, b in all_spans)]
    return bufferscope.summarize(picked)


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("path")
    ap.add_argument("--threshold", type=float, default=None,
                    help="Mbps above which a period counts as loaded")
    args = ap.parse_args(argv)

    doc = json.load(open(args.path, encoding="utf-8"))
    probes = (doc.get("phases") or {}).get("idle", {}).get("probes") or {}
    if not probes or "samples" not in next(iter(probes.values())):
        print("error: this run has no raw samples - re-run with --raw",
              file=sys.stderr)
        return 1

    router = (doc.get("router_throughput") or {}).get("samples")
    if not router:
        print("error: no router counter series in this run.\n"
              "  Re-run with --router-snmp so load periods can be derived.",
              file=sys.stderr)
        return 1

    points = rates([(t, {k: tuple(v) for k, v in c.items()}) for t, c in router])
    if not points:
        print("error: too few router samples", file=sys.stderr)
        return 1

    peak_down = max(p[1] for p in points)
    peak_up = max(p[2] for p in points)
    threshold = args.threshold
    down_spans = periods(points, 0,
                         threshold if threshold else peak_down * DEFAULT_SHARE)
    up_spans = periods(points, 1,
                       threshold if threshold else peak_up * DEFAULT_SHARE)
    # Upload periods win ties: a download test also generates ACK traffic.
    down_spans = [s for s in down_spans
                  if not any(a <= s[0] <= b for a, b in up_spans)]

    print("peak throughput: %.0f Mbps down, %.0f Mbps up" % (peak_down, peak_up))
    print("detected %d download period(s), %d upload period(s)"
          % (len(down_spans), len(up_spans)))
    for label, spans in (("download", down_spans), ("upload", up_spans)):
        for a, b in spans:
            print("  %-9s %6.1fs - %6.1fs  (%.0fs)" % (label, a, b, b - a))
    print()

    header = "%-16s %-10s %6s %8s %8s %8s" % (
        "probe", "period", "n", "p50", "p95", "added")
    print(header)
    print("-" * len(header))
    for name, stats in probes.items():
        series = [(t, v) for t, v in stats["samples"]]
        idle = outside(series, down_spans + up_spans)
        base = idle.get("p95")
        rows = [("idle", idle)]
        if down_spans:
            rows.append(("download", stats_in(series, down_spans)))
        if up_spans:
            rows.append(("upload", stats_in(series, up_spans)))
        for label, s in rows:
            added = ("%8.2f" % (s["p95"] - base)
                     if (base is not None and s.get("p95") is not None
                         and label != "idle") else "%8s" % "-")
            print("%-16s %-10s %6d %8s %8s %s" % (
                name[:16], label, s["n"],
                "%.2f" % s["p50"] if s.get("p50") is not None else "-",
                "%.2f" % s["p95"] if s.get("p95") is not None else "-",
                added))
    return 0


if __name__ == "__main__":
    sys.exit(main())
