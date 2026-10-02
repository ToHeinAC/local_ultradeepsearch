import struct
import zlib

import pytest
from support import encrypted_pdf, make_docx, make_pdf

from app.documents import (
    DocumentError,
    decode_text,
    docx_text,
    encode_png_gray,
    pdf_page_count,
    read_pdf,
    render_page_png,
    split_pages,
)

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def png_chunks(png: bytes) -> list[tuple[bytes, bytes]]:
    """(type, data) of every chunk, verifying each CRC on the way: an independent decoder."""
    assert png[:8] == PNG_SIGNATURE
    chunks: list[tuple[bytes, bytes]] = []
    position = 8
    while position < len(png):
        (length,) = struct.unpack(">I", png[position : position + 4])
        kind = png[position + 4 : position + 8]
        data = png[position + 8 : position + 8 + length]
        (crc,) = struct.unpack(">I", png[position + 8 + length : position + 12 + length])
        assert crc == zlib.crc32(kind + data), f"bad CRC in {kind!r}"
        chunks.append((kind, data))
        position += 12 + length
    return chunks


def decode_gray(png: bytes) -> tuple[int, int, list[bytes]]:
    chunks = dict(png_chunks(png))
    width, height, depth, color, _, _, _ = struct.unpack(">IIBBBBB", chunks[b"IHDR"])
    assert (depth, color) == (8, 0)  # 8-bit greyscale
    raw = zlib.decompress(chunks[b"IDAT"])
    assert len(raw) == height * (width + 1)
    rows = [raw[y * (width + 1) : (y + 1) * (width + 1)] for y in range(height)]
    assert {row[0] for row in rows} == {0}  # filter type "none"
    return width, height, [row[1:] for row in rows]


# ---- PNG encoder ----------------------------------------------------------------------------


def test_the_png_encoder_writes_a_valid_greyscale_image() -> None:
    pixels = bytes([0, 128, 255, 9, 10, 20, 30, 9])  # width 3, stride 4: the 4th byte is padding
    png = encode_png_gray(3, 2, pixels, stride=4)
    assert [kind for kind, _ in png_chunks(png)] == [b"IHDR", b"IDAT", b"IEND"]
    width, height, rows = decode_gray(png)
    assert (width, height) == (3, 2)
    assert rows == [bytes([0, 128, 255]), bytes([10, 20, 30])]


def test_the_png_encoder_rejects_a_buffer_that_is_too_short() -> None:
    with pytest.raises(ValueError, match="buffer"):
        encode_png_gray(4, 4, b"\x00" * 10, stride=4)


# ---- PDF ------------------------------------------------------------------------------------


def test_pdf_text_per_page_and_title() -> None:
    pdf = make_pdf(["Hello first page", "Second page text"], title="A Report")
    result = read_pdf(pdf)
    assert result.pages == ("Hello first page", "Second page text")
    assert result.title == "A Report"
    assert pdf_page_count(pdf) == 2


def test_pdf_page_text_is_stripped() -> None:
    assert read_pdf(make_pdf(["  padded  "])).pages == ("padded",)


def test_a_pdf_without_a_title_has_none() -> None:
    assert read_pdf(make_pdf(["x"])).title is None


def test_a_page_without_text_is_an_empty_string_not_dropped() -> None:
    assert read_pdf(make_pdf(["Text", "", "More"])).pages == ("Text", "", "More")


def test_an_encrypted_pdf_is_refused_with_a_clear_reason() -> None:
    for function in (pdf_page_count, read_pdf):
        with pytest.raises(DocumentError, match="password-protected"):
            function(encrypted_pdf())


def test_a_corrupt_pdf_is_unreadable() -> None:
    with pytest.raises(DocumentError, match="unreadable PDF"):
        read_pdf(b"%PDF-1.4 this is not a pdf")
    with pytest.raises(DocumentError, match="unreadable PDF"):
        pdf_page_count(b"%PDF-1.4 this is not a pdf")


