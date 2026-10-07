"""Page "Neue Recherche": the question, the clarification rounds and the decision (PRD M7)."""

from typing import Any

import streamlit as st

from app.client import ApiDown
from app.gui import texts
from app.gui.pages.brief_decision import decision_view
from app.gui.state import POLL_SECONDS, param, set_param

Json = dict[str, Any]


def go(page: str, **params: str) -> None:
    """Open another page; the ids it needs travel in the URL, so a reload keeps them."""
    set_param("page", page)
    for name, value in params.items():
        set_param(name, value)
    st.rerun()


def render(client: Any) -> None:
    st.header(texts.PAGE_NAMES["neue_recherche"])
    session_id = param("session")
    if session_id is None:
        _start_form(client)
        _resume_list(client)
    else:
        _session(client, session_id)


def _start_form(client: Any) -> None:
    question = st.text_area(texts.QUESTION_LABEL, key="question")
    files = st.file_uploader(
        texts.UPLOADS, accept_multiple_files=True, type=["pdf", "docx", "md", "txt"]
    )
    if not st.button(texts.START):
        return
    if not question.strip():
        st.warning(texts.QUESTION_EMPTY)
        return
    started = client.start_session(question, [(f.name, f.getvalue()) for f in files or []])
    set_param("session", started["session_id"])
    st.rerun()


def _resume_list(client: Any) -> None:
    open_sessions = [s for s in client.list_sessions() if s["status"] != "approved"]
    if not open_sessions:
        return
    st.subheader(texts.RESUME_TITLE)
    labels = {
        f"{s['session_id']} - {s['title'] or texts.NO_TITLE} ({s['status']})": s["session_id"]
        for s in open_sessions
    }
    chosen = st.selectbox(texts.RESUME_TITLE, list(labels), label_visibility="collapsed")
    if st.button(texts.RESUME) and chosen:
        set_param("session", labels[chosen])
        st.rerun()


@st.fragment(run_every=POLL_SECONDS)
def _poll(client: Any, session_id: str) -> None:
    try:
        busy = client.get_session(session_id)["busy"]
    except ApiDown:
        st.warning(texts.API_DOWN)
        return
    if busy:
        st.info(texts.BUSY)
    else:
        st.rerun()


def _session(client: Any, session_id: str) -> None:
    view = client.get_session(session_id)
    for notice in view["notices"]:
        st.info(notice)
    if view["busy"]:
        _poll(client, session_id)
    elif view["error"]:
        st.error(view["error"])
        if st.button(texts.RETRY):
            client.retry(session_id)
            st.rerun()
    else:
        _by_state(client, session_id, view)


def _by_state(client: Any, session_id: str, view: Json) -> None:
    waiting = view["waiting_for"]
    if waiting == "questions":
        _questions(client, session_id, view)
    elif waiting == "offer":
        _offer(client, session_id, view)
    elif waiting == "decision":
        decision_view(client, session_id, view)
    elif view["run_id"]:
        st.success(texts.APPROVED.format(run_id=view["run_id"]))
        if st.button(texts.TO_PLAN):
            go("suchplan", run=view["run_id"])
    elif view["status"] == "saved":
        st.info(texts.SAVED.format(path=view.get("draft_path") or ""))
    elif st.button(texts.RETRY):
        client.retry(session_id)
        st.rerun()


def _answer(round_number: int, index: int, question: Json) -> Json:
    st.markdown(f"**{index + 1}. {question['question']}**")
    candidate = question["candidate"]
    value = st.text_input(
        question["question"],
        value=candidate,
        key=f"answer-{round_number}-{index}",
        label_visibility="collapsed",
    )
    if candidate and value == candidate:
        return {"kind": "accept"}
    text = str(value or "")
    return {"kind": "text", "text": text} if text.strip() else {"kind": "unknown"}


def _checklist(checklist: Json) -> None:
    st.markdown(f"**{texts.CHECKLIST}**")
    st.markdown("\n".join(f"- {item}: {status}" for item, status in checklist.items()))


def _uploads(client: Any, session_id: str, view: Json) -> None:
    for upload in view["uploads"]:
        st.caption(texts.UPLOAD_LINE.format(**upload))
        for warning in upload.get("warnings", []):
            st.warning(f"{upload['name']}: {warning}")
    more = st.file_uploader(texts.UPLOAD_MORE, accept_multiple_files=True, key="more")
    if more and st.button(texts.UPLOAD_SEND):
        client.add_uploads(session_id, [(f.name, f.getvalue()) for f in more])
        st.rerun()


def _questions(client: Any, session_id: str, view: Json) -> None:
    round_number = view["round"]
    st.subheader(texts.ROUND.format(n=round_number + 1, max=view["max_rounds"]))
    _checklist(view["checklist"])
    _uploads(client, session_id, view)
    st.caption(texts.ANSWER_HINT)
    answers = [_answer(round_number, i, q) for i, q in enumerate(view["questions"])]
    note = st.text_input(texts.NOTE, key="note")
    genug = st.checkbox(texts.GENUG, key="genug")
    if st.button(texts.SEND):
        client.send_message(session_id, answers, note=note, genug=genug)
        st.rerun()


def _offer(client: Any, session_id: str, view: Json) -> None:
    st.subheader(texts.OFFER_TITLE)
    st.code(view["pasted"], language="markdown")
    if view["refusal"]:
        st.warning(view["refusal"])
    if st.button(texts.STRENGTHEN):
        client.send_message(session_id, offer="strengthen")
        st.rerun()
    if st.button(texts.INSTALL, disabled=bool(view["refusal"])):
        client.send_message(session_id, offer="install")
        st.rerun()
