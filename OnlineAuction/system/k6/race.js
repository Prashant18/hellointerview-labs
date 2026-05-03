import http from 'k6/http';
import { check } from 'k6';

// Lab 01 — bid race demo. DEMONSTRATES the bug in the lab 00 naive bid
// handler: GetItem → check-in-Python → PutItem(bids) → unconditional
// UpdateItem(items). Multiple concurrent bids of the same amount all
// observe the same `current_high_bid`, all decide "valid", all write —
// last writer wins on `current_high_bidder` but EVERY one already
// returned 201 to its client.
//
// We create ONE item in setup() (runs once before any VU), then 30 VUs
// each fire one bid of the SAME amount against that one item. With the
// naive handler, multiple bids get 201. After lab 02's
// ConditionExpression fix, exactly ONE 201 + 29× 409.

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
    // BUG ASSERTION (flips in lab 02): count >= 2 means multiple "winners"
    // were accepted for the same item — the race fired.
    'http_reqs{status:201}': ['count>=2'],
  },
};

export default function (data) {
  const res = http.post(
    `${BASE}/v1/items/${data.itemId}/bids`,
    JSON.stringify({ bidder: `racer-${__VU}`, amount: 100 }),
    { headers: { 'Content-Type': 'application/json' } },
  );
  check(res, { 'is 201 or 409': (r) => r.status === 201 || r.status === 409 });
}
