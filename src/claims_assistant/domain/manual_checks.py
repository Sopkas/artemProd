"""What the product knows matters but cannot check by itself (S7-03).

Each entry is said to the user where the facts are shown — the card and the report — so
that a missing section reads as «not checked, here is where to look», never as «nothing
found». Only public pages that a person can open without our keys go here.
"""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ManualCheck:
    title: str
    reason: str
    url: str
    # What the page asks for that a person would not guess.
    hint: str = ""

    def line(self) -> str:
        line = f"{self.title}: {self.reason} Проверить вручную: {self.url}"
        return f"{line} — {self.hint}" if self.hint else line


# 26–27.09.2026: service.nalog.ru answers the «действующие решения о приостановлении»
# query only after a picture captcha — from a Russian address from the first request,
# from abroad after the third. We do not get around a captcha; see docs/fns-account-blocks.md.
ACCOUNT_BLOCKS = ManualCheck(
    title="Блокировки счетов ФНС",
    reason="не проверяются автоматически — сервис ФНС требует ввести код с картинки.",
    url="https://service.nalog.ru/bi.do",
    # The form also asks for a BIK: that of the bank making the query, so any bank will do.
    hint="введите ИНН контрагента и БИК любого банка, например вашего.",
)

MANUAL_CHECKS: tuple[ManualCheck, ...] = (ACCOUNT_BLOCKS,)
