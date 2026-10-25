# AP-09 — единый экран очереди и проверки письма

> Реализовывать через `superpowers:executing-plans`: отрицательный тест → реализация → профильная проверка.

## Результат и границы

Основа: feature/ai-posteingang@32a6ae3; AP-01…08 завершены. Единая Desk Page
объединяет очередь, защищённое PDF-превью, подбор Mandant и редактор проверки.
Запись только через существующие сервисы AP-03…07. Новых production-зависимостей,
DocTypes и Custom Fields нет. Реальные ключи Mistral не требуются.

Согласовано: все незавершённые письма по умолчанию; единый рабочий экран с
последовательными панелями на узком экране; административное исправление там же;
явное сохранение без autosave/merge; после confirm виден итог, следующее письмо
выбирает сотрудник. AP-10, массовые mutations, live Mistral, реальные документы,
основной сайт, полный CI image build, commit/PR/deployment не входят.

## Серверные проекции

Новый posteingang_workspace.py: get_queue(filters,cursor,page_length=20),
get_letter_workspace(letter), get_workspace_status(letter),
download_original(letter,disposition=inline|attachment), последний GET/HEAD.
Все требуют active System User и явно назначенной роли Operator; для Letter —
текущий доступ. Manager без Operator не получает очередь. AP-08 history policy
без Operator не меняется. Строгие параметры, schema_version=1.0, серверный user
в JSON; клиент не задаёт actor/Run/File/Customer источника/URL/storage path.

Фильтры: незавершённые (Received/Queued/Processing/Needs Review/Needs Clarification/
Error), история (Completed/Cancelled), все или конкретный статус; загрузивший
все/я; поиск <=140 по Letter ID/PDF name/сохранённым sender/letter_type; oldest
по умолчанию или newest. Карантин виден только по AP-07. Страница 20, максимум50.
Cursor: immutable (received_at,name), direction, receipt snapshot и version/user/
filters fingerprint. Новые загрузки после refresh; права/статусы/quarantine
перепроверяются каждый запрос, это живая очередь, не замороженный экспорт.

Строка: Letter/PDF/status/received_at/возраст/uploader, received_date или unknown,
доступный selected/confirmed Customer, <=3 текущих кандидатов/ambiguity, безопасный
Run status/error, ближайшая явная предложенная дата с «не проверено», количество
остальных сроков/relative. Matching по текущим правам, deadline из валидного
успешного review source, без вычисления relative. Нет анализа != not_found !=
not_found deadline. Обрабатывать results только возвращаемой страницы, без общего
matching cache. Повреждённый Run помечает строку INVALID_RESULT, не всю очередь.

Выделить внутренние analysis/matching/review projection helpers с загруженным doc;
старые публичные ответы/guards сохранить. Агрегатор начинает одну свежую read
границу, очищает permission caches, собирает один DB snapshot без вложенных
HTTP boundaries/rollback/commit. Возвращает одну revision, current/applied/latest
successful Run, allowed_operations/capabilities и original metadata/Letter-only
URLs. Без raw file_url/OCR/Events/snapshots/lease. Status endpoint лёгкий, различает
Settings stop и потерю доступа. Новый PDF endpoint повторно проверяет Operator,
Letter, private File/binding и использует AP-02/08 helper GET/HEAD/Range/no-store/
nosniff/bounded read; без public/presigned URL. Read-only HTTP errors безопасны,
без traceback; не переносить decorator на mutations.

## Page и клиент

Новая Page page/posteingang: /desk/posteingang и /desk/posteingang/<letter>.
Page role Operator, sidebar по page ID + active staff, без автоназначения роли.
Отдельный ключ перевода: DE Posteingang, RU Входящие PDF; email labels не менять.
Нет автооткрытия первого письма. Широкий экран: очередь/PDF/editor; уже — очередь
сворачивается; mobile вкладки Очередь/PDF/Проверка. CSS variables, light/dark,
focus, labels, клавиатура без focus trap.

