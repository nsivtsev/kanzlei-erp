# AI Posteingang — контракт v1.0 (AP-01)

Дата фиксации: 18.10.2026. Основание: [дизайн](../2026-10-18-ai-posteingang-design.md), [roadmap](../2026-10-18-ai-posteingang-implementation-roadmap.md), согласованный AP-01.

## 1. Статус, версии и границы

Это нормативный контракт последующих этапов, а не работающий Posteingang. AP-01 поставляет документацию, JSON Schema Draft 2020-12, синтетические PDF, gold-ответы и автономные проверки. DocTypes, migrations, API, permissions hooks, jobs и UI ещё не реализованы. Изменение несовместимого формата требует новой версии схемы; сохранённые Runs и receipts сохраняют исходную версию. Провайдерные модели/SDK и их живые лимиты фиксируются в AP-04.

Один PDF — одно письмо с приложениями. Несколько отдельных PDF принимаются по отдельности. Пачка с несколькими письмами не разделяется в MVP. Дата получения может быть неизвестна. Несколько Mandanten в одном письме означают Needs Clarification.

| Термин | Значение и источник истины |
|---|---|
| Source / original | Неизменяемые принятые байты PDF в приватном ERP File |
| Ground truth | Ручная синтетическая разметка для оценки; никогда не вход живого анализа |
| OCR | Текст по страницам, полученный из PDF; не ground truth |
| Extraction | Серверно валидированные предложения AI, ещё не решения сотрудника |
| Evidence | Номер страницы и цитата; `matched` означает точное совпадение с нормализованным OCR, а не доказательство правильной интерпретации |
| Candidate | Customer из разрешённого набора ERP и объяснение совпадения; не ответ модели |
| Review | Сохранённые решения человека, включая отклонённые предложения |
| Confirmation | Атомарная фиксация Customer, 0…N Tasks, ToDo, receipt и Event |
| Revision | Версия изменяемого состояния письма для optimistic concurrency |
| Generation | Поколение фонового анализа; запрещает публикацию старого результата |
| Correction | Административная замена ошибочного подтверждения с изоляцией исходника |

### Интеграции и владельцы этапов

| Компонент | Существующая точка / будущий результат | Этап |
|---|---|---|
| Данные и доступ | Customer; будущие Incoming Letter/Run/Action/Event и Settings | AP-02 |
| PDF intake | ERP File, текущие File/S3 hooks; будущий intake service | AP-03 |
| OCR/extraction | Будущие fake/real адаптеры на этом контракте | AP-04 |
| Очередь | Frappe enqueue-after-commit, future Runs и recovery | AP-05 |
| Подбор | Customer, будущий Mandant Identifier и permission-aware matching | AP-06 |
| Подтверждение | `Task.kanzlei_customer`, стандартное assign_to/ToDo | AP-07 |
| Документы | Текущий `mandant_documents.py`, новый источник Posteingang | AP-08 |
| Проверка | Будущая Desk page, DE/RU | AP-09 |
| Измерения | Сценарии, настоящий provider и отложенный набор | AP-10 |

В `fibu_permissions.py` текущие `linked_task_has_permission` и `task_query_conditions` ограничивают FiBu Tasks по FiBu Package. Связь только через `kanzlei_customer` такого ограничения не даёт. AP-07 дополняет hooks для происхождения Posteingang, сохраняя прежнюю семантику остальных Tasks. Аналогично новые File guards должны композиционно дополнять текущие Suite/storage hooks.

Оригинал остаётся прикреплённым к Incoming Letter; физического переноса через границу Suite Drive нет. Dokumente получает новый источник. Один File отображается один раз при нескольких связанных Tasks. Назначение — обычный ToDo. Внешняя дата не записывается в календарный срок: `exp_start_date = exp_end_date = internal_date`, либо оба `null`.

## 2. Модель хранения для AP-02

### Общие обозначения

Все DocTypes находятся в модуле Kanzlei ERP. Стандартные `name`, `owner`, `creation`, `modified`, `modified_by` сохраняются, но не заменяют фактическую дату получения и явного автора подтверждения. Даты — ISO `YYYY-MM-DD`; время API/JSON — RFC3339 UTC, Frappe Datetime хранится через стандартную конверсию site timezone. Пустые Date/Link в JSON нормализуются в `null`, не в `""`. UUID Action в runtime — UUIDv4; стабильные UUIDv5 в fixtures — только воспроизводимые тестовые идентификаторы.

В таблицах: **R** — обязательное поле с момента создания, **C** — обязательное при указанном условии, **—** — nullable. **S** — только сервер; **H** — человек через сервис с проверкой прав; **M** — System Manager. Прямой CRUD защищённых полей не заменяет эти проверки. `index` означает обычный search index, `unique` — индекс уникальности. Все неотмеченные поля индексом не снабжаются. Data имеет length 140, кроме явно указанного. JSON хранится в Frappe JSON field, проверяется схемой/серверными инвариантами, никогда не исполняется.

### Incoming Letter (самостоятельный DocType)

