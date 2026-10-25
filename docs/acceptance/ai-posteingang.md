# AI Posteingang — приёмка AP-10

**25.10.2026: AP-10A завершён. AP-10B NOT RUN; весь AP-10 остаётся открытым.**
Основа — `feature/ai-posteingang@d79ba0a`; изменения AP-10 пока не закоммичены.
Production-код, схемы extraction/review, настройки основного сайта и зависимости
не изменялись. Fake подтверждает контракты и процесс, а не качество Mistral.

Порядок работы и воспроизводимые команды: [инструкция](../posteingang.md).
Требования: [план AP-10](../plans/2026-10-25-ai-posteingang-ap10-implementation.md)
и [контракты](../plans/ai-posteingang/contracts.md).

## Выполненные проверки

| Проверка | Результат и предел доказательства |
|---|---|
| `python scripts/validate_posteingang_fixtures.py` и `--pilot-profile ap10-v1` | PASS: dataset 1.1, 42 письма / 46 страниц, 12 development + 30 holdout; 25 gold-действий holdout с явной датой. Baseline исходных 16 сохранён побайтно; новые 26 имеют отдельные source families |
| Poppler новых PDF | 28 новых страниц отрендерены; визуально просмотрены contact sheets, включая растровый AP01-39 и трёхстраничный AP01-40. Случайных обрезаний, наложений и пропавших символов не обнаружено |
| `python -m unittest discover -s tests/posteingang -p 'test_*.py' -v` в Linux | PASS, 161 тест: контракты/PDF, pilot profile, арифметика и негативные входы оценщика, guards/resume сборщика |
| `bench --site posteingang-ap10.localhost run-tests --app kanzlei_erp --module kanzlei_erp.tests.test_posteingang_acceptance` | PASS, 7 сценариев (6 + отдельный worker-checkpoint тест). Настоящий Redis/RQ и собственный worker; receive → fake → review → confirm → Task/ToDo → календарь → источник → Dokumente; потерянный ответ после commit; ручной no-actions; остановка; восстановление доставки; исправление с Completed Task; прерывание собственного worker после OCR |
| AI suite | PASS: 20 тестов, 1 ожидаемый skip S3 без Suite. Новый manifest проходит fake/общую валидацию |
| Matching suite | Первоначально 26 из 27 PASS; старый тест считал AP01-06 единственным multi-addressee. Исправлено только ожидание теста по manifest; изменённый тест повторно PASS для всех 42 писем |
| Сбор `run_posteingang_evaluation.py --site posteingang-ap10-final.localhost --actor ap10-final@example.invalid --split all --max-documents 42 --output /workspace/artifacts/posteingang-evaluation/ap10-final` из Bench Python | PASS: 42 Succeeded через отдельную RQ-очередь на новом отмеченном сайте без Suite. Кандидаты соответствуют ожиданиям runtime fixtures; автоматического подтверждения нет |
| Та же команда с `--resume` | PASS: неизменны 42 Letter, 42 Run, 252 Event, 44 File (включая setup), 42 intake receipts и hash observations. Новых обработок не было |
| CLI отчёта | Валидные входы: exit 0, `contract_process / INCOMPLETE`; `--require-gates`: exit 1; повреждённый JSON: exit 2 с безопасным отказом |
| Browser DE | AP01-17: upload → fake Run → apply → явный Customer → review → confirm → одна Task/ToDo → source/evidence → Dokumente. Внутренний срок 18.11.2026, внешний 01.12.2026; оригинал один, два контекста |
| Browser RU | AP01-41: upload с неизвестной датой → manual → явный Customer → no-actions с причиной → Completed. В БД нет Run и Tasks; язык синтетического профиля возвращён к исходному DE |
| Ruff / JSON / `git diff --check` | PASS для изменённых Python/JSON и итогового diff |

Приёмка выполнялась на одноразовых `posteingang-ap10*.localhost`; браузер — на
существующем синтетическом `posteingang-ap07.localhost`. Общие workers, Redis и
scheduler не останавливались и не очищались. Внешняя доставка сообщений в тестах
перехватывалась. Нет новых клиентов/assets/setup/schema: Node, frontend build,
повторные миграции и полная регрессия AP-02…09 не запускались повторно. Drive/S3
код не менялся; новая вертикальная проверка выполнена без Suite, прежняя
disk/MemoryS3-приёмка AP-08/09 не переобозначается как новый прогон AP-10.

