"""Person names.

Stored names are Unicode NFC (composed) so "é" has one representation. Matching keys
go further: accents stripped and case folded, so "Muñoz" and "MUNOZ" compare equal.
"""

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Literal

from app.pipeline.core import Rule

NAME_RULE = Rule("name.parse", 1)
NAME_PART_RULE = Rule("name.part", 1)
NameOrder = Literal["given_family", "family_given"]

_WHITESPACE = re.compile(r"\s+")


@dataclass(frozen=True)
class ParsedName:
    text: str
    family: str | None = None
    given: list[str] = field(default_factory=list)

    @property
    def certain(self) -> bool:
        return self.family is not None


def clean(raw: str) -> str:
    return _WHITESPACE.sub(" ", unicodedata.normalize("NFC", raw)).strip()


def _fix_case(text: str) -> str:
    """Title-case names written entirely in upper or lower case; leave mixed case alone."""
    letters = [char for char in text if char.isalpha()]
    if letters and (text.isupper() or text.islower()):
        return text.title()
    return text


def name_part(raw: str) -> str:
    return _fix_case(clean(raw))


def parse_full_name(raw: str, order: NameOrder) -> ParsedName:
    text = clean(raw)
    if "," in text:
        family_part, _, given_part = text.partition(",")
        family = name_part(family_part)
        given = [name_part(token) for token in given_part.split()]
        if family and given:
            return ParsedName(" ".join([*given, family]), family, given)
        return ParsedName(text)

    tokens = text.split(" ")
    # One token, or four and more (particles, double surnames), is too risky to split.
    if not 2 <= len(tokens) <= 3:
        return ParsedName(text)
    tokens = [_fix_case(token) for token in tokens]
    if order == "given_family":
        given, family = tokens[:-1], tokens[-1]
    else:
        family, given = tokens[0], tokens[1:]
    return ParsedName(" ".join([*given, family]), family, given)


def match_key(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text)
    stripped = "".join(char for char in decomposed if not unicodedata.combining(char))
    return "".join(char for char in stripped.casefold() if char.isalpha())
