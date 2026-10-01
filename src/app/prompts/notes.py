"""Prompts for turning a fetched source into a summary and verified claims."""

from app.prompts.untrusted import UNTRUSTED_NOTE

EXTRACT_SYSTEM = (
    "You extract verifiable claims from one source text for a research report.\n"
    "\n"
    "Rules:\n"
    "- Reply with JSON that matches the schema, nothing else.\n"
    "- summary: a faithful summary of what the source says that matters for the research focus. "
    "Write it in the language of the research focus.\n"
    "- claims: atomic factual statements the source makes that help answer the research "
    "questions. Skip filler, navigation text and advertising. If the source contains nothing "
    "relevant, return an empty list.\n"
    "- claim, stance_target and scope_conditions: in the language of the research focus.\n"
    "- quoted_support: copy ONE contiguous passage of at most two sentences from the source text, "
    "character for character, in the source's own language. Never paraphrase, translate, shorten "
    'with "..." or join passages. If you cannot copy a supporting passage exactly, leave the '
    "claim out.\n"
    "- stance: whether the source supports, refutes or is neutral about the stance_target.\n"
    "- evidence_type: one of empirical, theoretical, anecdotal, expert-opinion, statistical, "
    "legal, historical.\n"
    "- numbers: figures exactly as written in the source, with their unit. entities: named "
    "organisations, laws, standards and people. time_period and region: only if the source "
    "states them.\n"
    "- confidence: high if the source states it plainly with evidence, medium if it is hedged, "
    "low if it is speculative or unclear.\n"
    "\n" + UNTRUSTED_NOTE
)

EXTRACT_USER = (
    "Research focus: {title}\n"
    "Research questions:\n"
    "{questions}\n"
    "\n"
    "Source part {index} of {total}. Length class: {length_class}.\n"
    "Write the summary in {summary_hint}. Return at most {claim_limit} claims.\n"
    "\n"
    "{source}"
)

SUMMARY_MERGE_SYSTEM = (
    "You merge partial summaries of one document into a single faithful summary. Use only what "
    "the partial summaries say and do not add facts. Write in the language of the research "
    'focus. Reply with JSON: {{"summary": "..."}}.\n'
    "\n" + UNTRUSTED_NOTE
)

SUMMARY_MERGE_USER = (
    "Research focus: {title}\n"
    "\n"
    "Write the final summary in {summary_hint}.\n"
    "\n"
    "Partial summaries, in document order:\n"
    "{parts}"
)
