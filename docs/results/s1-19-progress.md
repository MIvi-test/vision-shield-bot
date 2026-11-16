# S1-19: обязательный CI и защита веток

## Настройки

До изменения у `main` не было активной защиты; ruleset `develop`
не требовал ни одного status check и одобрения.

Теперь действуют независимые ruleset:

| Ветка | Назначение | Ruleset | История |
| --- | --- | --- | --- |
| `develop` | Интеграция задач через PR | [develop](https://github.com/MIvi-test/vision-shield-bot/rules/25749464) | Линейная; squash/rebase |
| `main` | Выпуск из `develop` отдельным релизным PR | [main](https://github.com/MIvi-test/vision-shield-bot/rules/27317069) | Merge commit разрешён |

Оба ruleset активны и требуют:

- `Python checks`, `Gitleaks`, `Frontend build`, `Dependency audit`,
  `CodeQL (python)`, `CodeQL (actions)` от GitHub Actions (integration ID 15368);
- актуальность PR относительно целевой ветки;
- результаты CodeQL с порогами security `High or higher` и обычных alerts `Errors`;
- PR, одно одобрение другого участника со сбросом при новых изменениях
  и разрешение обсуждений; обязательный CODEOWNERS выключен;
- запрет удаления ветки и force push.

Административный обход разрешён согласно правилам проекта; в проверках
он не применялся. JSON правил и административные credentials в репозитории
не хранятся. Workflow не управляют настройками защиты. Направление
релизного PR описано как порядок работы; отдельного автоматического
запрета PR в `main` из других веток не добавляли.

## Проверки в GitHub

Положительный исходный прогон на `develop`:
[`dcc9911`](https://github.com/MIvi-test/vision-shield-bot/commit/dcc991125fc5e392493676cc7c85326386efc17e).
[CI](https://github.com/MIvi-test/vision-shield-bot/actions/runs/43634570314),
[Security checks](https://github.com/MIvi-test/vision-shield-bot/actions/runs/43634570313)
и [Gitleaks](https://github.com/MIvi-test/vision-shield-bot/actions/runs/43634570270)
успешны. Имена и источник checks сверены через API с настройками ruleset.

| PR | Негативный сценарий | Подтверждение |
| --- | --- | --- |
| [#16 → develop](https://github.com/MIvi-test/vision-shield-bot/pull/16) | Неиспользуемый импорт | [Python checks](https://github.com/MIvi-test/vision-shield-bot/actions/runs/43727105911/job/132851335127) — failure; остальные обязательные checks прошли; `mergeable_state=blocked` |
| [#17 → develop](https://github.com/MIvi-test/vision-shield-bot/pull/17) | Command injection в отдельном FastAPI-модуле, который не запускается | Все шесть обязательных jobs прошли; проверка [результатов CodeQL](https://github.com/MIvi-test/vision-shield-bot/runs/132852277082) — failure, одна critical-находка; `mergeable_state=blocked` |
| [#18 → develop](https://github.com/MIvi-test/vision-shield-bot/pull/18) | Уязвимая dev-зависимость urllib3 1.26.5 | [Dependency audit](https://github.com/MIvi-test/vision-shield-bot/actions/runs/43727751026/job/132853286182) — failure после успешного `uv lock --check`; остальные checks прошли; `mergeable_state=blocked` |
| [#19 → main](https://github.com/MIvi-test/vision-shield-bot/pull/19) | Проверка релизной ветки той же CodeQL-пробой | GitHub показывает critical-находку CodeQL и `mergeable_state=blocked`; релиз не выполнялся |

В PR #18 `uv audit --locked` сообщил о 20 известных уязвимостях urllib3
1.26.5 и завершился с кодом 1. Ошибка вызвана находками аудита, а не
рассинхронизацией pyproject/lock-файла. Реальные зависимости проекта
не менялись.

У всех четырёх PR перед закрытием API вернул `mergeable=true`
(конфликтов Git нет), но `mergeable_state=blocked`. Требование одобрения
также действовало: проверки не изолируют его вклад в общий статус blocked.

PR #16–#19 закрыты без merge; временные ветки `s1-19-probe-python`,
`s1-19-probe-codeql` и `s1-19-probe-audit` удалены после проверки.
Подтверждения доступны по ссылкам на закрытые PR и прогоны CI.
Код и зависимости `develop`/`main` этими PR не изменены.


Порядок работы и обязательные checks описаны в
[CONTRIBUTING.md](../../CONTRIBUTING.md#защита-main-и-develop).
Принцип CodeQL merge protection — в
[документации GitHub](https://docs.github.com/en/code-security/how-tos/find-and-fix-code-vulnerabilities/manage-your-configuration/set-merge-protection).
