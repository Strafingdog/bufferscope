# netdiag

Measure what your speed test won't: **latency under load**.

A connection can report 950/840 Mbps with 4 ms idle latency and still make video
calls unusable. The reason is bufferbloat - when the link saturates, packets
queue behind bulk traffic and latency collapses. On the router that motivated
this tool, saturating the upstream drove p95 latency from 4.9 ms to 245 ms while
every conventional speed test still reported a perfect 900/900 connection.

netdiag saturates the link with the Ookla CLI, probes latency at 50 Hz on
background threads, and correlates every sample to the exact test phase it
occurred in. It reports percentiles rather than means, tells you whether the
delay originates at your router or beyond it, and refuses to publish a result
from a run that failed to saturate the link.

**Router-agnostic.** Nothing is hardcoded to a vendor. The gateway, the network
interfaces and the speedtest binary are discovered at runtime; router telemetry
uses standard IF-MIB SNMP counters that any managed router exposes; and
device-specific behaviour such as how often counters refresh is **measured
rather than assumed**. It was developed against a DrayTek Vigor 2865 because
that is what was to hand - the observations from that device appear below as
examples, not as requirements.

## Why this matters

Bufferbloat is invisible to throughput tests and obvious to anyone on a call.
Fixing it is usually a matter of shaping egress slightly below line rate so the
router owns the queue, which costs a little bandwidth and needs measurement to
tune.

On the router this was developed against, enabling hardware QoS took worst-case
loaded latency from **245 ms to roughly 30 ms p95**. Tuning the shaping rate
then produced a curve worth knowing about:

```
egress   upload Mbps   added p95 upload
  900         867.0          38.3
  940         924.4          26.5     <- latency minimum
  943         932.2          33.0     <- throughput plateau begins
  945         932.5          43.5     <- dominated by 943
```

**Non-monotonic, with a plateau.** Below the optimum the drain rate is slow, so
the same backlog costs more milliseconds; above it the shaper stops binding
hard enough to own the queue. The useful range is only a few Mbps wide, just
under the line's real capacity - which is why guessing at "95% of line rate"
is unlikely to land on it, and why single-run testing found the wrong answer
three times before the arms above were run properly.

## Requirements

- **Python 3.9+** - no `pip install`, standard library only
- **Ookla Speedtest CLI** - required; generates the saturating load

```bash
winget install Ookla.Speedtest.CLI     # Windows
brew install speedtest-cli             # macOS
apt install speedtest-cli              # Debian/Ubuntu
```

netdiag finds the binary automatically on PATH or in the usual install
locations. Nothing is hardcoded.

## Install

Download `netdiag.py`. That is the whole tool.

```bash
chmod +x netdiag.py        # optional, POSIX
python netdiag.py --version
```

## Usage

### `bufferbloat` - the main command

```
$ python netdiag.py bufferbloat
```

```
netdiag 1.0.0   2026-08-21T13:33:07
server: Example Networks, Anytown (id 12345)

Download: 947.8 Mbps    Upload: 836.7 Mbps    Idle latency: 4.21 ms

phase / probe                       p50      p95      p99      max   loss%
--------------------------------------------------------------------------
idle
  tcp:192.168.1.1:80              12.65    16.62    16.87    25.05     0.0
  icmp:192.168.1.1                 0.58     0.87     1.05     2.38     0.0
  dns:1.1.1.1                      5.01     6.10     7.87   381.49     0.0
  tcp:1.1.1.1:443                 15.64    29.02    29.33    29.64     0.0
  icmp:1.1.1.1                     4.36     4.91     5.14     8.82     0.0
download   [NIC 985 Mbps]
  icmp:192.168.1.1                 0.57     1.04     1.15    11.19     0.0
  icmp:1.1.1.1                     7.87     8.66    11.14    22.19     0.0
upload   [NIC 867 Mbps]
  icmp:192.168.1.1                 0.94    22.99    25.25    26.05     0.0
  icmp:1.1.1.1                     4.60    26.50    28.34    29.32     0.0

Bufferbloat: worst added p95 22.12 ms  ->  grade A
```

