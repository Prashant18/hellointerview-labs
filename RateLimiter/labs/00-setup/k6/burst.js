import http from 'k6/http';
import { check } from 'k6';

// Burst test — REGRESSION ASSERTION.
//
// As of lab 03, state lives in Redis (centralized) and the per-replica
// leak from lab 02 is fixed. Even with 3 gateway replicas behind a
// round-robin LB, a single client firing 50 sequential requests gets
// EXACTLY `capacity` allowed and the rest denied. If this ever fails
// again, something has slipped back into per-replica state.
//
// Note this test runs SEQUENTIALLY (1 VU, 50 iterations one at a time),
// so it doesn't expose the TOCTOU race between HMGET and HSET. That's
// what k6/race.js is for.

export const options = {
  scenarios: {
    burst: {
      executor: 'per-vu-iterations',
      vus: 1,
      iterations: 50,
      maxDuration: '5s',
    },
  },
  thresholds: {
    'http_reqs{status:200}': ['count==10'],
    'http_reqs{status:429}': ['count==40'],
  },
};

const BASE = __ENV.GATEWAY_URL || 'http://caddy:8000';
const CLIENT_ID = `burst-vu-${__VU}-${Date.now()}`;

export default function () {
  const res = http.get(`${BASE}/v1/check`, {
    headers: { 'x-client-id': CLIENT_ID },
  });

  if (res.status === 200) {
    check(res, {
      '200 has X-RateLimit-Limit': (r) => r.headers['X-Ratelimit-Limit'] !== undefined,
      '200 has X-RateLimit-Remaining': (r) => r.headers['X-Ratelimit-Remaining'] !== undefined,
      '200 has X-RateLimit-Reset': (r) => r.headers['X-Ratelimit-Reset'] !== undefined,
    });
  } else if (res.status === 429) {
    check(res, {
      '429 has Retry-After': (r) => r.headers['Retry-After'] !== undefined,
      '429 has X-RateLimit-Remaining=0': (r) => r.headers['X-Ratelimit-Remaining'] === '0',
    });
  } else {
    check(res, { 'unexpected status': () => false });
  }
}