| Поле | Frappe тип | Обяз./default | Автор | Индекс / ограничения |
|---|---|---|---|---|
| original_file | Link File | R | S | index; private, not folder, attachment на это письмо, неизменяем |
| content_sha256 | Data(64) | R | S | index, **не unique**; lowercase hex, hash сохранённых байтов |
| page_count | Int | R | S | 1…50 |
| source | Select | R / `erp_upload` | S | `erp_upload`; будущий `suite_drive` только AP-11 |
| received_by | Link User | R | S | текущий инициатор, неизменяем |
| received_at | Datetime | R / now | S | index, время приёма неизменяемо |
| received_date_known | Check | R / 0 | H | явный ответ, не выводится из времени загрузки |
| received_date | Date | C / null | H | дата iff known=1; изменение после подтверждения только correction |
| status | Select | R / Received | S | index; состояния из workflow.json |
| revision | Int | R / 1 | S | >=1, каждое сохранённое бизнес-изменение +1 |
| analysis_generation | Int | R / 0 | S | >=0, fencing фоновых результатов |
| current_run | Link Incoming Letter Run | — | S | только принадлежащий письму Run, не означает применение к черновику |
| applied_run | Link Incoming Letter Run | — | S | источник текущих AI-предложений; null в ручном режиме |
| sender | Data(255) | — | H | текущая проверяемая версия, исходное значение остаётся в Run |
| document_date | Date | — | H | не received_date и не срок |
| letter_type | Data(255) | — | H | свободная категория, не команда |
| summary | Small Text | — | H | <=4000 символов |
| fact_evidence_json | JSON | R / `{}` | S | evidence по полям; ручные изменения имеют origin=human в Event |
| matching_result_json | JSON | — | S | permission-filtered candidates, matching contract ниже |
| selected_customer | Link Customer | — | H | выбор черновика, до подтверждения не окончательная принадлежность |
| customer_reason | Small Text | C | H | <=1000, обязателен при подтверждении |
| confirmed_customer | Link Customer | C | S | index; обязателен при Completed, неизменяем после него |
| review_outcome | Select | R / pending | S | `pending`, `actions`, `no_actions`; по результату confirm |
| no_actions_reason | Small Text | C | H | <=1000, когда принято 0 действий |
| clarification_reason | Small Text | C | H | <=1000, при Needs Clarification |
| actions | Table Incoming Letter Action | R / `[]` | H через сервис | максимум 30; пропуск строки не равен отклонению |
| confirmed_by | Link User | C | S | при Completed |
| confirmed_at | Datetime | C | S | при Completed |
| intake_key | Data(64) | R | S | unique; SHA-256 от user scope + request key |
| intake_fingerprint | Data(64) | R | S | hash canonical параметров и content_sha256 |
| confirmation_receipt_json | JSON | C | S | immutable result: letter, revision, confirmed_customer, action→Task, event |
| replaces_letter | Link Incoming Letter | — | S | index; новая исправленная запись указывает исходную |
| replacement_letter | Link Incoming Letter | — | S | unique nullable; только одна непосредственная замена |
| correction_reason | Small Text | C | M через сервис | <=1000; при замене |
| correction_quarantined | Check | R / 0 | S | блокировка обычного доступа к исходнику и Tasks |
| cancellation_reason | Small Text | C | H через сервис | <=1000; при Cancelled |

В AP-02 добавить составной индекс `(status, received_at)`, отдельные индексы confirmed_customer и content_sha256. Hash не уникален: разрешённая correction может принять те же байты с новым File. Не создавать обязательный Customer при intake. Информация о выбранном Customer черновика не меняет принадлежность File до confirm.

### Incoming Letter Run (самостоятельный DocType)

| Поле | Тип | Обяз./default | Автор | Индекс / ограничения |
|---|---|---|---|---|
| letter | Link Incoming Letter | R | S | index; права только через родителя |
| generation | Int | R | S | >=1; unique составной `(letter, generation)` |
| schema_version | Data | R / 1.0 | S | версия canonical extraction |
| prompt_version | Data | R | S | неизменяемая версия шаблона, fake-v1 для fake |
| provider | Select | R | S | `fake`, `mistral`; не выбирается из документа |
| ocr_model | Data | C | S | конкретная версия реальной модели, null только для fake/manual |
| extraction_model | Data | C | S | аналогично; плавающий latest не принимается |
| status | Select | R / Queued | S | `Queued`, `Running`, `Succeeded`, `Failed`, `Cancelled`, `Superseded` |
| queued_at | Datetime | R | S | время принятия попытки |
| started_at | Datetime | — | S | при первом исполнении |
| finished_at | Datetime | C | S | любой терминальный статус |
| attempt_count | Int | R / 0 | S | число сетевых исполнений текущей логической попытки |
| error_code | Data | C | S | только безопасный код, не тело ответа/секрет |
| ocr_pages_json | JSON | — | S | support schema `pages`; весь текст <=2 млн символов |
| result_json | JSON | C | S | extraction.schema.json, обязателен для Succeeded |

Терминальный Run неизменяем. Сетевой retry может повышать attempt_count одного нетерминального Run; явный повтор анализа создаёт новое поколение и Run. Результат старого поколения сохраняется как Superseded без изменения черновика. Отдельный поздний ответ после финализации вообще не переписывает Run. Сетевые вызовы вне транзакционной блокировки. Детали leases/recovery — AP-05, без изменения правил revision/generation.

### Incoming Letter Action (дочерняя таблица)

Стандартные parent/parenttype/parentfield/idx используются как Frappe child table; idx никогда не идентичность действия. Чтение/экспорт/доступ через дочерние API требуют чтения родителя.

| Поле | Тип | Обяз./default | Автор | Ограничения |
|---|---|---|---|---|
| action_id | Data(36) | R | S | UUID; unique составной `(parent, action_id)` |
| source_run | Link Incoming Letter Run | — | S | null для ручного действия |
| source_index | Int | — | S | 0-based индекс в Run, null при ручном вводе |
| original_proposal_json | JSON | — | S | неизменяемый исходный extraction action |
| title | Data(140) | R | H | 1…140 |
| description | Small Text | R | H | 1…4000 |
| decision | Select | R / pending | H | `pending`, `accept`, `reject` |
| assigned_to | Link User | — | H | активный System User с чтением Customer |
| assignment_decision | Select | R / pending | H | `pending`, `assigned`, `unassigned` |
| assignment_reason | Small Text | C | H | <=1000, явно unassigned |
| external_date | Date | — | H | подтверждённая внешняя дата |
| external_date_basis | Small Text | C | H | <=1000; обязательно при внешней дате, в том числе вычисленной человеком |
| internal_date | Date | — | H | Task dates, не вычисляется из внешней даты автоматически |
| scheduling_decision | Select | R / pending | H | `pending`, `scheduled`, `unscheduled` |
| scheduling_reason | Small Text | C | H | <=1000, явно unscheduled |
| rejection_reason | Small Text | C | H | <=1000, при reject |
| needs_planning | Check | R / 0 | S | у принятого действия true iff нет исполнителя или internal_date |
| evidence_json | JSON | R / `[]` | H через сервис | максимум 5 evidence; проверяются по OCR, либо явно unverified/missing |
| created_task | Link Task | — | S | после confirm, защищено от CRUD/копирования |

