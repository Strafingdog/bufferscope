#!/usr/bin/env python3
"""netdiag - measure latency under load (bufferbloat) and tune router QoS.

Single file, standard library only, Python 3.9+.
"""
import argparse
import csv
import datetime
import glob
import io
import json
import math
import os
import platform
import random
import re
import shutil
import socket
import struct
import subprocess
import sys
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

VERSION = "1.0.0"
SCHEMA_VERSION = 1

Sample = Tuple[float, Optional[float]]

# --------------------------------------------------------------------------
# STATS
# --------------------------------------------------------------------------


def percentile(values: List[float], q: float) -> Optional[float]:
    """Nearest-rank percentile. q is a fraction in [0, 1]."""
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    idx = int(math.floor(q * (len(ordered) - 1)))
    idx = max(0, min(len(ordered) - 1, idx))
    return ordered[idx]


def summarize(samples: List[Sample], keep_samples: bool = False
              ) -> Dict[str, Any]:
    """Reduce (timestamp, rtt_or_None) samples to a statistics dict.

    With keep_samples, the series is carried alongside the statistics so a
    result can be re-analysed later - for example bucketing latency against a
    router's throughput timeline rather than the load generator's own phase
    claims. Off by default because at 50 Hz the series dwarfs the summary.
    """
    values = [v for _, v in samples if v is not None]
    lost = sum(1 for _, v in samples if v is None)
    out: Dict[str, Any] = {
        "n": len(values),
        "lost": lost,
        "loss_pct": (100.0 * lost / len(samples)) if samples else 0.0,
        "min": None, "mean": None, "p50": None, "p90": None,
        "p95": None, "p99": None, "max": None, "stddev": None,
        "jitter_consecutive": None, "jitter_rfc3550": None,
    }
    if keep_samples:
        out["samples"] = [[float(t), (float(v) if v is not None else None)]
                          for t, v in samples]
    if not values:
        return out

    mean = sum(values) / len(values)
    variance = sum((v - mean) ** 2 for v in values) / len(values)
    out.update({
        "min": min(values),
        "mean": mean,
        "p50": percentile(values, 0.50),
        "p90": percentile(values, 0.90),
        "p95": percentile(values, 0.95),
        "p99": percentile(values, 0.99),
        "max": max(values),
        "stddev": math.sqrt(variance),
    })

    if len(values) >= 2:
        deltas = [abs(values[i] - values[i - 1]) for i in range(1, len(values))]
        out["jitter_consecutive"] = sum(deltas) / len(deltas)
        jitter = 0.0
        for d in deltas:
            jitter += (d - jitter) / 16.0
        out["jitter_rfc3550"] = jitter
    return out


# --------------------------------------------------------------------------
# SIGNIFICANCE
#
# Loaded-latency measurements on a real line are extremely noisy - repeated
# runs of an identical configuration have been observed spanning 1.75 ms to
# 40.91 ms. Any comparison of two configurations must therefore carry error
# bars, or it will report noise as a finding.
# --------------------------------------------------------------------------

# Two-tailed t values for 95% confidence, by degrees of freedom.
_T95 = {
    1: 12.706, 2: 4.303, 3: 3.182, 4: 2.776, 5: 2.571, 6: 2.447, 7: 2.365,
    8: 2.306, 9: 2.262, 10: 2.228, 11: 2.201, 12: 2.179, 13: 2.160, 14: 2.145,
    15: 2.131, 16: 2.120, 17: 2.110, 18: 2.101, 19: 2.093, 20: 2.086,
    25: 2.060, 30: 2.042, 40: 2.021, 60: 2.000, 120: 1.980,
}


def t_critical(df: float) -> float:
    """Two-tailed 95% t value. Rounds down to the nearest tabulated df."""
    if df <= 0:
        return float("inf")
    keys = sorted(_T95)
    if df >= keys[-1]:
        return 1.960
    best = keys[0]
    for k in keys:
        if k <= df:
            best = k
        else:
            break
    return _T95[best]


def confidence_interval(values: List[float], ) -> Tuple[Optional[float],
                                                        Optional[float],
                                                        Optional[float]]:
    """Mean and 95% confidence interval. A single sample bounds nothing."""
    if not values:
        return (None, None, None)
    n = len(values)
    mean = sum(values) / n
    if n == 1:
        return (mean, float("-inf"), float("inf"))
    variance = sum((v - mean) ** 2 for v in values) / (n - 1)
    stderr = math.sqrt(variance / n)
    margin = t_critical(n - 1) * stderr
    return (mean, mean - margin, mean + margin)


def difference_ci(a: List[float], b: List[float]
                  ) -> Tuple[Optional[float], Optional[float],
                             Optional[float], bool]:
    """Welch confidence interval for mean(b) - mean(a).

    Significant when the interval excludes zero. Returns (diff, lo, hi, sig).
    """
    if len(a) < 2 or len(b) < 2:
        diff = ((sum(b) / len(b)) - (sum(a) / len(a))) if a and b else None
        return (diff, None, None, False)
    na, nb = len(a), len(b)
    ma, mb = sum(a) / na, sum(b) / nb
    va = sum((v - ma) ** 2 for v in a) / (na - 1)
    vb = sum((v - mb) ** 2 for v in b) / (nb - 1)
    se = math.sqrt(va / na + vb / nb)
    diff = mb - ma
    if se == 0:
        return (diff, diff, diff, diff != 0)
    # Welch-Satterthwaite degrees of freedom
    denom = ((va / na) ** 2 / (na - 1)) + ((vb / nb) ** 2 / (nb - 1))
    df = ((va / na + vb / nb) ** 2 / denom) if denom > 0 else (na + nb - 2)
    margin = t_critical(df) * se
    lo, hi = diff - margin, diff + margin
    return (diff, lo, hi, (lo > 0) or (hi < 0))


def _stats_block(values: List[float]) -> Dict[str, Any]:
    mean, lo, hi = confidence_interval(values)
    stdev = None
    if len(values) > 1:
        var = sum((v - mean) ** 2 for v in values) / (len(values) - 1)
        stdev = math.sqrt(var)
    return {
        "n": len(values), "mean": mean, "stdev": stdev,
        "min": min(values) if values else None,
        "max": max(values) if values else None,
        "ci95_lo": lo, "ci95_hi": hi,
        "values": values,
    }


def _median(values: List[float]) -> Optional[float]:
    if not values:
        return None
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2.0


def _drop_throughput_outliers(runs: List[Dict[str, Any]]
                              ) -> Tuple[List[Dict[str, Any]], List[str]]:
    """Remove runs far below the arm's median throughput.

    The saturation rule compares a run against its own reported speed, so a
    run where the entire line was slow passes it. Comparing against the arm's
    median catches that. The median is used rather than the mean precisely so
    one bad run cannot move the threshold.
    """
    if len(runs) < MIN_RUNS_FOR_OUTLIER_TEST:
        return runs, []

    def value(run, key):
        v = (run.get("speedtest") or {}).get(key)
        return float(v) if isinstance(v, (int, float)) else None

    kept: List[Dict[str, Any]] = []
    dropped: List[str] = []
    medians = {k: _median([v for v in (value(r, k) for r in runs) if v])
               for k in ("download_mbps", "upload_mbps")}
    for index, run in enumerate(runs, 1):
        reason = None
        for key, median in medians.items():
            observed = value(run, key)
            if median and observed is not None and observed < OUTLIER_FLOOR * median:
                reason = ("run %d: throughput outlier - %s %.0f Mbps against a "
                          "median of %.0f" % (index, key, observed, median))
                break
        if reason:
            dropped.append(reason)
        else:
            kept.append(run)
    return kept, dropped


