"""Project Sentinel — Autonomous Fortified Supply Chain Orchestrator UI (§8.7).

Interactive Streamlit Application featuring:
1. Deviation Triage & Multi-Agent Workflow Execution
2. Orchestrator Qualitative Synthesis (I-1: strictly non-numeric)
3. Three Scenario Comparison Cards (§8.7: STATUS_QUO, AIR_EXPEDITE, LINE_REBALANCE)
4. Interactive Sensitivity & Risk-Tolerance Slider (§8.7)
5. OKF Policy Governor & 24h Dimensional Velocity Gauges (I-3, I-4)
6. Signed HITL Operator Approval (I-7)
7. Seven Analytical Views Dashboard (§6.3)
8. Tamper-Evident Ledger Integrity & Solver Hash Footer (I-2, I-6)
"""

import os
import sys
import json
from decimal import Decimal
from datetime import datetime, timezone
import pandas as pd
import streamlit as st
import altair as alt

from google.cloud import bigquery
from google.cloud import firestore

# Ensure root directory in sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from config import settings
from contracts.models import Deviation, Scenario, ScenarioSet, OrchestratorNarrative
from core.solver import load_deviation_from_bq, solve_mitigation, _get_credentials
from core.okf import OKFGovernor
from core.heal import execute_healing_action
from core.ledger import append_ledger_record, verify_chain
from core.approval import create_signed_approval_envelope, execute_signed_approval, verify_approval_signature
from agents.pipeline import run_sentinel_workflow


# Page Configuration
st.set_page_config(
    page_title="Project Sentinel — Fortified Autonomous Fleet",
    page_icon="🛡️",
    layout="wide",
    initial_sidebar_state="expanded",
)

# Custom Styling
st.markdown(
    """
    <style>
    .block-container {
        padding-top: 2.8rem;
        padding-bottom: 2rem;
        padding-left: 2.5rem;
        padding-right: 2.5rem;
    }
    .main-header {
        background-color: transparent;
        font-size: 2.3rem;
        font-weight: 700;
        color: #1E293B;
        margin-bottom: 0.3rem;
        padding-top: 0.2rem;
        line-height: 1.3;
        overflow: visible;
    }
    .sub-header {
        background-color: transparent;
        font-size: 1.05rem;
        color: #64748B;
        margin-bottom: 1.2rem;
    }
    .scenario-card-recommended {
        background-color: #F0FDF4;
        color: #1E293B;
        border: 2px solid #22C55E;
        border-radius: 12px;
        padding: 16px;
        margin-bottom: 12px;
        min-height: 100px;
        display: flex;
        flex-direction: column;
        justify-content: center;
    }
    .scenario-card-standard {
        background-color: #FFFFFF;
        color: #1E293B;
        border: 1px solid #CBD5E1;
        border-radius: 12px;
        padding: 16px;
        margin-bottom: 12px;
        min-height: 100px;
        display: flex;
        flex-direction: column;
        justify-content: center;
    }
    .status-badge-auto {
        background-color: #DCFCE7;
        color: #15803D;
        padding: 4px 10px;
        border-radius: 6px;
        font-weight: 600;
        font-size: 0.9rem;
    }
    .status-badge-hitl {
        background-color: #FEF3C7;
        color: #B45309;
        padding: 4px 10px;
        border-radius: 6px;
        font-weight: 600;
        font-size: 0.9rem;
    }
    .status-badge-blocked {
        background-color: #FEE2E2;
        color: #B91C1C;
        padding: 4px 10px;
        border-radius: 6px;
        font-weight: 600;
        font-size: 0.9rem;
    }
    .hash-code {
        font-family: 'Courier New', Courier, monospace;
        background-color: #0F172A;
        color: #38BDF8;
        padding: 4px 8px;
        border-radius: 4px;
        font-size: 0.82rem;
    }
    div[role="radiogroup"] label {
        background-color: #F1F5F9;
        color: #1E293B;
        border: 1px solid #E2E8F0;
        border-radius: 8px;
        padding: 8px 12px;
        margin-bottom: 4px;
        width: 100%;
    }
    </style>
    """,
    unsafe_allow_html=True,
)


@st.cache_resource
def get_clients():
    creds = _get_credentials()
    bq = bigquery.Client(project=settings.PROJECT_ID, location=settings.GCP_REGION, credentials=creds)
    try:
        fs = firestore.Client(project=settings.PROJECT_ID, credentials=creds)
    except Exception:
        fs = None
    return bq, fs


