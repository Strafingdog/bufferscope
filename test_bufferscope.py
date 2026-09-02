"""Tests for bufferscope. Standard library only; no network, no root, no OS assumptions."""
import argparse
import contextlib
import io
import json
import os
import socket
import sys
import tempfile
import threading
import time
import unittest

import bufferscope


# --------------------------------------------------------------------------
# STATS
# --------------------------------------------------------------------------


class TestPercentile(unittest.TestCase):
    def test_returns_none_for_empty(self):
        self.assertIsNone(bufferscope.percentile([], 0.5))

    def test_single_value(self):
        self.assertEqual(bufferscope.percentile([4.0], 0.95), 4.0)

    def test_p50_of_known_series(self):
        self.assertEqual(bufferscope.percentile([1.0, 2.0, 3.0, 4.0, 5.0], 0.5), 3.0)

    def test_p95_picks_tail_not_middle(self):
        # 10% of samples are slow, so the 95th percentile must land in the tail.
        values = [1.0] * 90 + [500.0] * 10
        self.assertEqual(bufferscope.percentile(values, 0.95), 500.0)

    def test_p95_excludes_a_tail_smaller_than_five_percent(self):
        # Exactly 5% slow: nearest-rank p95 correctly stays at the fast value.
        # This pins the boundary semantics so the definition cannot drift.
        values = [1.0] * 95 + [500.0] * 5
        self.assertEqual(bufferscope.percentile(values, 0.95), 1.0)
        self.assertEqual(bufferscope.percentile(values, 0.99), 500.0)

    def test_unsorted_input_is_sorted(self):
        self.assertEqual(bufferscope.percentile([5.0, 1.0, 3.0, 2.0, 4.0], 0.5), 3.0)


class TestSummarize(unittest.TestCase):
    def _samples(self, values):
        return [(float(i) * 0.02, v) for i, v in enumerate(values)]

    def test_empty_returns_none_stats_but_zero_counts(self):
        out = bufferscope.summarize([])
        self.assertEqual(out["n"], 0)
        self.assertEqual(out["lost"], 0)
        self.assertIsNone(out["p95"])

    def test_counts_losses(self):
        out = bufferscope.summarize(self._samples([1.0, None, 2.0, None]))
        self.assertEqual(out["n"], 2)
        self.assertEqual(out["lost"], 2)
        self.assertEqual(out["loss_pct"], 50.0)

    def test_bimodal_mean_hides_what_p95_reveals(self):
        out = bufferscope.summarize(self._samples([4.0] * 90 + [400.0] * 10))
        self.assertLess(out["mean"], 45.0)
        self.assertEqual(out["p95"], 400.0)

    def test_jitter_consecutive_is_mean_absolute_delta(self):
        out = bufferscope.summarize(self._samples([10.0, 12.0, 10.0]))
        self.assertAlmostEqual(out["jitter_consecutive"], 2.0)

    def test_jitter_ignores_losses_without_crashing(self):
        out = bufferscope.summarize(self._samples([10.0, None, 12.0]))
        self.assertAlmostEqual(out["jitter_consecutive"], 2.0)

    def test_rfc3550_jitter_converges_below_raw_delta(self):
        out = bufferscope.summarize(self._samples([10.0, 20.0] * 20))
        self.assertGreater(out["jitter_rfc3550"], 0.0)
        self.assertLess(out["jitter_rfc3550"], out["jitter_consecutive"])


# --------------------------------------------------------------------------
# GRADING AND VALIDATION
# --------------------------------------------------------------------------


class TestGrade(unittest.TestCase):
    def test_boundaries(self):
        self.assertEqual(bufferscope.grade(0.0), "A+")
        self.assertEqual(bufferscope.grade(4.99), "A+")
        self.assertEqual(bufferscope.grade(5.0), "A")
        self.assertEqual(bufferscope.grade(29.99), "A")
        self.assertEqual(bufferscope.grade(30.0), "B")
        self.assertEqual(bufferscope.grade(59.99), "B")
        self.assertEqual(bufferscope.grade(60.0), "C")
        self.assertEqual(bufferscope.grade(199.99), "C")
        self.assertEqual(bufferscope.grade(200.0), "D")
        self.assertEqual(bufferscope.grade(5000.0), "D")

    def test_none_is_not_applicable(self):
        self.assertEqual(bufferscope.grade(None), "n/a")

    def test_negative_added_latency_is_best_grade(self):
        self.assertEqual(bufferscope.grade(-3.0), "A+")


def _result(nic, reported, n=100, idle_p95=5.0):
    return {
        "phases": {
            "idle": {"probes": {"tcp": {"n": n, "p95": idle_p95}}},
            "upload": {
                "throughput_mbps_nic": nic,
                "throughput_mbps_reported": reported,
                "probes": {"tcp": {"n": n, "p95": 9.0}},
            },
        }
    }


class TestValidateRun(unittest.TestCase):
    def test_saturated_run_is_trustworthy(self):
        out = bufferscope.validate_run(_result(880.0, 900.0))
        self.assertTrue(out["trustworthy"])
        self.assertEqual(out["reasons"], [])

    def test_nic_above_reported_is_fine(self):
        # NIC counters include protocol overhead, so they read slightly high.
        out = bufferscope.validate_run(_result(930.0, 900.0))
        self.assertTrue(out["trustworthy"])

    def test_unsaturated_run_is_rejected(self):
        out = bufferscope.validate_run(_result(500.0, 900.0))
        self.assertFalse(out["trustworthy"])
        self.assertTrue(any("saturat" in r.lower() for r in out["reasons"]))

    def test_too_few_samples_is_rejected(self):
        out = bufferscope.validate_run(_result(880.0, 900.0, n=5))
        self.assertFalse(out["trustworthy"])
        self.assertTrue(any("sample" in r.lower() for r in out["reasons"]))

    def test_congested_baseline_is_rejected(self):
        out = bufferscope.validate_run(_result(880.0, 900.0, idle_p95=250.0))
        self.assertFalse(out["trustworthy"])
        self.assertTrue(any("baseline" in r.lower() for r in out["reasons"]))

    def test_missing_throughput_keys_add_a_reason(self):
        out = bufferscope.validate_run({"phases": {"upload": {"probes": {}}}})
        self.assertFalse(out["trustworthy"])
        self.assertTrue(out["reasons"])

    def test_load_mode_with_no_loaded_phases_is_rejected(self):
        # Ookla produced no phase events: the run measured nothing, but the
        # saturation rules have no phase to fire on. Must still be rejected.
        out = bufferscope.validate_run({
            "mode": "bufferbloat",
            "phases": {"idle": {"probes": {"tcp": {"n": 100, "p95": 5.0}}}},
        })
        self.assertFalse(out["trustworthy"])
        self.assertTrue(any("loaded phase" in r.lower() for r in out["reasons"]))

    def test_passive_probe_mode_needs_no_loaded_phases(self):
        out = bufferscope.validate_run({
            "mode": "probe",
            "phases": {"idle": {"probes": {"tcp": {"n": 5000, "p95": 6.0}}}},
        })
        self.assertTrue(out["trustworthy"])
        self.assertEqual(out["reasons"], [])

    def test_passive_probe_on_a_busy_line_is_still_trustworthy(self):
        # A congested line is the finding, not a fault of the measurement.
        out = bufferscope.validate_run({
            "mode": "probe",
            "phases": {"idle": {"probes": {"tcp": {"n": 5000, "p95": 350.0}}}},
        })
        self.assertTrue(out["trustworthy"])

    def test_load_mode_still_rejects_a_congested_baseline(self):
        out = bufferscope.validate_run(dict(_result(880.0, 900.0, idle_p95=250.0),
                                        mode="bufferbloat"))
        self.assertFalse(out["trustworthy"])


# --------------------------------------------------------------------------
# PLATFORM PARSERS
# --------------------------------------------------------------------------

WIN_ROUTE_CSV = '''"NextHop","RouteMetric"
"0.0.0.0","256"
"192.168.1.1","0"
'''

LINUX_ROUTE = """Iface\tDestination\tGateway \tFlags\tRefCnt\tUse\tMetric\tMask\t\tMTU\tWindow\tIRTT
eth0\t00000000\t0101A8C0\t0003\t0\t0\t100\t00000000\t0\t0\t0
eth0\t0001A8C0\t00000000\t0001\t0\t0\t100\t00FFFFFF\t0\t0\t0
"""

MACOS_ROUTE = """Routing tables

Internet:
Destination        Gateway            Flags        Netif Expire
default            192.168.1.1        UGScg          en0
127                127.0.0.1          UCS            lo0
"""

WIN_NIC_CSV = '''"Name","ReceivedBytes","SentBytes"
"Ethernet","123456789","987654321"
"Wi-Fi","10","20"
'''

LINUX_NIC = """Inter-|   Receive                                                |  Transmit
 face |bytes    packets errs drop fifo frame compressed multicast|bytes    packets
    lo: 1000      10    0    0    0     0          0         0     1000      10
  eth0: 123456789 1000   0    0    0     0          0         0   987654321  900
"""

MACOS_NIC = """Name  Mtu   Network       Address            Ipkts Ierrs     Ibytes    Opkts Oerrs     Obytes  Coll
lo0   16384 <Link#1>                            100     0       1000      100     0       1000     0
en0   1500  <Link#4>  a1:b2:c3:d4:e5:f6      100000     0  123456789    90000     0  987654321     0
"""


class TestGatewayParsers(unittest.TestCase):
    def test_windows_prefers_lowest_metric_and_skips_zero_nexthop(self):
        self.assertEqual(bufferscope.parse_gateway_windows(WIN_ROUTE_CSV), "192.168.1.1")

    def test_linux_decodes_little_endian_hex(self):
        self.assertEqual(bufferscope.parse_gateway_linux(LINUX_ROUTE), "192.168.1.1")

    def test_macos_reads_default_row(self):
        self.assertEqual(bufferscope.parse_gateway_macos(MACOS_ROUTE), "192.168.1.1")

    def test_all_return_none_on_garbage(self):
        for fn in (bufferscope.parse_gateway_windows,
                   bufferscope.parse_gateway_linux,
                   bufferscope.parse_gateway_macos):
            self.assertIsNone(fn(""))
            self.assertIsNone(fn("not remotely valid output"))


class TestNicParsers(unittest.TestCase):
    def test_windows(self):
        out = bufferscope.parse_nic_windows(WIN_NIC_CSV)
        self.assertEqual(out["Ethernet"], (123456789, 987654321))

    def test_linux(self):
        out = bufferscope.parse_nic_linux(LINUX_NIC)
        self.assertEqual(out["eth0"], (123456789, 987654321))

    def test_macos_takes_first_row_per_interface(self):
        out = bufferscope.parse_nic_macos(MACOS_NIC)
        self.assertEqual(out["en0"], (123456789, 987654321))

    def test_all_return_empty_on_garbage(self):
        for fn in (bufferscope.parse_nic_windows,
                   bufferscope.parse_nic_linux,
                   bufferscope.parse_nic_macos):
            self.assertEqual(fn(""), {})
            self.assertEqual(fn("garbage"), {})


# --------------------------------------------------------------------------
# PLATFORM DISCOVERY
# --------------------------------------------------------------------------


class TestDiscovery(unittest.TestCase):
    def setUp(self):
        self._real_run = bufferscope.run_cmd
        self._real_system = bufferscope.platform.system

    def tearDown(self):
        bufferscope.run_cmd = self._real_run
        bufferscope.platform.system = self._real_system

    def test_detect_gateway_uses_platform_specific_parser(self):
        bufferscope.platform.system = lambda: "Linux"
        bufferscope.run_cmd = lambda *a, **k: LINUX_ROUTE
        self.assertEqual(bufferscope.detect_gateway(), "192.168.1.1")

    def test_detect_gateway_returns_none_when_command_fails(self):
        bufferscope.platform.system = lambda: "Darwin"
        bufferscope.run_cmd = lambda *a, **k: None
        self.assertIsNone(bufferscope.detect_gateway())

    def test_detect_gateway_never_raises_on_unknown_os(self):
        bufferscope.platform.system = lambda: "Plan9"
        bufferscope.run_cmd = lambda *a, **k: "whatever"
        self.assertIsNone(bufferscope.detect_gateway())

    def test_run_cmd_returns_none_for_missing_binary(self):
        self.assertIsNone(bufferscope.run_cmd(["definitely-not-a-real-binary-xyz"]))

    def test_collect_env_has_required_keys_and_never_raises(self):
        bufferscope.run_cmd = lambda *a, **k: None
        env = bufferscope.collect_env()
        for key in ("os", "python", "bufferscope_version", "schema_version",
                    "gateway", "interfaces", "speedtest_path",
                    "speedtest_version", "icmp_available"):
            self.assertIn(key, env)
        self.assertEqual(env["bufferscope_version"], bufferscope.VERSION)


# --------------------------------------------------------------------------
# PROBES
# --------------------------------------------------------------------------


class FakeProbe(bufferscope.Probe):
    def __init__(self, name, values):
        self.name = name
        self._values = list(values)
        self.calls = 0

    def sample(self):
        self.calls += 1
        if not self._values:
            return 1.0
        return self._values.pop(0)


class TestProbeRunner(unittest.TestCase):
    def test_collects_samples_from_each_probe(self):
        a = FakeProbe("a", [1.0, 2.0, 3.0])
        b = FakeProbe("b", [None, 5.0])
        runner = bufferscope.ProbeRunner([a, b], interval_ms=5)
        runner.start()
        time.sleep(0.15)
        runner.stop()
        out = runner.samples()
        self.assertIn("a", out)
        self.assertIn("b", out)
        self.assertGreater(len(out["a"]), 2)
        for timestamp, value in out["a"]:
            self.assertIsInstance(timestamp, float)
            self.assertTrue(value is None or isinstance(value, float))

    def test_stop_is_idempotent(self):
        runner = bufferscope.ProbeRunner([FakeProbe("a", [1.0])], interval_ms=5)
        runner.start()
        runner.stop()
        runner.stop()

    def test_probe_exception_is_recorded_as_loss_not_crash(self):
        class Exploding(bufferscope.Probe):
            name = "boom"

            def sample(self):
                raise RuntimeError("nope")

        runner = bufferscope.ProbeRunner([Exploding()], interval_ms=5)
        runner.start()
        time.sleep(0.08)
        runner.stop()
        values = [v for _, v in runner.samples()["boom"]]
        self.assertTrue(values)
        self.assertTrue(all(v is None for v in values))


