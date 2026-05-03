import http from 'k6/http';
import { check } from 'k6';

// Race test — REGRESSION ASSERTION (as of lab 04).
//
// 20 VUs hammer ONE client_id concurrently. Each gateway replica calls the
// Lua script which performs the entire read-modify-write atomically inside
// Redis. With capacity=10, EXACTLY 10 requests are allowed and 90 denied
// regardless of the concurrency level. If this ever overshoots, atomicity
// has slipped — most likely because the implementation moved any decision
// logic out of the Lua script back into Python.
//
// Lab history of this test:
//   - Lab 03 (HMGET/HSET): asserted overshoot (count >= 11) to prove the
//     TOCTOU race window between HMGET and HSET. Got ~50 allowed.
//   - Lab 04 (Lua atomic): flipped to count == 10. Same chaos load, fixed
//     implementation. To re-observe the race for comparison, run with:
//
//        BUCKET_BACKEND=redis docker compose up -d gateway
//        make race
//
//     The lab 03 racy implementation is preserved at app/redis_tokenbucket.py.

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
    // Atomicity means EXACTLY capacity. No more, no fewer.
    'http_reqs{status:200}': ['count==10'],
    'http_reqs{status:429}': ['count==90'],
  },
};

const BASE = __ENV.GATEWAY_URL || 'http://caddy:8000';

// k6 runs the init context (top of file) ONCE PER VU. So a top-level
// `Date.now()` would give each of our 20 VUs a slightly different
// value — which would split the race across N different Redis buckets
// and silently give N×capacity allowed. setup() runs ONCE before any
// VU starts and its return value is shared into every default() call,
// which is what we actually want for a "20 VUs vs ONE client" test.
export function setup() {
  return { clientId: `race-${Date.now()}` };
}

export default function (data) {
  const res = http.get(`${BASE}/v1/check`, {
    headers: { 'x-client-id': data.clientId },
  });
  check(res, {
    'is 200 or 429': (r) => r.status === 200 || r.status === 429,
  });
}
