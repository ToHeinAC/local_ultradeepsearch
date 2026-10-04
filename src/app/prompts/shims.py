"""Shim texts, copied verbatim from hyperresearch `core/levers.py` (MIT, Copyright (c) 2026
Jordan Gibbs; see THIRD_PARTY_NOTICES.md). Do not edit: `compose_shims` must stay byte-equal to the
upstream composition. Shims carry posture only, never a number."""

SHIM_HEADER = (
    "## Run directives\n\n"
    "Auto-selected for this run in step 1; binding wherever they adjust "
    "a default in your prompt. Absent instructions here leave your "
    "prompt's defaults untouched.\n\n"
    "- register: {register}\n"
    "- inference depth: {inference_depth}\n"
)


REGISTER_DRAFTING: dict[str, str] = {
    "analyze": (
        "Default evaluative posture (this run confirms your prompt's defaults).\n"
        "Write authoritative analysis: commit to the positions the evidence\n"
        "supports, engage the strongest counterarguments explicitly, and rank\n"
        "when the query asks which option wins. No adjustment to your prompt."
    ),
    "teach": (
        "TEACH register. The reader is a motivated non-specialist who wants to\n"
        "UNDERSTAND this subject, not to receive a verdict. Voice: patient\n"
        "expert. Extend the primer discipline downward: subsections open with\n"
        "plain-language explanation too, and every term is defined at first\n"
        "use. A short glossary section is permitted where the vocabulary is\n"
        'heavy. Frame conclusions as "what the evidence supports" rather than\n'
        "rankings; a committed top-pick is NOT required. On contested points,\n"
        "present each side's best case fairly before saying which way the\n"
        "evidence leans. Worked examples and concrete analogies are encouraged\n"
        "wherever they make a mechanism click."
    ),
    "survey": (
        "SURVEY register. The product is a map of the field, not a verdict.\n"
        "Voice: neutral cartographer. Coverage and organization outrank\n"
        "argument: name every school of thought, position each approach\n"
        "relative to the others, and prefer comparison tables wherever three\n"
        "or more entities share dimensions. A committed thesis or ranking is\n"
        "NOT required; where the field disagrees, chart the disagreement and\n"
        "attribute each position instead of resolving it. Keep evaluative\n"
        "asides brief and clearly sourced. Dramatic standalone sentences do\n"
        "not belong in this register."
    ),
    "advocate": (
        "ADVOCATE register. The report defends ONE thesis, named early and\n"
        "argued throughout. Every section either advances the thesis or\n"
        "disarms an objection to it. A steel-man treatment of the strongest\n"
        "opposing case is MANDATORY: state the objection at full strength,\n"
        "then answer it with evidence. Commitment is maximal; hedges belong\n"
        "only on unverified secondhand specifics, never on the thesis or its\n"
        "supporting chain."
    ),
}

REGISTER_CRITICS: dict[str, str] = {
    "analyze": (
        "Default evaluative posture (this run confirms your prompt's defaults).\n"
        "Commitment checks apply as written: a draft that hedges where its own\n"
        "evidence supports a stronger claim is a finding."
    ),
    "teach": (
        "TEACH register. Adjust your failure modes: hedged neutrality on\n"
        "genuinely contested points is CORRECT in this register, not a\n"
        "finding. Instead, flag places where a view is represented unfairly\n"
        "or a mechanism is asserted without being explained. Do not demand a\n"
        "committed ranking; the instruction-following standard is that the\n"
        "reader comes away understanding the subject. Findings that would\n"
        "convert patient explanation into verdict-first argument are wrong\n"
        "for this run."
    ),
    "survey": (
        "SURVEY register. Adjust your failure modes: the draft is a map, not\n"
        "an argument. Do not flag the absence of a committed thesis or\n"
        "ranking. DO flag: a school of thought the corpus supports that the\n"
        "draft omits, imbalanced coverage (one approach detailed, a peer\n"
        "approach skimmed), unattributed resolution of a live disagreement,\n"
        "and comparisons left in prose that belong in tables."
    ),
    "advocate": (
        "ADVOCATE register. Tighten the commitment checks: the draft defends\n"
        "one thesis, and the quality of its steel-man is your central\n"
        "standard. A weakly stated or strawmanned opposing case is a critical\n"
        "finding. Hedging on the thesis or its supporting chain is a finding;\n"
        "hedging on unverified secondhand specifics is honesty and stays."
    ),
}