### `probe` - latency only, generates no load

```
$ python netdiag.py probe --duration 60 --targets 1.1.1.1,8.8.8.8
```

Useful for characterising a link at rest, or for measuring from a second
machine while a first machine generates load.

### `monitor` - long-running, catches intermittent faults

```
$ python netdiag.py monitor --duration 3600 --interval 60
```

One summary line per probe per interval, then a final aggregate. Ctrl-C exits
cleanly and still writes a complete result.

### `env` - what netdiag detected about this machine

```
$ python netdiag.py env
```

Paste this into a bug report or an LLM session before anything else.

### `compare` - diff two runs

```
$ python netdiag.py bufferbloat --out before.json
   ... change one router setting ...
$ python netdiag.py bufferbloat --out after.json
$ python netdiag.py compare before.json after.json
```

```
metric                              A            B        delta  verdict
------------------------------------------------------------------------
download_mbps                  952.31       952.31        +0.00  unchanged
upload_mbps                    902.40       835.38       -67.02  worse
worst_added_p95_ms             241.60        27.51      -214.09  better

grade: D  ->  A
```

That is a real result: enabling hardware QoS cost 67 Mbps of upload and removed
214 ms of loaded latency. `compare` deliberately labels each metric on its own
terms - losing upload throughput is "worse" even when it is the price of a
change you want - so you weigh the trade rather than being handed a verdict.

### Options

```
--json                  machine-readable output to stdout
--out FILE              write JSON result to FILE
--targets HOST[,HOST]   internet probe targets (default 1.1.1.1)
--no-gateway            do not probe the default gateway
--probe-interval MS     sampling interval (default 20 ms = 50 Hz)
--baseline-secs N       idle baseline duration (default 10)
--server-id ID          pin the Ookla server for comparable runs
--no-icmp               skip ICMP even when privileged
--quiet                 suppress progress output
--duration N            probe/monitor run time (30 / 3600)
--interval N            monitor summary interval (default 60)
--repeat N              run N times, report mean/stdev/95% CI
--router-snmp HOST      poll the router for whole-household traffic
--snmp-community STR    overrides $NETDIAG_SNMP_COMMUNITY
--snmp-interface NAME   router interface to measure (default: busiest)
--snmp-port N           default 161
--snmp-device SPEC      extra SNMP target, repeatable (see below)
--raw                   include the full latency sample series in the JSON
```

### `--repeat N` - the flag that makes tuning honest

**Loaded latency on a real line is extremely noisy.** Eight runs of an identical
configuration on the connection this tool was built for produced added-p95
values from **1.75 ms to 40.91 ms** - a 23x spread with nothing changed between
runs. Any single-run comparison of two router settings is therefore almost
worthless: the difference you see is usually noise.

```
$ netdiag.py classes --repeat 8 --server-id 12345 --out arm-a.json
```

```
netdiag 1.0.0   2026-08-21T15:19:26   repeats: 8 (excluded 0)
  download_mbps  mean    948.5 Mbps  95% CI [ 939.4, 957.6]  n=8
  upload_mbps    mean    867.7 Mbps  95% CI [ 866.7, 868.6]  n=8

phase / probe            n      mean     stdev     range  95% CI of added p95
-----------------------------------------------------------------------------
upload:
  udp-control        n=8       22.65      4.76 19.3-26.0  [  18.6,  26.7]
  dns-class1         n=8       22.23      4.07 19.4-25.1  [  18.8,  25.7]
```

Then `compare` two repeated-run files and it performs a Welch test on every
metric rather than subtracting two numbers:

```
metric                       A mean     B mean       diff  95% CI of diff / verdict
upload/dns-class1             38.87      33.24      -5.63  [-18.2, +6.9]  not distinguishable from noise
upload/udp-control            39.83      12.10     -27.73  [-38.1, -17.4]  SIGNIFICANT - better
```

