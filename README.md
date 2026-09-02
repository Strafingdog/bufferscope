# bufferscope

Measure what your speed test won't: **latency under load**.

A connection can report 950/840 Mbps with 4 ms idle latency and still make video
calls unusable. The reason is bufferbloat - when the link saturates, packets
queue behind bulk traffic and latency collapses. On the router that motivated
this tool, saturating the upstream drove p95 latency from 4.9 ms to 245 ms while
every conventional speed test still reported a perfect 900/900 connection.

bufferscope saturates the link with the Ookla CLI, probes latency at 50 Hz on
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
- **A load generator** - one of the two below, to saturate the link

**Ookla Speedtest CLI**, the default:

```bash
winget install Ookla.Speedtest.CLI     # Windows
brew install speedtest-cli             # macOS
apt install speedtest-cli              # Debian/Ubuntu
```

**LibreSpeed CLI**, optional, selected with `--generator librespeed`:

```bash
winget install LibreSpeed.librespeed-cli    # Windows
# or a release binary from https://github.com/librespeed/speedtest-cli
```

bufferscope finds either binary automatically on PATH or in the usual install
locations. Nothing is hardcoded. See
[Choosing the load generator](#choosing-the-load-generator) for which to use.

## Install

Download `bufferscope.py`. That is the whole tool.

```bash
chmod +x bufferscope.py        # optional, POSIX
python bufferscope.py --version
```

## Usage

### `bufferbloat` - the main command

```
$ python bufferscope.py bufferbloat
```

```
bufferscope 1.0.0   2026-08-21T13:33:07
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

### `classes` - check that QoS rules match the traffic you think they do

```
$ python bufferscope.py classes
```

Runs the same loaded measurement as `bufferbloat`, but with a preset of probes
chosen to land in different QoS classes - DNS, a STUN port a conferencing app
would use, and a control probe on a port no rule should match. If a rule works,
the probes it covers stay low while the control probe suffers. See
[Verifying classification rules](#verifying-classification-rules).

### `probe` - latency only, generates no load

```
$ python bufferscope.py probe --duration 60 --targets 1.1.1.1,8.8.8.8
```

Useful for characterising a link at rest, or for measuring from a second
machine while a first machine generates load.

### `monitor` - long-running, catches intermittent faults

```
$ python bufferscope.py monitor --duration 3600 --interval 60
```

One summary line per probe per interval, then a final aggregate. Ctrl-C exits
cleanly and still writes a complete result.

### `router` - list the router's interfaces and counters

```
$ python bufferscope.py router
```

Lists the interfaces the router exposes over SNMP, identifies the busiest one
(usually the WAN), and reports the measured counter refresh rate so you know
which measurement windows it can support. Run this once when setting up
`--router-snmp`. See [Seeing the whole household](#seeing-the-whole-household-not-just-this-pc).

### `agent` - lend this machine's view to a run on another

```
$ BUFFERSCOPE_AGENT_TOKEN=... python bufferscope.py agent --listen 0.0.0.0
```

Answers probe requests from another bufferscope so a run can measure latency
from more than one place at once. See
[Measuring from more than one host](#measuring-from-more-than-one-host).

### `env` - what bufferscope detected about this machine

```
$ python bufferscope.py env
```

Paste this into a bug report or an LLM session before anything else.

### `compare` - diff two runs

```
$ python bufferscope.py bufferbloat --out before.json
   ... change one router setting ...
$ python bufferscope.py bufferbloat --out after.json
$ python bufferscope.py compare before.json after.json
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

#### Pooling several arms a side

One arm a side is rarely enough. Conditions drift between arms - the evening
gets busier, the route changes, a neighbour starts streaming - and on the line
this was built for the **between-arm** standard deviation was 4.59 ms, larger
than the spread within any single arm. Three arms a side is the working rule.

```
$ python bufferscope.py compare -a off-1.json off-2.json off-3.json \
                                -b on-1.json  on-2.json  on-3.json
```

```
pooled: A = 3 arms (24 runs)   B = 3 arms (23 runs)
each arm contributes its own mean, so the interval carries between-arm spread

metric                         A mean     B mean       diff  95% CI of diff
upload/teams-class1             37.66      26.70     -10.96  [-20.1, -1.8]  SIGNIFICANT - better
```

**Pooling is by arm, not by run,** and the distinction decides verdicts. Runs
inside one arm share their conditions, so treating 24 runs as 24 independent
replicates would shrink the interval by roughly the square root of the number
of runs per arm and manufacture significance that is not there. Each arm
therefore contributes one number - its own mean - and the interval carries the
disagreement *between* arms, which is the noise you actually have to beat.

The positional two-file form still works and is unchanged.

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
--snmp-community STR    overrides $BUFFERSCOPE_SNMP_COMMUNITY
--snmp-interface NAME   router interface to measure (default: busiest)
--snmp-port N           default 161
--snmp-device SPEC      extra SNMP target, repeatable (see below)
--probe SPEC            probe spec, repeatable, replaces the default set
--raw                   include the full latency sample series in the JSON
--generator NAME        ookla (default) or librespeed
--ls-concurrent N       librespeed parallel streams (default 8)
--ls-duration SECS      librespeed per-direction duration (default 15)
--family {auto,4,6}     address family for probes (default auto)
--peer SPEC             another host running 'agent', repeatable:
                        HOST[:PORT][#label]
--peer-token TOKEN      shared secret for --peer, or $BUFFERSCOPE_PEER_TOKEN
```

`monitor` adds:

```
--alert-p95 MS          alert when an interval p95 exceeds MS
--alert-loss PCT        alert when an interval loss exceeds PCT
--alert-cooldown SECS   minimum gap between commands for one probe and
                        metric (default 300)
--on-alert CMD          command to run on a breach; no shell is used
```

`compare` adds `-a/--arm-a` and `-b/--arm-b`, each taking several files.
`agent` takes `--listen`, `--port`, `--token` and `--max-duration`.

### Choosing the load generator

`--generator ookla` (the default) or `--generator librespeed`. Every saved run
records which one produced it, and `compare` warns when two documents disagree,
because latency measured through two generators is not the same measurement: on
one line, minutes apart, Ookla reported download +3.9 ms / upload +26.5 ms where
Waveform reported +11.3 / +3.6.

**Ookla** pins to a chosen server with `--server-id`, which is what makes one
arm comparable to the arm before it. It also rate-limits per source address
aggressively enough to abort a long `--repeat` arm part way through.

**LibreSpeed** is not rate-limited, so it will finish an arm that Ookla refuses.
It publishes no phase event stream, so bufferscope runs `--no-upload` and then
`--no-download` as separate processes and times each directly - the phase
windows are exact rather than inferred from progress events. A direction that
fails is omitted rather than recorded as zero, so a failed upload cannot
masquerade as a saturated link carrying nothing.

**LibreSpeed's ceiling is the thing to watch.** On a line where Ookla reaches
~947 Mbps upload, LibreSpeed reaches ~774, and raising `--ls-concurrent` makes
it worse (8 -> 774, 16 -> 704, 32 -> 583). It therefore cannot exercise a shaper
set above roughly 780 Mbps. Such a run still validates as trustworthy, because
saturation is checked against reported throughput rather than against line rate,
so this will not announce itself. If you are tuning egress near gigabit, use
Ookla.

Runs recorded before this option existed are treated as Ookla, which they were.

### IPv6

Probes follow the system's address selection by default. `--family 4` or
`--family 6` forces one, and forcing a family the target does not have fails
rather than quietly falling back - which is what makes it a check rather than a
preference.

```
$ python bufferscope.py probe --family 6 --targets 2606:4700:4700::1111
```

An IPv6 literal in a probe spec must be bracketed, because otherwise there is
no telling an address from a port:

```
--probe tcp:[2606:4700:4700::1111]:853#control
```

Marking on IPv6 goes to `IPV6_TCLASS` rather than the IPv4 ToS byte, ICMP
becomes ICMPv6, and gateway detection finds the v6 default route with its scope
attached - a `fe80::` gateway is unreachable without the interface it belongs
to. A `--family 6` run probes the v6 gateway, since attributing delay to a
router leg the traffic never crossed would be worse than not measuring it.

### `--repeat N` - the flag that makes tuning honest

**Loaded latency on a real line is extremely noisy.** Eight runs of an identical
configuration on the connection this tool was built for produced added-p95
values from **1.75 ms to 40.91 ms** - a 23x spread with nothing changed between
runs. Any single-run comparison of two router settings is therefore almost
worthless: the difference you see is usually noise.

```
$ bufferscope.py classes --repeat 8 --server-id 12345 --out arm-a.json
```

```
bufferscope 1.0.0   2026-08-21T15:19:26   repeats: 8 (excluded 0)
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
bufferscope surfaces the error and aborts after two consecutive failed runs rather
than grinding through the rest of the repeat. The runs that already completed
are still aggregated and written out - they are not suspect, and they cost
minutes each - so an arm cut short by rate limiting remains usable, and the
saved document records why it stopped early.

### Seeing the whole household, not just this PC

bufferscope samples the NIC of the machine it runs on. Traffic from a TV, a phone or
any other device never crosses that adapter, so a host-based measurement is
blind to most of what your connection is actually doing. The router is the only
device that sees every client.

`--router-snmp` polls the router's own interface counters over SNMPv2c:

```
$ bufferscope.py router                      # find the WAN interface
$ bufferscope.py probe --duration 300 --router-snmp 192.168.1.1
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
   your router's configuration and bufferscope never uses it.
4. Make sure SNMP is **not** exposed to the internet. On most routers this is
   a separate switch on the management or remote-access page.

```bash
export BUFFERSCOPE_SNMP_COMMUNITY="your-read-community"     # or setx on Windows
```

The community is read from the environment, never written to a result file and
never logged. `--snmp-community` overrides it if you must.

### Measuring from more than one host

`--router-snmp` gives household-wide *throughput*, but latency still comes from
whichever machine you ran the command on. To see what the connection feels like
from the laptop on Wi-Fi while the desktop saturates the line, run an agent
there and point the run at it.

On the second machine:

```
$ export BUFFERSCOPE_AGENT_TOKEN=$(python -c "import secrets;print(secrets.token_urlsafe(24))")
$ python bufferscope.py agent --listen 0.0.0.0
bufferscope agent listening on 0.0.0.0 port 7419
```

On the machine generating load:

```
$ export BUFFERSCOPE_PEER_TOKEN=...same token...
$ python bufferscope.py bufferbloat --peer 192.168.1.50#laptop
```

```
peers:
  host         probe            this host      peer       gap
  laptop       icmp:1.1.1.1          5.11     31.40     26.29
  laptop       icmp:192.168.1.1      1.20      3.10      1.90
```

Both hosts crossed the same router and the same line, so the gap is the cost of
the peer's own leg - Wi-Fi, powerline, a cheap switch. A peer that cannot be
reached, or answers with the wrong token, is recorded in the result and the run
carries on; it never aborts the measurement.

**What the agent is, and is not.** It runs in the foreground for the duration
of a measurement and exits. It is not installed as a service, does not start at
boot, and does not run unattended. It binds `127.0.0.1` unless you widen it on
purpose, a shared token is mandatory rather than optional, it serves one
connection at a time, and the entire vocabulary is start a probe, report what
the probe saw, observe a marking, stop. It runs no commands and touches no
files. Probe specs arrive as text and go through the same parser the CLI uses,
which can only name a host, a port and a marking.

### Polling more than one device

A mesh or multi-AP network has no single vantage point - the gateway sees the
internet link, but nothing about which access point is carrying which client.
`--snmp-device` is repeatable and takes
`HOST[,env=VAR][,label=NAME][,iface=NAME][,port=N]`:

```
$ bufferscope.py probe --duration 300 \
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
--snmp-community  ->  device's env=VAR  ->  $BUFFERSCOPE_SNMP_COMMUNITY  ->  "public"
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

**Counter width.** bufferscope reads the 64-bit `ifHC` octet counters where a
device implements them, and falls back to the 32-bit originals where it does
not - which many access points do not. The 32-bit counters wrap roughly every
34 seconds at gigabit, so each delta is corrected against the counter's
modulus: a reading lower than the previous one is treated as a wrap where that
yields a plausible delta, and as a counter reset otherwise.

**Counter granularity - read this before trusting a short window.** Routers
refresh SNMP counters on their own schedule, and the rate varies widely by
vendor. bufferscope measures it rather than assuming, and `bufferscope.py router`
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
Integrating across it undercounted upload by 18%. bufferscope handles this by
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

### Alerting from `monitor`

A monitor left running overnight is only useful if it tells you when something
happened.

```
$ python bufferscope.py monitor --duration 86400 --alert-p95 50 \
      --on-alert "curl -s -d bufferbloat https://ntfy.sh/my-topic"
```

```
12:04:31  p95  4.9 ms  ok
ALERT  icmp:1.1.1.1 p95 62.30 over threshold 50.00
       on-alert command ok
```

A breach prints to stderr, is recorded in the result under `alerts`, and sets
**exit code 3**, so a scheduled run can be checked without parsing output.

`--on-alert` runs **without a shell**, and the breach values are never
interpolated into the command - they arrive as `BUFFERSCOPE_ALERT_PROBE`,
`_METRIC`, `_VALUE`, `_THRESHOLD` and `_AT` in the environment. A notifier that
fails is reported and the monitor keeps measuring, because losing the
measurement to a broken webhook would be the worse outcome.

`--alert-cooldown` (default 300 s) holds down repeat commands for the same
probe and metric, so a bad hour notifies you once rather than sixty times. The
alert records themselves are never suppressed - only the command is.

## The QoS tuning workflow

1. `bufferscope.py bufferbloat --repeat 8 --server-id ID --out baseline.json`
2. Change **one** router setting. Typically: enable QoS on the WAN interface and
   set egress bandwidth slightly *below* your measured line rate, so the router
   becomes the bottleneck and owns the queue.
3. `bufferscope.py bufferbloat --repeat 8 --server-id ID --out attempt.json`
4. `bufferscope.py compare baseline.json attempt.json` - keep it only if the change
   is reported SIGNIFICANT.
5. Repeat, moving the shaper up until latency degrades, then step back.

**Always pin `--server-id`** - comparing results from different servers compares
the servers as much as your changes. Pick one from `speedtest --servers` and
keep it for the whole tuning session.

**Always `--repeat`.** Steps 1 and 3 with single runs will hand you a confident
number and a wrong conclusion; see the section above.

### Verifying classification rules

Marking a probe is only half of it. bufferscope reads the mark back off the
socket after setting it, so a marking the OS refused - Windows commonly ignores
`IP_TOS` without a QoS policy - is reported as `NOT applied` rather than
quietly producing a comparison that cannot speak to QoS at all. With a
`--peer`, it goes further and asks the far end what marking actually arrived:

```
marked probe       dscp     applied
  voice            ef       yes
  ef arrived at laptop still marked (10 packets)
```

or, honestly, when the peer's OS cannot see it:

```
  ef to laptop: this OS cannot report the marking of a received packet, so
  the mark could not be checked on the wire
```


`classes` probes each QoS rule with traffic that should match it, alongside a
**control** on a port no rule matches:

```
$ bufferscope.py classes --repeat 8 --server-id ID \
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
passive `bufferscope probe` run *during* a Waveform test reproduced Waveform's
figures, confirming the measurement is sound and the difference is the load.

Two consequences:

- **Comparisons are valid only within one generator.** Every A/B in this
  README used Ookla throughout, so those results stand. Never compare a
  bufferscope figure against a Waveform or fast.com figure.
- **Absolute numbers need their generator attached.** "This line adds 26 ms
  under upload saturation" means *under Ookla's upload saturation*. Under
  ordinary household use the same connection measured 6.9 ms p95.

If you want to know how your link behaves for real traffic, measure real
traffic: `bufferscope probe` generates no load and observes what is actually
happening.

**Attribution.** bufferscope probes your gateway *and* an internet target:

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
| 3 | `monitor` only: an alert threshold was breached |

Exit 2 means the measurement completed but bufferscope does not stand behind it. The
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
  "bufferscope_version": "1.0.0",
  "started_at": "2026-08-21T13:33:07",
  "mode": "bufferbloat",  // or "classes", "probe", "monitor"
  "generator": "ookla",   // or "librespeed"; absent on runs that
                          // predate the option, which were ookla
  "env": {
    "os", "python", "bufferscope_version", "schema_version",
    "gateway",            // detected IPv4 default gateway, or null
    "gateway6",           // IPv6 default gateway, scope included, or null
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
      "throughput_mbps_reported",  // what the generator claimed
      "router_note",               // present instead of a router figure
                                   // when the window was too short for
                                   // the device's counter refresh rate
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
  "validation": {"trustworthy": bool, "reasons": [str]},
  "observed_throughput":  // what was actually flowing during a passive run,
    {"down_mbps", "up_mbps", "seconds", "samples"},
                          // null when too few NIC samples to measure
  "router_throughput":    // the same, from the router, with --router-snmp
    {"down_mbps", "up_mbps", "seconds", "sample_count",
     "counter_transitions", "refresh_interval_s",
     "interface", "source": "snmp",
     "note"},             // present, with null rates, when the window was
                          // too short for that router's refresh rate
  "marking": [            // one entry per marked probe
    {"probe", "dscp", "name",
     "applied"}          // false when the OS refused the mark
  ],
  "dscp_wire": [          // present with --peer and a marked probe
    {"peer", "sent_dscp", "packets", "observed_dscp",
     "survived",         // null when the peer's OS cannot tell
     "note", "error"}
  ],
  "peers": [              // present with --peer
    {"label", "host", "os", "started_at",
     "probes": {"<probe name>": { ...same stats as above... }},
     "error"}            // present instead, if that peer failed
  ],
  "alerts": [             // monitor only
    {"probe", "metric", "value", "threshold", "at"}
  ],
  "devices": [            // present with --router-snmp / --snmp-device
    {"label", "host", "down_mbps", "up_mbps", "interface",
     "sample_count", "source": "snmp",
     "note"}             // present instead of figures when unmeasurable
  ]
}
```

Probe names encode transport and target: `tcp:1.1.1.1:443`, `dns:1.1.1.1`,
`icmp:192.168.1.1`.

### Before you share a result file

Result files are meant to be shared - pasted into a bug report, or sent to a
vendor chasing a fault. Two fields describe you rather than the network:

- **`speedtest.result_url`** links to a public speedtest.net page showing your
  ISP and your approximate location. Ookla publishes that page; bufferscope
  only records the link to it. Drop the field if the recipient does not need it.
- **`env.speedtest_path`** would otherwise carry your account name, so the home
  directory is replaced with `~` before the file is written.

Nothing else in a result file identifies the operator. SNMP community strings
are never written out - a device is recorded by label and host only. The
`server` block describes the speedtest server, not you. Probe targets are
whichever ones you asked for.

`--repeat N` writes a different, wrapping document. The individual runs are
kept in full, so nothing is lost by aggregating:

```
{
  "schema_version": 1,
  "bufferscope_version": "1.0.0",
  "started_at": "2026-08-21T17:15:10",
  "env": { ... },        // the first run's environment
  "repeat": {"n": 5, "completed": 5, "aborted": false},
  "runs": [ ... ],       // every completed run, in the shape above
  "aggregate": {
    "included_runs", "excluded_runs",
    "exclusions": [str],       // why each run was left out
    "speedtest": {"download_mbps" | "upload_mbps" | ...: STATS},
    "<loaded phase>": {"<probe name>": STATS}
                               // added p95 vs that run's own idle
                               // baseline, e.g. "upload": {"dns-class1": ...}
  },
  "validation": {"trustworthy": bool, "reasons": [str]}
}
```

where `STATS` is `{"n", "mean", "stdev", "min", "max", "ci95_lo", "ci95_hi",
"values"}` - `values` keeps the per-run figures so an arm can be re-pooled by
hand.

An arm that aborted early is written in this same shape, with `repeat.aborted`
true and the reason in `validation.reasons`. Arms recorded before that field
existed carry `repeat` as `{"n": N}` alone.

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

- **Latency still comes from hosts, not from the router.** A measurement
  includes the measuring machine's own network stack. `--peer` puts a second
  and third host in the picture, and `--router-snmp` covers throughput
  household-wide, but nothing here measures latency the way the router
  experiences it.
- **Router counters are coarse.** See the granularity note above. bufferscope
  declines to report rather than reporting a biased figure.
- **ICMP needs privileges.** Raw sockets require root on Linux/macOS and admin on
  Windows. Without them bufferscope silently uses TCP and UDP probes, which are
  unprivileged and sub-millisecond. Detail degrades; correctness does not.
- **An external load generator is required.** Self-generated HTTP load was tried
  and abandoned: providers rate-limit concurrent connections per source IP well
  below a gigabit, silently capping the load and invalidating results. Ookla or
  LibreSpeed does the saturating instead.
- **A received DSCP cannot be read on every OS.** The mark a probe asked for is
  read back off the socket everywhere, so a refusal is never silent. Confirming
  that the mark was still there *on arrival* needs `recvmsg`, which Windows
  does not implement: a Windows peer reports that it cannot tell rather than
  reporting an unmarked packet as though the router had stripped it.
- **Not a router configuration tool.** It measures; you make the changes.

## Testing

```bash
python -m unittest test_bufferscope -v          # 359 tests, offline
BUFFERSCOPE_E2E=1 python -m unittest test_bufferscope.TestEndToEnd -v
```

The default suite requires no network, no root and no particular OS - platform
parsers are tested against captured command output from Windows, Linux and
macOS. Agent and peer tests run a real server on loopback rather than mocking
the protocol. Two tests skip themselves where the platform cannot support them:
reading a received DSCP needs `recvmsg`, and the IPv6 socket tests need a
usable `::1`. The end-to-end test is opt-in, takes about 20 seconds and uses roughly
1.4 GB of data.

## Licence

[PolyForm Noncommercial 1.0.0](https://polyformproject.org/licenses/noncommercial/1.0.0)
- free for personal use, research, teaching, charities, government and public
bodies. Commercial use is not permitted, which includes running it as part of
paid work for a business.

Not an OSI-approved open source licence, so it will not be accepted by
distributions or package repositories that require one.
