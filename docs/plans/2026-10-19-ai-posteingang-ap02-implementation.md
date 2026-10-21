# AP-02 — модель письма, приватный оригинал и права: Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** создать модель хранения AP-01 и проверяемую границу доступа к письму, истории и оригиналу, подготовив основу для приёма PDF в AP-03.

**Architecture:** пять DocTypes модуля Kanzlei ERP, единая политика доступа поверх актуальных прав Customer и серверные инварианты сохранения. Оригинал использует существующий ERP File и File/S3 слой; проверки Posteingang дополняют текущие ограничения Drive. Публичный intake, анализ и подтверждение остаются закрыты до своих этапов.

**Tech Stack:** установленный Frappe/ERPNext v16, Python >=3.14, MariaDB, текущие Suite/File extensions, FrappeTestCase; JSON Schema и fixtures AP-01 как спецификация.

---

## 1. Основание, границы и принятые технические решения

Проверено 19.10.2026: ветка `feature/ai-posteingang`, HEAD `1f6bd6b`, рабочее дерево перед подготовкой плана чистое. AP-01 завершён. Нормативные документы: [контракт v1.0](ai-posteingang/contracts.md), [workflow](ai-posteingang/workflow.json), [roadmap](2026-10-18-ai-posteingang-implementation-roadmap.md), [дизайн](2026-10-18-ai-posteingang-design.md). При исполнении повторно проверить ветку и несвязанные изменения.

AP-02 реализует:

- Все поля пяти сущностей из §2 контракта, ограничения хранения, индексы, управляемую роль оператора и выключенные настройки.
- Серверную сборку ручной записи с приватным File для проверок и последующего intake; запись без Run и без действий допустима.
- Чтение через формы, списки, Link search, API, дочерние строки, Run/Event и File с одной политикой доступа.
- Запрет обычного изменения принятого оригинала, серверного происхождения, истории и подтверждения.
- Защиту байтов и производных, включая альтернативные пути, владельца File, DocShare и уже отозванные права.
- Установку и повторную миграцию на одноразовом локальном сайте.

Не входят: публичный upload/дедупликация/replay приёма (AP-03); провайдер и runtime schema validation AI (AP-04); worker/retry (AP-05); Mandant Identifier и matching (AP-06); подтверждение, Task-поля и Task/ToDo permission hooks, операция исправления (AP-07); источник Dokumente/ZIP писем (AP-08); очередь/экран проверки (AP-09). Поля будущих операций существуют, но обычным CRUD эти операции выполнить нельзя.

Технические решения для исполнителя:

1. Схему полей брать буквально из AP-01. Не дублировать весь контракт в этом плане; тест метаданных должен перечислить поля и ограничения из §2. Несовместимое изменение требует явного обновления контракта, а не молчаливого отклонения.
2. `confirmed_customer`, а не выбранный Customer и не один только `status`, определяет принадлежность подтверждённого письма. Это сохраняет защиту после Completed → Cancelled. `correction_quarantined` имеет приоритет над обычным чтением.
3. Пока письмо не подтверждено, его читает оператор общего входа. Выбранный Customer дополнительно ограничивает **изменение**, но не превращает черновик в документ Customer. Это конкретизирует краткую формулировку «после привязки» roadmap по нормативному AP-01.
4. Базовое read право для Incoming Letter/Run/Event дать системной роли `Desk User`; Operator получает необходимую базу для будущих сервисных изменений. Дополнительные hooks всегда проверяют активного System User и Customer/Operator. Проверить фактическое наличие автоматической роли Desk User в установленной версии. Не назначать Posteingang Operator ни одному существующему пользователю.
5. Не создавать whitelisted универсальную mutation-функцию. В AP-02 обычное создание/запись бизнес-полей через REST/Desk закрыто; ручная сборка выполняется внутренним Python-конструктором из доверенного серверного кода. Это проверяет модель без преждевременного запуска AP-03. Desk показывает созданную запись; пользовательский upload появится в AP-03.
6. Производственные новые записи создаются только в Received. Completed/Cancelled/quarantine в permission tests создаются явно ограниченными test fixtures, а не экспортируемым обходом валидатора.
7. `enabled=0` запрещает новую рабочую обработку, но не чтение существующей истории. Настройки не запускают внешние вызовы; API-ключ/Mistral для этого этапа не нужны.
8. Доступ к уже скачанной пользователем копии отозвать невозможно. Приёмка проверяет следующий запрос сервера, включая повторный URL, Range, HEAD и получение готового архива.

