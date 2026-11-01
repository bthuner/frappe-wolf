# Load test: 100 hosts for 7 days (#10)

Measured on 2026-11-01, twice:
- first as #55 found it;
- then after the row order and inventory index of the next section.

The target is #10's "done when": a synthetic load of 100 hosts for 7 days keeps `push` p95 under
200 ms and the overview under 1 s.

| Target | Before (#55) | After |
|---|---|---|
| `push` p95 under 200 ms | **Met.** 115 ms over HTTP | **Met.** 108 ms over HTTP |
| Desk overview under 1 s | Met, except its 24 h chart: 2.1 s | Met, except its 24 h chart: 1.8 s |
| Wolf UI overview under 1 s | **Not met: 13.8 to 23.8 s** | **Not met: 1.5 s** |

All runs used synthetic data and a demo bench, not production observations.

## What changed

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

## Data and machine

- Data, written by [`ci/load_test.py`](../ci/load_test.py) on a throwaway site:
  - 100 hosts × 15 metrics × 7 days, samples every 30 s;
  - raw samples for the last 48 h (8.7 M rows);
  - 1-minute and 1-hour rollups before that, then the real rollup job over the raw window
    (15.5 M rollups);
  - 16,900 events (one per host per hour).
- Rows are named as the app names them. #55's run named its synthetic rows in sequence, so in
  time order. Only the last 48 h, written by the rollup job, had the scattered layout of a real
  site. A real site was therefore slower than #55 measured, not faster.
- Bench: the [demo stack](../dev/demo/README.md) in Docker, on 4 vCPU and 15 GB RAM:
  - Dodock 5.18.8;
  - gunicorn, 2 workers × 4 threads;
  - MariaDB 10.6 with its default 128 MB `innodb_buffer_pool_size`.
- Loading took 34 min, down from 75. The rollup job caught up on 48 h of raw samples at once in
  12.5 min, down from 32. A running site never does that.

## Results

In milliseconds. Reads: p95 of 10 runs. Pushes: over every timed push.

**Push, server side** (`ingest.accept()`, 200 pushes of 15 metrics):

| Mode | Before p95 | After p95 |
|---|---|---|
| async (validate and queue) | 16.5 | 15.2 |
| store_batch (the worker) | 9.8 | 11.0 |
| sync | 22.6 | 20.1 |

**Push, over HTTP** ([`ci/load_http.py`](../ci/load_http.py)):
- the agent's request with its headers, straight to gunicorn;
- 600 pushes from 100 agents, through 8 connections at once.

| | Statuses | p50 | p95 | max | Throughput |
|---|---|---|---|---|---|
| Before | 600 × 200 | 64.4 | 114.9 | 170.4 | 116 pushes/s |
| After | 600 × 200 | 58.0 | 107.9 | 195.3 | 125 pushes/s |

100 hosts every 30 s need 3.3 pushes/s.

**Desk overview** (`ui.get_overview` plus one host chart):

| Range | 1 h | 6 h | 24 h | 7 d |
|---|---|---|---|---|
| Before | 176 | 606 | **2,102** | 240 |
| After | 207 | 516 | **1,754** | 214 |

The 24 h chart reads one host's minute rollups. The new order keeps a metric together, not a
host. That host's rows are still one per page.

**Wolf UI overview** (`/wolf`). Its six reads go out at once, so a page waits at least for the
slowest one:
- the reads were timed one after another;
- run in parallel, they share the CPU, the disk and the buffer pool, so the page itself is
  slower.

| Read | Before | After |
|---|---|---|
| **get_signals** | **7,971 to 10,569** | **1,471** |
| **get_inventory** | **13,836 to 23,799** | 870 |
| get_counts (1 h to 7 d) | 6.8 to 75.3 | 4.2 to 29.1 |
| get_http (1 h to 7 d) | 7.3 to 16.7 | 4.0 to 7.6 |
| list_incidents | 0.9 to 4.3 | 1.1 |
| get_monitoring | 11.4 to 15.0 | 41.1 |
| **Page, slowest read** | **13,836 to 23,799** | **1,471** |

#55 timed the reads that ignore the range once per range: its ranges there are run-to-run
spread.

**Other reads:**

| Read | 1 h | 6 h | 24 h | 7 d |
|---|---|---|---|---|
| Metrics explorer, cpu_usage by host, before | 1,757 | 7,126 | **25,762** | 2,467 |
| Metrics explorer, after | 219 | 914 | **2,936** | 506 |

| Rule query | Before p95 | After p95 |
|---|---|---|
| 5 min avg by host | 1.7 | 6.2 |
| 24 h max by host | **11,355** | 906 |
| 7 d last by host | 2,244 | 261 |

## What is left over 1 s

These reads now wait on the CPU, not on the disk. They read 20 k to 300 k rows, each with a
B-tree lookup, and Python merges them.

- `get_signals`, 1.5 s. Three queries:
  - the newest minute rollups;
  - the last value of each series;
  - the hourly rollups of 7 days, for series that went quiet.
- The metrics explorer over 24 h, 2.9 s:
  - it reads one metric's 144 k minute rollups twice, once for the totals and once for the
    chart;
  - the chart's buckets are merged in Python.
- Desk's 24 h host chart, 1.8 s: one host's rows are spread one per page.

An index does not shorten these. What would:
- chart buckets computed in SQL rather than in Python;
- one read instead of two in the explorer;
- the latest value of each series kept where `get_signals` can read it without scanning.

## Not measured

- A browser's full page time.
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

`load_http.py` comes second:
- Its agents report as 100 more hosts (`load-http-NNN`), which the reads would then include. The
  next `load_test.py` run deletes them.
- It raises the site's push limit for the run and puts it back afterwards.
- Its p50 / p95 are over accepted pushes (HTTP 200) only. Every other status or connection error
  is counted separately.
