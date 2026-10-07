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
