import { useEffect, useRef, useState } from 'react'
import type { FormEvent } from 'react'

interface Identity {
  user: { id: string; username: string; role: string }
  csrf_token: string
}
interface Submission {
  id: string; status: string; created_at: string; updated_at: string
  author_ref: string; campaign_id: string; version: number
}
interface Detail extends Submission {
  consent: { rules_version: string; accepted_at: string } | null
  history: { id: number; occurred_at: string; from_status: string | null; to_status: string; reason_code: string | null; actor: string }[]
}
interface Page { items: Submission[]; next_cursor: string | null }
const statuses: Record<string, string> = {
  awaiting_consent: 'Ожидает согласия', validating: 'Ожидает проверки файла',
  moderating: 'Автоматическая проверка', review_required: 'Ручная проверка',
  approved: 'Одобрено', rejected: 'Отклонено',
}
const messages: Record<string, string> = {
  invalid_credentials: 'Неверный логин или пароль.', unauthorized: 'Сессия завершилась. Войдите снова.',
  forbidden: 'Недостаточно прав.', csrf_failed: 'Не удалось подтвердить запрос. Обновите страницу.',
  validation_error: 'Неверные параметры запроса.', rate_limited: 'Слишком много попыток. Повторите вход через минуту.',
  not_found: 'Заявка не найдена.', internal_error: 'Сервис временно недоступен. Повторите позже.',
}
class ApiError extends Error {
  status: number
  constructor(status: number, message: string) { super(message); this.status = status }
}
async function api<T>(path: string, options: RequestInit = {}): Promise<T> {
  const response = await fetch('/api' + path, { ...options, credentials: 'same-origin' })
  if (!response.ok) {
    const body = await response.json().catch(() => ({})) as { code?: string }
    throw new ApiError(response.status, messages[body.code ?? ''] ?? 'Не удалось выполнить запрос.')
  }
  return response.status === 204 ? undefined as T : response.json() as Promise<T>
}
const date = (value: string) => new Date(value).toLocaleString('ru-RU')