Внешнее условие завершения: работающий **одноразовый локальный Bench/site** с проверенными project patches. Если среда недоступна, сохранить реализацию и результаты автономных проверок, но не отмечать runtime security и миграцию как пройденные. Живой S3 не обязателен: MemoryS3 покрывает логику; локальный диск проверяется реально.

## 2. Подтверждённые точки интеграции и риски

| Сейчас существует | Что требуется в AP-02 |
|---|---|
| `kanzlei_erp/hooks.py`: File mixin, document events, File/Suite permission extensions | Добавить Posteingang с сохранением прежних обработчиков и их порядка |
| `kanzlei_erp/suite_file.py:ERPAttachmentGuard` | Ограничить оригинал до операций чтения/изменения байтов, а не только после `validate` |
| `drive_storage.require_storage_access` / `require_path_access` | Сейчас ранний выход без Suite; проверить Posteingang **до** него, сохранить Drive-ветку |
| `drive_storage._managed` / `_index` | Сейчас индекс защищённых путей ориентирован на Mandant Drive; включить происхождение Posteingang и derived paths без подстановки ложного `kanzlei_drive_customer` |
| `mandant_drive.file_access_override` и `file_query_condition_override` | Первый возвращённый не-None override завершает проверку; query hooks последовательно преобразуют условие. Порядок должен исключать повторное разрешение уже запрещённого File |
| `object_storage.open_file`, `open_binding`, `url_content`, `replace_content`, `materialize` | Проверять реальный persisted File/source binding, не присланный marker; запретить замену принятого оригинала |
| `s3_http.before_request` / `authorize` | Сейчас GET отдаёт S3 presigned redirect на 300 секунд. Для Posteingang отдавать защищённый поток через приложение без bearer URL; обычные файлы сохраняют существующий путь |
| Suite `drive/api/files.py` | Есть собственные presigned download и ZIP. Нельзя оставить их альтернативой защищённой выдаче Posteingang |
| `drive_realtime.py` | Уже проверяет получателя при отправке File invalidation. Проверить новые файлы и отсутствие snippets/OCR в событиях |
| `fibu_permissions.py` Task hooks | Не менять в AP-02; защита будущих Tasks относится к AP-07 |

Локальные исходники для проверки сигнатур: `development/frappe-bench/apps/frappe/frappe/core/doctype/file/file.py`, `frappe/permissions.py`, `frappe/utils/file_manager.py`; `development/frappe-bench/apps/suite/suite/drive/api/permissions.py`, `api/files.py`, `utils/files.py`. Эти каталоги — runtime, **не место постоянного исправления**. Если нужного extension hook нет, изменение оформлять в версионируемых `deploy/frappe-mandant-access.patch` / `deploy/suite-mandant-access.patch` и соответствующих patch-upgrade tests; не редактировать исторические v1…v6 patches.

Дополнительный риск: generic REST сериализует поля целиком. Скрытый Link в форме не защищает `matching_result_json`, correction links, receipt/Event payload и прежние snapshots. Ответы с Customer-кандидатами фильтровать по текущим правам; связь с другой записью показывать только при чтении обеих. Не писать в технические логи OCR, snapshots, PDF или секреты.

## 3. Карта файлов

Новые пути, кроме явно обозначенных существующих:

| Файл/каталог | Назначение |
|---|---|
| `kanzlei_erp/kanzlei_erp/doctype/incoming_letter/{__init__.py,incoming_letter.json,incoming_letter.py}` | Основная запись, контроллер инвариантов |
| `kanzlei_erp/kanzlei_erp/doctype/incoming_letter_run/{__init__.py,incoming_letter_run.json,incoming_letter_run.py}` | Самостоятельные попытки, родительские права |
| `kanzlei_erp/kanzlei_erp/doctype/incoming_letter_action/{__init__.py,incoming_letter_action.json,incoming_letter_action.py}` | Только child table |
| `kanzlei_erp/kanzlei_erp/doctype/incoming_letter_event/{__init__.py,incoming_letter_event.json,incoming_letter_event.py}` | Самостоятельный неизменяемый журнал |
| `kanzlei_erp/kanzlei_erp/doctype/posteingang_settings/{__init__.py,posteingang_settings.json,posteingang_settings.py}` | Single, Password, пределы |
| `kanzlei_erp/posteingang_model.py` | Внутренняя сборка, capability scope, ограничения модели, append Event |
| `kanzlei_erp/posteingang_permissions.py` | Одна политика и адаптеры document/query/File/Suite |
| `kanzlei_erp/posteingang_files.py` | Определение защищённого оригинала/derivative, binding guards, выдача байтов |
| `kanzlei_erp/posteingang_setup.py` | Идемпотентная роль, defaults и индексы |
| `kanzlei_erp/tests/posteingang_helpers.py` | Только тестовые пользователи/Customer/исторические состояния |
| `kanzlei_erp/tests/test_posteingang_model.py` | DocTypes, серверные поля, история, Settings |
| `kanzlei_erp/tests/test_posteingang_permissions.py` | Права и parity list/detail/search/child |
| `kanzlei_erp/tests/test_posteingang_files.py` | Оригинал, aliases, disk/S3, HTTP/ZIP |
| `kanzlei_erp/tests/test_posteingang_setup.py` | Установка, повтор setup, индексы, defaults |

