"""Prompts of Phase 1 (PRD M4). Numbers come from `config/profiles.toml` through the placeholders
(PRD AD3); nothing here hardcodes a limit."""

from app.prompts.untrusted import UNTRUSTED_UPLOAD

OCR_PAGE = "Free OCR."

UPLOAD_FACTS_SYSTEM = (
    "You read part of a file the user uploaded as background for a research question and list "
    "the facts in it that could matter for that question. Each fact is one self-contained "
    "sentence, in the file's own wording or a faithful short paraphrase, with the page it "
    "appears on. List only what the text states: no interpretation, nothing from outside the "
    "text. Numbers, dates, names and units stay exact. " + UNTRUSTED_UPLOAD
)

UPLOAD_FACTS_USER = (
    "Research question (the user's first message):\n{question}\n\n"
    "File: {name}. The text below is pages {first} to {last}; each page starts with a "
    "[Page N] marker. List at most {max_facts} facts, most relevant first, and give each "
    "fact the number N of its page.\n\n"
    "{fenced}"
)

UPLOAD_DIGEST_SYSTEM = (
    "You condense facts extracted from the user's uploaded files into a short context digest "
    "for a research brief. Keep the facts most relevant to the research question, merge "
    "duplicates, and keep numbers, dates, names and units exact. Never add anything that is "
    "not in the facts. Every item names the file and the page it came from exactly as given "
    "in the facts. " + UNTRUSTED_UPLOAD
)

UPLOAD_DIGEST_USER = (
    "Research question:\n{question}\n\n"
    "Facts (one per line, as [file, page N] fact):\n{fenced}\n\n"
    "Return the items worth keeping, most relevant first. The digest is cut to {max_words} "
    "words, so the most important facts must come first."
)
