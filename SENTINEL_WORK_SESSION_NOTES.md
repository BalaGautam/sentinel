# SENTINEL — Work Session Notes

Live document. Keep it open in a tab. Update it as you go — this is what you'll be reading at
2am when you've forgotten why you chose something.

**Deadline:** Mon Aug 31, 8:00pm ET · **Draft submission target:** Mon 09:00 ET · **Freeze:** Mon 10:00 ET

---

## 1. Gate tracker

| Gate | When | Criterion | Status |
|---|---|---|---|
| **0** | ~~Sat~~ **DONE Sun** | Model + region locked: `gemini-3.7-flash` / global, data `us-central1` | ☑ |
| **1** | Sat night | `core.solver` prints 3 scored scenarios + stable SHA-256 | ☐ |
| **2** | Sun AM | Smurfing, replay, injection tests pass from CLI | ☐ |
| **3** | Sun PM | One deviation end-to-end → verifiable ledger chain; agents in Agent Registry | ☐ |
| **4** | Sun eve | All 4 deviations run clean, twice in a row | ☐ |
| **5** | Mon AM | CA agent answers 3 questions with charts; DE Agent repairs one feed | ☐ |

**Gate 1 is the one that matters.** If the solver isn't returning scored scenarios with a
stable hash before you sleep Saturday, Sunday becomes debugging instead of assembly. Protect it.

---

## 2. Decision log

Fill this in as you go. It becomes the "Insights" section of the README for free.

| # | Decision | Why | When |
|---|---|---|---|
| 1 | Fortified Enterprise Fleet track | Track's own example is an Enterprise Supply Chain Orchestrator | Pre-build |
| 2 | `gemini-3.7-flash` on Vertex `global` | 3.5 Flash unavailable in us-east4/us-central1. 3.7 clears the floor with room; residency stated honestly instead | Gate 0, Sun |
| 3 | GEAP Agent Registry, not hand-rolled | Judges said outright they prefer platform components | Pre-build |
| 4 | Firestore for spend lease, BigQuery for everything else | BigQuery has no row-level transaction | Pre-build |
| 5 | | | |
| 6 | | | |

---

## 3. Environment

Fill in at Gate 0, then never think about it again.

```
PROJECT_ID       = ________________
GCP_REGION               = us-central1        ← data + compute
VERTEX_INFERENCE_LOCATION = global             ← inference ONLY. Do not merge with GCP_REGION.
MODEL_ID                 = gemini-3.7-flash    ← the only model ID in the repo
BQ_DATASETS      = sentinel_raw / sentinel_clean / sentinel
PUBSUB_TOPIC     = sentinel-deviations
CLOUD_RUN_SVCS   = sentinel-orchestrator / sentinel-watch / sentinel-ui / sentinel-guardrail
SERVICE_ACCOUNTS = sa-hygiene / sa-sourcing / sa-orchestrator / sa-de-agent / sa-ca-agent
```

**APIs to enable at Gate 0:** aiplatform, run, pubsub, bigquery, firestore, secretmanager,
geminidataanalytics, dataplex, dataform, cloudtrace.

---

## 4. Command palette

```bash
# Gate 0
gcloud config set project $PROJECT_ID
python -m scripts.preflight                      # fails fast on a bad model ID
pytest tests/test_model_pinning.py               # fails on any invented gemini-* string

# Build / deploy
./deploy.sh
bq query --use_legacy_sql=false < sql/ddl.sql
bq query --use_legacy_sql=false < sql/seed.sql
bq query --use_legacy_sql=false < sql/views.sql

# Run
python -m core.solver --deviation DEV-001        # GATE 1
python -m scripts.adversarial_tests              # GATE 2
python -m scripts.watch_once
python -m scripts.publish_deviation --id DEV-004
python -m core.ledger verify                     # GATE 3
python -m scripts.seed_registry

# Teardown after recording
gcloud run services update <svc> --min-instances=0
```

---

## 5. Blocker triage — decide now, not at 2am

| If this breaks | Do this | Do NOT |
|---|---|---|
| Model ID unresolved | Check `MODEL_ID` is exactly `gemini-3.7-flash` and location is `global`. **There is no Gemini 3.5 Pro** — if an agent wrote one, that's the bug | Invent a variant or add a fallback string |
| Inference fails after region change | Confirm the Vertex client uses `global`, not `us-central1`. These are separate config values | Collapse them into one variable |
| Memory Bank won't provision | Fall back to BigQuery `supplier_reliability` | Call it a Memory Bank in the README. Persistence is not memory |
| Model Armor blocked/slow | Fall back to the Gemma classifier on Cloud Run (this is also the bonus point) | Skip guardrails entirely |
| Agent Registry publishing fails | Keep trying — judges named this specifically | Drop it. This is the one fallback that costs real points |
| OR-Tools infeasible | Check MOQ vs `max_qty` in seed data first — it's almost always seed data, not the model | Loosen constraints to force a solution |
| A2UI not rendering by Sun midnight | Ship Streamlit, strip all A2UI references from README and video | Ship a broken component |
| DE Agent not working by Mon 08:00 | Seed `sentinel_clean` directly, remove the PREPARE claim | Leave the claim in |
| CA agent gives bad answers | Add verified queries for the three seeded questions; rename view columns to business language | Hand-roll NL-to-SQL |
| Cloud Run 403s on Pub/Sub push | Push SA needs `roles/run.invoker` — this is the usual one | Make the service public |
| Running out of Sunday | Cut in this order: A2UI → DE Agent → CA agent → Memory Bank → third agent. **Never cut: solver, OKF, ledger, Agent Registry** | Cut the ledger to save time |

---

## 6. Claim discipline

Judges confirmed they run the code and check whether it does what you claim. Before freeze,
grep the README for each of these and confirm it's true of what shipped:

- [ ] "tamper-evident" — **not** "immutable"
- [ ] Residency stated as §2.6 verbatim; the phrase "data sovereignty" appears nowhere
- [ ] Approval signing method matches what you built (OIDC vs HMAC)
- [ ] "Memory Bank" only if you actually used Memory Bank
- [ ] "Model Armor" only if you actually used Model Armor
- [ ] Every component in the architecture diagram exists in the repo
- [ ] Every command in the spin-up instructions has been run in a clean Cloud Shell
- [ ] No claim about A2UI / DE Agent / CA agent that got cut

**One false claim poisons everything else they read.** The cost of an honest "known limitations"
section is zero; the cost of an unsupported claim is your credibility on all the true ones.

---

## 7. Demo prep checklist (Sunday evening)

- [ ] All four deviations seeded and rehearsed end-to-end, twice
- [ ] Counters reset before recording (a pre-tripped cap ruins DEV-003)
- [ ] Terminal font size up; browser zoom up
- [ ] Cloud Run console tab open; GEAP Agent Registry tab open; BigQuery tab open
- [ ] Notifications off, second monitor cleared
- [ ] Cold open rehearsed — the first 30 seconds decide how they read the rest
- [ ] **You are narrating, not an AI voice**
- [ ] Timed run-through under 3:45

---

## 8. Parking lot

Things worth doing that are explicitly not in scope this weekend. Write them here instead of
building them — they become the README's "what we'd build next".

- Real ERP connector
- Multi-tier BOM traversal
- Weekly MEIO safety-stock recalculation
- Agent eval sets / regression harness
- Multi-currency FX from a live rate source
- Cross-department registry consumer (Finance AP workflow)