function App() {
  const [identity, setIdentity] = useState<Identity | null>(null)
  const [starting, setStarting] = useState(true)
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  const [filter, setFilter] = useState('')
  const [items, setItems] = useState<Submission[]>([])
  const [cursor, setCursor] = useState<string | null>(null)
  const [detail, setDetail] = useState<Detail | null>(null)
  const [loading, setLoading] = useState(true)
  const pending = useRef<AbortController | null>(null)

  useEffect(() => {
    const controller = new AbortController()
    api<Identity>('/auth/me', { signal: controller.signal }).then(setIdentity).catch((e: unknown) => {
      if (e instanceof Error && e.name === 'AbortError') return
      if (!(e instanceof ApiError && e.status === 401)) setError('Не удалось проверить сессию. Попробуйте войти.')
    }).finally(() => { if (!controller.signal.aborted) setStarting(false) })
    return () => controller.abort()
  }, [])

  function failure(e: unknown) {
    if (e instanceof Error && e.name === 'AbortError') return
    if (e instanceof ApiError && e.status === 401) { setIdentity(null); setDetail(null); setItems([]) }
    setError(e instanceof ApiError ? e.message : 'Нет связи с сервером. Повторите запрос.')
  }

  async function load(next: string | null = null, status = filter) {
    pending.current?.abort()
    const controller = new AbortController()
    pending.current = controller
    const params = new URLSearchParams({ limit: '20' })
    if (status) params.set('status', status)
    if (next) params.set('cursor', next)
    try {
      const page = await api<Page>('/submissions?' + params.toString(), { signal: controller.signal })
      if (controller.signal.aborted) return
      setItems(old => next ? [...new Map([...old, ...page.items].map(row => [row.id, row])).values()] : page.items)
      setCursor(page.next_cursor)
    } catch (e) { if (!controller.signal.aborted) failure(e) }
    finally { if (!controller.signal.aborted) setLoading(false) }
  }

  useEffect(() => {
    let active = true
    if (identity) void Promise.resolve().then(() => { if (active) return load(null, filter) })
    return () => { active = false; pending.current?.abort() }
    // Identity and filter are the complete list query scope; load is an event helper.
  }, [identity, filter])

  async function signIn(event: FormEvent<HTMLFormElement>) {
    event.preventDefault(); setBusy(true); setLoading(true); setError('')
    try {
      setIdentity(await api<Identity>('/auth/login', {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ username, password }),
      }))
      setPassword('')
    } catch (e) { failure(e) } finally { setBusy(false) }
  }
  async function signOut() {
    if (!identity) return
    setBusy(true); setError('')
    try {
      await api<void>('/auth/logout', { method: 'POST', headers: { 'X-CSRF-Token': identity.csrf_token } })
      pending.current?.abort(); setIdentity(null); setItems([]); setDetail(null)
    } catch (e) { failure(e) } finally { setBusy(false) }
  }
  async function open(id: string) {
    pending.current?.abort()
    const controller = new AbortController(); pending.current = controller
    setLoading(true); setError('')
    try { const value = await api<Detail>('/submissions/' + encodeURIComponent(id), { signal: controller.signal }); if (!controller.signal.aborted) setDetail(value) }
    catch (e) { if (!controller.signal.aborted) failure(e) }
    finally { if (!controller.signal.aborted) setLoading(false) }
  }

  if (starting) return <main><h1>Панель модератора</h1><p role="status">Проверяем сессию…</p></main>
  return <main>
    <header><h1>Панель модератора</h1>{identity && <div><span>{identity.user.username} · {identity.user.role}</span> <button onClick={() => void signOut()} disabled={busy}>Выйти</button></div>}</header>
    {error && <p role="alert" className="error">{error}</p>}
    {!identity ? <section className="login"><h2>Вход</h2><form onSubmit={e => void signIn(e)}>
      <label>Логин<input name="username" autoComplete="username" maxLength={64} required value={username} onChange={e => setUsername(e.target.value)} /></label>
      <label>Пароль<input name="password" type="password" autoComplete="current-password" maxLength={256} required value={password} onChange={e => setPassword(e.target.value)} /></label>
      <button type="submit" disabled={busy}>{busy ? 'Входим…' : 'Войти'}</button>
    </form></section> : <>
      <section aria-label="Список заявок">
        <div className="toolbar"><h2>Заявки</h2><label>Статус<select value={filter} onChange={e => { setItems([]); setCursor(null); setDetail(null); setLoading(true); setError(''); setFilter(e.target.value) }}><option value="">Все статусы</option>{Object.entries(statuses).map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select></label><button disabled={loading} onClick={() => { setDetail(null); setLoading(true); setError(''); void load() }}>Обновить</button></div>
        {loading && <p role="status">Загрузка…</p>}
        {!loading && items.length === 0 && !error && <p>Заявок пока нет.</p>}
        {items.length > 0 && <div className="table-wrap"><table><thead><tr><th>ID заявки</th><th>Создана</th><th>Статус</th><th>Версия</th></tr></thead><tbody>{items.map(row => <tr key={row.id}><td><button className="link" onClick={() => void open(row.id)}>{row.id}</button></td><td>{date(row.created_at)}</td><td>{statuses[row.status] ?? row.status}</td><td>{row.version}</td></tr>)}</tbody></table></div>}
        {cursor && <button disabled={loading} onClick={() => { setLoading(true); setError(''); void load(cursor) }}>Загрузить ещё</button>}
      </section>
      {detail && <section aria-label="Карточка заявки"><h2>Карточка заявки</h2><button onClick={() => setDetail(null)}>Закрыть карточку</button><dl>
        <dt>ID</dt><dd>{detail.id}</dd><dt>Автор</dt><dd>{detail.author_ref}</dd><dt>Кампания</dt><dd>{detail.campaign_id}</dd><dt>Статус</dt><dd>{statuses[detail.status] ?? detail.status}</dd><dt>Версия</dt><dd>{detail.version}</dd><dt>Согласие</dt><dd>{detail.consent ? `${detail.consent.rules_version} · ${date(detail.consent.accepted_at)}` : 'Не получено'}</dd>
      </dl><h3>История статусов</h3><ol>{detail.history.map(entry => <li key={entry.id}>{date(entry.occurred_at)}: {entry.from_status ?? 'создание'} → {entry.to_status} · {entry.reason_code ?? 'без кода'} · {entry.actor}</li>)}</ol></section>}
    </>}
  </main>
}
export default App