Различия canonical JSON: evidence_json сериализуется в `evidence`; служебные source_run/original_proposal/created_task не входят в принимаемый от браузера review payload. Совпадение source_index в нескольких строках запрещено. У всех предложений применённого Run перед confirm должно быть явное решение; ручные новые действия имеют source_index=null. Сервер не доверяет needs_planning из клиента: пересчитывает и отвергает противоречие.

При применении нового Run сервер сначала записывает старый canonical review snapshot в Event; новые предложения получают новые UUID. Ручные правки заменяются только после явного `acknowledge_replace=true`. Простое завершение анализа этого не делает. Task остаётся обычной рабочей задачей; AI больше её не обновляет.

### Incoming Letter Event (самостоятельный DocType)

| Поле | Тип | Обяз./default | Автор | Индекс / ограничения |
|---|---|---|---|---|
| letter | Link Incoming Letter | R | S | index, наследует доступ письма |
| event_type | Data | R | S | operation/event из workflow; также save_review/apply_result/correction_disposition |
| actor | Link User | R | S | реальный инициатор, worker указывает инициатора Run |
| occurred_at | Datetime | R / now | S | index, серверное время |
| revision_before | Int | R | S | 0 только для приёма |
| revision_after | Int | R | S | обычная mutation = before+1 |
| generation | Int | R | S | поколение после операции |
| payload_json | JSON | R / `{}` | S | дифф/причина/snapshot; ограничен доступом письма, не технический лог |
| operation_key | Data(64) | — | S | unique nullable; hash scope+operation+request key |
| payload_fingerprint | Data(64) | C | S | для идемпотентной mutation |
| result_json | JSON | C | S | immutable receipt для replay |

Серверный append-only журнал, обычный delete/write запрещён. Полные snapshots допустимы здесь как защищённая история, но не в logger/error trace. Изменение письма, Task/ToDo и Event фиксируются одной DB-транзакцией; откат не оставляет успешный receipt. Новый event для неуспешного запроса не маскируется под успешно выполненную mutation.

### Posteingang Settings (Single)

| Поле | Тип | Обяз./default | Автор | Ограничения |
|---|---|---|---|---|
| enabled | Check | R / 0 | M | выключатель новых intake/analysis/confirm, не исторического чтения |
| external_analysis_enabled | Check | R / 0 | M | отдельно от функции, fake не отправляет данные наружу |
| provider | Select | R / fake | M | fake/mistral; автоматического fallback нет |
| ocr_model | Data | — | M | до внешнего включения заполняется в AP-04 |
| extraction_model | Data | — | M | аналогично |
| api_key | Password | — | M | только сервер, не в JSON обычных API и не в логах |
| max_file_bytes | Int | R / 20971520 | M | положительно, не выше hard cap v1 |
| max_pages | Int | R / 50 | M | 1…50 |
| max_actions | Int | R / 30 | M | 1…30 |
| max_retries | Int | R / 2 | M | 0…2 дополнительных технических повторов |
| request_timeout_seconds | Int | R / 120 | M | 1…120; согласование с provider API в AP-04 |

Схемы определяют hard caps: 20 адресатов, 20 идентификаторов на адресата и 20 иных идентификаторов, 5 evidence на факт/действие, 1000 символов цитаты, 4000 summary/description, 140 title, 100000 OCR на страницу, 2000000 всего. Настройки могут снижать пределы; повышение hard cap требует изменения версии контракта. Превышение не обрезается молча. Ошибки PDF — INTAKE_INVALID_PDF / INTAKE_ENCRYPTED_PDF / INTAKE_LIMIT; provider/OCR/schema — PROVIDER_UNAVAILABLE / OCR_FAILED / INVALID_RESULT / LIMIT_EXCEEDED. Сетевые timeout не означают no_actions.

### Защищённое происхождение Task (реализация AP-07)

| Поле | Тип | Default / индекс | Правило |
|---|---|---|---|
| kanzlei_incoming_letter | Link Incoming Letter | null / index | server-only, read_only, no_copy; нельзя снять или заменить обычным save |
| kanzlei_incoming_action_id | Data(36) | null | UUID принятого действия; server-only, no_copy |
| kanzlei_incoming_action_key | Data(64) | null / unique | SHA-256 от letter + action_id, server-only, no_copy |
| kanzlei_incoming_needs_planning | Check | 0 | server-only первоначально; последующее планирование обычной Task снимает метку по актуальным полям |

Существующие kanzlei_customer/exp_start_date/exp_end_date сохраняются. Внешняя дата и её основание читаются через защищённую Action связь, не дублируются незащищённым сниппетом. Источник/Customer не меняются обычным редактированием; отдельные ограничения FiBu продолжают применяться. Копирование Task не копирует provenance/key и не может получить скрытое содержание: AP-07 проверяет создание копии по актуальным правам, не переносит приватные evidence/вложения. Нельзя использовать канонический Work Schedule occurrence key для Posteingang.

## 3. Форматы, evidence и решения

