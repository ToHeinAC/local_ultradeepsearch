"""Page "Suchplan": the queries of a run as an editable table, and its approval (PRD M7)."""

from typing import Any

import streamlit as st

from app.client import ApiError, plan_rows, rows_to_lines
from app.gui import texts
from app.gui.state import go, param, set_param

Json = dict[str, Any]
READ_ONLY = ["id", "kind", "sent", "removed", "blocked"]
LENSES = ["A", "B", "C", "D"]


def render(client: Any) -> None:
    st.header(texts.PAGE_NAMES["suchplan"])
    run_id = param("run")
    if run_id is None:
        _choose(client)
        return
    summary = client.run_summary(run_id)
    st.caption(f"{run_id}: {summary['title'] or ''}")
    data = client.get_plan(run_id)
    if data["plan"] is None:
        st.info(texts.PLAN_NONE)
        return
    rows = plan_rows(data["plan"])
    if summary["status"] != "awaiting_plan_approval":
        st.info(texts.PLAN_CLOSED.format(status=summary["status"]))
        st.dataframe(rows)  # pyright: ignore[reportUnknownMemberType]
        return
    _editor(client, run_id, data, rows)


def _choose(client: Any) -> None:
    waiting = [r for r in client.run_summaries() if r["status"] == "awaiting_plan_approval"]
    if not waiting:
        st.info(texts.NO_PLAN_RUNS)
        return
    labels = {f"{r['run_id']} - {r['title'] or texts.NO_TITLE}": r["run_id"] for r in waiting}
    chosen = st.selectbox(texts.PAGE_NAMES["suchplan"], list(labels), label_visibility="collapsed")
    if st.button(texts.OPEN) and chosen:
        set_param("run", labels[chosen])
        st.rerun()


def _as_rows(edited: Any) -> list[Json]:
    """What `st.data_editor` returned, as a list of dicts (it follows the input type)."""
    if hasattr(edited, "to_dict"):
        return edited.to_dict("records")
    return [dict(row) for row in edited]


def _editor(client: Any, run_id: str, data: Json, rows: list[Json]) -> None:
    blocked = [r for r in rows if r["blocked"]]
    for row in blocked:
        st.error(
            texts.PLAN_BLOCKED.format(id=row["id"], query=row["original"], reason=row["blocked"])
        )
    st.caption(texts.PLAN_HELP)
    edited = _as_rows(
        st.data_editor(
            rows,
            num_rows="dynamic",
            hide_index=True,
            disabled=READ_ONLY,
            key=f"plan-{data['plan_sha256']}",
            column_config={
                "lens": st.column_config.SelectboxColumn(
                    texts.PLAN_COLUMNS["lens"], options=LENSES
                ),
                **{k: v for k, v in _labels().items()},
            },
        )
    )
    changed = rows_to_lines(edited) != rows_to_lines(rows)
    if st.button(texts.PLAN_CHECK, disabled=not changed):
        client.put_plan(run_id, rows_to_lines(edited))
        st.rerun()
    if st.button(texts.APPROVE, disabled=bool(blocked) or changed):
        _approve(client, run_id, data["plan_sha256"])


def _labels() -> dict[str, Any]:
    return {
        key: st.column_config.Column(label)
        for key, label in texts.PLAN_COLUMNS.items()
        if key != "lens"
    }


def _approve(client: Any, run_id: str, plan_sha256: str) -> None:
    try:
        client.approve_plan(run_id, plan_sha256)
    except ApiError as exc:
        if exc.status != 409 or "hash" not in exc.detail:
            raise
        st.error(texts.PLAN_STALE)
        return
    go("laeufe", run=run_id)
