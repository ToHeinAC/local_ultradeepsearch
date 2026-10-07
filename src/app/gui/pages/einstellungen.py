"""Page "Einstellungen": the denylist, the doctor and the read-only role to model map (PRD M7)."""

from typing import Any

import streamlit as st

from app.gui import texts

Json = dict[str, Any]


def render(client: Any) -> None:
    st.header(texts.PAGE_NAMES["einstellungen"])
    _denylist(client)
    report = client.doctor()
    _doctor(report["checks"])
    _roles(report["roles"])


def _denylist(client: Any) -> None:
    st.subheader(texts.DENYLIST)
    st.caption(texts.DENYLIST_HELP)
    current = "\n".join(client.get_denylist()["terms"])
    text = st.text_area(texts.DENYLIST, value=current, key="denylist", label_visibility="collapsed")
    if st.button(texts.SAVE):
        terms = [line.strip() for line in text.splitlines() if line.strip()]
        saved = client.put_denylist(terms)
        st.success(texts.DENYLIST_SAVED.format(n=len(saved["terms"])))


def _doctor(checks: list[Json]) -> None:
    st.subheader(texts.DOCTOR)
    st.markdown(
        "\n".join(
            f"- `{texts.LEVEL_TAGS[c['level']]}` **{c['name']}**: {c['detail']}" for c in checks
        )
    )


def _roles(roles: list[Json]) -> None:
    st.subheader(texts.ROLES)
    role, model, endpoint = texts.ROLE_COLUMNS
    st.dataframe(  # pyright: ignore[reportUnknownMemberType]
        [{role: r["role"], model: r["model"], endpoint: r["endpoint"]} for r in roles],
        hide_index=True,
    )
