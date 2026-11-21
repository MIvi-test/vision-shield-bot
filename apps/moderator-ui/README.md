# Панель модератора

Веб-панель модератора фотоленты MAX (задача S1-11): вход, список заявок и карточка заявки. Сейчас это каркас приложения — экраны появятся на следующих этапах.

Стек: React 19, TypeScript 7, Vite 8. Нужен Node 24 — та же версия, что в CI.

## Команды

Выполняются в этом каталоге:

```bash
npm ci          # установка зависимостей строго по package-lock.json
npm run dev     # dev-сервер на http://127.0.0.1:5173
npm run build   # проверка типов (tsc -b) и сборка сайта в dist/
```

Дополнительно: `npm run lint` (oxlint), `npm run typecheck`, `npm run preview` (раздача собранного `dist/`).

## Контейнер: Node 24 → Caddy

Dockerfile собирает `dist/` через `npm ci` и `npm run build`, затем копирует
только результат в образ Caddy. Базовые образы закреплены по digest.
Веб-сервер работает с UID/GID 10001, поддерживает read-only rootfs;
Node и npm отсутствуют в runtime.

Команды из корня репозитория:

```bash
docker build -t vision-shield-panel:local apps/moderator-ui
docker network create vision-shield-panel-dev
docker run --rm --name moderator-ui --network vision-shield-panel-dev \
  --read-only --cap-drop ALL --security-opt no-new-privileges:true \
  --tmpfs /tmp:rw,noexec,nosuid,size=16m,mode=1777 \
  --mount type=volume,source=panel-caddy-data,target=/data \
  --mount type=volume,source=panel-caddy-config,target=/config \
  --memory 96m --cpus 0.5 --pids-limit 64 \
  -p 127.0.0.1:8080:8080 vision-shield-panel:local
```

Сеть создаётся один раз. Панель доступна на `http://127.0.0.1:8080`.
Backend подключается к той же сети с DNS-именем `backend` и портом 8000.
Параметры Caddy передаются контейнеру через environment:

| Переменная | По умолчанию | Назначение |
| --- | --- | --- |
| `PANEL_SITE_ADDRESS` | `http://:8080` | Локальный HTTP; для публичного HTTPS укажите домен без схемы и порта |
| `PANEL_BACKEND` | `backend:8000` | Внутренний адрес HTTP Backend |

`/api`, `/api/*`, `/webhook` и `/webhook/*` проксируются без удаления префикса,
с сохранением query string, cookies, Origin и заголовка webhook secret.
Остальные маршруты используют SPA fallback. Несуществующий `/assets/*`
возвращает 404; существующие хешированные assets кешируются на год.
HTML и ответы API не кешируются. Настроены CSP, `nosniff` и
`Referrer-Policy: same-origin`. Access logs выключены; объект запроса
удаляется из runtime error logs, чтобы исключить токены, cookies и URL.

Статика доступна и без Backend; запрос к отсутствующему Backend возвращает
502. Сейчас Backend реализует только liveness: API панели и обработчик
webhook ещё предстоит реализовать. Маршрут `/webhook` зарезервирован для
будущего обработчика, наличие proxy не означает готовность вебхуков.
Docker healthcheck проверяет Caddy на loopback-порту 8081, без обращения к БД
или Backend; этот порт не нужно публиковать.

### Публичный HTTPS и вебхуки

Этот же Caddy служит входным веб-сервером панели и Backend. Для публичного
режима задайте `PANEL_SITE_ADDRESS=panel.example.org`, заменив домен своим,
и используйте публикации `-p 80:8080 -p 443:8443` вместо локального порта.
DNS A/AAAA должен указывать на сервер; входящие TCP 80/443 и исходящий доступ
Caddy к ACME должны быть разрешены. HTTP перенаправляется на HTTPS.
Постоянный том `/data` хранит сертификаты и ключи: сохраняйте его при
пересоздании контейнера. Caddy автоматически получает и продлевает сертификат
публичного центра сертификации — [условия Automatic HTTPS](https://caddyserver.com/docs/automatic-https).
Для локального режима сертификаты не выпускаются.

MAX требует HTTPS на внешнем порту 443, соответствие домена сертификату
доверенного центра и полную цепочку; self-signed и локальный CA Caddy для
реальных вебхуков не подходят. Обработчик Backend должен проверить secret
и вернуть 200 за 30 секунд; по архитектуре проекта ответ отправляется после
фиксации inbox в БД. [Требования MAX](https://dev.max.ru/docs-api/methods/POST/subscriptions).
Выпуск публичного сертификата и доставку реального webhook проверяют на
стенде с доменом; локальный smoke эти проверки не заменяет.

### Проверка образа

Из корня репозитория, с Docker и Python 3.14:

```bash
python3 infra/scripts/smoke_moderator_ui.py
```

Эта же команда выполняется в `Frontend build`. Она собирает образ, запускает
его с read-only rootfs и временным Backend stub, проверяет SPA fallback,
кеширование, заголовки, передачу API/webhook-запросов и конфигурацию HTTPS.
Свои контейнеры, сеть и теги образов удаляет после проверки; кеш сборки остаётся.

Архитектура и границы компонентов — [docs/ARCHITECTURE.md](../../../docs/ARCHITECTURE.md).
Контракт API панели — [packages/contracts/api/openapi.yaml](../../../packages/contracts/api/openapi.yaml).
