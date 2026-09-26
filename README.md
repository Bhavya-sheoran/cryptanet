# CRYPTANET — SIH26183 Real-Time Identification of Fraud-Linked Cryptocurrency Exchanges

Prototype for Smart India Hackathon 2026, Problem Statement **SIH26183**
(Ministry of Home Affairs · Blockchain & Cybersecurity).

A victim reports a suspect wallet address. The system traces the flow of funds
across hops, attributes the terminal address cluster to an exchange/VASP, scores
that exchange's fraud linkage against prior reported cases, and hands an
investigator an explainable, actionable result.

---

## Honest scoping — read this first

**On the problem statement.** The official SIH portal has not published an
expanded background or expected-outcome annexure for SIH26183 (unlike its
sibling SIH26184). The architecture here is a considered engineering
interpretation of the problem statement *title*, not a paraphrase of an official
brief.

**On the data.** This system contains **no real NCRP complaint data and no real
exchange KYC data**, and no such access is claimed anywhere in this repository.
Everything it operates on is one of:

- **Synthetic** — victim complaints and the fraud-ring transaction graph are
  produced by `scripts/generate_synthetic_complaints.py`, a permanent part of
  this codebase.
- **Public datasets** — the Elliptic Bitcoin Dataset (classifier training and
  validation), Etherscan Label Cloud, GraphSense TagPacks, and the OFAC SDN
  crypto address list (VASP tagging seed data).
- **Public blockchain indexer APIs** — Blockstream Esplora, Etherscan, TronGrid
  — when `DEMO_MODE=false`. Bitcoin needs no key at all.
- **Public third-party intelligence** — Binance Proof of Reserves and WazirX
  (keyless), Arkham and Chainabuse (each only with a key). A source that was not
  consulted is reported as *not checked*, never as a clean result.

The backend reports its own provenance at `/api/v1/health/ready`, and the UI
banner is rendered from that field rather than from hardcoded copy, so the claim
on screen cannot drift from how the system is actually configured.

**On enforcement actions.** The system **recommends**; an authorised officer
**approves**. Freeze requests and disclosure/STR drafts are created in a `draft`
state and cannot reach `approved` without an explicit click by a user holding the
`supervisor` role. Nothing auto-fires. This is enforced at three layers: a
database `CHECK` constraint, a service-layer role check, and a test.

---

## Status

| Phase | Scope | State |
|---|---|---|
| **0** | Repo scaffold, docker-compose, Postgres + Neo4j schema | **complete** |
| **1** | Wallet intake, chain detection, connectors, graph writer, clustering | **complete** |
| **2** | VASP attribution, fraud-risk scoring, `/api/wallet` | **complete** |
| **3** | React (JSX) investigator dashboard, Sankey trace view | **complete** |
| **4** | Case management, hashed PDF export, mock NCRP, STR drafts, freeze workflow, alerts | **complete** |
| **5** | End-to-end integration test, RBAC/security pass | **complete** |
| **6** | Live-chain operation, trace budgets, external intelligence, LAN deployment | **complete** |

