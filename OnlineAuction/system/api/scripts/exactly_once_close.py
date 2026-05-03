"""Lab 05 — exactly-once auction close chaos probe.

Creates N auctions ending in ~2s. Waits a window long enough for at least
2 closer cycles + Prometheus scrape. Asserts:
  (a) every item has `closed_by` set in DDB (no misses)
  (b) sum(auctions_closed_total) == N from Prometheus (no dupes; the
      conditional UpdateItem made each close exactly-once)
  (c) prints per-replica counter values so distribution is visible.
"""

from __future__ import annotations

import asyncio
import os
import sys
import time

import httpx

API = os.getenv("API_URL", "http://localhost:18001")
PROM = os.getenv("PROM_URL", "http://localhost:19091")
N = int(os.getenv("N", "100"))
WINDOW_S = int(os.getenv("WINDOW_S", "20"))


async def main() -> int:
    async with httpx.AsyncClient(timeout=30.0) as client:
        end_time = int(time.time()) + 2
        print(f"creating {N} items, end_time_epoch={end_time} ({WINDOW_S}s window)...")

        # Concurrent creates so we don't burn the budget on serial latency.
        async def create_one(i: int) -> str:
            r = await client.post(
                f"{API}/v1/items",
                json={"title": f"close-{i}", "start_price": 1, "end_time_epoch": end_time},
            )
            r.raise_for_status()
            return r.json()["item_id"]

        ids = await asyncio.gather(*[create_one(i) for i in range(N)])

        print(f"created. sleeping {WINDOW_S}s for closer to drain + Prometheus to scrape...")
        await asyncio.sleep(WINDOW_S)

        # (a) Per-item correctness
        async def check_one(item_id: str) -> bool:
            r = await client.get(f"{API}/v1/items/{item_id}")
            r.raise_for_status()
            return bool(r.json().get("closed_by"))

        results = await asyncio.gather(*[check_one(i) for i in ids])
        closed = sum(1 for ok in results if ok)
        print(f"items with closed_by set: {closed}/{N}")
        if closed != N:
            print(f"FAIL: {N - closed} item(s) missing closed_by", file=sys.stderr)
            return 1

        # (b) No-dupe via Prometheus
        q_sum = "sum(auctions_closed_total)"
        r = await client.get(f"{PROM}/api/v1/query", params={"query": q_sum})
        r.raise_for_status()
        result = r.json().get("data", {}).get("result", [])
        if not result:
            print("FAIL: no auctions_closed_total metric in Prometheus yet", file=sys.stderr)
            return 1
        total = float(result[0]["value"][1])
        print(f"sum(auctions_closed_total) = {total} (expected {N})")
        if int(total) != N:
            print(f"FAIL: counter {total} != {N} — dupes or undercount", file=sys.stderr)
            return 1

        # (c) Distribution log (informational)
        r = await client.get(f"{PROM}/api/v1/query", params={"query": "auctions_closed_total"})
        r.raise_for_status()
        per_replica = r.json().get("data", {}).get("result", [])
        if per_replica:
            print("per-replica close counts:")
            for series in per_replica:
                instance = series["metric"].get("instance", "?")
                val = series["value"][1]
                print(f"  {instance}: {val}")

        print("PASS")
        return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