class TestBuildProbes(unittest.TestCase):
    def test_includes_gateway_and_targets(self):
        probes = bufferscope.build_probes(["1.1.1.1"], "192.168.1.1", use_icmp=False)
        names = [p.name for p in probes]
        self.assertTrue(any("192.168.1.1" in n for n in names))
        self.assertTrue(any("1.1.1.1" in n for n in names))
        self.assertFalse(any("icmp" in n for n in names))

    def test_omits_gateway_when_absent(self):
        probes = bufferscope.build_probes(["1.1.1.1"], None, use_icmp=False)
        self.assertTrue(all("192.168" not in p.name for p in probes))

    def test_icmp_added_when_requested(self):
        probes = bufferscope.build_probes(["1.1.1.1"], None, use_icmp=True)
        self.assertTrue(any("icmp" in p.name for p in probes))

    def test_probe_names_are_unique(self):
        probes = bufferscope.build_probes(["1.1.1.1", "8.8.8.8"], "192.168.1.1",
                                      use_icmp=True)
        names = [p.name for p in probes]
        self.assertEqual(len(names), len(set(names)))


class TestUdpDnsProbe(unittest.TestCase):
    def test_query_encodes_name_and_transaction_id(self):
        probe = bufferscope.UdpDnsProbe("1.1.1.1", qname="example.com")
        packet = probe._query(0xABCD)
        self.assertEqual(packet[:2], b"\xab\xcd")
        self.assertIn(b"\x07example\x03com\x00", packet)

    def test_unreachable_host_returns_none_quickly(self):
        # 192.0.2.1 is TEST-NET-1: reserved, guaranteed not to answer.
        probe = bufferscope.UdpDnsProbe("192.0.2.1", timeout=0.2)
        self.assertIsNone(probe.sample())


class TestTcpProbe(unittest.TestCase):
    def test_unreachable_host_returns_none(self):
        probe = bufferscope.TcpProbe("192.0.2.1", port=9, timeout=0.2)
        self.assertIsNone(probe.sample())


# --------------------------------------------------------------------------
# RUNNER
# --------------------------------------------------------------------------


class TestJsonlParsing(unittest.TestCase):
    def test_parses_valid_event(self):
        out = bufferscope.parse_jsonl_event(
            '{"type":"download","download":{"bandwidth":1000}}')
        self.assertEqual(out["type"], "download")

    def test_returns_none_for_noise(self):
        self.assertIsNone(bufferscope.parse_jsonl_event(""))
        self.assertIsNone(bufferscope.parse_jsonl_event("not json"))
        self.assertIsNone(bufferscope.parse_jsonl_event("[1,2,3]"))
        self.assertIsNone(bufferscope.parse_jsonl_event('{"no":"type key"}'))


class TestPhases(unittest.TestCase):
    def _events(self):
        return [
            (0.0, {"type": "testStart"}),
            (2.0, {"type": "download"}),
            (2.5, {"type": "download"}),
            (9.0, {"type": "upload"}),
            (16.0, {"type": "result"}),
        ]

    def test_phase_windows_derived_from_first_event_of_each_type(self):
        phases = bufferscope.phases_from_events(self._events(), end_time=17.0)
        as_dict = {name: (start, end) for name, start, end in phases}
        self.assertEqual(as_dict["download"][0], 2.0)
        self.assertEqual(as_dict["download"][1], 9.0)
        self.assertEqual(as_dict["upload"][0], 9.0)
        self.assertEqual(as_dict["upload"][1], 16.0)

    def test_missing_upload_phase_is_simply_absent(self):
        phases = bufferscope.phases_from_events(
            [(0.0, {"type": "testStart"}), (1.0, {"type": "download"})],
            end_time=5.0)
        self.assertNotIn("upload", [name for name, _, _ in phases])

    def test_no_events_yields_no_phases(self):
        self.assertEqual(bufferscope.phases_from_events([], end_time=5.0), [])


class TestBucketSamples(unittest.TestCase):
    def test_assigns_samples_by_timestamp(self):
        samples = {"tcp": [(1.0, 4.0), (3.0, 50.0), (10.0, 200.0), (99.0, 1.0)]}
        phases = [("download", 2.0, 9.0), ("upload", 9.0, 16.0)]
        out = bufferscope.bucket_samples(samples, phases)
        self.assertEqual([v for _, v in out["download"]["tcp"]], [50.0])
        self.assertEqual([v for _, v in out["upload"]["tcp"]], [200.0])

    def test_boundary_sample_belongs_to_the_phase_it_starts(self):
        samples = {"tcp": [(9.0, 7.0)]}
        phases = [("download", 2.0, 9.0), ("upload", 9.0, 16.0)]
        out = bufferscope.bucket_samples(samples, phases)
        self.assertEqual(out["download"]["tcp"], [])
        self.assertEqual([v for _, v in out["upload"]["tcp"]], [7.0])


class TestExtractResult(unittest.TestCase):
    def test_converts_bytes_per_second_to_mbps(self):
        events = [(1.0, {
            "type": "result",
            "download": {"bandwidth": 118750000},
            "upload": {"bandwidth": 104625000},
            "ping": {"latency": 3.47},
            "packetLoss": 0.0,
            "server": {"name": "Example Networks", "location": "Anytown", "id": 12345},
            "result": {"url": "https://example/result/1"},
        })]
        out = bufferscope.extract_result(events)
        self.assertAlmostEqual(out["download_mbps"], 950.0, places=1)
        self.assertAlmostEqual(out["upload_mbps"], 837.0, places=1)
        self.assertEqual(out["idle_latency_ms"], 3.47)
        self.assertEqual(out["server"]["id"], 12345)
        self.assertEqual(out["result_url"], "https://example/result/1")

    def test_missing_result_event_returns_none_fields(self):
        out = bufferscope.extract_result([(0.0, {"type": "testStart"})])
        self.assertIsNone(out["download_mbps"])
        self.assertIsNone(out["upload_mbps"])


class TestNicDelta(unittest.TestCase):
    def test_picks_busiest_interface(self):
        before = {"lo": (0, 0), "eth0": (0, 0)}
        after = {"lo": (10, 10), "eth0": (125_000_000, 12_500_000)}
        down, up = bufferscope.nic_delta_mbps(before, after, seconds=1.0)
        self.assertAlmostEqual(down, 1000.0, places=1)
        self.assertAlmostEqual(up, 100.0, places=1)

    def test_zero_seconds_is_safe(self):
        self.assertEqual(bufferscope.nic_delta_mbps({}, {}, 0.0), (0.0, 0.0))

    def test_interface_absent_from_before_contributes_nothing(self):
        down, up = bufferscope.nic_delta_mbps({}, {"eth0": (999, 999)}, seconds=1.0)
        self.assertEqual((down, up), (0.0, 0.0))


# --------------------------------------------------------------------------
# REPORTERS
# --------------------------------------------------------------------------


def _fake_buckets():
    idle = {"tcp:1.1.1.1:443": [(0.1 * i, 4.0) for i in range(50)]}
    loaded = {"upload": {"tcp:1.1.1.1:443": [(10.0 + 0.1 * i, 60.0)
                                             for i in range(50)]}}
    return idle, loaded


_SPEED = {"download_mbps": 950.0, "upload_mbps": 837.0,
          "idle_latency_ms": 3.5, "packet_loss_pct": 0.0,
          "server": None, "result_url": None}


class TestBuildResult(unittest.TestCase):
    def test_shape_and_bufferbloat_maths(self):
        idle, buckets = _fake_buckets()
        result = bufferscope.build_result(
            env={"os": "test"}, speedtest_result=_SPEED,
            phase_buckets=buckets, idle_samples=idle,
            throughput={"upload": (5.0, 830.0)}, started_at="2026-08-21T13:00:00")
        self.assertEqual(result["schema_version"], bufferscope.SCHEMA_VERSION)
        upload = result["phases"]["upload"]
        self.assertAlmostEqual(upload["throughput_mbps_nic"], 830.0)
        self.assertAlmostEqual(upload["throughput_mbps_reported"], 837.0)
        bloat = result["bufferbloat"]["upload"]["tcp:1.1.1.1:443"]
        self.assertAlmostEqual(bloat["added_p95_ms"], 56.0)
        self.assertEqual(bloat["grade"], "B")
        self.assertEqual(result["bufferbloat"]["overall_grade"], "B")
        self.assertIn("trustworthy", result["validation"])

    def test_result_is_json_serialisable(self):
        idle, buckets = _fake_buckets()
        result = bufferscope.build_result(
            env={"os": "test"},
            speedtest_result={"download_mbps": None, "upload_mbps": None,
                              "idle_latency_ms": None, "packet_loss_pct": None,
                              "server": None, "result_url": None},
            phase_buckets=buckets, idle_samples=idle,
            throughput={"upload": (0.0, 0.0)}, started_at="x")
        json.dumps(result)

    def test_unsaturated_run_is_flagged_untrustworthy(self):
        idle, buckets = _fake_buckets()
        result = bufferscope.build_result(
            env={"os": "test"}, speedtest_result=_SPEED,
            phase_buckets=buckets, idle_samples=idle,
            throughput={"upload": (5.0, 100.0)}, started_at="x")
        self.assertFalse(result["validation"]["trustworthy"])


class TestRenderers(unittest.TestCase):
    def _result(self):
        idle, buckets = _fake_buckets()
        return bufferscope.build_result(
            env={"os": "test"}, speedtest_result=_SPEED,
            phase_buckets=buckets, idle_samples=idle,
            throughput={"upload": (5.0, 830.0)}, started_at="x")

    def test_human_output_mentions_grade_and_is_ascii(self):
        text = bufferscope.render_human(self._result())
        self.assertIn("B", text)
        text.encode("ascii")

    def test_human_output_shows_untrustworthy_banner(self):
        idle, buckets = _fake_buckets()
        bad = bufferscope.build_result(
            env={"os": "test"}, speedtest_result=_SPEED,
            phase_buckets=buckets, idle_samples=idle,
            throughput={"upload": (5.0, 100.0)}, started_at="x")
        self.assertIn("NOT TRUSTWORTHY", bufferscope.render_human(bad))

    def test_compare_marks_direction_of_change(self):
        a = self._result()
        b = self._result()
        b["bufferbloat"]["worst_added_p95_ms"] = 2.0
        b["bufferbloat"]["overall_grade"] = "A+"
        text = bufferscope.render_compare(a, b)
        self.assertIn("better", text.lower())

    def test_compare_marks_regression_as_worse(self):
        a = self._result()
        b = self._result()
        b["speedtest"] = dict(_SPEED, upload_mbps=400.0)
        text = bufferscope.render_compare(a, b)
        self.assertIn("worse", text.lower())


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


class CliMixin:
    def _run(self, argv):
        out = io.StringIO()
        err = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = bufferscope.main(argv)
        return code, out.getvalue(), err.getvalue()


class TestCli(CliMixin, unittest.TestCase):
    def test_version_flag(self):
        code, out, _ = self._run(["--version"])
        self.assertEqual(code, 0)
        self.assertIn(bufferscope.VERSION, out)

    def test_env_json_is_parseable(self):
        code, out, _ = self._run(["env", "--json"])
        self.assertEqual(code, 0)
        parsed = json.loads(out)
        self.assertIn("gateway", parsed)

    def test_no_subcommand_prints_help_and_errors(self):
        code, _, _ = self._run([])
        self.assertEqual(code, bufferscope.EXIT_ERROR)

    def test_compare_two_files(self):
        doc = {"speedtest": {"download_mbps": 900.0, "upload_mbps": 800.0},
               "bufferbloat": {"worst_added_p95_ms": 200.0, "overall_grade": "D"},
               "schema_version": bufferscope.SCHEMA_VERSION}
        other = json.loads(json.dumps(doc))
        other["bufferbloat"] = {"worst_added_p95_ms": 4.0, "overall_grade": "A+"}
        paths = []
        for payload in (doc, other):
            handle = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
            json.dump(payload, handle)
            handle.close()
            paths.append(handle.name)
        try:
            code, out, _ = self._run(["compare"] + paths)
            self.assertEqual(code, 0)
            self.assertIn("better", out.lower())
        finally:
            for path in paths:
                os.unlink(path)

    def test_compare_missing_file_exits_1(self):
        code, _, err = self._run(["compare", "nope-a.json", "nope-b.json"])
        self.assertEqual(code, bufferscope.EXIT_ERROR)
        self.assertTrue(err.strip())

    def test_compare_schema_mismatch_exits_1(self):
        paths = []
        for version in (bufferscope.SCHEMA_VERSION, bufferscope.SCHEMA_VERSION + 1):
            handle = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
            json.dump({"schema_version": version}, handle)
            handle.close()
            paths.append(handle.name)
        try:
            code, _, err = self._run(["compare"] + paths)
            self.assertEqual(code, bufferscope.EXIT_ERROR)
            self.assertIn("schema", err.lower())
        finally:
            for path in paths:
                os.unlink(path)

    def test_unknown_subcommand_exits_nonzero(self):
        with self.assertRaises(SystemExit):
            bufferscope.main(["nonsense"])


