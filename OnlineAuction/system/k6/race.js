import http from 'k6/http';
import { check } from 'k6';

// Lab 02 — same chaos test, INVERTED assertion. After collapsing
// place_bid() into a single conditional UpdateItem, exactly ONE of the
// 30 concurrent equal-amount bids wins (201); the other 29 get
// ConditionalCheckFailedException → 409. The race is now decided
// server-side under the partition's serialized ordering.

const BASE = __ENV.API_URL || 'http://caddy:8000';

export function setup() {
  const res = http.post(
    `${BASE}/v1/items`,
    JSON.stringify({ title: 'race-target', start_price: 10, end_time_epoch: 9999999999 }),
    { headers: { 'Content-Type': 'application/json' } },
  );
  if (res.status !== 201) throw new Error(`setup failed: ${res.status} ${res.body}`);
  return { itemId: res.json('item_id') };
}

export const options = {
  scenarios: {
    race: {
      executor: 'shared-iterations',
      vus: 30,
      iterations: 30,
      maxDuration: '10s',
    },
  },
  thresholds: {
    // Invariant scoped to the bid endpoint only (setup's item-create is a 201 too).
    'http_reqs{endpoint:bid,status:201}': ['count==1'],
    'http_reqs{endpoint:bid,status:409}': ['count==29'],
  },
};

export default function (data) {
  const res = http.post(
    `${BASE}/v1/items/${data.itemId}/bids`,
    JSON.stringify({ bidder: `racer-${__VU}`, amount: 100 }),
    { headers: { 'Content-Type': 'application/json' }, tags: { endpoint: 'bid' } },
  );
  check(res, { 'is 201 or 409': (r) => r.status === 201 || r.status === 409 });
}
