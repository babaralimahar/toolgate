# ToolGate overhead (stubbed downstream)

2026-11-15 17:15 UTC · 500 sequential allowed calls · local Postgres 17 (Docker) · win32

| Measure | Value |
|---|---|
| Gateway overhead p50 | 33.35 ms |
| Gateway overhead p95 | 51.51 ms |
| Gateway overhead p99 | 66.60 ms |
| Mean | 32.45 ms |
| `decide()` alone | 13.8 µs |
| Audit chain after run | intact (1040 rows) |

Method: in-process `Gateway.call` with an httpx MockTransport downstream that answers instantly, so the numbers are the governance path only (token hash lookup, validation, cached kill-switch read, decide(), output labelling, advisory-locked hash-chained audit insert). Sequential calls; concurrent writers serialise on the audit lock, which bounds audited throughput per database.

## Where the time goes (measured on this machine)

| Step | Cost |
|---|---|
| Pooled session checkout (pre-ping + reset), Docker Desktop on Windows | ~20 ms |
| One query on an open session | ~3 ms |
| Audit append = 1 checkout + 4 round trips (lock, read chain head, insert, commit) | ~30 ms |
| Before the 1 s identity cache (one more checkout per call) | p50 55 ms, p95 75 ms |

So the overhead is database round trips, not policy logic (`decide()` is microseconds). A Linux host
with Postgres on the same network typically has sub-millisecond round trips; that was not measured
here. Next steps if it matters: write the audit row in one statement (hash in SQL), or batch audit
rows asynchronously with a write-ahead buffer (trading the "no unaudited action" guarantee for latency).
