# SkillsUpgrade — Staff+ System Design Labs

This repo turns HelloInterview system design problems into **runnable, end-to-end labs**. The bar is staff+: every claim is demonstrated by code you can `docker compose up` and break under load, not by a diagram.

## Repo layout

```
SkillsUpgrade/
├── CLAUDE.md                     # this file — global conventions
├── RateLimiter/                  # one problem = one folder
│   ├── problem.txt               # source HelloInterview material
│   ├── CLAUDE.md                 # phased lab plan for this problem
│   ├── JOURNAL.md                # chronological log: what each lab added/changed
│   ├── shared/observability/     # Prometheus + Grafana provisioning (mounted by compose)
│   └── labs/00-setup/            # the evolving codebase — ONE folder, all labs
│       ├── README.md
│       ├── Makefile
│       ├── docker-compose.yml
│       ├── gateway/              # FastAPI service (grows lab-by-lab)
│       └── ...
├── OnlineAuction/                # next problem, same template
│   └── system/                   # for new problems, start with a clean unnumbered folder
└── ...
```

Every new problem folder contains:

1. `problem.txt` — pasted from hellointerview.com (already in place by the time Claude reads it).
2. `CLAUDE.md` — Claude writes this first. It distills the problem into a phased lab plan. User reviews and approves before any code is written.
3. `JOURNAL.md` — a chronological log. One entry per lab phase: what was added/changed (file paths), the property proved, the staff+ talking points unlocked, what's next.
4. `shared/` — observability provisioning and any other cross-lab artifacts.
5. **One** code folder that **evolves in place** across all lab phases (e.g. `system/` for new problems; for the existing RateLimiter we keep `labs/00-setup/`).

## Lab phase contract — single-folder evolution

Each lab phase is a **diff** to the existing codebase, not a new folder.

When Claude finishes a lab, the contract is:

| Artifact          | Update                                                                                            |
| ----------------- | ------------------------------------------------------------------------------------------------- |
| Source code       | Modified in place. New files added where they belong. No copying of the previous lab's tree.      |
| `README.md`       | Reflects the **current state** of the system, not the lab number. Sections that no longer apply are removed/rewritten. |
| `JOURNAL.md`      | Append a new entry: `## Lab NN — Title (date)` with files-touched / property-proved / talking-points / next. This is the time machine. |
| `verify.sh`       | Extended to assert the new property along with all previous properties. Old assertions still run. |
| `docker-compose.yml` | New services added (Redis, etcd) on top of existing ones. Don't fork it.                       |