**A change that cannot beat the noise is reported as noise.** Runs that fail
validation are excluded from the aggregate rather than averaged in, since an
unsaturated run would drag the mean toward "no bufferbloat".

Rule of thumb: **n=8 per configuration**, and pin `--server-id`. Fewer runs, and
you are back to reading tea leaves. At roughly 90 seconds and 1.4 GB per run,
that is about 12 minutes and 11 GB per configuration - the honest price of an
answer.

**Budget your speedtests.** Ookla rate-limits the CLI. Around 70 runs in a day
triggered:

```
[error] Limit reached:
Speedtest CLI. Too many requests received. To maintain a fair and stable
environment, please review and adjust the frequency of your requests.
```

The limit is global, not per-server - selecting a different server does not
help. It appears to be a **rolling window rather than a daily quota**: one
session hit it at roughly 70 runs, cleared after about three hours, then hit it
again after only 25-30 runs inside the following hour.

Practical planning: **three arms of eight, then expect to wait.** Decide which
comparisons matter before you start, because you will not get to run them all
back to back.
netdiag surfaces the error and aborts after two consecutive failed runs rather
than grinding through the rest of the repeat.

### Seeing the whole household, not just this PC

netdiag samples the NIC of the machine it runs on. Traffic from a TV, a phone or
any other device never crosses that adapter, so a host-based measurement is
blind to most of what your connection is actually doing. The router is the only
device that sees every client.

`--router-snmp` polls the router's own interface counters over SNMPv2c:

```
$ netdiag.py router                      # find the WAN interface
$ netdiag.py probe --duration 300 --router-snmp 192.168.1.1
```

```
source                  down Mbps    up Mbps
this PC (NIC)               0.036      0.036
whole household (WAN)       1.146      0.214   [WAN2, 45 transitions]
other devices               1.110      0.179
```

**Router setup** (once):

1. Enable the SNMP agent and the **SNMPv2C** agent. Leave v3 off - it would
   require AES, which is not in the Python standard library, to encrypt byte
   counters on a trusted LAN.
2. Change the **Get community** from `public` to something unguessable.
3. Change the **Set community** from `private` too. It grants *write* access to
   your router's configuration and netdiag never uses it.
4. Make sure SNMP is **not** exposed to the internet. On most routers this is
   a separate switch on the management or remote-access page.

```bash
export NETDIAG_SNMP_COMMUNITY="your-read-community"     # or setx on Windows
```

The community is read from the environment, never written to a result file and
never logged. `--snmp-community` overrides it if you must.

### Polling more than one device

A mesh or multi-AP network has no single vantage point - the gateway sees the
internet link, but nothing about which access point is carrying which client.
`--snmp-device` is repeatable and takes
`HOST[,env=VAR][,label=NAME][,iface=NAME][,port=N]`:

```
$ netdiag.py probe --duration 300 \
    --snmp-device 192.0.2.1,label=gateway \
    --snmp-device 192.0.2.2,env=AP_UPSTAIRS_COMMUNITY,label=upstairs \
    --snmp-device 192.0.2.3,env=AP_GARAGE_COMMUNITY,label=garage,iface=LAN
```

`env` names **an environment variable, never the community itself**. A
community passed with `--snmp-community` lands in your shell history and is
visible to any process that can list command-line arguments. A variable name
is not a secret, so it is safe to write in a script, a Makefile, or a README.

Each device resolves its community in this order:

```
--snmp-community  ->  device's env=VAR  ->  $NETDIAG_SNMP_COMMUNITY  ->  "public"
```

Give each device its own community where the hardware allows it. Sharing one
across every device means a single leak exposes all of them.

Results gain a `devices` array alongside the existing `router_throughput`:

