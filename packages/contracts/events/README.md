# Контракты событий media/CV

Формат сообщений между Backend (Dev2) и workers media/CV (Dev3) через RabbitMQ. Задача S1-08; общие гарантии доставки — в [contracts.md](../../../docs/contracts.md), политика и лимиты — в [policies/](../policies/README.md). Схемы — JSON Schema 2020-12; примеры в `examples/` проверяет `tests/test_event_contracts.py`.

## События

| `event_type` | Кто → кому | Очередь | Смысл |
| --- | --- | --- | --- |
| `media.requested` | Backend → media | `media.tasks` | Проверить и очистить оригинал из карантина |
| `media.completed` | media → Backend | `backend.results` | Очищенный JPEG записан в `sanitized/` |
| `media.rejected` | media → Backend | `backend.results` | Файл невалиден или вне лимитов: технический отказ |
| `media.failed` | media → Backend | `backend.results` | Сбой обработки, а не свойство файла |
| `cv.requested` | Backend → CV | `cv.tasks` | Оценить очищенное фото по снимку политики |
| `cv.completed` | CV → Backend | `backend.results` | Scores, рекомендация и причины |
| `cv.failed` | CV → Backend | `backend.results` | Анализ не выполнен; Backend ставит `review_required` / `CV_ERROR` |

Файлы: `envelope.schema.json` — общий конверт, `common.schema.json` — общие определения, `<event_type>.schema.json` — схема каждого события.

## Идентификаторы

- `event_id` не меняется при повторной доставке того же сообщения.
- `operation_id` — один логический запуск обработки. Повторная проверка того же файла (пересмотр, перезапуск после DLQ) получает новый `operation_id`.
- `causation_id`: у задания — ID outbox-записи Backend, у результата — `event_id` задания.
- `correlation_id` в v1 равен `submission_id`.
- **ID результата детерминирован:** `event_id = uuid5(UUID(operation_id), "<producer>.result")`, где `producer` — `media-worker` или `cv-worker`. Повторное вычисление после падения worker даёт тот же ID, и Backend отбрасывает дубль по `(consumer, event_id)`.

```python
import uuid

result_id = uuid.uuid5(uuid.UUID(operation_id), "media-worker.result")
```

## Один финальный результат на операцию

На каждую операцию worker публикует **ровно один** результат: `completed`, `rejected` или `failed`.

- **Повторяемая ошибка** (таблица ниже) результатом не публикуется. Задание уходит в retry-очередь (TTL/DLX) с ограниченным числом попыток.
- Когда попытки исчерпаны или ошибка неповторяемая, worker публикует `*.failed`. Это финальный исход: Backend не ждёт после него другого результата.
- `media.rejected` — не ошибка системы, а свойство файла. Повтор не нужен, модератор не может одобрить.
- ACK исходного задания отправляется **после** publisher confirm результата. Падение между ними приводит к повторной обработке с тем же ID результата.

Без этого правила возможна ловушка: worker публикует `failed`, затем на повторе получает `completed` с тем же `event_id`, и Backend отбрасывает правильный результат как дубль.

| `error.code` | Когда | Повторять |
| --- | --- | --- |
| `INVALID_TASK` | Payload не проходит схему или версия не поддерживается | Нет |
| `OBJECT_NOT_FOUND` | Объекта нет по ключу | Да, ограниченно |
| `OBJECT_HASH_MISMATCH` | SHA-256 объекта не совпал с заданием | Нет |
| `STORAGE_UNAVAILABLE` | MinIO недоступен | Да |
| `ACCESS_DENIED` | Нет прав на бакет/ключ | Нет: ошибка конфигурации |
| `MODEL_NOT_READY` | Модель CV не загружена | Да |
| `TIMEOUT` | Превышен общий таймаут задачи | Да |
| `INTERNAL` | Неожиданная ошибка | Да, ограниченно |

Таймаут или нехватка памяти **при декодировании** — свойство файла: это `media.rejected` с `DECODE_LIMIT_EXCEEDED`, а не `TIMEOUT`.

Сообщение, у которого не проходит даже конверт (нельзя доверять ID), результатом не отвечается: оно уходит в DLQ для ручного разбора.

## Объекты и права

В событии передаётся внутренний ключ MinIO, а не URL. Ключ задаёт Backend; worker пишет только в выданный `target.key`.

| Объект | Формат ключа |
| --- | --- |
| Оригинал | `quarantine` / `submissions/{submission_id}/{operation_id}/original` |
| Очищенный | `sanitized` / `submissions/{submission_id}/{asset_id}/{operation_id}.jpg` |

| Кто | `quarantine` | `sanitized` |
| --- | --- | --- |
| Fetcher (Backend) | запись | — |
| Media | чтение | запись |
| CV | — | чтение |
| Backend API, publisher | — | чтение |

Media сверяет SHA-256 и размер оригинала с `source` до декодирования. CV сверяет SHA-256 очищенного объекта с `image.sha256`.

## Снимки политики

- `media.requested.payload.media_profile` — полный снимок media-профиля. Media применяет лимиты из задания и возвращает `media_profile_version`.
- `cv.requested.payload.policy` — полный снимок политики кампании. CV не читает тему из других мест; смена темы не требует изменения кода.
- Результаты содержат версии worker, профиля/политики, препроцессинга и моделей, чтобы решение можно было воспроизвести.

## Что проверяет Backend

Backend принимает результат только для ожидаемых `operation_id`, `asset_id` и версии политики. Повторный или устаревший результат подтверждается без бизнес-эффекта. Поздний `cv.completed` не отменяет решение модератора. `recommended_decision` — рекомендация: итоговый статус выставляет Backend.

## Изменение контракта

Все объекты закрыты (`additionalProperties: false`), поэтому новое поле — тоже изменение контракта. Схема, примеры и обе стороны (Backend и worker) меняются в одном PR или с согласованным порядком выкладки. Несовместимое изменение поднимает `schema_version`. Изменение контракта согласует Dev2, ревью Dev1.
