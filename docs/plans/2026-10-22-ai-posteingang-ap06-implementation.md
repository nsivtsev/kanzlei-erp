# AP-06 — подбор Mandant и ручной выбор Customer

> Для реализации использовать `superpowers:executing-plans`.

## Результат и границы

Цель: объяснимые кандидаты только из доступных Customer и явный выбор сотрудника с
причиной. Основа: `feature/ai-posteingang@6fb3d9f`, чистое дерево до реализации.
Сервер заново сопоставляет факты текущего успешного Run с проверенными номерами;
поиск не пишет письмо. Выбор — атомарная mutation с revision/Event/idempotency.

Согласовано: минимальная форма сейчас, отдельное подтверждение сохранённого номера,
консервативное полное имя без fuzzy/транслитерации. Никаких подтверждений письма,
Tasks, применения extraction к черновику, исправлений Completed или AP-09.
Ключ Mistral не нужен, новые production dependencies не нужны. Не делать commit,
PR/deployment, AP-07, не менять основной сайт или глобальный scheduler.

## Справочник Customer

Новый child DocType Mandant Identifier, Customer.kanzlei_identifiers.
identifier_type (Select: de_steuernummer/de_tax_id/de_vat_id/other), raw_value
(Data140) обязательны. normalized_value (Data140) вычисляет сервер; jurisdiction,
authority, other_type (Data140) условны. verification_source (Small Text <=1000),
verified_by (Link User), verified_at (Datetime) серверные, до проверки null.
Неуникальный индекс identifier_type+normalized_value+jurisdiction.

Таблица наследует Customer rights. Проверка требует активного System User с
Customer read/write, не Operator. Изменение raw/type/context очищает verification.
Поддельные stamps/normalized value не принимаются, копирование их не переносит.
Защитить чужие child IDs, standalone writes и generic list/export/read. Customer
hooks дополняются; до появления поля установка остаётся безопасной.

NFKC+trim, сохранить ведущие нули. Steuernummer: удалить whitespace и '/', только
ASCII цифры; для verification/exact match нужны одинаковые jurisdiction+authority.
Tax ID: удалить whitespace, только ASCII цифры. VAT ID: whitespace убрать, uppercase,
только ASCII A-Z/0-9. Other: внутренние знаки/пробелы/регистр сохраняются; обязательны
other_type+authority, jurisdiction сравнивается включая null. Контекст только
NFKC+trim, без алиасов. Нет checksum/конверсии региональных форматов/догадки типа.
Customer.tax_id не переносить и не использовать для совпадений.

POST verify_identifier(customer, identifier, expected_modified, verification_source):
актуальные права, lock Customer, modified и принадлежность строки, формат/контекст,
серверные actor/time, обычный Customer.save с Version audit. Не принимать actor,
normalized/stamps. CUSTOMER_CHANGED при stale form. Потеря ответа — reread,
не автоматическая повторная verification.

## Matching и выбор

Основной модуль posteingang_matching, справочник mandant_identifiers.
Источник только current Succeeded Run с верными generation/letter/source snapshot.
OCR/extraction повторно валидировать AP-04 hard caps, evidence вычислять заново.
Нет Run — matching отсутствует, доступен ручной поиск. Corrupt Run — INVALID_RESULT,
без сети. matching_result_json не служит общим permission-sensitive кэшем.

Только recipients identifiers с holder_role=addressee, verified Customer row и
matched evidence, содержащий нормализованный номер. Sender/representative/unknown
и непривязанные other_identifiers не дают точного сигнала. При exact matches пул
состоит из них; иначе полное имя NFKC/casefold/whitespace. Address уточняет порядок
только по читаемому связанному structured Address (улица/индекс/город), не HTML.
Порядок: число различных exact IDs, имя, адрес, затем имя Customer и ID.

Максимум 3 кандидата. Ambiguity вычисляется до top-3: none/multiple_matches/
multiple_addressees/not_found. Одинаковые факты адресата с другим evidence deduplicate.
Показывать identifier/name conflicts. Недоступный Customer не влияет на ответ.
Несколько адресатов блокируют выбор одного Mandant, но поиск не меняет статус.
Неоднозначный номер/нет кандидатов допускают явный ручной выбор с причиной.

API get_matching_state(letter): matching/source Run/revision, доступный selection и
allowed operations. search_customers(letter,text,start=0,page_length=20): текущие
читаемые Customer, max page50, text140, pagination только по readable. Read-only
кандидат виден, выбрать нельзя. Чтение требует активного System User/Operator и
letter access, доступно при выключенной функции.

