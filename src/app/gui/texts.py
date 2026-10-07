"""The German texts of the GUI."""

APP_TITLE = "UltraDeepSearch"
PAGE_NAMES = {
    "neue_recherche": "Neue Recherche",
    "suchplan": "Suchplan",
    "laeufe": "Läufe",
    "bericht": "Bericht",
    "einstellungen": "Einstellungen",
}
PAGE_LABEL = "Seite"
EXIT = "Beenden"
EXITED = "Die Oberfläche wurde beendet. Dieses Fenster kann geschlossen werden."
API_DOWN = "API nicht erreichbar. Läuft `udr serve`?"
RETRY = "Erneut versuchen"
KEY_MISSING = "Kein API-Schlüssel: {detail}"

# ---- Neue Recherche -------------------------------------------------------------------------
QUESTION_LABEL = "Forschungsfrage"
UPLOADS = "Dateien (PDF, DOCX, MD, TXT)"
START = "Starten"
QUESTION_EMPTY = "Bitte zuerst eine Frage eingeben."
RESUME_TITLE = "Unfertige Sitzungen"
RESUME = "Fortsetzen"
NO_TITLE = "ohne Titel"
BUSY = "Das Modell arbeitet … (die Seite aktualisiert sich selbst)"
ROUND = "Runde {n} von {max}"
CHECKLIST = "Checkliste"
ANSWER_HINT = "Vorschlag bearbeiten, leeren (= weiß nicht) oder so lassen (= annehmen)."
NOTE = "Weitere Hinweise"
GENUG = "genug (keine weiteren Fragen)"
SEND = "Antworten senden"
UPLOAD_MORE = "Weitere Dateien hinzufügen"
UPLOAD_SEND = "Hochladen"
UPLOAD_LINE = "{name} ({kind}, {pages} Seiten, {stage})"
OFFER_TITLE = "Fertiger Prompt erkannt"
STRENGTHEN = "Verstärken"
INSTALL = "Unverändert übernehmen"
BRIEF_TITLE = "Briefing"
BRIEF_TAKE = "Text übernehmen"
BRIEF_EDITED = "Der Text wurde geändert: erst übernehmen, dann freigeben."
RECOMMENDATION = "Empfehlung: **{tier}** ({fmt})"
TIER_NAMES = {"light": "Lite", "full": "Full"}
TIER = "Tiefe"
FULL_LATER = "Full: ab M8 verfügbar."
SETTINGS_TITLE = "Einstellungen"
TEMPLATE = "Vorlage"
TEMPLATE_SECTIONS = "Abschnitte: {sections}"
TEMPLATE_UPLOAD = "Eigene Vorlage (Markdown)"
TEMPLATE_UPLOAD_SEND = "Vorlage hochladen"
REPORT_LANGUAGE = "Berichtssprache (zwei Buchstaben)"
RESPONSE_FORMAT = "Antwortformat"
RESPONSE_FORMATS = ("short", "structured", "argumentative")
SETTINGS_TAKE = "Einstellungen übernehmen"
SUMMARIZE_MODEL = "Zusammenfassungsmodell"
SUMMARIZE_DEFAULT = "Standard"
SUMMARIZE_MODELS = ("gemma4:e4b", "gemma4:e2b")
CAP_ON = "Tavily-Budget begrenzen"
CAP = "Tavily-Credits für diesen Lauf"
APPROVE = "Freigeben"
REVISE = "Überarbeiten"
FEEDBACK = "Was soll geändert werden?"
SAVE = "Speichern"
STALE = "Das Briefing hat sich geändert. Die Seite wurde neu geladen; bitte erneut prüfen."
SAVED = "Entwurf gespeichert: {path}"
APPROVED = "Freigegeben: Lauf {run_id}."
TO_PLAN = "Zum Suchplan"

# ---- Suchplan -------------------------------------------------------------------------------
NO_PLAN_RUNS = "Kein Lauf wartet auf die Freigabe eines Suchplans."
OPEN = "Öffnen"
PLAN_NONE = "Zu diesem Lauf gibt es noch keinen Suchplan."
PLAN_CLOSED = "Der Plan ist nicht zur Freigabe offen (Status: {status}); er wird nur angezeigt."
PLAN_BLOCKED = "{id}: {query} - gesperrt ({reason})"
PLAN_HELP = (
    "Anfragen bearbeiten, Zeilen löschen oder neue ergänzen (Spalte `id` leer lassen). "
    "Geänderte Anfragen werden erneut geprüft (Denylist, Bereinigung)."
)
PLAN_CHECK = "Änderungen prüfen"
PLAN_STALE = "Der Plan hat sich geändert. Er wurde neu geladen; bitte erneut prüfen."
PLAN_COLUMNS = {
    "id": "id",
    "item": "Punkt",
    "lens": "Linse",
    "kind": "Art",
    "original": "Anfrage",
    "sent": "Gesendet",
    "removed": "Entfernt",
    "blocked": "Gesperrt",
}

# ---- Läufe and Bericht ----------------------------------------------------------------------
NO_RUNS = "Noch keine Läufe."
RUN = "Lauf"
HISTORY = "Verlauf"
PENDING = "Offene Freigaben"
PENDING_BRIEF = "- Lauf {id} ({title}) von **{by}** wartet auf die Freigabe des Briefings."
PENDING_PLAN = "- Lauf {id} ({title}) wartet auf die Freigabe des Suchplans."
PENDING_SESSION = "- Sitzung {id} ({title}) von **{by}** wartet auf die Entscheidung."
APPROVE_BRIEF = "Briefing freigeben ({id})"
CHECK_PLAN = "Plan prüfen ({id})"
OPEN_SESSION = "Öffnen ({id})"
M_STATUS = "Status"
M_STEP = "Schritt"
M_ELAPSED = "Dauer"
M_SOURCES = "Quellen"
M_CREDITS_RUN = "Tavily-Credits (Lauf)"
M_CREDITS_MONTH = "Tavily-Credits (Monat)"
M_WARNINGS = "Warnungen"
STEP_LINE = "**Schritt {step}** - {status} ({start} bis {end})"
STEP_RUNNING = "läuft"
STEP_DONE = "fertig"
EVENTS = "Ereignisse"
OUTBOUND = "Outbound-Log anzeigen"
CANCEL = "Abbrechen"
RESUME_RUN = "Fortsetzen"
DELETE = "Löschen"
DELETE_CONFIRM = "Wirklich löschen (Lauf, Dateien, Prüfpunkte)"
TO_REPORT = "Zum Bericht"
REPORT_NOT_READY = "Der Bericht ist noch nicht fertig (Status: {status})."
GATE_PASSED = "Gate bestanden."
GATE_FAILED = "Gate nicht bestanden: {checks}"
DOWNLOAD = "{fmt} herunterladen"
FORMAT_MISSING = "{fmt} nicht verfügbar."
DOCX_HINT = "Für DOCX wird pandoc benötigt."
NO_REPORT_RUNS = "Noch kein Lauf mit Bericht."
BIG_REPORT_CHARS = 100_000
