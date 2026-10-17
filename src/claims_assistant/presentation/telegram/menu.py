from aiogram.types import (
    BotCommand,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
)

CHECK_INN = "Проверить ИНН"
NEW_CHECK = "Новая проверка"
STATUS = "Статус"
REPORT = "Отчёт"
CANCEL = "Отмена"
TODAY = "Сегодня"
LAUNCH = "Запустить проверку"
ADD_PAYMENTS = "Добавить платежи"
ADD_HISTORY = "Добавить историю долга"
ADD_INTERACTIONS = "Добавить взаимодействия"
ADD_DEBT_REPORT = "Добавить отчёт по договорам"
PAYMENTS_TEMPLATE = "По нашему шаблону"
PAYMENTS_EXPORT = "Выгрузка из 1С"
DETAILS = "Подробнее"
# S8-09: the monitoring list and its daily notices.
MONITORING = "Мониторинг"
WATCH_ADD = "Добавить на мониторинг"
WATCH_REMOVE = "Убрать из мониторинга"
WATCH_FILE = "Список файлом"
WATCH_CLEAR = "Очистить весь список"
WATCH_CLEAR_CONFIRM = "Да, очистить список"
BACK = "В главное меню"
UNWATCH = "Больше не следить"
# Callback data of the button under a notice: «unwatch:<ИНН>».
UNWATCH_PREFIX = "unwatch:"


def _keyboard(rows: list[list[str]]) -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=text) for text in row] for row in rows],
        resize_keyboard=True,
        is_persistent=True,
    )


# The customer's main use since 15.10.2026 is monitoring: it stands next to the card.
_MAIN_ROWS = [[CHECK_INN, MONITORING], [NEW_CHECK, STATUS, REPORT], ["О сервисе", "Помощь"]]


def main_menu() -> ReplyKeyboardMarkup:
    return _keyboard(_MAIN_ROWS)


def card_menu() -> ReplyKeyboardMarkup:
    """After the brief card: the full one is a button away, the main menu stays."""
    return _keyboard([[DETAILS], *_MAIN_ROWS])


def monitoring_menu() -> ReplyKeyboardMarkup:
    return _keyboard([[WATCH_ADD, WATCH_REMOVE], [WATCH_FILE, WATCH_CLEAR], [BACK]])


def clear_menu() -> ReplyKeyboardMarkup:
    return _keyboard([[WATCH_CLEAR_CONFIRM], [CANCEL]])


def unwatch_button(inn: str) -> InlineKeyboardMarkup:
    """Under a change notice: stop watching this company in one tap."""
    button = InlineKeyboardButton(text=UNWATCH, callback_data=f"{UNWATCH_PREFIX}{inn}")
    return InlineKeyboardMarkup(inline_keyboard=[[button]])


def cancel_menu() -> ReplyKeyboardMarkup:
    return _keyboard([[CANCEL]])


def date_menu() -> ReplyKeyboardMarkup:
    return _keyboard([[TODAY, CANCEL]])


def launch_menu() -> ReplyKeyboardMarkup:
    """Package confirmation: launch, or add the optional files first (S4-01)."""
    return _keyboard(
        [[LAUNCH], [ADD_PAYMENTS, ADD_HISTORY], [ADD_INTERACTIONS, ADD_DEBT_REPORT], [CANCEL]]
    )


def payments_source_menu() -> ReplyKeyboardMarkup:
    """Payments come either in our template or as the customer's own 1C print (S4-05)."""
    return _keyboard([[PAYMENTS_TEMPLATE, PAYMENTS_EXPORT], [CANCEL]])


def bot_commands() -> list[BotCommand]:
    return [
        BotCommand(command="start", description="Главное меню"),
        BotCommand(command="help", description="Помощь"),
        BotCommand(command="about", description="О сервисе"),
        BotCommand(command="inn", description="Проверить ИНН"),
        BotCommand(command="monitor", description="Мониторинг организаций"),
        BotCommand(command="check", description="Новая проверка по файлу"),
        BotCommand(command="status", description="Состояние последней проверки"),
        BotCommand(command="report", description="Отчёт по последней проверке"),
        BotCommand(command="cancel", description="Отменить ввод"),
    ]
