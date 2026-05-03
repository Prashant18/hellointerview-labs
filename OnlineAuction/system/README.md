# OnlineAuction — system

DynamoDB-native auction system, Docker-only.

```bash
make verify     # build + smoke + assertions
make up         # bring up the stack
make down       # tear down
make logs       # follow api logs
make ddb-shell  # list DDB tables
make sync       # populate api/.venv for IDE
```

URLs after `make up` (override defaults via env: `API_PORT=8001 make up`):

| Service       | URL                              |
| ------------- | -------------------------------- |
| API (Caddy)   | http://localhost:18001           |
| DDB Local     | http://localhost:18002           |
| Prometheus    | http://localhost:19091           |
| Grafana       | http://localhost:13001           |

Endpoints:

```
POST /v1/items                       # create item
GET  /v1/items/{id}                  # read item state
POST /v1/items/{id}/bids             # place bid (NAIVE — see ../JOURNAL.md lab 01)
GET  /health                         # liveness
GET  /metrics                        # Prometheus
```

Lab status: see [`../JOURNAL.md`](../JOURNAL.md). Plan: [`../CLAUDE.md`](../CLAUDE.md).
