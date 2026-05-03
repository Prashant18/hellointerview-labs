import http from 'k6/http';
import { check } from 'k6';

// Lab 0 smoke: place limit order with unique Idempotency-Key per VU iteration,
// expect 201/SUBMITTED with external_order_id. Verifies the broker → mock-exchange
// happy-path round-trip via Caddy LB.

const BASE = __ENV.API_URL || 'http://caddy:8000';

export const options = {
  scenarios: {
    smoke: {
      executor: 'per-vu-iterations',
      vus: 1,
      iterations: 5,
      maxDuration: '10s',
    },
  },
  thresholds: {
    http_req_failed: ['rate<0.01'],
    http_req_duration: ['p(99)<500'],
  },
};

export default function () {
  const idempo = `smoke-${__VU}-${__ITER}-${Date.now()}`;
  const r = http.post(
    `${BASE}/v1/orders`,
    JSON.stringify({
      position: 'buy',
      symbol: 'AAPL',
      price_cents: 15000,
      num_shares: 10,
      order_type: 'limit',
    }),
    { headers: { 'Content-Type': 'application/json', 'Idempotency-Key': idempo } },
  );
  check(r, {
    'place 201': (res) => res.status === 201,
    'status SUBMITTED': (res) => res.json('status') === 'SUBMITTED',
    'external_order_id present': (res) => !!res.json('external_order_id'),
  });
}
