# Инфраструктура

Конфигурация инфраструктуры: очереди, хранилище и мониторинг по мере реализации.
Начальный [compose.yaml](../compose.yaml) находится в корне репозитория (S1-03).
Он запускает PostgreSQL, RabbitMQ, MAX-эмулятор, миграции, HTTP Backend
и отдельный inbox-бот. [Dockerfile](../Dockerfile) содержит цели `backend`
и `max-emulator`; зависимости устанавливаются из `uv.lock` без dev-группы.
Приложения работают с непривилегированным UID и read-only rootfs.
База и брокер доступны только внутри сети Compose; HTTP-порты — на loopback.
Тома БД и брокера сохраняются при `docker compose down`.
CPU/RAM/PID и ротация логов заданы по [плану ресурсов, §6](../docs/results/capacity-planning.md#6-лимиты-начального-compose).
`rabbitmq/20-resources.conf` задаёт абсолютный memory watermark брокера
и подключается как read-only Compose config.

Запуск, настройка кампании, перезапуск и ограничения описаны в
[operations.md](../docs/operations.md#локальный-compose-s1-03).
Профиль `frontend` добавляет `moderator-ui`: Node собирает статику,
Caddy раздаёт её на loopback-порту `PANEL_PORT` (8080) и проксирует API
к `backend:8000`. Запуск: `docker compose up --build -d --wait moderator-ui backend`;
для всего стенда — `docker compose --profile frontend up --build -d --wait`.
Тома `panel-caddy-data` и `panel-caddy-config` сохраняют данные Caddy.
RabbitMQ пока не подключён к коду бота (S1-17), панель остаётся каркасом без API (S1-11).
Поэтому это основа стенда, а не полная приёмка S1-03/S1-16.

`python3 infra/scripts/smoke_compose.py` собирает образы и проверяет
текущий сценарий в одноразовом проекте со случайными портами и отдельными
томами; cleanup удаляет только его тестовые ресурсы. Та же команда
запускается в CI job `Compose smoke`. Нужен Compose 2.24.4+;
[подробности и артефакты](../docs/operations.md#автоматический-smoke-тест).

`scripts/install-gitleaks.sh` устанавливает Gitleaks для Linux x64 в GitHub Actions:
скачивает архив, проверяет SHA-256 и добавляет каталог бинарника в `GITHUB_PATH`.
Версия и контрольная сумма задаются в `.github/workflows/secrets.yml`.
Workflow вызывает скрипт через `bash`; сканирование запускается отдельным шагом.

`scripts/smoke_moderator_ui.py` собирает контейнер панели на Caddy и проверяет
статику, proxy API/webhook, кеширование, заголовки и конфигурацию HTTPS.
Запускается из корня репозитория с Docker и Python 3.14; использует временные
контейнеры и сеть с уникальными именами, удаляет только свои ресурсы.
Команда включена в `Frontend build`. [Запуск панели и HTTPS](../apps/moderator-ui/README.md#контейнер-node-24--caddy).

Владелец — Dev1. Окружения и диагностика — в [operations.md](../docs/operations.md).
