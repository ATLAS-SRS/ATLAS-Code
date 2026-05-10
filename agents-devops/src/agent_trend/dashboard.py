#!/usr/bin/env python3
from typing import Any

import os

import requests

import streamlit as st
from openai import OpenAI

from src.agent_guardian.config import LLM_API_KEY, LLM_API_URL, LLM_MODEL, LOGGER
from src.agent_guardian.config import _normalize_llm_base_url
from src.agent_trend.trend_analyzer import analyze_recent_trends_sync
from src.database import (
    fetch_incident_reports_sync,
    fetch_pending_approvals_sync,
    fetch_trend_reports_sync,
    get_sync_session,
    init_database_sync,
)

API_BASE_URL = os.getenv("GUARDIAN_API_URL", "http://localhost:8000").rstrip("/")
API_FALLBACK_URL = os.getenv(
    "GUARDIAN_API_FALLBACK_URL",
    "http://atlas-guardian-service.default.svc.cluster.local:8000",
).rstrip("/")

_SCALING_ACTIONS = {
    "SCALE_UP",
    "SCALE_DOWN",
    "SET_REPLICAS",
    "BUDGET_REALLOCATION",
}

st.set_page_config(layout="wide", page_title="ATLAS Post-Mortem")


def ensure_database_ready() -> None:
    try:
        init_database_sync()
    except Exception as exc:
        LOGGER.error("Failed to initialize database for dashboard", extra={"error": str(exc)})
        raise


def inject_styles() -> None:
    st.markdown(
        """
        <style>
        .stApp {
            background:
                radial-gradient(circle at top left, rgba(0, 153, 204, 0.14), transparent 28%),
                radial-gradient(circle at top right, rgba(255, 140, 66, 0.10), transparent 24%),
                #0b1220;
            color: #e5eef9;
        }
        [data-testid="stSidebar"] {
            background: linear-gradient(180deg, #0f172a 0%, #111827 100%);
        }
        .atlas-panel {
            background: rgba(15, 23, 42, 0.72);
            border: 1px solid rgba(148, 163, 184, 0.18);
            border-radius: 18px;
            padding: 1rem 1.1rem;
            margin-bottom: 1rem;
            box-shadow: 0 18px 45px rgba(0, 0, 0, 0.18);
        }
        .atlas-kicker {
            text-transform: uppercase;
            letter-spacing: 0.12em;
            color: #7dd3fc;
            font-size: 0.78rem;
            margin-bottom: 0.3rem;
        }
        .atlas-title {
            font-size: 1.7rem;
            font-weight: 700;
            margin-bottom: 0.3rem;
        }
        .atlas-muted {
            color: #94a3b8;
            font-size: 0.95rem;
        }
        </style>
        """,
        unsafe_allow_html=True,
    )


def load_incidents() -> list[dict[str, Any]]:
    try:
        with get_sync_session() as session:
            incidents = fetch_incident_reports_sync(session)
    except Exception as exc:
        return [
            {
                "incident_id": "database-error",
                "timestamp_utc": "",
                "load_error": str(exc),
            }
        ]

    return sorted(
        incidents,
        key=lambda item: item.get("last_seen_utc") or item.get("timestamp_utc") or "",
        reverse=True,
    )


def load_trends() -> list[dict[str, Any]]:
    try:
        with get_sync_session() as session:
            trends = fetch_trend_reports_sync(session)
    except Exception as exc:
        return [
            {
                "trend_id": "database-error",
                "generated_at_utc": "",
                "load_error": str(exc),
            }
        ]

    return sorted(
        trends,
        key=lambda item: item.get("generated_at_utc") or item.get("timestamp_utc") or "",
        reverse=True,
    )


def load_pending_approvals() -> list[dict[str, Any]]:
    try:
        with get_sync_session() as session:
            approvals = fetch_pending_approvals_sync(session)
    except Exception as exc:
        return [
            {
                "approval_id": "database-error",
                "incident_id": "",
                "deployment": "",
                "load_error": str(exc),
            }
        ]

    return approvals


def trigger_trend_analysis() -> list[str]:
    llm_client = OpenAI(
        base_url=_normalize_llm_base_url(LLM_API_URL),
        api_key=LLM_API_KEY,
    )
    try:
        return analyze_recent_trends_sync(
            llm_client,
            LLM_MODEL,
            None,
        )
    finally:
        llm_client.close()


def display_key_value_grid(values: dict[str, Any]) -> None:
    columns = st.columns(3)
    for index, (label, value) in enumerate(values.items()):
        columns[index % 3].metric(label, "-" if value in (None, "") else str(value))


def format_action_dict(action: Any) -> str:
    """Format an action dict into readable text."""
    if isinstance(action, dict):
        action_type = action.get("action", "UNKNOWN")
        return str(action_type)
    return str(action) if action else "UNKNOWN"


