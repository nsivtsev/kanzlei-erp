# AP-07 — проверка письма, атомарное подтверждение и защищённые Tasks

> Реализация: `superpowers:executing-plans`; отрицательные тесты → реализация → профильная проверка. Коммит только по отдельному поручению.

## Основа и результат

Ветка `feature/ai-posteingang`, исходный commit `78a29c3`; AP-01…AP-06 завершены. Новых production-зависимостей нет.

Run хранит неизменяемое предложение; Letter/Action — текущую проверку человека. Одна транзакция подтверждения сохраняет Customer, решения, 0…N Tasks, стандартные ToDo, Event и квитанцию. Исправление создаёт связанную замену с карантином ошибочного исходника.

Согласовано: нейтральные уведомления без Customer/заголовка/документа/дат; запрет штатного copy исходных Tasks; явное применение нового Run либо сохранение старого черновика с причиной; Manager+Operator создаёт исправление и разбирает Tasks, затем оператор нового Customer подтверждает замену. Неизвестный Customer блокирует confirm; отсутствие исполнителя/внутреннего срока требует явного решения и причины.

## Проверка

- Runtime review schema — неизменённая AP-01 v1.0; локальный registry, строгий JSON, реальные даты. Production не импортирует scripts.
- Отдельный строгий полный `document_fields`: sender, document_date, letter_type, summary. Исходные факты/evidence остаются в Run; ручные изменения аудируются.
- Полный набор Action, серверные UUID и source_index; нельзя подменить происхождение или удалить предложение вместо reject. Evidence verification пересчитывает сервер.
- Исторические черновики проверяются hard caps AP-01, текущий max_actions применяется к новым наборам/добавлениям. Нет обрезания.
- `get_review_state(letter)` — защищённая текущая проекция, proposal, applied_run, разрешённые операции, доступная квитанция.
- POST `apply_result(letter, expected_run, expected_revision, request_key, acknowledge_replace)` — только текущий валидный Succeeded Run; snapshot старого draft в Event, новые UUID, никаких старых решений и автодат. Customer сохраняется.
- POST `add_manual_action(letter,title,description,expected_revision,request_key)` — серверный UUID, pending, без Run.
- POST `save_review(letter,review,document_fields,request_key)` — Needs Review/Needs Clarification; clarification требует причины. Из Received/Error сначала manual_review.
- POST `confirm(letter,review,document_fields,request_key,retained_result)` — только Needs Review. Если последний Run не применён, retained_result={run,reason} либо явное применение. Последний Run учитывается при блокировке нескольких адресатов.
- Выбор Customer только AP-06; Customer/reason в review должны совпадать с сохранёнными. Мутации: активный Operator/System User, письмо, Customer read/write, enabled для обычной проверки/confirm; внешний provider не требуется.
- Идемпотентность Event: user+letter+operation+key; fingerprint без expected_revision; права → replay → revision. Запрет неизвестных входных параметров, безопасные ошибки.

## Confirm и Tasks

Проверить human_verified, известного Customer с причиной, одного адресата, отсутствие pending, reject reason, no_actions_reason при 0 accepted, Task create при accepted, активных System User исполнителей с Customer read. Unassigned/unscheduled требуют отдельных причин; external_date требует основания; needs_planning соответствует пропускам.

Под Letter lock сохранить review и Completed/confirmed fields, затем Tasks/назначения, Action→Task, Event/receipt в одной транзакции. Completed до назначения виден только внутри незакоммиченной транзакции. Любая ошибка откатывает Tasks, ToDo, shares и callbacks.

Task provenance: kanzlei_incoming_letter (Link/index), kanzlei_incoming_action_id (UUID), kanzlei_incoming_action_key (unique nullable Data64), kanzlei_incoming_needs_planning. Источник/ключ no_copy/server-only, Customer неизменяем. Ключ Letter+UUID не переиспользует Work Schedule key.

Task: подтверждённые title/экранированное description, customer, work_kind=Task, обе exp-даты=internal_date либо null, обычный начальный статус; без FiBu/Project/Work Schedule. External/evidence/PDF не копируются. ToDo без срока получает явный null. Рабочие поля Task редактируются обычным разрешённым способом; Action — неизменяемый результат. Закрытые Tasks не требуют планирования. Удаление/rename/provenance mutation запрещены.

Повтор Completed с тем же semantic payload даже новым ключом возвращает квитанцию, иной — ALREADY_COMPLETED. Текущие права и Task links проверяются заново. Lock+unique key исключают второй набор.

## Исправление