bq_client, fs_client = get_clients()


# --- SIDEBAR NAVIGATION & FLEET HEALTH ---
with st.sidebar:
    # 1. Enriched Wordmark Lockup
    st.markdown(
        """
        <div style="text-align: center; margin-bottom: 0.8rem; padding-top: 0.4rem;">
            <div style="font-size: 2.7rem; font-weight: 900; letter-spacing: 0.16em; color: #0F172A; text-transform: uppercase; line-height: 1.05;">SENTINEL</div>
            <div style="font-size: 1.05rem; font-weight: 600; color: #475569; margin-top: 6px; letter-spacing: 0.04em;">Fortified Autonomous Fleet</div>
        </div>
        """,
        unsafe_allow_html=True,
    )
    st.divider()

    # 2. Navigation (directly beneath wordmark, above the fold)
    app_mode = st.radio(
        "Navigation",
        ["Deviation Triage & Remediation", "Executive Analytics Dashboard", "Audit Ledger Explorer"],
        index=0,
    )
    st.divider()

    # 3. Ledger Integrity
    st.markdown("**Audit Ledger Integrity**")
    try:
        ok, broken_at, rec_count = verify_chain(bq_client, settings.BQ_DATASET)
        if ok:
            st.success(f"🛡️ Verified ({rec_count} records)")
            st.caption("Status: 100% Tamper-Evident")
        else:
            st.error(f"⚠️ Check Failed: {broken_at}")
    except Exception:
        st.info("Ledger initializing...")
    st.divider()

    # 4. Environment Configuration
    st.markdown("**Environment Configuration**")
    st.markdown(f"**Model ID:** `{settings.MODEL_ID}`")
    st.markdown(f"**Inference:** `{settings.VERTEX_INFERENCE_LOCATION}`")
    st.markdown(f"**Data & Compute:** `{settings.GCP_REGION}`")
    st.markdown(f"**Project:** `{settings.PROJECT_ID}`")
    st.markdown(f"**Dataset:** `{settings.BQ_DATASET}`")
    st.divider()

    # 5. Reset Demo State (at the bottom)
    if st.button("🔄 Reset Demo State", use_container_width=True, help="Reset dynamic tables, Firestore leases, and OKF spend counters"):
        with st.spinner("Resetting demo state..."):
            from scripts.reset_demo import reset_demo_state
            reset_demo_state(bq_client, fs_client)
            st.session_state.clear()
            st.success("✅ Demo state reset complete. All spend counters at 0%.")
            st.rerun()


