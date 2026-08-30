# PROJECT SENTINEL — Build Specification

**Track:** Google All Things Agentic — *Fortified Enterprise Fleet*
**Code freeze:** Monday Aug 31, 10:00am ET
**Reader:** the coding agent. This document is the contract. Sections 4 and 6 are non-negotiable.

Implement in milestone order (§3). Stop at every gate and verify before continuing. If a gate
fails, take the documented fallback — do not attempt to repair the blocked component.
**Read §10 (anti-goals) before writing any code.**

---

## 1. What Sentinel does

An enterprise fleet of agents that manages demand autonomously, in the background.

The fleet continuously watches demand and supply signals. When it detects a deviation, it
generates the plausible forward scenarios, scores each one for cost and service impact using
a deterministic solver, and executes the healing action — autonomously when the action sits
inside policy, escalated to a signed human decision when it does not. Every step lands in a
tamper-evident BigQuery ledger.

**The spine is six verbs. Everything in this build maps to one of them:**

| | | |
|---|---|---|
| **PREPARE** | Repair and normalize dirty inbound feeds into clean BigQuery tables | BigQuery Data Engineering Agent |
| **SENSE** | Background watch over demand signals, inventory positions, and inbound ASNs | continuous |
| **SCENARIO** | Generate candidate forward outcomes for each detected deviation | agentic (ADK) |
| **SCORE** | Map each scenario to cost, service, and SLA exposure | deterministic (OR-Tools) |
| **HEAL** | Execute the remediation, or escalate it | OKF Policy Governor |
| **ASK** | Answer natural-language questions over the fleet's own record | Conversational Analytics Agent |

PREPARE and ASK bracket the autonomous loop: the fleet cleans its own inputs and explains its
own decisions. That is the full enterprise data lifecycle, not just an agent workflow.

The autonomy claim is that SENSE→SCENARIO→SCORE→HEAL closes without a human for routine
deviations, and that the human only ever sees the exceptions — pre-costed and pre-argued.

---

## 2. Compliance requirements (from the official rules)

| Requirement | Implementation |
|---|---|
| Gemini 3.5 or newer via Vertex AI | `gemini-3.7-flash` on the Vertex `global` endpoint, one model ID across all agents |
| A Google Agent Framework | Google ADK |
| A Google Cloud infra service | BigQuery, Cloud Run, Pub/Sub, Firestore |
| Public repo with README spin-up instructions | §9 — **judged even if not run** |
| Bonus: Gemma integration | Gemma guardrail classifier on Cloud Run |

### 2.1 Model pinning — read this twice

**RESOLVED AT GATE 0 (Sun Aug 30): `gemini-3.5-flash` is not available in `us-east4` or
`us-central1`. The permitted model is `gemini-3.7-flash` on the Vertex AI `global` endpoint.**

This clears the "Gemini 3.5 or newer" floor with room to spare, and 3.7 Flash reportedly
outperforms 3.1 Pro on agentic benchmarks. The tradeoff is inference residency, which we state
honestly rather than claim around — see the README line in §2.6.

**There is no Gemini 3.5 Pro.** It was delayed; the newest Pro model is still `gemini-3.1-pro`,
which sits *below* the 3.5 compliance floor and must not be used. A coding agent will very
plausibly write `gemini-3.5-pro` because the name is symmetrical and it "should" exist. It does
not. The same applies to invented suffixes like `-002`, `-latest`, or `-preview`.

**`gemini-3.7-flash` is the only permitted model ID in this repository.** Enforce it
structurally, not by intention:

1. **One constant.** `config/settings.py` exposes `MODEL_ID`, read from env, **with no default
   fallback string.** Missing env → hard fail at import.
2. **No construction.** Never build a model ID by concatenation, f-string, or template. Never
   accept one as a function argument. Agents are built through a single `build_agent()` factory
   that reads `settings.MODEL_ID` and exposes no model parameter.
3. **Preflight gate.** `scripts/preflight.py` resolves the publisher model against Vertex AI and
   sends a 1-token ping. It exits non-zero if the ID does not resolve. It is the first line of
   `deploy.sh` and runs at container startup.
4. **Repo assertion.** `tests/test_model_pinning.py` greps every file for the regex
   `gemini-[0-9]` and asserts the only match is the one line in `config/settings.py`.
5. **Managed agents take no model ID.** The Data Engineering Agent and Conversational Analytics
   Agent run on their own Gemini backends. Do not pass them a model parameter.
6. **Vertex client location is `global` for inference.** Everything else is `us-central1`. These
   are different values and must not be collapsed into one config variable — use
   `VERTEX_INFERENCE_LOCATION` and `GCP_REGION` separately, or an agent will "helpfully"
   unify them and break inference.

### 2.6 Region and residency — state it exactly this way

Core data and compute are pinned to `us-central1` (BigQuery, Cloud Run, Pub/Sub, Firestore).
Model inference routes via Vertex AI `global` for Gemini 3.7 Flash.

