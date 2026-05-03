# hellointerview-labs

Hands-on staff+ system design labs from [HelloInterview](https://hellointerview.com) problems — runnable end-to-end with real dependencies (Docker for Redis/etcd, real load tests with k6, real metrics in Prometheus + Grafana). No whiteboard-only diagrams. No pseudocode. If a lab claims a property, there's a `verify.sh` that proves it.

## Why this exists

Standard system-design prep is read-the-problem, draw-the-boxes. That's enough to pass a mid-level interview, not a staff+ one. Staff+ interviewers probe the parts you only know if you've actually built it: the TOCTOU race in your distributed counter, the failover gap when a Redis master dies, the cardinality decision that nuked your Prometheus instance last quarter, the bucket boundary math that made your sliding window wrong.

These labs build each system phase by phase, intentionally introducing the bugs the source material hand-waves over, then fixing them — so by the time you talk about the system in an interview, you've felt every trade-off it embodies.

## Status

| Problem        | Folder                     | Status                                                       |
| -------------- | -------------------------- | ------------------------------------------------------------ |
| Rate Limiter   | [`RateLimiter/`](RateLimiter/) | **✅ Complete (5/10 labs)** — labs 00–05 + lab 06 fail-closed wiring as bonus. See [`RateLimiter/RECAP.md`](RateLimiter/RECAP.md) for the cheat-sheet. |
| Online Auction | [`OnlineAuction/`](OnlineAuction/) | **✅ Complete (5/5 labs)** — DynamoDB Local + conditional writes for bid race + Redis SSE fanout + read-through cache + sparse-GSI race-claim for exactly-once auction close. See [`OnlineAuction/JOURNAL.md`](OnlineAuction/JOURNAL.md). |
| Robinhood     | [`Robinhood/`](Robinhood/) | Lab 00 done — broker shell (FastAPI api×3) + separate `mock-exchange` Docker service + DDB orders table with 3 GSIs ready for labs 1-3. Happy-path POST /v1/orders with idempotent client_order_id replay. Labs 1 (live price SSE fanout) → 2 (trade-feed → orders update via GSI) → 3 (consistency clean-up) up next. |

Each problem folder has its own `JOURNAL.md` chronicling what every lab phase added and the staff+ talking points it unlocks. That's the time-machine: read it top-to-bottom and you walk through the full design journey.

## Per-problem layout

```
<Problem>/
├── CLAUDE.md          # phased lab plan + tech stack rationale
├── JOURNAL.md         # chronological log: what each lab added/changed + interview takeaways
├── problem.txt        # source HelloInterview material (verbatim)
├── shared/            # cross-lab artifacts (Prometheus + Grafana provisioning, etc.)
└── labs/00-setup/     # ONE codebase that evolves in place across all labs
    ├── README.md      # current state of the system (updated each lab)
    ├── Makefile
    ├── docker-compose.yml
    ├── verify.sh      # asserts every property the labs have introduced so far
    ├── gateway/       # the Python service (FastAPI + uv)
    └── k6/            # load tests
```

The folder is named `labs/00-setup/` for historical reasons — it's actually the only code folder, evolving in place across every lab. New problems start with a cleaner name (`system/`).

## How to run a lab

```bash
git clone https://github.com/Prashant18/hellointerview-labs.git
cd hellointerview-labs/RateLimiter/labs/00-setup
make verify      # builds the stack, runs unit tests + k6 scenarios, asserts every property
```

You'll need: `docker`, `uv`, `make`, `gh` (only if you want to push from this repo). The Dockerfile installs everything else.

## Tech stack

- **Service**: Python 3.12 + FastAPI + `uvicorn[standard]` + `redis-py` 5.x
- **Packaging**: [uv](https://github.com/astral-sh/uv) (pinned via `uv.lock` in each service folder)
- **Containers**: Docker Compose v2; no host-installed Redis/etcd/etc.
- **Observability**: Prometheus + Grafana with provisioning configs in `<Problem>/shared/observability/`
- **Load**: [k6](https://k6.io) as a Docker service (`profiles: ["test"]`)
- **Chaos**: `pumba`, `tc`, `docker pause`/`kill` (introduced when relevant)

## Conventions

- **One folder per problem**, evolving in place across labs. Don't duplicate scaffolding into `labs/01-*`, `labs/02-*` — the diff between commits *is* the lab.
- **Tag namespace**: annotated tags `<Problem>/lab-NN` mark each completed lab so you can `git checkout RateLimiter/lab-02` to revisit a state.
- **Commit message format**: `<Problem>: lab NN — <one-line summary>` (e.g. `RateLimiter: lab 03 — Redis-backed Token Bucket; TOCTOU race exposed`).
- **TDD by default**: for code-bearing labs, the user implements; tests + skeleton come first.
- **CLAUDE.md** at root + per-problem documents the working agreement with Claude Code (the LLM partner this repo is built with).

## License

MIT — see [LICENSE](LICENSE). Use anything here freely with attribution.

## Author

[**Prashant18**](https://github.com/Prashant18) — built with Claude Code as a tutoring partner. Each lab ends with a "what to say in an interview" section in `JOURNAL.md` that converts the build into talking points.