Изменяемые существующие файлы: `hooks.py`, `suite_file.py`, `drive_storage.py`, `object_storage.py`, `s3_http.py`; по фактической необходимости `drive_realtime.py`, `mandant_drive.py`, `suite_setup.py`, названные deploy patches и их тесты. Добавить DE/RU переводы только реально видимых новых labels/errors в `kanzlei_erp/translations/de.csv` и `ru.csv`. `fixtures/custom_field.json` не менять ради Task provenance: это AP-07. Production dependency `jsonschema` и PDF parser этим этапом не добавляются.

## 4. Задачи реализации

Для каждой задачи: добавить тест существенного поведения → запустить и увидеть ожидаемый FAIL → минимальная реализация → повторить затронутые тесты до PASS. Ошибка окружения не считается ожидаемым красным тестом. Шаги ниже выполнять последовательно; сохранять контрольные результаты, не ждать дополнительного разрешения между задачами. Коммитить только по поручению владельца; приведённые границы задач удобны и для отдельного review.

### Задача 1. Проверить runtime и написать первые отрицательные тесты

**Files:** `test_posteingang_model.py`, `test_posteingang_permissions.py`, `posteingang_helpers.py`.

1. Проверить `git status --short`, HEAD, AGENTS.md; прочитать AP-01 §2/§5 и локальные Frappe hooks. Проверить контейнер командой из §6, не запускать setup/migrate на неизвестном сайте.
2. Создать helpers с уникальными synthetic именами, двумя Customer, operator, operator+Customer write, reader без Operator, stranger, Website User и System Manager без Operator. Все смены пользователя восстанавливать в `finally`/tearDown; очистка File идёт через test-only cleanup после rollback.
3. Добавить тесты: неполный original binding, `received_date_known=0` с датой, UUID дубликат, Completed через обычный insert/save, чужой Run, Event update/delete, просмотр общего входа неоператором.
4. Запустить новые модули: ожидаемые отказы из-за отсутствующих DocTypes/контроллеров зафиксировать, затем переходить к schema.

### Задача 2. Добавить DocTypes, роль и настройки

**Files:** пять DocType каталогов, `posteingang_setup.py`, `test_posteingang_setup.py`, `hooks.py`.

1. Создать JSON по всем строкам §2 AP-01: standalone Letter/Run/Event; `istable=1` только Action; `issingle=1` только Settings. Letter/Run/Event — `autoname=hash`, без submit/amend lifecycle. Названия/сортировка списка не должны раскрывать имя Customer в идентификаторе.
2. Служебным полям поставить `read_only`, `no_copy`; это UI-ограничение, обязательная серверная проверка добавляется в задаче 3. Отключить generic import/export/share/delete/report, пока защищённый путь не реализован. Не включать `track_changes` с дублированием чувствительных snapshots в generic Version; Event — источник истории.
3. `reqd` ставить только безусловным scalar-полям. Условные поля проверяет контроллер; пустая таблица actions допустима. Defaults JSON и Select применять явно на сервере; пустые Date/Link на выходе приводить к null.
4. В `posteingang_setup.ensure_role()` создавать Role Posteingang Operator идемпотентно. Обеспечить её наличие **до** sync DocTypes, где она указана в DocPerm, через подходящие `before_install`/`before_migrate` hooks. `after_install`/`after_migrate` добавляют индексы и недостающие Single defaults. Не перезаписывать настроенные значения и не создавать Has Role для пользователей.
5. Settings: обе функции выключены; provider=fake; bytes=20971520, pages=50, actions=30, retries=2, timeout=120. Пределы из контракта валидируются, отрицательные значения/превышения запрещены. Секрет только Password, в сериализации максимум стандартная маска; тест гарантирует отсутствие plaintext в ответах/логах.
6. Добавить тест метаданных: обязательность, длины Data255/140/64/36, Select значения, child/Single flags, nullable unique fields. Провести первую миграцию disposable site и повторить model/setup tests.

### Задача 3. Реализовать серверные инварианты и внутреннюю сборку

**Files:** `posteingang_model.py`, контроллеры Letter/Run/Action/Event, `test_posteingang_model.py`.

