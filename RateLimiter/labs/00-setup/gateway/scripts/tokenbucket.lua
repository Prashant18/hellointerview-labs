-- Lab 04 — Atomic Token Bucket in Lua.
--
-- This script executes INSIDE Redis as a single atomic unit. No other
-- Redis client can interleave commands between any two redis.call() lines
-- in here. That atomicity is what closes the TOCTOU race window from lab 03:
-- the read, the compute, and the write all happen with no other client
-- able to observe an intermediate state.
--
-- INPUTS (from lua_tokenbucket.py):
--   KEYS[1]  = the bucket key, e.g. "bucket:<client_id>"
--   ARGV[1]  = capacity        (max tokens, also the burst size)
--   ARGV[2]  = refill_per_sec  (sustained rate)
--   ARGV[3]  = ttl_seconds     (idle eviction window)
--
-- OUTPUT (a 3-element Lua table → Python list):
--   {allowed, remaining, reset_after_str}
--     allowed         : 1 if allowed, 0 if denied
--     remaining       : floor(tokens) AFTER the decision (integer)
--     reset_after_str : seconds-as-STRING. Redis truncates Lua numbers
--                       to integers on return, so floats need tostring().
--
-- ALGORITHM SHAPE (mirrors lab 03's RedisTokenBucket.allow, minus round trips):
--   1) HMGET tokens + last_refill (Lua array; missing fields come back as `false`)
--   2) Compute "now" from redis.call('TIME') — a single source of truth that
--      lives next to the state, no NTP dependency, no per-replica clock drift.
--   3) Lazy-init for new clients: tokens = capacity, last_refill = now.
--   4) Apply refill, capped at capacity.
--   5) Decide. DON'T push tokens negative on deny — same lesson as lab 01.
--   6) HSET both fields back, EXPIRE for idle eviction.
--   7) Return {allowed, floor(tokens), tostring(reset_after)}.
--
-- LUA GOTCHAS (where I always slip):
--   - HMGET returns Lua `false` for missing fields, not nil. Use `tonumber(x)`
--     which returns nil for both — then `if not <var> then ...`.
--   - redis.call('TIME') returns {seconds_str, microseconds_str} — both STRINGS.
--     Combine: `now = tonumber(t[1]) + tonumber(t[2]) / 1000000`.
--   - tonumber(ARGV[i]) — script args arrive as strings even if Python passed numbers.
--   - Lua has math.max, math.min, math.floor (built-in, no `import`).
--   - Returning a float as a Lua number truncates to int. Always tostring() floats.
--
-- WHEN YOU RUN `make verify`:
--   - burst.js must STILL give 10/40 (regression: limit enforced sequentially).
--   - race.js MUST give EXACTLY 10 allowed across 100 concurrent requests
--     (the new property: race fixed by atomicity).
--   - If race overshoots, your read-modify-write isn't actually atomic —
--     check that EVERY read and write goes through redis.call() (not back
--     to a Python helper) and that you didn't leak the decision logic out
--     of the script.

-- TODO(you): implement the algorithm above. Replace this `error_reply` with
--            real logic that computes the decision and returns the 3-element
--            table described in the OUTPUT section.

local key = KEYS[1]
local capacity = tonumber(ARGV[1])
local refill_per_sec = tonumber(ARGV[2])
local ttl_seconds = tonumber(ARGV[3])

local state = redis.call('HMGET', key, 'tokens', 'last_refill')
local tokens = tonumber(state[1])
local last_refill = tonumber(state[2])

local time_arr = redis.call('TIME')
local now = tonumber(time_arr[1]) + tonumber(time_arr[2]) / 1000000


if not tokens or not last_refill then
    tokens = capacity
    last_refill = now
end

local elapsed = math.max(0, now - last_refill)
tokens = math.min(capacity, tokens + elapsed * refill_per_sec)

local allowed
local reset_after

if tokens >= 1 then
    tokens = tokens - 1
    allowed = true
    if refill_per_sec > 0 then
        reset_after = (capacity - tokens) / refill_per_sec
    else
        reset_after = 0
    end
else
    allowed = 0
    if refill_per_sec > 0 then
        reset_after = (1 - capacity) / refill_per_sec
    else
        reset_after = 0
    end
end

redis.call("HSET", key, "tokens", tokens, "last_refill", now)
redis.call("EXPIRE", key, ttl_seconds)

return { allowed, math.floor(tokens), math.floor(reset_after) }