```json
"devices": [
  {"label": "gateway",  "host": "192.0.2.1", "interface": "WAN2",
   "down_mbps": 812.4, "up_mbps": 44.1, "sample_count": 297, "source": "snmp"},
  {"label": "upstairs", "host": "192.0.2.2", "interface": "LAN",
   "down_mbps": 210.7, "up_mbps": 18.3, "sample_count": 297, "source": "snmp"}
]
```

`router_throughput` still describes the first device listed, so results saved
by older versions and the `compare` command are unaffected.

Each device is polled once per second, so three devices means three SNMP
queries per second. A device that does not answer produces a warning on stderr
and is skipped - the run continues on whatever did respond, rather than failing.
The same counter-granularity caveat below applies per device, and cheap access
points often refresh their counters far more slowly than a router does.

**Counter granularity - read this before trusting a short window.** Routers
refresh SNMP counters on their own schedule, and the rate varies widely by
vendor. netdiag measures it rather than assuming, and `netdiag.py router`
reports what it found:

```
counter refresh: ~2.0 s  (measured)
  usable for windows of at least 10 s - a speedtest phase is ~7 s
```

That example device updated roughly **every 2 seconds**, in large jumps with
zeros between:

```
t=1.79    +122,714 bytes
t=2.07    0
t=2.34    0
t=4.06    +155,329,632 bytes
t=6.09    +155,341,585 bytes
```

An Ookla phase lasts about 7 seconds and therefore contains only ~3 refreshes.
Integrating across it undercounted upload by 18%. netdiag handles this by
measuring between observed counter *transitions* rather than sample boundaries,
and by **refusing to report a figure** when a window is too short for the
measured refresh rate - those phases record `router_note` instead, and
validation falls back to the host NIC.

The threshold is five refresh intervals per window, set from that 18% error
rather than from intuition. On a router refreshing every 0.2 s a speedtest
phase is perfectly measurable; at 2 s it is not.

The practical rule: **router data is authoritative for passive observation over
minutes, and unsuitable for individual speedtest phases.** `probe` and `monitor`
are where it earns its place.

## The QoS tuning workflow

1. `netdiag.py bufferbloat --repeat 8 --server-id ID --out baseline.json`
2. Change **one** router setting. Typically: enable QoS on the WAN interface and
   set egress bandwidth slightly *below* your measured line rate, so the router
   becomes the bottleneck and owns the queue.
3. `netdiag.py bufferbloat --repeat 8 --server-id ID --out attempt.json`
4. `netdiag.py compare baseline.json attempt.json` - keep it only if the change
   is reported SIGNIFICANT.
5. Repeat, moving the shaper up until latency degrades, then step back.

**Always pin `--server-id`** - comparing results from different servers compares
the servers as much as your changes. Pick one from `speedtest --servers` and
keep it for the whole tuning session.

**Always `--repeat`.** Steps 1 and 3 with single runs will hand you a confident
number and a wrong conclusion; see the section above.

### Verifying classification rules

`classes` probes each QoS rule with traffic that should match it, alongside a
**control** on a port no rule matches:

```
$ netdiag.py classes --repeat 8 --server-id ID \
    --probe tcp:1.1.1.1:853#tcp-control \
    --probe stun:stun.l.google.com:19302#udp-control \
    --probe dns:1.1.1.1#dns-rule \
    --probe stun:stun.l.google.com:3478#teams-rule
```

Two things make this work where naive probing fails:

- **Avoid TCP-connect probes as controls.** `tcp:1.1.1.1:853` produced 1030 ms
  outliers that no other probe saw, on three separate runs, and its p95 drifted
  between arms - fatal in a control, which must be the stable reference. The
  default preset uses STUN on UDP 19302 instead. TCP probes remain available
  for testing TCP-classified rules; just don't make one your baseline.
- **The control must use the same transport as the probe it is compared with.**
  TCP and UDP probes have different baselines, so a TCP control cannot tell you
  whether a UDP rule is working. Hence a UDP control on STUN port 19302, which
  no typical conferencing rule matches.
