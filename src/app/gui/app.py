"""The Streamlit app (`udr gui`): a German front end for the REST API (PRD M7). It imports only
`app.client` from the application, so it can run on another machine than the service."""

import os
import signal

import streamlit as st

from app.client import ApiDown, ApiError, MissingApiKey
from app.gui import texts
from app.gui.pages import PAGES
from app.gui.state import current_page, get_client, set_param


def safe_exit() -> None:
    """Stop this Streamlit process, and nothing else: never the API, the worker or a port."""
    os.kill(os.getpid(), signal.SIGTERM)


def _sidebar() -> str:
    names = list(texts.PAGE_NAMES)
    page = st.sidebar.radio(
        texts.PAGE_LABEL,
        names,
        index=names.index(current_page()),
        format_func=lambda name: texts.PAGE_NAMES[name],
    )
    set_param("page", page)
    if st.sidebar.button(texts.EXIT):
        st.sidebar.info(texts.EXITED)
        safe_exit()
    return page


def _banner(message: str) -> None:
    st.error(message)
    st.button(texts.RETRY)  # any click reruns the script, which asks the API again


def main() -> None:
    st.set_page_config(page_title=texts.APP_TITLE, layout="wide")
    try:
        client = get_client()
    except MissingApiKey as exc:
        _banner(texts.KEY_MISSING.format(detail=exc))
        return
    page = _sidebar()
    try:
        client.health()
        PAGES[page](client)
    except ApiDown:
        _banner(texts.API_DOWN)
    except ApiError as exc:
        st.error(exc.detail)


main()
