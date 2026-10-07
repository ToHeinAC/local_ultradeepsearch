"""The pages: one `render(client)` per name in `texts.PAGE_NAMES`."""

from collections.abc import Callable
from typing import Any

import streamlit as st

from app.gui import texts

Render = Callable[[Any], None]


def _placeholder(name: str) -> Render:
    def render(client: Any) -> None:
        st.header(texts.PAGE_NAMES[name])

    return render


PAGES: dict[str, Render] = {name: _placeholder(name) for name in texts.PAGE_NAMES}