- **Probes must not change class between runs.** If a rule you add happens to
  match an existing probe, that probe's numbers move for reasons unrelated to
  the setting you are testing, and cross-run comparison becomes meaningless.

If a classified probe cannot be distinguished from the control, the rule is not
buying you anything - regardless of what the router's status page says about
packets landing in that class.

## Interpreting results

**Grades** apply to `p95(loaded) - p95(idle)`, worst direction:

| Added p95 latency | Grade | Experience |
|---|---|---|
| under 5 ms | A+ | indistinguishable from idle |
| under 30 ms | A | calls and gaming unaffected |
| under 60 ms | B | occasionally noticeable |
| under 200 ms | C | calls degrade under load |
| 200 ms or more | D | unusable while anything uploads |

**Loaded latency depends on the load generator, and by a lot.** The same
connection, measured minutes apart:

```
                     under Ookla      under Waveform
added p95 download        +3.9 ms          +11.3 ms
added p95 upload         +26.5 ms           +3.6 ms
```

Not a contradiction, and not a fault in either tool - they stress different
things. Ookla's upload test is far more aggressive (more parallel streams,
harder ramp) while Waveform's download test is the harsher of its two. A
passive `netdiag probe` run *during* a Waveform test reproduced Waveform's
figures, confirming the measurement is sound and the difference is the load.

Two consequences:

- **Comparisons are valid only within one generator.** Every A/B in this
  README used Ookla throughout, so those results stand. Never compare a
  netdiag figure against a Waveform or fast.com figure.
- **Absolute numbers need their generator attached.** "This line adds 26 ms
  under upload saturation" means *under Ookla's upload saturation*. Under
  ordinary household use the same connection measured 6.9 ms p95.

If you want to know how your link behaves for real traffic, measure real
traffic: `netdiag probe` generates no load and observes what is actually
happening.

**Attribution.** netdiag probes your gateway *and* an internet target:

- Both rise together -> the delay is at or before your router. Your ISP is fine;
  look at the router.
- Only the internet target rises -> the delay is beyond your router.
- Neither rises -> no bufferbloat in that direction.

**Percentiles, not means.** Loaded-latency distributions are bimodal: most
packets sail through while a minority are delayed by hundreds of milliseconds. A
mean averages the problem away. During development a mean-based threshold
reported a genuine 450x p95 degradation as "no significant bufferbloat", which
is why everything here keys on p95.

**Watch `max` separately from `p95`.** A low p95 with a high max means brief
spikes - typically TCP overshooting at the start of a transfer before the shaper
reins it in. That is a different problem from sustained queuing, and usually a
much less harmful one.

## Trustworthiness

Exit codes:

| Code | Meaning |
|---|---|
| 0 | Success; result trustworthy |
| 1 | Error - missing dependency, no targets, speedtest failure |
| 2 | Completed, but the result is **not** trustworthy |

Exit 2 means the measurement completed but netdiag does not stand behind it. The
reasons are printed and included in the JSON. Rules that trigger it:

- **Saturation** - NIC byte counters must show at least 75% of the throughput
  Ookla reported. A shortfall means the link was never saturated, so the loaded
  latency figure describes an idle link.
- **Sample count** - each phase needs at least 20 samples for percentiles.
- **Baseline sanity** - idle p95 must be under 100 ms, or there is no usable
  baseline to compare against.

