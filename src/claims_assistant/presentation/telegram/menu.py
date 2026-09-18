from aiogram.types import BotCommand, KeyboardButton, ReplyKeyboardMarkup

CHECK_INN = "Проверить ИНН"
NEW_CHECK = "Новая проверка"
STATUS = "Статус"
CANCEL = "Отмена"
TODAY = "Сегодня"
LAUNCH = "Запустить проверку"


def _keyboard(rows: list[list[str]]) -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=text) for text in row] for row in rows],
        resize_keyboard=True,
        is_persistent=True,
    )


def main_menu() -> ReplyKeyboardMarkup:
    return _keyboard([[CHECK_INN, NEW_CHECK, STATUS], ["О сервисе", "Помощь"]])


def cancel_menu() -> ReplyKeyboardMarkup:
    return _keyboard([[CANCEL]])


def date_menu() -> ReplyKeyboardMarkup:
    return _keyboard([[TODAY, CANCEL]])


def launch_menu() -> ReplyKeyboardMarkup:
    return _keyboard([[LAUNCH, CANCEL]])


def bot_commands() -> list[BotCommand]:
    return [
        BotCommand(command="start", description="Главное меню"),
        BotCommand(command="help", description="Помощь"),
        BotCommand(command="about", description="О сервисе"),
        BotCommand(command="inn", description="Проверить ИНН"),
        BotCommand(command="check", description="Новая проверка по файлу"),
        BotCommand(command="status", description="Состояние последней проверки"),
        BotCommand(command="cancel", description="Отменить ввод"),
    ]