Use that sentence verbatim in the README and say it in the video. **Do not use the words
"data sovereignty" anywhere** — the honest claim is single-region data-at-rest with global
inference routing, and that is still a stronger residency posture than most submissions will
articulate at all. Pin Memory Bank to the `us` multi-region so stored memories stay consistent
with the data-at-rest claim.

### 2.2 GEAP component mapping (the track's recommended toolkit)

The Fortified Enterprise Fleet track names the Gemini Enterprise Agent Platform as its
recommended stack. Map every pillar explicitly — judges score against this list.

| GEAP pillar | Sentinel implementation | Fallback |
|---|---|---|
| Agent Registry | **GEAP Agent Registry — publish the agents to the real platform component.** Mirror the entries into `agent_registry` in BigQuery for the analytics views. | BigQuery table + YAML manifests only — **but this demonstrably costs points** |
| Agent Runtime | Cloud Run async + Pub/Sub push; background watch job on cron | — |
| Memory Bank | Vertex AI Memory Bank, computed-only supplier reliability | BigQuery `supplier_reliability` — **and if you fall back, do not call it a Memory Bank in the README. Persistence is not memory.** |
| Agent Identity | Per-agent service accounts, scoped IAM, workload identity | — |
| Agent Gateway | Cloud Run ingress + IAM policy enforcement per agent | — |
| Model Armor | Ingress + egress sanitization | Gemma classifier on Cloud Run |
| Agent Observability | OpenTelemetry → Cloud Trace, plus `agent_ops` via the ADK plugin | — |

**Track fit is not a stretch here.** The track's own worked example is an enterprise supply chain
orchestrator using Registry, Memory Bank, Agent Identity, Agent Gateway, and Model Armor.
Sentinel is that example, built. Name each pillar in the README using the platform's own
vocabulary.

**ADK orchestration pattern:** sequential (guardrail → hygiene) followed by LLM-delegated
sub-agent invocation (orchestrator → sourcing specialist). State the pattern by name in the
README; the track rewards knowing which pattern you reached for and why.

**Idempotency is a flagged theme.** Google ran a dedicated session on crash recovery, human
approval, and the idempotency trap — a resumable agent ordering two laptops. Invariant I-9 is
the direct answer. Make sure the replay test in `adversarial_tests.py` prints a clear
`duplicate: True` result; judges are primed to look for exactly this.

**Cost discipline (from the official tips):** Flash-first, `min-instances=0`, max-instance caps,
budget alerts, authenticated Cloud Run endpoints. **Record proof that it ran on GCP, then shut
services down.** The project does not need to be live at judging time.

### 2.3 Direct guidance from the Aug 25 judges' Q&A

Sourced from Google Cloud's own answers in the build session. These override inference.

**Use the platform's components, don't rebuild them.** A participant who built his own agent
registry and runtime instead of using the platform components was told plainly that Google
prefers implementations using the Agent Registry inside the agent platform, and that when
picking a winner they will prefer that. **Do not hand-roll a GEAP component that exists.**
This applies to Agent Registry, Memory Bank, and Model Armor above all.

**Being enterprise-flavored is not the same as being Fortified.** A submission described as a
governed multi-agent fleet on Gemini 3.5 Flash, ADK, Cloud Run and Firestore — durable async
supplier release workflows, separated authority, deterministic policy enforcement, replayable
evidence — was told it sounded more like Taskmaster. It had our shape and lacked our GEAP
surface. **The Fortified claim is carried by visibly using Registry, Memory Bank, Agent
Identity, and Model Armor, plus an explicit security story. Not by governance vocabulary.**

**Component proof should be brief.** Asked whether a Fortified demo should focus on the
end-to-end outcome or on proving Agent Runtime, Memory Bank and Model Armor, the answer was to
just show that you have them and what you use them for, and spend the rest of the time on the
workflow. **Name-check each component in ~5 seconds; spend the runtime on the work.**

**Judges run the code.** They confirmed they dig into the repository and have tooling that runs
the project to check whether it does what the submission claims. Every claim in the README must
be executable. This is why invariant I-13 and the "never overclaim" rule are load-bearing.

**Synthetic data is explicitly fine.** Confirmed twice for enterprise use cases where real data
can't be exposed. No hedging needed in the README.

**Submit early, refine after.** Closing advice from the judges was to submit first and keep
improving, because they judge what is there at the deadline. **Push a working draft submission
Monday morning and treat every later improvement as an edit, not a prerequisite.**

### 2.4 GEAP availability — settled

**GEAP is not an enterprise-only tier. It is Vertex AI, renamed.** Google's product page states
that all the power of Vertex AI is now within Gemini Enterprise Agent Platform, and the
announcement confirms all Vertex AI services are delivered exclusively through Agent Platform
rather than as a standalone service. Memory Bank is GA, initialized with a plain
`vertexai.Client(project=..., location=...)`, and metered at roughly $0.25 per 1,000 memories.
"Enterprise" is in the product name, not the access tier. **A personal project with billing
enabled is sufficient.**