This exists because ad-hoc predecessors of this tool cheerfully reported
"19144 Mbps" (dividing bytes that never transferred by an error's elapsed time)
and "5 Mbps sustained" (the load died silently to rate limiting, leaving 145
seconds of idle line averaged into the result). Both would have been caught by a
throughput cross-check. Now they are.

## JSON schema

`--json` and `--out` emit the full result. This is the contract for scripts and
LLMs; keys are stable and changes are additive.

Per-probe statistics carry a `samples` array **only when `--raw` is given** -
at 50 Hz the series dwarfs the summary. Pass `--raw` when you may want to
re-analyse a run later, for instance to bucket latency against a router's
throughput timeline instead of the load generator's own phase claims.

```
{
  "schema_version": 1,
  "netdiag_version": "1.0.0",
  "started_at": "2026-08-21T13:33:07",
  "env": {
    "os", "python", "netdiag_version", "schema_version",
    "gateway",            // detected default gateway, or null
    "interfaces",         // name -> {rx_bytes, tx_bytes}
    "speedtest_path", "speedtest_version",
    "icmp_available"      // false when unprivileged
  },
  "speedtest": {
    "download_mbps", "upload_mbps", "idle_latency_ms",
    "packet_loss_pct", "server", "result_url"
  },
  "phases": {
    "idle" | "download" | "upload": {
      "throughput_mbps_nic",       // measured from NIC counters
      "throughput_mbps_reported",  // what Ookla claimed
      "probes": {
        "<probe name>": {
          "n", "lost", "loss_pct",
          "min", "mean", "p50", "p90", "p95", "p99", "max", "stddev",
          "jitter_consecutive",    // mean |rtt[i] - rtt[i-1]|
          "jitter_rfc3550"         // EWMA, RFC 3550
        }
      }
    }
  },
  "bufferbloat": {
    "<phase>": {"<probe>": {"added_p95_ms", "grade"}},
    "worst_added_p95_ms",
    "overall_grade"
  },
  "validation": {"trustworthy": bool, "reasons": [str]}
}
```

Probe names encode transport and target: `tcp:1.1.1.1:443`, `dns:1.1.1.1`,
`icmp:192.168.1.1`.

## How it works

1. Probes start and collect an idle baseline.
2. Ookla is spawned with `--format=jsonl`, streaming phase events as they occur.
3. Every probe sample, NIC snapshot and Ookla event is timestamped against a
   single `perf_counter` origin, so samples can be attributed to phases without
   guesswork.
4. NIC counters are sampled on a background thread and integrated per phase.
   They are deliberately never read inside the event loop - on Windows that call
   takes over a second, which both delays event timestamps and shifts the
   measured byte window out of alignment with the phase. That bug understated
   download throughput by 3x before it was found.
5. Results are validated, graded and rendered.

## Limitations

- **Latency is measured from one host.** It includes this machine's own network
  stack. To rule that out, run `probe` on a second device while this one
  generates load. Throughput can now be measured household-wide via
  `--router-snmp`, but latency cannot.
- **Router counters are coarse.** See the granularity note above. netdiag
  declines to report rather than reporting a biased figure.
- **ICMP needs privileges.** Raw sockets require root on Linux/macOS and admin on
  Windows. Without them netdiag silently uses TCP and UDP probes, which are
  unprivileged and sub-millisecond. Detail degrades; correctness does not.
- **Ookla is required.** Self-generated HTTP load was tried and abandoned:
  providers rate-limit concurrent connections per source IP well below a gigabit,
  silently capping the load and invalidating results.
- **IPv4 only.** No IPv6-specific handling.
- **Not a router configuration tool.** It measures; you make the changes.

## Testing

```bash
python -m unittest test_netdiag -v          # 82 tests, offline
NETDIAG_E2E=1 python -m unittest test_netdiag.TestEndToEnd -v
```

The default suite requires no network, no root and no particular OS - platform
parsers are tested against captured command output from Windows, Linux and
macOS. The end-to-end test is opt-in, takes about 20 seconds and uses roughly
1.4 GB of data.

## Licence

[PolyForm Noncommercial 1.0.0](https://polyformproject.org/licenses/noncommercial/1.0.0)
- free for personal use, research, teaching, charities, government and public
bodies. Commercial use is not permitted, which includes running it as part of
paid work for a business.

Not an OSI-approved open source licence, so it will not be accepted by
distributions or package repositories that require one.