REGISTER_POLISH: dict[str, str] = {
    "analyze": (
        "Default evaluative posture (this run confirms your prompt's defaults).\n"
        "Hedge-striking and the one-kicker-per-section budget apply as\n"
        "written."
    ),
    "teach": (
        "TEACH register. Do NOT strike hedges to force commitment: on\n"
        "contested points, even-handed language is correct here, and the\n"
        "hedge rule applies only to hedge-stacks and filler softeners.\n"
        "Kicker budget is effectively zero: fold dramatic standalone\n"
        "sentences into plain prose. Preserve primers, definitions, worked\n"
        "examples, and glossary material; they are the product, not filler."
    ),
    "survey": (
        "SURVEY register. Do NOT strike hedges to force commitment: neutral,\n"
        "attributed language on disagreements is correct here, and the hedge\n"
        "rule applies only to hedge-stacks and filler softeners. Kicker\n"
        "budget is effectively zero: fold dramatic standalone sentences into\n"
        "plain prose. Preserve coverage material and comparison tables even\n"
        "where they read as unopinionated; the map is the product."
    ),
    "advocate": (
        "ADVOCATE register. Hedge-striking at maximum: any softener on the\n"
        "thesis or its supporting chain goes, per the existing evidence-backed\n"
        "rule. Preserve the steel-man section intact; trimming the opposing\n"
        "case's best evidence is forbidden."
    ),
}

INFERENCE_RESEARCH: dict[str, str] = {
    "standard": (
        "Standard depth (this run confirms your prompt's defaults). Follow the\n"
        "normal sourcing playbook."
    ),
    "surface": (
        "SURFACE depth. The question is answerable from authoritative\n"
        "consensus sources. Prefer canonical reviews, primary standards, and\n"
        "high-citation papers; stop when they agree. Do not open rabbitholes\n"
        "or chase gray literature; a consistent consensus answer ends the\n"
        "search."
    ),
    "deep": (
        "DEEP inference. The surface web underdetermines this question, so the\n"
        "value is in what takes digging: gray literature, regulatory and\n"
        "financial filings, conference posters, theses, and named-practitioner\n"
        "forum or mailing-list posts are all in scope. Treat audited absences\n"
        "as findings: when a figure SHOULD be published and is not, search for\n"
        "it hard, then record the absence itself with what you searched.\n"
        "Lower-authority sources are usable WITH their provenance and\n"
        "reliability tagged in the note. Spend toward the top of your assigned\n"
        "budgets when a lead is genuinely load-bearing."
    ),
}

INFERENCE_DRAFTING: dict[str, str] = {
    "standard": ("Standard depth (this run confirms your prompt's defaults)."),
    "surface": (
        "SURFACE depth. Report the consensus; do not construct novel inference\n"
        "chains beyond what sources state directly."
    ),
    "deep": (
        "DEEP inference. Explicit inference chains are licensed WITH\n"
        "provenance stated: when no source states X but sourced claims A and B\n"
        "jointly imply it, say so in exactly that shape, citing A and B. Never\n"
        "present an inference as a sourced fact; the citation checker verifies\n"
        "number-bearing sentences pair-by-pair and an inference dressed as a\n"
        "quote or finding will be flagged. Audited absences (what the record\n"
        "should contain but does not) are reportable findings."
    ),
}

INFERENCE_CRITICS: dict[str, str] = {
    "standard": ("Standard depth (this run confirms your prompt's defaults)."),
    "surface": (
        "SURFACE depth. Flag any inference chain that goes beyond what the\n"
        "cited sources state; this run stays on consensus ground."
    ),
    "deep": (
        "DEEP inference. Inferential syntheses are expected in this draft; do\n"
        "not flag inference itself. DO flag inference without provenance\n"
        "discipline: any derived claim that fails to name the sourced claims\n"
        "it derives from, and any audited absence asserted without stating\n"
        "what was searched."
    ),
}