class TestCmdMeasure(CliMixin, unittest.TestCase):
    def setUp(self):
        self._find = bufferscope.find_speedtest
        self._run_st = bufferscope.run_speedtest
        self._env = bufferscope.collect_env

    def tearDown(self):
        bufferscope.find_speedtest = self._find
        bufferscope.run_speedtest = self._run_st
        bufferscope.collect_env = self._env

    def test_missing_speedtest_binary_exits_1_with_hint(self):
        bufferscope.find_speedtest = lambda: None
        bufferscope.collect_env = lambda: {"os": "test", "gateway": None,
                                       "icmp_available": False}
        code, _, err = self._run(["bufferbloat", "--baseline-secs", "0"])
        self.assertEqual(code, bufferscope.EXIT_ERROR)
        self.assertIn("speedtest", err.lower())

    def test_untrustworthy_result_exits_2(self):
        bufferscope.find_speedtest = lambda: "/fake/speedtest"
        bufferscope.collect_env = lambda: {"os": "test", "gateway": None,
                                       "icmp_available": False}

        def fake_run(binary, server_id, on_event, origin, **kwargs):
            now = time.perf_counter() - origin
            events = [(now, {"type": "testStart"}),
                      (now + 0.2, {"type": "upload"}),
                      (now + 0.6, {"type": "result",
                                   "upload": {"bandwidth": 112500000},
                                   "download": {"bandwidth": 0},
                                   "ping": {"latency": 4.0}})]
            time.sleep(0.7)
            return events

        bufferscope.run_speedtest = fake_run
        code, _, _ = self._run(
            ["bufferbloat", "--baseline-secs", "0", "--probe-interval", "5",
             "--targets", "127.0.0.1", "--quiet"])
        self.assertEqual(code, bufferscope.EXIT_UNTRUSTWORTHY)

    def test_samples_land_in_the_right_phase(self):
        bufferscope.find_speedtest = lambda: "/fake/speedtest"
        bufferscope.collect_env = lambda: {"os": "test", "gateway": None,
                                       "icmp_available": False}
        captured = {}

        def fake_run(binary, server_id, on_event, origin, **kwargs):
            start = time.perf_counter() - origin
            events = [(start, {"type": "download"})]
            time.sleep(0.3)
            mid = time.perf_counter() - origin
            events.append((mid, {"type": "upload"}))
            time.sleep(0.3)
            end = time.perf_counter() - origin
            events.append((end, {"type": "result",
                                 "download": {"bandwidth": 1},
                                 "upload": {"bandwidth": 1},
                                 "ping": {"latency": 4.0}}))
            captured["windows"] = (start, mid, end)
            return events

        bufferscope.run_speedtest = fake_run
        out_path = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
        out_path.close()
        try:
            self._run(["bufferbloat", "--baseline-secs", "0",
                       "--probe-interval", "5", "--targets", "127.0.0.1",
                       "--quiet", "--out", out_path.name])
            with open(out_path.name) as handle:
                doc = json.load(handle)
        finally:
            os.unlink(out_path.name)

        # Both loaded phases must have received samples on the shared clock.
        self.assertIn("download", doc["phases"])
        self.assertIn("upload", doc["phases"])
        for phase in ("download", "upload"):
            counts = [s["n"] + s["lost"]
                      for s in doc["phases"][phase]["probes"].values()]
            self.assertTrue(any(c > 0 for c in counts),
                            "no samples bucketed into %s" % phase)

    def test_probe_subcommand_needs_no_speedtest(self):
        bufferscope.find_speedtest = lambda: None
        bufferscope.collect_env = lambda: {"os": "test", "gateway": None,
                                       "icmp_available": False}
        code, _, _ = self._run(
            ["probe", "--duration", "1", "--probe-interval", "50",
             "--targets", "127.0.0.1"])
        self.assertIn(code, (bufferscope.EXIT_OK, bufferscope.EXIT_UNTRUSTWORTHY))


class TestMonitor(CliMixin, unittest.TestCase):
    def setUp(self):
        self._env = bufferscope.collect_env
        bufferscope.collect_env = lambda: {"os": "test", "gateway": None,
                                       "icmp_available": False}

    def tearDown(self):
        bufferscope.collect_env = self._env

    def _forbid_speedtest(self):
        """monitor must never generate load, so neither hook may be reached."""
        def boom(*a, **k):
            raise AssertionError("monitor must not run speedtest")
        bufferscope.run_speedtest = boom
        bufferscope.find_speedtest = lambda: None

    def test_emits_interval_lines_and_final_summary(self):
        real_run, real_find = bufferscope.run_speedtest, bufferscope.find_speedtest
        self._forbid_speedtest()
        try:
            code, out, _ = self._run(
                ["monitor", "--duration", "2", "--interval", "1",
                 "--probe-interval", "50", "--targets", "127.0.0.1"])
        finally:
            bufferscope.run_speedtest, bufferscope.find_speedtest = real_run, real_find
        self.assertIn(code, (bufferscope.EXIT_OK, bufferscope.EXIT_UNTRUSTWORTHY))
        # Two interval summaries, each naming every probe.
        self.assertGreaterEqual(out.count("p95="), 2)
        self.assertIn("Bufferbloat", out)

    def test_monitor_writes_json_when_asked(self):
        real_run, real_find = bufferscope.run_speedtest, bufferscope.find_speedtest
        self._forbid_speedtest()
        handle = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
        handle.close()
        try:
            self._run(["monitor", "--duration", "1", "--interval", "1",
                       "--probe-interval", "50", "--targets", "127.0.0.1",
                       "--out", handle.name])
            with open(handle.name) as fh:
                doc = json.load(fh)
            self.assertEqual(doc["schema_version"], bufferscope.SCHEMA_VERSION)
        finally:
            bufferscope.run_speedtest, bufferscope.find_speedtest = real_run, real_find
            os.unlink(handle.name)


class TestThroughputInWindow(unittest.TestCase):
    """NIC counters are sampled on a thread; throughput is integrated after."""

    def _samples(self):
        # 1 second apart; eth0 gains 125 MB/s down (1000 Mbps), 12.5 MB/s up.
        return [
            (0.0, {"eth0": (0, 0), "lo": (0, 0)}),
            (1.0, {"eth0": (125_000_000, 12_500_000), "lo": (5, 5)}),
            (2.0, {"eth0": (250_000_000, 25_000_000), "lo": (10, 10)}),
            (3.0, {"eth0": (375_000_000, 37_500_000), "lo": (15, 15)}),
        ]

    def test_integrates_over_the_requested_window(self):
        down, up = bufferscope.throughput_in_window(self._samples(), 1.0, 3.0)
        self.assertAlmostEqual(down, 1000.0, places=1)
        self.assertAlmostEqual(up, 100.0, places=1)

    def test_uses_only_samples_inside_the_window(self):
        # Restricting to [0,1] must give the same rate, not the whole-run average.
        down, _ = bufferscope.throughput_in_window(self._samples(), 0.0, 1.0)
        self.assertAlmostEqual(down, 1000.0, places=1)

    def test_returns_none_when_fewer_than_two_samples(self):
        self.assertEqual(bufferscope.throughput_in_window(self._samples(), 1.4, 1.6),
                         (None, None))
        self.assertEqual(bufferscope.throughput_in_window([], 0.0, 5.0), (None, None))

    def test_counter_reset_does_not_produce_negative_throughput(self):
        samples = [(0.0, {"eth0": (500, 500)}), (1.0, {"eth0": (10, 10)})]
        down, up = bufferscope.throughput_in_window(samples, 0.0, 1.0)
        self.assertEqual((down, up), (0.0, 0.0))


class TestNicSampler(unittest.TestCase):
    def test_collects_timestamped_snapshots(self):
        real = bufferscope.read_nic_counters
        bufferscope.read_nic_counters = lambda: {"eth0": (1, 2)}
        try:
            sampler = bufferscope.NicSampler(origin=time.perf_counter(), interval=0.02)
            sampler.start()
            time.sleep(0.12)
            sampler.stop()
        finally:
            bufferscope.read_nic_counters = real
        samples = sampler.samples()
        self.assertGreaterEqual(len(samples), 2)
        for stamp, counters in samples:
            self.assertIsInstance(stamp, float)
            self.assertIn("eth0", counters)


# --------------------------------------------------------------------------
# DSCP, PROBE SPECS, STUN
# --------------------------------------------------------------------------


class TestDscp(unittest.TestCase):
    def test_known_names(self):
        self.assertEqual(bufferscope.dscp_value("ef"), 46)
        self.assertEqual(bufferscope.dscp_value("EF"), 46)
        self.assertEqual(bufferscope.dscp_value("cs0"), 0)
        self.assertEqual(bufferscope.dscp_value("cs5"), 40)
        self.assertEqual(bufferscope.dscp_value("af41"), 34)
        self.assertEqual(bufferscope.dscp_value("default"), 0)

    def test_numeric_accepted(self):
        self.assertEqual(bufferscope.dscp_value("46"), 46)
        self.assertEqual(bufferscope.dscp_value("0"), 0)

    def test_rejects_nonsense_and_out_of_range(self):
        for bad in ("banana", "-1", "64", ""):
            with self.assertRaises(ValueError):
                bufferscope.dscp_value(bad)

    def test_tos_byte_is_dscp_shifted_left_two(self):
        # EF is DSCP 46; the ToS byte carrying it is 46 << 2 = 184.
        self.assertEqual(bufferscope.dscp_to_tos(46), 184)
        self.assertEqual(bufferscope.dscp_to_tos(0), 0)


class TestProbeSpec(unittest.TestCase):
    def test_tcp_with_explicit_port(self):
        p = bufferscope.parse_probe_spec("tcp:1.1.1.1:853")
        self.assertIsInstance(p, bufferscope.TcpProbe)
        self.assertEqual((p.host, p.port), ("1.1.1.1", 853))

    def test_defaults_per_kind(self):
        self.assertEqual(bufferscope.parse_probe_spec("tcp:1.1.1.1").port, 443)
        self.assertEqual(bufferscope.parse_probe_spec("dns:1.1.1.1").port, 53)
        self.assertEqual(bufferscope.parse_probe_spec("stun:example.net").port, 3478)

    def test_icmp(self):
        self.assertIsInstance(bufferscope.parse_probe_spec("icmp:1.1.1.1"),
                              bufferscope.IcmpProbe)

    def test_dscp_suffix(self):
        p = bufferscope.parse_probe_spec("dns:1.1.1.1@dscp=ef")
        self.assertEqual(p.dscp, 46)
        self.assertIn("ef", p.name)

    def test_label_suffix_overrides_name(self):
        p = bufferscope.parse_probe_spec("tcp:1.1.1.1:853#control")
        self.assertEqual(p.name, "control")

    def test_no_dscp_means_none(self):
        self.assertIsNone(bufferscope.parse_probe_spec("tcp:1.1.1.1:853").dscp)

    def test_invalid_specs_raise(self):
        for bad in ("", "nope:1.1.1.1", "tcp:", "tcp:1.1.1.1:notaport",
                    "tcp:1.1.1.1:99999", "dns:1.1.1.1@dscp=banana"):
            with self.assertRaises(ValueError):
                bufferscope.parse_probe_spec(bad)


class TestStunProbe(unittest.TestCase):
    def test_binding_request_is_well_formed(self):
        probe = bufferscope.StunProbe("example.net")
        txid = b"0123456789ab"
        pkt = probe._request(txid)
        self.assertEqual(len(pkt), 20)
        self.assertEqual(pkt[0:2], b"\x00\x01")          # binding request
        self.assertEqual(pkt[2:4], b"\x00\x00")          # zero length
        self.assertEqual(pkt[4:8], b"\x21\x12\xa4\x42")  # magic cookie
        self.assertEqual(pkt[8:20], txid)

    def test_reply_matching_requires_cookie_and_txid(self):
        probe = bufferscope.StunProbe("example.net")
        txid = b"0123456789ab"
        good = b"\x01\x01\x00\x00" + b"\x21\x12\xa4\x42" + txid
        self.assertTrue(probe._matches(good, txid))
        self.assertFalse(probe._matches(good[:19], txid))
        self.assertFalse(probe._matches(b"\x01\x01\x00\x00" + b"\x00\x00\x00\x00"
                                        + txid, txid))
        self.assertFalse(probe._matches(good[:8] + b"ba9876543210", txid))

    def test_unreachable_host_returns_none(self):
        probe = bufferscope.StunProbe("192.0.2.1", timeout=0.2)
        self.assertIsNone(probe.sample())


class TestProbeOverride(CliMixin, unittest.TestCase):
    def setUp(self):
        self._env = bufferscope.collect_env
        bufferscope.collect_env = lambda: {"os": "test", "gateway": "192.168.1.1",
                                       "icmp_available": False}

    def tearDown(self):
        bufferscope.collect_env = self._env

    def test_probe_flag_replaces_default_set(self):
        args = bufferscope.build_parser().parse_args(
            ["probe", "--probe", "tcp:1.1.1.1:853#control",
             "--probe", "dns:9.9.9.9"])
        probes = bufferscope.select_probes(args, bufferscope.collect_env())
        self.assertEqual([p.name for p in probes], ["control", "dns:9.9.9.9"])

    def test_default_set_used_when_no_probe_flag(self):
        args = bufferscope.build_parser().parse_args(["probe"])
        probes = bufferscope.select_probes(args, bufferscope.collect_env())
        self.assertTrue(any("192.168.1.1" in p.name for p in probes))

    def test_classes_uses_the_preset(self):
        args = bufferscope.build_parser().parse_args(["classes"])
        probes = bufferscope.select_probes(args, bufferscope.collect_env())
        names = [p.name for p in probes]
        self.assertIn("control-others", names)
        self.assertTrue(any("ef" in n for n in names))

    def test_bad_probe_spec_exits_1(self):
        code, _, err = self._run(["probe", "--probe", "banana:1.1.1.1",
                                  "--duration", "1"])
        self.assertEqual(code, bufferscope.EXIT_ERROR)
        self.assertIn("banana", err)


class TestRenderClasses(unittest.TestCase):
    def test_table_lists_each_probe_with_expected_class(self):
        result = {
            "phases": {
                "idle": {"probes": {"control-others": {"p95": 5.0},
                                    "dns-rule": {"p95": 6.0}}},
                "upload": {"probes": {"control-others": {"p95": 45.0},
                                      "dns-rule": {"p95": 9.0}}},
            }
        }
        text = bufferscope.render_classes(result)
        self.assertIn("control-others", text)
        self.assertIn("dns-rule", text)
        # The annotation names the queue in vendor-neutral terms - "Others" is
        # DrayTek's word for it, "default queue" works for any router.
        self.assertIn("default queue", text)
        text.encode("ascii")


# --------------------------------------------------------------------------
# REPEATED RUNS AND SIGNIFICANCE
# --------------------------------------------------------------------------


class TestTCritical(unittest.TestCase):
    def test_known_values(self):
        self.assertAlmostEqual(bufferscope.t_critical(1), 12.706, places=3)
        self.assertAlmostEqual(bufferscope.t_critical(4), 2.776, places=3)
        self.assertAlmostEqual(bufferscope.t_critical(9), 2.262, places=3)

    def test_large_df_approaches_normal(self):
        self.assertAlmostEqual(bufferscope.t_critical(1000), 1.960, places=3)

    def test_between_table_entries_uses_conservative_lower_df(self):
        # df 11 is not tabulated; must not be smaller than the df 12 value.
        self.assertGreaterEqual(bufferscope.t_critical(11), bufferscope.t_critical(12))

    def test_zero_or_negative_df_is_infinite(self):
        self.assertEqual(bufferscope.t_critical(0), float("inf"))


class TestConfidenceInterval(unittest.TestCase):
    def test_identical_values_have_zero_width(self):
        mean, lo, hi = bufferscope.confidence_interval([5.0] * 6)
        self.assertAlmostEqual(mean, 5.0)
        self.assertAlmostEqual(lo, 5.0)
        self.assertAlmostEqual(hi, 5.0)

    def test_interval_brackets_the_mean(self):
        mean, lo, hi = bufferscope.confidence_interval([1.0, 2.0, 3.0, 4.0, 5.0])
        self.assertAlmostEqual(mean, 3.0)
        self.assertLess(lo, 3.0)
        self.assertGreater(hi, 3.0)

    def test_single_sample_is_unbounded(self):
        mean, lo, hi = bufferscope.confidence_interval([7.0])
        self.assertEqual(mean, 7.0)
        self.assertEqual((lo, hi), (float("-inf"), float("inf")))

    def test_empty_returns_none(self):
        self.assertEqual(bufferscope.confidence_interval([]), (None, None, None))


