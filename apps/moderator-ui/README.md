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

Архитектура и границы компонентов — [docs/ARCHITECTURE.md](../../docs/ARCHITECTURE.md). Контракт API панели — [packages/contracts/api/openapi.yaml](../../packages/contracts/api/openapi.yaml).