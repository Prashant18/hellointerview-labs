"""Lab 03 — SSE fanout latency probe.

Subscribes to GET /v1/items/{id}/stream via httpx async streaming, fires N
bids in parallel, measures per-frame latency (recv_ms − server commit_ts),
asserts p95 < budget. Run from the host: `uv run python scripts/sse_latency.py`.
"""

from __future__ import annotations

import asyncio
import json
import os
import statistics
import sys
import time

import httpx

API = os.getenv("API_URL", "http://localhost:18001")
N_BIDS = 5
INTERVAL_S = 0.3
P95_BUDGET_MS = float(os.getenv("P95_BUDGET_MS", "200"))


async def main() -> int:
    async with httpx.AsyncClient(timeout=20.0) as client:
        r = await client.post(
            f"{API}/v1/items",
            json={"title": "probe", "start_price": 10, "end_time_epoch": 9999999999},
        )
        r.raise_for_status()
        item_id = r.json()["item_id"]

        latencies_ms: list[float] = []
        ready = asyncio.Event()

        async def watcher() -> None:
            async with client.stream("GET", f"{API}/v1/items/{item_id}/stream") as resp:
                resp.raise_for_status()
                ready.set()
                async for line in resp.aiter_lines():
                    if not line.startswith("data: "):
                        continue
                    recv_ms = time.time() * 1000.0
                    try:
                        ev = json.loads(line[len("data: "):])
                    except json.JSONDecodeError:
                        continue
                    if "ts" in ev:
                        latencies_ms.append(recv_ms - float(ev["ts"]))
                    if len(latencies_ms) >= N_BIDS:
                        return

        async def bidder() -> None:
            await ready.wait()
            await asyncio.sleep(0.2)  # let subscribe propagate to Redis
            for i in range(N_BIDS):
                resp = await client.post(
                    f"{API}/v1/items/{item_id}/bids",
                    json={"bidder": f"probe-{i}", "amount": 100 + 10 * i},
                )
                if resp.status_code != 201:
                    print(f"bid {i} failed: {resp.status_code} {resp.text}", file=sys.stderr)
                await asyncio.sleep(INTERVAL_S)

        try:
            await asyncio.wait_for(asyncio.gather(watcher(), bidder()), timeout=15.0)
        except asyncio.TimeoutError:
            print(f"FAIL: timeout, got {len(latencies_ms)}/{N_BIDS} events", file=sys.stderr)
            return 1

    if len(latencies_ms) < N_BIDS:
        print(f"FAIL: expected {N_BIDS} events, got {len(latencies_ms)}", file=sys.stderr)
        return 1

    avg = statistics.mean(latencies_ms)
    p95 = max(latencies_ms) if len(latencies_ms) < 20 else statistics.quantiles(latencies_ms, n=20)[18]
    print(f"events={len(latencies_ms)} avg={avg:.1f}ms p95={p95:.1f}ms budget={P95_BUDGET_MS}ms")
    if p95 > P95_BUDGET_MS:
        print(f"FAIL: p95 {p95:.1f}ms > budget {P95_BUDGET_MS}ms", file=sys.stderr)
        return 1
    print("PASS")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