class TestDifferenceCI(unittest.TestCase):
    def test_clearly_different_populations_are_significant(self):
        a = [10.0, 10.5, 9.5, 10.2, 10.1]
        b = [50.0, 50.5, 49.5, 50.2, 50.1]
        diff, lo, hi, sig = bufferscope.difference_ci(a, b)
        self.assertAlmostEqual(diff, 40.0, places=1)
        self.assertTrue(sig)
        self.assertGreater(lo, 0.0)

    def test_noisy_overlapping_populations_are_not_significant(self):
        # This is today's real situation: a large apparent gap, huge spread.
        a = [26.0, 16.2, 40.9, 2.3, 1.7]
        b = [36.0, 39.8, 12.0, 5.0, 30.0]
        diff, lo, hi, sig = bufferscope.difference_ci(a, b)
        self.assertFalse(sig)
        self.assertLess(lo, 0.0)
        self.assertGreater(hi, 0.0)

    def test_too_few_samples_is_never_significant(self):
        _, _, _, sig = bufferscope.difference_ci([1.0], [50.0])
        self.assertFalse(sig)


class TestAggregateRuns(unittest.TestCase):
    def _run(self, idle_p95, up_p95, dl=900.0, up=800.0):
        return {
            "speedtest": {"download_mbps": dl, "upload_mbps": up},
            "phases": {
                "idle": {"probes": {"ctrl": {"p95": idle_p95}}},
                "upload": {"probes": {"ctrl": {"p95": up_p95}}},
            },
            "validation": {"trustworthy": True, "reasons": []},
        }

    def test_aggregates_added_p95_per_probe(self):
        runs = [self._run(5.0, 25.0), self._run(5.0, 35.0), self._run(5.0, 30.0)]
        agg = bufferscope.aggregate_runs(runs)
        ctrl = agg["upload"]["ctrl"]
        self.assertEqual(ctrl["n"], 3)
        self.assertAlmostEqual(ctrl["mean"], 25.0)
        self.assertAlmostEqual(ctrl["min"], 20.0)
        self.assertAlmostEqual(ctrl["max"], 30.0)
        self.assertLess(ctrl["ci95_lo"], ctrl["mean"])

    def test_aggregates_throughput(self):
        runs = [self._run(5.0, 25.0, dl=900.0), self._run(5.0, 25.0, dl=910.0)]
        agg = bufferscope.aggregate_runs(runs)
        self.assertAlmostEqual(agg["speedtest"]["download_mbps"]["mean"], 905.0)

    def test_untrustworthy_runs_are_excluded_and_counted(self):
        bad = self._run(5.0, 25.0)
        bad["validation"] = {"trustworthy": False, "reasons": ["not saturated"]}
        agg = bufferscope.aggregate_runs([self._run(5.0, 25.0), bad])
        self.assertEqual(agg["upload"]["ctrl"]["n"], 1)
        self.assertEqual(agg["excluded_runs"], 1)


def _agg_doc(values, up_mbps=800.0):
    runs = []
    for v in values:
        runs.append({
            "speedtest": {"download_mbps": 900.0, "upload_mbps": up_mbps},
            "phases": {
                "idle": {"probes": {"ctrl": {"p95": 5.0}}},
                "upload": {"probes": {"ctrl": {"p95": 5.0 + v}}},
            },
            "validation": {"trustworthy": True, "reasons": []},
        })
    return {
        "schema_version": bufferscope.SCHEMA_VERSION,
        "bufferscope_version": bufferscope.VERSION,
        "started_at": "x", "env": {}, "repeat": {"n": len(values)},
        "aggregate": bufferscope.aggregate_runs(runs),
        "validation": {"trustworthy": True, "reasons": []},
    }


class TestRenderAggregate(unittest.TestCase):
    def test_shows_mean_and_interval(self):
        text = bufferscope.render_aggregate(_agg_doc([10.0, 12.0, 11.0, 13.0, 9.0]))
        self.assertIn("ctrl", text)
        self.assertIn("n=5", text)
        text.encode("ascii")


class TestCompareAggregate(CliMixin, unittest.TestCase):
    def _write(self, doc):
        handle = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
        json.dump(doc, handle)
        handle.close()
        return handle.name

    def test_clear_difference_reported_significant(self):
        a = self._write(_agg_doc([10.0, 11.0, 9.0, 10.5, 10.2]))
        b = self._write(_agg_doc([50.0, 51.0, 49.0, 50.5, 50.2]))
        try:
            code, out, _ = self._run(["compare", a, b])
            self.assertEqual(code, 0)
            self.assertIn("SIGNIFICANT", out.upper())
        finally:
            os.unlink(a); os.unlink(b)

    def test_noise_reported_as_not_distinguishable(self):
        a = self._write(_agg_doc([26.0, 16.2, 40.9, 2.3, 1.7]))
        b = self._write(_agg_doc([36.0, 39.8, 12.0, 5.0, 30.0]))
        try:
            code, out, _ = self._run(["compare", a, b])
            self.assertEqual(code, 0)
            self.assertIn("not distinguishable", out.lower())
        finally:
            os.unlink(a); os.unlink(b)


class TestObservedThroughput(CliMixin, unittest.TestCase):
    """Passive runs must record what traffic was actually flowing.

    Without this, a probe run yields latency with no idea what caused it -
    which invites narrating a scenario the data cannot support.
    """

    def setUp(self):
        self._env = bufferscope.collect_env
        self._nic = bufferscope.read_nic_counters
        bufferscope.collect_env = lambda: {"os": "test", "gateway": None,
                                       "icmp_available": False}
        self._counter = {"n": 0}

        def fake_nic():
            # 12.5 MB/s down, 1.25 MB/s up per 100 ms tick = 1000 / 100 Mbps
            self._counter["n"] += 1
            tick = self._counter["n"]
            return {"eth0": (1_250_000 * tick, 125_000 * tick)}

        bufferscope.read_nic_counters = fake_nic

    def tearDown(self):
        bufferscope.collect_env = self._env
        bufferscope.read_nic_counters = self._nic

    def test_probe_records_observed_throughput(self):
        handle = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
        handle.close()
        try:
            code, _, _ = self._run(
                ["probe", "--duration", "2", "--probe-interval", "100",
                 "--targets", "127.0.0.1", "--out", handle.name])
            # 127.0.0.1 answers nothing, so the run is correctly flagged
            # unreachable. What matters here is that throughput was recorded
            # regardless - the conditions must always be captured.
            self.assertEqual(code, bufferscope.EXIT_UNTRUSTWORTHY)
            with open(handle.name) as fh:
                doc = json.load(fh)
            obs = doc.get("observed_throughput")
            self.assertIsNotNone(obs, "passive run recorded no throughput")
            self.assertGreater(obs["down_mbps"], 0.0)
            self.assertGreater(obs["up_mbps"], 0.0)
            self.assertGreater(obs["seconds"], 0.0)
        finally:
            os.unlink(handle.name)


# --------------------------------------------------------------------------
# SNMP CODEC  (pure BER encode/decode - no network)
# --------------------------------------------------------------------------


class TestBerEncoding(unittest.TestCase):
    def test_short_form_length(self):
        self.assertEqual(bufferscope._ber_len(5), bytes([5]))
        self.assertEqual(bufferscope._ber_len(127), bytes([127]))

    def test_long_form_length(self):
        self.assertEqual(bufferscope._ber_len(128), bytes([0x81, 0x80]))
        self.assertEqual(bufferscope._ber_len(300), bytes([0x82, 0x01, 0x2C]))

    def test_integer_encoding(self):
        self.assertEqual(bufferscope._ber_int(0), bytes([0x02, 0x01, 0x00]))
        self.assertEqual(bufferscope._ber_int(1), bytes([0x02, 0x01, 0x01]))
        # 128 needs a leading zero byte so it is not read as negative.
        self.assertEqual(bufferscope._ber_int(128), bytes([0x02, 0x02, 0x00, 0x80]))

    def test_oid_encoding_first_two_arcs_are_combined(self):
        out = bufferscope._ber_oid("1.3.6.1.2.1.1.1.0")
        self.assertEqual(out[0], 0x06)
        self.assertEqual(out[2], 0x2B)  # 1*40 + 3

    def test_oid_encoding_multibyte_arc(self):
        # 7367 (DrayTek enterprise number) exceeds 127 and must use base-128
        # continuation bytes rather than a single octet.
        out = bufferscope._ber_oid("1.3.6.1.4.1.7367")
        # 7367 >> 7 = 57 -> 0xB9 with the continuation bit; 7367 & 0x7F = 71
        # -> 0x47 with the bit clear, because it is the final byte.
        self.assertIn(bytes([0xB9, 0x47]), out)

    def test_oid_round_trip(self):
        for oid in ("1.3.6.1.2.1.1.1.0", "1.3.6.1.4.1.7367.1.2",
                    "1.3.6.1.2.1.31.1.1.1.6.2"):
            encoded = bufferscope._ber_oid(oid)
            tag, raw, _ = bufferscope._ber_read(encoded, 0)
            self.assertEqual(tag, 0x06)
            self.assertEqual(bufferscope._decode_oid(raw), oid)


class TestSnmpMessages(unittest.TestCase):
    def test_get_request_round_trips_through_our_own_parser(self):
        msg = bufferscope.snmp_build_request("mycommunity",
                                         ["1.3.6.1.2.1.1.1.0"], 4242)
        parsed = bufferscope.snmp_parse_message(msg)
        self.assertEqual(parsed["community"], "mycommunity")
        self.assertEqual(parsed["request_id"], 4242)
        self.assertEqual(parsed["pdu_tag"], bufferscope.PDU_GET)
        self.assertEqual([o for o, _ in parsed["varbinds"]],
                         ["1.3.6.1.2.1.1.1.0"])

    def test_getnext_uses_a_different_pdu_tag(self):
        msg = bufferscope.snmp_build_request("c", ["1.3.6.1.2.1.2.2.1.2"], 1,
                                         next_request=True)
        self.assertEqual(bufferscope.snmp_parse_message(msg)["pdu_tag"],
                         bufferscope.PDU_GETNEXT)

    def test_parses_counter64_value(self):
        # ifHCInOctets = 2**33, beyond anything a 32-bit counter could hold.
        big = 2 ** 33
        varbind = bufferscope._ber_tlv(0x30,
                                   bufferscope._ber_oid("1.3.6.1.2.1.31.1.1.1.6.2")
                                   + bufferscope._ber_tlv(0x46, big.to_bytes(8, "big")))
        pdu = bufferscope._ber_tlv(bufferscope.PDU_RESPONSE,
                               bufferscope._ber_int(7) + bufferscope._ber_int(0)
                               + bufferscope._ber_int(0)
                               + bufferscope._ber_tlv(0x30, varbind))
        msg = bufferscope._ber_tlv(0x30, bufferscope._ber_int(1)
                               + bufferscope._ber_tlv(0x04, b"public") + pdu)
        parsed = bufferscope.snmp_parse_message(msg)
        self.assertEqual(parsed["varbinds"][0][1], big)
        self.assertEqual(parsed["error_status"], 0)

    def test_error_status_is_surfaced(self):
        pdu = bufferscope._ber_tlv(bufferscope.PDU_RESPONSE,
                               bufferscope._ber_int(1) + bufferscope._ber_int(2)
                               + bufferscope._ber_int(1)
                               + bufferscope._ber_tlv(0x30, b""))
        msg = bufferscope._ber_tlv(0x30, bufferscope._ber_int(1)
                               + bufferscope._ber_tlv(0x04, b"x") + pdu)
        self.assertEqual(bufferscope.snmp_parse_message(msg)["error_status"], 2)

    def test_malformed_input_returns_none_never_raises(self):
        for bad in (b"", bytes([0]), bytes([0x30, 0x84, 0xFF, 0xFF, 0xFF, 0xFF]),
                    b"garbage"):
            self.assertIsNone(bufferscope.snmp_parse_message(bad))


class TestSnmpWalkLogic(unittest.TestCase):
    def test_stops_when_oid_leaves_the_subtree(self):
        base = "1.3.6.1.2.1.31.1.1.1.6"
        self.assertFalse(bufferscope._oid_in_subtree("1.3.6.1.2.1.31.1.1.1.10.1", base))
        self.assertTrue(bufferscope._oid_in_subtree(base + ".1", base))
        self.assertTrue(bufferscope._oid_in_subtree(base + ".2", base))

    def test_prefix_match_is_arc_aware_not_string_aware(self):
        # "...1.60" must NOT count as inside "...1.6"
        self.assertFalse(bufferscope._oid_in_subtree("1.3.6.1.2.1.31.1.1.1.60",
                                                 "1.3.6.1.2.1.31.1.1.1.6"))