Нормативные схемы: [common](schemas/common.schema.json), [extraction](schemas/extraction.schema.json), [review](schemas/review.schema.json), [manifest](schemas/manifest.schema.json), [смысловая разметка](schemas/annotations.schema.json), [служебные fixtures](schemas/support.schema.json). Все JSON objects закрыты через additionalProperties=false; nullable явно; отсутствующая коллекция — [], неизвестный факт — null, пустая строка неизвестным не считается. Структурные default в JSON Schema не подставляются автоматически. Sender, letter_type и имя адресата ограничены 255 символами; summary/address — 4000.

### AI, canonical extraction и OCR

Extraction schema описывает **серверный canonical результат**, не сырой ответ модели. AP-04 строит provider request schema из того же формата, исключая server field `verification`. При поступлении сырого ответа server-only поля модели отвергаются, а verification добавляется заново сервером. Customer IDs, usernames, Task IDs, decision и confirmation недопустимы в обоих extraction форматах. Никакие значения из текста не выбирают произвольный endpoint/URL, инструменты или API-ключ.

Evidence: `{page, quote, verification}`. Нумерация PDF — 1-based; source_index действия — 0-based. `matched` требует непустую цитату, которая является подстрокой OCR **той же** страницы после NFKC и схлопывания whitespace. `unverified` сохраняет непустую неподтверждённую цитату с явной пометкой. `missing` имеет quote=null и не притворяется доказательством. Отсутствие evidence — [] вместо выдуманной цитаты. Bounding boxes в v1 отсутствуют.

Не смешивать два случая: OCR может содержать ошибку, а точная цитата может быть интерпретирована неверно. Text match не подтверждает юридическую дату или Mandant. Человек проверяет оригинал; его исправление получает origin=human и основание в Event. OCR хранится отдельно от чистой синтетической разметки.

Верхний extraction содержит sender/document_date/letter_type/summary как facts `{value,evidence}`, recipients с именем/адресом/типизированными идентификаторами, other_identifiers с holder_role, readability, action_assessment, actions и warnings. Читаемость readable/partial/unreadable; action_assessment actions_found/none_found/unknown. Unreadable требует unknown; none_found/unknown требуют пустой actions. Ошибка сервиса вообще не валидный extraction и не none_found.

Каждый deadline содержит kind/date/wording/anchor/evidence:

| kind | date | wording / anchor |
|---|---|---|
| explicit_date | реальная календарная дата | формулировка обязательна; anchor nullable |
| relative | null | формулировка обязательна; указанный ориентир либо null |
| not_found | null | wording=null, anchor=null, evidence=[] |
| uncertain | null | противоречивая/неполная формулировка обязательна, anchor nullable |

Никаких относительных дат, рассчитанных моделью или backend по календарю. Человек может подтвердить внешнюю дату вручную, указав основание, и отдельно выбрать внутреннюю. Нет автоматического вычитания рабочих дней или предположения о доставке.

### Review и неизвестные значения

Review schema — payload сохранения: schema_version, mode (draft/clarification/confirm), expected_revision, selected_customer, customer_reason, no_actions_reason, clarification_reason, human_verified, actions. Поля созданного Task и server-only provenance сюда не принимаются.

Draft допускает pending, неизвестного Customer, отсутствие назначений и дат. Clarification требует причины. Confirm требует human_verified=true, известного доступного Customer, причины выбора и решения по каждому предложению. Действия могут быть вручную добавлены с source_index=null. Удаление AI-строки без reject не считается решением. При accepted_count=0 no_actions_reason обязателен, при accepted_count>0 он null.

У accept отсутствие assigned_to допустимо только с assignment_decision=unassigned и причиной; отсутствие internal_date — только с scheduling_decision=unscheduled и причиной. Сотрудник видит отдельные подтверждения «оставить без исполнителя» / «срок запланировать позже»; автоподстановки нет. В Task показывается требует планирования. Внешняя дата требует external_date_basis; её отсутствие не означает отсутствия обязанности. Для reject обязательна причина. Completed фиксирует весь набор одним вызовом, без частичного confirm.

## 4. Состояния, операции и конкурентность

Нормативная машинная таблица — [workflow.json](workflow.json). Каждая строка содержит from/to/operation/initiator/requires/revision_delta/generation/event. Не перечисленные переходы запрещены. Сохранение draft и применение result — операции без смены status; это не дополнительные переходы в матрице.

| From | To |
|---|---|
| Received | Queued, Needs Review, Needs Clarification, Cancelled |
| Queued | Processing, Error, Needs Review, Needs Clarification, Cancelled |
| Processing | Needs Review, Error, Needs Clarification, Cancelled |
| Needs Review | Completed, Needs Clarification, Queued, Cancelled |
| Needs Clarification | Needs Review, Queued, Cancelled |
| Error | Queued, Needs Review, Needs Clarification, Cancelled |
| Completed | Cancelled только create_correction |
| Cancelled | нет |

Operator означает текущего System User с ролью. При наличии confirmed_customer любые действия дополнительно проверяют его права; прочие customer-specific mutations проверяют selected_customer. Worker выполняет полномочия инициатора, не технического администратора. feature_enabled ограничивает новые изменения, но безопасная отмена и чтение истории доступны при выключении. Выполнение старого Run после выключения не публикует содержимое.

Предикат analysis_enabled означает `provider == fake OR external_analysis_enabled`; fake не требует внешнего включения. Предикат проверяется и при запуске, и при публикации результата. Отключение внешнего анализа во время real provider запроса запрещает применение пришедшего ответа.

### Будущие сервисные операции

Это контракты сервисов, а не существующие whitelisted endpoints. AP-03/05/07 реализуют их с allowlist полей и серверными транзакциями. Каждый ответ отдаёт revision; ссылки и названия фильтруются текущими правами.