Предлагаемые внутренние интерфейсы (не whitelisted):

```python
def create_received_letter(*, actor, original, receipt, operation):
    """Build Letter + private File binding + initial Event atomically.

    original: trusted server bytes + server-validated page_count;
    receipt: explicit received_date_known/date;
    operation: scoped intake key/fingerprint prepared by future AP-03.
    No client URLs, client identity, confirmation or caller-chosen UUIDs.
    """

def validate_letter(doc, previous): ...
def validate_actions(doc, previous): ...
def append_event(*, letter, actor, event_type, before, after, payload): ...
```

1. Ввести приватный context manager/capability с фиксированной операцией и конкретными именами документов; состояние хранить вне сериализуемого Document (`ContextVar`, обязательный reset). Клиентский `flags`, `ignore_permissions=True`, `frappe.flags.in_test` и `in_migrate` сами по себе не разрешают подмену серверных полей. Не делать универсальный `allow_all` режим.
2. Разрешить сборку только активному оператору при включённой функции. Сохранённые байты должны давать серверный SHA-256; page_count — результат доверенного server input, реальный PDF parser/validation поставляет AP-03. Этот helper не является безопасным публичным intake до AP-03.
3. Решить цикл Letter.original_file ↔ File.attached_to_name без nullable принятого оригинала: предварительно назначить идентичности; собрать и сохранить оба объекта в одной транзакции под узким capability, проверить reciprocal binding перед выходом. Если lifecycle требует временного пропуска проверки существования Link, разрешить его только внутри этого scope и финально проверить оба направления. Никакого commit между ними и никакой видимой «принятой» незавершённой записи.
4. Начальное состояние Received/revision=1/generation=0, `current_run=applied_run=null`, receipt автор/время сервера; Event before=0/after=1. Не перепривязывать существующий чужой File. При исключении откатить DB и созданные физические объекты через существующий rollback storage механизм.
5. Контроллер отвергает изменения original_file/hash/page_count/source/received_by/received_at/intake identity, серверных полей подтверждения/correction, необоснованной revision/generation. Прямой CRUD не вызывает переходы workflow; все состояния известны, но операция confirm пока отсутствует. Новые сервисы AP-03…07 будут получать отдельные узкие capabilities.
6. Для Action: UUIDv4 назначается сервером, сохраняется при перестановке строк/правках; исходное предложение, source_run/index и created_task защищены. Максимум 30 действий, ограничения текста/evidence, ссылки только внутри письма. В draft допустим pending и неизвестные назначения/даты; условия confirm не навязывать черновику.
7. Для Run: generation>=1, уникальность `(letter,generation)`, только собственное письмо; терминальные Run полностью неизменяемы. Пока нет worker, ordinary insert/write запрещены. Для Event: только серверная вставка, запрет update/delete/rename; payload не логируется. Сбой Event откатывает сборку письма.
8. Тесты должны проверять сохранение bytes/hash после неуспешных атак; отдельно отсутствие частично сохранённого File/Letter/Event. Не заявлять абсолютную защиту от доверенного SQL: прямой DB-доступ не граница web API.

### Задача 4. Единая политика доступа и безопасная сериализация

**Files:** `posteingang_permissions.py`, `hooks.py`, Letter/Run/Event controllers, `test_posteingang_permissions.py`.

1. Написать функцию policy, которая не вызывает рекурсивно permission check собственного DocType. Она принимает persisted letter, user и ptype; Customer проверяется через актуальный `frappe.has_permission`, не через Owner/роль Customer и не по клиентскому JSON.
2. Зафиксировать таблицу:

| Состояние записи | Чтение | Изменение через будущий сервис |
|---|---|---|
| Без confirmed_customer | активный System User + Operator | Operator; при выбранном Customer ещё Customer read/write |
| Есть confirmed_customer | активный System User + Customer read | обычные изменения закрыты; позднее только разрешённые операции AP-07 |
| correction_quarantined | System Manager + Operator + исходный Customer read | только отдельный будущий correction service |
| Guest/Website/disabled user | запрещено | запрещено |

Administrator — явное техническое исключение Frappe из AP-01, не замена actor; permission suites должны использовать обычных пользователей. Settings — только System Manager.

