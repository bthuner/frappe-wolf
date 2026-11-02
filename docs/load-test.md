# Load test: 100 hosts for 7 days (#10)

Measured on 2026-11-01, three times:
- first as #55 found it;
- then after the row order and inventory index of #56;
- then after #57 moved the remaining reads to SQL (see "What changed").

The target is #10's "done when": a synthetic load of 100 hosts for 7 days keeps `push` p95 under
200 ms and the overview under 1 s.

| Target | Before (#55) | Row order (#56) | SQL reads (#57) |
|---|---|---|---|
| `push` p95 under 200 ms | **Met.** 115 ms over HTTP | **Met.** 108 ms over HTTP | **Met.** 20.7 ms server side (HTTP not run again) |
| Desk overview under 1 s | Met, except its 24 h chart: 2.1 s | Met, except its 24 h chart: 1.8 s | **Met.** 0.56 s at most (24 h) |
| Wolf UI overview under 1 s | **Not met: 13.8 to 23.8 s** | **Not met: 1.5 s** | **Met per read: 0.73 s for the slowest** |
| Wolf UI overview page in a browser | | | Without cache: p50 0.82 to 0.97 s, p95 1.00 to 1.26 s. **With the cache, met: p95 0.61 s; after 45 s or 150 s idle, max 0.65 s** |

The page time is from navigation start until every panel is drawn, in Chromium on the bench
(see "The page in a browser"). Without the cache its median is under 1 s, its p95 is not. With
it, and with it read again in the background (see "The short cache"), visits after 45 s and
150 s idle were all served from it. A visit that still reads (the first of the day, a scope
nobody opened for 10 min, or the scheduler at its default 4 min tick) takes the time without
cache.

All runs used synthetic data and a demo bench, not production observations.

## What changed

### Row order and inventory index (#56)

1. **Row order.** InnoDB stores a table's rows in primary key order, and Frappe's primary key
   is `name`.
   - Before, metric samples got a random name and rollups a hash of their key. One metric's
     day was spread over the whole table, and each row of a read was a page read of its own.
   - Now names start with the resolution, a 6-character hash of the metric and the time, then
     end with a hash of the key ([`storage.rollup_name`](../frappe_wolf/wolf/storage.py),
     `sample_name`). A time range of one metric is read from neighbouring pages.
   - Rows are inserted in name order, so InnoDB fills its pages instead of splitting them in
     the middle.
2. **Inventory index.** `get_inventory` asks which host, bench and site sent anything in a
   range.
   - MariaDB answered through `(resolution, metric_name, bucket_start)`, then fetched every row
     of the range.
   - `(resolution, bucket_start, host, bench, site)` answers it from the index alone.
   - It replaces `(resolution, bucket_start)`, which it starts with. That index served the
     rollup job and the purge, and still does. `bucket_start` loses its own index too: no read
     filters it without a resolution.

A first try put the full metric name in the row name (61 characters). That made the rollup
table 50 % bigger and filled the bench's disk:
- every secondary index entry repeats the primary key;
- rows of one bucket arrived in hash order, so pages split in the middle.

The names are now 33 characters for rollups and 28 for samples, against 32 and 12 before.

| Table (data + indexes) | Before | After |
|---|---|---|
| Wolf Metric Rollup | 7.2 GB for 15.2 M rows | 7.3 GB for 15.5 M rows |
| Wolf Metric Sample | 2.2 GB for 8.7 M rows | 3.1 GB for 8.7 M rows (kept 48 h) |

**On an existing site:**
- `bench migrate` creates the index and drops the two it replaces. On 15 M rows, that took
  1.5 min.
- Rows already stored keep their names, so a read speeds up once its window holds only new
  rows: after a day for the 24 h explorer, 7 days for the hourly reads.

### SQL reads (#57)

1. **Name ranges.** A read of known metrics scans one primary key range per metric
   ([`rollups.name_passes`](../frappe_wolf/wolf/rollups.py)): rows of one metric and resolution
   sit together in the key since #56, so a window is a stretch of it. No row is looked up
   through a secondary index. Postgres does not store rows in key order and keeps one plain
   read.
2. **Chart buckets in SQL** (`rollups.bucket_cells`). The explorer read one metric's 144 k
   minute rollups twice, for the totals and the chart, and merged them in Python. It now reads
   one row per host and bucket, summed by the database; the totals are the merge of those.
3. **Last values in one pass.** The value at each series' newest time came from a join of the
   rows with their own group-by. On fresh data MariaDB turned that join into a lookup per
   series, each walking all of its metric's minute rollups: `get_signals` took **194 to 233 s**
   (two runs). `row_number()` over each series reads the rows once: 0.62 s.
4. **Host chart index.** `(resolution, host, metric_name, bucket_start)` replaces
   `(resolution, host, bucket_start)`: Desk's host chart reads four metrics of one host, no longer
   all fifteen.

The 194 s were missed at first. #56's "after" reads ran on data 40 to 45 min old, and a later
run on data 85 to 103 min old: `get_signals` reads the last 2 h, so it saw part of its window.
One read of 243 s in that later run, taken for an outlier, was this. Only a full run on fresh
data showed it on every read.

**Rows named before #56.** Their names are 32 hex digits and sort below every new name, outside
the name ranges. While some remain, a second pass reads them (`name < 'g'`), and MariaDB weighs
it against the indexes. Measured with the 153 k minute rollups of one metric's last day renamed
to old names, explorer over 24 h:

| Second pass | Plan | Median |
|---|---|---|
| `name < 'g'` | primary key, old rows only | 770 ms |
| `left(name, 1) < 'g'` (a function: indexes only) | `(resolution, metric_name, bucket_start)` | 1,145 ms |

With millions of old rows, the primary key range would cost more than the index and the
optimizer takes the index. The plain range stays. Old rows go with retention: 30 days for
minute rollups, 365 for hour rollups, by default.

## Data and machine

- Data, written by [`ci/load_test.py`](../ci/load_test.py) on a throwaway site:
  - 100 hosts × 15 metrics × 7 days, samples every 30 s;
  - raw samples for the last 48 h (8.7 M rows);
  - 1-minute and 1-hour rollups before that, then the real rollup job over the raw window
    (15.3 to 15.5 M rollups);
  - 16,900 events (one per host per hour).
- Rows are named as the app names them. #55's run named its synthetic rows in sequence, so in
  time order. Only the last 48 h, written by the rollup job, had the scattered layout of a real
  site. A real site was therefore slower than #55 measured, not faster.
- All hosts' samples of one instant go in at once, in name order. Real pushes arrive host by host
  and a little out of time order, so a real sample table is somewhat less dense than this one.
- The data end when loading starts, and read windows end when they are read:
  - the full run read 20 to 35 min after the newest data;
  - #56's "after" reads (`--reads-only`, once the index was in) read 40 to 45 min after it;
  - #57's run loaded everything again and read straight after, like the first.

  Between the two runs, reads the index does not touch moved by 11 % at most. `--reads-only`
  refuses data over 2 h old.
- Bench: the [demo stack](../dev/demo/README.md) in Docker, on 4 vCPU and 15 GB RAM:
  - Dodock 5.18.8;
  - gunicorn, 2 workers × 4 threads;
  - MariaDB 10.6 with its default 128 MB `innodb_buffer_pool_size`.
- Loading took 34 min, down from 75 (#56), then 20 min (#57). The rollup job caught up on 48 h of
  raw samples at once in 12.5 min, down from 32, then 9.5 min. A running site never does that.
- #57's numbers were taken before its review fixes. Those keep only the charted groups'
  buckets in memory and let `get_http` use name ranges: neither adds a read.

## Results

In milliseconds. Reads: p95 of 10 runs. Pushes: over every timed push.

**Push, server side** (`ingest.accept()`, 200 pushes of 15 metrics):

| Mode | Before p95 | #56 p95 | #57 p95 |
|---|---|---|---|
| async (validate and queue) | 16.5 | 15.2 | 15.8 |
| store_batch (the worker) | 9.8 | 11.0 | 10.5 |
| sync | 22.6 | 20.1 | 20.7 |

**Push, over HTTP** ([`ci/load_http.py`](../ci/load_http.py)):
- the agent's request with its headers, straight to gunicorn;
- 600 pushes from 100 agents, through 8 connections at once.

| | Statuses | p50 | p95 | max | Throughput |
|---|---|---|---|---|---|
| Before | 600 × 200 | 64.4 | 114.9 | 170.4 | 116 pushes/s |
| After (#56) | 600 × 200 | 58.0 | 107.9 | 195.3 | 125 pushes/s |

100 hosts every 30 s need 3.3 pushes/s. #57 does not touch the push path; HTTP was not timed
again.

**Desk overview** (`ui.get_overview` plus one host chart):

| Range | 1 h | 6 h | 24 h | 7 d |
|---|---|---|---|---|
| Before | 176 | 606 | **2,102** | 240 |
| #56 | 207 | 516 | **1,754** | 214 |
| #57 | 170 | 180 | 559 | 149 |

The 24 h chart reads one host's minute rollups, and the row order keeps a metric together, not
a host: that host's rows are one per page. With the metric in the host index (#57), it fetches
the four charted metrics' rows only.

**Wolf UI overview** (`/wolf`). Its six reads go out at once, so a page waits at least for the
slowest one:
- the reads were timed one after another;
- run in parallel, they share the CPU, the disk and the buffer pool, so the page itself is
  slower.

| Read | Before | #56 | #57 |
|---|---|---|---|
| **get_signals** | **7,971 to 10,569** | **1,471** (on older data, see above) | 617 |
| **get_inventory** | **13,836 to 23,799** | 870 | 726 |
| get_counts (1 h to 7 d) | 6.8 to 75.3 | 4.2 to 29.1 | 3.6 to 40.7 |
| get_http (1 h to 7 d) | 7.3 to 16.7 | 4.0 to 7.6 | 4.3 to 11.7 |
| list_incidents | 0.9 to 4.3 | 1.1 | 0.9 |
| get_monitoring | 11.4 to 15.0 | 41.1 | 10.2 |
| **Page, slowest read** | **13,836 to 23,799** | **1,471** | **726** |
| The six one after another | | | 1,359 to 1,398 |

#55 timed the reads that ignore the range once per range: its ranges there are run-to-run
spread.

### The page in a browser

[`frontend/scripts/page-time.mjs`](../frontend/scripts/page-time.mjs) opens `/wolf` in headless
Chromium 153, logged in, through the bench's nginx. A page is ready once its six reads have
answered and no panel says it is loading. Two series of 10 runs each:
- cold: a new browser context each time, so the app's scripts and styles are fetched again;
- warm: the same tab navigating again, with its cache.

Two runs, straight after a fresh load of the same data, in ms:

| | Cold p50 | Cold p95 | Warm p50 | Warm p95 |
|---|---|---|---|---|
| **Page ready** | **971 to 973** | **1,129 to 1,262** | **887 to 892** | **1,008 to 1,134** |
| App shell (DOMContentLoaded) | 109 | 134 to 140 | 79 to 85 | 103 to 143 |
| First read sent (app up, session checked) | 211 to 213 | 254 to 262 | 103 to 110 | 130 to 166 |
| Reads, first sent to last answered | 568 to 569 | 631 to 705 | 586 to 595 | 738 to 801 |
| Drawing, last answer to panels drawn | 189 | 270 | 120 | 317 |

Where the time goes, at the median of a cold page:
- 0.21 s before the first read: the app's scripts, then its session check.
- 0.57 s of reads. They go out at once and overlap well:
  - `get_inventory`, 0.56 s, and `get_signals`, 0.51 s, set the length;
  - the other four answer in 23 to 62 ms.
  
  In parallel, each takes about what it takes alone (0.57 s and 0.56 s in-process): the bench's
  two gunicorn workers run them side by side.
- 0.19 s to draw them: the signals table holds its first 300 series, the inventory 101 hosts.

So the p95 goes over 1 s by up to a quarter of a second, in the two slow reads and in drawing.

The demo's nginx serves one site whatever the host name (`wolf.localhost`). The measurement used
a second nginx in the same container, on another port, with the same configuration set to the
load site. A first run through the demo's own nginx timed the demo site, not the load: it is not
counted.

### The short cache

`get_inventory` and `get_signals` keep what they read for 30 s (`wolf_read_cache_seconds`,
[`wolf/read_cache.py`](../frappe_wolf/wolf/read_cache.py)). The cached value is read without
access conditions, shared by every caller, and each request keeps what its caller may see
([docs/ui-api.md](ui-api.md#short-cache-get_inventory-and-get_signals)). Rules and incidents
are read per request.

The same page, on one fresh load, in ms:

| `/wolf`, page ready | Cold p50 | Cold p95 | Warm p50 | Warm p95 |
|---|---|---|---|---|
| Cache off (two runs) | 924 to 955 | 1,134 to 1,169 | 822 to 851 | 1,003 to 1,106 |
| **Served from the cache** | **529** | **608** | **280** | **457** |

| Served from the cache | Cold p50 | Warm p50 |
|---|---|---|
| First read sent | 237 | 91 |
| Reads, first sent to last answered | 62 | 58 |
| `get_inventory` / `get_signals` | 28 / 59 | 22 / 46 |
| Drawing | 228 | 127 |

- With the reads at 60 ms, the page is the app's start (0.1 to 0.25 s) and drawing (0.13 to
  0.23 s at the median, up to 0.32 s).
- In the "served from the cache" runs, every page came within 30 s of a read, so all of them hit.
  How often a real page does depends on how often the overview is opened; at most one visit per
  30 s, per scope, pays the read.
- The second "cache off" run was meant to be a cached one: it started seconds after
  `wolf_read_cache_seconds` was set, and Dodock's web workers keep the site config for up to
  60 s. They still read 0.

**Read again in the background.** A cached value is fresh for 30 s. Up to 2 min old, it is still
served at once while one background job reads it again; and every minute the scheduler reads
again the values asked for in the last 10 minutes. Timed on another fresh load, with the load
site's scheduler on and `scheduler_tick_interval` at 60, one cold page every 45 s, then every
150 s (6 visits each), in ms:

| `/wolf`, page ready | p50 | p95 | max | `get_inventory` / `get_signals`, p50 |
|---|---|---|---|---|
| Every 45 s | 548 | 649 | 649 | 36 / 78 |
| Every 150 s | 563 | 654 | 654 | 49 / 70 |

- Every visit was served from the cache. At 150 s, past the 2 min a value is kept, only the
  scheduler's read could have kept it: without it, each of those visits would read (0.9 to 1.2 s,
  as without cache).
- The data such a page shows is older. `as_of` sampled 8 times, 40 s apart: 11 to 51 s old, 40 s
  for most. A value read by the scheduler can be up to its minute plus a tick old; at most 2 min,
  after which a request reads.
- A first try of the 45 s series had one 3.2 s page. It came right after `bench clear-cache`,
  run to register the new scheduled job, which empties all of Frappe's cache (its app shell took
  1.8 s): it is not counted. On a site, `bench migrate` registers the job.

**Drawing the first rows.** The signals panel draws the first 10 series of each group, the
inventory its first 20 hosts; a button under each shows the rest, and how many of the hidden
series fire or have no new data. The server sorts problems first, so those are the rows drawn;
the counts per group stay exact. Timed without a bench by
[`frontend/scripts/draw-time.mjs`](../frontend/scripts/draw-time.mjs): the production build,
the API mocked in Chromium with 300 series over 8 groups and 101 hosts of one bench and two
sites each, a new browser context each run, from the last answer to the next frame. 20 runs, on
a laptop, not the bench, in ms:

| Drawing | p50 | p95 / max | List items drawn |
|---|---|---|---|
| Every row (before) | 193 | 208 | 503 |
| **First rows** (two series) | **131 to 134** | **148 to 150** | 121 |
| One series, one host (`--empty`) | 97 to 112 | 126 to 128 | 4 |

The rows cost 80 to 100 ms; 60 ms of it is gone. The rest of the drawing is the page's own,
rows or not.

**The same, on a load bench.** The demo stack (`dev/demo`) on a 16-thread laptop, with a
throwaway site loaded by `ci/load_test.py` (100 hosts × 15 metrics × 7 days: 11.0 M rollups,
8.7 M samples) and a second nginx for it. The overview gets 300 series, all host resources, and
101 hosts. `page-time.mjs`, 10 runs per series, both UI builds swapped into the same containers
and timed twice each, alternating, in ms:

| `/wolf`, p50 (p95) | Cold, page ready | Cold, drawing | Warm, page ready | Warm, drawing |
|---|---|---|---|---|
| Every row (before) | 234 to 236 (248 to 258) | 100 to 103 (115) | 118 to 126 (129 to 154) | 48 to 51 (62 to 66) |
| **First rows** | **180 to 188 (190 to 206)** | **53 to 55 (56 to 63)** | **80 to 81 (92 to 101)** | **18 to 21 (27 to 30)** |

- Drawing halves cold and drops by 60 % warm: 47 to 50 ms and 27 to 33 ms less. A page opened
  cold is 50 ms faster, a warm one 40 ms.
- Every page was served from the short cache (`get_signals` about 20 ms, `get_inventory` about
  8 ms), so the drawing is what changed; the app's start (first read at 105 to 110 ms cold) did
  not.
- This machine is several times faster than the 4 vCPU bench of the tables above: the uncached
  `get_inventory` took 0.46 s here against 0.73 s there, the whole cached page 0.18 s against
  0.53 s. The bench was not run again; at its speed, the same share of its 0.13 to 0.23 s of
  drawing is about 0.06 to 0.1 s.

**Other reads:**

| Read | 1 h | 6 h | 24 h | 7 d |
|---|---|---|---|---|
| Metrics explorer, cpu_usage by host, before | 1,757 | 7,126 | **25,762** | 2,467 |
| Metrics explorer, #56 | 219 | 914 | **2,936** | 506 |
| Metrics explorer, #57 | 173 | 390 | 863 | 281 |

| Rule query | Before p95 | #56 p95 | #57 p95 |
|---|---|---|---|
| 5 min avg by host | 1.7 | 6.2 | 7.3 |
| 24 h max by host | **11,355** | 906 | 468 |
| 7 d last by host | 2,244 | 261 | 67 |

## What is left

Each read is under 1 s, and so is the overview page when its two slow reads come from the cache.
- A visit that reads still takes 0.9 to 1.2 s: the first after 10 quiet minutes on a scope, or
  after 2 minutes when the scheduler ticks every 4 min (Dodock's default; the README sets 60 s).
- Drawing, 0.13 to 0.32 s on the bench with every row. Drawing the first rows only halves it
  on a faster load bench (100 to 53 ms cold, 50 to 20 ms warm); the rest is the page's own. The
  4 vCPU bench was not run again.
- The metrics explorer over 24 h, 0.86 s, is the closest other read.

These margins are for 100 hosts on this bench. At several times that, the inventory and the 24 h
reads grow with the rows of their window, and so does a visit that reads.

## Not measured

- A user's network: the page was opened on the bench itself, without latency.
- Browsers other than Chromium, and slower machines than this bench's 4 vCPU.
- Rows named before #56 at the scale of an upgraded site: only one metric's day was renamed.
- Postgres. CI installs the app there, which creates the new index, but nothing was timed and
  the upgrade from the old indexes was run on MariaDB only. Postgres stores rows roughly in
  insertion order, which is already time order, so row names matter less there.
- Several sites at once.
- Rule evaluation of every preset at once.
- Disk other than this container's.

## Run it again

```bash
# on a throwaway site of the bench, from sites/
../env/bin/python /path/to/ci/load_test.py <site> --hosts 100 --metrics 15 --days 7
../env/bin/python /path/to/ci/load_http.py <site> --url http://127.0.0.1:8000 --agents 100 --pushes 600 --concurrency 8
```

`load_test.py` deletes and rewrites the site's metric tables: never point it at a real site.
- To time the reads again on the data already loaded, for instance after an index change, add
  `--reads-only`.
- On a site loaded before, empty the three tables first (`truncate`). The script deletes its own
  rows one transaction per table, which on 20 M rows takes long and needs as much undo space.

The page in a browser, from `frontend/` once `yarn install` has run:

```bash
node scripts/page-time.mjs --url http://127.0.0.1:8080 --site <site> --password <admin password> --runs 10
```

- `--url` is the bench's nginx. It must serve `<site>`: the demo's serves `wolf.localhost` only
  (see "The page in a browser").
- `--path` and `--reads` time another page: its path, and how many Wolf reads it sends.

`load_http.py` comes second:
- Its agents report as 100 more hosts (`load-http-NNN`), which the reads would then include. The
  next `load_test.py` run deletes them.
- It raises the site's push limit for the run and puts it back afterwards.
- Its p50 / p95 are over accepted pushes (HTTP 200) only. Every other status or connection error
  is counted separately.
