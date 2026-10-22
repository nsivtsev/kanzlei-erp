# AP-05 — фоновая обработка, повторы и восстановление

> Для реализации использовать `superpowers:executing-plans`.

**Цель:** связать надёжный приём AP-03 и read-only адаптер AP-04 через durable Runs, RQ и управляемое восстановление.
**Основа:** feature/ai-posteingang, d2f700c; чистое дерево до реализации.

## Согласованные решения

- MariaDB Run — источник истины, Redis доставляет только идентификаторы; очередь long.
- Автозапуск отдельным флагом default=0, только новые загрузки. Старые письма — явным действием.
- После обнаруженного отключения/отзыва прав нужен явный новый запуск; смена конфигурации отслеживается epoch.
- Неизвестный исход запроса при падении worker — WORKER_INTERRUPTED и явный повтор, без скрытой повторной оплаты.
- Автоматические повторы только типизированных временных ошибок AP-04; успешный OCR сохраняется отдельным checkpoint.
- Успех сохраняет Run и Needs Review, без применения к черновику, поиска Customer, подтверждения или Tasks.
- Минимальные кнопки в существующей форме, DE/RU; полный экран и применение предложений — последующие этапы.
- Реальные ключи/документы, AP-06, commit/PR/deployment не входят. Live smoke NOT RUN допустим.

## Хранение и настройки

Run дополнен queued_by, source_snapshot_json, configuration_json, configuration_epoch, stage
(pending/prepare/ocr/extraction/finished), next_attempt_at, lease_token, lease_expires_at,
error_stage (dispatch/prepare/ocr/extraction/worker) и reused_ocr_from. Все поля серверные;
lease и snapshots не сериализуются обычной формой. Индексы status+next_attempt_at и
status+lease_expires_at; существующая уникальность letter+generation сохраняется.
Терминальные Runs неизменяемы, generic CRUD не расширяется.

Settings: auto_analysis_enabled=0; retry_backoff_seconds=60 (30..3600);
recovery_interval_seconds=60 (60..3600); recovery_batch_size=100 (1..500);
worker_timeout_seconds=600 (300..1800, минимум 2*request_timeout+60);
серверный analysis_configuration_epoch=1. Epoch растёт при смене включения,
провайдера/моделей/ключа/лимитов адаптера, не при повторном сохранении маски ключа.
Изменение автозапуска/recovery не инвалидирует Runs. Миграция не меняет прежние
значения, секреты, роли или результаты; сохранение Settings не вызывает сеть.

## API и атомарность

POST queue_analysis(letter, expected_revision, request_key), manual_review с тем же
вводом, cancel с обязательной reason<=1000. Только явная allowlist; actor — сессия.
get_analysis_state читает безопасный статус, revision/generation, stage, попытки,
next_attempt_at, error_code/error_stage и allowed_operations, без OCR/контента/lease.
Оператор — активный System User с текущим чтением письма и write выбранного Customer.
Чтение разрешено при выключении функции, cancel тоже; manual_review требует enabled,
но не внешней обработки. Completed/Cancelled не анализируются.

Event хранит scoped operation_key и semantic fingerprint без expected_revision;
текущие права проверяются до replay, replay до конфликта старой ревизии.
Другой payload — IDEMPOTENCY_CONFLICT. Любая бизнес-mutation повышает revision и
создаёт Event одной транзакцией; блокировки всегда Letter -> Run.

При автостарте receive сохраняет историческую квитанцию Received/revision=1, затем
в той же транзакции создаёт Run/Queued/revision=2/generation=1. Только после commit
выполняется безопасный dispatcher; Redis failure не отменяет durable intake.
Duplicate/replay не создаёт Run. Rollback не отправляет job.

## Worker, retry и recovery

1. Свежая транзакция, актуальные права/Settings, short row locks, generation/current_run.
2. Claim UUID lease на worker_timeout+60, attempt_count++, Processing, Event, commit.
3. prepare_source; перед каждым provider call свежая проверка, stage, commit, без locks.
4. OCR checkpoint валидируется и сохраняется отдельно. Extraction не повторяет OCR.
5. Публикация под проверкой generation/current_run/token/lease/прав/epoch; Succeeded и
   Needs Review атомарно. Старый ответ не пишет терминальный Run и не меняет черновик.