class TestCoarseThroughput(unittest.TestCase):
    """Router SNMP counters refresh in steps, not continuously.

    A DrayTek Vigor 2865 was observed refreshing every ~2 seconds, so naive
    first-to-last integration over a short window undercounts badly. These
    tests pin the behaviour that avoids that.
    """

    def _stepped(self, rate_bytes_per_s, step, ticks, dt=0.25):
        """Counter that only advances every `step` seconds."""
        out = []
        value = 0
        last_step = 0.0
        for i in range(ticks):
            t = i * dt
            if t - last_step >= step:
                value += int(rate_bytes_per_s * (t - last_step))
                last_step = t
            out.append((t, {"WAN2": (value, value // 2)}))
        return out

    def test_naive_window_undercounts_stepped_counters(self):
        samples = self._stepped(125_000_000, 2.0, 28)   # 1000 Mbps, 7 s
        naive_down, _ = bufferscope.throughput_in_window(
            samples, samples[0][0], samples[-1][0])
        coarse_down, _, _ = bufferscope.coarse_throughput_in_window(
            samples, samples[0][0], samples[-1][0])
        self.assertIsNotNone(coarse_down)
        # The transition-aligned figure must be closer to the true 1000 Mbps.
        self.assertLess(abs(coarse_down - 1000.0), abs(naive_down - 1000.0))

    def test_uses_transition_timestamps(self):
        samples = self._stepped(125_000_000, 2.0, 60)   # 15 s
        down, up, transitions = bufferscope.coarse_throughput_in_window(
            samples, samples[0][0], samples[-1][0])
        self.assertGreaterEqual(transitions, 3)
        self.assertAlmostEqual(down, 1000.0, delta=60.0)
        self.assertAlmostEqual(up, 500.0, delta=30.0)

    def test_too_few_transitions_returns_none(self):
        samples = self._stepped(125_000_000, 2.0, 8)    # 2 s, one transition
        down, up, transitions = bufferscope.coarse_throughput_in_window(
            samples, samples[0][0], samples[-1][0])
        self.assertIsNone(down)
        self.assertIsNone(up)
        self.assertLess(transitions, 3)

    def test_empty_input_is_safe(self):
        self.assertEqual(bufferscope.coarse_throughput_in_window([], 0.0, 5.0),
                         (None, None, 0))


class TestOutlierExclusion(unittest.TestCase):
    """A run where the whole line was slow passes the saturation ratio test.

    Saturation compares NIC throughput against what Ookla reported in the same
    run, so if both are low the ratio looks healthy. An arm of eight runs at
    ~950 Mbps containing one at 181 Mbps is not measuring the same conditions,
    and averaging it in destroys the confidence interval.
    """

    def _run(self, dl, ul, up_p95=30.0):
        return {
            "speedtest": {"download_mbps": dl, "upload_mbps": ul},
            "phases": {
                "idle": {"probes": {"ctrl": {"p95": 5.0}}},
                "upload": {"probes": {"ctrl": {"p95": 5.0 + up_p95}}},
            },
            "validation": {"trustworthy": True, "reasons": []},
        }

    def test_slow_outlier_is_excluded(self):
        runs = [self._run(950.0, 866.0) for _ in range(7)]
        runs.append(self._run(181.0, 304.0))
        agg = bufferscope.aggregate_runs(runs)
        self.assertEqual(agg["included_runs"], 7)
        self.assertEqual(agg["excluded_runs"], 1)
        self.assertTrue(any("outlier" in r.lower() for r in agg["exclusions"]))

    def test_normal_variation_is_kept(self):
        runs = [self._run(v, 866.0) for v in (949.0, 951.0, 938.0, 947.0, 950.0)]
        agg = bufferscope.aggregate_runs(runs)
        self.assertEqual(agg["included_runs"], 5)
        self.assertEqual(agg["excluded_runs"], 0)

    def test_outlier_rule_needs_enough_runs_to_have_a_median(self):
        # With two runs there is no basis for calling either one anomalous.
        agg = bufferscope.aggregate_runs([self._run(950.0, 866.0),
                                      self._run(181.0, 304.0)])
        self.assertEqual(agg["included_runs"], 2)

    def test_confidence_interval_tightens_once_the_outlier_is_gone(self):
        clean = [self._run(950.0, 866.0) for _ in range(7)]
        agg = bufferscope.aggregate_runs(clean + [self._run(181.0, 304.0)])
        stats = agg["speedtest"]["upload_mbps"]
        self.assertAlmostEqual(stats["mean"], 866.0, places=1)


class TestCompareReaggregates(CliMixin, unittest.TestCase):
    def test_compare_recomputes_aggregates_from_stored_runs(self):
        """Arms measured under older aggregation rules must still compare.

        Storing raw runs means aggregation rules can improve without
        invalidating measurements already taken.
        """
        def doc(values):
            runs = [{
                "speedtest": {"download_mbps": 950.0, "upload_mbps": 866.0},
                "phases": {
                    "idle": {"probes": {"ctrl": {"p95": 5.0}}},
                    "upload": {"probes": {"ctrl": {"p95": 5.0 + v}}},
                },
                "validation": {"trustworthy": True, "reasons": []},
            } for v in values]
            return {"schema_version": bufferscope.SCHEMA_VERSION,
                    "bufferscope_version": bufferscope.VERSION, "started_at": "x",
                    "env": {}, "repeat": {"n": len(values)}, "runs": runs,
                    "aggregate": {"included_runs": 0, "excluded_runs": 0,
                                  "speedtest": {}},
                    "validation": {"trustworthy": True, "reasons": []}}

        paths = []
        for values in ([10.0, 11.0, 9.0, 10.5, 10.2],
                       [50.0, 51.0, 49.0, 50.5, 50.2]):
            h = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
            json.dump(doc(values), h)
            h.close()
            paths.append(h.name)
        try:
            code, out, _ = self._run(["compare"] + paths)
            self.assertEqual(code, 0)
            # The stale stored aggregate was empty; recomputation must fill it.
            self.assertIn("SIGNIFICANT", out.upper())
        finally:
            for p in paths:
                os.unlink(p)


class TestSpeedtestErrorSurfacing(unittest.TestCase):
    def test_validation_reason_includes_the_speedtest_error(self):
        out = bufferscope.validate_run({
            "mode": "bufferbloat",
            "speedtest_error": "Limit reached: Too many requests received.",
            "phases": {"idle": {"probes": {"tcp": {"n": 100, "p95": 5.0}}}},
        })
        self.assertFalse(out["trustworthy"])
        joined = " ".join(out["reasons"]).lower()
        self.assertIn("limit reached", joined)

    def test_missing_error_still_reports_the_generic_reason(self):
        out = bufferscope.validate_run({
            "mode": "bufferbloat",
            "phases": {"idle": {"probes": {"tcp": {"n": 100, "p95": 5.0}}}},
        })
        self.assertFalse(out["trustworthy"])
        self.assertTrue(any("loaded phase" in r.lower() for r in out["reasons"]))


class TestRepeatFailFast(CliMixin, unittest.TestCase):
    """Repeating a run that fails identically wastes minutes for nothing."""

    def setUp(self):
        self._find = bufferscope.find_speedtest
        self._run_st = bufferscope.run_speedtest
        self._env = bufferscope.collect_env
        bufferscope.find_speedtest = lambda: "/fake/speedtest"
        bufferscope.collect_env = lambda: {"os": "test", "gateway": None,
                                       "icmp_available": False}
        self.calls = {"n": 0}

        def always_fails(binary, server_id, on_event, origin, **kwargs):
            self.calls["n"] += 1
            sink = kwargs.get("error_sink")
            if sink is not None:
                sink.append("Limit reached: Too many requests received.")
            return []

        bufferscope.run_speedtest = always_fails

    def tearDown(self):
        bufferscope.find_speedtest = self._find
        bufferscope.run_speedtest = self._run_st
        bufferscope.collect_env = self._env

    def test_stops_after_consecutive_failures(self):
        code, _, err = self._run(
            ["bufferbloat", "--repeat", "8", "--baseline-secs", "0",
             "--probe-interval", "50", "--targets", "127.0.0.1", "--quiet"])
        self.assertEqual(code, bufferscope.EXIT_ERROR)
        self.assertLess(self.calls["n"], 8,
                        "kept running after repeated identical failures")
        self.assertIn("limit reached", err.lower())


class TestRawSamples(CliMixin, unittest.TestCase):
    """The schema promised a full sample series; only summaries were stored.

    Without raw samples a result cannot be re-analysed - for instance, bucketing
    latency against a router's throughput timeline rather than the load
    generator's own phase claims.
    """

    def setUp(self):
        self._env = bufferscope.collect_env
        bufferscope.collect_env = lambda: {"os": "test", "gateway": None,
                                       "icmp_available": False}

    def tearDown(self):
        bufferscope.collect_env = self._env

    def _probe_run(self, extra):
        handle = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
        handle.close()
        try:
            self._run(["probe", "--duration", "1", "--probe-interval", "100",
                       "--targets", "127.0.0.1", "--out", handle.name] + extra)
            with open(handle.name) as fh:
                return json.load(fh)
        finally:
            os.unlink(handle.name)

    def test_raw_flag_stores_the_sample_series(self):
        doc = self._probe_run(["--raw"])
        probes = doc["phases"]["idle"]["probes"]
        self.assertTrue(probes)
        for name, stats in probes.items():
            self.assertIn("samples", stats, "%s has no raw samples" % name)
            self.assertTrue(stats["samples"])
            stamp, value = stats["samples"][0]
            self.assertIsInstance(stamp, float)
            self.assertTrue(value is None or isinstance(value, float))

    def test_samples_omitted_by_default(self):
        doc = self._probe_run([])
        for stats in doc["phases"]["idle"]["probes"].values():
            self.assertNotIn("samples", stats)

    def test_raw_samples_are_json_round_trippable(self):
        doc = self._probe_run(["--raw"])
        json.loads(json.dumps(doc))


class TestDefaultControlProbe(unittest.TestCase):
    def test_preset_control_is_not_a_tcp_connect_probe(self):
        """TCP-connect probes to shared public endpoints are unstable.

        tcp:1.1.1.1:853 produced 1030 ms outliers on three separate runs that
        no other probe saw, and its p95 shifted between arms, which made it
        useless as a control.
        """
        specs = [spec for spec, _ in bufferscope.CLASS_PRESET]
        controls = [s for s in specs if "control" in s]
        self.assertTrue(controls)
        for spec in controls:
            self.assertFalse(spec.startswith("tcp:"),
                             "control probe %s uses an unstable transport" % spec)

    def test_preset_still_has_a_control_and_classified_probes(self):
        labels = [s.split("#", 1)[1] for s, _ in bufferscope.CLASS_PRESET]
        self.assertTrue(any("control" in l for l in labels))
        self.assertTrue(any("dns" in l for l in labels))


class TestRouterSamplePersistence(unittest.TestCase):
    """--raw must also keep the router counter series.

    Without it a passive run cannot be segmented by what the WAN actually
    carried, which is the only neutral referee when the load generator
    publishes no phase events of its own.
    """

    def test_router_throughput_helper_exposes_series(self):
        samples = [(0.0, {"WAN2": (0, 0)}),
                   (1.0, {"WAN2": (125_000_000, 12_500_000)}),
                   (2.0, {"WAN2": (250_000_000, 25_000_000)}),
                   (3.0, {"WAN2": (375_000_000, 37_500_000)})]

        class FakeSampler:
            def samples(self_inner):
                return samples

        out = bufferscope.router_throughput(FakeSampler(), keep_samples=True)
        self.assertIn("samples", out)
        self.assertEqual(len(out["samples"]), 4)
        stamp, counters = out["samples"][0]
        self.assertIsInstance(stamp, float)
        self.assertIn("WAN2", counters)

    def test_series_omitted_by_default(self):
        samples = [(0.0, {"WAN2": (0, 0)}), (1.0, {"WAN2": (10, 10)}),
                   (2.0, {"WAN2": (20, 20)}), (3.0, {"WAN2": (30, 30)})]

        class FakeSampler:
            def samples(self_inner):
                return samples

        out = bufferscope.router_throughput(FakeSampler())
        self.assertNotIn("samples", out)


class TestPathRedaction(unittest.TestCase):
    """Result files get pasted into bug reports and chats.

    The README tells people to share `env` output, so it must not carry the
    operator's username in a filesystem path.
    """

    def test_home_directory_is_replaced_with_tilde(self):
        home = os.path.expanduser("~")
        path = os.path.join(home, "AppData", "Local", "speedtest.exe")
        out = bufferscope.redact_path(path)
        self.assertTrue(out.startswith("~"), out)
        self.assertNotIn(os.path.basename(home), out)
        self.assertIn("speedtest.exe", out)

    def test_paths_outside_home_are_untouched(self):
        self.assertEqual(bufferscope.redact_path("/usr/bin/speedtest"),
                         "/usr/bin/speedtest")

    def test_none_and_empty_are_safe(self):
        self.assertIsNone(bufferscope.redact_path(None))
        self.assertEqual(bufferscope.redact_path(""), "")

    def test_collect_env_does_not_leak_the_username(self):
        real = bufferscope.find_speedtest
        home = os.path.expanduser("~")
        bufferscope.find_speedtest = lambda: os.path.join(home, "bin", "speedtest.exe")
        try:
            env = bufferscope.collect_env()
        finally:
            bufferscope.find_speedtest = real
        self.assertNotIn(os.path.basename(home), env["speedtest_path"] or "")


class TestRefreshEstimation(unittest.TestCase):
    """Counter refresh rate is a property of the router, so measure it.

    Hardcoding an interval that suited one device would silently produce bad
    numbers on another - a router refreshing every 10 seconds cannot
    characterise a 7-second phase at all, and the tool should know that
    rather than assume otherwise.
    """

    def _stepped(self, step, ticks, dt=0.25):
        out, value, last = [], 0, 0.0
        for i in range(ticks):
            t = i * dt
            if t - last >= step:
                value += 1_000_000
                last = t
            out.append((t, {"wan": (value, value)}))
        return out

    def test_estimates_a_two_second_refresh(self):
        got = bufferscope.estimate_refresh_interval(self._stepped(2.0, 60))
        self.assertAlmostEqual(got, 2.0, delta=0.4)

    def test_estimates_a_ten_second_refresh(self):
        got = bufferscope.estimate_refresh_interval(self._stepped(10.0, 200))
        self.assertAlmostEqual(got, 10.0, delta=1.0)

    def test_continuous_counters_report_the_sampling_interval(self):
        samples = [(i * 0.25, {"wan": (i * 1000, i * 500)}) for i in range(40)]
        got = bufferscope.estimate_refresh_interval(samples)
        self.assertAlmostEqual(got, 0.25, delta=0.1)

    def test_too_few_transitions_returns_none(self):
        self.assertIsNone(bufferscope.estimate_refresh_interval(
            [(0.0, {"wan": (0, 0)}), (1.0, {"wan": (0, 0)})]))
        self.assertIsNone(bufferscope.estimate_refresh_interval([]))

    def test_window_usability_follows_the_measured_refresh(self):
        # A 7 s phase is fine at a 0.5 s refresh, hopeless at 10 s.
        self.assertTrue(bufferscope.window_is_usable(7.0, 0.5))
        self.assertFalse(bufferscope.window_is_usable(7.0, 10.0))
        self.assertTrue(bufferscope.window_is_usable(600.0, 10.0))

    def test_unknown_refresh_is_treated_as_usable(self):
        self.assertTrue(bufferscope.window_is_usable(7.0, None))


# --------------------------------------------------------------------------
# END TO END  (opt-in: needs a real network and the Ookla CLI)
# --------------------------------------------------------------------------


@unittest.skipUnless(os.environ.get("BUFFERSCOPE_E2E") == "1",
                     "set BUFFERSCOPE_E2E=1 to run live network tests")
class TestEndToEnd(unittest.TestCase):
    def test_bufferbloat_run_produces_valid_json(self):
        handle = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
        handle.close()
        try:
            code = bufferscope.main(["bufferbloat", "--baseline-secs", "5",
                                 "--out", handle.name, "--quiet"])
            self.assertIn(code, (bufferscope.EXIT_OK, bufferscope.EXIT_UNTRUSTWORTHY))
            with open(handle.name) as fh:
                doc = json.load(fh)
            self.assertEqual(doc["schema_version"], bufferscope.SCHEMA_VERSION)
            self.assertIn("download", doc["phases"])
            self.assertIn("upload", doc["phases"])
            self.assertIsNotNone(doc["speedtest"]["download_mbps"])
            self.assertIsNotNone(doc["speedtest"]["upload_mbps"])
            self.assertIn(doc["bufferbloat"]["overall_grade"],
                          ["A+", "A", "B", "C", "D", "n/a"])
            # Every phase must actually contain samples, or correlation is broken.
            for phase in ("idle", "download", "upload"):
                total = sum(s["n"] + s["lost"]
                            for s in doc["phases"][phase]["probes"].values())
                self.assertGreater(total, 0, "no samples in %s phase" % phase)
        finally:
            os.unlink(handle.name)


# --------------------------------------------------------------------------
# MULTI-DEVICE SNMP
# --------------------------------------------------------------------------


class FakeSampler:
    """Stands in for RouterSampler. Carries a community so the leak guard
    below is testing something real rather than a vacuous assertion."""

    def __init__(self, samples, community="unit-test-community-value"):
        self._samples = samples
        self.community = community
        self.stopped = False
        self.failures = 0

    def samples(self):
        return list(self._samples)

    def stop(self):
        self.stopped = True


def _counter_samples(mbps_down=100.0, mbps_up=50.0, seconds=10, name="WAN"):
    """Synthetic (stamp, {iface: (in_octets, out_octets)}) series."""
    out = []
    for i in range(seconds + 1):
        down = int(mbps_down * 1e6 / 8 * i)
        up = int(mbps_up * 1e6 / 8 * i)
        out.append((float(i), {name: (down, up)}))
    return out


class TestSnmpDeviceSpec(unittest.TestCase):
    def test_bare_host(self):
        d = bufferscope.parse_snmp_device_spec("192.0.2.1")
        self.assertEqual(d.host, "192.0.2.1")
        self.assertIsNone(d.community_env)
        self.assertIsNone(d.interface)
        self.assertIsNone(d.port)

    def test_label_defaults_to_host(self):
        self.assertEqual(bufferscope.parse_snmp_device_spec("192.0.2.1").label,
                         "192.0.2.1")

    def test_env_key_stores_the_name_only(self):
        d = bufferscope.parse_snmp_device_spec("192.0.2.2,env=SOME_VAR_NAME")
        self.assertEqual(d.community_env, "SOME_VAR_NAME")

    def test_label_key_overrides_default(self):
        d = bufferscope.parse_snmp_device_spec("192.0.2.2,label=upstairs")
        self.assertEqual(d.label, "upstairs")

    def test_iface_and_port(self):
        d = bufferscope.parse_snmp_device_spec("192.0.2.3,iface=LAN,port=1610")
        self.assertEqual(d.interface, "LAN")
        self.assertEqual(d.port, 1610)

    def test_all_keys_together(self):
        d = bufferscope.parse_snmp_device_spec(
            "192.0.2.4,env=V,label=edge,iface=WAN2,port=161")
        self.assertEqual((d.host, d.community_env, d.label, d.interface, d.port),
                         ("192.0.2.4", "V", "edge", "WAN2", 161))

    def test_whitespace_is_tolerated(self):
        d = bufferscope.parse_snmp_device_spec("  192.0.2.5 , env = V , label = x ")
        self.assertEqual((d.host, d.community_env, d.label), ("192.0.2.5", "V", "x"))

    def test_hostname_as_well_as_address(self):
        d = bufferscope.parse_snmp_device_spec("gateway.example.invalid")
        self.assertEqual(d.host, "gateway.example.invalid")

    def test_invalid_specs_raise(self):
        for bad in ("", "   ", ",env=V", "192.0.2.1,nosuchkey=1",
                    "192.0.2.1,env", "192.0.2.1,port=notanumber",
                    "192.0.2.1,port=99999", "192.0.2.1,port=0",
                    "192.0.2.1,env="):
            with self.assertRaises(ValueError, msg="expected reject: %r" % bad):
                bufferscope.parse_snmp_device_spec(bad)

    def test_device_stores_only_the_variable_name_never_the_value(self):
        os.environ["BUFFERSCOPE_TEST_COMMUNITY"] = "s3cret-value"
        try:
            d = bufferscope.parse_snmp_device_spec(
                "192.0.2.9,env=BUFFERSCOPE_TEST_COMMUNITY")
            # __slots__ means there is no __dict__ to inspect, which is
            # itself part of the guarantee: the object cannot accumulate a
            # stray attribute holding a secret.
            blob = repr(d) + json.dumps(
                {f: str(getattr(d, f)) for f in d.__slots__})
            self.assertNotIn("s3cret-value", blob)
            self.assertEqual(d.community_env, "BUFFERSCOPE_TEST_COMMUNITY")
        finally:
            os.environ.pop("BUFFERSCOPE_TEST_COMMUNITY", None)


class TestDeviceCommunityResolution(unittest.TestCase):
    def setUp(self):
        self._saved = {k: os.environ.get(k) for k in
                       ("BUFFERSCOPE_SNMP_COMMUNITY", "BUFFERSCOPE_TEST_DEV")}
        for k in self._saved:
            os.environ.pop(k, None)

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def _args(self, **kw):
        return argparse.Namespace(**kw)

    def test_flag_beats_everything(self):
        os.environ["BUFFERSCOPE_TEST_DEV"] = "from-device-env"
        os.environ["BUFFERSCOPE_SNMP_COMMUNITY"] = "from-global-env"
        d = bufferscope.parse_snmp_device_spec("192.0.2.1,env=BUFFERSCOPE_TEST_DEV")
        self.assertEqual(
            bufferscope.resolve_device_community(d, self._args(snmp_community="flag")),
            "flag")

    def test_device_env_beats_global_env(self):
        os.environ["BUFFERSCOPE_TEST_DEV"] = "from-device-env"
        os.environ["BUFFERSCOPE_SNMP_COMMUNITY"] = "from-global-env"
        d = bufferscope.parse_snmp_device_spec("192.0.2.1,env=BUFFERSCOPE_TEST_DEV")
        self.assertEqual(
            bufferscope.resolve_device_community(d, self._args(snmp_community=None)),
            "from-device-env")

    def test_falls_back_to_global_env(self):
        os.environ["BUFFERSCOPE_SNMP_COMMUNITY"] = "from-global-env"
        d = bufferscope.parse_snmp_device_spec("192.0.2.1")
        self.assertEqual(
            bufferscope.resolve_device_community(d, self._args(snmp_community=None)),
            "from-global-env")

    def test_named_env_var_absent_falls_through_to_global(self):
        os.environ["BUFFERSCOPE_SNMP_COMMUNITY"] = "from-global-env"
        d = bufferscope.parse_snmp_device_spec("192.0.2.1,env=BUFFERSCOPE_TEST_DEV")
        self.assertEqual(
            bufferscope.resolve_device_community(d, self._args(snmp_community=None)),
            "from-global-env")

    def test_defaults_to_public(self):
        d = bufferscope.parse_snmp_device_spec("192.0.2.1")
        self.assertEqual(
            bufferscope.resolve_device_community(d, self._args(snmp_community=None)),
            "public")


class TestBuildSnmpDevices(unittest.TestCase):
    def test_router_snmp_alone_still_works(self):
        args = argparse.Namespace(router_snmp="192.0.2.1", snmp_device=None)
        devices = bufferscope.build_snmp_devices(args)
        self.assertEqual([d.host for d in devices], ["192.0.2.1"])

    def test_snmp_device_flags(self):
        args = argparse.Namespace(
            router_snmp=None,
            snmp_device=["192.0.2.1", "192.0.2.2,label=ap,env=V"])
        devices = bufferscope.build_snmp_devices(args)
        self.assertEqual([d.label for d in devices], ["192.0.2.1", "ap"])

    def test_router_snmp_is_not_duplicated(self):
        args = argparse.Namespace(router_snmp="192.0.2.1",
                                  snmp_device=["192.0.2.1,label=edge"])
        devices = bufferscope.build_snmp_devices(args)
        self.assertEqual(len(devices), 1)
        self.assertEqual(devices[0].label, "edge")

    def test_router_snmp_leads_when_both_given(self):
        args = argparse.Namespace(router_snmp="192.0.2.1",
                                  snmp_device=["192.0.2.2,label=ap"])
        devices = bufferscope.build_snmp_devices(args)
        self.assertEqual([d.host for d in devices], ["192.0.2.1", "192.0.2.2"])

    def test_no_targets_gives_empty_list(self):
        args = argparse.Namespace(router_snmp=None, snmp_device=None)
        self.assertEqual(bufferscope.build_snmp_devices(args), [])


class TestSnmpDeviceGroup(unittest.TestCase):
    def _group(self):
        a = bufferscope.parse_snmp_device_spec("192.0.2.1,label=edge")
        b = bufferscope.parse_snmp_device_spec("192.0.2.2,label=ap")
        return bufferscope.SnmpDeviceGroup([
            (a, FakeSampler(_counter_samples(100.0, 50.0, name="WAN"))),
            (b, FakeSampler(_counter_samples(20.0, 10.0, name="LAN"))),
        ])

    def test_samples_proxy_to_the_primary_device(self):
        # router_throughput() takes anything with .samples(), so a group must
        # behave like the single sampler it replaces.
        group = self._group()
        self.assertEqual(group.samples(), group.entries[0][1].samples())

    def test_router_throughput_still_works_on_a_group(self):
        report = bufferscope.router_throughput(self._group())
        self.assertIsNotNone(report)
        self.assertEqual(report["source"], "snmp")
        self.assertAlmostEqual(report["down_mbps"], 100.0, delta=1.0)

    def test_device_reports_one_entry_per_device(self):
        reports = self._group().device_reports()
        self.assertEqual([r["label"] for r in reports], ["edge", "ap"])
        self.assertEqual([r["host"] for r in reports],
                         ["192.0.2.1", "192.0.2.2"])

    def test_device_reports_carry_throughput(self):
        reports = self._group().device_reports()
        self.assertAlmostEqual(reports[0]["down_mbps"], 100.0, delta=1.0)
        self.assertAlmostEqual(reports[1]["down_mbps"], 20.0, delta=1.0)

    def test_stop_stops_every_sampler(self):
        group = self._group()
        group.stop()
        self.assertTrue(all(s.stopped for _, s in group.entries))

    def test_empty_group_is_falsy_and_safe(self):
        group = bufferscope.SnmpDeviceGroup([])
        self.assertFalse(group)
        self.assertEqual(group.device_reports(), [])
        self.assertIsNone(bufferscope.router_throughput(group))


class TestNoCommunityLeak(unittest.TestCase):
    def test_community_never_reaches_a_serialised_report(self):
        secret = "unit-test-community-value"
        device = bufferscope.parse_snmp_device_spec("192.0.2.1,env=SOME_VAR")
        sampler = FakeSampler(_counter_samples(), community=secret)
        group = bufferscope.SnmpDeviceGroup([(device, sampler)])
        blob = json.dumps({
            "devices": group.device_reports(keep_samples=True),
            "router_throughput": bufferscope.router_throughput(group,
                                                           keep_samples=True),
        })
        self.assertNotIn(secret, blob)
        self.assertNotIn("community", blob)


# --------------------------------------------------------------------------
# COUNTER32 FALLBACK
#
# Cheap access points often implement only the 32-bit ifInOctets/ifOutOctets
# counters. Those wrap about every 34 seconds at gigabit, which is why the
# 64-bit ifHC counters are preferred - but a device that lacks them is
# perfectly measurable as long as the wrap is corrected between samples.
# --------------------------------------------------------------------------


class TestUnwrapDelta(unittest.TestCase):
    def test_normal_increase(self):
        self.assertEqual(bufferscope.unwrap_delta(100, 350, bufferscope.COUNTER32_MODULUS), 250)

    def test_no_change(self):
        self.assertEqual(bufferscope.unwrap_delta(100, 100, bufferscope.COUNTER32_MODULUS), 0)

    def test_single_wrap_is_corrected(self):
        m = bufferscope.COUNTER32_MODULUS
        # 1000 bytes before the wrap, 500 after it
        self.assertEqual(bufferscope.unwrap_delta(m - 1000, 500, m), 1500)

    def test_wrap_at_exact_boundary(self):
        m = bufferscope.COUNTER32_MODULUS
        self.assertEqual(bufferscope.unwrap_delta(m - 1, 0, m), 1)

    def test_counter_reset_is_reported_as_zero_not_as_a_spike(self):
        # A device reboot sets the counter to a small value. Adding a modulus
        # would invent ~4 GB of traffic that never happened, so an implausible
        # delta must be discarded rather than reported.
        m = bufferscope.COUNTER32_MODULUS
        self.assertEqual(bufferscope.unwrap_delta(3_000_000_000, 5, m, max_delta=10_000_000), 0)

    def test_plausible_wrap_survives_the_max_delta_guard(self):
        m = bufferscope.COUNTER32_MODULUS
        self.assertEqual(bufferscope.unwrap_delta(m - 1000, 500, m, max_delta=10_000_000), 1500)

    def test_64bit_counters_use_their_own_modulus(self):
        m = bufferscope.COUNTER64_MODULUS
        self.assertEqual(bufferscope.unwrap_delta(m - 10, 5, m), 15)


class TestCounterReadings(unittest.TestCase):
    """snmp_counter_readings must prefer 64-bit counters and fall back to the
    32-bit pair on devices that do not implement them, reporting which modulus
    applies so a wrap can be corrected later."""

    def _fake_query(self, hc_values, legacy_values):
        """Stand in for snmp_query, answering by which OID subtree is asked."""
        def query(host, community, oids, next_request=False, timeout=2.0, port=161):
            values = hc_values if oids[0].startswith(bufferscope.OID_IF_HC_IN) else legacy_values
            if values is None:
                return None
            return {"error_status": 0,
                    "varbinds": list(zip(oids, values))}
        return query

    def _run(self, hc_values, legacy_values):
        original = bufferscope.snmp_query
        bufferscope.snmp_query = self._fake_query(hc_values, legacy_values)
        try:
            return bufferscope.snmp_counter_readings("192.0.2.1", "community", [3])
        finally:
            bufferscope.snmp_query = original

    def test_prefers_64bit_when_available(self):
        out = self._run([111, 222], [7, 8])
        self.assertEqual(out[3], (111, 222, bufferscope.COUNTER64_MODULUS))

    def test_falls_back_to_32bit_when_hc_returns_null(self):
        # Some access points answer ifHCInOctets with a bare NULL rather
        # than noSuchObject, which the parser surfaces as None.
        out = self._run([None, None], [4321, 8765])
        self.assertEqual(out[3], (4321, 8765, bufferscope.COUNTER32_MODULUS))

    def test_falls_back_when_hc_reports_no_such_object(self):
        out = self._run(["noSuchObject", "noSuchObject"], [10, 20])
        self.assertEqual(out[3], (10, 20, bufferscope.COUNTER32_MODULUS))

    def test_falls_back_when_hc_request_times_out(self):
        out = self._run(None, [10, 20])
        self.assertEqual(out[3], (10, 20, bufferscope.COUNTER32_MODULUS))

    def test_index_absent_when_neither_counter_exists(self):
        self.assertEqual(self._run([None, None], [None, None]), {})

    def test_snmp_counters_keeps_its_two_tuple_contract(self):
        # Existing callers must be unaffected by the added modulus.
        original = bufferscope.snmp_query
        bufferscope.snmp_query = self._fake_query([111, 222], [7, 8])
        try:
            self.assertEqual(bufferscope.snmp_counters("192.0.2.1", "c", [3]), {3: (111, 222)})
        finally:
            bufferscope.snmp_query = original


class TestCounterAccumulator(unittest.TestCase):
    """throughput_in_window subtracts the first sample from the last, so a
    counter that wraps mid-window would read as a decrease and be clamped to
    zero. The accumulator converts raw readings into monotonic totals first."""

    def test_first_reading_is_the_zero_point(self):
        acc = bufferscope.CounterAccumulator()
        self.assertEqual(acc.update({1: (5000, 9000, bufferscope.COUNTER32_MODULUS)}),
                         {1: (0, 0)})

    def test_totals_accumulate(self):
        acc = bufferscope.CounterAccumulator()
        m = bufferscope.COUNTER32_MODULUS
        acc.update({1: (1000, 2000, m)})
        self.assertEqual(acc.update({1: (1500, 2200, m)}), {1: (500, 200)})
        self.assertEqual(acc.update({1: (1800, 2600, m)}), {1: (800, 600)})

    def test_wrap_keeps_the_total_rising(self):
        acc = bufferscope.CounterAccumulator()
        m = bufferscope.COUNTER32_MODULUS
        acc.update({1: (m - 1000, m - 500, m)})
        self.assertEqual(acc.update({1: (500, 1500, m)}), {1: (1500, 2000)})

    def test_wrap_does_not_look_like_a_decrease(self):
        acc = bufferscope.CounterAccumulator()
        m = bufferscope.COUNTER32_MODULUS
        first = acc.update({1: (m - 100, m - 100, m)})[1][0]
        second = acc.update({1: (900, 900, m)})[1][0]
        self.assertGreater(second, first)

    def test_reset_is_absorbed_rather_than_reported_as_a_spike(self):
        acc = bufferscope.CounterAccumulator(max_delta=1_000_000)
        m = bufferscope.COUNTER32_MODULUS
        acc.update({1: (3_000_000_000, 3_000_000_000, m)})
        self.assertEqual(acc.update({1: (5, 5, m)}), {1: (0, 0)})

    def test_independent_interfaces_do_not_interfere(self):
        acc = bufferscope.CounterAccumulator()
        m = bufferscope.COUNTER64_MODULUS
        acc.update({1: (100, 100, m), 2: (5000, 5000, m)})
        self.assertEqual(acc.update({1: (150, 160, m), 2: (5001, 5002, m)}),
                         {1: (50, 60), 2: (1, 2)})

    def test_interface_appearing_late_starts_from_its_own_zero(self):
        acc = bufferscope.CounterAccumulator()
        m = bufferscope.COUNTER64_MODULUS
        acc.update({1: (100, 100, m)})
        out = acc.update({1: (200, 200, m), 2: (77, 88, m)})
        self.assertEqual(out[2], (0, 0))
        self.assertEqual(out[1], (100, 100))


class TestRouterSamplerWrapHandling(unittest.TestCase):
    """A 32-bit counter wraps every ~34 s at gigabit. A sampler that stored
    raw readings would report that window as zero throughput."""

    def _sampler_over(self, readings_series, names={1: "LAN"}):
        original = bufferscope.snmp_counter_readings
        series = list(readings_series)
        def fake(host, community, indexes, timeout=2.0, port=161):
            return series.pop(0) if series else {}
        bufferscope.snmp_counter_readings = fake
        try:
            sampler = bufferscope.RouterSampler("192.0.2.1", "community", [1],
                                            names, origin=0.0)
            for i in range(len(readings_series)):
                sampler.poll_once(stamp=float(i))
            return sampler
        finally:
            bufferscope.snmp_counter_readings = original

    def test_counters_rise_across_a_wrap(self):
        m = bufferscope.COUNTER32_MODULUS
        s = self._sampler_over([{1: (m - 1000, 0, m)}, {1: (500, 0, m)}])
        values = [snap["LAN"][0] for _stamp, snap in s.samples()]
        self.assertEqual(values, [0, 1500])

    def test_throughput_across_a_wrap_is_measured_not_lost(self):
        # 100 Mbps = 12.5 MB/s. Three one-second samples straddling a wrap.
        m = bufferscope.COUNTER32_MODULUS
        step = 12_500_000
        start = m - step // 2          # wraps between sample 1 and 2
        readings = [{1: ((start + i * step) % m, 0, m)} for i in range(3)]
        s = self._sampler_over(readings)
        down, _up = bufferscope.throughput_in_window(s.samples(), 0.0, 2.0)
        self.assertAlmostEqual(down, 100.0, delta=0.1)

    def test_64bit_devices_are_unaffected(self):
        m = bufferscope.COUNTER64_MODULUS
        s = self._sampler_over([{1: (1_000, 0, m)}, {1: (1_000 + 12_500_000, 0, m)}])
        down, _up = bufferscope.throughput_in_window(s.samples(), 0.0, 1.0)
        self.assertAlmostEqual(down, 100.0, delta=0.1)


if __name__ == "__main__":
    unittest.main()


# --------------------------------------------------------------------------
# PARTIAL RESULTS ON ABORT
# --------------------------------------------------------------------------


class TestAbortSavesPartialResults(unittest.TestCase):
    """A rate-limit abort must not discard the runs already completed.

    Losing nine good runs because the tenth hit a server limit costs the whole
    arm and cannot be recovered without re-measuring.
    """

    def setUp(self):
        self._real_measure_once = bufferscope._measure_once
        self.addCleanup(setattr, bufferscope, "_measure_once", self._real_measure_once)

    @staticmethod
    def _good_run(upload_mbps=900.0):
        return {
            "env": {"os": "test"},
            "validation": {"trustworthy": True, "reasons": []},
            "speedtest": {"upload_mbps": upload_mbps, "download_mbps": 940.0,
                          "idle_latency_ms": 4.0, "packet_loss_pct": 0},
            "phases": {
                "idle": {"probes": {"ctrl": {"p95": 4.0, "n": 50}}},
                "upload": {"probes": {"ctrl": {"p95": 30.0, "n": 50}}},
            },
        }

    @staticmethod
    def _failed_run():
        return {
            "env": {"os": "test"},
            "validation": {"trustworthy": False, "reasons": ["speedtest failed"]},
            "speedtest_error": "Limit reached",
            "phases": {},
        }

    def _args(self, out):
        return argparse.Namespace(command="bufferbloat", repeat=10, quiet=True,
                                  out=out, json=False)

    def _run_with(self, sequence, out):
        calls = {"n": 0}

        def fake(args):
            index = calls["n"]
            calls["n"] += 1
            return sequence[index]

        bufferscope._measure_once = fake
        with contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
            code = bufferscope.cmd_measure(self._args(out))
        return code

    def test_aborted_arm_still_writes_completed_runs(self):
        sequence = [self._good_run(900.0), self._good_run(901.0),
                    self._failed_run(), self._failed_run()]
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "arm.json")
            code = self._run_with(sequence, out)
            self.assertEqual(code, bufferscope.EXIT_ERROR)
            self.assertTrue(os.path.exists(out),
                            "aborting discarded the completed runs")
            doc = json.load(open(out))
            self.assertEqual(doc["aggregate"]["included_runs"], 2)

    def test_aborted_doc_records_the_abort_reason(self):
        sequence = [self._good_run(), self._failed_run(), self._failed_run()]
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "arm.json")
            self._run_with(sequence, out)
            doc = json.load(open(out))
            self.assertFalse(doc["validation"]["trustworthy"])
            self.assertTrue(
                any("Limit reached" in r for r in doc["validation"]["reasons"]),
                "abort reason not recorded in validation: %r"
                % (doc["validation"]["reasons"],))

    def test_aborted_doc_records_attempted_and_completed_counts(self):
        sequence = [self._good_run(), self._failed_run(), self._failed_run()]
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "arm.json")
            self._run_with(sequence, out)
            doc = json.load(open(out))
            self.assertEqual(doc["repeat"]["n"], 10, "planned repeats")
            self.assertEqual(doc["repeat"]["completed"], 3, "runs attempted")
            self.assertTrue(doc["repeat"]["aborted"])

    def test_normal_completion_is_not_marked_aborted(self):
        sequence = [self._good_run(900.0 + i) for i in range(10)]
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "arm.json")
            code = self._run_with(sequence, out)
            self.assertEqual(code, bufferscope.EXIT_OK)
            doc = json.load(open(out))
            self.assertFalse(doc["repeat"].get("aborted", False))
            self.assertEqual(doc["aggregate"]["included_runs"], 10)

    def test_abort_on_the_first_two_runs_does_not_crash(self):
        # Rate limiting can strike immediately, leaving nothing worth
        # aggregating. Rendering must still survive and write the document.
        sequence = [self._failed_run(), self._failed_run()]
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "arm.json")
            code = self._run_with(sequence, out)
            self.assertEqual(code, bufferscope.EXIT_ERROR)
            doc = json.load(open(out))
            self.assertEqual(doc["aggregate"]["included_runs"], 0)
            self.assertEqual(doc["repeat"]["completed"], 2)