If the user wants to time-travel back to "what the system looked like at end of lab 02", recommend `git tag lab-NN` after each lab and `git checkout lab-NN` to revisit. (Most problem folders are not yet `git init`'d — offer to initialize when the first lab is done.)

This convention deliberately **replaces** an earlier draft of this CLAUDE.md that required each lab to be independently runnable in its own folder. That rule made sense for production repos, not for learning labs where every "lab N" arrives from "lab N-1" anyway. The diff is what teaches.

## Tech defaults (override per problem if the problem demands it)

- **Service language**: Python 3.12 by default (FastAPI + asyncio). The problem's `CLAUDE.md` declares the choice and justifies it; only override when the problem genuinely needs another runtime.
- **Python packaging**: Every Python service uses **uv** (`pyproject.toml` + committed `uv.lock` + `.python-version`). No `pip install` or bare `requirements.txt`. Dockerfiles base on `ghcr.io/astral-sh/uv:python3.12-bookworm-slim` and run `uv sync --frozen --no-dev`. For local IDE: user runs `uv sync` in the service folder, the resulting `<service>/.venv` is auto-detected by Pyright/Pylance/pylsp.
- **Containers**: Docker Compose v2. Each lab has its own compose file or extends a shared one in `<problem>/shared/`.
- **Datastores**: real images — `redis:7`, `postgres:16`, `etcd:v3.5`, etc. Never mock the datastore unless the lab is *about* mocking.
- **Load**: [k6](https://k6.io) for HTTP load. `wrk` only when k6 is overkill.
- **Observability**: Prometheus + Grafana stack lives in `<problem>/shared/observability/`, included from any lab that needs metrics. Every service exports `/metrics`.
- **Chaos**: `pumba` or compose `kill`/`pause` for failure injection; `tc` (via a sidecar) for latency injection.

## How Claude works inside this repo

When the user opens a new problem folder with a fresh `problem.txt`:

1. **Read the whole `problem.txt`.** Don't skim. The interviewer's hints in the "what's expected at each level" sections matter.
2. **Write `<problem>/CLAUDE.md`** with the phased lab plan. Show it to the user. Wait for approval before generating code.
3. **Build one lab at a time, in place.** Don't get ahead of the user. Don't create a new folder per lab. After each lab, append the JOURNAL entry and ask: "ready for the next phase, or want to dig deeper here?"
4. **Every lab ends with a "what to say in the interview" section** in the JOURNAL entry — the 3–5 bullet talking points the user should be able to deliver fluently after running the lab. This is the bridge from "I built it" to "I can articulate it."
5. **No premature abstractions.** If something looks reusable across labs, leave it where it is. The codebase is small enough that "extract to a module" can wait until you actually have the second use site.

## Quality bar — staff+ specifics

A lab is **not done** until at least these are true:

- It runs cleanly on `darwin/arm64` (the user's machine) and the verification script passes.
- The README states the **non-functional property** the lab achieves and how it's measured (latency p99, throughput, MTTR after Redis kill, etc.).
- The README contains at least one **trade-off** with the alternative explicitly named and dismissed (e.g. "we picked Lua over WATCH/MULTI because…").
- The README has an **operational concerns** section: what would page on-call, what the runbook step is, what the metric/SLO would be.
- There is a **failure mode demo** wherever it's relevant (kill the master, partition the network, exhaust connection pool).

## Interaction style

- Phases are short — aim for 30–60 minutes per lab including reading time. If a phase is bigger, split it.
- Claude proposes, user disposes. Never blow through 4 phases without checking in.
- When a lab teaches a *pattern* (consistent hashing, leader election, idempotency keys), call it out by name in the README — the user should leave the lab with vocabulary, not just code.
- Diagrams live in `README.md` as ASCII or mermaid. No external tools.

## Response style — HARD anti-bloat rules

The user has flagged response bloat twice. These rules are not aesthetic preferences; they are constraints. Treat every line of chat output as a token budget.

**Default response shape after a green verify:**

```
<one-line headline of result, with the key metric>
<3-5 bullets max: file changes, only if non-obvious from git status>
<the 4 git commands in a fenced block>
Reply Y to push.
```

That's it. Total: ~10 lines.

**Hard rules:**
- **No section headers** in chat for routine work (`# What you do now`, `## Drive loop`, `## Hints`, `## Three staff+ talking points`, `## Files changed`). Use bullets or skip entirely. Headers belong in JOURNAL/RECAP files, not in chat.
- **No talking-points blocks in chat.** Talking points live in JOURNAL only — writing them twice (chat + file) is pure duplication. Reference: "talking points are in the JOURNAL entry."
- **No pre-narration of tool calls.** Don't say "Implementing X now." or "Reading current state." or "Re-running quickly to grab the metric." The tool result speaks for itself.
- **No restating metrics the user just saw.** k6 output already scrolled across their terminal. A one-line summary ("p95 = 2.4ms, hit 99.8%") is fine; a 6-line table is not.
- **No unsolicited follow-up questions.** "Want me to also write X?" / "Want me to also add hints?" — only ask if the user is mid-lab and the answer changes what you do next. Per-problem completion follow-ups (RECAP, schedule, etc.) get ONE one-line offer, not a paragraph.
- **No previewing the next lab in the commit-proposal message.** Push current → wait for Y → push lands → THEN talk about next lab. One beat at a time.
- **Code skeletons carry their own pseudocode in docstrings.** Don't repeat the algorithm in chat after writing it in the docstring. The user reads the docstring; if they need a hint they'll ask.

**JOURNAL.md per-lab entry — HARD MAX 8 lines.** This rule already exists in problem CLAUDE.md files but has been violated routinely (lab 04 entry was 4 lines of bullets each containing 4-sentence run-on; lab 05 was 6 such bullets). The bullet count is not the limit; the **line count is**. If you can't fit it, you're padding. Compress until you can.

**Per-problem RECAP.md** — only on user request. Even when requested, keep it to 8-10 Q&A pairs, each Q&A ≤ 6 lines. No section preamble.

**Per-problem README** — current state + run command. ~30 lines. No "history" section, no "future work" section.

**Code comments** — only at WHY, never at WHAT. The agreed convention is "if removing the comment wouldn't confuse a reader, don't write it." This applies to docstrings too — a function called `try_claim` does not need a docstring explaining that it tries to claim something. Docstrings are reserved for non-obvious algorithm pseudocode (e.g., the user-implementation skeletons in TDD labs) or non-obvious invariants.

**When in doubt, output less.** A user can always ask for more detail; they cannot un-read a wall of text.

## What "done" looks like for a problem

The problem folder is complete when:

1. All planned labs run cleanly.
2. There's a top-level `<problem>/RECAP.md` listing the patterns covered, the talking points, and the questions the user should be able to answer cold (e.g. "Why Lua and not Redis transactions?", "What happens to in-flight requests during a Redis failover?").
3. The user has run a final integration scenario combining multiple labs (e.g. "load test the sharded + HA + dynamic-config version end to end").

## Git workflow

This repo lives at `https://github.com/Prashant18/hellointerview-labs` (public, MIT). Every completed lab gets its own commit + annotated tag.

**Per-lab loop, after `make verify` goes green:**

```text
[user] make verify  → all green
[claude] proposes:
   git add -A
   git commit -m "<Problem>: lab NN — <one-line summary>"
   git tag -a <Problem>/lab-NN -m "<Problem> lab NN done"
   git push --follow-tags
[user] approves Y/N
[claude] runs the commands; appends JOURNAL.md entry
```

Conventions:
- **Active gh account**: `Prashant18`. If `gh auth status` ever shows a different active account, switch with `gh auth switch --user Prashant18` before any push.
- **Tag namespace**: annotated tags formatted `<Problem>/lab-NN` (e.g. `RateLimiter/lab-03`). Annotated, not lightweight, so they carry author + date + message.
- **Commit format**: `<Problem>: lab NN — <one-line summary>`. Example: `RateLimiter: lab 03 — Redis-backed Token Bucket; TOCTOU race exposed by race.js`.
- **Push command**: `git push --follow-tags` (single push covers the new commit and any new tags reachable from it).
- **Confirmation rule**: pushing is a visible-to-others action. Claude proposes the exact commands and waits for user Y/N before executing. Local-only commits (no push) can be made more freely if the user has authorized that scope earlier in the conversation.
- **Time-travel**: `git checkout RateLimiter/lab-02` (or any tag) returns the working tree to that lab's state. The `JOURNAL.md` entry for that lab tells you what was new there.

## Memory

User profile and preferences live in `~/.claude/projects/-Users-prashant-Work-SkillsUpgrade/memory/`. Update them when the user reveals a new preference about lab style, language choice, or interview targets.