# =============================================================================
# VIEW 1: DEVIATION TRIAGE & REMEDIATION
# =============================================================================
if app_mode == "Deviation Triage & Remediation":
    st.markdown('<div class="main-header">🛡️ Autonomous Mitigation & Triage Cockpit</div>', unsafe_allow_html=True)
    st.markdown(
        '<div class="sub-header">Deterministic MILP Optimization with Tamper-Evident Hash-Chained Verification</div>',
        unsafe_allow_html=True,
    )

    col1, col2, col3 = st.columns([2, 1, 1])

    with col1:
        # Descriptive input-only labels without leaking cost, mode, or policy outcome (§8.7, Item 6)
        deviation_options = {
            "DEV-001 — Demand spike, SKU-003, DC-EAST, 100 units": "DEV-001",
            "DEV-002 — Port delay, SKU-001, DC-CENTRAL, 200 units": "DEV-002",
            "DEV-003a — Supplier shortage batch 1, SKU-001, DC-WEST, 240 units": "DEV-003a",
            "DEV-003b — Supplier shortage batch 2, SKU-001, DC-WEST, 240 units": "DEV-003b",
            "DEV-003c — Supplier shortage batch 3, SKU-001, DC-WEST, 240 units": "DEV-003c",
            "DEV-004 — Demand spike, SKU-002, DC-EAST, 500 units": "DEV-004",
        }
        selected_label = st.selectbox("Select Inbound Supply Chain Deviation:", list(deviation_options.keys()), index=0)
        dev_id = deviation_options[selected_label]

    with col2:
        st.write("")
        st.write("")
        run_button = st.button("🚀 Run Fleet Triage", type="primary", use_container_width=True)

    with col3:
        st.write("")
        st.write("")
        refresh_button = st.button("🔄 Refresh Data", use_container_width=True)

    # Load deviation details safely
    try:
        dev_obj, sup_options, penalty_rate = load_deviation_from_bq(bq_client, settings.BQ_DATASET, dev_id)
    except Exception as e:
        st.warning(f"Could not load deviation details for {dev_id}: {e}")
        st.stop()

    # Inbound Deviation Dossier
    with st.expander(f"📋 Inbound Deviation Dossier: {dev_id} ({dev_obj.deviation_type})", expanded=True):
        dcol1, dcol2, dcol3, dcol4, dcol5 = st.columns(5)
        dcol1.markdown(
            f"""
            <div style="display: flex; flex-direction: column;">
                <span style="font-size: 0.78rem; font-weight: 600; text-transform: uppercase; color: #64748B; margin-bottom: 2px;">SKU ID</span>
                <span style="font-size: 1.15rem; font-weight: 700; color: #1E293B; word-break: break-word;">{dev_obj.sku_id}</span>
            </div>
            """,
            unsafe_allow_html=True,
        )
        dcol2.markdown(
            f"""
            <div style="display: flex; flex-direction: column;">
                <span style="font-size: 0.78rem; font-weight: 600; text-transform: uppercase; color: #64748B; margin-bottom: 2px;">Target DC</span>
                <span style="font-size: 1.15rem; font-weight: 700; color: #1E293B; word-break: break-word;">{dev_obj.dc_id}</span>
            </div>
            """,
            unsafe_allow_html=True,
        )
        dcol3.markdown(
            f"""
            <div style="display: flex; flex-direction: column;">
                <span style="font-size: 0.78rem; font-weight: 600; text-transform: uppercase; color: #64748B; margin-bottom: 2px;">Magnitude</span>
                <span style="font-size: 1.15rem; font-weight: 700; color: #1E293B; word-break: break-word;">{dev_obj.magnitude_units} units</span>
            </div>
            """,
            unsafe_allow_html=True,
        )
        dcol4.markdown(
            f"""
            <div style="display: flex; flex-direction: column;">
                <span style="font-size: 0.78rem; font-weight: 600; text-transform: uppercase; color: #64748B; margin-bottom: 2px;">Delay Risk</span>
                <span style="font-size: 1.15rem; font-weight: 700; color: #1E293B; word-break: break-word;">{dev_obj.delay_days} days</span>
            </div>
            """,
            unsafe_allow_html=True,
        )
        dcol5.markdown(
            f"""
            <div style="display: flex; flex-direction: column;">
                <span style="font-size: 0.78rem; font-weight: 600; text-transform: uppercase; color: #64748B; margin-bottom: 2px;">Source System</span>
                <span style="font-size: 1.15rem; font-weight: 700; color: #1E293B; word-break: break-word;">{dev_obj.source_system}</span>
            </div>
            """,
            unsafe_allow_html=True,
        )

        st.markdown("**Untrusted Inbound Note:**")
        st.code(dev_obj.raw_note)

    # Execute workflow when triggered
    if run_button:
        with st.spinner("Executing Autonomous Fleet Pipeline (Hygiene -> Sourcing -> MILP Solver -> OKF Governor)..."):
            payload = {
                "deviation_id": dev_obj.deviation_id,
                "deviation_type": dev_obj.deviation_type,
                "sku_id": dev_obj.sku_id,
                "dc_id": dev_obj.dc_id,
                "magnitude_units": dev_obj.magnitude_units,
                "delay_days": dev_obj.delay_days,
                "source_system": dev_obj.source_system,
                "raw_note": dev_obj.raw_note,
                "detected_at": dev_obj.detected_at.isoformat(),
            }
            res = run_sentinel_workflow(payload, tenant="SENTINEL_CORP", cost_center="CC_LOGISTICS")
            st.session_state[f"workflow_{dev_id}"] = res
            st.session_state["current_dev_id"] = dev_id

    wf_res = st.session_state.get(f"workflow_{dev_id}")
    if not wf_res:
        st.info("💡 Select an inbound deviation and click **'🚀 Run Fleet Triage'** to execute the multi-agent pipeline.")
        st.stop()

    if wf_res.get("status") == "BLOCKED_BY_GUARDRAIL":
        st.error(f"🛑 **Security Guardrail Tripped**: Inbound payload rejected by security filter. Reason: {wf_res.get('reason')}")
        st.stop()

    # --- SECTION: ORCHESTRATOR NARRATIVE ---
    st.markdown("### 🤖 Orchestrator Qualitative Triage")
    ncol1, ncol2 = st.columns(2)
    with ncol1:
        st.info(f"**Operational Narrative:**\n\n{wf_res.get('narrative', 'N/A')}")
    with ncol2:
        st.warning(f"**Risk Assessment:**\n\n{wf_res.get('risk_summary', 'N/A')}")

    # Solve MILP scenarios using exact fixed signature (Item 2)
    try:
        scenario_set = solve_mitigation(
            deviation=dev_obj,
            supply_options=sup_options,
            penalty_rate_per_day=penalty_rate,
        )
        scenarios = scenario_set.scenarios
        rec_id = scenario_set.recommended_scenario_id
        solver_sha256 = scenario_set.result_sha256
    except Exception as e:
        st.warning(f"Solver evaluation note: {e}")
        scenarios = []
        rec_id = None
        solver_sha256 = wf_res.get("solver_sha256", "N/A")

    # --- SECTION: SENSITIVITY & RISK-TOLERANCE SLIDER ---
    st.markdown("### ⚖️ Mitigation Sensitivity & Trade-Off Control")
    slider_col1, slider_col2 = st.columns([3, 1])
    with slider_col1:
        sensitivity = st.slider(
            "Priority Balance: Cost Minimization vs Delivery Speed / SLA Protection",
            min_value=0.0,
            max_value=1.0,
            value=0.5,
            step=0.05,
            help="Adjust weight between Direct Procurement Cost (0.0) and Immediate SLA Protection (1.0).",
        )
    with slider_col2:
        if sensitivity < 0.35:
            st.metric("Strategy", "Cost Lean", "Contract Focused")
        elif sensitivity > 0.65:
            st.metric("Strategy", "SLA Defensive", "Max Speed")
        else:
            st.metric("Strategy", "Balanced", "Optimal Trade-Off")

    # --- SECTION: THREE SCENARIO COMPARISON CARDS ---
    st.markdown("### 📊 Scored Mitigation Scenarios")
    if scenarios:
        scol1, scol2, scol3 = st.columns(3)
        scenario_cols = [scol1, scol2, scol3]

        for idx, sc in enumerate(scenarios):
            is_recommended = (sc.scenario_id == rec_id)
            with scenario_cols[idx]:
                card_class = "scenario-card-recommended" if is_recommended else "scenario-card-standard"
                border_badge = "⭐ <strong>RECOMMENDED BY SOLVER</strong>" if is_recommended else f"<strong>Option {idx+1}</strong>"

                st.markdown(
                    f"""
                    <div class="{card_class}">
                        <h4>{border_badge}</h4>
                        <h3>{sc.label}</h3>
                    </div>
                    """,
                    unsafe_allow_html=True,
                )

                st.metric("Total Cost", f"${sc.total_cost_usd:,.2f}" if sc.total_cost_usd is not None else "N/A")
                st.metric("Days to Coverage", f"{sc.days_to_coverage} days" if sc.days_to_coverage is not None else "N/A")
                st.metric("SLA Penalty Exposure", f"${sc.sla_penalty_usd:,.2f}" if sc.sla_penalty_usd is not None else "$0.00")
                st.metric("Total Net Exposure", f"${sc.total_exposure_usd:,.2f}" if sc.total_exposure_usd is not None else "N/A")

                st.caption(f"Solver Status: `{sc.solver_status}` | Feasible: `{sc.feasible}`")

                if sc.selected:
                    st.markdown("**Selected Options Allocation:**")
                    for opt in sc.selected:
                        st.markdown(f"- `{opt['option_id']}`: **{opt['qty']} units** (${opt['cost_usd']})")

    # --- SECTION: OKF POLICY GOVERNOR OUTCOME & VELOCITY GAUGES ---
    st.markdown("### 🏛️ Policy Governor Decision & Spend Velocity")

    gcol1, gcol2 = st.columns([1.2, 2])
    with gcol1:
        okf_outcome = wf_res.get("okf_outcome", "UNKNOWN")
        if okf_outcome == "AUTO_HEAL":
            st.markdown('<h4>Policy Verdict: <span class="status-badge-auto">AUTO-HEAL APPROVED</span></h4>', unsafe_allow_html=True)
            st.success("✅ Cleared all multi-dimensional velocity caps and VIP guards. Remediated autonomously.")
        elif okf_outcome == "REQUIRE_HITL":
            st.markdown('<h4>Policy Verdict: <span class="status-badge-hitl">REQUIRE HITL APPROVAL</span></h4>', unsafe_allow_html=True)
            st.warning("⚠️ Escalated to Human-in-the-Loop Operator due to policy rule breach.")
            triggered = wf_res.get("triggered_rules", [])
            st.markdown(f"**Triggered Rules:** `{', '.join(triggered)}`")
        else:
            st.markdown('<h4>Policy Verdict: <span class="status-badge-blocked">BLOCKED</span></h4>', unsafe_allow_html=True)
            st.error("🛑 Fleet execution blocked by policy or security constraint.")

    with gcol2:
        st.markdown("**Rolling 24-Hour Spend Velocity vs Budget Ceilings:**")
        try:
            v_df = bq_client.query(
                f"SELECT dimension, dimension_key, spent_24h_usd, ceiling_usd, remaining_budget_usd, utilization_pct FROM `{settings.PROJECT_ID}.{settings.BQ_DATASET}.v_spend_velocity_24h` LIMIT 10"
            ).to_dataframe()
            if not v_df.empty:
                for _, r in v_df.iterrows():
                    u_pct = min(100.0, float(r["utilization_pct"]))
                    st.write(f"**{r['dimension']}** (`{r['dimension_key']}`): ${float(r['spent_24h_usd']):,.2f} / ${float(r['ceiling_usd']):,.2f} ({u_pct:.1f}%)")
                    st.progress(u_pct / 100.0)
            else:
                st.info("No active 24h spend recorded.")
        except Exception:
            st.info("Spend velocity counters currently clear.")

    # --- SECTION: SIGNED HITL APPROVAL ---
    if okf_outcome == "REQUIRE_HITL" and scenarios:
        st.divider()
        st.markdown("### ✍️ Human-in-the-Loop Operator Signed Approval")
        st.markdown("This deviation requires an authorized operator signature before funds can be committed and purchase orders cut.")

        appr_col1, appr_col2 = st.columns([1.5, 1])
        with appr_col1:
            op_name = st.text_input("Operator Name", value="Bala Gautam")
            op_email = st.text_input("Operator Email", value="bala.gautam@sentinel-corp.internal")
            op_role = st.selectbox("Authorized Role", ["Director, Supply Chain", "VP Supply Chain Operations", "Chief Procurement Officer", "Logistics Director"])
            op_sub = "usr-op-7842"

        with appr_col2:
            rec_scenario_obj = next((s for s in scenarios if s.scenario_id == rec_id), scenarios[0])
            st.markdown(f"**Action to Approve:** `{rec_scenario_obj.label}`")
            st.markdown(f"**Total Capital Commitment:** ${rec_scenario_obj.total_cost_usd:,.2f}")
            st.markdown(f"**Target SKU:** `{dev_obj.sku_id}`")
            st.markdown(f"**Supplier:** `SUP-09`")

            approve_btn = st.button("🔏 Sign & Authorize Mitigation Plan", type="primary", use_container_width=True)

        if approve_btn:
            with st.spinner("Generating signature, writing APPROVAL record to audit ledger, and executing healing..."):
                try:
                    envelope = create_signed_approval_envelope(
                        deviation_id=dev_obj.deviation_id,
                        workflow_root_id=f"WF-{dev_obj.deviation_id}",
                        selected_scenario=rec_scenario_obj,
                        sku_id=dev_obj.sku_id,
                        supplier_id="SUP-09",
                        operator_sub=op_sub,
                        operator_email=op_email,
                        operator_role=op_role,
                    )
                    approval_result = execute_signed_approval(
                        bq_client=bq_client,
                        dataset_id=settings.BQ_DATASET,
                        deviation=dev_obj,
                        selected_scenario=rec_scenario_obj,
                        approval_envelope=envelope,
                        fs_client=fs_client,
                        tenant="SENTINEL_CORP",
                        cost_center="CC_LOGISTICS",
                    )
                    st.success("🎉 **Mitigation Plan Signed and Executed!**")
                    st.json(approval_result)
                except Exception as ex:
                    st.error(f"Approval execution error: {ex}")

    # --- SECTION: FOOTER WITH SOLVER SHA-256 ---
    st.divider()
    st.markdown(
        f"""
        <div style="text-align: center; color: #64748B; font-size: 0.88rem;">
            Deterministic Solver Digest: <span class="hash-code">{solver_sha256}</span>
            <br>
            Audit Ledger: <b>Append-Only Tamper-Evident Hash Chain</b> · Model: <b>{settings.MODEL_ID} ({settings.VERTEX_INFERENCE_LOCATION})</b>
        </div>
        """,
        unsafe_allow_html=True,
    )