# --------------------------------------------------------------------------
# LIBRESPEED GENERATOR
# --------------------------------------------------------------------------


# Captured verbatim from librespeed-cli 1.0.14 --json --no-upload.
LIBRESPEED_DOWNLOAD_JSON = (
    '[{"timestamp":"2026-08-24T21:03:22.9390035+01:00",'
    '"server":{"name":"London, England (Clouvider)",'
    '"url":"https://lon.speedtest.clouvider.net/backend"},'
    '"client":{"ip":"","hostname":"","city":""},'
    '"bytes_sent":0,"bytes_received":1777276739,'
    '"ping":3.59,"jitter":0.56,"upload":0,"download":911.35,"share":""}]'
)

LIBRESPEED_UPLOAD_JSON = (
    '[{"timestamp":"2026-08-24T21:05:10.1000000+01:00",'
    '"server":{"name":"London, England (Clouvider)",'
    '"url":"https://lon.speedtest.clouvider.net/backend"},'
    '"client":{"ip":""},'
    '"bytes_sent":1600000000,"bytes_received":0,'
    '"ping":4.10,"jitter":0.61,"upload":903.20,"download":0,'
    '"share":"https://libre.sh/x.png"}]'
)


class TestParseLibrespeedJson(unittest.TestCase):
    def test_parses_real_download_output(self):
        out = bufferscope.parse_librespeed_json(LIBRESPEED_DOWNLOAD_JSON)
        self.assertAlmostEqual(out["download_mbps"], 911.35)
        self.assertAlmostEqual(out["idle_latency_ms"], 3.59)
        self.assertEqual(out["server"]["name"], "London, England (Clouvider)")

    def test_empty_share_becomes_none_not_empty_string(self):
        out = bufferscope.parse_librespeed_json(LIBRESPEED_DOWNLOAD_JSON)
        self.assertIsNone(out["result_url"])

    def test_share_url_is_kept_when_present(self):
        out = bufferscope.parse_librespeed_json(LIBRESPEED_UPLOAD_JSON)
        self.assertEqual(out["result_url"], "https://libre.sh/x.png")

    def test_packet_loss_is_none_because_librespeed_does_not_report_it(self):
        out = bufferscope.parse_librespeed_json(LIBRESPEED_DOWNLOAD_JSON)
        self.assertIsNone(out["packet_loss_pct"])

    def test_garbage_yields_all_none_rather_than_raising(self):
        for text in ("", "not json", "[]", "null"):
            out = bufferscope.parse_librespeed_json(text)
            self.assertIsNone(out["download_mbps"], text)
            self.assertIsNone(out["upload_mbps"], text)

    def test_accepts_a_bare_object_as_well_as_a_list(self):
        out = bufferscope.parse_librespeed_json('{"download":500.0,"ping":9.0}')
        self.assertAlmostEqual(out["download_mbps"], 500.0)


