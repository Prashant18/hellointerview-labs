import http from "k6/http";
import { check } from "k6";

// Smoke test: 100 RPS for 5s. Asserts the gateway returns 200 and stays
// under a generous p99 latency budget.
//
// This test is for HARNESS LIVENESS — not for exercising the rate limiter.
// We give every iteration a unique client_id (`smoke-${__VU}-${__ITER}`)
// so each request hits a fresh, full bucket and is always allowed. If you
// want to see the rate limiter actually fire, run `make burst` instead.

export const options = {
  scenarios: {
    smoke: {
      executor: "constant-arrival-rate",
      rate: 100,
      timeUnit: "1s",
      duration: "5s",
      preAllocatedVUs: 20,
      maxVUs: 50,
    },
  },
  thresholds: {
    http_req_failed: ["rate<0.01"],
    http_req_duration: ["p(99)<200"],
  },
};

const BASE = __ENV.GATEWAY_URL || "http://gateway:8000";

export default function () {
  // Unique per (VU, iteration) so we never hit a rate limit. Smoke =
  // "harness is alive", not "limits are enforced."
  const res = http.get(`${BASE}/v1/check`, {
    headers: { "x-client-id": `smoke-${__VU}-${__ITER}` },
  });
  check(res, {
    "status is 200": (r) => r.status === 200,
    "allowed is true": (r) => r.json("allowed") === true,
  });
}