Verify Agent Registry self-serve access empirically at Gate 0 (ten minutes in the console).
Do not abandon it on inference.

### 2.5 CLAIM INTEGRITY — absolute

**Every claim in the README, video, and Devpost description must be true of the code that
shipped.** Judges confirmed they run the project and check it against what you claim.

Explicitly forbidden, no exceptions, regardless of scoring pressure:
- Claiming a GEAP component you fell back on. If Agent Registry failed, say so.
- Naming a fallback so it implies a Google product — "Compliance Memory Bank", "Memory Bank (emulated)", "Registry-equivalent". If it's a BigQuery table, call it a BigQuery table.
- "Immutable" for the ledger. It is tamper-evident.
- Claiming a signing method you didn't build.
- Any component in the architecture diagram that isn't in the repo.

**If a component is cut, the claim is cut in the same commit.** A "Known limitations" section
costs nothing and reads as maturity. One unsupported claim discredits the fifteen invariants
that are real. This rule outranks every scoring consideration in this document.

---

## 3. Build timeline and gates

**Saturday evening → GATE 1**
`contracts/models.py` · `sql/ddl.sql` · `sql/seed.sql` loaded · `core/solver.py`
> **GATE 1:** `python -m core.solver --deviation DEV-001` prints three scored scenarios with a
> stable SHA-256. Everything downstream reads this JSON. If this is not green, stop and fix it.

**Sunday morning → GATE 2**
`core/okf.py` · `core/ledger.py` · `core/heal.py` · `scripts/adversarial_tests.py`
> **GATE 2:** smurfing, replay, and injection tests all pass from the CLI, each printing the
> rule that caught it. **These are three distinct tests — do not conflate them:**
> - *Injection (DEV-002):* `raw_note` carries a prompt-injection payload → blocked at guardrail.
> - *Smurfing (DEV-003):* **three separate $4,900 transactions against one supplier, each with its own distinct idempotency key.** The second trips `VELOCITY_CAP` on the supplier dimension. **If you reuse one idempotency key here you have accidentally written the replay test — it will trip I-9, pass, and prove nothing about the counters.**
> - *Replay (DEV-001):* the same deviation published twice, same idempotency key → second returns `duplicate: True`, no second order.

**Sunday afternoon → GATE 3**
Three ADK agents · guardrail layer · Memory Bank · **publish agents to the GEAP Agent
Registry** · Cloud Run deploy · Pub/Sub trigger
> **GATE 3:** one published deviation flows end-to-end and produces a verifiable ledger chain,
> and the agents are visible in the Agent Registry console.
> **If red:** drop Memory Bank → BigQuery table, drop Model Armor → Gemma, drop A2UI. Ship the
> spine working rather than the fleet broken. **Do not drop the Agent Registry — judges said
> outright they prefer submissions that use it.**

**Sunday evening → GATE 4**
`sql/views.sql` · Streamlit UI · signed approval · OpenTelemetry spans
> **GATE 4:** all four demo deviations (§8) run clean, back to back, twice.

**Monday morning → GATE 5**
**09:00 ET — push a complete draft submission to Devpost before anything else.** Judges' own
advice: submit first, refine after. Everything below becomes an edit rather than a race.
Then: BigQuery Data Engineering Agent (PREPARE) · Conversational Analytics data agent (ASK) ·
README verified in a clean Cloud Shell · `deploy.sh` end-to-end · A2UI adapter only if Gate 3
was green. **Freeze 10:00am ET.**
> **GATE 5:** the CA agent answers three seeded business questions with charts, and the DE
> Agent pipeline repairs one dirty feed.
> **These are configuration, not code — but they are additive risk. If either is not working by
> 8:00am ET Monday, drop it, seed the clean tables directly, and remove the claim from the
> README.** Never ship a claim you cannot show.

**GATE 0 — COMPLETE (Sun Aug 30).** `gemini-3.5-flash` unavailable in `us-east4` and
`us-central1`. Locked: `gemini-3.7-flash` on Vertex `global`; all data and compute in
`us-central1`. Residency wording per §2.6. Billing and APIs already enabled.

---

## 4. Invariants (NON-NEGOTIABLE)

Each closes an attack a judge will look for. If a milestone gets cut, these survive.

**I-1 — No LLM produces or transforms a number that reaches BigQuery or the UI.**
OR-Tools emits typed JSON; the ledger and UI render directly from it. The LLM may populate one
field, `narrative: str`, and nothing else. Enforced structurally: the orchestrator's response
schema physically contains no numeric fields.

**I-2 — Solver output is hashed.** `result_sha256` over canonical JSON, stored in the ledger,
displayed in the UI. This makes the no-hallucinated-math claim provable rather than asserted.

**I-3 — Spend caps are multi-dimensional.** Rolling 24h counters on five keys: `tenant`,
`supplier_id`, `sku_id`, `cost_center`, `workflow_root_id`. A transaction must clear all
applicable counters. A single global counter is trivially sliced.