| Операция | Минимальный ввод | Результат и условия |
|---|---|---|
| receive | проверенный сервером upload stream; request_key; received_date_known; received_date | letter, revision=1, status=Received, original_file; после надёжного сохранения; не принимает произвольный URL |
| queue_analysis | letter, expected_revision, request_key | run, generation, revision, Queued; enqueue after commit, snapshot версии настроек |
| manual_review | letter, expected_revision | Needs Review, revision; из queued/processing повышает generation и делает активный Run неприменимым |
| save_review | letter, review payload | revision; разрешено в Needs Review/Needs Clarification, либо сначала manual_review; полный набор без удаления непринятых решений |
| apply_result | letter, run, expected_revision, acknowledge_replace | новый draft, revision; Run Succeeded текущего поколения; обязательный snapshot прежнего draft и acknowledge_replace=true при замене |
| mark_clarification | letter, expected_revision, reason | Needs Clarification, revision; инвалидирует незавершённый Run |
| confirm | letter, request_key, полный review mode=confirm | immutable receipt с action→Task; Customer, Tasks, ToDo, Event одной транзакцией |
| cancel | letter, expected_revision, reason, request_key | Cancelled, revision; для Completed запрещено; generation++ |
| create_correction | old_letter, expected_revision, request_key, reason; новый upload либо reuse_original | old=Cancelled/quarantined, replacement=Received, новый File; никаких новых Tasks |
| correction_disposition | replacement, expected_revision, original_action_id, disposition, reason | Event; только manager+operator; проверка текущего Task и явного решения по нему |

`stage_result` у первого анализа переводит Processing→Needs Review, сохраняя предложения в Run. Перед review требуется явное apply_result; UI AP-09 может объединить просмотр и применение отдельным подтверждаемым действием. При повторном анализе существующий draft сохраняется во время Queued/Processing и после них; публикация Run не заменяет его.

### Revision и fencing

Любое сохранённое бизнес-изменение, включая смену статуса worker, повышает revision на 1 и пишет Event в той же транзакции. Ожидаемая ревизия обязательна у пользовательской mutation; stale вкладка получает REVISION_CONFLICT без частичного сохранения. Worker сопоставляет generation и ожидаемый текущий статус под короткой блокировкой строки, а не устаревшую revision на момент enqueue. Нет network I/O под этой блокировкой.

queue_analysis, cancel, correction и ручной выход из Queued/Processing повышают generation. Обычное редактирование draft запрещено в Queued/Processing: сначала manual_review инвалидирует Run. Только один Run на `(letter,generation)`. Дубли запуска одного job не получают второй publish; AP-05 обеспечивает lease/recovery. Поздний результат возвращает STALE_RUN/не публикуется; старый snapshot и текущие Tasks не меняются.

### Идемпотентность

request_key — непустая строка <=80 символов, генерируемая клиентом один раз на намерение и сохраняемая до получения результата. Fingerprint — SHA-256 canonical JSON (sort_keys, UTF-8, compact separators), явные null и include schema_version; для receive включает hash байтов/известность и дату получения, не имя временного upload. Scope intake — user + operation; confirm/correction — letter + operation. Expected_revision — transport guard, **не часть semantic fingerprint**, чтобы replay не менял идентичность операции.

Порядок проверки: аутентификация и текущий доступ → поиск receipt по operation_key → сравнение semantic fingerprint → replay либо IDEMPOTENCY_CONFLICT → проверка expected_revision для нового запроса → блокировка и атомарная mutation. Успешный replay со старой revision возвращает тот же результат, если доступ сохранён. После Completed confirm с тем же итоговым semantic payload возвращает существующий receipt даже с новым request_key; другой payload получает ALREADY_COMPLETED, без новых Tasks. Исправление идёт только через create_correction.

Уникальные intake_key/Event.operation_key/Task.action_key и транзакционная блокировка защищают реальные параллельные запросы. Их работоспособность проверяется реальными DB-сессиями в AP-03/07; AP-01 проверяет только согласованность спецификации. Поиск одинаковых байтов — отдельная проверка, не синоним request replay. Недоступный дубль не раскрывает письмо, его имя, Customer или существование связи; безопасная нейтральная ошибка/отказ без указания объекта. Административный reuse_original в correction разрешён явно и обходит только duplicate-intake решение, не права или File guards.

### Уточнение AP-03: серверная квитанция приёма

Аддитивное уточнение v1.0 от 20.10.2026. `Incoming Letter Intake Receipt` — самостоятельный серверный DocType: name = SHA-256 canonical [actor, receive, request_key] (PK), actor (Link User, R), payload_fingerprint (Data, R), letter (Link Incoming Letter, R/index), outcome (Select created/duplicate, R), result_json (JSON, R), created_at (Datetime, R). Все поля серверные, запись неизменяема; generic CRUD/list/export/DocShare запрещены, включая Administrator. Исходный request key и байты здесь не хранятся.

`receive` принимает ровно один multipart file, request_key, received_date_known (`true`/`false`) и received_date (YYYY-MM-DD или null). Не принимает File/URL/Customer/actor/hash/pages от клиента. Разрешён только служебный routing cmd самой receive. Ответ: schema_version=1.0, outcome=created/duplicate, letter, original_file, revision/status на момент операции, replayed. Квитанция хранит ответ без replayed. Для duplicate квитанция не меняет письмо/Event/revision; существующая дата получения сохраняется. При нескольких доступных совпадениях предпочтителен не-Cancelled, затем самый ранний received_at/name. Недоступный дубль даёт CANNOT_ACCEPT_FILE без сведений о причине/объекте.

POST get_intake_result(request_key) ищет только namespace текущего активного System User с ролью оператора и повторно проверяет права письма; неизвестный/недоступный результат одинаково возвращает outcome=unknown. Replay и чтение результата доступны при enabled=0; новые приёмы/duplicate receipts запрещены. Результат не означает актуального статуса письма: для него читается сама запись.

