"""PII detection and redaction.

Two jobs with deliberately different severities, and conflating them would make
one of them wrong.

**At ingestion: detect and record, never block.** This platform exists to answer
questions about enterprise documents, and enterprise documents contain people.
An HR handbook naming an employee, a support export full of customer emails, a
contract with a signatory — refusing to ingest those would refuse the product.
What ingestion does instead is *count* what it found and store the counts on the
document, so an operator can see that `support-export.csv` carries 4,000 email
addresses before deciding who may read it. The label system is the control;
this makes the decision an informed one.

**In traces and logs: redact, always.** A trace is a second copy of tenant data
that nobody thinks to review, retained on a different clock, and readable by
admins who may hold none of the labels that gated the original. Anything written
there passes through `redact()` first.

**What this is not.** Regexes find *formats*, not meaning. This catches an email
address, a card-shaped number, a national identifier; it does not catch "the
patient in room 3" or a name in prose. Claiming otherwise would be worse than
claiming nothing, because it invites trusting a control that does not hold. The
threat model records this limit explicitly.

The card check is Luhn-validated rather than pattern-only. A sixteen-digit
number is usually an order id, and a detector that flags every one of them
trains people to ignore it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum


class PIIKind(StrEnum):
    EMAIL = "email"
    PHONE = "phone"
    CREDIT_CARD = "credit_card"
    SSN = "ssn"
    IBAN = "iban"
    IP_ADDRESS = "ip_address"
    API_KEY = "api_key"


# Ordered: the first pattern to match a span wins, so more specific patterns
# come first. An API key that happens to look like a phone number should be
# reported as the more alarming of the two.
_PATTERNS: tuple[tuple[PIIKind, re.Pattern[str]], ...] = (
    # Provider key formats, listed first because a leaked credential is the
    # most urgent thing this module can find. Deliberately narrow: a broad
    # "long random string" rule matches every UUID in the corpus.
    (
        PIIKind.API_KEY,
        re.compile(
            r"\b(?:sk-[A-Za-z0-9]{20,}"
            r"|xox[baprs]-[A-Za-z0-9-]{10,}"
            r"|ghp_[A-Za-z0-9]{36}"
            r"|AKIA[0-9A-Z]{16})\b"
        ),
    ),
    (PIIKind.EMAIL, re.compile(r"\b[\w.%+-]+@[\w.-]+\.[A-Za-z]{2,}\b")),
    # US-format SSN. Excludes the ranges the SSA never issues, which removes
    # most date-like false positives.
    (
        PIIKind.SSN,
        re.compile(r"\b(?!000|666|9\d\d)\d{3}-(?!00)\d{2}-(?!0000)\d{4}\b"),
    ),
    (PIIKind.IBAN, re.compile(r"\b[A-Z]{2}\d{2}[A-Z0-9]{11,30}\b")),
    # Candidate card numbers. Luhn-checked below before being reported.
    # The trailing \d is not redundant: without it the group's optional
    # separator swallows the space AFTER the number, and redaction produced
    # "[CREDIT_CARD]on file".
    (PIIKind.CREDIT_CARD, re.compile(r"\b(?:\d[ -]?){12,18}\d\b")),
    (
        PIIKind.PHONE,
        re.compile(r"(?:\+\d{1,3}[ -]?)?\(?\d{3}\)?[ -]\d{3}[ -]\d{4}\b"),
    ),
    (
        PIIKind.IP_ADDRESS,
        # The lookarounds reject a quad inside a longer dotted run, so
        # "10.0.26200.1" is not an address. A four-part VERSION — "1.2.3.4" —
        # is still indistinguishable from an IP by shape, and no regex resolves
        # that: both are four dotted numbers under 256.
        #
        # Kept anyway, and listed last, because the cost of the error is
        # asymmetric. A version counted as an IP inflates a number in a
        # document's metadata; a real internal address that goes unrecorded is
        # reconnaissance sitting in a corpus. Reviewers are told in
        # DATA_POLICY.md that this kind over-reports.
        re.compile(r"(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.])"),
    ),
)


@dataclass(frozen=True)
class PIIMatch:
    kind: PIIKind
    start: int
    end: int
    value: str


def _luhn(digits: str) -> bool:
    """Whether a digit string passes the Luhn checksum.

    Applied to card candidates because a sixteen-digit number is far more often
    an order id than a card, and a detector that cries wolf on every one of them
    is a detector people learn to ignore.
    """
    cleaned = [int(c) for c in digits if c.isdigit()]
    if len(cleaned) < 13:
        return False
    total = 0
    for index, digit in enumerate(reversed(cleaned)):
        if index % 2 == 1:
            digit *= 2
            if digit > 9:
                digit -= 9
        total += digit
    return total % 10 == 0


def _valid_ip(value: str) -> bool:
    return all(0 <= int(part) <= 255 for part in value.split("."))


def find(text: str) -> list[PIIMatch]:
    """Every match, ordered by position, without overlaps.

    Overlaps are resolved by pattern order rather than by length: the patterns
    are listed most-alarming first, so a string that looks like both an API key
    and a phone number is reported as the key.
    """
    if not text:
        return []

    matches: list[PIIMatch] = []
    claimed: list[tuple[int, int]] = []

    for kind, pattern in _PATTERNS:
        for found in pattern.finditer(text):
            start, end = found.span()
            if any(start < c_end and end > c_start for c_start, c_end in claimed):
                continue

            value = found.group()
            if kind is PIIKind.CREDIT_CARD and not _luhn(value):
                continue
            if kind is PIIKind.IP_ADDRESS and not _valid_ip(value):
                continue

            matches.append(PIIMatch(kind=kind, start=start, end=end, value=value))
            claimed.append((start, end))

    return sorted(matches, key=lambda m: m.start)


def summarise(text: str) -> dict[str, int]:
    """Counts by kind. What ingestion records.

    Counts, never values. A summary that carried examples would put the PII into
    the very metadata written to make it visible — the same mistake as logging a
    password to prove it was rejected.
    """
    counts: dict[str, int] = {}
    for match in find(text):
        counts[match.kind.value] = counts.get(match.kind.value, 0) + 1
    return counts


def redact(text: str) -> str:
    """Replace every match with a typed placeholder.

    Typed rather than uniform: `[EMAIL]` tells someone reading a trace that a
    message contained an address without telling them whose, and preserves
    enough shape to debug with. A uniform `[REDACTED]` loses that for no gain.

    Applied to anything written to a trace, a log line, or an error message.
    """
    if not text:
        return text

    result: list[str] = []
    cursor = 0
    for match in find(text):
        result.append(text[cursor : match.start])
        result.append(f"[{match.kind.value.upper()}]")
        cursor = match.end
    result.append(text[cursor:])
    return "".join(result)


def contains_pii(text: str) -> bool:
    return bool(find(text))