3. Добавить `has_permission` и `permission_query_conditions` для Letter/Run/Event. Run/Event фильтруются по родителю через EXISTS/подзапрос; orphan запрещён. Использовать безопасное экранирование/Query Builder, не интерполировать пользовательские строки в SQL. Учитывать как Frappe list User Permissions, так и doc-level Customer checks; никакого глобального кэша разрешённых Customer между запросами.
4. Проверить базовое read для читателя Customer без Operator: политика не должна оказаться недостижимой из-за DocPerm. Для draft с selected_customer автоматические Link User Permissions не должны неожиданно скрыть общий вход оператора; при необходимости `ignore_user_permissions` **только на соответствующих Link полях**, с явной ручной проверкой по этой таблице. Не отключать Customer permissions глобально.
5. Проверить отдельные Frappe REST resource, `getdoc`, `get_list`, count/search/Link queries и direct child access с поддельным parent. DocShare письма/Run/Event и владение не обходят policy; запретить создание таких shares как рабочий механизм. Share Customer остаётся законным источником текущих прав Customer.
6. Добавить sanitizer отдаваемых структур: candidate IDs/names/reasons фильтруются заново; correction links скрыты без чтения второй стороны; та же обработка применяется к embedded snapshots/receipts/Event JSON. Сохраняемые данные не менять при чтении. Перехват должен покрывать и generic REST, и list fields, а не только form onload. Если стандартный список позволяет выдавать JSON/history без sanitizer, запретить эти поля в generic list/export и отдавать через защищённый detail path. Добавить отрицательный тест на каждый включённый путь.
7. Не предоставлять generic print/export с raw JSON. В AP-02 обычная форма показывает метаданные/оригинал; необработанные OCR/история доступны только проверенным detail responses. Отозванные права проверять на **следующем запросе** с новой request context.

### Задача 5. Защитить File и storage binding

**Files:** `posteingang_files.py`, `posteingang_permissions.py`, `suite_file.py`, `drive_storage.py`, `object_storage.py`, `hooks.py`, `test_posteingang_files.py`.

1. Написать resolver защищённого источника по persisted File, Letter.original_file, attachment и S3 derivative source_file. Расхождение связей/отсутствующий родитель — отказ; нельзя снять защиту сменой `attached_to_doctype` в присланном объекте. Проверять старую и новую связь при mutation.
2. Добавить File override до любого owner/share/public shortcut. Для не-Posteingang вернуть None и сохранить прежние Drive rules. Для неверно одновременно маркированного Drive+Posteingang объекта — отказ, не выбор более широкого разрешения.
3. Query composition: сначала существующее условие обычных/Drive File, затем исключить все Posteingang и их неразрешённые aliases, вернуть только разрешённые Posteingang по политике письма. Проверить SQL и Suite ORM criteria, одиночную и batch permission ветки. Не полагаться на один hook, который вызывается лишь в Desk.
4. Встроить защиту в чтение по пути, File.get_content/get_full_path, open_binding/url_content/materialize до bytes I/O. Проверка должна действовать без Suite; Suite-specific imports выполняются только в нужной ветке. Расширить существующую нормализацию путей, а не вводить несовместимое сравнение URL строк.
5. Запретить прямую замену содержимого, privacy change, detach/reparent, rename/move/trash/delete оригинала, присвоение защищённого thumbnail URL, `create_attachment_copy` и повторное использование URL/ключа вне разрешённой операции. Автор/оператор с правом чтения тоже не может снять неизменяемость. File content dedup не должен создавать независимый alias на оригинал.
6. Новая контролируемая replacement в AP-07 получит новый File; общий physical blob пока не разрешать, если его авторизация по нескольким связям может расширить доступ. SHA-256 не уникален и сам по себе не даёт доступа.
7. Tests: persisted bad alias, path normalization/symlink, same bytes dedup, поддельные flags, старый uploader после отзыва Customer, File DocShare, derivative/thumbnail. Обычные Customer ERP attachments и Mandant Drive остаются регрессионными контрольными примерами.

### Задача 6. Закрыть HTTP и альтернативное скачивание

**Files:** `posteingang_files.py`, `s3_http.py`, при необходимости `object_storage.py`, `suite_setup.py`, `hooks.py`, `test_posteingang_files.py`; версионируемые framework patches только при отсутствии hook.