Ограничения PDF: 20 MiB/50 страниц или меньшие Settings. pypdf работает в отдельном процессе с CPU/memory/wall limits; шифрование и структурные ошибки отклоняются, растровый скан допустим. Читаемость/действия определяются последующими этапами. Полный результат и новые bytes сохраняются до отправки успешного ответа. При неизвестном исходе COMMIT удаление оригинала допускается только после подтверждения отсутствия durable references.

## 5. Права и идентификаторы

### Матрица доступа

Все чтения дополнительно требуют базового Frappe read для соответствующего DocType и System User. Table ниже — обязательные ограничения поверх него, не безусловная выдача права. Guest/Website User запрещены независимо от DocShare/назначения/владения. Administrator остаётся техническим superuser Frappe, не обычной рабочей ролью; его использование аудируется и не применяется для обхода инициаторских проверок worker.

| Объект / путь | Не подтверждено | Подтверждено | Correction quarantine |
|---|---|---|---|
| Список, форма, Link search, API письма | Posteingang Operator | текущий Customer read | Manager + Operator + исходный Customer read |
| Original File metadata/bytes, alternate download URL | та же политика письма | та же политика письма | та же quarantine policy |
| OCR, previews, история, Run, Action | та же политика письма | та же политика письма | та же quarantine policy |
| Dokumente / ZIP | письмо ещё не принадлежит Customer | Customer read + File/letter read | обычным читателям скрыто |
| Task detail/list/search/calendar | Tasks ещё нет | Task permission + Customer read + ограничения FiBu при наличии | Manager + Operator + исходный Customer read + Task permission |
| ToDo, assignments, DocShare snippets | Tasks ещё нет | проверка исходной Task/Customer; не выдаёт доступ назначением | такое же ограничение, включая метаданные |
| Изменение/confirm | Operator + выбранный Customer write/read; Task create при accepted actions | обычный повтор только receipt | отдельный correction service |
| Settings | только System Manager | только System Manager | только System Manager |
| Отправка provider | инициатор ещё Operator, функция/внешний анализ включены, актуальное поколение | запрещена | запрещена |

Роль Posteingang Operator создаётся в AP-02 без автоматической раздачи. До выбора Customer оператору доступен общий вход. После подтверждения роль сама по себе Customer read не заменяет; читатель Customer без Operator может читать подтверждённое письмо. File owner/DocShare не обходят политику attachment. HTTP bytes, previews, ZIP и generic File CRUD требуют тех же проверок. Task/ToDo read не раскрывает закрытые title, due-date или фрагменты источника. Отзыв Customer permission действует при следующем чтении и replay; клиентский кэш/превью не должны продолжать выдавать защищённые ответы.

### Mandant Identifier (дочерняя таблица Customer, реализация AP-06)

Поле Customer `kanzlei_identifiers` — Table Mandant Identifier. Поля: identifier_type (Select, R), raw_value (Data140,R), normalized_value (Data140,R,S), jurisdiction (Data140,C), authority (Data140,C), other_type (Data140,C), verification_source (Small Text<=1000,R,H), verified_by (Link User,R,S), verified_at (Datetime,R,S). Типы: de_steuernummer/de_tax_id/de_vat_id/other. Изменение raw/type/context обнуляет старое подтверждение и требует явной повторной проверки сотрудником; AI не пишет таблицу. Индекс `(identifier_type, normalized_value, jurisdiction)`, глобальной уникальности нет: одинаковые значения у клиентов должны давать неоднозначность.

Правила нормализации v1 (это ключи сравнения, не юридическая валидация номера):

- Общая основа — NFKC + trim; ведущие нули сохраняются, значения не приводятся к числу.
- de_steuernummer — удалить пробельные разделители и `/`; принимать для совпадения только ASCII цифры и **точно одинаковые** вручную заданные jurisdiction и authority. Без контекста номер сохраняется как предложение, точное совпадение не заявляется. Региональные форматы не преобразуются друг в друга.
- de_tax_id — удалить whitespace, затем только ASCII цифры. Не угадывать тип/валидность из длины; проверка контрольных цифр не входит в v1.
- de_vat_id — удалить whitespace и uppercase; сохранять буквенный префикс и ведущие нули; только ASCII A–Z/0–9. Не удалять произвольную пунктуацию.
- other — NFKC + trim, без удаления внутренних знаков/пробелов и без смены регистра; совпадают только одинаковые other_type + authority + jurisdiction.

Customer.tax_id не мигрируется и не используется как универсальный Steuernummer. Источник истины — только явно проверенная запись справочника. Совпадение типов/context обязательно. Номер отправителя/представителя, даже совпавший с Customer, не является сигналом адресата. Неизвестная holder_role не получает точного match. Имя/адрес — только дополнительные кандидаты; при конфликте идентификаторов нет молчаливого выбора.

Matching result (AP-06): `{schema_version:"1.0", candidates:[{customer, reasons:[{kind, identifier_type, evidence}], conflicts:[]}], ambiguity:"none|multiple_matches|multiple_addressees|not_found"}`. Не более 3 candidates; kind `exact_identifier`, `name_address`. В reasons не выдаются сырые справочники или имена недоступных клиентов; ссылки/evidence проверяются как у письма. Возвращается только актуально читаемый набор. Модель никогда не получает каталог клиентов. Поисковый результат не устанавливает confirmed_customer и не заменяет человеческий customer_reason.

## 6. Контролируемое исправление

create_correction требует System Manager **и** Posteingang Operator, ожидаемую ревизию, причину и действующие права чтения/изменения исходного Customer; новый Customer проверяется при его выборе и подтверждении. Операция атомарно:

