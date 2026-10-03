"""Prompts of Phase 2 (PRD M5). Numbers come from `config/profiles.toml` through the placeholders
(PRD AD3); nothing here hardcodes a limit. Fetched text enters only inside untrusted fences."""

# ---- step 1: decomposition ------------------------------------------------------------------

DECOMPOSE_SYSTEM = (
    "You prepare a research run. You receive an approved research brief and break it down into "
    "the parts a search plan and a report must cover, before any research happens. Reply with "
    "JSON that matches the schema, nothing else.\n"
    "\n"
    "- entities: every named thing the brief asks about (organisations, laws, standards, "
    "products, places, concepts), each with the fields the brief wants described.\n"
    "- required_formats: output forms the brief asks for (table, ranked list, scenario matrix).\n"
    "- required_sections: sections the brief explicitly demands.\n"
    "- time_horizons: forward-looking spans the brief names.\n"
    "- time_periods: backward-looking, specific reporting periods the brief names (a quarter, a "
    "fiscal year, a date), each with the primary source that would hold the figures and its "
    "issuer. Leave empty if the brief names none.\n"
    "- scope_conditions: restrictions the brief puts on the answer.\n"
    "- domains: one or more of tech_standards, regulation_de_eu, science_medicine, "
    "business_markets that the question belongs to.\n"
    "- tier_recommendation: light for a bounded question with a clear answer, full for deep "
    "analysis, contested evidence or a defended thesis. When uncertain, full. Give the reason "
    "in tier_rationale, two or three sentences.\n"
    "- modality: collect (enumerate per entity), synthesize (defended thesis), compare "
    "(proportionate depth plus a recommendation) or forecast (predictions grounded in past and "
    "present).\n"
    "- levers.voice: the voice of the report: teach, survey, analyze or advocate. Choose "
    "analyze unless the brief's wording strongly points elsewhere, and then say so in "
    "voice_confidence (high or low).\n"
    "- levers.domain_notes: two or three sentences on how to source this topic, what counts as "
    "strong evidence and which recency window matters.\n"
    "- levers.inference_depth: surface (a bounded question), standard, or deep (the answer "
    "likely lives in gray literature, filings or in what sources leave out).\n"
    "\n"
    "Write every free-text value in the language of the brief. Omit nothing the brief names "
    "explicitly: a missed item cannot be searched for later."
)

HEADINGS_FIXED = (
    "The report structure is fixed by a template; leave required_section_headings empty."
)

HEADINGS_DERIVE = (
    "Also write required_section_headings: the report's section headings, in order, as plain "
    "text without # marks, between {min_headings} and {max_headings} of them, each phrased as a "
    "declarative topic in the report language. Together they must cover every research "
    "question. Never use the words Sources, Quellen, Appendix or Anhang."
)

DECOMPOSE_USER = (
    "Approved research brief (binding):\n"
    "{brief}\n"
    "\n"
    "Research questions of the brief, numbered:\n"
    "{questions}\n"
    "\n"
    "Report language: {language}. Response format: {response_format}.\n"
    "{headings}\n"
    "{feedback}"
)

DECOMPOSE_FEEDBACK = (
    "\nA coverage check found phrases of the brief that the breakdown does not cover well. "
    "Add entities, time periods or scope conditions so that each is covered, and keep what was "
    "right:\n{gaps}\n"
)

MATRIX_SYSTEM = (
    "You check that a breakdown of a research brief covers the brief. Walk through the brief "
    "phrase by phrase. List every significant noun phrase, proper noun, technical term and "
    "category name, verbatim as it appears in the brief. For each, name the ids of the items "
    "that cover it (the ids are given), and set scope_ok to false if the items interpret the "
    "phrase more narrowly than its natural scope, or if the phrase also has another plausible "
    "meaning that no item covers. Reply with JSON that matches the schema, nothing else."
)

MATRIX_USER = "Approved research brief:\n{brief}\n\nItems (id: kind: text):\n{items}"

# ---- shims: posture rendered into later prompts ---------------------------------------------