**I-4 — Reserve-then-commit, never check-then-act.** Pending reservations count against the
ceiling. Two concurrent workflows must not both pass at $9,800 of a $10,000 ceiling.
*Implementation note: BigQuery has no row-level transaction, so the in-flight reservation lease
lives in a single Firestore document (~30 lines). BigQuery remains the system of record —
`spend_transactions` is append-only and the counters are BigQuery views. This split is
deliberate; state it in the README rather than pretending BigQuery is transactional.*

**I-5 — Normalize money before the policy check.** Convert to USD at a pinned FX rate in
config. Recompute `total = qty × unit_price` server-side. A €6,200 order must not slip a
$5,000 USD gate.

**I-6 — The ledger is hash-chained.** `record_hash = SHA256(prev_record_hash + canonical_payload)`.
BigQuery permits DML, so "immutable" is false — **"tamper-evident" is true and provable.**
Also deny DML at IAM and note it in the README. Ship `verify_chain()`.

**I-7 — Approvals are genuinely signed.** Capture the operator's OIDC ID token; persist `sub`,
`email`, `jti`, `iat`, signature. Fallback: HMAC over the approval payload using a Secret
Manager key — and then the README says "HMAC-signed", not "cryptographically signed by the
operator". Never overclaim.

**I-8 — Memory is written only from deterministic computation.** Supplier reliability comes
from SQL aggregation over `delivery_history`. No LLM-authored text ever enters memory. Every
record carries `provenance: Literal["computed","operator"]`; `memory_write()` raises otherwise.
This closes slow memory-poisoning.

**I-9 — Healing actions are idempotent.** `idempotency_key = SHA256(deviation_id + sku_id + option_id)`.
Pub/Sub is at-least-once; a redelivered deviation must not cut a second order.

**I-10 — Ledger-first write ordering.** Write `INTENT` → execute → write `OUTCOME`. If the
process dies mid-flight the ledger shows an open intent. Never the reverse.

**I-11 — Degraded paths never auto-heal.** If the solver hits its 2000ms cap and falls back to
the heuristic, force `REQUIRE_HITL` regardless of amount. Cap inputs at 25 supply options
*before* solving so an inflated problem cannot be used to force degradation.

**I-12 — Guardrails run on ingress and on tool return paths.** Injection arrives in inbound
free-text *and* in tool results re-entering the orchestrator context. Sanitize both directions.

**I-13 — One model ID, structurally enforced.** Per §2.1: single constant, no default fallback,
no string construction, preflight resolution check, repo-wide grep assertion. A model ID that
does not exist must fail at deploy time.

**I-14 — The Conversational Analytics agent is read-only and cannot act.** It runs under its own
service account with `roles/bigquery.dataViewer` on an explicit allowlist of the six analytical
views plus the operational tables. It has **no** access to `audit_ledger`, `spend_transactions`,
or `healing_actions`, and no path to trigger a healing action. It answers questions; it never
decides. Configure the six views as *verified queries* so it resolves known business questions
against reviewed SQL rather than improvising.

**I-15 — The Data Engineering Agent is scoped to the raw and clean datasets only.** It writes to
`sentinel_raw` and `sentinel_clean` under its own service account. It has no access to the
governance datasets. Every pipeline change it makes is itself written to `audit_ledger` with
phase `PREPARE`. An agent that repairs pipelines must not be able to repair the evidence.

---

## 5. Architecture

```
   PREPARE — BigQuery DATA ENGINEERING AGENT
   dirty feeds (ASN drops, EDI 856, demand extracts) → sentinel_raw
   agent-built pipeline repairs + semantic metadata → sentinel_clean
   pipeline changes logged to audit_ledger (phase=PREPARE)
                     │
                     ▼
   BACKGROUND WATCH (Cloud Run job, cron)
   scans demand_signals + inventory_position → publishes deviations
                     │
                     ▼
        Pub/Sub: sentinel-deviations
                     │
                     ▼
   ┌─────────────────────────────────────────┐
   │ GUARDRAIL  (Model Armor → Gemma)         │  ingress + egress
   └─────────────────┬───────────────────────┘
                     ▼   STRICTLY SEQUENTIAL
   ┌─────────────────────────────────────────┐
   │ hygiene_agent    Pydantic contract       │
   └─────────────────┬───────────────────────┘
                     ▼
   ┌─────────────────────────────────────────┐
   │ triage_orchestrator                      │
   │   memory_read()  → supplier reliability  │
   │   sourcing_specialist (sub-agent)        │
   │   atp_query()    → exposed orders  [SQL] │
   │   solve_mitigation() → 3 scenarios [MILP]│
   └─────────────────┬───────────────────────┘
                     ▼
   ┌─────────────────────────────────────────┐
   │ OKF POLICY GOVERNOR                      │
   │  single-txn cap · 5-dim velocity caps    │
   │  VIP guard · degraded guard · kill switch│
   └────────┬────────────────────┬───────────┘
       AUTO_HEAL            REQUIRE_HITL
            ▼                    ▼
     heal_execute()      decision card (Streamlit → A2UI)
     idempotent          1-click signed approval
            └──────────┬─────────┘
                       ▼
   ┌─────────────────────────────────────────┐
   │ BIGQUERY — system of record              │
   │  audit_ledger (hash-chained)             │
   │  scenario_library · healing_actions      │
   │  spend_transactions · agent_registry     │
   │  supplier_reliability · analytics views  │
   │  agent_ops (ADK plugin stream)           │
   └───────────────────┬─────────────────────┘
                       ▼
   ┌─────────────────────────────────────────┐
   │ ASK — CONVERSATIONAL ANALYTICS AGENT     │
   │  knowledge sources: 6 views + ops tables │
   │  verified queries · glossary · READ-ONLY │
   │  returns text, tables, charts            │
   └─────────────────────────────────────────┘
   OpenTelemetry → Cloud Trace across all hops
```

