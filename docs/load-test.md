# Load test: 100 hosts for 7 days (#10)

Measured on 2026-11-01. The target is #10's "done when": a synthetic load of 100 hosts for 7 days
keeps `push` p95 under 200 ms and the overview under 1 s.

| Target | Result |
|---|---|
| `push` p95 under 200 ms | **Met.** 115 ms over HTTP (100 agents through 8 connections at once), 16.5 ms server side. |
| Overview under 1 s | **Met by Desk's overview, except its 24 h chart. Not met by the Wolf UI's overview: 13 to 24 s.** |

Both runs used synthetic data and a demo bench, not production observations.

## Data and machine

- Data, written by [`ci/load_test.py`](../ci/load_test.py) on a throwaway site:
  - 100 hosts × 15 metrics × 7 days, samples every 30 s;
  - raw samples for the last 48 h (8.72 M rows);
  - 1-minute and 1-hour rollups before that, then the real rollup job over the raw window
    (15.2 M minute and 252 k hour rollups);
  - 16,900 events (one per host per hour).
- Bench: the [demo stack](../dev/demo/README.md) in Docker, on 4 vCPU and 15 GB RAM:
  - Dodock 5.18.8;
  - gunicorn, 2 workers × 4 threads;
  - MariaDB 10.6 with its default 128 MB `innodb_buffer_pool_size`.
- Table sizes: Wolf Metric Rollup 6.2 GB, Wolf Metric Sample 2.1 GB.
- Loading took 75 min. 32 min of it was the rollup job catching up on 48 h of raw samples at once,
  which a running site never does.

## Results

In milliseconds. Reads: p95 of 10 runs. Pushes: over every timed push.

**Push, server side** (`ingest.accept()`, 200 pushes of 15 metrics):

| Mode | p50 | p95 | max |
|---|---|---|---|
| async (validate and queue) | 11.5 | 16.5 | 51.1 |
| store_batch (the worker) | 5.2 | 9.8 | 22.8 |
| sync | 15.3 | 22.6 | 38.6 |

**Push, over HTTP** ([`ci/load_http.py`](../ci/load_http.py), the agent's request with its
headers, straight to gunicorn):

| Pushes | Concurrency | Statuses | p50 | p95 | max | Throughput |
|---|---|---|---|---|---|---|
| 600 | 8 | 600 × 200 | 64.4 | 114.9 | 170.4 | 116 pushes/s |

100 hosts every 30 s need 3.3 pushes/s.

**Desk overview** (`ui.get_overview` plus one host chart):

| Range | 1 h | 6 h | 24 h | 7 d |
|---|---|---|---|---|
| overview + chart | 176 | 606 | **2,102** | 240 |

The 24 h chart alone takes 1,899 ms: it reads 1-minute rollups.

**Wolf UI overview** (`/wolf`). Its six reads go out at once, so a page waits at least for the
slowest one: the reads were timed one after another, and run in parallel they share the CPU, the
disk and the buffer pool, so the page itself is slower.

| Read | 1 h | 6 h | 24 h | 7 d |
|---|---|---|---|---|
| get_counts | 6.8 | 10.4 | 29.4 | 75.3 |
| get_http | 7.3 | 16.6 | 16.7 | 12.5 |
| **get_signals** | **10,569** | **9,873** | **9,002** | **7,971** |
| list_incidents | 1.0 | 1.0 | 0.9 | 4.3 |
| **get_inventory** | **23,799** | **16,887** | **21,214** | **13,836** |
| get_monitoring | 13.2 | 11.4 | 15.0 | 11.7 |

Only `get_counts` and `get_http` follow the range. The four others ignore it: in their rows the
four columns are four series of the same read, and their spread (8.0 to 10.6 s, 13.8 to 23.8 s)
is run-to-run variation. `load_test.py` now times them once.

**Other reads:**

| Read | 1 h | 6 h | 24 h | 7 d |
|---|---|---|---|---|
| Metrics explorer, cpu_usage by host | 1,757 | 7,126 | **25,762** | 2,467 |

| Rule query | p95 |
|---|---|
| 5 min avg by host | 1.7 |
| 24 h max by host | **11,355**, at every evaluation |
| 7 d last by host | 2,244 |

## Why

Reads that cover hours of 1-minute rollups, or days of hourly ones, fetch every matching row
from the table. Three things make that slow.

1. **Row order.** Wolf Metric Sample and Wolf Metric Rollup are named by random hash, and
   InnoDB stores rows in name order. One metric's rows for one day are therefore spread over the
   whole 6.2 GB table, and each is a random read.
2. **Cache size.** The default 128 MB buffer pool holds none of the 6 GB working set.
   - With 4 GB, set online and warmed by a first run: `get_signals` 1.4 s, `get_inventory` 1.8 s,
     explorer 24 h 2.9 s, rule 24 h 1.1 s.
   - Better, but still over 1 s: memory alone does not meet the target.
3. **Index choice.** The inventory groups hourly rollups by host, bench and site, and
   MariaDB picks `(resolution, host, bucket_start)`, which cannot use the time range.
   - A covering index `(resolution, host, bench, site, bucket_start)`, tried on the load site,
     cut its two queries from 8.0 s to 0.15 s and from 5.5 s to 0.43 s. They are then read from
     the index alone.
   - It is not in the app yet.

## Not measured

- A browser's full page time.
- Postgres.
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

`load_http.py` comes second. Its agents report as 100 more hosts (`load-http-NNN`), which the reads
would then include; the next `load_test.py` run deletes them. It raises the site's push limit for
the run and puts it back afterwards. Its p50 / p95 are over accepted pushes (HTTP 200) only; every
other status or connection error is counted separately.
