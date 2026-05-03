# OnlineAuction — lab plan

DynamoDB-native auction system, all-in-Docker, 6 labs. Companion plan: `~/.claude/plans/toasty-petting-scott.md`.

## Phases

| # | Title | What it adds | Property |
|---|---|---|---|
| 0 | Setup | api×3 + dynamodb-local + caddy + obs; naive bid endpoint | smoke green |
| 1 | Bid race demo | k6 chaos: N concurrent bids of equal amount | invariant broken (multi-winner) |
| 2 | Conditional-write fix | `UpdateItem ConditionExpression` | exactly one 201, rest 409 |
| 3 | SSE fanout | Redis pub/sub + `GET /items/{id}/stream` | watcher gets event <200ms |
| 4 | Hot-read cache | Read-through Redis cache, write invalidates | p95 read <1ms; hit ratio >90% |
| 5 | Exactly-once close | Replicas race-claim via ConditionExpression on `closed_by` | N replicas, M ending → exactly M closes |

## Stack

Python 3.12 + FastAPI + uv + aioboto3 + DynamoDB Local + Redis (lab 3+) + Caddy + Prom + Grafana + k6. All Docker. Ports 18001/18002/16380/19091/13001.

## Conventions

Single-folder evolution (`system/`). TDD: business logic = user implements; infra = Claude writes. Per-lab: green verify → commit → tag `OnlineAuction/lab-NN` → push (user Y/N). JOURNAL ≤8 lines/lab. No RECAP unless asked.