# =============================================================================
# VIEW 2: EXECUTIVE ANALYTICS DASHBOARD
# =============================================================================
elif app_mode == "Executive Analytics Dashboard":
    st.markdown('<div class="main-header">📈 Executive Analytics & Intelligence</div>', unsafe_allow_html=True)
    st.markdown(
        '<div class="sub-header">Live Analytical Views with Business-Language Metrics</div>',
        unsafe_allow_html=True,
    )

    tab1, tab2, tab3, tab4, tab5, tab6, tab7 = st.tabs([
        "1. Autonomy Rate",
        "2. Exposure Avoided",
        "3. Scenario Impact Map",
        "4. Spend Velocity 24h",
        "5. Governance Events",
        "6. Supplier Reliability",
        "7. Feed Quality",
    ])

    with tab1:
        st.markdown("### `v_autonomy_rate`: Daily Autonomy Percentage")
        try:
            df1 = bq_client.query(f"SELECT * FROM `{settings.PROJECT_ID}.{settings.BQ_DATASET}.v_autonomy_rate`").to_dataframe()
            if not df1.empty:
                m1, m2, m3 = st.columns(3)
                latest_auto = df1.iloc[0]["autonomy_rate_pct"]
                m1.metric("Autonomy Rate", f"{latest_auto:.1f}%")
                m2.metric("Auto-Healed Decisions", int(df1["auto_healed_count"].sum()))
                m3.metric("Escalated Decisions", int(df1["escalated_count"].sum()))
                st.dataframe(df1, use_container_width=True)
            else:
                st.info("No autonomy data recorded yet.")
        except Exception as e:
            st.info(f"Autonomy data view currently empty: {e}")

    with tab2:
        st.markdown("### `v_exposure_avoided`: Cumulative USD SLA Exposure Avoided")
        try:
            df2 = bq_client.query(f"SELECT * FROM `{settings.PROJECT_ID}.{settings.BQ_DATASET}.v_exposure_avoided`").to_dataframe()
            if not df2.empty:
                total_avoided = df2["exposure_avoided_usd"].sum()
                st.metric("Total Cumulative SLA Exposure Avoided", f"${total_avoided:,.2f}")
                st.dataframe(df2, use_container_width=True)
            else:
                st.info("No exposure avoided records yet.")
        except Exception as e:
            st.info(f"Exposure data view currently empty: {e}")

    with tab3:
        st.markdown("### `v_scenario_impact_map`: Decision Space Scatter (Cost vs Days vs Exposure)")
        try:
            df3 = bq_client.query(f"SELECT * FROM `{settings.PROJECT_ID}.{settings.BQ_DATASET}.v_scenario_impact_map`").to_dataframe()
            if not df3.empty:
                df3["total_cost_usd"] = pd.to_numeric(df3["total_cost_usd"], errors="coerce")
                df3["days_to_coverage"] = pd.to_numeric(df3["days_to_coverage"], errors="coerce")
                df3["sla_penalty_usd"] = pd.to_numeric(df3["sla_penalty_usd"], errors="coerce")
                df3["total_exposure_usd"] = pd.to_numeric(df3["total_exposure_usd"], errors="coerce")
                scatter = alt.Chart(df3).mark_circle(size=120).encode(
                    x=alt.X("total_cost_usd:Q", title="Total Cost ($USD)", scale=alt.Scale(zero=False)),
                    y=alt.Y("days_to_coverage:Q", title="Days to Coverage"),
                    color=alt.Color("scenario_label:N", title="Scenario Mode"),
                    tooltip=["scenario_id", "scenario_label", "total_cost_usd", "days_to_coverage", "total_exposure_usd"],
                ).properties(height=380).interactive()
                st.altair_chart(scatter, use_container_width=True)
                st.dataframe(df3, use_container_width=True)
            else:
                st.info("No scenario impact records found.")
        except Exception as e:
            st.info(f"Scenario impact map currently empty: {e}")

    with tab4:
        st.markdown("### `v_spend_velocity_24h`: Rolling 24h Spend vs Ceilings")
        try:
            df4 = bq_client.query(f"SELECT * FROM `{settings.PROJECT_ID}.{settings.BQ_DATASET}.v_spend_velocity_24h`").to_dataframe()
            if not df4.empty:
                st.dataframe(df4, use_container_width=True)
            else:
                st.info("No spend in rolling 24h window.")
        except Exception as e:
            st.info(f"Spend velocity view currently empty: {e}")

    with tab5:
        st.markdown("### `v_governance_events`: Policy Rule Trips Over Time")
        try:
            df5 = bq_client.query(f"SELECT * FROM `{settings.PROJECT_ID}.{settings.BQ_DATASET}.v_governance_events` ORDER BY event_timestamp DESC LIMIT 50").to_dataframe()
            if not df5.empty:
                st.dataframe(df5, use_container_width=True)
            else:
                st.info("No governance events recorded yet.")
        except Exception as e:
            st.info(f"Governance events view currently empty: {e}")

    with tab6:
        st.markdown("### `v_supplier_reliability_trend`: 90-Day Supplier On-Time Performance")
        try:
            df6 = bq_client.query(f"SELECT * FROM `{settings.PROJECT_ID}.{settings.BQ_DATASET}.v_supplier_reliability_trend` ORDER BY on_time_delivery_rate_pct DESC").to_dataframe()
            if not df6.empty:
                bar = alt.Chart(df6).mark_bar().encode(
                    x=alt.X("supplier_name:N", sort="-y", title="Supplier"),
                    y=alt.Y("on_time_delivery_rate_pct:Q", title="On-Time Rate (%)"),
                    color=alt.Color("reliability_tier:N", title="Reliability Tier"),
                    tooltip=["supplier_name", "supplier_tier", "on_time_delivery_rate_pct", "avg_lead_time_drift_days"],
                ).properties(height=380)
                st.altair_chart(bar, use_container_width=True)
                st.dataframe(df6, use_container_width=True)
            else:
                st.info("No supplier delivery data.")
        except Exception as e:
            st.info(f"Supplier reliability view currently empty: {e}")

    with tab7:
        st.markdown("### `v_feed_quality`: Inbound ASN Data Quality & Defect Classification")
        try:
            df7 = bq_client.query(f"SELECT * FROM `{settings.PROJECT_ID}.{settings.BQ_DATASET}.v_feed_quality`").to_dataframe()
            if not df7.empty:
                st.dataframe(df7, use_container_width=True)
            else:
                st.info("No feed data.")
        except Exception as e:
            st.info(f"Feed quality view currently empty: {e}")