1. Сохраняет исходное подтверждение/receipt/Action→Task без изменений.
2. Переводит исходник Completed→Cancelled, устанавливает correction_quarantined=1 и Event с причиной.
3. Создаёт одну новую Received запись с reciprocal links и собственным приватным File. reuse_original читает исходные байты после проверки доступа; storage binding может переиспользовать физический blob только если File guards не допускают обхода attachment permissions. Новый File имеет собственную привязку.
4. Не копирует подтверждение, Customer, назначения, решения или action UUID. Новые обязательные поля приёма выставляются сервером, неизвестность received_date вводится явно.
5. Старые Tasks остаются связаны с исходником, не меняют Customer, статус, описание и сроки автоматически. Completed Task остаётся Completed. Все обычные читатели старых Tasks/File/OCR/ToDo, включая ошибочного Customer, теряют доступ из-за quarantine.

Перед confirm замены manager+operator должен явно разобрать каждую старую созданную Task. `cancelled` допускается только после ручной отмены обычным механизмом ERP; `historical` требует причины и сохраняет статус выполненной работы. Disposition хранится в Event исходника и замены с исходным action_id/Task, автором, временем и текущим Task status. Статусы Task проверяются снова при confirm замены. Это не создаёт второе подтверждение исходника и не переиспользует task_creation_key. Неотменённая историческая Task остаётся в quarantine; новая работа явно подтверждается в замене. Исторический статус сам по себе не возвращает обычный доступ к ошибочному содержимому.

Quarantine исходника в MVP не снимается автоматически. Административный просмотр требует Manager+Operator+исходный Customer read; прав нового Customer недостаточно. Отображение двусторонней ссылки/имени разрешено только при чтении обоих документов. Неверная связь не превращается в утечку через breadcrumb, ошибку или Event. Обычное cancel до Completed также терминально и не создаёт Tasks.

## 7. Синтетические данные и отложенная оценка

Набор: `kanzlei_erp/tests/fixtures/posteingang/manifest.json`. 16 PDF, 18 страниц, 12 development / 4 holdout. Holdout: AP01-04, AP01-07, AP01-11, AP01-13. Остальные development. AP01-01 и AP01-16 — одна source_family, оба development. Варианты одного оригинала запрещено помещать в разные split; одинаковые PDF в основном наборе запрещены, точный дубль описан machine scenario без второго файла.

| Case | Сценарий | Ожидается |
|---|---|---|
| AP01-01 | простой запрос/явная дата | Completed, 1 Task |
| AP01-02 | 3 действия/разные сроки | 2 accepted Tasks, третье явно rejected как уже выполненное |
| AP01-03 | информация | Completed, 0 Tasks, no_actions_reason |
| AP01-04 | одинаковый номер у двух доступных клиентов | Needs Clarification, 2 кандидата |
| AP01-05 | неизвестный клиент | Needs Clarification, нет выбранного Customer |
| AP01-06 | несколько Mandanten | Needs Clarification, подтверждение запрещено |
| AP01-07 | относительный срок | без вычисленной внешней даты; внутреннюю выбирает человек |
| AP01-08 | срок не найден | Task без даты с причиной, needs_planning |
| AP01-09 | противоречивые сроки | uncertain, Needs Clarification |
| AP01-10 | неизвестна дата получения | received_date=null |
| AP01-11 | плохой растровый скан | unreadable/unknown, пустой OCR, ручное уточнение |
| AP01-12 | письмо + 2 приложения | evidence со страниц 2/3 |
| AP01-13 | prompt injection | инструкции остаются текстом, 1 обычное принятое действие |
| AP01-14 | номер отправителя принадлежит другому клиенту | адресат Alpha по имени/адресу, не Gamma по номеру отправителя |
| AP01-15 | нет исполнителя | Task без назначения с причиной, needs_planning |
| AP01-16 | исправленный скан | отдельный File/UUID, provenance AP01-01 |

Все организации, адреса, ссылки и пользователи искусственные. Справочные номера имеют synthetic type `other`; их не следует принимать за действительные налоговые номера. Отдельные contract-примеры типизированной нормализации описаны выше; AP-06 добавляет их executable matching tests.

`sources.json` — только вход генератора PDF. `texts/` — чистый исходный текст по страницам. `annotations/` — независимый смысловой ground truth: фактические адресаты/действия/сроки с gold_action_id и цитатами чистого источника, а также Customer-кандидаты с provenance. `ocr/` — отдельный синтетический OCR для проверки canonical evidence. `extraction/` — ожидаемые canonical ответы fake/ручного fallback, `reviews/` — согласованные решения. Каталог клиентов используется только серверными fixtures, не provider input. Для плохого скана PDF целиком растровый, скрытого текста нет, OCR не получает чистую разметку. Его annotations сохраняют реальное действие и явный срок 20.11.2026, хотя ожидаемый unreadable extraction имеет actions=[]: recall оценивается по annotations, поэтому пропуск не исчезает из denominator. PDF имеют видимый synthetic watermark; качество плохого скана намеренно снижено.

Annotation evidence не имеет поля verification: это ручная опора на чистый источник, а не утверждение о совпадении с OCR. Candidate provenance ссылается на адресата/идентификатор; валидатор проверяет точное типизированное совпадение с каталогом либо точные имя/адрес этих synthetic fixtures. Это проверка корректности разметки, не реализация ranking/fuzzy matching AP-06. `annotations/`, `texts/`, `sources.json`, `reviews/`, manifest и каталог нельзя добавлять во вход модели или OCR; разрешён только исходный PDF и результаты реального OCR. Holdout данные не используются для генерации fake-ответов при измерении модели.

`scenarios.json` содержит 64 пары состояний и 11 declarative operation scenarios: upload retry, точный дубль, confirm retry, конфликт ревизии, конфликт ключа, поздний Run, отзыв прав, rollback assignment, retry Completed, correction и ручной переход во время Run. `purpose=specification_only_not_runtime_acceptance`. Валидатор проверяет их внутренние ожидания, не выполняет сервисы ERP и не доказывает конкурентность или безопасность реальных файлов.