class TestMergeLibrespeedResults(unittest.TestCase):
    """Each direction is measured in its own process, so the halves are joined."""

    def _down(self):
        return bufferscope.parse_librespeed_json(LIBRESPEED_DOWNLOAD_JSON)

    def _up(self):
        return bufferscope.parse_librespeed_json(LIBRESPEED_UPLOAD_JSON)

    def test_takes_each_direction_from_its_own_run(self):
        out = bufferscope.merge_librespeed_results(self._down(), self._up())
        self.assertAlmostEqual(out["download_mbps"], 911.35)
        self.assertAlmostEqual(out["upload_mbps"], 903.20)

    def test_zero_from_the_skipped_direction_is_never_used(self):
        # The download run reports upload:0 because it did not run one.
        # Taking that zero would silently report a dead uplink.
        out = bufferscope.merge_librespeed_results(self._down(), self._up())
        self.assertNotEqual(out["upload_mbps"], 0)

    def test_missing_upload_run_leaves_upload_none(self):
        out = bufferscope.merge_librespeed_results(self._down(), None)
        self.assertAlmostEqual(out["download_mbps"], 911.35)
        self.assertIsNone(out["upload_mbps"])

    def test_result_has_exactly_the_canonical_speedtest_keys(self):
        out = bufferscope.merge_librespeed_results(self._down(), self._up())
        self.assertEqual(set(out), set(bufferscope.EMPTY_SPEEDTEST))


