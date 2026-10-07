"""Page "Läufe": the history, what waits for an approval, and one run in detail (PRD M7)."""

from typing import Any

import streamlit as st

from app.gui import texts
from app.gui.pages import run_detail
from app.gui.state import go, param, set_param

Json = dict[str, Any]


def render(client: Any) -> None:
    st.header(texts.PAGE_NAMES["laeufe"])
    summaries = client.run_summaries()
    _pending(client, summaries)
    if not summaries:
        st.info(texts.NO_RUNS)
        return
    ids = [s["run_id"] for s in summaries]
    current = param("run")
    chosen = st.selectbox(
        texts.RUN,
        ids,
        index=ids.index(current) if current in ids else None,
        format_func=lambda i: f"{i} - {_title(summaries, i)}",
    )
    if chosen != current and chosen is not None:
        set_param("run", chosen)
    _history(summaries)
    if chosen is not None:
        run_detail.show(client, chosen)


def _title(summaries: list[Json], run_id: str) -> str:
    return next(s["title"] for s in summaries if s["run_id"] == run_id) or texts.NO_TITLE


def _history(summaries: list[Json]) -> None:
    st.subheader(texts.HISTORY)
    st.dataframe(  # pyright: ignore[reportUnknownMemberType]
        [
            {
                texts.RUN: s["run_id"],
                "Titel": s["title"],
                texts.M_STATUS: s["status"],
                "Erstellt": s["created_at"][:16].replace("T", " "),
                "Von": s["created_by"],
            }
            for s in summaries
        ],
        hide_index=True,
    )


def _pending(client: Any, summaries: list[Json]) -> None:
    sessions = [s for s in client.list_sessions() if s["status"] == "awaiting_decision"]
    briefs = [s for s in summaries if s["status"] == "awaiting_brief_approval"]
    plans = [s for s in summaries if s["status"] == "awaiting_plan_approval"]
    if not (sessions or briefs or plans):
        return
    st.subheader(texts.PENDING)
    for run in briefs:
        by = run["created_by"] or "-"
        st.markdown(texts.PENDING_BRIEF.format(id=run["run_id"], title=run["title"], by=by))
        if st.button(texts.APPROVE_BRIEF.format(id=run["run_id"])):
            client.approve_run(run["run_id"], run["brief_sha256"])
            st.rerun()
    for run in plans:
        st.markdown(texts.PENDING_PLAN.format(id=run["run_id"], title=run["title"]))
        if st.button(texts.CHECK_PLAN.format(id=run["run_id"])):
            go("suchplan", run=run["run_id"])
    for session in sessions:
        title = session["title"] or texts.NO_TITLE
        by = session["created_by"] or "-"
        st.markdown(texts.PENDING_SESSION.format(id=session["session_id"], title=title, by=by))
        if st.button(texts.OPEN_SESSION.format(id=session["session_id"])):
            go("neue_recherche", session=session["session_id"])
