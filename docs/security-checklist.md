# Проверки защиты S1 — на ревью Dev4

26 ноября 2026. Подготовлено Dev2, независимое AppSec-ревью остаётся Dev4.
Это набор применимых проверок, не заявление о полной сертификации OWASP.

База: [ASVS 5.0.0](https://github.com/OWASP/ASVS/tree/v5.0.0) для auth,
session, access control, validation и ошибок; [API Security Top 10 2023](https://owasp.org/API-Security/editions/2023/en/0x00-header/)
для рисков аутентификации, раскрытия свойств, ресурсов и разрешений.
Параметры scrypt соответствуют варианту N=2^14,r=8,p=5 из
[OWASP Password Storage Cheat Sheet](https://cheatsheetseries.owasp.org/cheatsheets/Password_Storage_Cheat_Sheet.html#scrypt).

| Проверка | Локальное доказательство |
| --- | --- |
| Нет данных без сессии | test_backend_panel.py; через Caddy 401 до входа |
| Organizer не читает модераторские данные, admin может | test_expired_disabled_and_role_checks |
| Нет пароля по умолчанию / публичной регистрации | create_account.py с явным паролем; нет registration route |
| Соль и scrypt; upgrade старого development-хеша | test_legacy_development_hash_is_upgraded_on_successful_login |
| Чужой/пустой Origin отклоняется, Referer fallback работает | test_origin_fail_closed, test_referer_fallback_and_rotation |
| Неудачные входы ограничены между процессами | test_login_failures_share_throttle_across_instances |
| Cookie HttpOnly/Strict/Path=/api, срок 8 часов, без sliding | test_sessions_cookie_logout_and_no_sliding_expiry |
| Logout требует CSRF, отозванная cookie не действует | Тот же SQL/HTTP-тест и Chromium |
| /auth/me сохраняет CSRF и срок | Тесты и browser reload |
| Проекции не раскрывают MAX-ID/текст/токены | test_cursor_history_consent_and_private_projection |
| Ошибки не содержат SQL/пароль/URL, no-store | test_not_found_and_generic_server_error, Compose/API test |
| Повтор входа/очереди не повторяет эффекты; unknown не повторяется | test_backend_inbox.py, test_backend_delivery.py, Compose replay |
| UI без токенов в localStorage, без HTML-вставок из API | Обзор App.tsx; Chromium без pageerror; CSP Caddy |
| Python/JS lock, аудит, secret scan, CodeQL | Локальные lock/audit/Gitleaks; итоговые GitHub checks проверяются на head PR |

## До закрытия командой

Dev4 проверяет соответствие угрозам и выбирает дополнительные негативные
сценарии/DAST. Dev1 подтверждает обязательность checks и актуальный UI build
в CI (S1-19). Непроверенные пункты не отмечаются зелёными автоматически.

Публичный HTTPS, глобальные rate/body limits, ротация/удаление данных,
изоляция workers и файловая безопасность не входят в выполненные проверки
этого loopback-only сценария; владельцы и задачи перечислены в threat-model.md.
