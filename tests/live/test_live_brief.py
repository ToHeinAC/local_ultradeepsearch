"""Live check of Phase 1 with the real models: a question, a PDF with a scanned page, the rounds.

Run with `pytest -m live tests/live/test_live_brief.py -s`. It prints each view so the first real
session shows whether `assess` asks few enough questions and whether the `ocr` prompt reads the
scanned page. Nothing here uses the Internet.
"""

import io
from pathlib import Path

import pytest
from PIL import Image, ImageDraw, ImageFont
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas
from support import make_settings

from app import bootstrap
from app.adapters.ollama_instance import InstanceState
from app.brief.protocol import AnswerInput
from app.brief.service import SessionView
from app.brief.uploads import UploadFile
from app.events import JsonlEventSink

pytestmark = pytest.mark.live

FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
TEXT_PAGE = (
    "Rückbau des Forschungsreaktors: Das Genehmigungsverfahren dauert in der Regel mehrere Jahre."
)
SCANNED = ("Zwischenlager Ahaus", "Genehmigung 2026-0815", "Kosten 1250 Euro je Tonne")
MAX_ROUNDS = 8


def _scanned_page() -> bytes:
    image = Image.new("L", (1600, 500), 255)
    draw = ImageDraw.Draw(image)
    font = ImageFont.truetype(FONT, 64)
    for row, line in enumerate(SCANNED):
        draw.text((60, 40 + row * 140), line, fill=0, font=font)
    out = io.BytesIO()
    image.save(out, format="PNG")
    return out.getvalue()


def _pdf_with_a_scanned_page() -> bytes:
    out = io.BytesIO()
    page = canvas.Canvas(out)
    page.drawString(72, 750, TEXT_PAGE[:80])
    page.drawString(72, 730, TEXT_PAGE[80:])
    page.showPage()
    page.drawImage(ImageReader(io.BytesIO(_scanned_page())), 40, 400, width=500, height=156)
    page.showPage()
    page.save()
    return out.getvalue()


def _accept_everything(view: SessionView) -> list[AnswerInput]:
    return [AnswerInput(kind="accept") for _ in view.questions]


def test_a_real_session_reads_the_scanned_page_and_reaches_a_decision(tmp_path: Path) -> None:
    settings = make_settings(data_dir=tmp_path)
    rt = bootstrap.build_runtime(settings, JsonlEventSink(tmp_path / "events.jsonl"))
    assert rt.status.state in (InstanceState.ADOPTED, InstanceState.STARTED), rt.status.reason
    service = bootstrap.build_brief_service(rt)
    question = (
        "Wie lange dauert der Rückbau eines Forschungsreaktors in Deutschland und was kostet er?"
    )
    view = service.start(question, [UploadFile("rueckbau.pdf", _pdf_with_a_scanned_page())])
    rounds = 0
    while view.waiting_for == "questions" and rounds < MAX_ROUNDS:
        rounds += 1
        print(f"\nround {rounds}: {len(view.questions)} question(s), checklist {view.checklist}")
        for item in view.questions:
            print("  ", item)
        view = service.answer(view.session_id, _accept_everything(view))
    print(f"\nwaiting_for={view.waiting_for} error={view.error} uploads={view.uploads}")
    print(f"\n{view.brief_text}")
    assert view.error is None, view.error
    assert view.waiting_for == "decision", view.waiting_for
