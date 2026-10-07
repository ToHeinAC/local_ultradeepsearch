"""The decision of Phase 1: the exact brief, its settings, and Freigeben / Überarbeiten /
Speichern. "Freigeben" sends the hash of the brief the page shows."""

from typing import Any

import streamlit as st

from app.client import ApiError
from app.gui import texts
from app.gui.state import go

Json = dict[str, Any]


def _recommendation(view: Json) -> None:
    recommendation = view["recommendation"]
    if recommendation:
        tier = texts.TIER_NAMES.get(recommendation["tier"], recommendation["tier"])
        st.markdown(texts.RECOMMENDATION.format(tier=tier, fmt=recommendation["response_format"]))
        st.caption(recommendation["rationale"])


def _brief_text(client: Any, session_id: str, view: Json) -> bool:
    """The brief as an editable text. True while the shown text is the stored one."""
    st.subheader(texts.BRIEF_TITLE)
    shown = st.text_area(
        texts.BRIEF_TITLE,
        value=view["brief_text"],
        key=f"brief-{view['brief_sha256']}",
        height=400,
        label_visibility="collapsed",
    )
    if shown == view["brief_text"]:
        return True
    st.warning(texts.BRIEF_EDITED)
    if st.button(texts.BRIEF_TAKE):
        client.edit_brief(session_id, shown)
        st.rerun()
    return False


def _settings(client: Any, session_id: str, view: Json) -> None:
    st.subheader(texts.SETTINGS_TITLE)
    current: Json = view["settings"] or {}
    templates = {t["id"]: t for t in client.templates()}
    ids = list(templates)
    index = ids.index(current["template_id"]) if current.get("template_id") in ids else 0
    template_id = st.selectbox(
        texts.TEMPLATE,
        ids,
        index=index,
        key="template_id",
        format_func=lambda i: templates[i]["name"],
    )
    if template_id:
        sections = ", ".join(templates[template_id]["sections"])
        st.markdown(texts.TEMPLATE_SECTIONS.format(sections=sections))
    _template_upload(client)
    language = st.text_input(
        texts.REPORT_LANGUAGE,
        value=str(current.get("report_language", "")),
        key="report_language",
    )
    formats = list(texts.RESPONSE_FORMATS)
    chosen: str | None = current.get("response_format")
    fmt = st.selectbox(
        texts.RESPONSE_FORMAT,
        formats,
        index=formats.index(chosen) if chosen in formats else 0,
        key="response_format",
    )
    if st.button(texts.SETTINGS_TAKE):
        client.set_settings(
            session_id, report_language=language, response_format=fmt, template_id=template_id
        )
        st.rerun()


def _template_upload(client: Any) -> None:
    upload = st.file_uploader(texts.TEMPLATE_UPLOAD, type=["md"], key="template_file")
    if upload and st.button(texts.TEMPLATE_UPLOAD_SEND):
        client.upload_template(upload.name, upload.getvalue())
        st.rerun()


def _run_options() -> tuple[str | None, int | None]:
    st.radio(texts.TIER, ["light"], format_func=lambda t: texts.TIER_NAMES[t], key="tier")
    st.caption(texts.FULL_LATER)
    model = st.selectbox(
        texts.SUMMARIZE_MODEL,
        [None, *texts.SUMMARIZE_MODELS],
        format_func=lambda m: texts.SUMMARIZE_DEFAULT if m is None else m,
        key="summarize_model",
    )
    limited = st.checkbox(texts.CAP_ON, key="cap_on")
    cap = st.number_input(texts.CAP, min_value=0, value=60, step=5, key="cap", disabled=not limited)
    return model, int(cap) if limited else None


def _approve(client: Any, session_id: str, view: Json, model: str | None, cap: int | None) -> None:
    try:
        approved = client.approve_session(
            session_id, view["brief_sha256"], "light", summarize_model=model, tavily_cap=cap
        )
    except ApiError as exc:
        if exc.status != 409:
            raise
        st.error(texts.STALE)
        return
    go("suchplan", run=approved["run_id"])


def decision_view(client: Any, session_id: str, view: Json) -> None:
    unchanged = _brief_text(client, session_id, view)
    _recommendation(view)
    _settings(client, session_id, view)
    model, cap = _run_options()
    if st.button(texts.APPROVE, disabled=not unchanged):
        _approve(client, session_id, view, model, cap)
    feedback = st.text_input(texts.FEEDBACK, key="feedback")
    if st.button(texts.REVISE):
        client.revise(session_id, feedback)
        st.rerun()
    if st.button(texts.SAVE):
        client.save(session_id)
        st.rerun()
