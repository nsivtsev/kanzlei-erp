# AP-04 — настройки и адаптер OCR/извлечения Mistral

> Для реализации использовать `superpowers:executing-plans`.

**Цель:** серверный адаптер читает принятый приватный PDF, выполняет OCR и извлечение и возвращает проверенный результат AP-01 для будущего AP-05.

**Архитектура:** отдельные OCR → extraction; fake/Mistral используют общую валидацию. Нет публичного API анализа, записи Run, изменения Letter, фоновых заданий, поиска Customer или Tasks.

**Основа:** `feature/ai-posteingang`, `94a0262`, чистое дерево перед реализацией. Коммит, PR и deployment не входят в поручение.

## Согласованные решения

- Живой smoke не блокирует этап; только синтетический development PDF при разрешённой конфигурации.
- REST через установленный Frappe `requests`; PDF передаётся Base64, без публичной ссылки, Files API, временного PDF и fallback.
- Конкретные модели: `mistral-ocr-4-1`, `mistral-small-2603`. Провайдер fake и оба выключателя по умолчанию выключены для внешней обработки.
- Единственная новая production-зависимость — `jsonschema==4.26.0`; runtime-копии схем внутри Python-пакета проверяются на совпадение с AP-01.

## Последовательность и интерфейсы

1. Отрицательные тесты схем → runtime resources → локальный Draft 2020-12 registry с FormatChecker. Raw schema исключает server-only verification; provider projection разворачивает ссылки и исключает условные правила, которые полностью проверяются локально. Unknown fields, duplicate keys, NaN/Infinity, невозможные даты, неправильные страницы и превышения лимитов отвергаются.
2. `prepare_source(letter_name, *, actor)` читает через `object_storage.open_file`, проверяет активного System User/Operator, текущую сессию, права, binding, неизменность hash и ограниченный размер. Completed/Cancelled/quarantine не анализируются. Адаптер не совершает commit/rollback; AP-05 обязан предоставить свежие транзакционные границы и generation guard перед вызовами.
3. `recognize(source)` и `extract(source, ocr_result)` повторно проверяют доступ/Settings перед вызовом. `analyze_letter` объединяет шаги. Все функции внутренние, не whitelisted. Типизированные результаты имеют безопасный repr; ожидаемые ошибки возвращают code/stage/retryable/retry_after_seconds, без provider exceptions. Успешный OCR доступен вызывающему коду при последующей ошибке extraction.
4. Fake сопоставляет hash с manifest, читает только OCR/extraction, удаляет verification и проходит общий валидатор. Не читает ground truth/review/catalog/Tasks и не вызывает сеть. Все 16 cases проверяются как контракт; holdout не попадает в prompt.
5. OCR: POST `/v1/ocr`, все страницы; include_image_base64/include_blocks/extract_header/extract_footer=false. Требовать индексы 0…N−1, преобразовать в 1…N; 100000 символов/страницу, 2000000 всего. Все страницы пусты → unreadable/unknown, без второго вызова.
6. Extraction: POST `/v1/chat/completions`, temperature=0, stream=false, n=1, tool_choice=none, без tools/reasoning. Prompt `ap04-extraction-v1`, fake `fake-v1`. Передавать OCR по страницам и receipt metadata отдельно от system prompt. Структура запроса ограничена 192000 UTF-8 bytes, max_tokens=32768. Не обрезать и не добавлять chunking.
7. Evidence: NFKC + whitespace, exact substring той же страницы → matched; непустая несовпавшая цитата → unverified; null → missing. Никакого fuzzy match или вычисления относительных сроков. Canonical extraction повторно валидируется исходной схемой.
8. HTTP: TLS, без redirects/retries; connect=min(5, timeout), read=timeout≤120; контроль времени чтения; decoded response cap OCR 16 MiB, extraction 8 MiB; закрытие session/response всегда. Не обещать строгий wall-time задания.
9. Settings: сохранить текущие defaults, права и лимиты; при внешнем Mistral обязательны секрет и конкретные модели, запрещены latest/URL/whitespace/control characters. get_password только перед запросом. Сохранение настроек не вызывает сеть. Новые описания/ошибки DE/RU. Миграции не заменяют секрет, модели, значения и роли.

| Ошибка | Код | Retry |
|---|---|---|
| Конфигурация, 401/403, неизвестная модель | CONFIGURATION_INVALID | нет |
| Timeout/connection/429/5xx | PROVIDER_UNAVAILABLE | возможен в AP-05 |
| Неполный/повреждённый OCR | OCR_FAILED | нет |
| JSON/schema/date/page extraction | INVALID_RESULT | нет |
| Размер/контекст/результат | LIMIT_EXCEEDED | нет |
| Отзыв прав/выключение/config drift | ANALYSIS_STOPPED | нет |

## Проверки

- Новый AI suite: 16 fixtures, все uncertainty/deadline enums, JSON/schema/evidence/limits, fake network isolation, strict HTTP envelopes, timeout/429/5xx/401/redirect/read error/length finish, no sensitive logs/repr, право/роль/settings drift между шагами, disk/MemoryS3 и без Suite, отсутствие записей/изменений Letter/Run/Action/Event/Task.
- Профильные suites AP-02 model/permissions/files/setup и AP-03 intake: изменение Settings и новый потребитель защищённых байтов требуют регрессии. Общие storage hooks не менять; при изменении расширить соответствующие suites.
- AP-01 validator/unittest; Ruff, JSON/DE/RU CSV, diff. Wheel build и import runtime resources без docs на Python 3.14; pip check не получает новых конфликтов.
- Две миграции одноразового сайта с контрольным снимком настроек, ключа, ролей и прикладных записей.
- DE/RU браузер: Manager получает ошибку latest, сохраняет synthetic key/конкретные модели, reload маскирует секрет. Operator без System Manager не получает Settings. Без живых внешних вызовов.
- Живой smoke отмечать отдельно; NOT RUN допустим и не доказывает качество Mistral. Полный CI image build и основной сайт не затрагивать.