def test_a_page_renders_to_a_png_at_the_requested_dpi() -> None:
    pdf = make_pdf(["", "Some text on the second page"])
    width, height, rows = decode_gray(render_page_png(pdf, 0, 200))
    assert (width, height) == (1700, 2200)  # US Letter, 612 x 792 pt, at 200 dpi
    assert {byte for row in rows for byte in row} == {255}  # a blank page is white
    half_width, half_height, _ = decode_gray(render_page_png(pdf, 0, 100))
    assert (half_width, half_height) == (850, 1100)


def test_a_page_with_text_has_dark_pixels() -> None:
    _, _, rows = decode_gray(render_page_png(make_pdf(["Some text on the page"]), 0, 100))
    assert min(min(row) for row in rows) < 100


@pytest.mark.parametrize("index", [-1, 2])
def test_rendering_a_page_that_does_not_exist_is_an_error(index: int) -> None:
    with pytest.raises(DocumentError, match="page"):
        render_page_png(make_pdf(["a", "b"]), index, 100)


# ---- DOCX -----------------------------------------------------------------------------------


def test_docx_paragraphs_become_text() -> None:
    assert (
        docx_text(make_docx(["Erster Absatz", "Zweiter Absatz"]))
        == "Erster Absatz\n\nZweiter Absatz"
    )


def test_docx_tables_stay_where_they_are_in_the_document() -> None:
    data = make_docx(
        ["Vor der Tabelle", "Nach der Tabelle"], table=[["A", "B"], ["1", "2"]], table_after=0
    )
    assert docx_text(data) == "Vor der Tabelle\n\nA | B\n1 | 2\n\nNach der Tabelle"


def test_empty_docx_paragraphs_are_skipped() -> None:
    assert docx_text(make_docx(["Eins", "", "   ", "Zwei"])) == "Eins\n\nZwei"


@pytest.mark.parametrize("data", [b"not a zip at all", b"PK\x03\x04 broken"])
def test_a_broken_docx_is_unreadable(data: bytes) -> None:
    with pytest.raises(DocumentError, match="unreadable DOCX"):
        docx_text(data)


def test_a_zip_that_is_not_a_docx_is_unreadable() -> None:
    import io
    import zipfile

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("hello.txt", "hi")
    with pytest.raises(DocumentError, match="unreadable DOCX"):
        docx_text(buffer.getvalue())


# ---- plain text -----------------------------------------------------------------------------


def test_utf8_text_is_decoded_and_a_bom_is_dropped() -> None:
    assert decode_text("Überprüfung".encode()) == "Überprüfung"
    assert decode_text(b"\xef\xbb\xbfMit BOM") == "Mit BOM"


def test_legacy_encodings_are_detected() -> None:
    text = "Die Prüfung der Anlage für Wärme und Größe ergab keine Beanstandungen."
    assert decode_text(text.encode("cp1252")) == text


@pytest.mark.parametrize("data", [b"\x00\x01\x02binary\x00", b"", b"   \n\t "])
def test_binary_or_empty_data_is_not_text(data: bytes) -> None:
    with pytest.raises(DocumentError, match="no readable text"):
        decode_text(data)


def test_a_few_control_characters_do_not_make_a_text_binary() -> None:
    prose = "Ein normaler Absatz mit genug Text, um kein Binärmüll zu sein. " * 10
    assert decode_text((prose + "\x0c" + prose + "\x0c").encode()).count("\x0c") == 2


def test_mostly_control_characters_are_binary() -> None:
    with pytest.raises(DocumentError, match="no readable text"):
        decode_text(("ab\x01\x02\x03" * 200).encode())


def test_line_endings_are_unified() -> None:
    assert decode_text(b"a\r\nb\rc") == "a\nb\nc"


# ---- pseudo pages ---------------------------------------------------------------------------


def test_text_is_split_into_pages_at_blank_lines() -> None:
    text = "\n\n".join(f"Absatz {n} " + "wort " * 20 for n in range(10))
    pages = split_pages(text, 300)
    assert all(len(page) <= 300 for page in pages)
    assert len(pages) > 1
    assert " ".join(" ".join(pages).split()) == " ".join(text.split())  # nothing lost or reordered


def test_short_text_is_one_page_and_empty_text_is_none() -> None:
    assert split_pages("kurz", 3000) == ["kurz"]
    assert split_pages("   ", 3000) == []
