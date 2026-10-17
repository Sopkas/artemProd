# ruff: noqa: E501  -- the instructions sheet holds whole sentences, as the customer reads them
"""The comparison table of the pilot and the bot's own templates, for the customer.

Run with the project's interpreter: the templates come from the same builders the dialog
uses, so they are byte-for-byte what the bot sends out.
"""

import sys
from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.datavalidation import DataValidation

from claims_assistant.infrastructure.excel.counterparties import build_counterparties_template
from claims_assistant.infrastructure.excel.ledgers import (
    build_debt_history_template,
    build_interactions_template,
    build_payments_template,
)

out = Path(sys.argv[1])
(out / "Шаблоны").mkdir(parents=True, exist_ok=True)

# --- the templates, under the names the bot gives them ---------------------------------
for name, data in {
    "kontragenty.xlsx": build_counterparties_template(),
    "platezhi.xlsx": build_payments_template(),
    "istoriya-dolga.xlsx": build_debt_history_template(),
    "vzaimodeystviya.xlsx": build_interactions_template(),
}.items():
    (out / "Шаблоны" / name).write_bytes(data)

# --- the comparison table -------------------------------------------------------------
ROWS = 20  # the trial list: 15–20 companies (about 16 a day on the free tariff)
FIRST, LAST = 3, 2 + ROWS  # row 2 is the example

FONT = Font(name="Arial", size=10)
BOLD = Font(name="Arial", size=10, bold=True)
TITLE = Font(name="Arial", size=14, bold=True)
NOTE = Font(name="Arial", size=10, italic=True, color="555555")
HEAD_FILL = PatternFill("solid", fgColor="DDE3EA")
INPUT_FILL = PatternFill("solid", fgColor="FFF9DB")  # cells the specialist fills in
EXAMPLE_FILL = PatternFill("solid", fgColor="EEEEEE")
THIN = Side(style="thin", color="B0B0B0")
BORDER = Border(left=THIN, right=THIN, top=THIN, bottom=THIN)
WRAP = Alignment(wrap_text=True, vertical="top")

MANUAL = ("Критичный", "Высокий", "Средний", "Низкий")
BOT = (*MANUAL, "Недостаточно данных")
VERDICTS = ("бот", "специалист", "ошибка правил", "пробел в данных", "спорно")

COLUMNS = (
    # title, width, the list of allowed values (or None), filled by the specialist
    ("№", 5, None),
    ("Организация", 30, None),
    ("ИНН", 14, None),
    ("Приоритет вручную", 17, MANUAL),
    ("Причины вручную (2–3 главные)", 38, None),
    ("Минут вручную", 10, None),
    ("Приоритет бота", 19, BOT),
    ("Причины бота (лист «Приоритеты»)", 38, None),
    ("Совпало?", 10, None),  # a formula
    ("Кто прав при расхождении", 20, VERDICTS),
    ("ИИ: верно по фактам?", 13, ("да", "нет")),
    ("ИИ: полезно для работы (1–3)", 14, ("1", "2", "3")),
    ("ИИ: вводит в заблуждение?", 14, ("да", "нет")),
    ("Согласны со «Следующим шагом»?", 16, ("да", "нет")),
    ("Комментарий", 40, None),
)
MATCH = 9  # the column of the formula

book = Workbook()
sheet = book.active
sheet.title = "Сравнение"
for index, (title, width, _) in enumerate(COLUMNS, 1):
    cell = sheet.cell(row=1, column=index, value=title)
    cell.font, cell.fill, cell.border, cell.alignment = BOLD, HEAD_FILL, BORDER, WRAP
    sheet.column_dimensions[get_column_letter(index)].width = width
sheet.row_dimensions[1].height = 45
sheet.freeze_panes = "D2"

example = (
    "пример",
    "ООО «Пример» (строка-образец, удалите)",
    "0000000000",
    "Высокий",
    "Просрочка 70 дней; не выходят на связь",
    12,
    "Средний",
    "Просрочка 40 дн. — от 31 до 60",
    None,
    "пробел в данных",
    "да",
    2,
    "нет",
    "да",
    "В файле «Контрагенты» стояла просрочка на прошлую дату",
)
for row in range(2, LAST + 1):
    for index, (_, _, allowed) in enumerate(COLUMNS, 1):
        cell = sheet.cell(row=row, column=index)
        cell.font, cell.border, cell.alignment = FONT, BORDER, WRAP
        if index == MATCH:
            d, g = f"D{row}", f"G{row}"
            cell.value = f'=IF(OR({d}="",{g}=""),"",IF({d}={g},"да","нет"))'
        elif row == 2:
            cell.value = example[index - 1]
            cell.fill = EXAMPLE_FILL
        else:
            cell.fill = INPUT_FILL
            if index == 1:
                cell.value = row - 2
    sheet.cell(row=row, column=3).number_format = "@"  # the INN keeps its leading zeros
sheet.cell(row=2, column=MATCH).fill = EXAMPLE_FILL
for index, (_, _, allowed) in enumerate(COLUMNS, 1):
    if allowed is None:
        continue
    letter = get_column_letter(index)
    rule = DataValidation(type="list", formula1='"' + ",".join(allowed) + '"', allow_blank=True)
    rule.error, rule.errorTitle = "Выберите значение из списка.", "Не из списка"
    sheet.add_data_validation(rule)
    rule.add(f"{letter}{FIRST}:{letter}{LAST}")