# =============================================================================
# VIEW 3: AUDIT LEDGER EXPLORER
# =============================================================================
elif app_mode == "Audit Ledger Explorer":
    st.markdown('<div class="main-header">📜 Tamper-Evident Hash Chain Audit Ledger</div>', unsafe_allow_html=True)
    st.markdown(
        '<div class="sub-header">Append-Only Hash Chain: record_hash = SHA256(prev_record_hash + canonical_json)</div>',
        unsafe_allow_html=True,
    )

    col1, col2 = st.columns([3, 1])
    with col1:
        st.info("Every fleet decision logs phase records: SENSE -> SANITIZE -> SCORE -> POLICY -> INTENT -> OUTCOME / APPROVAL.")
    with col2:
        if st.button("🛡️ Re-Verify Full Hash Chain"):
            try:
                ok, broken_at, count = verify_chain(bq_client, settings.BQ_DATASET)
                if ok:
                    st.success(f"Verified {count} records! 100% Tamper-Evident.")
                else:
                    st.error(f"Broken at: {broken_at}")
            except Exception as e:
                st.error(f"Verification error: {e}")

    try:
        ledger_df = bq_client.query(
            f"""
            SELECT record_id, deviation_id, workflow_root_id, phase, okf_outcome,
                   operator_sub, approval_signature, prev_record_hash, record_hash, created_at
            FROM `{settings.PROJECT_ID}.{settings.BQ_DATASET}.audit_ledger`
            ORDER BY created_at DESC
            LIMIT 100
            """
        ).to_dataframe()
        if not ledger_df.empty:
            st.dataframe(ledger_df, use_container_width=True)
        else:
            st.info("Audit ledger is empty.")
    except Exception as e:
        st.info(f"Audit ledger data unavailable: {e}")