Phase 6 is the one that is not in the original plan. It exists because
everything above it was built and tested against the synthetic fraud ring, and
pointing the same code at real chains surfaced a different class of problem —
rate limits reported as success, traces that ran for six minutes, a query that
took 101 seconds. See *[Running on live chain data](#running-on-live-chain-data)*.

---

## Architecture

```
victim report ──▶ FastAPI intake ──▶ chain detect ──▶ blockchain connectors
                       │                                (Esplora/Etherscan/TronGrid
                       │                                 or synthetic in DEMO_MODE)
                       ▼
                 Neo4j graph writer ──▶ clustering (common-input-ownership,
                       │                            change-address) via GDS
                       ▼
                 multi-hop trace (default depth 6) ──▶ terminal cluster
                       │
                       ▼
                 VASP attribution ──▶ tagged-address DB (public sources)
                       │              ├─ then: Arkham entity labels (if keyed)
                       │              └─ fallback: behavioural classifier
                       ▼
                 fraud-linkage score (time-decayed, per contributing case)
                       │
          ┌────────────┴────────────┐
          ▼                         ▼
   Redis Stream ──▶ WebSocket   PostgreSQL (cases, scores, evidence, audit)
          │                         │
          ▼                         ▼
   live alert on dashboard    React (JSX) investigator dashboard
```

| Layer | Choice |
|---|---|
| Backend | Python 3.11 · FastAPI |
| Chain data | Blockstream Esplora (BTC, keyless) · Etherscan v2 (ETH) · TronGrid (TRON) |
| Valuation | WazirX (INR-direct) → CoinGecko fallback |
| External intel | Arkham · Chainabuse · Binance Proof of Reserves (all optional, key-gated) |
| Graph | Neo4j 5.26 Community + Graph Data Science |
| Relational | PostgreSQL 16 |
| Events | Redis Streams → WebSocket |
| ML | scikit-learn / XGBoost, trained on the public Elliptic dataset (a PyTorch Geometric GNN remains a stretch goal only) |
| Frontend | React 18 · **plain JavaScript + JSX only** · Vite 6 · D3 / d3-sankey · Vitest |
| Auth | Simplified JWT with role claims |
| Infra | Docker Compose · Caddy (TLS, LAN access) |

Architecture and design rationale: **[docs/architecture.md](docs/architecture.md)**.
Live demo click-path: **[docs/demo-script.md](docs/demo-script.md)**.
Full data model: **[docs/schema.md](docs/schema.md)**.
API contracts: **[docs/api.md](docs/api.md)**.

### What Phase 1 actually does

1. **Intake** validates the address at *checksum* level, not with a regex —
   Base58Check for BTC/TRON, bech32/bech32m polymod for segwit, EIP-55 for
   Ethereum. A mixed-case ETH address that fails EIP-55 is rejected, because
   that is exactly the transcription error the checksum exists to catch.
2. **Dedupe** resolves the address to a single global wallet row. One wallet
   named by several victims yields several cases — that fan-out *is* the
   fraud-ring signal, surfaced at `/api/v1/wallets/multi-reported`.
3. **Connectors** fetch transactions. `DEMO_MODE=true` serves the synthetic
   fraud ring; otherwise **Blockstream Esplora** (BTC — keyless, which is why it
   is the default rather than Blockchair), **Etherscan v2** (ETH — native,
   ERC-20 and internal transfers merged, because a trail that moves through a
   contract disappears from `txlist` alone) and **TronGrid** (TRON — TRC-20 and
   native TRX merged). If a live connector is requested but unconfigured it
   falls back to synthetic **and reports `synthetic` as the source** rather than
   misrepresenting provenance.
4. **Tracing** walks breadth-first in the direction the money moved, to a
   configurable depth (default 6).
5. **Graph writer** MERGEs both the full UTXO shape
   (`:Address`→`:Transaction`→`:Address`) and a denormalised `:TRANSFERRED`
   edge for cheap multi-hop traversal. Idempotent.
6. **Clustering** applies common-input-ownership and a deliberately
   conservative change-address heuristic, both emitting `:SAME_OWNER` edges,
   then resolves them with a GDS Weakly Connected Components pass.

### What Phase 2 actually does

7. **VASP attribution**, three tiers, and the tier used is always reported:
   `tagged_db` (a curated tag from a public source, citing that source),
   `arkham` (Arkham Intelligence's entity label, only when a key is configured)
   or `classifier` (nothing matched; a category predicted from behaviour). The
   order is deliberate — a published, citable tag outranks a commercial label,
   which outranks a model's guess. The behavioural tier **never invents a
   company name**: it returns a category and a confidence, because behaviour
   alone cannot identify a company. `none` is a legitimate result; a fabricated
   attribution is not.
8. **Fraud-linkage scoring** aggregates how often victim-reported wallets
   terminate at an exchange, time-decayed with a configurable half-life
   (default 90 days) and squashed onto 0-100 so one prolific reporter cannot
   dominate. Every contributing case is stored with its age, decay weight and
   points, so the number is reconstructable by hand.
9. **`GET /api/v1/wallet`** returns trace path, attribution, risk label, risk
   score and the contributing case IDs in one response.

### What Phase 3 actually does

Open **https://localhost:8443** (Caddy, TLS — accept the local-CA certificate on first visit; see `docs/tls.md`). `http://localhost:5174` remains available for development. Plain JavaScript + JSX throughout — no
TypeScript, PropTypes for runtime type-checking.

10. **Intake form** validates as you type against `/wallets/validate`, so a
    mistyped address is rejected *before* it becomes a case that can never be
    traced. The feedback is real checksum validation (Base58Check, bech32
    polymod, EIP-55), not a regex. The victim field is labelled pseudonymous and
    the copy tells the investigator not to enter PII.
11. **Sankey money-flow trace** (D3 `d3-sankey`). Columns are pinned to the
    backend's own `hop` number rather than d3's default alignment — otherwise
    every dead-end is drawn in the last column, showing a peel-off at hop 3
    level with the terminal exchange at hop 7 and visually asserting a depth
    that is not true. Ribbon width is value-proportional with a floor, so
    low-value peel-chain hops stay visible instead of becoming hairlines.
    Colour marks the reported wallet, attributed services, mixers and sanctioned
    addresses. Nodes are clickable and every node/ribbon has a hover tooltip
    with exact amounts and transaction ids.
12. **Attribution card** leads with the *method* badge — `CURATED TAG` vs
    `BEHAVIOURAL GUESS` vs `UNATTRIBUTED` — because whether an attribution is a
    sourced fact or a model's guess matters more than the name attached to it.
    The source (`ofac_sdn`, `etherscan_labels`, …) is always shown.
13. **Risk panel + contributing cases.** The score is never displayed alone: the
    label, the meter with threshold ticks, the contributing factors, and a table
    of every contributing case with its age, decay weight and points. The table
    footer sums the raw points and shows the resulting score, so an investigator
    can recompute the number by hand. That is the explainability requirement.
14. **Deep links.** `?address=<addr>` analyses on load, so a case can be shared
    as a URL rather than a description of where to click.
15. **Freeze workflow** is live as of Phase 4 (see below).

### What Phase 5 actually does

**`scripts/e2e_demo.py`** drives the whole system over HTTP and asserts each
stage really happened, printing the value it observed so a failure names the
broken link. **42/42 checks pass.** It covers intake and checksum rejection,
graph build, clustering, attribution with its source, the explainable score,
alert publication, live delivery over the authenticated WebSocket, an
independently verified PDF hash, evidence chain of custody, every path through
the freeze approval gate, the mock NCRP feed, the STR draft, and that
case-linked data is unreachable anonymously.

```bash
docker compose exec backend python scripts/e2e_demo.py         # 42 pipeline checks
docker compose exec backend python scripts/rbac_audit.py       # who can reach what
docker compose exec backend python scripts/security_probe.py   # what breaks when hostile
```

**`scripts/rbac_audit.py`** probes every endpoint three ways — anonymous, as an
investigator, as a supervisor — and fails if any is reachable more freely than
its class allows. It tests behaviour, not declarations: a forgotten
`Depends(get_current_user)` shows up as a 200 where a 401 belongs. It also
checks separation of duties, token forgery, and input handling.

**`scripts/security_probe.py`** asks the other question: not who may call an
endpoint, but what it does when the request is hostile. **32/32 checks pass.**
Forged and expired tokens, `alg=none`, an edited role claim, mass assignment of
`status` and `approved_by`, malformed and oversized bodies, injection strings,
path traversal in a UUID slot and in an upload filename, stack-trace leakage,
security headers, CORS, and the login lockout — including that a correct
password does not lift it. It cleans up the rate-limit keys it dirties, so the
stack is usable immediately afterwards.

#### The finding this pass produced

Four endpoints were serving the **investigation picture to anonymous callers**:

| Endpoint | What it leaked |
|---|---|
| `GET /api/v1/wallet` | contributing case numbers, case ids, reported timestamps |
| `GET /api/v1/exchanges/ranked` | the case numbers behind every exchange score |
| `GET /api/v1/alerts/recent` | alert text naming the case and destination exchange |
| `GET /api/v1/wallets/multi-reported` | which addresses recur across complaints |

A single unauthenticated request returned 23 case numbers and the exchanges
under scrutiny. All four now require a signed-in officer, as does the alert
WebSocket (token as a query parameter, since a browser socket cannot set
headers). The dashboard gates those views behind sign-in.

Worth being explicit: the first run of the audit reported *no findings*, because
the audit's own classification said those endpoints were meant to be public —
written by the same author as the endpoints. The finding only surfaced when the
responses were read rather than the status codes counted.

### Interface design

The dashboard is hand-built — no component library, no image assets, 19 `.jsx`
files and a token-based stylesheet. The layout conventions are drawn from the
tools investigators already use, adapted rather than copied:

| Pattern | Borrowed from | Why it earns its place here |
|---|---|---|
| Light theme by default, dark as a real second palette | Etherscan, TRM, MetaSleuth | Findings get screenshotted into briefs, where a dark capture reads badly. Dark follows the OS preference on first visit. |
| Persistent global search, `/` to focus | Etherscan, developer consoles | An address is the entry point to everything; it should never be more than one keystroke away. |
| Address chip: truncated mono + one-click copy + inline entity tag | Etherscan | A raw 42-character hex string is unusable. This is the single most repeated element in the UI. |
| Summary stat row above the detail | Etherscan address pages | Chain, hops, terminal service and risk answer "what am I looking at" before any scrolling. |
| Tabbed detail (Flow / Attribution / Risk / Case file) | MetaSleuth, Arkham | A trace produces four kinds of evidence; stacking them means scrolling past three to reach the fourth. |
| Left rail for Investigate / Cases / Alerts / Exchanges | Compliance consoles generally | These are destinations, not sections of a document. |
| Expandable, pinnable flow graph with entity labels | MetaSleuth fund-flow maps | Following hops is the core task; nodes carry their attribution inline. |
| Relative timestamps with exact value on hover | Every block explorer | "3m ago" is scannable; evidence needs the precise instant. |

**Design system.** `styles/tokens.css` holds one source of truth for colour,
type, spacing and elevation; components reference tokens and never hardcode a
hex value. Dark theme redefines only the applied tokens, so a component written
against `--bg` / `--text` needs no dark-specific rule — including the D3 Sankey,
which reads its palette from the same tokens at render time.

Colour is semantic, never decorative: green means low-risk or an attributed
service, amber means a mixer, red means sanctioned or high risk. Mixer contact
gets its own tone rather than reusing red, so an investigator never reads
"touched a mixer" as "sanctioned entity".

### What Phase 4 actually does

16. **Officer sign-in** with JWT + role claims. Two roles matter:
    `investigator` files and drafts; `supervisor` is the **only** role that can
    approve a freeze or an STR. The role is re-read from the database on every
    request rather than trusted from the token, so revoking it takes effect
    immediately instead of at token expiry.
17. **Case file**: notes, evidence upload, and a hashed forensic PDF.
18. **Chain of custody.** Every exhibit and every exported report stores a
    SHA-256 of the bytes actually written. `Verify hash` re-reads the file and
    recomputes — and the digest is reproducible outside the system entirely
    (`sha256sum report.pdf`).
19. **Mock NCRP/1930 intake** (`POST /api/v1/ncrp/intake`). Models the contract
    such an integration would need. Not connected to anything real; every
    complaint it accepts is stored with `source = "ncrp_mock"`, and it rejects a
    bad checksum at the boundary rather than opening an untraceable case.
20. **FIU-IND-style STR draft.** Draft only — nothing is filed, and the document
    says so. It also states its own evidential gaps (no KYC, no account holder,
    clustering is probabilistic), because a narrative that hides them is worse
    than one that names them.
21. **Freeze workflow with mandatory human approval** — see below.
22. **Real-time alerts**: Redis Streams → WebSocket → dashboard. A Medium/High
    resolution pushes an alert; **Low deliberately does not**, because alerting
    on everything trains investigators to ignore the feed. The stream is
    replayable, so an alert raised while nobody had the dashboard open is still
    delivered on connect.

### The freeze workflow: how "no auto-fire" is actually enforced

```
draft  →  pending_approval  →  approved  →  dispatched
                          ↘   rejected
```

Five independent guards, each with a test:

| Guard | Enforced by |
|---|---|
| Creation never yields an approved request | `status` is hardcoded to `draft`; not settable by the caller |
| A draft cannot be approved | `409` unless status is `pending_approval` |
| Only a supervisor/admin can approve | `require_approver` dependency → `403` |
| The requester cannot approve their own request | Explicit identity check → `403`, even for a supervisor |
| Nothing is dispatched unapproved | `409` unless status is `approved` |

Plus a database constraint (`ck_freeze_approved_needs_actor`) that refuses an
approved row with no approver, and an audit-log entry for every transition.

**No code path in this system calls the approval function.** Not intake, not
scoring, not alerting. It is reachable only from an authenticated HTTP request
made by a person. There is a test (`test_no_analysis_call_ever_creates_an_approved_freeze`)
that runs the whole pipeline and asserts no approval appeared.

Verified against the live database after the test run:

```
status            count  with_approver
approved              4              4
dispatched            5              5
-- approved/dispatched rows lacking an approver: 0
```

### The tagged-address database

Seeded from the public sources named in the brief, **3,344 tags / 1,472
entities**, every one recording where it came from:

| Source | Tags | What it is |
|---|---:|---|
| Etherscan Label Cloud | 1,066 | via GraphSense `etherscan-wordcloud-*` packs |
| OFAC SDN | 948 | US Treasury sanctioned digital-currency addresses |
| GraphSense OFAC pack | 544 | community-curated sanctions tags |
| WalletExplorer | 386 | BTC service-wallet attributions |
| GraphSense TagPacks | 272 | exchange + mixer packs |
| Binance Proof of Reserves | 123 | published by the exchange itself — the only self-attested source here |
| Synthetic (demo ring) | 5 | fictional, marked `source: synthetic` |

By type: 1,072 exchange, 948 sanctioned, 697 unknown, 240 darknet, 201 gambling,
184 mixer, 2 payment processor.

Refresh with:
```bash
docker compose exec backend python ml/src/download_data.py all
docker compose exec backend python ml/src/seed_tags.py
```

### Fraud classifier - real held-out numbers

Trained on the **public Elliptic Bitcoin Dataset** (46,564 labelled
transactions, 4,545 illicit, 182 features), validated on a **temporal split** -
train on time steps 1-34, test on 35-49. A random split would leak future
information (transactions within a time step are highly correlated) and inflate
these numbers, so it is not used.

| Metric (illicit class, held out) | Value |
|---|---:|
| Precision | **0.8824** |
| Recall | **0.7341** |
| F1 | **0.8014** |
| ROC-AUC | **0.9299** |
| Average precision | **0.8048** |

Held-out confusion matrix: TP 795, FP 106, FN 288, TN 15,481 (16,670 rows,
1,083 illicit). Full report: `ml/artifacts/metrics.json`.

Trained and validated with xgboost's **native Booster API**, not the
scikit-learn wrapper. The wrapper writes `_estimator_type` into the model JSON,
which scikit-learn 1.9 no longer defines, so a wrapper-written artifact stops
loading the moment the image is rebuilt onto a different xgboost. The native
JSON loads across xgboost 2.x and 3.x; `test_model_artifact.py` guards this.

Recall is materially lower than precision, and that is the honest result rather
than a tuning failure: the Elliptic data contains a known distribution shift
after time step 43 (the "dark market shutdown"), which the temporal protocol
deliberately exposes. Reproduce with:

```bash
docker compose exec backend python ml/src/train_fraud_clf.py
```

### Stretch goal: a GNN, and an honest negative result

Your brief listed a PyTorch Geometric GNN as a stretch goal once the MVP was
done. It is built, reproducible, and **it does not beat the baseline** — which
is the result, not a failure to report.

2-layer GraphSAGE over the full 203,769-node / 234,355-edge Elliptic graph,
same temporal split and same metrics as the XGBoost baseline:

| Metric (illicit, held out) | XGBoost | GraphSAGE | Δ |
|---|---:|---:|---:|
| Precision | **0.8824** | 0.5672 | −0.3152 |
| Recall | **0.7341** | 0.6076 | −0.1265 |
| F1 | **0.8014** | 0.5867 | −0.2147 |
| ROC-AUC | **0.9299** | 0.8900 | −0.0399 |

This matches the published finding for Elliptic: in Weber et al. (2019) Random
Forest outperformed a GCN on illicit recall (0.67 vs 0.51); this GraphSAGE sits
between the two. Elliptic's node features already encode aggregated
neighbourhood statistics, so much of what message passing would add is present
in the features, and the post-step-43 distribution shift hurts the graph model
harder.

**XGBoost remains the shipped model. The GNN is not wired into the API.**

Two real bugs surfaced getting here, both worth recording:

1. **A gutted graph.** Building it from labelled nodes only discarded 197,731 of
   234,355 edges — Elliptic's labelled transactions connect to each other
   *through* unlabelled ones. Fixed by including all 203,769 nodes for message
   passing while keeping loss and metrics on labelled nodes only.
2. **Unnormalised features.** The matrix spans −13 to 445,268 with σ≈300. Trees
   are scale-invariant so the baseline never cared; the GNN's first-epoch loss
   was 35 (cross-entropy should start near 0.69) and it collapsed to predicting
   one class. Z-scoring — fitted on **training nodes only**, to avoid leaking
   the held-out tail — moved F1 from 0.137 to 0.587 and ROC-AUC from 0.576 to
   0.890.

```bash
docker compose exec backend python ml/src/download_data.py elliptic-full   # ~67 min
docker compose exec backend python ml/src/download_data.py edgelist
docker compose --profile ml run --rm ml python ml/src/train_gnn.py
```

PyTorch lives in a **separate `ml/` image** (1.9 GB) behind a compose profile,
so the API image stays at 1.46 GB and the service never ships a training stack
it cannot use.

**What this model is not.** It scores how illicit a *Bitcoin transaction* looks.
It is not the exchange fraud-linkage score - Elliptic labels transactions, not
exchange culpability. The exchange score is the explainable aggregate in
`app/services/risk.py`; conflating the two would misrepresent what the model
knows.

---

## Running on live chain data

`DEMO_MODE=false` points the same code at real chains. Everything below exists
because that switch broke things the synthetic dataset could never have exposed.

### Rate limits that arrive disguised as success

Etherscan's free tier refuses an over-rate request with **HTTP 200** and a body
saying `NOTOK`. The HTTP helper was caching that refusal for five minutes, so
one burst of concurrency poisoned every subsequent lookup of those addresses
and the trace came back **quietly incomplete** — the worst possible failure for
evidence. Three things now prevent it:

- a **pacer** holding Etherscan to 2.8 calls/second, below the documented 3;
- `cache_if` on the HTTP helper, so a response is only cached when it is an
  actual answer (`get_json(..., cache_if=_etherscan_answered)`);
- a trace with any failed lookup **cannot report itself complete**. The panel
  says which addresses were not read.

### Budgets, because chain fan-out is not ours to control

Each expanded address costs an indexer call, and how far a wallet fans out is a
property of the chain, not of anything this system decides. Without ceilings a
single complaint could spend a daily quota or leave an officer watching a
spinner for six minutes. Every one of these is a setting:

| Setting | Default | What it bounds |
|---|---:|---|
| `TRACE_TIME_BUDGET_SECONDS` | 18 | wall-clock for the upstream walk |
| `CONNECTOR_CALL_BUDGET` | 200 | indexer calls for one traced address |
| `TRACE_CONCURRENCY` | 6 | parallel lookups per depth level |
| `GRAPH_MAX_NODES` | 200 | nodes in the *picture* — findings use separate, selective queries |
| `TRACE_STOP_AT_SERVICES` | true | stop at exchanges/processors/gambling |
| `TRACE_MAX_DEPTH` | 8 | hops |

`TRACE_STOP_AT_SERVICES` is the one that matters most. Following money *into* an
exchange's hot wallet walks other customers' funds — it consumed most of the
call budget and told an investigator nothing, because the answer at that point
is already "it reached this exchange; serve them a legal request".

Measured on live BTC/ETH/TRON wallets: **1–25 seconds** end to end, against
roughly six minutes before this work.

### Correctness bugs that only live data could surface

| Symptom | Cause | Fix |
|---|---|---|
| `/wallet` 500s on some live wallets | Neo4j refuses `shortestPath` when start == end | bind `dest` before the path search |
| Risk scoring took 101 s | path enumeration over a real subgraph | bound-endpoint `shortestPath`, capped target set — now ~1 s |
| `MemoryPoolOutOfMemoryError` | one enormous delete transaction | batched via `apoc.periodic.iterate` |
| A TRC-20 transfer valued at ₹1.0 × 10²⁶ | TronGrid returns `"token_info": {}` for some real transfers; `contract=None` is how `Asset` spells *native TRX*, so a token was priced at the TRX rate with a guessed decimal scale | the transfer is kept (the movement is evidence) as an asset that cannot be named, scaled or priced |

The clustering heuristic also changed shape: common-input-ownership now emits a
**star** (n−1 edges) rather than a clique (n(n−1)/2). The connected components
are identical; the write cost is not.

### Reaching it from another device

`https://<LAN_IP>:8443` serves the dashboard to anything on the same Wi-Fi.
Browsers send **no SNI for a bare IP address**, so Caddy needs an explicit
`default_sni` — without it the handshake fails before any HTTP is spoken.

Closing that door opened another, which is now also closed: Postgres, Neo4j and
Redis were published on all interfaces, Redis without a password. They are bound
to `127.0.0.1` in `docker-compose.yml`. Only Caddy listens on the LAN.

```bash
LAN_IP=192.168.1.42 docker compose up -d      # your machine's address on the Wi-Fi
```

---

## External intelligence sources

Four public services, each doing the one job it is actually good at. **Every one
of them is optional**, and a source that was not consulted is reported as *not
checked* — never as a clean result. Saying "0 scam reports" about a lookup that
never ran is how a system launders its own gaps into evidence.

| Source | Used for | Needs a key | Trust weight |
|---|---|---|---:|
| **Binance Proof of Reserves** | authoritative Binance wallet addresses, straight from the exchange | no | 1.00 |
| **WazirX** | INR valuation without a USD cross-rate — it quotes INR directly | no | — |
| **Arkham Intelligence** | entity labels for wallets the curated tag DB does not know | yes | 0.85 |
| **Chainabuse** | scam reports already filed against the suspect wallet | yes | — |

```bash
docker compose exec backend python scripts/sync_binance_por.py   # refresh PoR addresses
```

Binance PoR currently contributes **123 tags**. Arkham sits between the curated
tag database and the behavioural classifier (`METHOD_ARKHAM`): a commercial
label is better than a guess and weaker than a published, citable tag.

Chainabuse's free tier is metered per month, so `CHAINABUSE_MONTHLY_BUDGET`
(default 10) reserves each call in Redis — shared across workers, since an
in-process counter would let a four-worker deployment spend four times the
quota. A spent quota reports `budget_exhausted`, which the UI renders as *not
checked*.

**Coupcoin is not integrated.** No public explorer, API or source code for it
could be found, and tracing a chain requires one of those or a node to query. It
is left out rather than stubbed.

---

## Frontend rule: JSX only

No TypeScript anywhere in this repo — no `.ts`, no `.tsx`, no `tsconfig.json`,
no `@types/*`. Runtime type-checking is done with **PropTypes**, and **ESLint is
the static gate** in place of `tsc` (`react/prop-types` is set to `error`).
Currently **25 `.jsx` + 10 `.js`**, zero TypeScript.

Enforced by `scripts/check_no_typescript.sh`, which fails on any TypeScript
source, tsconfig, or TS tooling dependency:

```bash
bash scripts/check_no_typescript.sh
```

---

## Setup

**Prerequisites:** Docker with Compose v2.20+. Python 3.11 and Node 22+ are only
needed if you want to run services outside containers.

```bash
cp .env.example .env      # optional: the defaults work as-is
docker compose up -d      # build and start postgres, neo4j, redis, backend, frontend
```

First start takes a few minutes: images pull, and **Neo4j needs ~85 s** to load
the GDS and APOC plugins before it reports healthy. The backend waits for it.

A `Makefile` wraps the common commands, but `make` is not installed on every
Windows setup — the raw equivalents are:

| Task | Make | Raw |
|---|---|---|
| Start | `make up` | `docker compose up -d --build` |
| Stop | `make down` | `docker compose down` |
| Stop + wipe data | `make nuke` | `docker compose down -v` |
| Status / ports | `make ports` | `docker compose ps` |
| Readiness | `make health` | `curl -s localhost:8001/api/v1/health/ready` |
| Backend tests | `make test` | `docker compose exec backend pytest -q` |
| Frontend lint | `make lint-frontend` | `docker compose exec frontend npm run lint` |

| Service | URL |
|---|---|
| Dashboard (TLS) | https://localhost:8443 |
| Dashboard from another device on the Wi-Fi | `https://<LAN_IP>:8443` (start with `LAN_IP=<your ip>`) |
| Dashboard (plain HTTP) | http://localhost:5174 |
| Backend API docs | http://localhost:8001/docs |
| Readiness probe | http://localhost:8001/api/v1/health/ready |
| Neo4j Browser | http://localhost:7475 (`neo4j` / `sihdevpass`) |
| PostgreSQL | `localhost:5434` (`sih` / `sihdev` / db `sih183`) |

> **The datastores listen on `127.0.0.1` only.** Postgres, Neo4j and Redis were
> published on all interfaces — Redis with no password — which made them
> reachable by anything on the same Wi-Fi the moment LAN access was added. Only
> Caddy is exposed to the network.

> **Host ports are deliberately non-default** (5434 / 7475 / 7688 / 6380 / 8001 /
> 5174) so this stack can run side by side with another local project holding the
> conventional ports. Container-internal ports are unchanged; override any of
> them in `.env`.

The Postgres schema in `infra/postgres/init.sql` is applied automatically on
first start. Neo4j constraints from `infra/neo4j/init.cypher` are applied by the
backend at startup, idempotently.

### API keys — all optional

The stack runs fully without any key: `DEMO_MODE=true` serves the synthetic
fraud-ring dataset. To trace live chain data, set `DEMO_MODE=false`.

| Key | For | Without it |
|---|---|---|
| — | **Bitcoin** via Blockstream Esplora | works keyless |
| `ETHERSCAN_API_KEY` | Ethereum | free tier, 3 calls/s |
| `TRONGRID_API_KEY` | TRON | works keyless at a lower rate |
| `ARKHAM_API_KEY` | entity labels | tier skipped; attribution falls through to the classifier |
| `CHAINABUSE_API_KEY` | scam reports | reported as *not checked*, never as *no reports* |
| — | **WazirX / Binance PoR** | keyless |

Turning off `DEMO_MODE` is the act of claiming real provenance, so it also
**force-disables the published demo accounts** (`demo_auth_enabled` is the AND
of two switches) and makes `check_secrets()` refuse to start on a placeholder
signing key rather than merely warn.

### Troubleshooting

**`Bind for 0.0.0.0:<port> failed: port is already allocated`** — another stack
holds that port. Override the offending entry in `.env` (see *Host ports* above)
rather than stopping the other stack.

**Neo4j reported unhealthy on first `up`** — its `start_period` is 150 s to cover
plugin loading. If the host is slow, re-run `docker compose up -d`; the container
keeps starting in the background and the second invocation picks it up healthy.

**`docker compose logs neo4j` shows `chown: ... Read-only file system`** — the
Neo4j entrypoint chowns its import directory, so it cannot take a read-only bind
mount there. `infra/neo4j/init.cypher` is therefore mounted into the *backend*
container, which applies the constraints itself at startup.

---

## Running the demo

Two paths: the deterministic synthetic ring, and live chain data.

```bash
# 1. (Re)generate the synthetic fraud ring - deterministic, seed 26183
docker compose exec backend python scripts/generate_synthetic_complaints.py   --out /app/ml_seeds/synthetic_dataset.json

# 2. Validate an address without opening a case
curl -s -X POST http://localhost:8001/api/v1/wallets/validate   -H 'Content-Type: application/json'   -d '{"address":"1A1zP1eP5QGefi2DMPTfTL5SLmv7DivfNa"}'

# 3. Report a suspect wallet - traces, builds the graph, clusters
curl -s -X POST http://localhost:8001/api/v1/wallets   -H 'Content-Type: application/json'   -d '{"address":"<address from ml/seeds/synthetic_dataset.json>","source":"synthetic"}'

# 4. Wallets reported by more than one victim
curl -s http://localhost:8001/api/v1/wallets/multi-reported
```

### One-command demo load

```bash
docker compose exec backend python ml/src/download_data.py all   # once: public datasets
docker compose exec backend python scripts/load_demo.py          # seed tags + file complaints
docker compose exec backend python scripts/load_demo.py --reset  # ...or start from zero
```

`load_demo.py` seeds the tag database and files all 13 synthetic complaints,
then prints the exchange ranking. From a clean slate (`--reset`, or fresh
volumes) that is:

```
  MEDIUM  63.21    6 cases  TRON  Meridian Exchange
  LOW     39.35    3 cases  ETH   Northwind Digital
  LOW     28.35    2 cases  BTC   Kestrel Trade
```

**These climb on every run.** Filing the same thirteen complaints again adds
six more cases against Meridian, and the score follows — so a stack that has
been demonstrated a few times shows High where a clean one shows Medium. That
is the scoring working as designed, but it makes any number written down here
wrong by the second run. `--reset` clears the case-side data (cases, wallets,
scores, alerts, the transaction graph) while leaving the tag database and the
demo accounts intact, which is what makes the figures above reproducible.

Then analyse a single wallet end to end:

```bash
curl -s "http://localhost:8001/api/v1/wallet?address=<synthetic address>"
curl -s "http://localhost:8001/api/v1/exchanges/ranked"
```

All three fictional exchanges are reached by tracing: Meridian at hop 7 (TRON),
Northwind at hop 7 (ETH, via a mixer, flagged), Kestrel at hop 5 (BTC).

### Dashboard demo path

1. Open **https://localhost:8443** (Caddy, TLS — accept the local-CA certificate on first visit; see `docs/tls.md`). `http://localhost:5174` remains available for development.
2. Paste a synthetic address from `ml/seeds/synthetic_dataset.json` into the
   form — chain and checksum validate as you type.
3. **Analyse only** draws the trace for an address already in the graph;
   **File complaint & trace** opens a new case, builds the graph, then analyses.
   Filing the same address twice surfaces the repeat-report fraud-ring banner.
4. Or jump straight in with a deep link:
   `http://localhost:5174/?address=<synthetic address>`

The TRON address reaches *Meridian Exchange* at hop 7 with a **HIGH** score and
ten contributing cases listed underneath.

The generator writes `ml/seeds/synthetic_dataset.json`: 43 transactions across
BTC/ETH/TRON, 13 complaints, 4 entities (3 fictional exchanges + 1 mixer). Every
generated address is genuinely checksum-valid, so the demo data passes the same
validator real input does.

### Demonstrating on live chain data

With `DEMO_MODE=false`, file any real address. Finding good demonstration
wallets is itself a task, because a wallet that was quiet last week may be busy
today — one address used here went from 9 nodes in 7 seconds to 130 nodes in 62
seconds over three days.

What makes a wallet worth demonstrating is that it **reaches a tagged exchange
within a few hops while staying small enough to read**. Both halves can be
checked without filing anything: walk the chain with the live connector and test
each hop against the tag database. That keeps the case genuinely new when the
officer files it, at the cost of not knowing the risk score in advance — the
score does not exist until a case does.

Two properties of the scoring are worth knowing before a demonstration:

- **Filing the same address twice raises its score.** The score counts linked
  complaints with age decay, so a stack that has been demonstrated a few times
  shows High where a clean one shows Medium.
- **The filed amount does not affect the score.** It is carried into the case
  record and the PDF, but the score is driven by complaint count and age alone.


> On Git Bash for Windows, prefix `docker compose exec` with `MSYS_NO_PATHCONV=1`
> when an argument is an absolute container path, or Git Bash rewrites `/app/...`
> into a Windows path.

---

## Tests

```bash
docker compose exec backend pytest -q      # backend tests
docker compose exec backend ruff check app/
docker compose exec frontend npx vitest run # frontend tests
docker compose exec frontend npm run lint  # ESLint - the static gate in place of tsc
docker compose exec frontend npm run build # production build must succeed
bash scripts/check_no_typescript.sh        # hard JSX-only gate
```

Latest result: **391 passed** (pytest), **63 passed / 7 files** (Vitest),
**ruff clean**, **ESLint 0 errors**, **production build OK**, **PASS**
(JSX-only gate — 25 `.jsx` + 10 `.js`, zero TypeScript).

> The backend suite refuses to run against a deployment with `DEMO_MODE=false`,
> because its fixtures delete graph data. Override deliberately with
> `ALLOW_TESTS_ON_LIVE_DEPLOYMENT=1` when the live stack is the only one you have.

The frontend now has **Vitest + Testing Library** alongside ESLint (which carries
the weight `tsc` would in a TypeScript project, with `prop-types` validation on)
and a clean production build. The tests cover the things a lint pass cannot see:
that money is formatted in rupees an officer recognises, that a wallet
identification names its source, that the sidebar width clamps and survives a
corrupt stored value, and — most importantly — that a check which did not run is
rendered as *not checked* rather than as a clean result.

**Backend — 391 tests across 26 files.** The largest:

| Suite | Tests | Covers |
|---|---:|---|
| `test_chain_detect.py` | 33 | BIP-173/BIP-350/EIP-55 reference vectors, checksum rejection, normalisation |
| `test_phase4.py` | 33 | Auth, notes, evidence + report hashing, NCRP mock, STR, **freeze approval gate**, alerts, WebSocket |
| `test_exposure.py` | 30 | Which service the money reached, ranking, and the plain-language answer |
| `test_connectors.py` | 23 | Factory + provenance fallback, synthetic dataset integrity, ETH normalisation |
| `test_external_intel.py` | 22 | Arkham, Chainabuse, WazirX, Binance PoR — parsing, ordering, and "not checked ≠ clean" |
| `test_observability.py` | 21 | Structured logging, request ids, metrics |
| `test_connector_http.py` | 17 | Retry, backoff, and the rule that a rate-limit refusal is never cached |
| `test_analysis.py` | 17 | `/api/wallet` contract, Sankey shape, explainability, ranking |
| `test_clustering.py` | 16 | Both UTXO heuristics against a real Neo4j + GDS |
| `test_risk.py` | 15 | Time decay, saturation, thresholds, aggravating factors |
| `test_provenance.py` | 15 | Synthetic data can never be reported as live-chain data |
| `test_live_tracing.py` | 14 | Live connector behaviour under rate limits, budgets and malformed upstream payloads |
| `test_illicit_model.py` | 14 | Classifier inference contract |
| `test_report_sections.py` | 13 | Every section of the forensic PDF, including the "not checked" states |
| `test_sessions.py` | 11 | Token revocation, role re-read, cutoffs |
| `test_intake.py` | 11 | Intake API, dedupe, cross-case view |
| `test_audit_chain.py` | 10 | Tamper-evident audit log |
| `test_attribution.py` | 10 | Tag lookup, cluster propagation, "never name a guess" |

**Frontend — 63 tests across 7 files** (`npx vitest run`): number and currency
formatting, the exposure panel's empty and populated states, the identification
card's source naming and *not checked* handling, sign-in, and the sidebar
resizer's clamping and storage recovery.

Graph-backed tests run against the live compose stack and skip cleanly when it
is down.

> **Hot reload.** Both services reload on save (verified: ~2s each). If the
> backend ever stops picking up edits, check its logs for
> `WatchfilesRustInternalError: File system loop found` — a symlink pointing at
> its own ancestor inside a mounted directory crashes the file watcher outright
> and silently kills reload. Remove the symlink; do not reach for a restart loop.

> **Running the tests clears graph transaction data.** The fixtures scope their
> cleanup so the seeded tag database survives, but the traced money flow does
> not. Reload the demo afterwards with
> `docker compose exec backend python scripts/load_demo.py --skip-tags`.

---

## Assumptions

Recorded here rather than blocking on questions, per the working agreement.

1. **Redis Streams instead of Kafka.** One less heavy container for a demo
   stack. The publish path is isolated behind `app/services/alerts.py`, so
   moving to Kafka is a driver swap rather than a rewrite.
2. **Simplified JWT with role claims instead of Keycloak.** Roles:
   `investigator`, `supervisor` (the only role that can approve a freeze
   request), `admin`. Keycloak would add a container and realm configuration
   without changing what the prototype demonstrates.
3. **`DEMO_MODE` defaults to true**, including when configuration is missing.
   Fail-safe: absent explicit configuration the system assumes synthetic data
   rather than claiming live-chain provenance.
4. **Neo4j 5.26 Community with the GDS plugin.** GDS on Community lacks the
   enterprise projection features, but Weakly Connected Components and label
   propagation — what the clustering heuristics need — work at demo scale.
5. **The Elliptic dataset is not committed** (~200 MB, redistribution
   restrictions). `ml/src/download_data.py` fetches it and
   `ml/artifacts/metrics.json` holds the reported held-out numbers so the
   metric is reproducible.
6. **Default trace depth 8** (`TRACE_MAX_DEPTH`), raised from 6 during Phase 2.
   Peel chains in this problem domain routinely run past six hops - the demo
   ring's exchange sits at hop 7, and at depth 6 the trace missed it entirely
   and fell back to a behavioural guess. The brief's range is 6-8; 8 is the end
   of it that actually reaches the destination.
7. **Mixers are flagged, not unwound.** Interaction with a known mixer is
   recorded as a risk signal; this system does not attempt to defeat mixing.
8. **Tracing stops at custodial services** (`TRACE_STOP_AT_SERVICES`, default
   on). Past an exchange's hot wallet the trail runs through other customers'
   money, and the investigative answer is already known: serve that exchange a
   legal request. Turning it off is supported and slow.
9. **An unvalued asset is left unvalued.** Where no INR price exists for a token,
   the amount is shown in the token and excluded from valuation. It is never
   coerced to zero, which would silently rank a real exposure as worthless, and
   never guessed.
10. **Every external source is optional, and absence is stated.** A source that
    was not consulted — no key, spent quota, failed call — reports *not checked*.
    A finding of "none" is only ever printed when a lookup actually returned
    none.
11. **Coupcoin is not supported.** Integrating a chain needs a public explorer,
    an API, or a node to query; none is publicly available for Coupcoin.

---

## Repository layout

```
backend/    FastAPI service - API, services, connectors, graph/DB access, tests
frontend/   Vite + React (JSX only) investigator dashboard, Vitest suite
ml/         Dataset fetch, feature engineering, model training, tag seed data
infra/      Postgres DDL, Neo4j constraints, Caddy TLS/LAN config
docs/       Architecture, schema, API contracts, demo script, TLS notes
scripts/    Synthetic generator, e2e demo, security probes, Binance PoR sync, JSX-only guard
```