POST select_customer(letter,customer,reason,expected_revision,request_key): null
очищает выбор. enabled, Operator, letter access, read/write старого и нового Customer.
Только Received/Needs Review/Needs Clarification/Error; не active/terminal/quarantine.
Reason 1..1000 обязателен и для clear. Letter lock, повтор guards/revision, менять
только selected_customer/customer_reason, revision++, Event одной транзакцией.
Никаких status/generation/Run/actions/confirmed_customer/Customer writes.
Узкий AP-05 allowlist +2 поля, generic CRUD остаётся закрытым.
Event idempotency scope actor/letter/op/key, fingerprint customer+reason без revision.
Rights до replay, replay до revision conflict. Receipt без названия Customer,
актуальный selection читать отдельно. Неизвестные бизнес-параметры запрещены,
безопасные ошибки без SQL/контента/parser exceptions.

## Интерфейс и миграция

Customer: таблица, состояние проверки, отдельная verification сохранённой чистой
строки с обязательным источником; правка сразу показывает unchecked; read-only
пользователь без действия. Incoming Letter: панель candidates/reasons/conflicts/
evidence pages, явный выбор/ручной поиск/reason, selection/clear. No Run отличается
от not_found. Нет autoselect. Двойной клик блокировать; network retry сохраняет UUID.
Late reply не обновляет dirty/покинутую форму, новые запросы/denial очищают старое.
Text-only вывод, ничего чувствительного в browser storage. DE/RU, сохранить AP-05 UI.

Аддитивная миграция child/Custom Field без tax_id backfill/roles/enabling/jobs.
Существующие настройки и данные сохраняются. Обновить contracts и roadmap по факту,
не менять AP-01 extraction/review schemas и gold.

## Разработка и приёмка

Отрицательные тесты -> registry/guards -> verification/concurrency -> matching ->
selection/concurrency -> UI/translations -> migrations/regression.
Основной suite test_posteingang_matching, отдельные independent DB/process concurrency
и Node suites. Все 16 fixtures с синтетическим каталогом/permissions; сравнивать
кандидатов/ambiguity, не будущие Completed/Tasks. Покрыть все normalizers/contexts,
unverified/edited/copied/forged rows, sender/representative, unknown/multi/conflicting
IDs/name-only/>3, unverified quote, hidden Customer/no oracle, revocations/disabled/
Guest/Website/read-only/DocShare, revision/replay/conflict, verify/edit race/old snapshot.
Ни поиск, ни selection не создают Run/Action/Task и не применяют draft.

На одноразовом Bench: matching/concurrency; AP-02 model/permissions/files/setup,
AP-03 intake, AP-04 AI, AP-05 jobs/jobs concurrency. FiBu checklist, Customer Drive
синхронизация/защита mapping/timestamp conflicts — из-за общего Customer hook,
Custom Fields и записи Letter. Node новый+intake/jobs/Customer Drive. Linux AP-01
validator и contract/validation/PDF. Ruff/JSON/CSV/diff/frontend assets. Fresh install,
две миграции с контролем tax_id/rows/stamps/Letter/Run/Settings/secret/roles; matching
без Suite. Полные storage suites только при изменении общего storage code.

Browser DE/RU: save identifier -> verify -> fake Run candidate -> select with reason
-> reload; ambiguity/read-only/revocation/dirty edits. Live Mistral NOT RUN. В режиме
планирования файлы не создаются; этот файл сохраняется при исполнении плана.


## Результат исполнения

Реализованы справочник Mandant Identifier, отдельная проверка строки Customer с
Version audit, повторная валидация текущего Run, объяснимые кандидаты только из
доступных Customer и явный идемпотентный выбор/очистка с Event. Нет автоподтверждения,
Tasks, применения результата к действиям или запросов к AI. Новых production
зависимостей нет. Runtime схемы и gold AP-01 не изменены.

### Уточнения по результатам отрицательных тестов и браузера

- Frappe new query engine передавал обязательному hook только Customer вместо
  фактического child type. Добавлен совместимый query_doctype в существующий hook,
  child list/Customer join ограничиваются readable parents.
- DataExporter all_doctypes читал дочерние строки raw query. Новый узкий обязательный
  child export hook фильтрует только Mandant Identifier по текущим правам Customer;
  прочие таблицы не меняют поведение. Воспроизведён отказ для select-only Customer
  с System Manager export privileges; скрытые номера не попадают в CSV.
- Для обновления framework patch сохранён точный предыдущий v8 patch; fetch-suite
  умеет безопасный upgrade/repeat/rollback, Docker context включает predecessor.
- Browser RED: Frappe автоматически помещал новую Section Break под скрытую
  sales_team_tab. Обе новые поля явно включены в существующий Customer field_order
  до settings_tab; тест проверяет видимую tax_tab.
- Browser RED: быстрый поиск отвечал до появления модального окна и терял результат.
  Запрос начинается из on_page_show; Node test моделирует этот порядок событий.
