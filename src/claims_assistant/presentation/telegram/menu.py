from aiogram.types import BotCommand, KeyboardButton, ReplyKeyboardMarkup

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
PAYMENTS_TEMPLATE = "По нашему шаблону"
PAYMENTS_EXPORT = "Выгрузка из 1С"


def _keyboard(rows: list[list[str]]) -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=text) for text in row] for row in rows],
        resize_keyboard=True,
        is_persistent=True,
    )


def main_menu() -> ReplyKeyboardMarkup:
    return _keyboard([[CHECK_INN, NEW_CHECK, STATUS], [REPORT, "О сервисе", "Помощь"]])


def cancel_menu() -> ReplyKeyboardMarkup:
    return _keyboard([[CANCEL]])


def date_menu() -> ReplyKeyboardMarkup:
    return _keyboard([[TODAY, CANCEL]])


def launch_menu() -> ReplyKeyboardMarkup:
    """Package confirmation: launch, or add the optional files first (S4-01)."""
    return _keyboard([[LAUNCH], [ADD_PAYMENTS, ADD_HISTORY], [ADD_INTERACTIONS, CANCEL]])


def payments_source_menu() -> ReplyKeyboardMarkup:
    """Payments come either in our template or as the customer's own 1C print (S4-05)."""
    return _keyboard([[PAYMENTS_TEMPLATE, PAYMENTS_EXPORT], [CANCEL]])


def bot_commands() -> list[BotCommand]:
    return [
        BotCommand(command="start", description="Главное меню"),
        BotCommand(command="help", description="Помощь"),
        BotCommand(command="about", description="О сервисе"),
        BotCommand(command="inn", description="Проверить ИНН"),
        BotCommand(command="check", description="Новая проверка по файлу"),
        BotCommand(command="status", description="Состояние последней проверки"),
        BotCommand(command="report", description="Отчёт по последней проверке"),
        BotCommand(command="cancel", description="Отменить ввод"),
    ]