1. Для protected originals/derivatives отвечать потоком приложения с `Cache-Control: private, no-store`, корректными Content-Type/Content-Disposition/nosniff. Не выдавать клиенту presigned S3 URL или постоянный публичный URL. Авторизация — до HEAD/GET/Range/conditional response, не после чтения S3.
2. Поддержать нормальное открытие PDF: GET, HEAD, один bytes Range (206), некорректный диапазон (416). Закрыть stream и временный материализованный файл по завершении/ошибке/отключению клиента. Не делать буферизацию всего архива и не менять обычную выдачу остальных File.
3. Проверить стандартный `/private/files/...`, download_file/URL helper и Suite get_file_content/trigger_download. Если Suite генерирует presigned URL отдельной веткой, перенаправить protected File в тот же серверный streaming helper; запретить Suite tokens/public sharing для Posteingang.
4. До AP-08 **не поддерживать создание Suite ZIP, содержащего Posteingang**: отклонять всю такую selection до enqueue, включая вложенную папку и смешанную выборку, нейтральной ошибкой без чужих имён. Для уже существующего/поддельного archive manifest с protected File также отказ на status/download. Обычные Drive ZIP продолжают работать. Это ограниченный fail-closed путь, а не реализация Dokumente.
5. Проверить, что alias другого File и URL без `fid` не позволяют `s3_http.authorize` выбрать менее защищённую запись того же объекта. При конфликтующей принадлежности storage binding отказывать; честные ordinary attachment aliases сохраняют прежнее поведение.
6. Написать HTTP-level tests с настоящими сессиями: разрешённый GET → отзыв Customer → повтор того же URL/HEAD/Range запрещён; ответ никогда не содержит S3 Location. Повторить для quarantine, производного preview и Suite entry point. Проверять отсутствие имени/текста/размера скрытого документа в ошибке.

### Задача 7. Индексы, установка и повтор миграции

**Files:** `posteingang_setup.py`, `hooks.py`, `test_posteingang_setup.py`.

1. Добавить обычные индексы Letter `(status,received_at)`, `confirmed_customer`, `content_sha256`, `original_file`, `replaces_letter`; Run/Event `letter`; служебные indexes из AP-01. Hash обязательно не unique.
2. Добавить unique Letter.intake_key, nullable replacement_letter, Run `(letter,generation)`, Action `(parent,action_id)`, nullable Event.operation_key. Пустые optional unique значения сохранять как NULL, не одинаковую пустую строку. Проверить реальные MariaDB indexes/вставки, а не только JSON metadata.
3. Setup добавляет отсутствующее, не пересоздаёт DocTypes/roles и не переписывает данные. Не создавать production letters, Runs, Tasks, Customer identifiers и пользовательские назначения роли. Не запускать AI/queue. Для settings сохранить пользовательские значения при повторной миграции.
4. Проверить свежую установку приложения на одноразовом сайте с ERPNext и штатными patches; отдельно обновление существующего disposable site и второй `migrate`. Зафиксировать до/после контрольные existing Customer/Task/File, settings и Has Role. Не мигрировать основной пользовательский сайт без отдельного поручения.
5. Если нужен patch, оформить `kanzlei_erp/patches/setup_posteingang.py` в post_model_sync и запись в `patches.txt`; не дублировать одни и те же DB mutations в patch и after_migrate без необходимости. По умолчанию идемпотентного setup достаточно.

### Задача 8. Проверить границы доступа целиком

**Files:** три runtime test suites, затронутые существующие regression suites.

Минимальная матрица для параметризованных tests:

| Пользователь/условие | Draft | Confirmed A | Quarantine A |
|---|---:|---:|---:|
| Operator без Customer A | read | deny | deny |
| System User с Customer A read, без Operator | deny | read | deny |
| Operator + Customer A read | read | read | deny |
| Manager + Operator + Customer A read | read | read | read |
| Manager без Operator/Customer A | deny | deny | deny |
| Guest/Website/disabled, даже с share/owner | deny | deny | deny |
| Прежний uploader/assignee после отзыва Customer A | по роли Operator | deny | deny |

Для каждого класса проверить Letter, Run, Event, Action, File metadata и bytes; для разрешённых пользователей также parity list/detail/search. Отдельно Cancelled без confirmed_customer и Cancelled после Completed; settings off не скрывает историю. Минимум один тест каждого маршрута выполняет настоящий permission/query pipeline, не mock `frappe.has_permission`.

Тесты секретов/приватности: сериализация Settings без plaintext; sensitive payload не попадает в trace/realtime; скрытый candidate и correction link не возвращаются в JSON. Неправомерная запись должна оставить revision/history/original без изменений. Не выдавать эти тесты за проверку AP-07 Task/ToDo или конкурентной confirmation.

### Задача 9. Проверить технические формы и оформить передачу

**Files:** переводы, при необходимости labels/metadata; roadmap и этот план.

1. После миграции открыть созданное тестовое письмо обычным оператором: видны метаданные, неизвестная received_date пустая, PDF открывается. Нет рабочего действия confirm/analyze/upload, которое ещё не реализовано.
2. Отозвать доступ к тестовому подтверждённому Customer и открыть прямые URL письма и PDF в другой сессии: отказ. Settings доступны Manager, оператору — нет; secret не отображается plaintext.
3. Поскольку добавляются формы/labels, провести короткий браузерный проход на DE и RU. Адаптивный экран AP-09 не создаётся, дополнительные viewport не нужны. Проверка core Frappe форм не означает готовность пользовательского Posteingang.
4. Обновить roadmap фактическими PASS/FAIL/непроверенными пунктами. AP-02 завершать только после runtime permission/File/миграционных проверок. AP-03 автоматически не запускать.