def render_action_details(action: Any) -> None:
    """Render action details in a readable format."""
    if not action or action in (None, ""):
        st.write("No action details")
        return
    
    if isinstance(action, dict):
        # Display key action fields
        col1, col2 = st.columns(2)
        with col1:
            st.markdown(f"**Action Type**: `{action.get('action', 'UNKNOWN')}`")
        with col2:
            st.markdown(f"**Outcome**: {action.get('outcome', 'N/A')}")
        
        # Display rationale if present
        if action.get("rationale"):
            st.markdown("**Rationale**:")
            st.write(action["rationale"])
        
        # Display follow-up if present
        if action.get("follow_up"):
            st.markdown("**Follow-up**:")
            st.warning(action["follow_up"])
    else:
        st.write(str(action))


def _approval_remediation_text(approval: dict[str, Any]) -> str:
    payload = approval.get("payload") or {}
    report = payload.get("report") or {}
    execution = report.get("execution_details") or {}
    action = execution.get("action") or {}

    suggested_action = payload.get("suggested_action") or action.get("action") or "UNKNOWN"
    deployment = payload.get("suggested_deployment") or execution.get("deployment") or approval.get("deployment", "unknown")
    replicas = payload.get("suggested_replicas")
    rationale = payload.get("suggested_rationale") or action.get("rationale") or execution.get("rationale") or "No rationale captured."
    follow_up = payload.get("suggested_follow_up") or action.get("follow_up") or execution.get("follow_up") or ""

    lines = [f"Suggested action: {suggested_action}", f"Deployment: {deployment}"]
    if replicas is not None:
        lines.append(f"Suggested replicas: {replicas}")
    if rationale:
        lines.append(f"Why: {rationale}")
    if follow_up:
        lines.append(f"Follow-up: {follow_up}")
    return "\n".join(lines)


def _approval_action(approval: dict[str, Any]) -> str:
    payload = approval.get("payload") or {}
    report = payload.get("report") or {}
    execution = report.get("execution_details") or {}
    action = execution.get("action") or {}
    value = payload.get("suggested_action") or action.get("action") or "UNKNOWN"
    return str(value).strip().upper()


def _approval_requires_execute(approval: dict[str, Any]) -> bool:
    return _approval_action(approval) in _SCALING_ACTIONS


def _guardian_post(path: str, *, json_payload: dict[str, Any], timeout: int = 15) -> requests.Response:
    """POST to Guardian using the primary URL, then a cluster fallback if needed."""
    urls = [API_BASE_URL]
    if API_FALLBACK_URL and API_FALLBACK_URL not in urls:
        urls.append(API_FALLBACK_URL)

    last_exc: Exception | None = None
    for base_url in urls:
        try:
            response = requests.post(f"{base_url}{path}", json=json_payload, timeout=timeout)
            response.raise_for_status()
            return response
        except Exception as exc:
            last_exc = exc
            continue

    if last_exc is not None:
        raise last_exc
    raise RuntimeError("Guardian request failed without an exception")


def _approval_modal(approval: dict[str, Any]) -> None:
    payload = approval.get("payload") or {}
    suggested_replicas = payload.get("suggested_replicas")
    action_name = _approval_action(approval)
    requires_execute = _approval_requires_execute(approval)

    if not hasattr(st, "dialog"):
        st.warning("Streamlit dialog support is unavailable; showing approval inline.")
        st.info(_approval_remediation_text(approval))
        return

    @st.dialog(f"Approval required: {approval.get('deployment', 'unknown')}")
    def _dialog() -> None:
        st.markdown("### Suggested remediation")
        st.code(_approval_remediation_text(approval), language="text")
        st.caption(f"Action type: {action_name}")
        if suggested_replicas is not None:
            st.caption(f"Default replicas: {suggested_replicas}")
        if not requires_execute:
            st.info("Rollback is recommendation-only. No execution or approval buttons are available.")
            return

        allow_col, deny_col = st.columns(2)
        with allow_col:
            if st.button("Allow", type="primary", use_container_width=True):
                try:
                    replicas = int(suggested_replicas) if suggested_replicas is not None else 1
                    _guardian_post(
                        f"/approvals/{approval['approval_id']}/approve",
                        json_payload={"approver": os.getenv("USER", "streamlit_user"), "reason": "approved from dashboard"},
                    )
                    _guardian_post(
                        f"/approvals/{approval['approval_id']}/execute",
                        json_payload={"replicas": replicas},
                    )
                    st.success("Remediation executed and approved.")
                    st.session_state.pop("selected_approval_id", None)
                    st.rerun()
                except Exception as exc:
                    st.error(f"Allow failed: {exc}")
        with deny_col:
            if st.button("Not allow", use_container_width=True):
                try:
                    _guardian_post(
                        f"/approvals/{approval['approval_id']}/deny",
                        json_payload={"approver": os.getenv("USER", "streamlit_user"), "reason": "denied from dashboard"},
                    )
                    st.info("Remediation denied.")
                    st.session_state.pop("selected_approval_id", None)
                    st.rerun()
                except Exception as exc:
                    st.error(f"Deny failed: {exc}")

    _dialog()