Data and compute: `us-central1`. Inference: Vertex `global`. Per-agent service accounts, scoped IAM.

---

## 6. BigQuery data model — the centerpiece

Dataset `sentinel`. This is both the system of record and the analytical surface, so build the
views in §6.3; they are deliverables, not decoration.

### 6.1 Operational tables (seeded)
| Table | Contents |
|---|---|
| `sku_master` | 40 SKUs, category, unit value, criticality |
| `supplier_master` | 12 suppliers, tier, contract terms, cost center |
| `supply_options` | 25 options: mode ∈ {CONTRACT, SPOT, AIR_EXPEDITE, DC_REBALANCE}, unit price, MOQ, max qty, lead time, fixed fee |
| `inventory_position` | SKU × DC on-hand, in-transit, safety stock |
| `customer_orders` | 60 orders, promise date, qty, `tier` ∈ {TIER_1, STANDARD}, SLA penalty rate |
| `delivery_history` | 200 rows, 90 days — the *only* source for reliability metrics |
| `demand_signals` | daily forecast vs actual per SKU × DC — what the background watch scans |

**Dataset layout.** `sentinel_raw` (dirty landing) → `sentinel_clean` (DE Agent output) →
`sentinel` (governed system of record). The Data Engineering Agent touches the first two only.

Seed `sentinel_raw.asn_landing` with **three deliberate defect classes** so the DE Agent has
something real to repair: mixed date formats (`MM/DD/YYYY` vs ISO), unit-of-measure mismatch
(EA vs CASE on the same SKU), and duplicate ASN IDs with conflicting quantities.

### 6.2 Agentic tables (written at runtime)
| Table | Purpose |
|---|---|
| `deviations` | every deviation the watch detected: type, sku, dc, magnitude, detected_at |
| `scenario_library` | **every scenario the fleet ever generated**, with cost, days-to-coverage, SLA exposure, feasibility, solver status. This is the proactive impact map. |
| `healing_actions` | action taken, mode, qty, cost, `AUTO_HEALED` vs `HITL_APPROVED`, idempotency_key |
| `spend_transactions` | append-only; the five counter dimensions are columns here |
| `audit_ledger` | hash-chained; see §7 |
| `supplier_reliability` | computed memory: on-time rate 90d, lead-time drift, quote variance, sample size, provenance, computed_at |
| `agent_registry` | agent_id, version, input/output schema refs, RBAC scopes, owning department, status |
| `okf_policy` | **the policy itself as data**: rule_name, dimension, ceiling_usd, effective_from, owner, version. The governor reads its rules from here, not from hardcoded constants. Policy changes are versioned rows. |
| `agent_ops` | ADK interaction stream (BigQuery agent ops plugin) — tokens, latency, tool calls per hop |

### 6.3 Analytical views (`sql/views.sql`) — build all six
| View | Shows |
|---|---|
| `v_autonomy_rate` | daily count auto-healed vs escalated, and % autonomous |
| `v_exposure_avoided` | cumulative USD SLA exposure avoided by healing actions |
| `v_scenario_impact_map` | every scenario as (total_cost_usd, days_to_coverage, exposure) — a scatter of the decision space |
| `v_spend_velocity_24h` | rolling 24h spend per dimension against its ceiling |
| `v_governance_events` | policy rule trips over time, by rule name |
| `v_supplier_reliability_trend` | on-time rate by supplier over the 90-day window |
| `v_feed_quality` | rows landed vs rows repaired vs rows quarantined, by defect class and day |

Each view must return non-empty rows against seeded + demo data. **These six views are also the
knowledge sources for the Conversational Analytics agent (§8.11), so name columns in business
language — `exposure_avoided_usd`, not `exp_avd`.** The agent's answer quality is a direct
function of how readable these views are.

---

## 7. Contracts (`contracts/models.py`) — write this file first