## 5. Критерии приёмки

- Пять сущностей соответствуют v1.0; ручная запись без Run/actions валидна, original File приватный и правильно прикреплён.
- Нет публичного обхода серверных полей, Event неизменяем; завершённый Run нельзя переписать. При неудаче сборки нет частичного принятого письма/оригинала.
- Общий вход видят операторы; confirmed — только текущие читатели Customer; quarantine — строго Manager+Operator+исходный Customer. Читатель без Operator не теряется из-за базового DocPerm.
- List/search/detail/child/File/HTTP применяют одинаковую политику. Owner/DocShare/alias/derived URL не обходят её. Отзыв прав действует при следующем запросе, включая S3 download.
- Не возникает новых presigned ссылок на Posteingang; Suite ZIP с таким содержимым безопасно отклоняется до AP-08.
- Повтор setup/migrate безопасен; настройки и старые документы сохранены; роль никому не назначена автоматически; функция и внешний анализ по умолчанию выключены.
- Регрессии обычных File/S3/Mandant Drive пройдены. Task/ToDo hooks, provider, intake API и UI очереди не реализованы.

## 6. Команды и порядок проверки

Из корня репозитория, сначала только inventory без вывода секретов:

```sh
git status --short
git log -1 --oneline
docker compose --env-file .env -f .devcontainer/docker-compose.yml ps
```

Убедиться, что выбран одноразовый тестовый site. Ниже `$AP02_SITE` — его имя внутри контейнера, например `posteingang-ap02.localhost`; не подставлять production site. Создание сайта использовать по существующей локальной процедуре проекта, пароли не писать в команды/отчёт. Для fresh-install проверки на сайте предварительно установлен ERPNext; установка Kanzlei должна сама вызвать setup:

```sh
export AP02_SITE=posteingang-ap02.localhost
docker compose --env-file .env -f .devcontainer/docker-compose.yml exec -T -e AP02_SITE="$AP02_SITE" frappe bash -lc 'cd /workspace/development/frappe-bench && bench --site "$AP02_SITE" install-app kanzlei_erp'
docker compose --env-file .env -f .devcontainer/docker-compose.yml exec -T -e AP02_SITE="$AP02_SITE" frappe bash -lc 'cd /workspace/development/frappe-bench && bench --site "$AP02_SITE" migrate'
```

Второй `migrate` выполняется намеренно один раз как тест повторяемости, после сохранения контрольных данных. Suite-dependent tests требуют Suite на соответствующем disposable site; baseline без Suite проверяется на отдельном сайте либо существующей отдельной test-конфигурации, не удалением Suite из рабочего сайта.

На каждом шаге запускать конкретный созданный модуль, заменяя `test_posteingang_model` на permissions/files/setup:

```sh
docker compose --env-file .env -f .devcontainer/docker-compose.yml exec -T -e AP02_SITE="$AP02_SITE" frappe bash -lc 'cd /workspace/development/frappe-bench && bench --site "$AP02_SITE" run-tests --module kanzlei_erp.tests.test_posteingang_model'
```

После изменений общих storage hooks выполнить `kanzlei_erp.tests.test_drive_storage_security`, `kanzlei_erp.tests.test_mandant_drive`, `kanzlei_erp.tests.test_object_storage` тем же способом. Причина расширения: вмешательство в общие File permissions, alias/dedup checks, путь выдачи S3 и миграцию метаданных. Если меняется realtime — дополнительно `node --test tests/realtime-file-presence.test.cjs`; если deploy patches — `python -m unittest discover -s tests/deploy -p 'test_suite_patch_upgrade.py' -v` подходящим host Python. Остальные Task/FiBu suites не запускать без изменения их зависимостей.

Завершение:

```sh
git diff --check
```

Запустить Ruff, поддерживающий проектный `py314`, на фактически изменённых Python-файлах. Автономные 43 теста AP-01 повторять только при изменениях контрактов/fixtures/validator; их прошлый PASS не считать проверкой AP-02. Полный CI image build, live Mistral, production migration/deploy и многократные приёмочные циклы не требуются.

## 7. Передача исполнителю