- POST create_correction: Manager+Operator, original Customer read/write, reason/revision/key; reuse_original либо один новый multipart PDF с AP-03 validation, явная received_date. URL/File входом запрещены.
- Атомарно: исходник Completed→Cancelled/quarantine, самостоятельный приватный File и Received replacement, reciprocal links/Events. Нет копирования Customer/Run/UUID/решений/назначений или автозапуска AI.
- Исправление/административный разбор доступны при enabled=0 через узкий trusted путь.
- Старые Tasks не меняются автоматически. Каждая disposition: cancelled только после обычной отмены ERP либо historical с причиной. Completed остаётся Completed. Event обеих записей, revision атомарно; статусы проверяются снова при confirm replacement.
- После разбора оператор нового Customer может подтвердить замену, не получая скрытых старых сведений. Quarantine не снимается. Административное чтение: Manager+Operator+old Customer read. Reciprocal link только при чтении обоих.
- Replay не создаёт вторую замену; сбой File/receipt/commit очищает только новые bytes.

## Права и UI

- Task base permissions AND active System User AND current Customer read AND FiBu AND source quarantine; DocShare/owner/assignment не обходят. Обычные Tasks без происхождения не меняются.
- Покрыть form/REST/list/search/export/print/calendar/tree/overview, ToDo/assignment/Notification Log, File/Comment/Communication/Version и child snippets. Проверять parent/dependencies, не раскрывать закрытые cached subjects.
- Закрыть raw child lookup, assignment/bulk status/mapping ignore_permissions. Ранние Task/ToDo persistence guards; lock order sorted Letters → Run → Tasks → ToDo; fresh reads после lock wait.
- Узкие framework hooks для нейтрализации назначения до title/queue serialization и copy до no_copy. Без runtime monkeypatch. Сохранить точный predecessor patch; installer upgrade/repeat/rollback tests. notify=0 недостаточно.
- Нейтральные Notification Log/RQ/email только с Task link; повторная проверка recipient перед созданием/отправкой. Block Duplicate/keyboard/Python/v2/Project copy, обычные Tasks без изменений. Ручной перенос текста не объявляется предотвращённым.
- Существующая Incoming Letter form: PDF/facts/evidence, apply/replace, full action editing/accept/reject/assignee/dates, manual action/save/clarification/preview/confirm/human checkbox, readonly result, admin correction/dispositions.
- Серверный поиск исполнителей — active System Users с Customer read. Human checkbox/retain decision сбрасываются после изменений/нового Run. Двойной клик блокируется, unknown network повторяет тот же intent/key. Общий dirty flag учитывают AP-05/06; late/route/permission ответы не подменяют буфер. Нет browser storage; DE/RU, text escaping.

## Порядок и приёмка

1. Review schema/semantics/snapshots/UUID.
2. Apply/manual/save/clarification/retain.
3. Task guards/provenance/ToDo/notifications/copy.
4. Confirm/receipt/rollback/concurrency.
5. Correction/quarantine/dispositions/replacement confirm.
6. UI/DE/RU/polling.
7. Регрессия, contracts, roadmap с фактическими результатами.

Обязательные сценарии: 16 AP-01 cases без изменения gold, manual/noRun, pending/reject/null/date/source tamper/omitted row/evidence/limits; replace/retain; 0/1/N Tasks и пропуски планирования; сбои второй Task/assignment/Event/receipt/commit; same/different key/payload/Completed/newkey/revoked replay; независимые DB processes для double confirm и гонок с analysis/customer/cancel/correction/Task update и old snapshot; Guest/Website/disabled/DocShare/exports/ToDo/metadata/raw API; wrong Customer correction с Completed Task и старым assignee; отсутствие AI/enqueue и document/secret в diagnostic/RQ/notifications; все штатные copy пути.

Одноразовый Bench: новые confirmation/task_access/correction/concurrency; AP02 model/permissions/files/setup, AP03 intake/concurrency, AP04 AI, AP05 jobs/concurrency, AP06 matching/concurrency; work_schedule, FiBu package/questions/checklist/workflow, overview. Общие guards/File требуют затронутые Mandant Documents/Drive security/download/Object Storage, disk/MemoryS3.

Node новые review + intake/jobs/matching/Task; AP01 validator и autonomous tests Linux; Ruff/JSON/CSV/diffcheck/frontend; patch installer; wheel schemas withoutdocs; fresh install+2 migrations с сохранением Settings/секрета/ролей/ordinaryTasks/Letters/terminalRuns/provenance; fake безSuite. Реальный Redis aftercommit с перехваченной внешней почтой; общие workers/Redis/scheduler не менять.

