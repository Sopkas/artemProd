from aiogram.types import BotCommand, KeyboardButton, ReplyKeyboardMarkup

CHECK_INN = "Проверить ИНН"
CANCEL = "Отмена"


def main_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=CHECK_INN)],
            [KeyboardButton(text="О сервисе"), KeyboardButton(text="Помощь")],
        ],
        resize_keyboard=True,
        is_persistent=True,
    )


def cancel_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=CANCEL)]],
        resize_keyboard=True,
        is_persistent=True,
    )


def bot_commands() -> list[BotCommand]:
    return [
        BotCommand(command="start", description="Главное меню"),
        BotCommand(command="help", description="Помощь"),
        BotCommand(command="about", description="О сервисе"),
        BotCommand(command="inn", description="Проверить ИНН"),
        BotCommand(command="cancel", description="Отменить ввод"),
    ]