def aggregate_runs(runs: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Combine repeated runs into per-probe added-latency statistics.

    Untrustworthy runs are excluded rather than averaged in - a run that never
    saturated the link would drag the mean toward "no bufferbloat".
    """
    exclusions: List[str] = []
    good = []
    for index, run in enumerate(runs, 1):
        validation = (run.get("validation") or {})
        if validation.get("trustworthy", True):
            good.append(run)
        else:
            reasons = validation.get("reasons") or ["failed validation"]
            exclusions.append("run %d: %s" % (index, reasons[0]))

    good, dropped = _drop_throughput_outliers(good)
    exclusions.extend(dropped)

    out: Dict[str, Any] = {"excluded_runs": len(runs) - len(good),
                           "included_runs": len(good),
                           "exclusions": exclusions}

    added: Dict[str, Dict[str, List[float]]] = {}
    for run in good:
        phases = run.get("phases") or {}
        idle = (phases.get("idle") or {}).get("probes") or {}
        for phase_name, phase in phases.items():
            if phase_name == "idle":
                continue
            for probe_name, stats in (phase.get("probes") or {}).items():
                base = (idle.get(probe_name) or {}).get("p95")
                loaded = stats.get("p95")
                if base is None or loaded is None:
                    continue
                added.setdefault(phase_name, {}).setdefault(
                    probe_name, []).append(loaded - base)
    for phase_name, probes in added.items():
        out[phase_name] = {name: _stats_block(vals)
                           for name, vals in probes.items()}

    speed: Dict[str, List[float]] = {}
    for run in good:
        for key in ("download_mbps", "upload_mbps"):
            value = (run.get("speedtest") or {}).get(key)
            if isinstance(value, (int, float)):
                speed.setdefault(key, []).append(float(value))
    out["speedtest"] = {k: _stats_block(v) for k, v in speed.items()}
    return out


# --------------------------------------------------------------------------
# GRADING AND VALIDATION
# --------------------------------------------------------------------------

GRADE_THRESHOLDS: List[Tuple[float, str]] = [
    (5.0, "A+"), (30.0, "A"), (60.0, "B"), (200.0, "C"),
]
MIN_PHASE_SAMPLES = 20
# A run below this fraction of the arm's median throughput was measured under
# different conditions - external traffic, a transient - not a different
# configuration. Averaging it in destroys the confidence interval.
OUTLIER_FLOOR = 0.5
MIN_RUNS_FOR_OUTLIER_TEST = 4
MAX_CONSECUTIVE_FAILURES = 2
SATURATION_FLOOR = 0.75
MAX_BASELINE_P95_MS = 100.0
LOADED_PHASES = ("download", "upload")
# Modes that generate their own load, and so must prove they saturated it.
LOAD_MODES = ("bufferbloat", "classes")


def grade(added_ms: Optional[float]) -> str:
    """Grade a latency increase over baseline. Lower is better."""
    if added_ms is None:
        return "n/a"
    for limit, label in GRADE_THRESHOLDS:
        if added_ms < limit:
            return label
    return "D"


def validate_run(result: Dict[str, Any]) -> Dict[str, Any]:
    """Decide whether a run's numbers can be trusted. Never raises.

    Load-generating modes are held to saturation and baseline rules. Passive
    modes are not: `probe` and `monitor` deliberately create no load, so a busy
    line is their finding rather than a fault in the measurement.
    """
    reasons: List[str] = []
    phases = result.get("phases") or {}
    generates_load = result.get("mode", "bufferbloat") in LOAD_MODES

    if generates_load:
        if not any(name in phases for name in LOADED_PHASES):
            detail = result.get("speedtest_error")
            reasons.append(
                "no loaded phase recorded: %s"
                % (detail if detail else
                   "the speedtest produced no download or upload events, so "
                   "nothing was measured under load")
            )
        idle_probes = (phases.get("idle") or {}).get("probes") or {}
        for name, stats in idle_probes.items():
            p95 = stats.get("p95")
            if p95 is not None and p95 > MAX_BASELINE_P95_MS:
                reasons.append(
                    "baseline already congested: idle p95 %.1f ms on %s exceeds "
                    "%.0f ms" % (p95, name, MAX_BASELINE_P95_MS)
                )

    for phase_name in LOADED_PHASES:
        phase = phases.get(phase_name)
        if not phase:
            continue
        # The router observes every client; the host NIC sees only this PC.
        # Prefer the router figure whenever it is available.
        nic = phase.get("throughput_mbps_router")
        if nic is None:
            nic = phase.get("throughput_mbps_nic")
        reported = phase.get("throughput_mbps_reported")
        if nic is None or reported is None:
            reasons.append(
                "cannot verify saturation for %s: throughput missing" % phase_name
            )
        elif reported > 0 and nic < SATURATION_FLOOR * reported:
            reasons.append(
                "link not saturated during %s: NIC saw %.0f Mbps vs %.0f Mbps reported"
                % (phase_name, nic, reported)
            )

    for phase_name, phase in phases.items():
        for probe_name, stats in (phase.get("probes") or {}).items():
            replies = stats.get("n", 0)
            attempts = replies + stats.get("lost", 0)
            if replies == 0 and attempts > 0:
                # Distinct from "too few samples": the probe ran fine, the
                # target never answered. Reporting that as missing data would
                # hide a dead target behind a sampling complaint.
                reasons.append(
                    "target unreachable in %s/%s: %d attempts, no replies"
                    % (phase_name, probe_name, attempts)
                )
            elif attempts < MIN_PHASE_SAMPLES:
                reasons.append(
                    "too few samples in %s/%s: %d < %d"
                    % (phase_name, probe_name, attempts, MIN_PHASE_SAMPLES)
                )

    return {"trustworthy": not reasons, "reasons": reasons}


# --------------------------------------------------------------------------
# PLATFORM PARSERS  (pure functions - no I/O, fully unit tested)
# --------------------------------------------------------------------------

_IPV4_RE = re.compile(r"^\d{1,3}(?:\.\d{1,3}){3}$")


def _rows(text: str) -> List[Dict[str, str]]:
    try:
        return list(csv.DictReader(io.StringIO(text)))
    except Exception:
        return []


def parse_gateway_windows(text: str) -> Optional[str]:
    """Parse CSV from Get-NetRoute; prefer the lowest RouteMetric."""
    best: Optional[Tuple[int, str]] = None
    for row in _rows(text):
        hop = (row.get("NextHop") or "").strip()
        if not _IPV4_RE.match(hop) or hop == "0.0.0.0":
            continue
        try:
            metric = int((row.get("RouteMetric") or "0").strip())
        except ValueError:
            metric = 0
        if best is None or metric < best[0]:
            best = (metric, hop)
    return best[1] if best else None


def parse_gateway_linux(text: str) -> Optional[str]:
    """Parse /proc/net/route; the gateway is little-endian hex."""
    for line in text.splitlines()[1:]:
        parts = line.split()
        if len(parts) < 3 or parts[1] != "00000000":
            continue
        hexgw = parts[2]
        if len(hexgw) != 8:
            continue
        try:
            octets = [int(hexgw[i:i + 2], 16) for i in (6, 4, 2, 0)]
        except ValueError:
            continue
        if any(o for o in octets):
            return ".".join(str(o) for o in octets)
    return None


def parse_gateway_macos(text: str) -> Optional[str]:
    """Parse netstat -rn output for the default route."""
    for line in text.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0] == "default" and _IPV4_RE.match(parts[1]):
            return parts[1]
    return None


def parse_nic_windows(text: str) -> Dict[str, Tuple[int, int]]:
    out: Dict[str, Tuple[int, int]] = {}
    for row in _rows(text):
        name = (row.get("Name") or "").strip()
        try:
            rx = int((row.get("ReceivedBytes") or "").strip())
            tx = int((row.get("SentBytes") or "").strip())
        except ValueError:
            continue
        if name:
            out[name] = (rx, tx)
    return out


def parse_nic_linux(text: str) -> Dict[str, Tuple[int, int]]:
    out: Dict[str, Tuple[int, int]] = {}
    for line in text.splitlines():
        if ":" not in line:
            continue
        name, _, rest = line.partition(":")
        fields = rest.split()
        if len(fields) < 9:
            continue
        try:
            out[name.strip()] = (int(fields[0]), int(fields[8]))
        except ValueError:
            continue
    return out


def parse_nic_macos(text: str) -> Dict[str, Tuple[int, int]]:
    """Parse netstat -ib.

    Columns end with: Ipkts Ierrs Ibytes Opkts Oerrs Obytes [Coll] [Drop].
    Indexing from the right keeps this correct whether or not the optional
    Address column is present, which counting from the left does not.
    """
    out: Dict[str, Tuple[int, int]] = {}
    for line in text.splitlines():
        fields = line.split()
        if len(fields) < 7 or fields[0] == "Name":
            continue
        name = fields[0]
        if name in out:
            continue
        # (rx, tx) offsets for trailing-column variants: Coll, Coll+Drop, none.
        for rx_idx, tx_idx in ((-5, -2), (-6, -3), (-4, -1)):
            try:
                rx_field, tx_field = fields[rx_idx], fields[tx_idx]
            except IndexError:
                continue
            if rx_field.isdigit() and tx_field.isdigit():
                out[name] = (int(rx_field), int(tx_field))
                break
    return out


# --------------------------------------------------------------------------
# PLATFORM DISCOVERY  (the only layer that touches the OS)
# --------------------------------------------------------------------------

_PS = ["powershell", "-NoProfile", "-NonInteractive", "-Command"]

_WIN_ROUTE_CMD = _PS + [
    "Get-NetRoute -DestinationPrefix 0.0.0.0/0 | "
    "Select-Object NextHop,RouteMetric | ConvertTo-Csv -NoTypeInformation"
]
_WIN_NIC_CMD = _PS + [
    "Get-NetAdapterStatistics | Select-Object Name,ReceivedBytes,SentBytes | "
    "ConvertTo-Csv -NoTypeInformation"
]


def run_cmd(args: List[str], timeout: float = 10.0) -> Optional[str]:
    """Run a command and return stdout, or None on any failure."""
    try:
        proc = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    except Exception:
        return None
    if proc.returncode != 0 and not proc.stdout:
        return None
    return proc.stdout


def detect_gateway() -> Optional[str]:
    """Find the default gateway. Returns None rather than raising."""
    system = platform.system()
    try:
        if system == "Windows":
            out = run_cmd(_WIN_ROUTE_CMD)
            return parse_gateway_windows(out) if out else None
        if system == "Linux":
            out = run_cmd(["cat", "/proc/net/route"])
            if out is None:
                try:
                    with open("/proc/net/route", "r") as handle:
                        out = handle.read()
                except OSError:
                    return None
            return parse_gateway_linux(out) if out else None
        if system == "Darwin":
            out = run_cmd(["netstat", "-rn"])
            return parse_gateway_macos(out) if out else None
    except Exception:
        return None
    return None


def read_nic_counters() -> Dict[str, Tuple[int, int]]:
    """Map interface name to (rx_bytes, tx_bytes). Empty dict on failure."""
    system = platform.system()
    try:
        if system == "Windows":
            out = run_cmd(_WIN_NIC_CMD)
            return parse_nic_windows(out) if out else {}
        if system == "Linux":
            try:
                with open("/proc/net/dev", "r") as handle:
                    return parse_nic_linux(handle.read())
            except OSError:
                return {}
        if system == "Darwin":
            out = run_cmd(["netstat", "-ib"])
            return parse_nic_macos(out) if out else {}
    except Exception:
        return {}
    return {}


def find_speedtest() -> Optional[str]:
    """Locate the Ookla Speedtest CLI without hardcoding a user path."""
    found = shutil.which("speedtest")
    if found:
        return found
    patterns = [
        os.path.expandvars(
            r"%LOCALAPPDATA%\Microsoft\WinGet\Packages"
            r"\Ookla.Speedtest.CLI*\speedtest.exe"
        ),
        r"C:\Program Files\Speedtest\speedtest.exe",
        "/usr/bin/speedtest",
        "/usr/local/bin/speedtest",
        "/opt/homebrew/bin/speedtest",
    ]
    for pattern in patterns:
        for match in sorted(glob.glob(pattern)):
            if os.path.isfile(match):
                return match
    return None


def speedtest_version(binary: str) -> Optional[str]:
    out = run_cmd([binary, "--version"], timeout=15.0)
    if not out or not out.strip():
        return None
    return out.strip().splitlines()[0]


def has_icmp_privilege() -> bool:
    """True if a raw ICMP socket can be opened (root/admin on most systems)."""
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_RAW, socket.IPPROTO_ICMP)
        sock.close()
        return True
    except Exception:
        return False


def redact_path(path: Optional[str]) -> Optional[str]:
    """Replace the user's home directory with ~ in a path.

    Result files are meant to be shared - the README says to paste env output
    into bug reports - so they must not carry an operator's username.
    """
    if not path:
        return path
    home = os.path.expanduser("~")
    if home and path.lower().startswith(home.lower()):
        return "~" + path[len(home):]
    return path


def collect_env() -> Dict[str, Any]:
    """Everything about this machine that affects a measurement."""
    binary = find_speedtest()
    return {
        "os": "%s %s" % (platform.system(), platform.release()),
        "python": sys.version.split()[0],
        "netdiag_version": VERSION,
        "schema_version": SCHEMA_VERSION,
        "gateway": detect_gateway(),
        "interfaces": {k: {"rx_bytes": v[0], "tx_bytes": v[1]}
                       for k, v in read_nic_counters().items()},
        "speedtest_path": redact_path(binary),
        "speedtest_version": speedtest_version(binary) if binary else None,
        "icmp_available": has_icmp_privilege(),
    }


# --------------------------------------------------------------------------
# PROBES
# --------------------------------------------------------------------------


_DSCP_NAMES = {"default": 0, "ef": 46}
for _i in range(8):
    _DSCP_NAMES["cs%d" % _i] = _i * 8
for _cls in (1, 2, 3, 4):
    for _drop in (1, 2, 3):
        _DSCP_NAMES["af%d%d" % (_cls, _drop)] = _cls * 8 + _drop * 2


def dscp_value(name: str) -> int:
    """Resolve a DSCP name (ef, cs5, af41, default) or number to 0-63."""
    key = (name or "").strip().lower()
    if key in _DSCP_NAMES:
        return _DSCP_NAMES[key]
    try:
        value = int(key)
    except ValueError:
        raise ValueError("unknown DSCP %r" % name)
    if not 0 <= value <= 63:
        raise ValueError("DSCP out of range: %r" % name)
    return value


def dscp_to_tos(dscp: int) -> int:
    """DSCP occupies the top 6 bits of the IPv4 ToS byte."""
    return (dscp & 0x3F) << 2


def _apply_dscp(sock: "socket.socket", dscp: Optional[int]) -> None:
    if dscp is None:
        return
    try:
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_TOS, dscp_to_tos(dscp))
    except Exception:
        pass  # some platforms refuse; the probe is still valid, just unmarked


def _dscp_suffix(dscp: Optional[int]) -> str:
    """Probe names carry their marking, so results are self-describing."""
    if dscp is None:
        return ""
    for name, value in _DSCP_NAMES.items():
        if value == dscp and name != "default":
            return "@%s" % name
    return "@dscp%d" % dscp


class Probe:
    """Base probe. Subclasses set `name` and implement `sample`."""

    name = "probe"
    dscp: Optional[int] = None

    def sample(self) -> Optional[float]:
        raise NotImplementedError


class TcpProbe(Probe):
    """Measures the TCP handshake round trip. Needs no privileges."""

    def __init__(self, host: str, port: int = 443, timeout: float = 2.0,
                 dscp: Optional[int] = None):
        self.host = host
        self.port = port
        self.timeout = timeout
        self.dscp = dscp
        self.name = "tcp:%s:%d%s" % (host, port, _dscp_suffix(dscp))

    def sample(self) -> Optional[float]:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        _apply_dscp(sock, self.dscp)
        start = time.perf_counter()
        try:
            sock.connect((self.host, self.port))
            return (time.perf_counter() - start) * 1000.0
        except Exception:
            return None
        finally:
            try:
                sock.close()
            except Exception:
                pass


class UdpDnsProbe(Probe):
    """Measures a DNS query round trip over UDP. Needs no privileges."""

    def __init__(self, host: str, port: int = 53, timeout: float = 2.0,
                 qname: str = "example.com", dscp: Optional[int] = None):
        self.host = host
        self.port = port
        self.timeout = timeout
        self.qname = qname
        self.dscp = dscp
        self.name = "dns:%s%s" % (host, _dscp_suffix(dscp))

    def _query(self, txid: int) -> bytes:
        header = struct.pack(">HHHHHH", txid, 0x0100, 1, 0, 0, 0)
        parts = b"".join(
            bytes([len(label)]) + label.encode("ascii")
            for label in self.qname.split(".")
        )
        return header + parts + b"\x00" + struct.pack(">HH", 1, 1)

    def sample(self) -> Optional[float]:
        txid = random.randint(0, 0xFFFF)
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.settimeout(self.timeout)
        _apply_dscp(sock, self.dscp)
        start = time.perf_counter()
        try:
            sock.sendto(self._query(txid), (self.host, self.port))
            while True:
                data, _ = sock.recvfrom(2048)
                if len(data) >= 2 and struct.unpack(">H", data[:2])[0] == txid:
                    return (time.perf_counter() - start) * 1000.0
        except Exception:
            return None
        finally:
            try:
                sock.close()
            except Exception:
                pass


class StunProbe(Probe):
    """STUN binding request over UDP.

    Exists so that UDP port ranges used by conferencing apps (Teams and Zoom
    use 3478-3481 for STUN/TURN) can be probed against a real responder,
    making a QoS rule on those ports verifiable rather than assumed.
    """

    MAGIC = b"\x21\x12\xa4\x42"

    def __init__(self, host: str, port: int = 3478, timeout: float = 2.0,
                 dscp: Optional[int] = None):
        self.host = host
        self.port = port
        self.timeout = timeout
        self.dscp = dscp
        self.name = "stun:%s:%d%s" % (host, port, _dscp_suffix(dscp))

    def _request(self, txid: bytes) -> bytes:
        return struct.pack(">HH", 0x0001, 0) + self.MAGIC + txid

    def _matches(self, data: bytes, txid: bytes) -> bool:
        return (len(data) >= 20 and data[4:8] == self.MAGIC
                and data[8:20] == txid)

    def sample(self) -> Optional[float]:
        txid = os.urandom(12)
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.settimeout(self.timeout)
        _apply_dscp(sock, self.dscp)
        start = time.perf_counter()
        try:
            sock.sendto(self._request(txid), (self.host, self.port))
            while True:
                data, _ = sock.recvfrom(2048)
                if self._matches(data, txid):
                    return (time.perf_counter() - start) * 1000.0
        except Exception:
            return None
        finally:
            try:
                sock.close()
            except Exception:
                pass


class IcmpProbe(Probe):
    """Raw-socket ICMP echo. Requires root on Linux/macOS, admin on Windows."""

    def __init__(self, host: str, timeout: float = 2.0,
                 dscp: Optional[int] = None):
        self.host = host
        self.timeout = timeout
        self.dscp = dscp
        self.name = "icmp:%s%s" % (host, _dscp_suffix(dscp))
        self._seq = 0

    @staticmethod
    def _checksum(data: bytes) -> int:
        if len(data) % 2:
            data += b"\x00"
        total = 0
        for i in range(0, len(data), 2):
            total += (data[i] << 8) + data[i + 1]
        total = (total >> 16) + (total & 0xFFFF)
        total += total >> 16
        return ~total & 0xFFFF

    def sample(self) -> Optional[float]:
        self._seq = (self._seq + 1) & 0xFFFF
        ident = os.getpid() & 0xFFFF
        header = struct.pack(">BBHHH", 8, 0, 0, ident, self._seq)
        payload = b"netdiag-" + bytes(24)
        checksum = self._checksum(header + payload)
        packet = struct.pack(">BBHHH", 8, 0, checksum, ident, self._seq) + payload
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_RAW, socket.IPPROTO_ICMP)
        except Exception:
            return None
        sock.settimeout(self.timeout)
        start = time.perf_counter()
        try:
            sock.sendto(packet, (self.host, 0))
            while True:
                data, _ = sock.recvfrom(1024)
                if len(data) >= 28 and data[20] == 0:
                    got_id, got_seq = struct.unpack(">HH", data[24:28])
                    if got_id == ident and got_seq == self._seq:
                        return (time.perf_counter() - start) * 1000.0
        except Exception:
            return None
        finally:
            try:
                sock.close()
            except Exception:
                pass


class ProbeRunner:
    """Runs probes on background threads at a fixed interval."""

    def __init__(self, probes: List[Probe], interval_ms: int = 20):
        self.probes = probes
        self.interval = max(1, interval_ms) / 1000.0
        self._stop = threading.Event()
        self._threads: List[threading.Thread] = []
        self._lock = threading.Lock()
        self._samples: Dict[str, List[Sample]] = {p.name: [] for p in probes}
        self._t0 = 0.0

    def _loop(self, probe: Probe) -> None:
        while not self._stop.is_set():
            try:
                value = probe.sample()
            except Exception:
                value = None
            stamp = time.perf_counter() - self._t0
            with self._lock:
                self._samples[probe.name].append((stamp, value))
            self._stop.wait(self.interval)

    def start(self) -> None:
        self._t0 = time.perf_counter()
        for probe in self.probes:
            thread = threading.Thread(target=self._loop, args=(probe,), daemon=True)
            thread.start()
            self._threads.append(thread)

    def stop(self) -> None:
        self._stop.set()
        for thread in self._threads:
            thread.join(timeout=5.0)
        self._threads = []

    def origin(self) -> float:
        """perf_counter value corresponding to sample timestamp 0.0."""
        return self._t0

    def samples(self) -> Dict[str, List[Sample]]:
        with self._lock:
            return {k: list(v) for k, v in self._samples.items()}


_PROBE_KINDS = {
    "tcp": (TcpProbe, 443),
    "dns": (UdpDnsProbe, 53),
    "stun": (StunProbe, 3478),
    "icmp": (IcmpProbe, None),
}


def parse_probe_spec(spec: str) -> Probe:
    """Parse 'kind:host[:port][@dscp=NAME][#label]' into a Probe.

    Examples:
      tcp:1.1.1.1:853            control probe on a port no QoS rule matches
      dns:1.1.1.1@dscp=ef        DNS query marked Expedited Forwarding
      stun:stun.l.google.com     STUN on 3478, hits conferencing port rules
    """
    text = (spec or "").strip()
    if not text:
        raise ValueError("empty probe spec")

    label = None
    if "#" in text:
        text, label = text.split("#", 1)
        label = label.strip() or None

    dscp = None
    if "@" in text:
        text, marking = text.split("@", 1)
        key, _, value = marking.partition("=")
        if key.strip().lower() != "dscp" or not value:
            raise ValueError("bad marking in probe spec: %r" % spec)
        dscp = dscp_value(value)

    parts = text.split(":")
    kind = parts[0].strip().lower()
    if kind not in _PROBE_KINDS:
        raise ValueError("unknown probe kind %r in %r" % (kind, spec))
    cls, default_port = _PROBE_KINDS[kind]

    host = parts[1].strip() if len(parts) > 1 else ""
    if not host:
        raise ValueError("missing host in probe spec: %r" % spec)

    port = default_port
    if len(parts) > 2 and parts[2].strip():
        try:
            port = int(parts[2])
        except ValueError:
            raise ValueError("bad port in probe spec: %r" % spec)
        if not 1 <= port <= 65535:
            raise ValueError("port out of range in probe spec: %r" % spec)
    if len(parts) > 3:
        raise ValueError("too many fields in probe spec: %r" % spec)

    probe = (cls(host, dscp=dscp) if default_port is None
             else cls(host, port=port, dscp=dscp))
    if label:
        probe.name = label
    return probe


def build_probes(targets: List[str], gateway: Optional[str],
                 use_icmp: bool) -> List[Probe]:
    """Gateway probes isolate the LAN path; target probes measure the internet."""
    probes: List[Probe] = []
    if gateway:
        probes.append(TcpProbe(gateway, 80))
        if use_icmp:
            probes.append(IcmpProbe(gateway))
    for target in targets:
        probes.append(UdpDnsProbe(target))
        probes.append(TcpProbe(target, 443))
        if use_icmp:
            probes.append(IcmpProbe(target))
    return probes


# --------------------------------------------------------------------------
# RUNNER
# --------------------------------------------------------------------------

PHASE_ORDER = ("download", "upload")


def parse_jsonl_event(line: str) -> Optional[Dict[str, Any]]:
    """Parse one line of Ookla --format=jsonl output. None if it is not an event."""
    line = (line or "").strip()
    if not line:
        return None
    try:
        obj = json.loads(line)
    except Exception:
        return None
    return obj if isinstance(obj, dict) and "type" in obj else None


def phases_from_events(events: List[Tuple[float, Dict[str, Any]]],
                       end_time: float) -> List[Tuple[str, float, float]]:
    """Derive (name, start, end) windows from the first event of each type.

    Ookla emits many progress events per phase; the first one marks the
    transition. A phase ends when the next phase starts, or at the result event.
    """
    starts: Dict[str, float] = {}
    for stamp, event in events:
        kind = event.get("type")
        if kind in PHASE_ORDER and kind not in starts:
            starts[kind] = stamp
    result_at = next((s for s, e in events if e.get("type") == "result"), end_time)
    present = [k for k in PHASE_ORDER if k in starts]
    phases: List[Tuple[str, float, float]] = []
    for index, name in enumerate(present):
        start = starts[name]
        end = starts[present[index + 1]] if index + 1 < len(present) else result_at
        phases.append((name, start, end))
    return phases


def bucket_samples(samples: Dict[str, List[Sample]],
                   phases: List[Tuple[str, float, float]]
                   ) -> Dict[str, Dict[str, List[Sample]]]:
    """Assign each sample to a phase. Start inclusive, end exclusive."""
    out: Dict[str, Dict[str, List[Sample]]] = {
        name: {probe: [] for probe in samples} for name, _, _ in phases
    }
    for probe, series in samples.items():
        for stamp, value in series:
            for name, start, end in phases:
                if start <= stamp < end:
                    out[name][probe].append((stamp, value))
                    break
    return out


def extract_result(events: List[Tuple[float, Dict[str, Any]]]) -> Dict[str, Any]:
    """Pull the final numbers out of the result event. Ookla reports bytes/sec."""
    final = next((e for _, e in reversed(events) if e.get("type") == "result"), None)
    if not final:
        return {
            "download_mbps": None, "upload_mbps": None,
            "idle_latency_ms": None, "packet_loss_pct": None,
            "server": None, "result_url": None,
        }

    def mbps(section: str) -> Optional[float]:
        node = final.get(section) or {}
        bandwidth = node.get("bandwidth")
        return (bandwidth * 8 / 1e6) if isinstance(bandwidth, (int, float)) else None

    return {
        "download_mbps": mbps("download"),
        "upload_mbps": mbps("upload"),
        "idle_latency_ms": (final.get("ping") or {}).get("latency"),
        "packet_loss_pct": final.get("packetLoss"),
        "server": final.get("server"),
        "result_url": (final.get("result") or {}).get("url"),
    }


def nic_delta_mbps(before: Dict[str, Tuple[int, int]],
                   after: Dict[str, Tuple[int, int]],
                   seconds: float) -> Tuple[float, float]:
    """Throughput on the busiest interface between two counter snapshots."""
    if seconds <= 0:
        return (0.0, 0.0)
    best_rx = 0
    best_tx = 0
    for name, (rx_after, tx_after) in after.items():
        rx_before, tx_before = before.get(name, (rx_after, tx_after))
        best_rx = max(best_rx, rx_after - rx_before)
        best_tx = max(best_tx, tx_after - tx_before)
    return (best_rx * 8 / 1e6 / seconds, best_tx * 8 / 1e6 / seconds)


NicSnapshot = Tuple[float, Dict[str, Tuple[int, int]]]

MIN_COUNTER_TRANSITIONS = 3
# A window must span at least this many refresh intervals to be characterised.
# Set from measurement rather than intuition: a 7 s window at a 2 s refresh is
# 3.5 intervals, and that combination undercounted upload throughput by 18%.
MIN_REFRESH_INTERVALS_PER_WINDOW = 5


def estimate_refresh_interval(samples: List["NicSnapshot"]) -> Optional[float]:
    """Measure how often a counter source actually updates.

    Routers refresh SNMP counters on their own schedule - observed values
    range from sub-second to several seconds across vendors. Assuming one
    device's rate would silently produce wrong numbers on another, so the
    interval is measured from the observed transitions instead.

    Returns the median interval between counter changes, or None when there
    are too few transitions to tell.
    """
    if len(samples) < 3:
        return None
    changed_at = []
    previous = None
    for stamp, counters in samples:
        if previous is not None and counters != previous:
            changed_at.append(stamp)
        previous = counters
    if len(changed_at) < 2:
        return None
    gaps = [b - a for a, b in zip(changed_at, changed_at[1:]) if b > a]
    if not gaps:
        return None
    gaps.sort()
    mid = len(gaps) // 2
    return gaps[mid] if len(gaps) % 2 else (gaps[mid - 1] + gaps[mid]) / 2.0


def window_is_usable(seconds: float, refresh: Optional[float]) -> bool:
    """Can a window of this length be characterised at this refresh rate?"""
    if not refresh or refresh <= 0:
        return True  # unknown: do not block on an unmeasured property
    return seconds >= MIN_REFRESH_INTERVALS_PER_WINDOW * refresh


def coarse_throughput_in_window(samples: List["NicSnapshot"], start: float,
                                end: float,
                                min_transitions: int = MIN_COUNTER_TRANSITIONS
                                ) -> Tuple[Optional[float], Optional[float], int]:
    """Throughput from counters that refresh in steps rather than continuously.

    Router SNMP counters are updated on the device's own schedule, and that
    schedule varies by vendor - anywhere from sub-second to several seconds.
    Integrating naively between the first and last sample in a window then
    straddles partial quanta and undercounts. Instead, measure only between
    the first and last observed counter *transitions*, which are real points
    in time. Use estimate_refresh_interval to learn the device's rate.

    Returns (down_mbps, up_mbps, transition_count). Below min_transitions the
    window is too short to characterise and both rates are None - the caller
    should fall back to another source rather than publish a bad figure.
    """
    inside = [s for s in samples if start <= s[0] <= end]
    if len(inside) < 2:
        return (None, None, 0)

    transitions: List[NicSnapshot] = []
    previous: Optional[Dict[str, Tuple[int, int]]] = None
    for stamp, counters in inside:
        if previous is None or counters != previous:
            transitions.append((stamp, counters))
            previous = counters
    count = max(0, len(transitions) - 1)
    if count < min_transitions:
        return (None, None, count)

    first_t, first = transitions[0]
    last_t, last = transitions[-1]
    seconds = last_t - first_t
    if seconds <= 0:
        return (None, None, count)
    best_rx = 0
    best_tx = 0
    for name, (rx_last, tx_last) in last.items():
        if name not in first:
            continue
        rx_first, tx_first = first[name]
        best_rx = max(best_rx, max(0, rx_last - rx_first))
        best_tx = max(best_tx, max(0, tx_last - tx_first))
    return (best_rx * 8 / 1e6 / seconds, best_tx * 8 / 1e6 / seconds, count)


class NicSampler:
    """Samples interface byte counters on a background thread.

    Reading counters can be slow (on Windows it shells out to PowerShell and
    takes over a second). Doing that inline in the event loop both blocks
    event processing and shifts the measured byte window out of alignment
    with the phase it is meant to describe, which understates throughput.
    Sampling on a thread and integrating afterwards avoids both problems.
    """

    def __init__(self, origin: float, interval: float = 0.5):
        self.origin = origin
        self.interval = max(0.01, interval)
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self._samples: List[NicSnapshot] = []

    def _loop(self) -> None:
        while not self._stop.is_set():
            counters = read_nic_counters()
            stamp = time.perf_counter() - self.origin
            if counters:
                with self._lock:
                    self._samples.append((stamp, counters))
            self._stop.wait(self.interval)

    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=10.0)
            self._thread = None

    def samples(self) -> List[NicSnapshot]:
        with self._lock:
            return list(self._samples)


def throughput_in_window(samples: List[NicSnapshot], start: float, end: float
                         ) -> Tuple[Optional[float], Optional[float]]:
    """Integrate counter snapshots over [start, end] into (down, up) Mbps.

    Returns (None, None) when the window contains fewer than two samples,
    which is honest about not knowing rather than inventing a figure.
    """
    inside = [s for s in samples if start <= s[0] <= end]
    if len(inside) < 2:
        return (None, None)
    first_t, first = inside[0]
    last_t, last = inside[-1]
    seconds = last_t - first_t
    if seconds <= 0:
        return (None, None)
    best_rx = 0
    best_tx = 0
    for name, (rx_last, tx_last) in last.items():
        if name not in first:
            continue
        rx_first, tx_first = first[name]
        # Counters can wrap or reset; never report negative throughput.
        best_rx = max(best_rx, max(0, rx_last - rx_first))
        best_tx = max(best_tx, max(0, tx_last - tx_first))
    return (best_rx * 8 / 1e6 / seconds, best_tx * 8 / 1e6 / seconds)


def observed_throughput(samples: List[NicSnapshot]) -> Optional[Dict[str, Any]]:
    """Average throughput across a passive observation window.

    A latency measurement with no record of what traffic was flowing invites
    inventing a scenario to explain it. Passive runs record this so the
    conditions are evidence rather than assumption.
    """
    if len(samples) < 2:
        return None
    start, end = samples[0][0], samples[-1][0]
    down, up = throughput_in_window(samples, start, end)
    if down is None or up is None:
        return None
    return {"down_mbps": down, "up_mbps": up,
            "seconds": end - start, "samples": len(samples)}


# --------------------------------------------------------------------------
# SNMP  (v2c, read-only, stdlib BER)
#
# A host-based tool sees only its own NIC. The router is the only device that
# observes every client, so household traffic can only be measured by asking
# it. SNMPv2c is used deliberately: v3 would require AES, which is not in the
# standard library, to encrypt byte counters on a trusted LAN.
# --------------------------------------------------------------------------

PDU_GET = 0xA0
PDU_GETNEXT = 0xA1
PDU_RESPONSE = 0xA2

OID_SYSDESCR = "1.3.6.1.2.1.1.1.0"
OID_IF_DESCR = "1.3.6.1.2.1.2.2.1.2"
OID_IF_NAME = "1.3.6.1.2.1.31.1.1.1.1"
# High-capacity (64-bit) octet counters. The 32-bit originals wrap roughly
# every 34 seconds at gigabit, which would silently produce nonsense.
OID_IF_HC_IN = "1.3.6.1.2.1.31.1.1.1.6"
OID_IF_HC_OUT = "1.3.6.1.2.1.31.1.1.1.10"

_UNSIGNED_TAGS = (0x41, 0x42, 0x43, 0x46)
_EXCEPTION_TAGS = {0x80: "noSuchObject", 0x81: "noSuchInstance",
                   0x82: "endOfMibView"}


def _ber_len(n: int) -> bytes:
    if n < 0x80:
        return bytes([n])
    raw = n.to_bytes((n.bit_length() + 7) // 8, "big")
    return bytes([0x80 | len(raw)]) + raw


def _ber_tlv(tag: int, value: bytes) -> bytes:
    return bytes([tag]) + _ber_len(len(value)) + value


def _ber_int(value: int) -> bytes:
    if value == 0:
        return _ber_tlv(0x02, bytes([0]))
    length = (value.bit_length() + 8) // 8
    return _ber_tlv(0x02, value.to_bytes(length, "big", signed=True))


def _ber_oid(oid: str) -> bytes:
    arcs = [int(a) for a in oid.split(".")]
    if len(arcs) < 2:
        raise ValueError("OID needs at least two arcs: %r" % oid)
    out = bytearray([arcs[0] * 40 + arcs[1]])
    for arc in arcs[2:]:
        if arc < 0x80:
            out.append(arc)
            continue
        chunks = []
        while arc:
            chunks.insert(0, arc & 0x7F)
            arc >>= 7
        for c in chunks[:-1]:
            out.append(c | 0x80)
        out.append(chunks[-1])
    return _ber_tlv(0x06, bytes(out))


def _ber_read(data: bytes, index: int) -> Tuple[int, bytes, int]:
    """Read one TLV. Returns (tag, value_bytes, next_index)."""
    if index + 2 > len(data):
        raise ValueError("truncated TLV")
    tag = data[index]
    first = data[index + 1]
    index += 2
    if first == 0x80:
        raise ValueError("indefinite length not supported")
    if first < 0x80:
        length = first
    else:
        count = first & 0x7F
        if index + count > len(data):
            raise ValueError("truncated length")
        length = int.from_bytes(data[index:index + count], "big")
        index += count
    if index + length > len(data):
        raise ValueError("truncated value")
    return tag, data[index:index + length], index + length


def _decode_oid(raw: bytes) -> str:
    if not raw:
        return ""
    arcs = [raw[0] // 40, raw[0] % 40]
    value = 0
    for byte in raw[1:]:
        value = (value << 7) | (byte & 0x7F)
        if not byte & 0x80:
            arcs.append(value)
            value = 0
    return ".".join(str(a) for a in arcs)


def _decode_value(tag: int, raw: bytes) -> Any:
    if tag == 0x02:
        return int.from_bytes(raw, "big", signed=True) if raw else 0
    if tag in _UNSIGNED_TAGS:
        return int.from_bytes(raw, "big") if raw else 0
    if tag == 0x04:
        return raw.decode("utf-8", "replace")
    if tag == 0x06:
        return _decode_oid(raw)
    if tag == 0x40:
        return ".".join(str(b) for b in raw)
    if tag == 0x05:
        return None
    if tag in _EXCEPTION_TAGS:
        return _EXCEPTION_TAGS[tag]
    return raw


def snmp_build_request(community: str, oids: List[str], request_id: int,
                       next_request: bool = False) -> bytes:
    varbinds = b"".join(
        _ber_tlv(0x30, _ber_oid(o) + _ber_tlv(0x05, b"")) for o in oids)
    pdu = _ber_tlv(PDU_GETNEXT if next_request else PDU_GET,
                   _ber_int(request_id) + _ber_int(0) + _ber_int(0)
                   + _ber_tlv(0x30, varbinds))
    return _ber_tlv(0x30, _ber_int(1)
                    + _ber_tlv(0x04, community.encode("utf-8")) + pdu)


def snmp_parse_message(data: bytes) -> Optional[Dict[str, Any]]:
    """Parse an SNMP message. Returns None on anything malformed."""
    try:
        _, body, _ = _ber_read(data, 0)
        tag, raw, i = _ber_read(body, 0)
        version = _decode_value(tag, raw)
        tag, raw, i = _ber_read(body, i)
        community = _decode_value(tag, raw)
        pdu_tag, pdu, _ = _ber_read(body, i)

        tag, raw, j = _ber_read(pdu, 0)
        request_id = _decode_value(tag, raw)
        tag, raw, j = _ber_read(pdu, j)
        error_status = _decode_value(tag, raw)
        tag, raw, j = _ber_read(pdu, j)
        error_index = _decode_value(tag, raw)
        _, vb_block, _ = _ber_read(pdu, j)

        varbinds = []
        k = 0
        while k < len(vb_block):
            _, one, k = _ber_read(vb_block, k)
            tag, raw, m = _ber_read(one, 0)
            oid = _decode_oid(raw)
            tag, raw, _ = _ber_read(one, m)
            varbinds.append((oid, _decode_value(tag, raw)))
        return {"version": version, "community": community,
                "pdu_tag": pdu_tag, "request_id": request_id,
                "error_status": error_status, "error_index": error_index,
                "varbinds": varbinds}
    except Exception:
        return None


def _summarise_speedtest_error(text: str) -> str:
    """Condense speedtest stderr to the line that explains the failure."""
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    for line in lines:
        cleaned = re.sub(r"^\[[^\]]*\]\s*", "", line)       # timestamp
        cleaned = re.sub(r"^\[[a-z]+\]\s*", "", cleaned)     # severity
        if cleaned and not cleaned.lower().startswith("speedtest by ookla"):
            return cleaned.rstrip(":")
    return lines[0] if lines else "speedtest produced no output"


def _oid_in_subtree(oid: str, base: str) -> bool:
    """Arc-aware prefix test. A string prefix would treat .60 as inside .6."""
    a = oid.split(".")
    b = base.split(".")
    return len(a) > len(b) and a[:len(b)] == b


def snmp_query(host: str, community: str, oids: List[str],
               next_request: bool = False, timeout: float = 2.0,
               port: int = 161) -> Optional[Dict[str, Any]]:
    """One SNMP round trip. Returns None on timeout or malformed reply."""
    request_id = int.from_bytes(os.urandom(3), "big")
    message = snmp_build_request(community, oids, request_id, next_request)
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(timeout)
    try:
        sock.sendto(message, (host, port))
        for _ in range(4):  # ignore stray datagrams from other requests
            data, _addr = sock.recvfrom(8192)
            parsed = snmp_parse_message(data)
            if parsed and parsed.get("request_id") == request_id:
                return parsed
        return None
    except Exception:
        return None
    finally:
        try:
            sock.close()
        except Exception:
            pass


def snmp_walk(host: str, community: str, base_oid: str,
              timeout: float = 2.0, port: int = 161,
              limit: int = 512) -> List[Tuple[str, Any]]:
    """GETNEXT until the subtree ends. Returns [(oid, value)]."""
    out: List[Tuple[str, Any]] = []
    current = base_oid
    for _ in range(limit):
        parsed = snmp_query(host, community, [current], next_request=True,
                            timeout=timeout, port=port)
        if not parsed or parsed.get("error_status") or not parsed["varbinds"]:
            break
        oid, value = parsed["varbinds"][0]
        if value in _EXCEPTION_TAGS.values() or not _oid_in_subtree(oid, base_oid):
            break
        out.append((oid, value))
        current = oid
    return out


def snmp_interfaces(host: str, community: str, timeout: float = 2.0,
                    port: int = 161) -> Dict[int, str]:
    """Map interface index to a human name, preferring ifName over ifDescr."""
    names: Dict[int, str] = {}
    for base in (OID_IF_DESCR, OID_IF_NAME):
        for oid, value in snmp_walk(host, community, base, timeout, port):
            try:
                index = int(oid.split(".")[-1])
            except ValueError:
                continue
            if isinstance(value, str) and value:
                names[index] = value
    return names


def snmp_counters(host: str, community: str, indexes: List[int],
                  timeout: float = 2.0, port: int = 161
                  ) -> Dict[int, Tuple[int, int]]:
    """Read 64-bit (in, out) octet counters for the given interface indexes."""
    out: Dict[int, Tuple[int, int]] = {}
    for index in indexes:
        parsed = snmp_query(
            host, community,
            ["%s.%d" % (OID_IF_HC_IN, index), "%s.%d" % (OID_IF_HC_OUT, index)],
            timeout=timeout, port=port)
        if not parsed or parsed.get("error_status"):
            continue
        values = [v for _, v in parsed["varbinds"]]
        if len(values) == 2 and all(isinstance(v, int) for v in values):
            out[index] = (values[0], values[1])
    return out


class RouterSampler:
    """Polls router interface counters on a background thread.

    Shares the timestamp origin and sample shape used by NicSampler, so
    throughput_in_window operates on either source unchanged.
    """

    def __init__(self, host: str, community: str, indexes: List[int],
                 names: Dict[int, str], origin: float, interval: float = 1.0,
                 port: int = 161):
        self.host = host
        self.community = community
        self.indexes = indexes
        self.names = names
        self.origin = origin
        self.interval = max(0.2, interval)
        self.port = port
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self._samples: List[NicSnapshot] = []
        self.failures = 0

    def _loop(self) -> None:
        while not self._stop.is_set():
            counters = snmp_counters(self.host, self.community, self.indexes,
                                     port=self.port)
            stamp = time.perf_counter() - self.origin
            if counters:
                snapshot = {self.names.get(i, "if%d" % i): v
                            for i, v in counters.items()}
                with self._lock:
                    self._samples.append((stamp, snapshot))
            else:
                self.failures += 1
            self._stop.wait(self.interval)

    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=10.0)
            self._thread = None

    def samples(self) -> List[NicSnapshot]:
        with self._lock:
            return list(self._samples)


# --------------------------------------------------------------------------
# REPORTERS
# --------------------------------------------------------------------------


def build_result(env: Dict[str, Any],
                 speedtest_result: Dict[str, Any],
                 phase_buckets: Dict[str, Dict[str, List[Sample]]],
                 idle_samples: Dict[str, List[Sample]],
                 throughput: Dict[str, Tuple[float, float]],
                 started_at: str,
                 mode: str = "bufferbloat",
                 keep_samples: bool = False) -> Dict[str, Any]:
    """Assemble the canonical JSON result document."""
    reported = {
        "download": speedtest_result.get("download_mbps"),
        "upload": speedtest_result.get("upload_mbps"),
    }
    phases: Dict[str, Any] = {
        "idle": {
            "throughput_mbps_nic": None,
            "throughput_mbps_reported": None,
            "probes": {name: summarize(s, keep_samples)
                       for name, s in idle_samples.items()},
        }
    }
    for phase_name, probes in phase_buckets.items():
        down, up = throughput.get(phase_name, (None, None))
        nic = up if phase_name == "upload" else down
        phases[phase_name] = {
            "throughput_mbps_nic": nic,
            "throughput_mbps_reported": reported.get(phase_name),
            "probes": {name: summarize(s, keep_samples)
                       for name, s in probes.items()},
        }

    bloat: Dict[str, Any] = {}
    worst: Optional[float] = None
    for phase_name, phase in phases.items():
        if phase_name == "idle":
            continue
        bloat[phase_name] = {}
        for probe_name, stats in phase["probes"].items():
            baseline = (phases["idle"]["probes"].get(probe_name) or {}).get("p95")
            loaded = stats.get("p95")
            added = None
            if baseline is not None and loaded is not None:
                added = loaded - baseline
                worst = added if worst is None else max(worst, added)
            bloat[phase_name][probe_name] = {
                "added_p95_ms": added, "grade": grade(added),
            }
    bloat["worst_added_p95_ms"] = worst
    bloat["overall_grade"] = grade(worst)

    result = {
        "schema_version": SCHEMA_VERSION,
        "netdiag_version": VERSION,
        "started_at": started_at,
        "mode": mode,
        "env": env,
        "speedtest": speedtest_result,
        "phases": phases,
        "bufferbloat": bloat,
    }
    result["validation"] = validate_run(result)
    return result


def _fmt(value: Optional[float], width: int = 8, places: int = 2) -> str:
    if value is None:
        return "%*s" % (width, "-")
    return "%*.*f" % (width, places, value)


def render_human(result: Dict[str, Any]) -> str:
    lines: List[str] = []
    speed = result.get("speedtest") or {}
    lines.append("netdiag %s   %s" % (result.get("netdiag_version"),
                                      result.get("started_at")))
    server = speed.get("server") or {}
    if server:
        lines.append("server: %s, %s (id %s)" % (
            server.get("name"), server.get("location"), server.get("id")))
    lines.append("")
    lines.append("Download: %s Mbps    Upload: %s Mbps    Idle latency: %s ms" % (
        _fmt(speed.get("download_mbps"), 1, 1),
        _fmt(speed.get("upload_mbps"), 1, 1),
        _fmt(speed.get("idle_latency_ms"), 1, 2)))
    lines.append("")
    header = "%-30s %8s %8s %8s %8s %7s" % (
        "phase / probe", "p50", "p95", "p99", "max", "loss%")
    lines.append(header)
    lines.append("-" * len(header))
    for phase_name, phase in (result.get("phases") or {}).items():
        nic = phase.get("throughput_mbps_nic")
        suffix = "" if nic is None else "   [NIC %.0f Mbps]" % nic
        lines.append("%s%s" % (phase_name, suffix))
        for probe_name, stats in (phase.get("probes") or {}).items():
            lines.append("  %-28s %s %s %s %s %s" % (
                probe_name[:28],
                _fmt(stats.get("p50")), _fmt(stats.get("p95")),
                _fmt(stats.get("p99")), _fmt(stats.get("max")),
                _fmt(stats.get("loss_pct"), 7, 1)))
    bloat = result.get("bufferbloat") or {}
    lines.append("")
    lines.append("Bufferbloat: worst added p95 %s ms  ->  grade %s" % (
        _fmt(bloat.get("worst_added_p95_ms"), 1, 2), bloat.get("overall_grade")))
    validation = result.get("validation") or {}
    if not validation.get("trustworthy", True):
        lines.append("")
        lines.append("*** RESULT NOT TRUSTWORTHY ***")
        for reason in validation.get("reasons", []):
            lines.append("  - %s" % reason)
    return "\n".join(lines)


def _ci(stats: Dict[str, Any]) -> str:
    lo, hi = stats.get("ci95_lo"), stats.get("ci95_hi")
    if lo is None or hi is None or lo == float("-inf"):
        return "     (n<2)"
    return "[%6.1f,%6.1f]" % (lo, hi)


def render_aggregate(doc: Dict[str, Any]) -> str:
    """Report repeated runs with error bars rather than a single number."""
    agg = doc.get("aggregate") or {}
    lines = ["netdiag %s   %s   repeats: %d (excluded %d)" % (
        doc.get("netdiag_version"), doc.get("started_at"),
        agg.get("included_runs", 0), agg.get("excluded_runs", 0))]
    for reason in (agg.get("exclusions") or []):
        lines.append("    excluded %s" % reason)
    speed = agg.get("speedtest") or {}
    for key in ("download_mbps", "upload_mbps"):
        stats = speed.get(key)
        if stats:
            lines.append("  %-14s mean %8.1f Mbps  95%% CI %s  n=%d" % (
                key, stats["mean"], _ci(stats), stats["n"]))
    lines.append("")
    header = "%-20s %5s %9s %9s %9s  %s" % (
        "phase / probe", "n", "mean", "stdev", "range", "95% CI of added p95")
    lines.append(header)
    lines.append("-" * len(header))
    for phase_name in ("download", "upload"):
        phase = agg.get(phase_name)
        if not isinstance(phase, dict):
            continue
        lines.append("%s:" % phase_name)
        for probe, stats in sorted(phase.items(),
                                   key=lambda kv: -(kv[1].get("mean") or 0)):
            lines.append("  %-18s n=%-3d %9.2f %9.2f %4.1f-%-4.1f  %s" % (
                probe[:18], stats["n"], stats["mean"] or 0.0,
                stats["stdev"] or 0.0, stats["min"] or 0.0, stats["max"] or 0.0,
                _ci(stats)))
    return "\n".join(lines)


def render_compare_aggregate(a: Dict[str, Any], b: Dict[str, Any]) -> str:
    """Compare two repeated-run documents, testing each metric for significance."""
    aa, ba = a.get("aggregate") or {}, b.get("aggregate") or {}
    lines = ["%-26s %10s %10s %10s  %s" % (
        "metric", "A mean", "B mean", "diff", "95% CI of diff / verdict"),
        "-" * 84]

    def row(label: str, va: List[float], vb: List[float], higher_better: bool):
        if not va or not vb:
            lines.append("%-26s %10s %10s %10s  %s" % (label, "-", "-", "-", "n/a"))
            return
        diff, lo, hi, sig = difference_ci(va, vb)
        ma, mb = sum(va) / len(va), sum(vb) / len(vb)
        if not sig:
            verdict = "not distinguishable from noise"
            span = "" if lo is None else "[%+.1f, %+.1f]  " % (lo, hi)
        else:
            improved = diff > 0 if higher_better else diff < 0
            verdict = "SIGNIFICANT - %s" % ("better" if improved else "worse")
            span = "[%+.1f, %+.1f]  " % (lo, hi)
        lines.append("%-26s %10.2f %10.2f %+10.2f  %s%s" % (
            label, ma, mb, diff, span, verdict))

    for key in ("download_mbps", "upload_mbps"):
        row(key,
            ((aa.get("speedtest") or {}).get(key) or {}).get("values") or [],
            ((ba.get("speedtest") or {}).get(key) or {}).get("values") or [],
            higher_better=True)

    for phase in ("download", "upload"):
        pa, pb = aa.get(phase), ba.get(phase)
        if not isinstance(pa, dict) or not isinstance(pb, dict):
            continue
        for probe in sorted(set(pa) & set(pb)):
            row("%s/%s" % (phase, probe[:14]),
                pa[probe].get("values") or [], pb[probe].get("values") or [],
                higher_better=False)
    return "\n".join(lines)


def render_compare(a: Dict[str, Any], b: Dict[str, Any]) -> str:
    """Diff two runs. Used to accept or reject a router setting change."""
    def pull(doc: Dict[str, Any]) -> Dict[str, Optional[float]]:
        speed = doc.get("speedtest") or {}
        bloat = doc.get("bufferbloat") or {}
        return {
            "download_mbps": speed.get("download_mbps"),
            "upload_mbps": speed.get("upload_mbps"),
            "worst_added_p95_ms": bloat.get("worst_added_p95_ms"),
        }

    higher_is_better = {"download_mbps": True, "upload_mbps": True,
                        "worst_added_p95_ms": False}
    left, right = pull(a), pull(b)
    lines = ["%-24s %12s %12s %12s  %s" % ("metric", "A", "B", "delta", "verdict"),
             "-" * 72]
    for key in ("download_mbps", "upload_mbps", "worst_added_p95_ms"):
        x, y = left[key], right[key]
        if x is None or y is None:
            lines.append("%-24s %12s %12s %12s  %s" % (key, x, y, "-", "n/a"))
            continue
        delta = y - x
        if abs(delta) < 1e-9:
            verdict = "unchanged"
        else:
            improved = delta > 0 if higher_is_better[key] else delta < 0
            verdict = "better" if improved else "worse"
        lines.append("%-24s %12.2f %12.2f %+12.2f  %s" % (key, x, y, delta, verdict))
    lines.append("")
    lines.append("grade: %s  ->  %s" % (
        (a.get("bufferbloat") or {}).get("overall_grade"),
        (b.get("bufferbloat") or {}).get("overall_grade")))
    return "\n".join(lines)


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_UNTRUSTWORTHY = 2

EMPTY_SPEEDTEST = {
    "download_mbps": None, "upload_mbps": None, "idle_latency_ms": None,
    "packet_loss_pct": None, "server": None, "result_url": None,
}


def _add_common(parser: "argparse.ArgumentParser") -> None:
    parser.add_argument("--json", action="store_true",
                        help="machine-readable output to stdout")
    parser.add_argument("--out", metavar="FILE",
                        help="write JSON result to FILE")
    parser.add_argument("--targets", default="1.1.1.1",
                        help="comma-separated internet probe targets")
    parser.add_argument("--no-gateway", action="store_true",
                        help="do not probe the default gateway")
    parser.add_argument("--probe-interval", type=int, default=20, metavar="MS",
                        help="probe sampling interval in ms (default 20)")
    parser.add_argument("--no-icmp", action="store_true",
                        help="skip ICMP even when privileged")
    parser.add_argument("--quiet", action="store_true",
                        help="suppress progress output")
    parser.add_argument("--raw", action="store_true",
                        help="include the full latency sample series in the "
                             "JSON result (large; needed to re-analyse a run)")
    parser.add_argument("--router-snmp", metavar="HOST",
                        help="poll this router over SNMP for whole-household "
                             "traffic, not just this PC")
    parser.add_argument("--snmp-community", metavar="STR",
                        help="SNMP community (default: $NETDIAG_SNMP_COMMUNITY, "
                             "else 'public')")
    parser.add_argument("--snmp-port", type=int, default=161)
    parser.add_argument("--snmp-interface", metavar="NAME",
                        help="router interface to measure (default: the busiest)")
    parser.add_argument("--probe", action="append", metavar="SPEC",
                        help="probe spec, repeatable, replaces the default set: "
                             "kind:host[:port][@dscp=NAME][#label] where kind is "
                             "tcp, dns, stun or icmp")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="netdiag",
        description="Measure latency under load (bufferbloat) and tune router QoS.")
    parser.add_argument("--version", action="store_true",
                        help="print tool and schema version")
    subparsers = parser.add_subparsers(dest="command")

    bufferbloat = subparsers.add_parser(
        "bufferbloat", help="idle baseline + Ookla run + phase-correlated latency")
    _add_common(bufferbloat)
    bufferbloat.add_argument("--baseline-secs", type=int, default=10)
    bufferbloat.add_argument("--server-id", help="pin the Ookla server id")
    bufferbloat.add_argument("--repeat", type=int, default=1, metavar="N",
                             help="run N times and report mean, stdev and 95%% CI")

    classes = subparsers.add_parser(
        "classes", help="verify QoS classification rules under load")
    _add_common(classes)
    classes.add_argument("--baseline-secs", type=int, default=10)
    classes.add_argument("--server-id", help="pin the Ookla server id")
    classes.add_argument("--repeat", type=int, default=1, metavar="N",
                             help="run N times and report mean, stdev and 95%% CI")

    probe = subparsers.add_parser("probe", help="latency only, no load generated")
    _add_common(probe)
    probe.add_argument("--duration", type=int, default=30)

    monitor = subparsers.add_parser("monitor", help="long-running latency monitor")
    _add_common(monitor)
    monitor.add_argument("--duration", type=int, default=3600)
    monitor.add_argument("--interval", type=int, default=60)

    router = subparsers.add_parser(
        "router", help="list router interfaces and counters over SNMP")
    router.add_argument("host", nargs="?", default=None,
                        help="router address (default: detected gateway)")
    router.add_argument("--snmp-community", metavar="STR")
    router.add_argument("--snmp-port", type=int, default=161)
    router.add_argument("--json", action="store_true")

    env_parser = subparsers.add_parser("env", help="dump detected environment")
    env_parser.add_argument("--json", action="store_true")

    compare = subparsers.add_parser("compare", help="diff two saved runs")
    compare.add_argument("file_a")
    compare.add_argument("file_b")
    return parser


def _emit(result: Dict[str, Any], args: "argparse.Namespace", text: str) -> None:
    if getattr(args, "out", None):
        with open(args.out, "w") as handle:
            json.dump(result, handle, indent=2)
    if getattr(args, "json", False):
        print(json.dumps(result, indent=2))
    else:
        print(text)


def snmp_community(args: "argparse.Namespace") -> str:
    """Flag beats environment beats the SNMP default. Never logged."""
    return (getattr(args, "snmp_community", None)
            or os.environ.get("NETDIAG_SNMP_COMMUNITY")
            or "public")


def pick_interface(counters: Dict[int, Tuple[int, int]],
                   names: Dict[int, str],
                   wanted: Optional[str]) -> Optional[int]:
    """Choose the interface to measure.

    With no preference, pick the one carrying the most traffic - on a home
    router that is the WAN, and it avoids asking the user for an index.
    """
    if wanted:
        for index, name in names.items():
            if name.lower() == wanted.lower() and index in counters:
                return index
        try:
            index = int(wanted)
            return index if index in counters else None
        except ValueError:
            return None
    if not counters:
        return None
    return max(counters, key=lambda i: counters[i][0] + counters[i][1])


def start_router_sampler(args: "argparse.Namespace", origin: float):
    """Return a started RouterSampler, or None with a warning on stderr."""
    host = getattr(args, "router_snmp", None)
    if not host:
        return None
    community = snmp_community(args)
    port = getattr(args, "snmp_port", 161)
    names = snmp_interfaces(host, community, port=port)
    if not names:
        print("warning: no SNMP reply from %s - continuing without router data"
              % host, file=sys.stderr)
        return None
    counters = snmp_counters(host, community, sorted(names), port=port)
    index = pick_interface(counters, names, getattr(args, "snmp_interface", None))
    if index is None:
        print("warning: no usable router interface - continuing without router "
              "data", file=sys.stderr)
        return None
    sampler = RouterSampler(host, community, [index], names, origin,
                            interval=1.0, port=port)
    sampler.start()
    if not getattr(args, "quiet", False):
        print("  router: polling %s (%s)" % (names.get(index, index), host),
              file=sys.stderr)
    return sampler


def router_throughput(sampler, keep_samples: bool = False
                      ) -> Optional[Dict[str, Any]]:
    if sampler is None:
        return None
    samples = sampler.samples()
    if len(samples) < 2:
        return None
    start, end = samples[0][0], samples[-1][0]
    down, up, transitions = coarse_throughput_in_window(samples, start, end)
    if down is None or up is None:
        return {"down_mbps": None, "up_mbps": None, "seconds": end - start,
                "sample_count": len(samples), "counter_transitions": transitions,
                "interface": next(iter(samples[-1][1]), None), "source": "snmp",
                "note": "window too short for this router's counter refresh rate"}
    name = next(iter(samples[-1][1]), None)
    out = {"down_mbps": down, "up_mbps": up, "seconds": end - start,
           "sample_count": len(samples), "counter_transitions": transitions,
           "refresh_interval_s": estimate_refresh_interval(samples),
           "interface": name, "source": "snmp"}
    if keep_samples:
        out["samples"] = [[float(t), {k: list(v) for k, v in c.items()}]
                          for t, c in samples]
    return out


def probe_refresh_interval(host: str, community: str, index: int,
                           seconds: float = 8.0, port: int = 161
                           ) -> Optional[float]:
    """Poll one interface quickly to learn how often its counters update."""
    samples: List[NicSnapshot] = []
    origin = time.perf_counter()
    while time.perf_counter() - origin < seconds:
        counters = snmp_counters(host, community, [index], port=port)
        if index in counters:
            samples.append((time.perf_counter() - origin,
                            {"if": counters[index]}))
        time.sleep(0.2)
    return estimate_refresh_interval(samples)


def cmd_router(args: "argparse.Namespace") -> int:
    host = args.host or detect_gateway()
    if not host:
        print("error: no router address given and no gateway detected",
              file=sys.stderr)
        return EXIT_ERROR
    community = snmp_community(args)
    names = snmp_interfaces(host, community, port=args.snmp_port)
    if not names:
        print("error: no SNMP reply from %s.\n"
              "  - is the SNMP agent enabled on the router?\n"
              "  - is the community string correct? (set "
              "NETDIAG_SNMP_COMMUNITY, or pass --snmp-community)\n"
              "  - does the router restrict SNMP to specific manager IPs?"
              % host, file=sys.stderr)
        return EXIT_ERROR
    counters = snmp_counters(host, community, sorted(names),
                             port=args.snmp_port)
    rows = []
    for index in sorted(names):
        octets_in, octets_out = counters.get(index, (None, None))
        rows.append({"index": index, "name": names[index],
                     "in_octets": octets_in, "out_octets": octets_out})
    busiest = pick_interface(counters, names, None)
    if args.json:
        print(json.dumps({"host": host, "interfaces": rows,
                          "busiest_index": busiest}, indent=2))
        return EXIT_OK
    print("router %s" % host)
    header = "%-6s %-24s %18s %18s" % ("index", "interface", "in octets",
                                       "out octets")
    print(header)
    print("-" * len(header))
    for row in rows:
        mark = " <- busiest" if row["index"] == busiest else ""
        print("%-6d %-24s %18s %18s%s" % (
            row["index"], row["name"][:24],
            "-" if row["in_octets"] is None else row["in_octets"],
            "-" if row["out_octets"] is None else row["out_octets"], mark))
    print("")
    if busiest is not None:
        refresh = probe_refresh_interval(host, community, busiest,
                                         port=args.snmp_port)
        if refresh:
            print("counter refresh: ~%.1f s  (measured)" % refresh)
            print("  usable for windows of at least %.0f s - a speedtest phase "
                  "is ~7 s" % (MIN_REFRESH_INTERVALS_PER_WINDOW * refresh))
        else:
            print("counter refresh: could not measure (too little traffic?)")
        print("")
    print("Use --router-snmp %s to include household traffic in a measurement."
          % host)
    return EXIT_OK


def cmd_env(args: "argparse.Namespace") -> int:
    env = collect_env()
    if args.json:
        print(json.dumps(env, indent=2))
        return EXIT_OK
    for key, value in env.items():
        if key == "interfaces":
            print("%-20s %d found" % (key, len(value)))
            for name, counters in value.items():
                print("  %-30s rx %15d  tx %15d"
                      % (name[:30], counters["rx_bytes"], counters["tx_bytes"]))
        else:
            print("%-20s %s" % (key, value))
    return EXIT_OK


def cmd_compare(args: "argparse.Namespace") -> int:
    docs = []
    for path in (args.file_a, args.file_b):
        try:
            with open(path, "r") as handle:
                docs.append(json.load(handle))
        except Exception as exc:
            print("error: cannot read %s: %s" % (path, exc), file=sys.stderr)
            return EXIT_ERROR
    if docs[0].get("schema_version") != docs[1].get("schema_version"):
        print("error: results use incompatible schema versions", file=sys.stderr)
        return EXIT_ERROR
    for doc in docs:
        # Raw runs are stored, so aggregation rules can be improved without
        # invalidating measurements already taken.
        if doc.get("runs"):
            doc["aggregate"] = aggregate_runs(doc["runs"])
    if docs[0].get("aggregate") and docs[1].get("aggregate"):
        print(render_compare_aggregate(docs[0], docs[1]))
    else:
        print(render_compare(docs[0], docs[1]))
        if docs[0].get("aggregate") or docs[1].get("aggregate"):
            print("\nnote: only one input has repeats; comparing single runs "
                  "cannot distinguish a real change from noise.")
    return EXIT_OK


def run_speedtest(binary: str, server_id: Optional[str], on_event,
                  origin: float,
                  error_sink: Optional[List[str]] = None
                  ) -> List[Tuple[float, Dict[str, Any]]]:
    """Run Ookla and stream its jsonl events, timestamped against `origin`.

    `origin` is a perf_counter value supplied by the caller - normally
    ProbeRunner.origin(). Sharing one clock base is what lets probe samples
    and NIC snapshots be bucketed into phases without offset arithmetic.

    This function deliberately does no NIC sampling: anything slow in this
    loop delays event timestamps and corrupts phase boundaries.
    """
    cmd = [binary, "--format=jsonl", "--accept-license", "--accept-gdpr"]
    if server_id:
        cmd += ["--server-id", str(server_id)]

    events: List[Tuple[float, Dict[str, Any]]] = []
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True, bufsize=1)
    try:
        for line in proc.stdout:
            event = parse_jsonl_event(line)
            if not event:
                continue
            stamp = time.perf_counter() - origin
            events.append((stamp, event))
            on_event(stamp, event)
    finally:
        try:
            proc.wait(timeout=30)
        except Exception:
            proc.kill()
    # Discarding stderr turns a clear "429 Limit reached" into a mute
    # "nothing was measured". Keep it and let the caller report it.
    if error_sink is not None:
        try:
            text = (proc.stderr.read() or "").strip() if proc.stderr else ""
        except Exception:
            text = ""
        if text:
            error_sink.append(_summarise_speedtest_error(text))
        elif proc.returncode:
            error_sink.append("speedtest exited with code %s" % proc.returncode)
    return events


CLASS_PRESET: List[Tuple[str, str]] = [
    # A TCP-connect control against a shared public endpoint proved unusable:
    # tcp:1.1.1.1:853 produced 1030 ms outliers no other probe saw, and its p95
    # drifted between arms, which is fatal in a control. STUN on 19302 is
    # stable and matches no typical conferencing rule.
    ("stun:stun.l.google.com:19302#control-others",
     "default queue (control - no rule should match UDP 19302)"),
    ("dns:1.1.1.1#dns-rule", "priority - DNS (TCP/UDP 53)"),
    ("stun:stun.l.google.com:3478#teams-rule",
     "priority - conferencing (UDP 3478-3481)"),
    ("tcp:github.com:22#ssh-rule", "priority - interactive (TCP 22)"),
    ("dns:1.1.1.1@dscp=ef#ef-rule", "priority - EF-marked (DSCP 46)"),
]


def select_probes(args: "argparse.Namespace", env: Dict[str, Any]) -> List[Probe]:
    """Explicit --probe wins; the classes preset next; otherwise the defaults."""
    specs = list(getattr(args, "probe", None) or [])
    if not specs and getattr(args, "command", None) == "classes":
        specs = [spec for spec, _ in CLASS_PRESET]
    if specs:
        return [parse_probe_spec(s) for s in specs]
    targets = [t.strip() for t in args.targets.split(",") if t.strip()]
    gateway = None if args.no_gateway else env.get("gateway")
    use_icmp = (not args.no_icmp) and bool(env.get("icmp_available"))
    return build_probes(targets, gateway, use_icmp)


def _probe_setup(args: "argparse.Namespace", env: Dict[str, Any]
                 ) -> Optional[List[Probe]]:
    return select_probes(args, env) or None


def render_classes(result: Dict[str, Any]) -> str:
    """Per-probe added latency, annotated with the class each should land in."""
    expected = {}
    for spec, note in CLASS_PRESET:
        label = spec.split("#", 1)[1] if "#" in spec else spec
        expected[label] = note
    phases = result.get("phases") or {}
    idle = (phases.get("idle") or {}).get("probes") or {}
    lines = ["%-18s %9s %9s %9s  %s" % ("probe", "idle p95", "load p95",
                                        "added", "expected class"),
             "-" * 78]
    for phase_name in ("download", "upload"):
        phase = phases.get(phase_name)
        if not phase:
            continue
        lines.append("%s:" % phase_name)
        rows = []
        for name, stats in (phase.get("probes") or {}).items():
            base = (idle.get(name) or {}).get("p95")
            loaded = stats.get("p95")
            added = (loaded - base) if (base is not None and loaded is not None) else None
            rows.append((added if added is not None else -1e9, name, base, loaded, added))
        for _, name, base, loaded, added in sorted(rows, reverse=True):
            lines.append("  %-16s %s %s %s  %s" % (
                name[:16], _fmt(base, 9), _fmt(loaded, 9), _fmt(added, 9),
                expected.get(name, "-")))
    return "\n".join(lines)


def _measure_once(args: "argparse.Namespace"):
    """One measurement. Returns a result dict, or an int exit code on error."""
    started_at = datetime.datetime.now().isoformat(timespec="seconds")
    env = collect_env()
    try:
        probes = _probe_setup(args, env)
    except ValueError as exc:
        print("error: %s" % exc, file=sys.stderr)
        return EXIT_ERROR
    if not probes:
        print("error: no probe targets available", file=sys.stderr)
        return EXIT_ERROR

    if args.command == "probe":
        runner = ProbeRunner(probes, args.probe_interval)
        runner.start()
        nic = NicSampler(runner.origin(), interval=1.0)
        nic.start()
        router = start_router_sampler(args, runner.origin())
        try:
            time.sleep(max(0, args.duration))
        except KeyboardInterrupt:
            pass
        nic.stop()
        if router:
            router.stop()
        runner.stop()
        result = build_result(env, dict(EMPTY_SPEEDTEST), {}, runner.samples(),
                              {}, started_at, mode="probe",
                              keep_samples=getattr(args, "raw", False))
        result["observed_throughput"] = observed_throughput(nic.samples())
        result["router_throughput"] = router_throughput(
            router, keep_samples=getattr(args, "raw", False))
        _emit(result, args, render_human(result))
        return EXIT_OK if result["validation"]["trustworthy"] else EXIT_UNTRUSTWORTHY

    binary = find_speedtest()
    if not binary:
        print("error: Ookla Speedtest CLI not found. Install it from "
              "https://www.speedtest.net/apps/cli and ensure 'speedtest' is "
              "on PATH.", file=sys.stderr)
        return EXIT_ERROR

    runner = ProbeRunner(probes, args.probe_interval)
    runner.start()
    nic = NicSampler(runner.origin(), interval=0.5)
    nic.start()
    router = start_router_sampler(args, runner.origin())
    baseline_secs = max(0, getattr(args, "baseline_secs", 0))
    if baseline_secs:
        time.sleep(baseline_secs)

    seen_phases = set()

    def on_event(stamp: float, event: Dict[str, Any]) -> None:
        kind = event.get("type")
        if not args.quiet and kind in PHASE_ORDER and kind not in seen_phases:
            seen_phases.add(kind)
            print("  ... %s phase" % kind, file=sys.stderr)

    speedtest_errors: List[str] = []
    events = run_speedtest(binary, getattr(args, "server_id", None),
                           on_event, runner.origin(),
                           error_sink=speedtest_errors)
    nic.stop()
    if router:
        router.stop()
    runner.stop()

    all_samples = runner.samples()
    idle = {name: [s for s in series if s[0] < baseline_secs]
            for name, series in all_samples.items()}
    phases = phases_from_events(
        events, end_time=max((t for t, _ in events), default=0.0))
    buckets = bucket_samples(all_samples, phases)
    nic_samples = nic.samples()
    throughput = {name: throughput_in_window(nic_samples, start, end)
                  for name, start, end in phases}

    result = build_result(env, extract_result(events), buckets, idle,
                          throughput, started_at, mode=args.command,
                          keep_samples=getattr(args, "raw", False))
    if speedtest_errors:
        result["speedtest_error"] = speedtest_errors[0]
        result["validation"] = validate_run(result)
    if router:
        router_samples = router.samples()
        for name, start, end in phases:
            down, up, transitions = coarse_throughput_in_window(
                router_samples, start, end)
            value = up if name == "upload" else down
            if name not in result["phases"]:
                continue
            if value is None:
                # Speedtest phases are ~7 s; a router refreshing its counters
                # every couple of seconds cannot characterise that. Record why,
                # and let validation fall back to the host NIC.
                result["phases"][name]["router_note"] = (
                    "phase too short for router counter granularity "
                    "(%d transitions)" % transitions)
            else:
                result["phases"][name]["throughput_mbps_router"] = value
        result["router_throughput"] = router_throughput(
            router, keep_samples=getattr(args, "raw", False))
    return result


def cmd_measure(args: "argparse.Namespace") -> int:
    repeat = max(1, getattr(args, "repeat", 1) or 1)
    if args.command == "probe" or repeat == 1:
        result = _measure_once(args)
        if isinstance(result, int):
            return result
        text = render_human(result)
        if args.command == "classes":
            text += "\n\nCLASS VERIFICATION\n" + render_classes(result)
        _emit(result, args, text)
        return EXIT_OK if result["validation"]["trustworthy"] else EXIT_UNTRUSTWORTHY

    started_at = datetime.datetime.now().isoformat(timespec="seconds")
    runs: List[Dict[str, Any]] = []
    consecutive_failures = 0
    for index in range(repeat):
        if not args.quiet:
            print("run %d/%d" % (index + 1, repeat), file=sys.stderr)
        result = _measure_once(args)
        if isinstance(result, int):
            return result
        runs.append(result)

        # Repeating a run that fails identically costs minutes and produces
        # nothing. Rate limiting and a dead server both look like this.
        if result.get("speedtest_error") or not any(
                name in (result.get("phases") or {}) for name in LOADED_PHASES):
            consecutive_failures += 1
        else:
            consecutive_failures = 0
        if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
            detail = result.get("speedtest_error") or "no loaded phase recorded"
            print("error: aborting after %d consecutive failed runs: %s"
                  % (consecutive_failures, detail), file=sys.stderr)
            return EXIT_ERROR

    agg = aggregate_runs(runs)
    doc = {
        "schema_version": SCHEMA_VERSION,
        "netdiag_version": VERSION,
        "started_at": started_at,
        "env": runs[0].get("env") if runs else {},
        "repeat": {"n": repeat},
        "runs": runs,
        "aggregate": agg,
    }
    reasons = []
    if agg.get("excluded_runs"):
        reasons.append("%d of %d runs were untrustworthy and excluded"
                       % (agg["excluded_runs"], repeat))
    if agg.get("included_runs", 0) < 2:
        reasons.append("fewer than 2 usable runs: no error bars possible")
    doc["validation"] = {"trustworthy": not reasons, "reasons": reasons}
    _emit(doc, args, render_aggregate(doc))
    return EXIT_OK if doc["validation"]["trustworthy"] else EXIT_UNTRUSTWORTHY


def cmd_monitor(args: "argparse.Namespace") -> int:
    """Probe continuously, summarising each interval. Generates no load."""
    started_at = datetime.datetime.now().isoformat(timespec="seconds")
    env = collect_env()
    probes = _probe_setup(args, env)
    if not probes:
        print("error: no probe targets available", file=sys.stderr)
        return EXIT_ERROR

    runner = ProbeRunner(probes, args.probe_interval)
    runner.start()
    nic = NicSampler(runner.origin(), interval=1.0)
    nic.start()
    router = start_router_sampler(args, runner.origin())
    deadline = time.perf_counter() + max(1, args.duration)
    consumed = {p.name: 0 for p in probes}
    try:
        while time.perf_counter() < deadline:
            remaining = deadline - time.perf_counter()
            time.sleep(max(0.0, min(args.interval, remaining)))
            snapshot = runner.samples()
            for name, series in snapshot.items():
                fresh = series[consumed[name]:]
                consumed[name] = len(series)
                stats = summarize(fresh)
                print("%-28s n=%-5d p50=%s p95=%s max=%s loss=%s%%" % (
                    name[:28], stats["n"] + stats["lost"],
                    _fmt(stats["p50"], 7), _fmt(stats["p95"], 7),
                    _fmt(stats["max"], 8), _fmt(stats["loss_pct"], 5, 1)))
            print("")
    except KeyboardInterrupt:
        print("(interrupted - writing partial result)", file=sys.stderr)
    runner.stop()

    nic.stop()
    if router:
        router.stop()
    result = build_result(env, dict(EMPTY_SPEEDTEST), {}, runner.samples(),
                          {}, started_at, mode="monitor",
                          keep_samples=getattr(args, "raw", False))
    result["observed_throughput"] = observed_throughput(nic.samples())
    result["router_throughput"] = router_throughput(
            router, keep_samples=getattr(args, "raw", False))
    _emit(result, args, render_human(result))
    return EXIT_OK if result["validation"]["trustworthy"] else EXIT_UNTRUSTWORTHY


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if getattr(args, "version", False):
        print("netdiag %s (schema %d)" % (VERSION, SCHEMA_VERSION))
        return EXIT_OK
    if args.command == "router":
        return cmd_router(args)
    if args.command == "env":
        return cmd_env(args)
    if args.command == "compare":
        return cmd_compare(args)
    if args.command == "monitor":
        return cmd_monitor(args)
    if args.command in ("bufferbloat", "probe", "classes"):
        return cmd_measure(args)
    parser.print_help()
    return EXIT_ERROR


if __name__ == "__main__":
    sys.exit(main())