```python
from pydantic import BaseModel, Field, condecimal
from typing import Literal, Optional
from datetime import datetime
from decimal import Decimal

Money = condecimal(max_digits=12, decimal_places=2)

class Deviation(BaseModel):
    deviation_id: str
    deviation_type: Literal["DEMAND_SPIKE", "PORT_DELAY", "ASN_DEVIATION", "SUPPLIER_SHORT"]
    sku_id: str
    dc_id: str
    magnitude_units: int = Field(ge=0)
    delay_days: int = Field(ge=0, le=365)
    source_system: str
    raw_note: str = Field(max_length=2000)   # UNTRUSTED — sanitize before any LLM sees it
    detected_at: datetime

class SupplyOption(BaseModel):
    option_id: str
    supplier_id: str
    mode: Literal["CONTRACT", "SPOT", "AIR_EXPEDITE", "DC_REBALANCE"]
    unit_price_usd: Money
    moq: int = Field(ge=0)
    max_qty: int = Field(ge=0)
    lead_time_days: int = Field(ge=0)
    fixed_fee_usd: Money = Decimal("0.00")

class Scenario(BaseModel):
    """Every field below is populated ONLY by solve_mitigation(). See I-1."""
    scenario_id: str
    label: Literal["STATUS_QUO", "AIR_EXPEDITE", "LINE_REBALANCE"]
    selected: list[dict]                 # [{option_id, qty, cost_usd}]
    total_cost_usd: Money
    sla_penalty_usd: Money
    total_exposure_usd: Money
    days_to_coverage: int
    feasible: bool
    solver_status: Literal["OPTIMAL", "FEASIBLE", "HEURISTIC_FALLBACK", "INFEASIBLE"]

class ScenarioSet(BaseModel):
    deviation_id: str
    scenarios: list[Scenario]
    recommended_scenario_id: str   # chosen by the SOLVER (min exposure), never by the LLM
    solve_ms: int
    degraded: bool                 # True → forces REQUIRE_HITL (I-11)
    result_sha256: str

class OrchestratorNarrative(BaseModel):
    """The ONLY structure an LLM may emit. Note the absence of numeric fields. See I-1."""
    narrative: str = Field(max_length=600)
    risk_summary: str = Field(max_length=300)

class OKFDecision(BaseModel):
    outcome: Literal["AUTO_HEAL", "REQUIRE_HITL", "BLOCKED"]
    triggered_rules: list[str]
    amount_usd: Money
    counters_snapshot: dict        # {dimension: {used, ceiling}}
    reservation_id: Optional[str]

class LedgerRecord(BaseModel):
    record_id: str
    deviation_id: str
    workflow_root_id: str
    phase: Literal["SENSE","SANITIZE","SCENARIO","SCORE","POLICY","INTENT","OUTCOME","APPROVAL"]
    payload_sha256: str
    prompt_digest: Optional[str]
    solver_result_sha256: Optional[str]
    okf_outcome: Optional[str]
    operator_sub: Optional[str]
    operator_jti: Optional[str]
    approval_signature: Optional[str]
    prev_record_hash: str
    record_hash: str
    created_at: datetime
```

---

## 8. Component specifications

### 8.1 `core/solver.py` — OR-Tools MILP
`pywraplp` with CBC. Variables per option *i*: `qty[i]` integer ≥ 0, `use[i]` boolean (MOQ
linkage via big-M).

Minimize `Σ(unit_price×qty) + Σ(fixed_fee×use) + sla_penalty`, where
`sla_penalty = penalty_rate_per_day × max(0, coverage_day − required_day)`, linearized with a
slack variable.

Constraints: `Σqty ≥ magnitude_units` · `qty[i] ≤ max_qty[i]×use[i]` · `qty[i] ≥ moq[i]×use[i]`
· mode gating per scenario.

**Three scenarios from one model**, re-solved with different mode sets:
`STATUS_QUO`={CONTRACT} · `AIR_EXPEDITE`={CONTRACT,SPOT,AIR_EXPEDITE} · `LINE_REBALANCE`={CONTRACT,DC_REBALANCE}

`SetTimeLimit(2000)` per solve · reject >25 options before solving · on timeout or infeasible
fall back to cheapest-first-meeting-lead-time, set `HEURISTIC_FALLBACK` and `degraded=True` ·
`recommended_scenario_id` = min `total_exposure_usd` among feasible, computed in Python ·
`result_sha256` over `json.dumps(canonical, sort_keys=True, separators=(",",":"))`.

**Write every scenario to `scenario_library`, including the ones not chosen.** That table is
the proactive impact map and it feeds `v_scenario_impact_map`.

### 8.2 `core/okf.py` — OKF Policy Governor
**Load all thresholds from the `okf_policy` table at startup, not from constants.** Policy is
data, versioned and auditable; the governor is the engine that applies it. Log the
`policy_version` into every ledger `POLICY` record so any decision can be replayed against the
rules that were in force at the time. This is the single most enterprise-credible detail in the
build — it is the difference between a script with magic numbers and a governed system.