Временная ошибка: тот же Run Queued, письмо Processing; next_attempt_at с exponential
backoff и ограниченным Retry-After, максимум 3600 секунд. Всего 1+max_retries исполнений.
Нет sleep/RQ Retry/RetryBackgroundJobError. Неуспех — Failed/Error; остановка —
Cancelled/Error/ANALYSIS_STOPPED. manual_review активного анализа — Superseded и
generation++; cancel — Cancelled и generation++. Ошибка не означает no_actions.

OCR допускается только того же письма/hash/File/pages/provider/OCR model/schema;
перевалидируется и копируется в новый Run с provenance. Повреждённый cache даёт
явную ошибку, без скрытого платного OCR. Между письмами OCR не переиспользуется.

Frappe cron каждую минуту с ограничением recovery_interval/batch; глобальный tick
не менять (фактический текущий интервал может быть около 4 минут). Recovery доставляет
due Queued, завершает expired Running как WORKER_INTERRUPTED без autoretry, останавливает
неактуальные Runs. Работает и при выключенном AI, не вызывает provider.
Безопасные дополнительные коды: ANALYSIS_IN_PROGRESS, QUEUE_UNAVAILABLE,
WORKER_INTERRUPTED, INTERNAL_ERROR. Никаких секретов/контента/traceback в RQ или логах.

## Интерфейс

Кнопки запуска/повтора, manual review, cancel с причиной; allowed_operations с сервера.
Повтор Needs Review подтверждается с сохранением прежних правок. Один request UUID на
намерение, повтор сети с тем же ключом, блокировка двойного клика; содержимое не хранится
в localStorage. Poll 5 секунд только открытой формы в работе; не перезагружать dirty
форму, прекратить при уходе/отказе/терминальном результате. Ошибки и подписи DE/RU.

## Разработка и приёмка

Отрицательные тесты -> workflow/epoch -> auto enqueue -> worker/checkpoint -> retry/
recovery -> UI -> регрессия. Main suite test_posteingang_jobs; независимые DB/process
concurrency и настоящий isolated Redis/RQ worker, без остановки общих workers/Redis.

Проверить commit/rollback, failed enqueue, disabled auto/duplicates/replay, две вкладки,
old snapshots, duplicate jobs, cached OCR/retry budget/Retry-After, падение worker на
границах checkpoint/публикации, expired lease, manual/cancel/late result, revocations,
config changes, unreadable/unknown, сохранение drafts, запрет CRUD/Tasks, safe diagnostics.
Fake использует исходные bytes fixtures без случайного суффикса.

Запустить jobs/concurrency и AP-02 model/permissions/files/setup, AP-03 intake и AP-04 AI:
изменены workflow guards, Settings и commit-путь. Offline AP-01/AP-04/PDF — в Linux;
Node jobs+intake; Ruff/JSON/DE/RU/diff. Две миграции с контрольным снимком данных/секрета/
ролей. Fake job без Suite. Browser DE/RU upload(auto)->Needs Review->retry->manual,
отказ доступа и внешний switch off. Общие Drive/storage suites только при изменении
их общего кода. Основной сайт, глобальный scheduler, полный CI image build не трогать.

## Завершение

Обновить contracts и roadmap фактическими результатами. Схемы/gold AP-01 неизменны.
Доставка восстанавливается из DB, generation/lease исключают второе применимое
исполнение, OCR переживает extraction failure, остановка требует явного повтора,
оператор управляет через форму. Не выдавать fake за проверку качества Mistral.

## Результат исполнения