## Артефакт fake-прогона и метрики

Локальный игнорируемый каталог:
`artifacts/posteingang-evaluation/ap10-final/` — `journal.json`,
`observations.json`, pending `judgments.json`, `report/report.json` и `report.md`.
Артефакты содержат синтетический OCR; в технические логи он не копируется.

| Идентичность | Значение |
|---|---|
| Run ID оценки | `753f768482ca372a522274fa` |
| Dataset / gold | `1.1 / pending_independent_review` |
| Provider / prompt / models | `fake / fake-v1 / null, null` |
| Code fingerprint | `d79ba0a5933b+local.41870c8686e56dfa` |
| Dataset SHA-256 | `f3fd42c4a19f24f502355790a16ffb346ba58093c03c256cc2480649c6257825` |
| Gold SHA-256 | `0b495b5cc61494608682dd17ad5de711ad55befdc7c585e412156d550c44023d` |
| Observations SHA-256 | `412767c5a7b4fac66a06177d16b7cd4bcfc30cfa9af42d38725ec80c297976c0` |

Сводка **30 holdout**, без смешивания с development; новые 26 также выделены в JSON:

| Метрика | Фактический результат fake-отчёта |
|---|---|
| Полнота / точность действий | NOT MEASURED: смысловые judgments pending; gold 34, предложений 32. Нулевой нижний предел числителя не является измеренным качеством |
| Полнота действий с явной датой / корректность дат | NOT MEASURED, знаменатель 25; нужны смысловые соответствия |
| Top-1 Customer | 24/26, FAIL относительно gold |
| Top-3 Customer | 26/28, FAIL относительно gold |
| Неоднозначность / несколько адресатов | 3/3 PASS в fake-наблюдениях |
| Проверенные цитаты | 262/262 PASS относительно fixture OCR; это не проверка распознавания PDF реальной моделью |
| Coverage существенных фактов | NOT MEASURED, 139 gold facts; нужна независимая смысловая проверка |
| Relative dates | NOT MEASURED до завершения judgments; отсутствие известного нарушения не выдаётся за завершённую оценку |
| Время / расходы / live-quality | NOT MEASURED |
| Runtime safety в quality JSON | NOT MEASURED: отдельные 7 выполненных интеграционных сценариев не подменяются автоматически утверждениями в observations |

Ошибки Customer в AP01-11/AP01-39 ожидаемы для unreadable fake-результата:
runtime корректно возвращает `not_found`, но semantic gold сохраняет истинного
адресата. Эти случаи не удаляются из знаменателя, чтобы не улучшать процент
исключением плохих сканов. Ошибки и знаменатели сохранены по case ID.
Итог **INCOMPLETE** является ожидаемым результатом автономной приёмки оценщика.

## Что требуется для AP-10B

1. Независимая человеческая проверка gold и разрешение спорных соответствий.
   Первые 4 holdout ранее использовались в контрактных тестах; новые 26 показаны
   отдельной группой. Fake-прогоны не являются измерением модели или разметкой человеком.
2. Разрешённая Mistral-конфигурация и бюджет; ключ только в Password Settings.
   До запроса сверить официальные конкретные модели/endpoints. Начать с development
   smoke, затем freeze и live holdout. Сейчас **live Mistral NOT RUN**.
3. Предварительно распределённый замер минимум двух участников с ротацией режимов,
   без повторного разбора одного письма тем же человеком в двух режимах. При одном
   Steuerberater возможен usability-разбор; порог ускорения остаётся NOT MEASURED.
4. Фактический billing/usage с периодом, валютой и основанием распределения;
   `attempt_count` не служит числом оплаченных запросов.
5. Все обязательные пороги PASS и записанное решение владельца. `GO_SYNTHETIC_PILOT`
   не даёт автоматического допуска реальных документов или deployment. До реальных
   данных владелец определяет ответственного, эскалацию и сроки хранения.

Удалённые uploads/временные PDF адаптер не создаёт: соответствующий cleanup —
NOT APPLICABLE. Автоматического удаления immutable истории нет. Реальные данные,
основной сайт, полный CI image build, коммит, PR и deployment не затрагивались.
