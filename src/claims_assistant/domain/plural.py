"""Russian plural of the word our texts count most (S4-03, S5-04, S7-01).

The rule was written out twice and drifted: the package review says «У 1 организации …»
(genitive), the report says «модель недоступна — 1 организация» (nominative). Both forms
are right in their sentence, so both live here instead of one of them quietly winning.
"""


def companies(count: int) -> str:
    """Nominative: «1 организация», «2 организации», «5 организаций»."""
    if count % 10 == 1 and count % 100 != 11:
        return f"{count} организация"
    if count % 10 in (2, 3, 4) and count % 100 not in (12, 13, 14):
        return f"{count} организации"
    return f"{count} организаций"


def of_companies(count: int) -> str:
    """Genitive, for «У 5 организаций …»: «1 организации», «5 организаций»."""
    if count % 10 == 1 and count % 100 != 11:
        return f"{count} организации"
    return f"{count} организаций"