Функции: AP-03 mixed uploads/duplicates/recovery; analysis/retry/manual/cancel;
PDF и evidence #page только validated integer; proposal отдельно, явное apply/
replace; matching reasons/conflicts/manual selection+reason; document fields,
accept/reject/assignee/external/internal dates/omission reasons; manual action,
save/clarification/no-actions; preview/human checkbox/confirm; Completed readonly
с доступными Task links; Manager correction (reuse/new PDF) и task dispositions.
Task status меняется обычным ERP, без новой бизнес-логики. Сохранять старую форму.
Переиспользовать независимые методы клиентов, экспортировать transports/messages;
не создавать fake frm. Intake dialog — optional callbacks open/update, прежние
вызовы списка/формы совместимы.

Один review buffer только memory. Edits/Mandant/source сбрасывают human/retained.
Для Mandant/add-action/apply/manage-analysis сначала save/discard dirty review.
Переход к другому Letter: Save and continue / Discard / Stay; ошибка отменяет
переход. При внешней SPA navigation очистить DOM/PDF/таймеры, buffer можно оставить
в Page memory; возврат — reauthorize. beforeunload предупреждает о dirty, без
обещания recovery после закрытия. Framework router/monkeypatch не менять.

Visible Page: queue15s/status5s, один in-flight каждого типа, без накопления timers;
hidden pauses, return first authorizes. Dirty editor polling не перерисовывает.
Новая revision сохраняет buffer, блокирует confirm, предлагает сравнить server/local;
нет auto-merge или замены expected_revision старого buffer. Requests привязаны к
user/Letter/sequence; late success/error игнорируется. Потеря роли/Letter read или
смена user очищает panels/PDF/buffers/intents. Скрытые Customer/Task labels удаляются.

Mutation double-click guard, frozen key/payload intent. Unknown network outcome
блокирует другие mutations до explicit replay того же запроса. Никаких новых keys/
payload автоматически. Storage только существующий AP03 site/user key+date, без
PDF/review/reasons/identifiers/OCR. Все значения text, AI HTML/links не исполняются.

## Порядок и приёмка

1. RED→GREEN read APIs/guards/queue/cursor/snapshot/PDF.
2. RED→GREEN Page/navigation/routes/layout/disabled/denied/empty.
3. RED→GREEN controller/buffer/sequences/polling/conflicts/intents.
4. RED→GREEN intake→analysis/manual→matching→review→confirm→Tasks.
5. RED→GREEN correction/quarantine/dispositions.
6. DE/RU/accessibility/build/regression/contracts/roadmap.

Новые suites test_posteingang_workspace и tests/posteingang.test.cjs. Сценарии:
all states/filter/sort/cursor/new receipt; Operator/readonly/no role/Guest/Website/
disabled/direct URL/revoke; hidden Customer/Task; consistent concurrent snapshot;
bad Run/no analysis/unreadable/relative; manual no provider/0/N actions/omissions;
dirty/poll/new Run/Mandant/2 tabs/late success+error/user change/navigation;
lost confirm/select/correction + frozen replay/no duplicates; mixed upload/recovery;
correction с Completed old Task/new operator; no writes/AI/enqueue on reads;
PDF binding/bytes/HEAD/Range/disk/MemoryS3/safe HTTP error; XSS text.

На disposable Bench: workspace/navigation; AP02 permissions/files, AP05 jobs,
AP06 matching, AP07 confirmation/review history, AP08 documents (shared projections/
clients/PDF). Node intake/jobs/matching/review/source/navigation. AP01 validator и
автономные contracts/validation; Ruff/JSON/CSV/diff/frontend build. Page/sidebar sync
и повторная миграция сохраняют Settings/secret/roles/Letters/Runs/Tasks/defaults.
Portal/Drive/storage/concurrency расширять только при изменении shared code.

Браузер DE/RU ~1440/~390px, light/dark: upload synthetic→fake→apply→Mandant→edit→
confirm→Task/source/Dokumente; отдельно manual/mixed batch/2-tab conflict/revoke/
correction. Клавиатура выбора/полей/save/confirm. Общие workers/Redis/scheduler не
останавливать/очищать. Live Mistral NOT RUN.

