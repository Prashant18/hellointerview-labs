import http from 'k6/http';
import { check, sleep } from 'k6';

// Lab 04 — read-heavy load against /v1/items/{id} with cache.
//
// 20 reader VUs hammer GET; 1 bidder VU posts a winning bid every 500ms
// (each invalidates the cache). Asserts:
//   - http_req_duration{name:get_item} p95 < 5ms (host overhead ~2ms baseline)
//   - cache_hit_ratio > 0.95 (sampled in teardown via /metrics)
//
// Per-replica counters approximate the global ratio because Caddy LB is
// uniform AND every replica reads the same shared Redis cache state.

const BASE = __ENV.API_URL || 'http://caddy:8000';
const DURATION = '5s';

export function setup() {
  const r = http.post(
    `${BASE}/v1/items`,
    JSON.stringify({ title: 'cache-target', start_price: 10, end_time_epoch: 9999999999 }),
    { headers: { 'Content-Type': 'application/json' } },
  );
  if (r.status !== 201) throw new Error(`setup failed: ${r.status}`);
  // First read so we have a non-zero baseline by the time readers arrive.
  http.get(`${BASE}/v1/items/${r.json('item_id')}`);
  return { itemId: r.json('item_id') };
}

export const options = {
  scenarios: {
    readers: {
      executor: 'constant-vus',
      vus: 20,
      duration: DURATION,
      exec: 'reader',
    },
    bidder: {
      executor: 'constant-vus',
      vus: 1,
      duration: DURATION,
      exec: 'bidder',
    },
  },
  thresholds: {
    'http_req_duration{name:get_item}': ['p(95)<5'],
    'http_reqs{name:get_item}': ['count>1000'],
  },
};

export function reader(data) {
  http.get(`${BASE}/v1/items/${data.itemId}`, { tags: { name: 'get_item' } });
}

export function bidder(data) {
  // VU-local counter so we never duplicate amounts within this run.
  if (typeof bidder.n === 'undefined') bidder.n = 0;
  bidder.n += 1;
  http.post(
    `${BASE}/v1/items/${data.itemId}/bids`,
    JSON.stringify({ bidder: `cache-bidder`, amount: 100 + bidder.n * 10 }),
    { headers: { 'Content-Type': 'application/json' }, tags: { name: 'place_bid' } },
  );
  sleep(0.5);
}

export function teardown() {
  // Scrape /metrics via Caddy. Per-replica counters; ratio is uniform across
  // replicas because Redis is shared.
  const res = http.get(`${BASE}/metrics`);
  const text = res.body || '';
  const hits = parseFloat((text.match(/^cache_hits_total(?:\{[^}]*\})?\s+(\S+)/m) || [])[1] || '0');
  const misses = parseFloat((text.match(/^cache_misses_total(?:\{[^}]*\})?\s+(\S+)/m) || [])[1] || '0');
  const total = hits + misses;
  const ratio = total > 0 ? hits / total : 0;
  console.log(`cache: hits=${hits} misses=${misses} ratio=${ratio.toFixed(4)} (sample from one replica)`);
  if (ratio < 0.95) {
    throw new Error(`cache hit ratio ${ratio.toFixed(4)} < 0.95`);
  }
}
