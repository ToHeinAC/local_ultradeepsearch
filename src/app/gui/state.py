"""State the pages share: the API client, the page and the ids that the URL carries (so a reload
restores them), and the polling interval."""

from typing import Any

import streamlit as st

from app.client import ApiClient
from app.gui import texts

POLL_SECONDS = 5  # PRD M7 AC4: progress is polled at most this often
DEFAULT_PAGE = "neue_recherche"


def get_client() -> Any:
    """The client of this browser session (tests put a fake into the session state)."""
    if "client" not in st.session_state:
        st.session_state["client"] = ApiClient.from_env()
    return st.session_state["client"]


def current_page() -> str:
    page = st.query_params.get("page", DEFAULT_PAGE)
    return page if page in texts.PAGE_NAMES else DEFAULT_PAGE


def param(name: str) -> str | None:
    """A query parameter (`session`, `run`), or None."""
    return st.query_params.get(name)


def go(page: str, **params: str) -> None:
    """Open another page; the ids it needs travel in the URL, so a reload keeps them."""
    set_param("page", page)
    for name, value in params.items():
        set_param(name, value)
    st.rerun()


def set_param(name: str, value: str | None) -> None:
    if value is None:
        st.query_params.pop(name, None)
    else:
        st.query_params[name] = value
