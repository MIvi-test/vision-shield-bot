# MVP

MVP — обязательная часть постановки ([assignment.md](assignment.md), раздел «Обязательная часть»), работающая на эмуляторе MAX с настоящими media и CV. Постановка допускает публикацию в эмулятор канала, поэтому реальный MAX в MVP не нужен. Это результат спринта 3 ([sprints.md](sprints.md)); документ задаёт критерии, а не утверждает, что MVP уже реализован.

До MVP есть два запускаемых результата: в спринте 1 — согласие, сохранённая заявка и защищённая панель без скачивания изображения; в спринте 2 — настоящая проверка и очистка, ручное решение, публикация и уведомление. В спринте 3 модель подключается к готовому ручному пути. Исследование моделей и подготовка данных начинаются в спринте 1.

## Что входит

| Требование постановки | Компоненты | Готово, когда |
| --- | --- | --- |
| Бот принимает изображение и обязательное согласие | Эмулятор (Dev1), Backend с входным адаптером (Dev2), PostgreSQL; Redis для временного диалога | Без согласия файл не скачивается; согласие хранится с пользователем, временем и версией правил |
| Проверка типа, размера и целостности до обработки | Fetcher Dev2 (скачивание, размер, SHA-256), media worker Dev3 (формат, лимиты, перекодирование) | Невалидный файл получает `rejected` с причиной, не доходит до CV и не может быть одобрен вручную |
| Повторная отправка того же файла выявляется | Fetcher | Дубль в той же кампании отклоняется с `DUPLICATE` |
| Тематическая модерация и статус решения | CV worker Dev3 с реальными моделями, Backend consumer Dev2 | У заявки `approved`, `rejected` или `review_required` с причинами и версиями; ошибка модели не даёт автоматического одобрения; качество проверено на контрольном наборе |
| Ручная модерация спорных материалов | Панель Dev4, Backend API Dev2; сценарий проверяет Dev3 | Доступ требует входа и роли; модератор решает спорную заявку; конфликт версий даёт `409`; решение есть в истории; превью только очищенное |
| Публикация в тестовую ленту или эмулятор канала | Publisher Dev2, эмулятор Dev1 | Одобренное очищенное фото появляется в канале; повтор завершённой задачи не создаёт второй пост; неоднозначная отправка сохраняет `publication_unknown` и не повторяется вслепую |
| Уведомление пользователя и журнал событий | Notifier, `audit_events` | Автор получает нейтральный статус; историю заявки можно восстановить по аудиту |

Гарантии вводятся вместе со сценарием: inbox, сохранение согласия и outbox ответов — в спринте 1; карантин, `sanitized/`, `processed_events`, подтверждения доставки и учёт публикаций — в спринте 2. К MVP проверены базовые входные/исходящие лимиты и запуск по README на чистом окружении. Media и CV не имеют доступа к PostgreSQL, MAX-токену и интернету. Exactly-once для внешнего MAX API не обещается.

## После MVP, до сдачи

Завершение продукта: пересмотр с отзывом опубликованного поста, рейтинг и розыгрыш по согласованному источнику голосов. Реальный MAX и тестовый канал — цель при наличии доступа, но не условие сдачи (ARCHITECTURE.md §1).

Подготовка к эксплуатации: расширенная сверка неизвестных исходов и разбор DLQ, сроки хранения и отзыв согласия, Prometheus/Grafana, расширенные отказные и нагрузочные проверки, восстановление резервных копий и проверка обновления. Базовые тесты повторов/отказов и контрактные тесты эмулятора уже нужны до MVP. Для ограниченной реальной эксплуатации отдельно подтверждаются ресурсы, нагрузка, HTTPS, режим интеграции и ответственный; учебное демо не объявляется production автоматически.

## Компоненты

Две схемы: путь файла и путь событий. Стрелка показывает, куда идут данные. Цвета: синий — эмулятор и панель, зелёный — процессы Backend, красный — недоверенный оригинал и изолированные workers, оранжевый — хранилища, фиолетовый — брокер.

### Путь файла и граница доверия

Оригинал лежит только в `quarantine/` и доступен только media. CV, панель и publisher получают только очищенную копию из `sanitized/`.

