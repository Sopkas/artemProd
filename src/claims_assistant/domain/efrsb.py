"""S3-06: what a message of the bankruptcy register means for our decision.

Until now every message of the register raised the priority alike and was labelled
«требует проверки». Two things learned from the live source (20–23.09.2026) shape the
rules here.

**The register answers by INN, not by role.** A message is returned when the company is
mentioned in the case, and it is just as often mentioned as the *creditor* — the four
messages of a large bank in our own probe are of that kind: a court ruling and a
creditor's claim in someone else's case. Nothing in the record says who the debtor is;
the name only appears inside the free text of the ruling. So **a message alone never
proves that this company is going bankrupt**, and it must not produce
``confirmed_procedure``. The confirmed procedure comes from the company's own status in
the «Организация» section, where there is no role to confuse.

**The kind of message still matters**, for what a person reads and for how much noise we
raise: a ruling that introduces a procedure is not the same as a creditors' meeting, and
a case that has been closed is history, not a warning. That is what ``classify`` is for.

The classification is by the register's own type — the machine code when we know it, the
Russian name otherwise — and anything unrecognised stays ``OTHER``: unknown means
«check it», never «fine».
"""

from enum import StrEnum


class EventKind(StrEnum):
    """What the message is about, from the strongest to the weakest."""

    PROCEDURE = "procedure"  # a procedure is introduced or the debtor declared bankrupt
    INTENTION = "intention"  # someone announced an intention to go to court
    CASE = "case"  # the case is alive: rulings, claims, meetings, auctions
    CLOSURE = "closure"  # the case is over: proceedings terminated, ruling cancelled
    OTHER = "other"  # not recognised — treated as «requires review»


# Codes seen on the live API; the register's own vocabulary is larger, so the names below
# carry most of the work and a new code simply falls through to OTHER.
_CODES = {
    "ArbitralDecree": EventKind.CASE,
    "ReceivingCreditorDemand": EventKind.CASE,
    "MeetingOfCreditors": EventKind.CASE,
    "MeetingOfCreditorsResult": EventKind.CASE,
    "AuctionAnnouncement": EventKind.CASE,
    "AuctionResult": EventKind.CASE,
}

# Matched against the lowercased Russian name of the type, in this order: the first hit
# wins, so «прекращение производства» is closure even though it also mentions a procedure.
# Each rule is a set of stems that must all be present in the type's Russian name; the
# first rule that matches wins. Stems, not whole words: the register inflects freely
# («отказе от исполнения», «завершении конкурсного»).
_RULES: tuple[tuple[tuple[str, ...], EventKind], ...] = (
    # Two traps A found on #52, both from the customer's own trade:
    # a refusal to perform a contract is a live event about a lease, not a closed case…
    (("исполнени", "договор"), EventKind.CASE),
    # …and the completion of конкурсное производство ends with the debtor struck off the
    # register: the strongest signal there is, not history.
    (("завершен", "конкурсн"), EventKind.PROCEDURE),
    (("прекращ",), EventKind.CLOSURE),
    (("отмен",), EventKind.CLOSURE),
    (("отказ",), EventKind.CLOSURE),  # отказ в признании банкротом, во введении процедуры
    (("завершен",), EventKind.CLOSURE),
    (("намерени",), EventKind.INTENTION),
    (("призна",), EventKind.PROCEDURE),  # о признании должника банкротом
    (("введени",), EventKind.PROCEDURE),
    (("наблюдени",), EventKind.PROCEDURE),
    (("конкурсн",), EventKind.PROCEDURE),
    (("внешне", "управлени"), EventKind.PROCEDURE),
    (("внешнего", "управления"), EventKind.PROCEDURE),
    (("оздоровлени",), EventKind.PROCEDURE),
    (("реструктуризаци",), EventKind.PROCEDURE),
    (("судебн",), EventKind.CASE),
    (("собрани",), EventKind.CASE),
    (("требовани",), EventKind.CASE),
    (("торг",), EventKind.CASE),
    (("оценк",), EventKind.CASE),
)

_LABELS = {
    EventKind.PROCEDURE: "процедура банкротства",
    EventKind.INTENTION: "намерение обратиться в суд",
    EventKind.CASE: "ход дела о банкротстве",
    EventKind.CLOSURE: "дело прекращено или решение отменено",
    EventKind.OTHER: "сообщение ЕФРСБ",
}


def classify(code: object, name: object) -> EventKind:
    """The kind of one message; anything unrecognised is ``OTHER``, never «harmless»."""
    if isinstance(code, str) and code.strip() in _CODES:
        return _CODES[code.strip()]
    text = name.strip().lower() if isinstance(name, str) else ""
    for stems, kind in _RULES:
        if all(stem in text for stem in stems):
            return kind
    return EventKind.OTHER


def label(kind: EventKind) -> str:
    """A short Russian name of the kind, for the report and the card."""
    return _LABELS[kind]


def raises_priority(kind: EventKind) -> bool:
    """Whether a message of this kind is a reason to look at the company now.

    A closed case is not: it stays in «Основания» with its date and link, and the report
    says so, but it does not raise anybody's priority years later.
    """
    return kind is not EventKind.CLOSURE
