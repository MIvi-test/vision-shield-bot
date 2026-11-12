"""Reply rules shared by future polling and webhook adapters."""


def choose_reply(text: str, *, has_photo: bool) -> str:
    command = text.strip()
    if command == "/start":
        return (
            "Привет! Я бот модерации фотографий. "
            "Сейчас доступна проверка связи. Команды: /start и /help. "
            "Приём заявок пока не подключён."
        )
    if command == "/help":
        return (
            "/start — приветствие. /help — помощь. "
            "Можно отправить фото для проверки связи: файл не скачивается, "
            "заявка не создаётся."
        )
    if has_photo:
        return (
            "В сообщении есть фото. Приём заявок пока не подключён: "
            "файл не скачан и не отправлен на модерацию."
        )
    return "Я понимаю /start и /help. Приём заявок пока не подключён."