```mermaid
flowchart LR
    EMU_IN["Эмулятор MAX · Dev1<br/>фото пользователя"]
    FETCH["Fetcher · Dev2<br/>скачивание без декодирования,<br/>лимит размера, SHA-256"]
    QUAR[("MinIO<br/>quarantine/")]
    SAN[("MinIO<br/>sanitized/")]
    UI["Панель · Dev4<br/>превью для модератора"]
    PUB["Publisher · Dev2"]
    EMU_CH["Эмулятор MAX · Dev1<br/>пост в канале"]

    subgraph ISO["Изоляция — сеть processing, internal: true"]
        MEDIA["Media worker · Dev3<br/>формат и лимиты,<br/>перекодирование, без EXIF"]
        CV["CV worker · Dev3<br/>тема и NSFW"]
    end

    EMU_IN -- файл вложения --> FETCH
    FETCH -- оригинал --> QUAR
    QUAR -- оригинал --> MEDIA
    MEDIA -- очищенная копия --> SAN
    SAN --> CV
    SAN -- presigned URL --> UI
    SAN --> PUB
    PUB -- пост --> EMU_CH

    classDef ext fill:#eef4ff,stroke:#5b7bd5,color:#1d2b4f
    classDef be fill:#eefaf1,stroke:#3d9a5b,color:#173a22
    classDef untrusted fill:#fdeeee,stroke:#c94b4b,color:#4d1515
    classDef trusted fill:#eefaf1,stroke:#3d9a5b,color:#173a22
    classDef proc fill:#fdeeee,stroke:#c94b4b,color:#4d1515
    class EMU_IN,EMU_CH,UI ext
    class FETCH,PUB be
    class QUAR untrusted
    class SAN trusted
    class MEDIA,CV proc
```

### События и состояние

Каждый процесс Backend меняет состояние в PostgreSQL вместе с записью в outbox, а dispatcher переносит события в RabbitMQ. Media и CV с PostgreSQL не связаны. Все исходящие сообщения, включая запрос согласия, отправляет notifier.

```mermaid
flowchart TB
    EMU_IN["Эмулятор MAX · Dev1<br/>сообщения пользователя"]
    UI["Панель модератора · Dev4"]

    API["API + входной адаптер · Dev2<br/>диалог, согласие, заявки, решения"]
    REDIS[("Redis<br/>FSM диалога")]
    PG[("PostgreSQL<br/>заявки, согласия, inbox,<br/>outbox, аудит")]
    OUT["Outbox dispatcher · Dev2"]
    RMQ[["RabbitMQ"]]

    FETCH["Fetcher · Dev2"]
    CON["Consumer · Dev2"]
    PUB["Publisher / notifier · Dev2<br/>все исходящие сообщения"]
    WORK["Media и CV workers · Dev3"]

    EMU_OUT["Эмулятор MAX · Dev1<br/>посты, уведомления"]

    EMU_IN -- события бота --> API
    UI <-- заявки, решения --> API
    API <-- шаг диалога --> REDIS
    API -- заявка + outbox --> PG
    PG -- события outbox --> OUT
    OUT --> RMQ
    RMQ -- fetch.tasks --> FETCH
    RMQ -- media.tasks, cv.tasks --> WORK
    WORK -- backend.results --> RMQ
    RMQ -- backend.results --> CON
    RMQ -- publication, notification --> PUB
    FETCH -- объект + outbox --> PG
    CON -- статус + outbox --> PG
    PUB -- исход операции --> PG
    PUB -- посты, уведомления --> EMU_OUT

    classDef ext fill:#eef4ff,stroke:#5b7bd5,color:#1d2b4f
    classDef be fill:#eefaf1,stroke:#3d9a5b,color:#173a22
    classDef data fill:#fff7e6,stroke:#c98a1b,color:#4a3208
    classDef proc fill:#fdeeee,stroke:#c94b4b,color:#4d1515
    classDef mq fill:#f3eefc,stroke:#7a52c7,color:#2d1a52
    class EMU_IN,EMU_OUT,UI ext
    class API,OUT,FETCH,CON,PUB be
    class REDIS,PG data
    class WORK proc
    class RMQ mq
```

## Сквозной сценарий

Три фазы. Время идёт сверху вниз, нумерация в каждой фазе своя. Цветная полоса — этап сценария, его название в заголовке полосы: синий — приём и уведомление, жёлтый — скачивание и автоматическая модерация, зелёный — успешная обработка и публикация, красный — отказ, фиолетовый — ручная модерация. Пунктирная стрелка с открытым наконечником — событие из outbox, которое dispatcher переносит в RabbitMQ.

### 1. Приём и проверка файла