## Источники (проверены при планировании 21.10.2026)

- https://docs.mistral.ai/models/ocr-4-1
- https://docs.mistral.ai/models/mistral-small-4-0-26-03
- https://docs.mistral.ai/studio/document-processing/basic_ocr
- https://docs.mistral.ai/api/endpoint/chat
- https://docs.mistral.ai/studio/conversations/structured-output/custom
- https://raw.githubusercontent.com/mistralai/client-python/main/src/mistralai/client/models/ocrpageobject.py
- https://pypi.org/project/jsonschema/

## Результат исполнения

**AP-04 завершён 21.10.2026.** Реализованы внутренние prepare_source/recognize/extract/analyze_letter, fake/Mistral, runtime schemas и bounded HTTP через requests. AP-05 получает проверенный OCR отдельно от extraction, версии схемы/prompt/запрошенных моделей и безопасные типизированные Failure; AP-04 не пишет Run и не меняет Letter. Worker обязан обеспечить свежую транзакцию и generation guard перед каждым шагом.

| Проверка | Результат |
|---|---|
| AP-04 AI (включая 16 fake cases, disk/MemoryS3, HTTP faults, revocations, safe diagnostics) | 20 PASS |
| AP-04 без Suite на отдельном новом сайте | 19 PASS, один ожидаемый S3 skip |
| AP-02 model / permissions / files / setup | 11 / 11 / 15 / 4 PASS |
| AP-03 intake disk/MemoryS3 | 20 PASS |
| Offline AP-01 contracts / AP-04 validation / PDF | 43 / 14 / 4 PASS в Linux (61 всего) |
| AP-01 dataset validator | 16 писем / 18 страниц, development/holdout сохранены, PASS |
| Python 3.14, jsonschema 4.26.0, pip check | PASS, новых конфликтов нет |
| Wheel и импорт package resources из установленного wheel вне repo, без docs | PASS; dependency pin включён |
| Две последовательные миграции | PASS: все контрольные Letter/Run/Action/Event/Receipt/File/Task/Customer rows, Settings, encrypted secret fingerprint и Operator assignments неизменны |
| DE/RU browser | PASS: latest отклоняется понятным сообщением, конкретные версии сохраняются, reload; сохранённый ключ проверен сервером и маскируется в JSON; Operator не открывает Settings |
| Секрет после ошибочного сохранения формы | PASS: serialized savedocs regression не пишет snapshot; browser synthetic key отсутствует в Error Log error/metadata |
| JSON / новые CSV DE/RU / Ruff / diff | PASS |
| Независимое read-only ревью после исправлений | Оставшихся конкретных P1/P2 нет |
| Живой Mistral synthetic smoke | NOT RUN: разрешённая конфигурация живого API не использовалась, этап не блокирует |

Защитные сценарии сначала воспроизведены как RED: невозможная дата/страница/enum/лимит; surrogate Unicode в OCR и non-ASCII API key; повторная проверка private/binding; plaintext unsaved Settings serialization и Frappe developer snapshot при ошибке serialized savedocs. Затем исправлены и включены в проходящие suites. Новый SettingsValidationError совместим с ValidationError/HTTP417 и исключается штатным SecurityException guard из error snapshots, без глобальных изменений Frappe logging. as_dict маскирует пароль и до save, а обычное сохранение/get_password сохраняет значение.

Серверные suites с Suite выполнялись на `posteingang-ap02-fresh.localhost`; новый `posteingang-ap04-nosuite.localhost` использован для варианта без Suite, DE/RU и двух миграций. Перед каждым migrate и после него сравнивался полный снимок контрольных таблиц, Single и ролей; секрет сравнивался по fingerprint зашифрованного значения. Браузерный сценарий использовал fake и отключённый внешний анализ; отдельный unittest запрещал HTTP при save Settings. Синтетические UI-пользователи после проверки отключены, оба выключателя на этом сайте выключены. Основной `development.localhost` не мигрировался.

Ограничение среды: первый запуск всей standalone discovery на macOS завершился ошибками неизменённого AP-03 PDF subprocess (512 MiB RLIMIT_AS); те же все 61 tests прошли в изолированном Linux runtime с закреплёнными инструментами AP-01. AP-03 parser не менялся. Host validator и новые validation tests также PASS. Это не выдаётся за успешный запуск PDF sandbox на macOS.

Логи внутри container: `/tmp/ap04-final2-{ai,model,permissions,files,setup,intake}.log`, `/tmp/ap04-final-ai-nosuite.log`, `/tmp/ap04-contracts-linux.log`, `/tmp/ap04-migrate-{1,2}.log`. Скриншоты DE/RU сохранены вне репозитория. Fixtures/manifest/gold AP-01 не менялись. Общие Drive/Object Storage hooks не менялись; повтор их полных suites не требовался. Живой S3 и качество Mistral не проверялись. Full CI image build, AP-05, основной сайт, commit, PR и deployment не выполнялись.
