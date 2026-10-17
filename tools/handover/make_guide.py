"""The user guide for the customer: docs/user-guide.md plus the known limitations of
docs/release-notes.md, with the references a reader outside the repository cannot follow
taken out. The wording of the guide itself is not changed."""

import sys
from pathlib import Path

repo, target = Path(sys.argv[1]), Path(sys.argv[2])
guide = (repo / "docs" / "user-guide.md").read_text(encoding="utf-8")
notes = (repo / "docs" / "release-notes.md").read_text(encoding="utf-8")


def swap(text: str, old: str, new: str) -> str:
    assert text.count(old) == 1, (old, text.count(old))
    return text.replace(old, new)


guide = swap(
    guide,
    "# Инструкция пользователя\n",
    "# Инструкция пользователя\n\n> Бот для претензионной работы, версия 0.2.0 (октябрь 2026).\n",
)
guide = swap(
    guide, "**Проверка завершена частично** (`partial`)", "**Проверка завершена частично**"
)
guide = swap(
    guide,
    "Известные ограничения текущей версии — в [заметках к выпуску](release-notes.md).",
    "",
)

# The limitations table, as the release notes give it.
start = notes.index("### Известные ограничения")
end = notes.find("\n## ", start)  # the newest release's table only
block = notes[start : end if end != -1 else None].rstrip().splitlines()
intro = next(line for line in block[1:] if line.strip())
rows = [line for line in block if line.startswith("|")]
closing = block[-1]
table = "\n".join(rows)
table = swap(table, " (`docs/fns-account-blocks.md`)", "")
table = swap(
    table,
    "Пороги и уровни (`docs/scoring.md`) — рабочая версия, её подтверждает пилот S6-05",
    "Пороги и уровни приоритета — рабочая версия, её подтверждает пробная проверка со специалистом",
)
assert "docs/" not in table and "S6-" not in table and "docs/" not in guide

target.write_text(
    guide.rstrip()
    + "\n\n## Известные ограничения\n\n"
    + intro
    + "\n\n"
    + table
    + "\n\n"
    + closing
    + "\n",
    encoding="utf-8",
)
print(target, len(target.read_text(encoding="utf-8").splitlines()), "строк")
