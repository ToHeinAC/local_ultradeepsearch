"""Prompts of Phase 2 (PRD M5), adapted from the hyperresearch step skills (MIT, Copyright (c) 2026
Jordan Gibbs; see THIRD_PARTY_NOTICES.md). Numbers come through placeholders (PRD AD3). Every
system prompt has its own first line, so tests can tell the calls apart."""

# ---- step 1: decomposition ------------------------------------------------------------------

DOMAIN_DESCRIPTIONS: dict[str, str] = {
    "tech_standards": "technical standards, engineering, norms and safety requirements",
    "regulation_de_eu": "German and EU law, regulation, authorities and official procedures",
    "science_medicine": "natural sciences, medicine, clinical and scientific evidence",
    "business_markets": "companies, markets, finance, prices and economic statistics",
}

DECOMPOSE_SYSTEM = (
    "You decompose an approved research brief into its atomic items before any research starts.\n"
    "The brief is binding. Read it end to end and extract every discrete thing it names:\n"
    "- sub_questions: every explicit or implicit question the report must answer;\n"
    "- entities: every named organisation, product, place, person, category or concept, with the "
    "fields the brief wants for each;\n"
    "- required_formats: formats the brief asks for (table, ranked list, timeline, ...);\n"
    "- required_sections: sections the brief explicitly asks for;\n"
    "- time_horizons: forward-looking spans;\n"
    "- time_periods: specific past reporting periods that have a primary source (a fiscal year, "
    "a quarter, a dated event), with that source and its issuer;\n"
    "- scope_conditions: limits the brief sets.\n"
    "Omit nothing the brief names explicitly, even if it feels redundant.\n\n"
    "section_headings: the second-level (##) headings of the report, in order. If a fixed "
    "template is given, copy its headings exactly. Otherwise derive them: one heading per "
    "enumerated ask of the brief, in its order; else one per entity the brief asks to "
    "discuss; else one per "
    "sub-question, phrased as a topic. Between {min_sections} and {max_sections} headings, in "
    "the report language, without numbering, never a sources or appendix heading. "
    "section_weights: one relative weight per heading for its share of the words (normal is "
    "one, a central section more, a minor one less).\n\n"
    "domains: the source domains that fit the brief (zero or more of the listed ones). "
    "tier_recommendation: light for a clear, bounded question; full for deep analysis of "
    "conflicting evidence. The run's tier is already fixed; your recommendation is recorded "
    "only. tier_rationale: two or three sentences. modality: collect, synthesize, compare or "
    "forecast.\n\n"
    "levers.register, the report's voice from the brief's verb shape: teach (explain, help me "
    "understand), survey (overview, landscape, no verdict asked), analyze (evaluate, assess, "
    "which is best; the default) or advocate (should we, make the case). Deviate from analyze "
    "only on a strong signal and then set register_confidence to high; an explicit directive "
    "in the brief always wins. levers.domain_notes: two or three sentences on the sourcing "
    "strategy, what counts as strong evidence here, and the recency that matters. "
    "levers.inference_depth: surface (consensus suffices), standard (the default) or deep (the "
    "answer lies in gray literature, filings, inference over absences)."
)

DECOMPOSE_USER = (
    "Run tier: {tier}. Response format: {response_format}. Report language: {language}.\n\n"
    "{template_block}\n\n"
    "Source domains:\n{domains}\n\n"
    "The research brief:\n\n{brief}"
)

TEMPLATE_FIXED = (
    "Fixed template {name}: the report has exactly these headings, in order:\n{headings}"
)
TEMPLATE_DERIVE = "No fixed template: derive the headings from the brief."

COVERAGE_SYSTEM = (
    "You audit a decomposition of a research brief for coverage.\n"
    "Walk through the brief phrase by phrase and list every significant noun phrase, proper "
    "noun, technical term and category name, verbatim. For each phrase give the ids of the "
    "atomic items it maps to, whether the items keep the phrase's full natural scope (scope_ok; "
    "a broad phrase narrowed to one reading is not ok, and a phrase with two plausible readings "
    "needs both), and gap = true when no item covers it or the scope is narrowed. Add a short "
    "note on every gap."
)

COVERAGE_USER = "The research brief:\n\n{brief}\n\nThe atomic items:\n{items}"

REVISE_DECOMPOSITION_SYSTEM = (
    "You repair a decomposition of a research brief so that it covers the brief completely.\n"
    "Add the missing atomic items, broaden narrowed scope and add both readings of an ambiguous "
    "phrase, as the gaps below say. Keep everything else as it is, including the headings unless "
    "a gap requires a change. Return the whole corrected decomposition."
)

REVISE_DECOMPOSITION_USER = (
    "The research brief:\n\n{brief}\n\n"
    "The decomposition (JSON):\n{decomposition}\n\n"
    "The gaps found:\n{gaps}"
)

HEADINGS_SYSTEM = (
    "You write the second-level (##) headings of a research report.\n"
    "Return between {min_sections} and {max_sections} headings in the report language, in "
    "reading order, unique, without numbering and without a leading hash. Never a sources or "
    "appendix heading. Together they must cover every sub-question."
)

HEADINGS_USER = (
    "Report language: {language}.\n\n"
    "The headings proposed so far were rejected: {problem}\n\n"
    "Sub-questions:\n{sub_questions}\n\n"
    "The research brief:\n\n{brief}"
)

# ---- step 2.1: the search plan --------------------------------------------------------------

PLAN_SYSTEM = (
    "You plan the web and literature searches of a research run from several perspectives.\n"
    "For every atomic item write searches through these lenses:\n"
    "- breadth: the core facts of the item, recent developments, each named sub-concept;\n"
    "- depth: canonical and primary sources, foundational studies, authoritative reports, the "
    "original data that commentary is built on. {depth_note}\n"
    "- adversarial: criticism, limitations, failures, competing explanations, dissenting "
    "experts, the strongest case against the emerging consensus;\n"
    "{period_lens}"
    "Plan between {min_queries} and {max_queries} searches in total, at least {adversarial_min} "
    "of them adversarial, and at least one for every item. A search is a short query of a few "
    "keywords, the way an expert would type it, in the language most likely to find the best "
    "sources for it. Never put a person's private details into a query."
)

PLAN_DEPTH_SCHOLARLY = (
    "Depth searches go to scholarly databases: write them as plain topic keywords, no site: "
    "operators, no quotation marks."
)
PLAN_DEPTH_WEB = "Depth searches go to the web search; they may target official publishers."
PLAN_PERIOD_LENS = (
    "- period: for every time-period item, a search for the primary publication of exactly "
    "that period (the annual report, the statutory filing, the official release), not "
    "commentary about it.\n"
)

PLAN_USER = "The atomic items:\n{items}\n\nThe research brief:\n\n{brief}"

PLAN_REPAIR = (
    "\n\nYour previous plan had these problems:\n{problems}\n"
    "Return the complete plan again with the problems fixed."
)
