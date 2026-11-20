# vision-shield-bot

Бот для тематической фотоленты в MAX. Пользователь отправляет фото и подтверждает
согласие с правилами; цель проекта — проверка изображения, автоматическая
и ручная модерация, публикация в канале и уведомление автора.

Сейчас работают эмулятор MAX и Backend: приём заявок, согласия, PostgreSQL-inbox
и аудит. Обработка изображений, панель модератора и доставка через RabbitMQ
ещё в разработке. Локальный стенд использует эмулятор; реальный MAX не подключён.

## Быстрый старт

Нужны Docker Engine и Docker Compose 2.24.4+. Из корня репозитория:

```bash
cp .env.example .env
```

В `.env` задайте разные `POSTGRES_PASSWORD` и `RABBITMQ_PASSWORD`:
для каждого выполните `openssl rand -hex 24`. Затем запустите стенд:

```bash
docker compose up --build -d --wait
```

Проверка Backend: <http://127.0.0.1:8000/health/live>.
Swagger эмулятора: <http://127.0.0.1:8081/docs>.

По умолчанию бот работает в режиме проверки связи. Чтобы включить приём заявок,
[создайте кампанию и пройдите демо](docs/operations.md#включить-приём-заявок-и-пройти-демо).
Перед перезапуском эмулятора остановите бота и задайте новый `MAX_SOURCE_ID` —
[порядок перезапуска и работа с данными](docs/operations.md#остановка-перезапуск-и-данные).
Секреты храните в локальном `.env`, исключённом из Git.

## Разработка и проверки

Нужны Python 3.14+ и uv 0.13.6:

```bash
uv sync --locked
uv run ruff check .
uv run ruff format --check .
uv run ty check
uv run pytest
```

Проверка Compose: `python3 infra/scripts/smoke_compose.py`.
Скрипт собирает и проверяет отдельный тестовый стенд, затем удаляет его
контейнеры и тома; рабочий `.env` не используется.
[Подробности и диагностика](docs/operations.md#автоматический-smoke-тест).

## Документация

- [Архитектура](docs/ARCHITECTURE.md) и [план задач](docs/sprints.md).
- [Запуск и эксплуатация](docs/operations.md).
- [Backend](apps/backend/README.md) и [эмулятор MAX](apps/max-emulator/README.md).
- [Правила участия](CONTRIBUTING.md), [роли команды](docs/team.md) и [история изменений](CHANGELOG.md).
