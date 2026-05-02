import http from 'k6/http';
import { check } from 'k6';

// Race test (lab 03) — DEMONSTRATES the TOCTOU bug between HMGET and HSET.
//
// 20 VUs hammer ONE client_id concurrently. Each gateway replica reads
// (HMGET) the bucket state, computes a decision, then writes (HSET). Two
// requests landing close in time can BOTH see the same `tokens` value,
// BOTH decide "allow", BOTH HSET — last write wins on the count, but two
// 200s already left the building.
//
// With capacity=10 we expect a CORRECT distributed implementation to allow
// exactly 10 (the limit) and deny the rest. The naive HMGET/HSET impl
// overshoots: typically 11–25 allowed depending on how many concurrent
// reads land before any write.
//
// Asserted threshold: `count > 10` for status:200. Anything > capacity is
// proof of the race. After lab 04 (Lua atomic), this same test must give
// exactly 10 again — the test stays, the implementation gets fixed.

export const options = {
  scenarios: {
    race: {
      executor: 'shared-iterations',
      vus: 20,           // 20 in-flight requests at once
      iterations: 100,   // 100 total
      maxDuration: '10s',
    },
  },
  thresholds: {
    // Anything > capacity proves overshoot. We don't bound the upper end:
    // empirically, the race can let 70+ of 100 through depending on how
    // much HMGET pipelining lands before the first HSET completes.
    'http_reqs{status:200}': ['count>=11'],
  },
};

const BASE = __ENV.GATEWAY_URL || 'http://caddy:8000';
// Single shared client_id so all VUs hit the same Redis key.
const CLIENT_ID = `race-${Date.now()}`;

export default function () {
  const res = http.get(`${BASE}/v1/check`, {
    headers: { 'x-client-id': CLIENT_ID },
  });
  check(res, {
    'is 200 or 429': (r) => r.status === 200 || r.status === 429,
  });
}