Rules in order, most restrictive wins:
1. `SINGLE_TXN_CAP` — > $5,000 → REQUIRE_HITL
2. `VELOCITY_CAP` — any 24h dimensional counter would breach → REQUIRE_HITL
   *tenant $10,000 · supplier $7,500 · sku $6,000 · cost_center $8,000 · workflow_root $5,000*
3. `VIP_GUARD` — any exposed order is `TIER_1` → REQUIRE_HITL
4. `DEGRADED_SOLVER` — `scenario_set.degraded` → REQUIRE_HITL (I-11)
5. `KILL_SWITCH` — Firestore `fleet/control.paused` → BLOCKED

Counters read from `v_spend_velocity_24h`; the in-flight reservation lease is a Firestore
transaction (`reserve` / `commit` / `release`). Pending reservations count against ceilings.

### 8.3 `core/ledger.py`
```
append(record):
    prev = SELECT record_hash FROM audit_ledger ORDER BY created_at DESC LIMIT 1
    record.prev_record_hash = prev or "GENESIS"
    record.record_hash = sha256(prev_hash + canonical_json(payload)).hexdigest()
    streaming insert (append-only)
```
Ship `verify_chain() -> (ok: bool, broken_at: str|None)` as a CLI command.

### 8.4 `core/guardrail.py`
`sanitize(text, direction: Literal["ingress","egress"]) -> GuardrailResult`
Primary Model Armor; fallback a `gemma-3` classifier on Cloud Run returning
`{injection, pii, commercial_leak, reason}` plus a deterministic pre-filter for
instruction-override phrasing, role-switch attempts, embedded tool-call syntax, and price-list
exfiltration markers. Applied to `raw_note` **and** to every tool result string re-entering the
orchestrator (I-12).

### 8.5 `core/memory.py`
BigQuery `supplier_reliability`, refreshed by SQL aggregation over `delivery_history`.
`memory_write()` raises unless `provenance == "computed"` (I-8).

### 8.6 `core/heal.py`
Idempotent write to `healing_actions` keyed on `idempotency_key`. On existing key, return the
prior result with `duplicate: True`. **No real ERP integration** — it is a two-day rabbit hole
with zero visible difference.

### 8.7 `ui/app.py` — Streamlit, build before A2UI
Renders `ScenarioSet` + `OrchestratorNarrative`: three comparison cards, a risk-tolerance
slider, an Approve button, and the solver hash in the footer. Then `ui/a2ui_adapter.py` maps
the same JSON to exactly three A2UI components — `ScenarioCard`, `SensitivitySlider`,
`ApprovalButton`. No other component types. **Only attempt A2UI after Streamlit renders and
Gate 3 is green.**

### 8.8 CLI surfaces (required — these are how the system is inspected)
```
python -m scripts.watch_once              # run the background SENSE pass
python -m scripts.publish_deviation --id DEV-00X
python -m core.ledger verify              # chain verification
python -m scripts.adversarial_tests       # injection · smurfing · replay
python -m scripts.seed_registry           # publish agent manifests to BigQuery
```

### 8.9 `scripts/preflight.py` — model pinning gate (I-13)
Resolves `settings.MODEL_ID` against the Vertex AI publisher model endpoint and sends a 1-token
ping. Exits non-zero with the resolved-vs-requested IDs printed. First line of `deploy.sh`, and
a startup check in each Cloud Run container. Pair with `tests/test_model_pinning.py`, which
greps the repo for `gemini-[0-9]` and asserts a single match in `config/settings.py`.

### 8.10 PREPARE — BigQuery Data Engineering Agent
Enable Gemini in BigQuery, the Gemini Data Analytics API, and the Dataplex API. Grant the DE
Agent service account write on `sentinel_raw` and `sentinel_clean` only (I-15).

Use natural-language prompts to have the agent build **one** pipeline:
`sentinel_raw.asn_landing → sentinel_clean.asn_normalized`, handling the three seeded defect
classes (§6.1) — normalize dates to ISO, convert CASE to EA using `sku_master.units_per_case`,
and deduplicate ASN IDs keeping the latest `received_at` while writing losers to
`sentinel_clean.asn_quarantine`. Let the agent generate the semantic metadata; it feeds the CA
agent's context.

Write a `PREPARE` ledger record for each pipeline run: rows in, rows repaired, rows quarantined,
pipeline version. `v_feed_quality` reads from these.

**Scope discipline: one pipeline, one source table, three defect classes. Do not let the agent
generalize into a full ingestion framework.**

### 8.11 ASK — Conversational Analytics data agent
Create a data agent in BigQuery Agents whose knowledge sources are the six views from §6.3 plus
`sku_master`, `supplier_master`, and `customer_orders`. Read-only service account, view
allowlist, no governance tables (I-14).

Configure: a **glossary** defining `exposure`, `auto-heal`, `deviation`, `Tier-1`, and
`velocity ceiling` in supply chain terms; **instructions** stating that the agent reports on
decisions already made and never recommends or triggers actions; and **verified queries** for
the three seeded business questions below, so it answers from reviewed SQL rather than
improvising.