class TestRunLibrespeed(unittest.TestCase):
    """librespeed has no phase event stream, so each direction is its own run."""

    def setUp(self):
        self._real_run = bufferscope.subprocess.run
        self.addCleanup(setattr, bufferscope.subprocess, "run", self._real_run)
        self.calls = []

    class _Completed:
        def __init__(self, stdout, stderr="", returncode=0):
            self.stdout, self.stderr, self.returncode = stdout, stderr, returncode

    def _stub(self, down=LIBRESPEED_DOWNLOAD_JSON, up=LIBRESPEED_UPLOAD_JSON):
        def fake(cmd, **kwargs):
            self.calls.append(list(cmd))
            if "--no-download" in cmd:
                return self._Completed(up if up is not None else "")
            return self._Completed(down if down is not None else "")
        bufferscope.subprocess.run = fake

    def _go(self, **kw):
        return bufferscope.run_librespeed("ls-bin", kw.pop("server_id", None),
                                      lambda name: None, time.perf_counter(), **kw)

    def test_runs_download_then_upload_as_separate_processes(self):
        self._stub()
        phases, _ = self._go()
        self.assertEqual([p[0] for p in phases], ["download", "upload"])
        self.assertEqual(len(self.calls), 2)
        self.assertIn("--no-upload", self.calls[0])
        self.assertIn("--no-download", self.calls[1])

    def test_phase_windows_are_ordered_and_non_overlapping(self):
        self._stub()
        phases, _ = self._go()
        (_, d_start, d_end), (_, u_start, u_end) = phases
        self.assertLessEqual(d_start, d_end)
        self.assertLessEqual(d_end, u_start)
        self.assertLessEqual(u_start, u_end)

    def test_each_direction_is_taken_from_its_own_run(self):
        self._stub()
        _, result = self._go()
        self.assertAlmostEqual(result["download_mbps"], 911.35)
        self.assertAlmostEqual(result["upload_mbps"], 903.20)

    def test_server_id_and_tuning_flags_are_passed_through(self):
        self._stub()
        self._go(server_id=42, duration=20, concurrent=12)
        self.assertIn("--server", self.calls[0])
        self.assertIn("42", self.calls[0])
        self.assertIn("20", self.calls[0])
        self.assertIn("12", self.calls[0])

    def test_a_failed_direction_is_reported_and_omitted_not_zeroed(self):
        self._stub(up="")           # upload run produced nothing
        errors = []
        phases, result = bufferscope.run_librespeed(
            "ls-bin", None, lambda n: None, time.perf_counter(),
            error_sink=errors)
        self.assertEqual([p[0] for p in phases], ["download"])
        self.assertIsNone(result["upload_mbps"])
        self.assertTrue(errors, "a failed direction must be reported")


class TestGeneratorProvenance(unittest.TestCase):
    """Comparing arms measured by different load generators is invalid:
    the same line gave inverted up/down latency under Ookla vs Waveform."""

    @staticmethod
    def _doc(generator):
        run = {
            "env": {"os": "test"},
            "validation": {"trustworthy": True, "reasons": []},
            "speedtest": {"upload_mbps": 900.0, "download_mbps": 940.0},
            "phases": {"idle": {"probes": {}}, "upload": {"probes": {}}},
        }
        if generator is not None:
            run["generator"] = generator
        return {"schema_version": bufferscope.SCHEMA_VERSION, "runs": [run, dict(run)],
                "repeat": {"n": 2}}

    def test_reads_generator_from_runs(self):
        self.assertEqual(bufferscope.doc_generator(self._doc("librespeed")), "librespeed")

    def test_legacy_doc_without_the_field_is_ookla(self):
        # Every arm measured before this feature existed was Ookla.
        self.assertEqual(bufferscope.doc_generator(self._doc(None)), "ookla")

    def test_compare_warns_when_generators_differ(self):
        with tempfile.TemporaryDirectory() as tmp:
            a, b = os.path.join(tmp, "a.json"), os.path.join(tmp, "b.json")
            json.dump(self._doc("ookla"), open(a, "w"))
            json.dump(self._doc("librespeed"), open(b, "w"))
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                bufferscope.cmd_compare(argparse.Namespace(file_a=a, file_b=b))
            self.assertIn("different load generators", buf.getvalue())

    def test_compare_is_silent_when_generators_match(self):
        with tempfile.TemporaryDirectory() as tmp:
            a, b = os.path.join(tmp, "a.json"), os.path.join(tmp, "b.json")
            json.dump(self._doc("ookla"), open(a, "w"))
            json.dump(self._doc("ookla"), open(b, "w"))
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                bufferscope.cmd_compare(argparse.Namespace(file_a=a, file_b=b))
            self.assertNotIn("different load generators", buf.getvalue())


def _arm(mean, spread=0.1, n=5):
    """One arm whose runs sit tightly around `mean` added-latency ms."""
    step = (spread * 2) / (n - 1) if n > 1 else 0.0
    return _agg_doc([mean - spread + step * i for i in range(n)])


class TestPoolArms(unittest.TestCase):
    """Runs inside one arm share conditions, so the arm is the unit."""

    def test_each_arm_contributes_one_value(self):
        pooled = bufferscope.pool_arms([_arm(10.0), _arm(11.0), _arm(12.0)])
        values = pooled["aggregate"]["upload"]["ctrl"]["values"]
        self.assertEqual(len(values), 3)
        self.assertAlmostEqual(sorted(values)[0], 10.0, places=6)

    def test_records_arm_and_run_counts(self):
        pooled = bufferscope.pool_arms([_arm(10.0, n=5), _arm(11.0, n=4)])
        self.assertEqual(pooled["pooled"]["arms"], 2)
        self.assertEqual(pooled["pooled"]["runs"], 9)

    def test_between_arm_spread_widens_the_interval(self):
        # Same grand mean either way. Only the disagreement between arms
        # differs, and that is exactly what the interval must reflect.
        tight = bufferscope.pool_arms([_arm(9.8), _arm(10.0), _arm(10.2)])
        loose = bufferscope.pool_arms([_arm(5.0), _arm(10.0), _arm(15.0)])
        def width(doc):
            s = doc["aggregate"]["upload"]["ctrl"]
            return s["ci95_hi"] - s["ci95_lo"]
        self.assertAlmostEqual(tight["aggregate"]["upload"]["ctrl"]["mean"],
                               loose["aggregate"]["upload"]["ctrl"]["mean"],
                               places=6)
        self.assertGreater(width(loose), width(tight) * 5)

    def test_single_arm_pools_to_itself(self):
        pooled = bufferscope.pool_arms([_arm(10.0, n=5)])
        self.assertEqual(pooled["pooled"]["arms"], 1)
        self.assertEqual(len(pooled["aggregate"]["upload"]["ctrl"]["values"]), 1)

    def test_rejects_no_arms(self):
        with self.assertRaises(ValueError):
            bufferscope.pool_arms([])


class TestComparePooledArms(CliMixin, unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def _write(self, doc, name):
        path = os.path.join(self.tmp.name, name)
        with open(path, "w") as handle:
            json.dump(doc, handle)
        return path

    def _side(self, means, prefix):
        return [self._write(_arm(m), "%s%d.json" % (prefix, i))
                for i, m in enumerate(means)]

    def test_pools_three_arms_a_side(self):
        a = self._side([9.8, 10.0, 10.2], "a")
        b = self._side([39.8, 40.0, 40.2], "b")
        code, out, _ = self._run(["compare", "-a"] + a + ["-b"] + b)
        self.assertEqual(code, 0)
        self.assertIn("3 arms", out)
        self.assertIn("SIGNIFICANT", out.upper())

    def test_between_arm_noise_prevents_a_false_verdict(self):
        # Arm means straddle each other. Naive run-level pooling would call
        # this significant; arm-level pooling must not.
        a = self._side([5.0, 10.0, 15.0], "a")
        b = self._side([8.0, 13.0, 18.0], "b")
        code, out, _ = self._run(["compare", "-a"] + a + ["-b"] + b)
        self.assertEqual(code, 0)
        self.assertIn("not distinguishable", out.lower())

    def test_positional_two_file_form_still_works(self):
        a = self._write(_arm(10.0), "one.json")
        b = self._write(_arm(40.0), "two.json")
        code, out, _ = self._run(["compare", a, b])
        self.assertEqual(code, 0)
        self.assertIn("SIGNIFICANT", out.upper())

    def test_mixing_positional_and_arm_flags_is_rejected(self):
        a = self._write(_arm(10.0), "one.json")
        code, _, err = self._run(["compare", a, "-b", a])
        self.assertNotEqual(code, 0)
        self.assertIn("either", err.lower())


class TestEvaluateAlerts(unittest.TestCase):
    def _stats(self, p95=None, loss=None):
        return {"probe": {"p95": p95, "loss_pct": loss}}

    def test_p95_over_threshold_is_a_breach(self):
        out = bufferscope.evaluate_alerts(self._stats(p95=62.3), p95=50.0)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["probe"], "probe")
        self.assertEqual(out[0]["metric"], "p95")
        self.assertAlmostEqual(out[0]["value"], 62.3)
        self.assertAlmostEqual(out[0]["threshold"], 50.0)

    def test_p95_under_threshold_is_quiet(self):
        self.assertEqual(bufferscope.evaluate_alerts(self._stats(p95=4.9),
                                                     p95=50.0), [])

    def test_p95_exactly_at_threshold_is_quiet(self):
        self.assertEqual(bufferscope.evaluate_alerts(self._stats(p95=50.0),
                                                     p95=50.0), [])

    def test_loss_over_threshold_is_a_breach(self):
        out = bufferscope.evaluate_alerts(self._stats(loss=3.0), loss=1.0)
        self.assertEqual([b["metric"] for b in out], ["loss_pct"])

    def test_no_thresholds_means_no_breaches(self):
        self.assertEqual(bufferscope.evaluate_alerts(self._stats(p95=999.0)), [])

    def test_absent_measurement_is_not_a_breach(self):
        self.assertEqual(bufferscope.evaluate_alerts(self._stats(p95=None),
                                                     p95=1.0), [])


class TestAlertGate(unittest.TestCase):
    def test_first_breach_fires(self):
        gate = bufferscope.AlertGate(cooldown=300.0)
        self.assertTrue(gate.allow("probe/p95", now=1000.0))

    def test_repeat_within_cooldown_is_suppressed(self):
        gate = bufferscope.AlertGate(cooldown=300.0)
        gate.allow("probe/p95", now=1000.0)
        self.assertFalse(gate.allow("probe/p95", now=1200.0))

    def test_fires_again_once_cooldown_elapses(self):
        gate = bufferscope.AlertGate(cooldown=300.0)
        gate.allow("probe/p95", now=1000.0)
        self.assertTrue(gate.allow("probe/p95", now=1301.0))

    def test_a_different_probe_is_not_suppressed(self):
        gate = bufferscope.AlertGate(cooldown=300.0)
        gate.allow("probe/p95", now=1000.0)
        self.assertTrue(gate.allow("other/p95", now=1000.0))


class TestAlertCommand(unittest.TestCase):
    def test_runs_the_command_with_context_in_the_environment(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "fired.txt")
            script = ("import os,sys;"
                      "open(sys.argv[1],'w').write("
                      "os.environ['BUFFERSCOPE_ALERT_PROBE']+' '+"
                      "os.environ['BUFFERSCOPE_ALERT_VALUE'])")
            cmd = '%s -c "%s" %s' % (sys.executable, script, out)
            ok = bufferscope.run_alert_command(
                cmd, {"probe": "icmp:1.1.1.1", "metric": "p95",
                      "value": 62.3, "threshold": 50.0})
            self.assertTrue(ok)
            with open(out) as handle:
                self.assertEqual(handle.read(), "icmp:1.1.1.1 62.3")

    def test_a_failing_command_is_reported_not_raised(self):
        ok = bufferscope.run_alert_command(
            "definitely-not-a-real-binary-9f3c", {"probe": "p", "metric": "m",
                                                  "value": 1.0, "threshold": 0.0})
        self.assertFalse(ok)

    def test_no_shell_is_used(self):
        # A shell would expand this into two commands and create the file.
        with tempfile.TemporaryDirectory() as tmp:
            marker = os.path.join(tmp, "shelled.txt")
            bufferscope.run_alert_command(
                "%s -c pass && %s -c \"open(r'%s','w')\""
                % (sys.executable, sys.executable, marker),
                {"probe": "p", "metric": "m", "value": 1.0, "threshold": 0.0})
            self.assertFalse(os.path.exists(marker))


@contextlib.contextmanager
def _tcp_listener():
    """A real socket to probe, so intervals carry genuine latency samples."""
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    srv.listen(64)
    srv.settimeout(0.2)
    stop = threading.Event()

    def serve():
        while not stop.is_set():
            try:
                conn, _ = srv.accept()
                conn.close()
            except Exception:
                pass

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    try:
        yield srv.getsockname()[1]
    finally:
        stop.set()
        thread.join(timeout=2)
        srv.close()


class TestMonitorAlerting(CliMixin, unittest.TestCase):
    def setUp(self):
        self._env = bufferscope.collect_env
        bufferscope.collect_env = lambda: {"os": "test", "gateway": None,
                                           "icmp_available": False}
        self._run_st, self._find = (bufferscope.run_speedtest,
                                    bufferscope.find_speedtest)
        bufferscope.find_speedtest = lambda: None

    def tearDown(self):
        bufferscope.collect_env = self._env
        bufferscope.run_speedtest = self._run_st
        bufferscope.find_speedtest = self._find

    def _monitor(self, port, *extra):
        return self._run(["monitor", "--duration", "2", "--interval", "1",
                          "--probe-interval", "50", "--no-gateway",
                          "--probe", "tcp:127.0.0.1:%d#local" % port]
                         + list(extra))

    def test_breach_sets_the_alert_exit_code(self):
        with _tcp_listener() as port:
            code, out, _ = self._monitor(port, "--alert-p95", "0")
        self.assertEqual(code, bufferscope.EXIT_ALERT)
        self.assertIn("ALERT", out.upper())

    def test_quiet_run_keeps_the_normal_exit_code(self):
        with _tcp_listener() as port:
            code, out, _ = self._monitor(port, "--alert-p95", "100000")
        self.assertIn(code, (bufferscope.EXIT_OK,
                             bufferscope.EXIT_UNTRUSTWORTHY))
        self.assertNotIn("ALERT", out.upper())

    def test_breaches_are_recorded_in_the_document(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "m.json")
            with _tcp_listener() as port:
                self._monitor(port, "--alert-p95", "0", "--out", path)
            with open(path) as handle:
                doc = json.load(handle)
            self.assertTrue(doc["alerts"])
            self.assertEqual(doc["alerts"][0]["metric"], "p95")

    def test_cooldown_holds_down_repeat_commands(self):
        with tempfile.TemporaryDirectory() as tmp:
            counter = os.path.join(tmp, "count")
            script = ("import os,sys;"
                      "p=sys.argv[1];"
                      "n=open(p).read() if os.path.exists(p) else '';"
                      "open(p,'w').write(n+'x')")
            cmd = "%s -c %s%s%s %s" % (sys.executable, chr(34), script,
                                       chr(34), counter)
            with _tcp_listener() as port:
                self._monitor(port, "--alert-p95", "0", "--on-alert", cmd,
                              "--alert-cooldown", "3600")
            fired = ""
            if os.path.exists(counter):
                with open(counter) as handle:
                    fired = handle.read()
            self.assertEqual(len(fired), 1)
