"""One run on the page "Läufe": numbers, step timeline, events, the outbound log and control.
While the run is active a fragment refreshes it every `POLL_SECONDS`."""

from datetime import UTC, datetime
from typing import Any

import streamlit as st

from app.client import ApiDown
from app.gui import texts
from app.gui.state import POLL_SECONDS, go, set_param

Json = dict[str, Any]
ACTIVE = ("queued", "running")
CANCELLABLE = ("queued", "running", "awaiting_plan_approval", "awaiting_brief_approval")
RESUMABLE = ("failed", "cancelled")
SHOWN_EVENTS = 15


def merge_events(client: Any, run_id: str, state: Json) -> None:
    """Append the events after the cursor in ``state`` (`lines`, `next`); each is read once."""
    answer = client.events(run_id, after=state["next"])
    state["lines"].extend(answer["events"])
    state["next"] = answer["next"]


def _elapsed(summary: Json) -> str:
    seconds = summary["elapsed_s"]
    if seconds is None and summary["started_at"]:
        started = datetime.fromisoformat(summary["started_at"])
        seconds = (datetime.now(UTC) - started).total_seconds()
    return "-" if seconds is None else f"{int(seconds) // 60} min {int(seconds) % 60} s"


def _metrics(summary: Json) -> None:
    columns = st.columns(4)
    columns[0].metric(texts.M_STATUS, summary["status"])
    columns[1].metric(texts.M_STEP, summary["step"] or "-")
    columns[2].metric(texts.M_ELAPSED, _elapsed(summary))
    columns[3].metric(
        texts.M_SOURCES, "-" if summary["sources"] is None else str(summary["sources"])
    )
    credits = st.columns(3)
    credits[0].metric(texts.M_CREDITS_RUN, f"{summary['credits_run']} / {summary['credit_cap']}")
    credits[1].metric(
        texts.M_CREDITS_MONTH, f"{summary['credits_month']} / {summary['month_limit']}"
    )
    credits[2].metric(texts.M_WARNINGS, str(summary["warnings"]))


def _timeline(summary: Json) -> None:
    lines = [
        texts.STEP_LINE.format(
            step=s["step"],
            status=texts.STEP_DONE if s["status"] == "done" else texts.STEP_RUNNING,
            start=s["started_at"][11:19],
            end=(s["ended_at"] or "...")[11:19],
        )
        for s in summary["steps"]
    ]
    st.markdown("\n\n".join(lines))


def _events(client: Any, run_id: str) -> None:
    state = st.session_state.setdefault(f"events-{run_id}", {"lines": [], "next": 0})
    merge_events(client, run_id, state)
    shown = state["lines"][-SHOWN_EVENTS:]
    with st.expander(texts.EVENTS):
        st.code("\n".join(f"{e['ts'][11:19]} {e['level']:7} {e['type']}" for e in shown))


def _outbound(client: Any, run_id: str) -> None:
    if st.checkbox(texts.OUTBOUND, key="show-outbound"):
        st.dataframe(client.outbound(run_id)["lines"])  # pyright: ignore[reportUnknownMemberType]


def _body(client: Any, run_id: str) -> None:
    try:
        summary = client.run_summary(run_id)
        _metrics(summary)
        _timeline(summary)
        _events(client, run_id)
        _outbound(client, run_id)
    except ApiDown:
        st.warning(texts.API_DOWN)


def _controls(client: Any, summary: Json) -> None:
    run_id, status = summary["run_id"], summary["status"]
    if status in ("done", "blocked") and st.button(texts.TO_REPORT):
        go("bericht", run=run_id)
    if status in CANCELLABLE and st.button(texts.CANCEL):
        client.cancel(run_id)
        st.rerun()
    if status in RESUMABLE and st.button(texts.RESUME_RUN):
        client.resume(run_id)
        st.rerun()
    if status != "running":
        confirmed = st.checkbox(texts.DELETE_CONFIRM, key="confirm-delete")
        if st.button(texts.DELETE, disabled=not confirmed):
            client.delete(run_id)
            set_param("run", None)
            st.rerun()


def show(client: Any, run_id: str) -> None:
    summary = client.run_summary(run_id)
    st.subheader(f"{run_id}: {summary['title'] or texts.NO_TITLE}")
    _controls(client, summary)
    interval = POLL_SECONDS if summary["status"] in ACTIVE else None
    st.fragment(run_every=interval)(_body)(client, run_id)