```mermaid
%%{init: {"sequence": {"mirrorActors": false, "actorMargin": 40, "boxMargin": 8, "noteMargin": 8, "messageMargin": 34}}}%%
sequenceDiagram
    autonumber
    participant E as Эмулятор MAX<br/>Dev1
    participant A as API<br/>Dev2
    participant F as Fetcher<br/>Dev2
    participant C as Consumer<br/>Dev2
    participant DB as PostgreSQL
    participant Q as RabbitMQ
    participant S as MinIO
    participant M as Media worker<br/>Dev3

    rect rgba(91, 123, 213, 0.14)
        Note over E,M: Приём и согласие
        E->>A: фото
        A->>DB: inbox + заявка awaiting_consent
        opt нет согласия на текущую версию правил
            Note over E,C: согласие запросит notifier (фаза 3)
            E->>A: согласие
            A->>DB: согласие с версией правил
        end
        A->>DB: заявка validating + outbox
    end

    rect rgba(201, 138, 27, 0.16)
        Note over E,M: Скачивание в карантин
        DB--)Q: outbox → fetch.tasks
        Q->>F: fetch.tasks
        E->>F: файл вложения
        F->>S: оригинал в quarantine/, по ходу SHA-256
    end

    alt тот же файл уже есть в кампании
        rect rgba(201, 75, 75, 0.16)
            F->>DB: rejected (DUPLICATE) + outbox уведомления
        end
    else новый файл
        rect rgba(61, 154, 91, 0.16)
            Note over E,M: Проверка и очистка файла
            F->>DB: объект + хеш + outbox
            DB--)Q: outbox → media.tasks
            Q->>M: media.tasks
            S->>M: оригинал из quarantine/
            M->>S: очищенная копия в sanitized/
            M->>Q: результат в backend.results
            Q->>C: результат media
            C->>DB: moderating + outbox или rejected + outbox уведомления
        end
    end
```

### 2. Модерация

```mermaid
%%{init: {"sequence": {"mirrorActors": false, "actorMargin": 40, "boxMargin": 8, "noteMargin": 8, "messageMargin": 34}}}%%
sequenceDiagram
    autonumber
    participant C as Consumer<br/>Dev2
    participant A as API<br/>Dev2
    participant DB as PostgreSQL
    participant Q as RabbitMQ
    participant S as MinIO
    participant V as CV worker<br/>Dev3
    participant U as Панель<br/>Dev4

    rect rgba(201, 138, 27, 0.16)
        Note over C,U: Автоматическая модерация
        DB--)Q: outbox → cv.tasks
        Q->>V: cv.tasks
        S->>V: очищенное фото из sanitized/
        V->>Q: вердикт в backend.results
        Q->>C: вердикт CV
        C->>DB: approved / rejected / review_required + outbox
    end

    opt review_required
        rect rgba(122, 82, 199, 0.16)
            Note over C,U: Ручная модерация
            DB->>A: очередь спорных заявок и история
            A->>U: заявки + короткие presigned URL
            S->>U: превью фото
            U->>A: решение модератора с version
            A->>DB: статус + аудит + outbox, при конфликте версий 409
        end
    end
```

### 3. Публикация и уведомление

```mermaid
%%{init: {"sequence": {"mirrorActors": false, "actorMargin": 40, "boxMargin": 8, "noteMargin": 8, "messageMargin": 34}}}%%
sequenceDiagram
    autonumber
    participant DB as PostgreSQL
    participant Q as RabbitMQ
    participant S as MinIO
    participant P as Publisher / notifier<br/>Dev2
    participant E as Эмулятор MAX<br/>Dev1

    opt заявка одобрена
        rect rgba(61, 154, 91, 0.16)
            Note over DB,E: Публикация
            DB--)Q: outbox → publication.tasks
            Q->>P: publication.tasks
            P->>DB: захват операции, попытка: publishing
            S->>P: очищенное фото из sanitized/
            P->>E: загрузка фото (POST /uploads)
            P->>E: пост в канал (POST /messages)
            E->>P: ID сообщения
            P->>DB: published, при таймауте publication_unknown
        end
    end

    rect rgba(91, 123, 213, 0.14)
        Note over DB,E: Уведомление автора
        DB--)Q: outbox → notification.tasks
        Q->>P: notification.tasks
        P->>E: нейтральный статус
    end
```

## Готовность MVP

MVP принимается на зафиксированной версии со следующими подтверждениями:

- Фото после согласия проходит настоящие media/CV до автоматического решения, публикации очищенной копии и уведомления; отдельно показаны отказ и ручная ветка.
- Ручной путь требует входа и роли, сохраняет историю и проверяет версию решения; поздний результат CV не отменяет решение человека.
- Невалидный файл и дубль отклоняются до CV; оригинал не доступен панели и publisher.
- Повтор события, задачи или результата не создаёт второй заявки или повторного завершённого эффекта. Падение worker до ACK восстанавливается. Неопределённая отправка явно учитывается и не повторяется без разбора.
- Есть воспроизводимый отчёт по качеству, ошибочным одобрениям/отклонениям, доле ручной проверки, времени и памяти. Критерии качества согласованы до настройки порогов; контрольный набор отделён от настройки. Пока критерии не выполнены, автопубликация выключена, сохраняется результат ручного этапа, а MVP не объявляется готовым.
- Базовые лимиты, файловые ограничения, права и отсутствие секретов в логах проверены; блокирующие дефекты исправлены.
- Человек вне команды запускает стенд по README; CI выполняет реальные тесты и сборку панели, результаты демо и ограничения записаны.
