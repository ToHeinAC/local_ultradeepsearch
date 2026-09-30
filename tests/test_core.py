import pytest

from app.core import slugify


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Hello, World!", "hello-world"),
        ("  many   spaces  ", "many-spaces"),
        ("Äpfel & Birnen_2", "äpfel-birnen-2"),
        ("", ""),
        ("!!!", ""),
    ],
)
def test_slugify(text: str, expected: str) -> None:
    assert slugify(text) == expected