Browser DE/RU: fake→apply→edit/reject→assign/date→confirm→Task/ToDo/calendar; noactions/unplanned; новый Run retain; correction completedTask→disposition→operator replacement; revoke/neutralnotification/copy/dirty.

Критерий: атомарный идемпотентный confirm, актуальные права на Tasks и исправление без утечки истории. Live Mistral NOT RUN. Основной сайт, реальные документы, полный CI image build, AP-08, commit/PR/deployment вне этапа.

## Результаты исполнения

Завершено 24.10.2026 на одноразовых сайтах `posteingang-ap07.localhost`
(без Suite) и `posteingang-ap07-fresh.localhost` (сначала fresh install без Suite,
затем Suite для storage-регрессии). Основной сайт не изменялся.

Реализованы строгий полный review, явное применение/замена Run, ручные действия,
уточнение, retained_result, атомарное подтверждение и защищённые Tasks/ToDo.
Исправление создаёт самостоятельный приватный File, оставляет исходник в карантине
и требует административных решений по прежним задачам. Framework patch содержит
узкие hooks назначения, доставки и запрета copy; точный predecessor v9 сохранён.

Профильные серверные проверки — PASS:

- confirmation — 17; task_access — 19; confirmation_concurrency — 6;
  review_history — 2; correction_transactions — 3; correction_s3 — 2.
- AP-02: model — 8, permissions — 11, files — 15, setup — 4.
  AP-03: intake — 20, intake_concurrency — 5.
  AP-04: AI — 20. AP-05: jobs — 26, jobs_concurrency — 4.
  AP-06: matching — 27, matching_concurrency — 3.
- Files/intake/intake_concurrency/AI дополнительно прошли с Suite без условных
  S3-пропусков; новые коррекции и rollback проверены отдельно с MemoryS3.
- Work Schedule — 10; FiBu Package — 18; Questions — 21; Checklist — 24;
  Workflow — 9; Workflow integration — 10; Overview — 13.
  Mandant Documents — группы 4 и 13; Drive security — 26; downloads — 18;
  Object Storage — 19.

Конкурентные проверки используют независимые процессы/соединения, включая старый
DB snapshot, два confirm, гонки confirm с queue/select/cancel и Task update с
карантином. Проверены SQL COMMIT failure и успешный COMMIT с потерянным ответом:
новые bytes очищаются только при доказанном rollback. Исходные bytes сохраняются.
Настоящий Redis/RQ after-commit путь назначения проверен на отдельной очереди с
одноразовым worker; внешняя почтовая доставка перехвачена. Общий Redis не очищался,
общие workers и scheduler не останавливались.

При расширенной регрессии исправлена несовместимость нового File guard с `Path`,
передаваемым Suite при обычном rename: SQL-фильтр получает строку. Исходный
Drive security тест воспроизвёл ошибку до правки и прошёл после неё; затронутые
Task access, disk/S3 correction и storage suites повторены.

Остальные проверки — PASS: AP-01 validator (16 PDF/18 страниц), автономный Linux
suite (84), Node review/intake/jobs/matching/copy/overview (30), patch installer
upgrade/repeat/rollback (23), Ruff изменённых Python, JSON и DE/RU CSV,
`git diff --check`, frontend build. Wheel собирается; runtime review schema и
валидатор доступны из установленного пакета в изолированном запуске без `docs/`.
Fresh install и реальный fake→confirm без Suite выполнены; две миграции сохранили
Customer.tax_id, письма, терминальный Run, Task provenance, Settings, зашифрованный
секрет и назначения ролей. Эталоны AP-01 и extraction/review schemas не изменены.

Браузерная проверка DE/RU — PASS: fake→apply→редактирование/отклонение→исполнитель
и срок→confirm; явные unassigned/unscheduled; подтверждение без действий; сохранение
прежнего draft после нового Run с отдельной причиной; сохранность несохранённых
правок и блокировка apply до save. В DE Manager создал исправление завершённого
письма с Completed Task и сохранил её как historical; обычный RU Operator
подтвердил замену. Старый PDF вернул 403. Календарь показал внутреннюю дату;
после отзыва Customer share событие исчезло, прямой Task URL отказал без содержимого.
Запрет Duplicate проверен в DE; нейтральность уведомлений дополнительно проверена
сервером до Redis-сериализации и при доставке. Программный browser-доступ был
отключён в Work settings; сценарии выполнены через обычный Chrome.

Live Mistral — **NOT RUN**. Реальная доставка email, реальные документы, полный
CI image build, основной сайт, PR и deployment не выполнялись. Качество Mistral
не оценивается fake-тестами. Следующий этап — AP-08.
