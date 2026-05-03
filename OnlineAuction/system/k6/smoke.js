import http from 'k6/http';
import { check } from 'k6';

// Smoke: create an item, place a bid, get the item back. Done as a single
// VU sequence (no concurrency yet — race demo arrives in lab 01).

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
  // 1) create
  const create = http.post(
    `${BASE}/v1/items`,
    JSON.stringify({ title: `widget-${__ITER}`, start_price: 10, end_time_epoch: 9999999999 }),
    { headers: { 'Content-Type': 'application/json' } },
  );
  check(create, { 'create 201': (r) => r.status === 201 });
  const item_id = create.json('item_id');

  // 2) bid
  const bid = http.post(
    `${BASE}/v1/items/${item_id}/bids`,
    JSON.stringify({ bidder: 'alice', amount: 100 }),
    { headers: { 'Content-Type': 'application/json' } },
  );
  check(bid, { 'bid 201': (r) => r.status === 201 });

  // 3) read back
  const get = http.get(`${BASE}/v1/items/${item_id}`);
  check(get, {
    'get 200': (r) => r.status === 200,
    'high bid is 100': (r) => r.json('current_high_bid') === 100,
    'high bidder is alice': (r) => r.json('current_high_bidder') === 'alice',
  });
}
