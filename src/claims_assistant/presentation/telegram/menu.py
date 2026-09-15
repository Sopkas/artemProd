from aiogram.types import BotCommand, KeyboardButton, ReplyKeyboardMarkup


def main_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="О сервисе"), KeyboardButton(text="Помощь")]],
        resize_keyboard=True,
        is_persistent=True,
    )


def bot_commands() -> list[BotCommand]:
    return [
        BotCommand(command="start", description="Главное меню"),
        BotCommand(command="help", description="Помощь"),
        BotCommand(command="about", description="О сервисе"),
    ]