> Реализуй только AP-02 по `docs/plans/2026-10-19-ai-posteingang-ap02-implementation.md` с `superpowers:executing-plans`. Сохрани несвязанную работу, следуй AGENTS.md и контракту AP-01. Создай модель хранения и реальные проверки доступа, включая оригинальные байты и альтернативные маршруты File/S3. Подтверждение, Tasks, публичный intake и AI не реализуй. Проверь профильные suites, установку/повтор миграции на одноразовом сайте и короткий DE/RU сценарий технических форм. Обнови roadmap только фактическими результатами. Не переходи к AP-03, не создавай PR и не выкатывай; коммит — по отдельному поручению владельца.

Статус этого документа: **AP-02 завершён 19.10.2026; серверная приёмка PASS, визуальная DE/RU проверка пропущена по явному решению владельца**.

## 8. Результат исполнения 19.10.2026

Реализованы задачи 1–8 и серверная часть задачи 9. Созданы пять DocTypes, внутренний конструктор и 41 профильный тест. Обычный CRUD не выполняет изменение бизнес-состояния; будущие сервисы получают отдельные серверные операции. Оригинал, Event и завершённые Run защищены от обычного изменения и удаления. Поля Task, intake API, provider, worker и очередь не добавлялись.

При проверке runtime понадобились следующие конкретизации плана:

- Frappe восстанавливает DocShare после отказа обычного permission hook и добавляет shares через OR в обоих query engines. Версионируемый framework patch добавляет обязательные ограничения после этих fallback; обычные объекты получают прежнее поведение.
- Suite получил серверные hooks для выдачи файла, запрета presigned/token и архивов, включая готовые manifest. Проверки действуют и при прямом вызове Python-функций, и через RPC legacy/v1/v2. Старые patches сохранены как v7; installer и Docker context поддерживают обновление с них.
- Нормализация учитывает legacy S3 URLs, site-qualified/absolute пути и symlinks. Защита распространяется на thumbnail aliases, зарегистрированные derivatives и мутации папки-предка. URL-чтение проверяет источник object key до выбора File alias.
- При откате savepoint новые локальные/S3 bytes очищаются сразу. Отдельный оригинал с тем же hash очищается по собственной привязке, не затрагивая первый File.
- Frappe Int по умолчанию не допускает NULL. Setup идемпотентно делает `Incoming Letter Action.source_index` nullable; контроллер сохраняет null при сериализации ручного действия. Повтор миграции с существующим null проверен.
- Generic REST/list не является типизированной проекцией приватной истории, matching или correction. Эти поля имеют отдельный permlevel и безопасную сериализацию; selected_customer/customer_reason также закрыты от общего списка. Их разрешённые представления должны появиться вместе с сервисами AP-06/07. Desk использует штатный транспорт дат Frappe; будущие canonical service payloads используют формат AP-01.

Проверки на отдельных `posteingang-ap02.localhost` и `posteingang-ap02-fresh.localhost`:

| Проверка | Результат |
|---|---|
| Модель / permissions / File+MemoryS3 / setup с Suite | 11 / 11 / 15 / 4 PASS, всего 41 |
| Те же suites без Suite | 34 PASS, 7 ожидаемых skip интеграции Suite |
| Drive storage security / Mandant Drive / Object Storage | 26 / 41 / 19 PASS |
| Patch installer upgrade, rollback, v7 snapshots/image context | 21 PASS |
| Ruff по изменённым Python, пять DocType JSON, новые строки CSV DE/RU, whitespace/diff | PASS |
| Обратная применимость обоих текущих patches к локальным runtime | PASS |
| Чистая установка ERPNext + Kanzlei без Suite | PASS |
| Две последовательные миграции с контрольными существующими данными | PASS; Customer/Task/File/Letter, ручной null source_index, Settings/Has Role не изменились |
| Реальные HTTP-сессии оператор/читатель/Manager, PDF GET/HEAD/Range, три RPC маршрута, token/ZIP deny, Settings, отзыв прав | PASS |
| Независимый code review после исправлений | Конкретных оставшихся P1/P2 блокеров не найдено |
| Визуальный проход DE/RU | Пропущен по явному решению владельца 19.10.2026; до этого CUA вернул `Local browser use is disabled in Work settings`. Визуальная корректность не проверена |

В runtime suites встречаются существующие предупреждения Frappe о deprecated `limit_page_length`; падений в итоговых прогонах нет. Тестовый код и временные HTTP-сценарии используют только синтетические данные. Рабочий сайт не мигрировался. Полный image build, PR/deployment и AP-03 не запускались. AP-02 закрыт по выполненным серверным проверкам и явному решению владельца пропустить визуальный проход DE/RU. HTTP acceptance не выдаётся за визуальную проверку; отображение технических форм и переводов остаётся непроверенным.
