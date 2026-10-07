"""Page "Bericht": the rendered report, the gate result and the downloads (PRD M7)."""

from typing import Any

import streamlit as st

from app.client import ApiError
from app.gui import texts
from app.gui.state import param, set_param

Json = dict[str, Any]
MIME = {
    "md": "text/markdown",
    "pdf": "application/pdf",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
}


def render(client: Any) -> None:
    st.header(texts.PAGE_NAMES["bericht"])
    run_id = param("run")
    if run_id is None:
        _choose(client)
        return
    try:
        markdown = client.report(run_id, "md").decode("utf-8")
    except ApiError as exc:
        if exc.status != 409:
            raise
        st.info(texts.REPORT_NOT_READY.format(status=exc.run_status))
        return
    _gate(client.gate(run_id))
    _downloads(client, run_id, markdown)
    _body(markdown)


def _choose(client: Any) -> None:
    ready = [r for r in client.run_summaries() if r["status"] in ("done", "blocked")]
    if not ready:
        st.info(texts.NO_REPORT_RUNS)
        return
    labels = {f"{r['run_id']} - {r['title'] or texts.NO_TITLE}": r["run_id"] for r in ready}
    chosen = st.selectbox(texts.RUN, list(labels))
    if st.button(texts.OPEN) and chosen:
        set_param("run", labels[chosen])
        st.rerun()


def _gate(gate: Json) -> None:
    if gate["passed"]:
        st.success(texts.GATE_PASSED)
    else:
        st.error(texts.GATE_FAILED.format(checks=", ".join(gate["failed"])))


def _downloads(client: Any, run_id: str, markdown: str) -> None:
    for fmt in ("md", "pdf", "docx"):
        try:
            data = markdown.encode("utf-8") if fmt == "md" else client.report(run_id, fmt)
        except ApiError as exc:
            if exc.status != 404:
                raise
            hint = f" {texts.DOCX_HINT}" if fmt == "docx" else ""
            st.caption(texts.FORMAT_MISSING.format(fmt=fmt.upper()) + hint)
            continue
        st.download_button(
            texts.DOWNLOAD.format(fmt=fmt.upper()),
            data,
            file_name=f"{run_id}.{fmt}",
            mime=MIME[fmt],
            key=f"download-{fmt}",
        )


def _body(markdown: str) -> None:
    """The report; one that is over 100 000 characters becomes collapsible sections."""
    if len(markdown) <= texts.BIG_REPORT_CHARS:
        st.markdown(markdown)
        return
    head, *sections = markdown.split("\n## ")
    st.markdown(head)
    for section in sections:
        title, _, text = section.partition("\n")
        with st.expander(title.strip()):
            st.markdown(text)