def render_incident(incident: dict[str, Any], approvals: list[dict[str, Any]]) -> None:
    alert_context = incident.get("alert_context") or {}
    investigation = incident.get("investigation") or {}
    execution = incident.get("execution_details") or {}

    st.title(incident.get("incident_id", "Unknown incident"))
    st.caption(f"Last seen UTC: {incident.get('last_seen_utc', incident.get('timestamp_utc', '-'))}")

    if incident.get("load_error"):
        st.error(f"Could not load incident report: {incident['load_error']}")
        return

    st.subheader("Alert Context")
    display_key_value_grid(
        {
            "Alert": alert_context.get("alert_name", "unknown"),
            "Severity": alert_context.get("severity", "unknown"),
            "Status": alert_context.get("status", "unknown"),
            "Deployment": alert_context.get("deployment", "-"),
            "Policy": alert_context.get("workload_policy", "-"),
            "Started": alert_context.get("startsAt", "-"),
        }
    )
    display_key_value_grid(
        {
            "Occurrences": incident.get("occurrence_count", 1),
            "First Seen": incident.get("first_seen_utc", "-"),
            "Last Seen": incident.get("last_seen_utc", incident.get("timestamp_utc", "-")),
        }
    )
    if alert_context.get("summary"):
        st.info(alert_context["summary"])
    if alert_context.get("description"):
        st.write(alert_context["description"])

    st.subheader("Investigation")
    display_key_value_grid(
        {
            "Verdict": investigation.get("verdict", "UNKNOWN"),
            "Confidence": investigation.get("confidence", "-"),
        }
    )
    
    # Display recommended next step as full-width text instead of in metric grid
    next_step = investigation.get("recommended_next_step", "")
    if next_step:
        st.info(f"**Next Step**: {next_step}")
    
    evidence = investigation.get("evidence") or []
    if evidence:
        st.markdown("**Evidence**:")
        for item in evidence:
            st.write(f"- {item}")
    else:
        st.write("No evidence captured.")

    st.subheader("Execution Details")
    display_key_value_grid(
        {
            "Action": format_action_dict(execution.get("action")),
            "Deployment": execution.get("deployment", "-"),
            "Human Approval": execution.get("human_approval", "-"),
        }
    )
    
    # Render detailed action information
    if execution.get("action"):
        st.markdown("**Action Details**:")
        render_action_details(execution.get("action"))
    
    if execution.get("replicas"):
        st.json(execution["replicas"], expanded=True)
    if execution.get("executed_tools"):
        st.write("Executed tools: " + ", ".join(map(str, execution["executed_tools"])))

    incident_id = incident.get("incident_id")
    incident_approval = next((item for item in approvals if item.get("incident_id") == incident_id), None)

    if incident_approval:
        st.subheader("Pending Approval")
        st.info(_approval_remediation_text(incident_approval))

        if _approval_requires_execute(incident_approval):
            st.warning("This incident is waiting for human approval.")
            if st.button("Review remediation", type="primary"):
                st.session_state["selected_approval_id"] = incident_approval.get("approval_id")
                st.rerun()
        else:
            st.info("Rollback recommendation only. Execute manually via your deployment process.")
    elif execution.get("human_approval") == "required":
        st.warning("Human approval is required but no pending approval request was found for this incident.")

    st.subheader("Final Summary")
    st.markdown(str(incident.get("final_summary") or "No final summary captured."))

    with st.expander("Raw JSON"):
        st.json(incident, expanded=False)


def render_incident_view() -> None:
    incidents = load_incidents()
    approvals = load_pending_approvals()
    st.sidebar.caption(f"Incident reports: {len(incidents)}")
    st.sidebar.caption(f"Pending approvals: {len(approvals)}")

    if not incidents:
        st.title("No incidents yet")
        st.caption("Waiting for incident reports in PostgreSQL")
        return

    labels = [
        (
            f"{incident.get('last_seen_utc', incident.get('timestamp_utc', 'unknown time'))} | "
            f"{incident.get('incident_id', 'unknown')} | "
            f"x{incident.get('occurrence_count', 1)}"
        )
        for incident in incidents
    ]
    selected_label = st.sidebar.selectbox("Incident Report", labels)
    selected_incident = incidents[labels.index(selected_label)]

    render_incident(selected_incident, approvals)