- Network unknown сохраняет исходные request key/revision; окончательный отказ
  завершает намерение. Отдельный Node RED→GREEN защищает повтор после REVISION_CONFLICT.

### Среда и выполненная приёмка

Одноразовый сайт posteingang-ap06.localhost, Python 3.14, MariaDB, Redis и ERPNext.
Сначала установка/проверки без Suite, затем Suite только на этом же тестовом сайте.
Основной сайт, глобальный scheduler и общие workers не менялись. Для профильных
тестов изолирован только несвязанный realtime notification fan-out (_deliver);
SQL, текущие права, File, commit/rollback и конкурентные соединения настоящие.
Для массового создания синтетических пользователей в расширенных suites изолирован
штатный per-minute creation throttle; настройки реальных пользователей не менялись.
Пустой ERPNext-сайт потребовал стандартные UOM Nos и leaf Item Group. Первые ошибки
подготовки FiBu устранены в тестовой среде; прошедшие 23 сценария не повторялись,
единственный throttle-blocked сценарий повторно прошёл.

После накопления 192 committed synthetic Letter fixtures регрессионный File list
оказался медленным: существующий AP-02 guard повторно обходит источники для каждого
File. Диагностика stack/DB подтвердила работу этого обхода, без DB lock. Собственный
длительный permissions run прерван; оставшиеся suites запускаются с очисткой
отработанных Letter fixtures только одноразового сайта между модулями. Runtime
storage/permission код для ускорения не подменялся. Это функциональная приёмка,
не нагрузочная проверка большой истории; указанное существующее ограничение
производительности остаётся отдельной задачей.

Проверенные результаты на текущий момент:

- Matching: 25 основных тестов PASS с Suite и без Suite; все 16 fixtures сравнивают
  кандидатов, дополнительный прогон сравнил также ambiguity каждого случая.
- Concurrency: 3 PASS с независимыми process/DB connections, включая старый snapshot,
  один commit/replay, revision conflict и verification/edit race.
- Дополнительные проверки видимой tax_tab, structured Address ranking/read denial
  и явных identifier/name conflicts — PASS. Для Address тест готовит штатный
  Address Template, отсутствовавший на пустом сайте.
- Linux AP-01 validator: 16 писем, 18 страниц, 12 development/4 holdout — PASS;
  автономные contract/validation/PDF suites — 61 PASS.
- Node: matching 6, intake 3, jobs 5, Customer Drive 22 — всего 36 PASS
  с повторным профильным прогоном matching после UI fixes. Lifecycle test также
  проверяет очистку старой панели при отказе ручного поиска.
- Patch installer/upgrades/rollback — 22 PASS; полный framework patch применим
  к pinned Frappe HEAD (git apply --check). Полная сборка CI image не запускалась.
- Fresh install — PASS; две миграции без Suite и повторные две с Suite сохранили
  Customer.tax_id, строки/stamps, письма/Runs, Settings, encrypted secret и роли.
- AP-02 model/permissions/files/setup — 11/11/15/4 PASS; AP-03 intake — 20 PASS;
  AP-04 AI — 20 PASS; AP-05 jobs/jobs concurrency — 26/4 PASS с настоящим Redis/RQ.
- Frontend assets — PASS. Ruff, JSON, DE/RU CSV и diff проверены; окончательный
  FiBu checklist — 24 PASS (23 в полном прогоне + единственный focused retry после
  fixture throttle); Customer Drive — 41 PASS, Drive storage security — 26 PASS,
  Object Storage — 19 PASS. Matching включает 25 основных + 2 новых focused tests.
  Итого 251 различных серверных сценариев проверены в основных/профильных запусках.

Browser DE/RU — PASS: обычное сохранение номера, запрет проверки dirty формы,
отдельное подтверждение с источником/автором/временем и Version, fake Run candidate
с evidence pages, явный выбор с причиной, очистка и перечитывание выбора.
Неоднозначный номер показывает двух кандидатов и конфликт имени без автозамены;
ручной поиск показывает только доступные результаты. Отзыв доступа очищает прежние
label/reason/кнопки; read-only Customer не даёт verification/select. Сохранность
несохранённых правок и игнорирование поздних ответов дополнительно проверены Node.
Дополнительный RU browser repeat на новой Received записи подтвердил ручной поиск
без Run, read-only результаты, затем отзыв Operator: HTTP403 показывает безопасное
сообщение и очищает результаты диалога и прежнюю панель. Браузерные тестовые
пользователи и обработка после обеих проверок отключены.

Независимое ревью: конкретных оставшихся P1/P2 нет; исправления диалога и
очистки при отказе проверены отрицательным Node test, повторным браузером и ревью.
Живой Mistral — **NOT RUN**. Реальные документы, AP-07, commit/PR/deployment не входили
в поручение и не выполнялись.
