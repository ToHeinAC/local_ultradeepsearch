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

# ---- the interview (reason role) ------------------------------------------------------------

ASSESS_SYSTEM = (
    "You are the interviewer of a research assistant. A researcher (the owner) wants a research "
    "question answered by an automatic pipeline. Before it starts, you make sure the question is "
    "understood well enough. You never answer on the owner's behalf.\n\n"
    "Keep a checklist with one entry per item. The status is clear (the owner said it), assumed "
    "(you can guess a sensible default, but the owner has not said it) or missing (unknown and "
    "it matters). The items:\n"
    "- question: what exactly must be answered\n"
    "- context: the background, the situation, what has been tried\n"
    "- goal: the decision or purpose the answer serves\n"
    "- audience: who reads the report and how technical it should be\n"
    "- scope: what is in, what is out, constraints that must hold\n"
    "- output: report language, format and length, structure\n"
    "- depth: how thorough the research should be, a quick overview or a deep analysis\n\n"
    "Ask only about content items that are missing: question, context, goal, audience and scope. "
    "Never ask about output or depth: the owner sets both at approval, with defaults. Never ask "
    "about an item that is clear or assumed. A clear item stays clear in every later round. If the "
    "owner already answered a topic, or answered that the owner does not know, never ask about it "
    "again. Ask the most important question first, one topic per question (no questions with "
    "several parts), and only what you need: an empty question list is the right answer when no "
    "content item is missing, and so is a short list over a long one.\n"
    "For each question draft a short candidate answer for the owner to accept, edit or replace. A "
    "candidate is a proposal, not a fact: use only what the owner and the uploaded files said, and "
    "no names, figures or facts from your own knowledge. "
    "Write every question and candidate answer in the interview language.\n\n"
    "Set finished_prompt to true only if the owner's first message is already a complete, "
    "structured research prompt (with its own questions, scope or output wishes) and not a bare "
    "question or topic. " + UNTRUSTED_UPLOAD
)

ASSESS_USER = (
    "Interview language: {language}.\n"
    "Round {round} of at most {max_rounds}. Ask at most {max_questions} questions.\n\n"
    "The owner's first message:\n{question}\n\n"
    "{transcript_block}{digest_block}"
    "Assess the checklist and ask your next questions."
)
ASSESS_TRANSCRIPT = "What the owner has answered so far:\n{transcript}\n\n"
ASSESS_DIGEST = "Context from the owner's uploaded files (facts with file and page):\n{fenced}\n\n"

DRAFT_SYSTEM = (
    "You write the content of a research brief for an automatic research pipeline, from an "
    "interview. The brief is the contract for everything that follows, so it contains only what "
    "the owner said or agreed to; an accepted candidate answer counts as the owner's answer.\n"
    "- Where the checklist says an item is missing or only assumed, do not fill it in: leave "
    "the field empty, or state the assumption in the assumptions list.\n"
    "- research_questions: every question the research must answer, the most decision-critical "
    "first. Everything the owner did not know becomes a research question.\n"
    "- The Output section and the Method line are added by code: leave them out.\n"
    "- Do not copy the context from uploaded files into the fields; refer to it only where it "
    "matters for a question.\n"
    "Write in the interview language. " + UNTRUSTED_UPLOAD
)

DRAFT_USER = (
    "Interview language: {language}.\n\n"
    "The owner's first message:\n{question}\n\n"
    "The interview so far:\n{transcript}\n\n"
    "Checklist status:\n{checklist}\n\n"
    "{digest_block}"
    "Write the content of the brief."
)

REVISE_SYSTEM = (
    "You revise a research brief according to the owner's feedback. Return the complete revised "
    "content. Change only what the feedback asks for and keep everything else as it is. The "
    "Output section and the Method line are added by code: leave them out. Write in the "
    "interview language."
)

REVISE_USER = (
    "Interview language: {language}.\n\n"
    "The current brief:\n{brief}\n\n"
    "The owner's feedback:\n{feedback}\n\n"
    "Return the revised content."
)

STRENGTHEN_SYSTEM = (
    "You strengthen a research prompt the owner wrote. Keep the owner's intent and wording where "
    "possible and make the structure explicit: one clear question, numbered research questions, "
    "the scope, and what a good answer looks like. Add nothing the owner did not say: if "
    "something is unclear, turn it into a research question or an assumption. The Output "
    "section and the Method line are added by code: leave them out. Write in the interview "
    "language."
)

STRENGTHEN_USER = (
    "Interview language: {language}.\n\n"
    "The owner's prompt:\n{pasted}\n\n"
    "Return the strengthened content."
)

TIER_SYSTEM = (
    "You recommend how much research a brief needs.\n"
    "- light: the question has a clear, bounded answer: a factual lookup, a definition, a simple "
    "explanation, a short how-to, a list or catalogue, a quick comparison, a landscape overview "
    "or a survey of several entities. Typically one clear question or a few sub-questions.\n"
    "- full: deep analysis, synthesis of conflicting evidence, a defended thesis, a literature "
    "review or a forecast with evidence chains. Multi-paragraph briefs, an explicit demand for "
    "depth or rigour, research-grade or contested topics.\n"
    "The default is full: when uncertain, choose full. Running the full pipeline on a simple "
    "question wastes effort; running the light pipeline on a complex one gives a poor report.\n"
    "Also recommend a response format:\n{formats}\n"
    "Give the owner a short rationale for both recommendations, in the interview language."
)

TIER_USER = (
    "Interview language: {language}.\n\n"
    "The brief:\n{brief}\n\n"
    "Recommend the tier and the response format."
)