# --- the totals: formulas over the filled rows, the example row left out ----------------
totals = book.create_sheet("Итоги")
totals.column_dimensions["A"].width = 58
totals.column_dimensions["B"].width = 14
totals.column_dimensions["C"].width = 60
totals["A1"], totals["A1"].font = "Итоги пробной проверки", TITLE
totals["A2"] = "Считается по листу «Сравнение», строка-образец не учитывается."
totals["A2"].font = NOTE


def span(letter: str) -> str:
    return f"Сравнение!${letter}${FIRST}:${letter}${LAST}"


LINES = (
    (
        "Организаций с двумя приоритетами (вручную и бота)",
        f'=COUNTIF({span("I")},"да")+COUNTIF({span("I")},"нет")',
        "Сравнивать можно только их",
    ),
    ("Приоритеты совпали", f'=COUNTIF({span("I")},"да")', ""),
    (
        "Приоритеты разошлись",
        f'=COUNTIF({span("I")},"нет")',
        "По каждой — столбец «Кто прав при расхождении»",
    ),
    ("Доля совпавших", '=IF(B4=0,"",B5/B4)', "Совпавшие / сравнённые"),
    (
        "Бот ответил «Недостаточно данных»",
        f'=COUNTIF({span("G")},"Недостаточно данных")',
        "Не хватило файлов или не ответил источник",
    ),
    ("Минут вручную всего", f"=SUM({span('F')})", ""),
    ("Минут вручную на организацию", f'=IF(COUNT({span("F")})=0,"",B9/COUNT({span("F")}))', ""),
    ("Пояснений ИИ оценено", f"=COUNT({span('L')})", "Строки с оценкой полезности 1–3"),
    (
        "Доля пояснений с оценкой 3",
        f'=IF(B11=0,"",COUNTIF({span("L")},3)/B11)',
        "«Помогает решить, что делать»",
    ),
    ("Пояснений, неверных по фактам", f'=COUNTIF({span("K")},"нет")', ""),
    ("Пояснений, вводящих в заблуждение", f'=COUNTIF({span("M")},"да")', ""),
    ("Не согласны со «Следующим шагом»", f'=COUNTIF({span("N")},"нет")', ""),
)
for offset, (label, formula, note) in enumerate(LINES):
    row = 4 + offset
    totals.cell(row=row, column=1, value=label).font = FONT
    value = totals.cell(row=row, column=2, value=formula)
    value.font, value.border = BOLD, BORDER
    totals.cell(row=row, column=3, value=note).font = NOTE
totals["B7"].number_format = "0%"
totals["B12"].number_format = "0%"
totals["B10"].number_format = "0.0"
totals["A17"] = "Время бота: от «Новая проверка» до получения отчёта, минут"
totals["A17"].font = FONT
totals["B17"].fill, totals["B17"].border = INPUT_FILL, BORDER
totals["C17"], totals["C17"].font = "Впишите руками", NOTE

# --- the legend -------------------------------------------------------------------------
legend = book.create_sheet("Как заполнять")
legend.column_dimensions["A"].width = 110
TEXT = (
    ("Как заполнять таблицу сравнения", TITLE),
    ("", FONT),
    (
        "1. Жёлтые ячейки на листе «Сравнение» заполняете вы. Серая строка 2 — образец, её можно удалить или оставить: в итоги она не попадает.",
        FONT,
    ),
    (
        "2. Сначала — столбцы «Приоритет вручную», «Причины вручную» и «Минут вручную». Их заполняют ДО запуска бота.",
        BOLD,
    ),
    (
        "3. Потом запускаете проверку в боте и переносите из отчёта (лист «Приоритеты») приоритет и причины бота.",
        FONT,
    ),
    (
        "4. Столбец «Совпало?» считается сам. Там, где стоит «нет», заполните «Кто прав при расхождении».",
        FONT,
    ),
    (
        "5. Четыре столбца про ИИ заполняются только для пояснений, которые начинаются с «ИИ:». Полезность: 1 — бесполезно, 2 — частично, 3 — помогает решить, что делать.",
        FONT,
    ),
    ("6. Лист «Итоги» считается сам. Руками там вписывается одно число — время работы бота.", FONT),
    ("", FONT),
    (
        "Приоритеты выбираются из списка в ячейке: Критичный, Высокий, Средний, Низкий; у бота ещё «Недостаточно данных».",
        FONT,
    ),
    (
        "На бесплатном тарифе источника внешних данных — около 16 организаций в день: 100 запросов в сутки, по шесть на организацию. Больший список проверяйте в два дня.",
        FONT,
    ),
    (
        "Таблица с названиями и ИНН остаётся у вас. Разработчикам нужны только числа с листа «Итоги» и разбор расхождений без названий.",
        FONT,
    ),
)
for row, (text, font) in enumerate(TEXT, 1):
    cell = legend.cell(row=row, column=1, value=text)
    cell.font, cell.alignment = font, Alignment(wrap_text=True, vertical="top")

book.save(out / "3. Таблица сравнения.xlsx")
print("saved")