def render_trend(trend: dict[str, Any]) -> None:
    if trend.get("load_error"):
        st.error(f"Could not load trend report: {trend['load_error']}")
        return

    deployment = trend.get("deployment", "unknown")
    incident_count = int(trend.get("incident_count", 0) or 0)
    total_occurrences = int(trend.get("total_occurrences", incident_count) or incident_count)

    metric_columns = st.columns((1.1, 1, 1))
    metric_columns[0].metric("Incident Count (24h)", incident_count, delta=f"Occurrences: {total_occurrences}")
    metric_columns[1].metric("Deployment", deployment)
    metric_columns[2].metric("Trend Reports Linked", len(trend.get("source_incident_ids") or []))

    st.info(trend.get("pattern_detected") or "No persistent pattern detected yet.")
    st.warning(trend.get("root_cause_hypothesis") or "No root cause hypothesis recorded.")
    st.info(trend.get("long_term_recommendation") or "No long-term recommendation recorded.")

    summaries = trend.get("incident_summaries") or []
    if summaries:
        st.subheader("Evidence Snapshot")
        st.json(summaries, expanded=False)

    with st.expander("Raw JSON"):
        st.json(trend, expanded=False)


def render_trend_view() -> None:
    trends = load_trends()
    st.sidebar.caption(f"Trend reports: {len(trends)}")

    trend_run_status = st.session_state.pop("trend_run_status", None)

    st.markdown(
        """
        <div class="atlas-panel">
            <div class="atlas-kicker">Problem Management / Trend Agent</div>
            <div class="atlas-title">Trend Analyzer</div>
            <div class="atlas-muted">
                Run the Trend Agent on demand to review the last 24 hours of incidents stored in PostgreSQL and generate long-term problem-management reports.
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    action_col, status_col = st.columns((0.95, 2.05))
    with action_col:
        trigger_clicked = st.button("Run Trend Analysis", type="primary", use_container_width=True)

    if trend_run_status:
        status_level = trend_run_status.get("level", "info")
        status_message = trend_run_status.get("message", "")
        if status_level == "success":
            st.success(status_message)
        elif status_level == "error":
            st.error(status_message)
        else:
            st.info(status_message)

    if trigger_clicked:
        with st.spinner("Trend Agent is analyzing recent incidents..."):
            try:
                saved_reports = trigger_trend_analysis()
            except Exception as exc:
                st.session_state["trend_run_status"] = {
                    "level": "error",
                    "message": f"Trend analysis failed: {exc}",
                }
            else:
                if saved_reports:
                    st.session_state["trend_run_status"] = {
                        "level": "success",
                        "message": f"Generated {len(saved_reports)} trend report(s).",
                    }
                else:
                    st.session_state["trend_run_status"] = {
                        "level": "info",
                        "message": (
                            "No new trend reports were generated. "
                            "Trend Agent did not find any deployment with at least 1 occurrence in the last 24 hours."
                        ),
                    }
            st.rerun()

    with status_col:
        st.caption("Storage backend: PostgreSQL")

    st.subheader("Trend Reports")
    if not trends:
        st.markdown(
            """
            <div class="atlas-panel">
                <div class="atlas-title">No trend reports yet</div>
                <div class="atlas-muted">
                    Click "Run Trend Analysis" to generate reports manually. Each report will appear below as an expandable entry.
                </div>
            </div>
            """,
            unsafe_allow_html=True,
        )
        return

    for index, trend in enumerate(trends, start=1):
        label = (
            f"{index}. "
            f"{trend.get('generated_at_utc', 'unknown time')} | "
            f"{trend.get('deployment', 'unknown deployment')} | "
            f"{trend.get('total_occurrences', trend.get('incident_count', 0))} occurrences"
        )
        with st.expander(label, expanded=index == 1):
            st.caption(trend.get("trend_id", ""))
            render_trend(trend)


def main() -> None:
    inject_styles()
    ensure_database_ready()

    st.sidebar.title("ATLAS Post-Mortem")
    selection = st.sidebar.radio(
        "View",
        ("Incident Management (Agent 1)", "Problem Management / Trends (Trend Agent)"),
    )
    if st.sidebar.button("Refresh"):
        st.rerun()

    selected_approval_id = st.session_state.get("selected_approval_id")
    if selected_approval_id:
        approvals = load_pending_approvals()
        approval = next((item for item in approvals if item.get("approval_id") == selected_approval_id), None)
        if approval:
            _approval_modal(approval)
        else:
            st.session_state.pop("selected_approval_id", None)

    auto_refresh = st.sidebar.toggle("Auto-refresh", value=False)
    if auto_refresh:
        st.sidebar.caption("Refreshing every 15 seconds")
        st.markdown("<meta http-equiv='refresh' content='15'>", unsafe_allow_html=True)

    st.sidebar.caption("Storage: PostgreSQL")

    if selection == "Incident Management (Agent 1)":
        render_incident_view()
    else:
        render_trend_view()


if __name__ == "__main__":
    main()