VOICE_TEXT = {
    "teach": "Explain: build understanding step by step, define terms, show how things work.",
    "survey": "Survey: cover the ground evenly and report what is known without a verdict.",
    "analyze": "Analyze: weigh the evidence, commit to an assessment and show why.",
    "advocate": "Advocate: make the case for a course of action and answer the objections.",
}

DEPTH_TEXT = {
    "surface": "A bounded question: authoritative consensus sources are enough.",
    "standard": "Standard depth: primary and secondary sources, with the main objections.",
    "deep": "Deep: the answer may lie beyond the first search results; look for primary documents.",
}

SHIM_RESEARCH = (
    "Research posture. Sourcing: {domain_notes}\n"
    "Inference depth: {depth}\n"
    "Prefer primary and authoritative sources over commentary about them."
)

SHIM_DRAFTING = (
    "Drafting posture. Voice: {voice}\nEvidence norms and recency: {domain_notes}\n{owner_register}"
)

SHIM_POLISH = "Polish posture. Keep the voice of the report. Voice: {voice}\n{owner_register}"

OWNER_REGISTER = (
    "The owner asked for this register in the brief: {register}. It takes precedence over the "
    "voice above."
)

# ---- step 2.1: the search plan --------------------------------------------------------------

PLAN_SYSTEM = (
    "You plan the web and literature searches of a research run from several independent "
    "perspectives, called lenses. Reply with JSON that matches the schema, nothing else.\n"
    "\n"
    "Lenses:\n"
    "- A, breadth: the core facts of an item; recent developments; every named entity or "
    "sub-concept in it. No item may stay uncovered.\n"
    "- B, scholarly: queries for scholarly databases: canonical and foundational works, "
    "authoritative reports, the original studies and primary data that commentary builds on.\n"
    "- C, adversarial: criticism, limitations, failure cases, competing frameworks and "
    "dissenting experts; at least one query per major item that argues against the "
    "emerging consensus.\n"
    "- D, period-pinned: for every time period an item names, a query for the primary document "
    "of exactly that period (filing, press release, statutory accounts, official release), "
    "never commentary about it.\n"
    "\n"
    "Rules:\n"
    "- item is one of the given item ids; lens is A, B, C or D.\n"
    "- query is a short search-engine query of a few keywords, not a sentence.\n"
    "- Plan between {min_queries} and {max_queries} queries in total, at least "
    "{adversarial_min} of them with lens C, and at least one for every item.\n"
    "- Write each query in the language in which the best sources on it are written."
)

PLAN_USER = (
    "Approved research brief:\n"
    "{brief}\n"
    "\n"
    "Domains: {domains}.\n"
    "{research_shim}\n"
    "\n"
    "Items (id: kind: text):\n"
    "{items}\n"
)

PLAN_MORE_USER = (
    "\nThe plan so far:\n"
    "{plan}\n"
    "\n"
    "It lacks the following: {missing}\n"
    "Return only additional queries that fix this, not the ones already planned."
)

PLAN_MISSING_ITEMS = "no query for the items {items}."
PLAN_MISSING_ADVERSARIAL = "{count} more queries with lens C."
PLAN_MISSING_PERIODS = "a lens D query for the periods {items}."
PLAN_MISSING_TOTAL = "{count} more queries in total."

# ---- step 2: the second wave for thin items -------------------------------------------------

WAVE2_SYSTEM = (
    "You plan follow-up searches for a research run whose first searches left some items "
    "thinly covered. Reply with JSON that matches the schema, nothing else.\n"
    "\n"
    "For each of the given items write between {min_queries} and {max_queries} new queries that "
    "approach it differently from the queries already tried: other terms, another language, "
    "a named authority, a primary document. Never repeat a query already tried. Each query is "
    "a short search-engine query of a few keywords. lens is A (breadth), B (scholarly), C "
    "(adversarial) or D (period-pinned)."
)

WAVE2_USER = (
    "Approved research brief:\n"
    "{brief}\n"
    "\n"
    "Thin items (id: kind: text):\n"
    "{items}\n"
    "\n"
    "Queries already tried for them:\n"
    "{tried}\n"
)
