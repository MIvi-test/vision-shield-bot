# vision-shield-bot

Тематическая фотолента в MAX с автоматической и ручной модерацией.

Архитектура — [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md),
задачи — [docs/sprints.md](docs/sprints.md), роли — [docs/team.md](docs/team.md).

| Каталог | Содержание |
| --- | --- |
| [apps/](apps/README.md) | Backend, эмулятор MAX, workers и панель |
| [packages/](packages/README.md) | Общие контракты |
| [infra/](infra/README.md) | Конфигурация инфраструктуры |
| `docs/` | Архитектура, задачи и эксплуатационная документация |

Подготовка локальной конфигурации:

```bash
cp .env.example .env
```

`.env` исключён из Git. Токены и пароли заполняются локально.
Код приложений и Compose пока отсутствуют; запуск стенда появится в S1-03.

Изменения вносим отдельной веткой через PR с минимум одним одобрением другого
участника команды. Ревьюера выбираем вручную; решение о CODEOWNERS отложено.
Настройка защиты `main` описана в [CONTRIBUTING.md](CONTRIBUTING.md).
