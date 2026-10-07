# GUI

Streamlit front end in German (PRD M7). It is a pure client of the REST API ([api.md](api.md)):
`src/app/gui/` imports from the application only `app.client` (`tests/test_layer_rules.py`), so it
could run on another machine than the service.

## Start

| Task | Command |
|---|---|
| Key (once) | `uv run udr apikey create --name gui --self-approve`, then `UDR_GUI_API_KEY=…` in `.env` |
| GUI | `uv run udr gui` (Streamlit on `127.0.0.1:8540`, `UDR_GUI_PORT`; the API at `UDR_API_URL`, default `http://127.0.0.1:8541`) |

`udr serve` and `udr worker` must run. `udr gui` refuses to start without the key. It passes
`--server.address 127.0.0.1`, `--server.headless true` and `--browser.gatherUsageStats false`;
`.streamlit/config.toml` holds the same values. From another machine use an SSH tunnel
(`ssh -L 8540:127.0.0.1:8540 -L 8541:127.0.0.1:8541 host`).

## Pages

| Page | What it does |
|---|---|
| Neue Recherche | Question and uploads; resume an unfinished session; rounds with editable candidate answers, the checklist, "genug"; a pasted finished prompt (verstärken or unverändert). The decision shows the exact brief (editable; an edit must be taken over before it can be approved), the recommendation, template choice and preview, language, format, `e4b`/`e2b`, the Tavily cap, and Freigeben / Überarbeiten / Speichern. Full is shown as "ab M8". |
| Suchplan | The plan as a table (`st.data_editor`): edit, delete, add. "Änderungen prüfen" sends it in the server's line format and the plan is checked again. A blocked query is listed as an error and disables Freigeben, as does an unchecked edit. |
| Läufe | History; runs and sessions waiting for an approval, with the creating key; one run's numbers (step, elapsed, sources, Tavily credits of run and month, warnings), step timeline, events, outbound log; cancel, resume, delete (confirmed). |
| Bericht | The rendered report (over 100 000 characters: one expander per section), the gate result, MD/PDF/DOCX downloads (a missing DOCX is a hint, see [research.md](research.md)). |
| Einstellungen | Denylist editor, doctor checks (`GET /v1/doctor`), read-only role to model map. |

## Behaviour

- **Hash of what is shown.** Freigeben sends the `brief_sha256` (or `plan_sha256`) the API returned
  with the text on screen; a 409 reloads the page with a message.
- **Reload.** `?page=…&session=…&run=…` carry the state; `go()` in `gui/state.py` changes page.
- **Polling.** A fragment (`st.fragment(run_every=POLL_SECONDS)`, 5 s) refreshes a busy session and
  an active run; events are read with a cursor, so none is read twice.
- **API down.** A banner with "Erneut versuchen"; a missing key is explained. Nothing raises.
- **Safe exit.** "Beenden" sends SIGTERM to `os.getpid()` only; never the API, the worker or a port.

## Code

`app/client.py` (`ApiClient`, `ApiDown`, `ApiError`, `plan_rows`, `rows_to_lines`; loopback URLs only,
the one file besides the outbound package that imports `httpx`), `gui/app.py` (shell),
`gui/state.py`, `gui/texts.py`, `gui/pages/` (`neue_recherche`, `brief_decision`, `suchplan`,
`laeufe`, `run_detail`, `bericht`, `einstellungen`).

## Tests

`tests/gui_rig.py` (a fake API and `AppTest`), `test_gui_shell.py`, `test_gui_brief.py`,
`test_gui_plan.py` (`st.data_editor` is replaced, AppTest cannot type into it), `test_gui_runs.py`,
`test_gui_settings.py`, `test_client.py`, `test_cli_gui.py`.