Seed and test exactly three questions:
1. "How many deviations did the fleet resolve autonomously this week, and what percentage is that?"
2. "Which suppliers drove the most SLA exposure over the last 90 days?"
3. "Show me the scenarios we generated but didn't select, by cost and days to coverage."

Each must return a chart. Publish the agent and record its ID in `agent_registry` alongside the
three ADK agents — a fourth catalog entry under a different owning department is exactly the
cross-department reuse this track asks for.

### 8.12 Agent ops and trace propagation
Install the BigQuery agent ops plugin for ADK and stream agent interactions to the `agent_ops`
table. One import, and it strengthens the observability story alongside OpenTelemetry.

**Trace context does not survive Pub/Sub automatically.** Inject `traceparent` into Pub/Sub
message attributes on publish and extract it in the push handler before starting the span.
Without this, your Cloud Trace view is three disconnected trees instead of one end-to-end
reasoning chain — and the end-to-end chain is precisely what the Observability pillar is scored
on. Verify one deviation produces a single connected trace before Gate 4 closes.

### 8.13 Seeded deviations
Seed exactly four, and make sure each lands on a different policy path:
| ID | Scenario | Expected outcome |
|---|---|---|
| `DEV-001` | $1,200 DC rebalance, standard orders | `AUTO_HEAL`, no human |
| `DEV-002` | `raw_note` carries a prompt-injection payload | blocked at guardrail, schema rejected |
| `DEV-003` | 3 × $4,900 against one supplier | 2nd trips `VELOCITY_CAP` on the supplier dimension |
| `DEV-004` | $12,400 Tier-1 air expedite | trips `SINGLE_TXN_CAP` + `VIP_GUARD` → signed approval |

---

## 9. README requirements (judged)
Prerequisites · `gcloud` auth · `.env` setup · `./deploy.sh` · seeding · running the watch ·
publishing a deviation · opening the UI · `verify_chain()` · adversarial tests · the
BigQuery-vs-Firestore split from I-4 explained honestly. **Run it yourself in a clean Cloud
Shell before freeze.**

---

## 10. ANTI-GOALS — do not build these
❌ A real ERP integration ❌ custom mTLS proxy or hand-rolled gateway (Cloud Run + service
identity + IAM is sufficient) ❌ Next.js ❌ MEIO cron beyond the simple watch pass ❌ BOM
traversal beyond 2 tiers ❌ a fourth agent ❌ more than 3 A2UI component types ❌ any second
model ID ❌ auth beyond the single approval path ❌ retry frameworks, custom orchestration
engines, plugin architectures ❌ **a hand-built NL-to-SQL layer** — the Conversational Analytics
agent is the product, do not reimplement it ❌ **a generalized ingestion framework** — the DE
Agent gets one pipeline and three defect classes ❌ any default fallback value for `MODEL_ID`.

If a task is not traceable to a gate in §3, it is out of scope.

---

## 11. Handoff prompts (one per session)

**Gate 1 —** "Read SENTINEL_BUILD_BRIEF.md. Implement `contracts/models.py` (§7) in full, then
`sql/ddl.sql` and `sql/seed.sql` per §6.1 and §8.9, then `core/solver.py` per §8.1. Stop and
show me three scored scenarios plus the hash."

**Gate 2 —** "Implement `core/okf.py`, `core/ledger.py`, `core/heal.py` per §8.2–8.3 and 8.6,
honoring invariants I-3 through I-11. Then `scripts/adversarial_tests.py` covering DEV-002,
DEV-003, and a replayed DEV-001. Stop and show each rule trip."

**Gate 3 —** "Implement the three ADK agents per §5 using only `gemini-3.7-flash` (Vertex `global` endpoint). Enforce I-1:
the orchestrator's response schema is `OrchestratorNarrative` and must contain no numeric
fields. Wire the guardrail ingress and egress. Deploy to Cloud Run with per-agent service
accounts and a Pub/Sub push subscription. Stop when one deviation flows end-to-end into a
verifiable ledger chain."

**Gate 4 —** "Implement `sql/views.sql` (§6.3), `ui/app.py` (§8.7), the signed approval path
(I-7), OpenTelemetry spans, and the agent ops plugin (§8.12). Verify all seven views return
rows. Only attempt the A2UI adapter once Streamlit renders correctly."

**Gate 5 —** "Configure the BigQuery Data Engineering Agent per §8.10 — one pipeline, three
defect classes, scoped per I-15 — and the Conversational Analytics data agent per §8.11, scoped
per I-14 with glossary, instructions, and three verified queries. Register the CA agent in
`agent_registry`. Do not write custom code for either; these are configuration."

---

*Every threshold, ceiling, and rule name must appear identically in the code, the BigQuery
tables, and the UI. Consistency across those three surfaces is what reads as production-minded.*