### Расширение и критерии пилота AP-10

Цель — 42 письма: неизменные 12 development и 30 holdout (добавить 26 holdout). До запуска модели завершить gold-разметку; спорные случаи разрешает второй проверяющий. Не смотреть holdout при настройке prompt. Зафиксировать SHA-256 manifest и версию набора перед измерением; новый исправленный gold повышает dataset_version и требует нового evaluation run. Открыто хранимый holdout — процедурное разделение, а не техническая секретность для разработчика.

Показатели на holdout вычисляются по семантическому сопоставлению предложенных и эталонных действий (one-to-one, дубли предложений — false positive). Не равенство заголовков. Отчёт содержит numerator/denominator, case IDs, типы ошибок, модель/prompt/schema/dataset версии и пропуски. Недостаточный denominator не считается успехом. В holdout нужно >=15 действий с явным сроком, все классы deadline, отсутствие действий, неизвестный/неоднозначный клиент, плохой скан и несколько адресатов.

| Метрика | Порог |
|---|---|
| Action recall = найденные gold actions / все gold actions | >=95% |
| Action precision = корректные предложения / все предложения | >=90% |
| Recall действий с явным внешним сроком | 100% |
| Явные даты: точно верная календарная дата соответствующего действия | 100% |
| Вычисленные без человека даты relative | 0 |
| Top-1 Customer в однозначных кейсах | >=95% |
| Top-3 Customer среди доступных gold-кандидатов | >=95% |
| Обнаружение неоднозначности/нескольких Mandanten | 100% соответствующих случаев |
| Проверенные цитаты: page + точное совпадение с OCR | 100% |
| Evidence coverage существенных фактов читаемых документов | >=95% |
| Медианное активное время сотрудника | <=70% ручного процесса |
| Утечки, автоподтверждения, дубли Tasks | 0 |

Существенные факты: отправитель/адресат, идентификатор, действие, явная дата и исходная формулировка срока; summary не оценивается как набор точных цитат. Evidence coverage denominator — gold-факты с видимым подтверждением на readable страницах, а не только те, которые модель решила вернуть. Ошибки unreadable не исключаются из общего recall действий; отдельно показывается ручной режим и покрытие автоматического извлечения. Top-1/Top-3 считаются до ручного исправления, по доступному Customer scope кейса; неоднозначный случай не попадает в denominator однозначного Top-1.

Для времени — сопоставимые по сложности группы, ротация сотрудников и назначение письма каждому сотруднику только в одном режиме. Замер активной работы включает проверку PDF, правки, планирование, подтверждение и поиск результата. Полное время с ожиданием OCR измеряется отдельно; медиана ratio по сопоставимым группам <=0.70. Минимум два сотрудника, каждый работает в обоих режимах на разных письмах. Зафиксировать группы и порядок до эксперимента. Экспертный ground truth не создаётся участником прямо перед его timed-разбором.

Отдельно измеряются доля правок по полям/действиям, доля manual fallback, задержки, стоимость письма и страницы (с retries/OCR), numerator/denominator каждого класса ошибок. Численного ценового порога в AP-01 нет: отчёт обязан показать фактические расходы. Fake provider не измеряет Mistral quality и скорость живого сервиса.

## 8. Воспроизведение и приёмка AP-01

Инструменты изолированы от pyproject.toml и production. Для обычного Python >=3.10:

```sh
python3 -m venv /tmp/kanzlei-posteingang-tools
/tmp/kanzlei-posteingang-tools/bin/python -m pip install -r scripts/requirements-posteingang.txt
/tmp/kanzlei-posteingang-tools/bin/python scripts/validate_posteingang_fixtures.py
/tmp/kanzlei-posteingang-tools/bin/python -m unittest discover -s tests/posteingang -p 'test_*.py' -v
/tmp/kanzlei-posteingang-tools/bin/python scripts/generate_posteingang_fixtures.py --output /tmp/posteingang-regenerated
pdftoppm -scale-to 1300 -png /tmp/posteingang-regenerated/pdfs/AP01-12.pdf /tmp/ap01-12
```

Авторские direct versions закреплены в requirements-posteingang.txt и manifest. Для повторения байтов PDF важны ReportLab/Pillow и фиксированные seed/date/fonts/metadata; validator не требует совпадения версий инструментов при одном лишь чтении. Генератор пишет только pdfs/, не переписывает manifest, тексты или ответы. Изменённый hash после осознанной замены PDF обновляется отдельно после просмотра; тест generator reproduction не должен «чинить» gold автоматически.

Валидатор проверяет schemas/formats, закрытые поля, календарные даты, UUID, лимиты, page refs/цитаты, hash/count, review invariants, ожидаемые Tasks/assignments/dates, путь внутри fixture root (включая symlinks), split/source_family и declarative scenarios. JSON Schema refs разрешаются только из локального registry, без сетевого fetch. PDF проверяется pypdf, визуальная проверка всех страниц — через Poppler отдельно.

Миграции, frontend assets и браузерные сценарии AP-01 не нужны. GitHub CI остаётся существующим release gate; CI pipeline этим этапом не меняется, поэтому автономные AP-01 команды запускаются явно. Нельзя выдавать passing fixtures за проверку уже реализованных прав, File hooks, транзакций или Mistral.

Перед реальными документами остаются внешние условия AP-04/AP-10: выбранный и разрешённый контур обработки, условия провайдера и политика удаления производных данных. [Исследование](../../research/2026-10-03-ai-mandantendaten.md) — вход для этого решения, не подтверждённый договор. AP-01 не назначает юридический срок хранения. До отдельного решения нет автоматической очистки принятых оригиналов/истории, необработанных Runs или тайного внешнего fallback.
