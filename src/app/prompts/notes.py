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

ANALYSIS_MAP_SYSTEM = (
    "You read one part of a long source for a research report. Reply with JSON that matches the "
    "schema, nothing else.\n"
    "- key_points: the points of this part that matter for the research focus, each a full "
    "sentence, in the language of the research focus.\n"
    "- numbers: figures exactly as written, with their unit.\n"
    "- quotes: up to 5 short passages copied character for character from the source text, in "
    "its own language. Never paraphrase them.\n"
    "\n" + UNTRUSTED_NOTE
)

ANALYSIS_MAP_USER = (
    "Research focus: {title}\n"
    "Research questions:\n"
    "{questions}\n"
    "\n"
    "Source part {index} of {total}.\n"
    "\n"
    "{source}"
)

ANALYSIS_REDUCE_SYSTEM = (
    "You merge several partial readings of one long source into one partial reading. Keep every "
    "distinct point, remove repetition, keep figures exactly as written and keep quotes unchanged. "
    "Reply with JSON that matches the schema, nothing else.\n"
    "\n" + UNTRUSTED_NOTE
)

ANALYSIS_REDUCE_USER = "Research focus: {title}\n\nPartial readings, in document order:\n{parts}"

ANALYSIS_FINAL_SYSTEM = (
    "You write the analysis of one long source for a research report, from partial readings of "
    "it. Use only what the partial readings say. Reply with JSON that matches the schema, "
    "nothing else.\n"
    "- thesis: the source's main claim in one or two sentences.\n"
    "- methodology: how the source reached it (data, method, scope); empty if not stated.\n"
    "- key_findings: the findings that matter for the research questions, each a full sentence, "
    "with figures exactly as written.\n"
    "- load_bearing_citations: works the source itself relies on most.\n"
    "- caveats: limits and weaknesses the source or the partial readings reveal.\n"
    "- relevance_to_query and relevance: how directly it answers the research questions "
    "(load-bearing = central evidence, useful, tangential, not-relevant).\n"
    "- quotes: up to 10 passages copied unchanged from the partial readings' quotes.\n"
    "Write in the language of the research focus.\n"
    "\n" + UNTRUSTED_NOTE
)

ANALYSIS_FINAL_USER = (
    "Research focus: {title}\n"
    "Research questions:\n"
    "{questions}\n"
    "\n"
    "Partial readings, in document order:\n"
    "{parts}"
)
