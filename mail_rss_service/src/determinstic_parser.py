import re


class ExtractionError(ValueError):
    """The email does not match the expected newsletter format."""


_HEADING = re.compile(
    r"(?<![א-ת])הלכות\s+[^\r\n]{1,160}?\s+(?=א\.\s)"
)

# Quoted promotional article title followed by "מאמר חדש".
_FOOTER = re.compile(
    r"""['״“][^'״“”\r\n]{1,200}['״”]\s*מאמר\s+חדש"""
)

_SECOND_HALACHA = re.compile(r"(?<!\w)ב\.\s")

_SOURCE_AT_END = re.compile(r"\([^()]+\)\.?\s*$")


def extract_halachot(text: str) -> str:
    """
    Extract the heading and two halachot from plain email text.

    Preserves wording and references, adding paragraph separation.
    Raises ExtractionError when expected boundaries are missing
    or ambiguous.
    """
    if not text or not text.strip():
        raise ExtractionError("Email body is empty")

    headings = list(_HEADING.finditer(text))
    if len(headings) != 1:
        raise ExtractionError(
            f"Expected one halachot heading, found {len(headings)}"
        )

    heading = headings[0]

    footers = list(_FOOTER.finditer(text, heading.end()))
    if len(footers) != 1:
        raise ExtractionError(
            f"Expected one article footer, found {len(footers)}"
        )

    body = text[heading.end():footers[0].start()].strip()

    if not body.startswith("א."):
        raise ExtractionError("First halacha marker is missing")

    second_matches = list(_SECOND_HALACHA.finditer(body))
    if len(second_matches) != 1:
        raise ExtractionError(
            "Expected exactly one second halacha marker"
        )

    split_at = second_matches[0].start()
    first = body[:split_at].strip()
    second = body[split_at:].strip()

    for paragraph in (first, second):
        if not _SOURCE_AT_END.search(paragraph):
            raise ExtractionError(
                "Halacha does not end with the expected source reference"
            )

    return "\n\n".join(
        (heading.group().strip(), first, second)
    )