Реализовано 21–22.10.2026 в `feature/ai-posteingang`, поверх `d2f700c`, без коммита.
`posteingang_jobs.py` связывает приём и адаптер через сохраняемый в БД Run: после
commit доставляется только ID, claim выдаёт lease, OCR сохраняется отдельно,
публикация проверяет generation/current_run/token/права/epoch. Retry сохраняет
следующую дату исполнения, recovery восстанавливает доставку, а неизвестный исход
падения требует явного повтора. Терминальная история и текущий черновик не меняются.
Автозапуск остаётся выключенным по умолчанию; новые кнопки DE/RU работают через
серверный allowed_operations, без содержимого в browser storage.

Дополнительно к исходному плану введён скрытый серверный `recovery_cursor` для
ротации пакетов: просроченные Running получают приоритет, остальные актуальные Runs
не вытесняются большим backlog. Cursor не меняет modified/epoch Settings и не
раскрывает Run ID обычной сериализацией. Generic CRUD не получил обходов защиты.

Фактические проверки:

| Проверка | Результат |
| --- | --- |
| `test_posteingang_jobs` | 26 PASS |
| `test_posteingang_jobs_concurrency` | 4 PASS: независимые процессы/DB, старый snapshot, двойная доставка, остановленный worker и настоящий Redis/RQ |
| AP-02 model / permissions / files / setup | 11 / 11 / 15 / 4 PASS |
| AP-03 intake / intake concurrency | 20 / 5 PASS, включая lost COMMIT acknowledgement с MemoryS3 |
| AP-04 AI | 20 PASS, HTTP замокирован, disk/MemoryS3 |
| Linux автономные contracts / validation / PDF | 61 PASS |
| AP-01 fixture validator | 16 писем / 18 страниц, development=12, holdout=4 — PASS |
| Node jobs / intake | 5 / 3 PASS |
| Ruff, DocType JSON/field_order, DE/RU CSV, `git diff --check` | PASS |
| `bench build --app kanzlei_erp` | PASS, оба Posteingang bundle собраны |
| Две последовательные миграции одноразового сайта без Suite | PASS: письма, терминальные Runs, Settings, encrypted secret и роли сохранены |
| Настоящий fake job без Suite | PASS |
| Живой Mistral smoke | **NOT RUN** — нет разрешённой конфигурации; не блокирует AP-05 |

Основная регрессия выполнена на одноразовом `posteingang-ap05.localhost`; model —
на прежнем тестовом `posteingang-ap02-fresh.localhost`. Новые jobs suites и отдельный
приёмочный wrapper intake concurrency изолируют только доставку несвязанных Drive
realtime notifications при fixture commits:
реальные SQL, транзакции, очереди и проверяемый workflow не заменяются. RQ-проверка
использовала собственную очередь и одноразовый worker, без очистки общего Redis и
без остановки общих workers. Структуры AP-01, extraction/review schemas и gold fixtures
не изменялись. Общий Drive/Object Storage код не менялся, поэтому их полные suites
не запускались; защищённое чтение disk/MemoryS3 покрыто профильной регрессией.

Браузер на отдельном сайте без Suite: DE загрузка исходного development PDF с
автостартом → Needs Review → явный повтор → Needs Review; DE/RU ручной переход;
RU повтор с подтверждением сохранения правок, отказ пользователю без Operator,
выключенный внешний анализ без кнопки запуска — PASS. Все проверенные письма
сохранили pending review и отсутствие Tasks. Повторный выбор файла на RU остался
**NOT VERIFIED** из-за timeout file chooser инструмента браузерной автоматизации;
локализованный диалог проверен, но это не полный повтор RU upload-сценария.
Синтетические UI-аккаунты отключены, автозапуск и внешняя обработка выключены после
проверки. Основной сайт и глобальный scheduler не менялись.

Отрицательные тесты выявили и проверили исправления безопасного завершения при DB
outage, классификации RQ timeout, starvation recovery, повреждённого OCR checkpoint,
epoch при повторном сохранении и dirty/navigation race в polling. Повторное
независимое ревью не нашло оставшихся конкретных P1/P2. Fake и mocked HTTP проверяют
контракт и workflow, не качество Mistral. Полный CI image build, AP-06, реальные
документы, commit/PR/deployment не выполнялись.