## Результаты исполнения

AP-09 завершён 25.10.2026 на синтетических данных и одноразовых сайтах.

- Workspace:17 PASS с Suite/MemoryS3; без Suite17 OK (1 S3 SKIP);
  independent-process snapshot:1 PASS.
- Профильная серверная регрессия permissions11/files15/jobs26/matching27/
  confirmation17/review history2/documents23/navigation23 — PASS.
- Node:49 PASS (включая17 новых workspace tests); AP01 validator16 PDF/18 pages
  и Linux autonomous85 — PASS; Ruff/JSON/CSV/diff/frontend build — PASS.
- Две повторные миграции: Page/Settings/encrypted secret/roles/Letters/terminal
  Runs/Tasks сохранены, роли и переключатели не назначались/не включались.
- Реальный HTTP: coherent workspace/queue, exact PDF, HEAD/Range/attachment/
  no-store, safe invalid parameters и Guest403 без traceback — PASS.
- Review нашёл stale popup callbacks, dirty Back/Forward, same-revision reference
  revocation и поздний post-mutation redirect. Исправления имеют RED→GREEN tests;
  повторный review не нашёл оставшихся P1/P2 в этих изменениях.

DE/light browser flow на synthetic AP01-10 прошёл загрузку, fake Run, apply,
Mandant с причиной, изменение action, assignee, разные external20.11/internal18.11,
save/human checkbox/confirm и Task source. Создана ровно одна TASK-2026-00165.
Доступный дубль отдельно вернул существующее подтверждённое письмо. Fake Run
выполнил штатный worker; ручная попытка исполнения уже terminal Run безопасно
вернула STALE_RUN. Общие workers не останавливались, Redis не очищался.

После разблокировки Mac браузерная приёмка продолжена без повторной реализации:

- DE/light: ручное письмо при отключённом внешнем провайдере подтверждено с явным
  «без действий»; 0 Run и 0 Tasks. Провайдер и настройки пользователя восстановлены.
- Две настоящие вкладки: локальная правка сохранена при новой серверной revision;
  запись/подтверждение заблокированы, сравнение показывает обе версии. Только явный
  discard загрузил серверную версию; автоматического merge нет.
- Смешанная пачка: повреждённый PDF получил понятную ошибку, растровый PDF принят
  отдельным Received без ложного none_found; ошибка не отменила успешную загрузку.
- RU/dark на 390 и 1440 px: очередь, защищённый PDF, ручная проверка и сохранение
  уточнения; DE/light на 1440 и 390 px: основной процесс и read-only результат.
  Проверены переключение панелей, перенос длинных подписей, focus/Tab и ввод полей.
  Добавлены перевод Sort, доступное имя мобильной кнопки загрузки и форматирование
  времени без микросекунд. Терминальный Run больше не показывает старое время retry
  (отдельный RED→GREEN regression test); retry отображается только для Queued.
- Manager создал исправление 8193c040bd → 54197aadcb с повторным использованием PDF;
  замена имеет самостоятельный File, тот же hash и не запускает анализ. Completed
  TASK-2026-00165 оставлена historical с причиной без изменения статуса. После разбора
  обычный оператор подтвердил замену без новых Tasks/Run. Исходник недоступен ему
  из-за quarantine. Замена и ручное письмо появились в Customer Dokumente, исходник
  отсутствует.
- Отзыв Customer read во время открытой Completed-замены: polling очистил PDF,
  editor и старые Customer/Task labels, показал нейтральный отказ. Исходная share
  восстановлена после проверки. Тестовые пользователи и данные остаются синтетическими.

Живой Mistral — NOT RUN. Реальная потеря HTTP-ответа и смена пользователя покрыты
controller/service tests; сетевой сбой браузера отдельно не имитировался. UI проверен
нативным Chrome с эмуляцией viewport, не на физическом мобильном устройстве.
Основной сайт, реальные документы, AP10, full CI image build, commit/PR/deployment
не затронуты. Общие workers/Redis/scheduler не останавливались и не очищались.
