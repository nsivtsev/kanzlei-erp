# AP-08 — документы манданта и источник задачи

> Реализация через `superpowers:executing-plans`: отрицательный тест → реализация → профильная проверка.

## Результат и границы

Основа: `feature/ai-posteingang@9c9c939`, AP-01…07 завершены. Подтверждённое письмо
появляется в Dokumente Customer; Task показывает оригинальный PDF, подтверждённый
внешний срок, основание и evidence. Проекции динамические, без новых сущностей,
production-зависимостей, копий PDF, переноса в Drive и дублирования сроков в Task.

В Dokumente только Completed без карантина, включая ручные письма без Run/Tasks.
Используется confirmed_customer, не selected_customer. Чтение требует актуальных
прав Customer/Letter/File; Operator не обязателен. История исправлений остаётся
административной. Settings.enabled не закрывает чтение подтверждённых данных.

## Dokumente

- Добавить source=posteingang. Объединять по File ID, не имени/hash. Основной
  контекст Letter; дополнительные — только доступные Tasks с проверенной Action→Task.
- Скрытые Tasks не влияют на поиск/количество контекстов. source=task работает
  через Task-контекст; искусственную связь FiBu Package не создавать.
- Известная received_date → date_kind=received («Получено»), иначе received_at →
  uploaded («Загружено»). Дата документа не подменяет дату получения. Календарная
  дата без timezone/time; для сортировки начало дня, tie-breaker File ID.
- Старые email/file даты неизменны. Сохранить newest/oldest, фильтры/лимиты.
  confirmed_at<=snapshot исключает новые подтверждения между страницами.
  Версионировать fingerprint курсора; права/карантин проверять каждый запрос.
- Поиск: имя File, Letter ID, подтверждённые sender/letter_type и доступные
  Task titles. Без индексации OCR, полного текста и истории.

## Источник Task и PDF

Новый posteingang_documents: get_task_source(task), download_task_source(task,
disposition=inline|attachment), последний GET/HEAD. Строгие параметры; клиент
не выбирает Customer/Letter/Action/File/URL. Обычная читаемая Task: available=false;
закрытая — нейтральный отказ. Проверить active System User, базовые Task/FiBu/AP07
права, provenance/key, confirmed Customer, accepted Action и обратную ссылку,
актуальные Letter/File права и private binding.

Проекция schema_version=1.0: available, Letter name/status/quarantine/document_date/
received_date_known/received_date/received_at, Action UUID/external_date/basis/evidence,
File ID/name/page_count, серверные preview/download URLs только по Task. Не отдавать
другие действия, полный review/OCR/extraction/snapshots/history. Evidence пересчитать
общим AP04/07 helper по applied Run с hard caps. Без OCR нет matched; повреждённые
данные → безопасный INVALID_RESULT без AI.

Task UI: источник, Letter, PDF/download, внешний срок/основание, evidence/page links.
Без даты — явное «Внешний срок не подтверждён»; AI/internal даты не подменяют её.
Late response/route guard, очистка при отказе, без reload несохранённых Task полей,
всё как text, без browser storage, DE/RU. Карантинная Task доступна только по AP07
административной политике и явно помечена; в обычный Dokumente не включается.

Переиспользовать PDF response AP02 (GET/HEAD/Range/open_file), добавить внутренний
disposition с прежним inline default. Dokumente использует тот же helper для
Posteingang. Без public/presigned URL. private,no-store/nosniff/лимит/закрытие потоков.
ZIP: прежние 100 файлов/100 MiB, уникальные File IDs/безопасные имена; повторная
авторизация/guards каждого потока, отказ целиком при любой ошибке, без частичного ZIP
или утечки имён/Customer/storage path/traceback.

## Порядок и приёмка

1. Отрицательные tests отбора, скрытых данных, объединения/контекстов → реализация.
2. Даты, фильтры, snapshot/курсор → реализация.
3. Task provenance/projection/evidence → реализация.
4. PDF HEAD/Range/download/ZIP disk/MemoryS3 → реализация.
5. Dokumente/Task UI, DE/RU, late responses → реализация.
6. Регрессия, contracts/roadmap с фактическими результатами.

Новый серверный test_posteingang_documents: 0/1/N Tasks/manual/noRun; все закрытые
статусы/quarantine; selected!=confirmed Customer; одинаковые имена/разные File;
скрытые Task titles; даты/старые источники/оба порядка/границы; подтверждение между
страницами/revoke/correction; external!=internal/null/relative; tampering provenance/
Action/File/параметров; read без Operator, Guest/Website/disabled/DocShare; отзыв между
списком и bytes; correction old→Received→Completed replacement; точные bytes/HEAD/
Range/ZIP dedup/полный отказ; отсутствие записей/AI/enqueue/изменения Task/Letter.

На одноразовых сайтах: новый suite, Mandant Documents, AP07 task_access/confirmation,
AP02 files/permissions, review validation, Drive security/download, Object Storage,
FiBu Checklist; без Suite и disk/MemoryS3 с Suite. Node Dokumente/Task source и
затронутые Task tests, Ruff/JSON/CSV/diffcheck/frontend build, AP01 validator/contracts.
Расширять остальное по фактическим общим изменениям; миграции/Custom Fields не нужны.

Браузер DE/RU: Customer→Dokumente→Posteingang→PDF→Task→external/evidence→page→download/
ZIP; noTasks/unknown received/revoke/correction/unsaved Task fields.

Live Mistral NOT RUN. Реальные документы, основной сайт, глобальный scheduler,
полный CI image build, AP09, commit/PR/deployment не входят в поручение.

## Результаты исполнения

Завершено 25.10.2026. Добавлены `posteingang_documents.py`, Task source bundle и
серверный/Node suites; расширены Dokumente, общий PDF disposition, review evidence
helper, Task refresh и DE/RU. Contracts и roadmap обновлены. Схемы и AP-01 эталоны
не менялись; новых миграций, Custom Fields и зависимостей нет.

Профильная приёмка на одноразовых `posteingang-ap07.localhost` (без Suite) и
`posteingang-ap07-fresh.localhost` (с Suite):

| Проверка | Результат |
|---|---|
| Новый documents suite | 23 OK без Suite (2 S3 SKIP); 23 PASS с Suite/MemoryS3 |
| Mandant Documents | 4 helper + 13 integration PASS |
| AP-07 Task access / confirmation / review history | 19 / 17 / 2 PASS |
| AP-02 files / permissions | 15 / 11 PASS |
| Drive security / downloads / Object Storage | 26 / 18 / 19 PASS |
| FiBu Checklist | 24 PASS |
| Linux autonomous contracts/validation/PDF | 85 PASS |
| AP-01 validator | 16 PDF, 18 страниц PASS |
| Node Dokumente / Task source / Task copy | 8 PASS |
| Frontend build / Ruff / JSON / DE-RU CSV / diff check | PASS |
| Реальный HTTP | Exact PDF bytes, HEAD, Range, attachment, no-store; безопасная ошибка без traceback PASS |

Регрессия общих graph/download/storage путей необходима из-за нового источника и
общего PDF helper; confirmation/review проверены из-за общего evidence helper.
После финального изменения HTTP-границы повторены новый documents suite в обоих
режимах, Mandant Documents, Drive downloads и FiBu Checklist. Уже успешные проверки
неизменённых зависимостей повторно не запускались.

Отрицательные tests воспроизводили недопустимые статусы/даты/связи, скрытые контексты,
повреждённые evidence, отсутствие защищённого source, ZIP storage failure и отсутствие
нового UI. При review дополнительно исправлены устаревший failed UI response, который
мог очистить более новый источник, и принятие OCR из неуспешного applied Run. Оба
дефекта покрыты RED→GREEN. Реальный HTTP выявил unconditional traceback в framework
при development mode: узкая граница read-only endpoints теперь возвращает только
безопасный code/статическое сообщение. Отдельный отрицательный HTTP-тест и реальный
запрос подтвердили исправление. Внутренние вызовы сохраняют типизированные исключения.

Без Suite обнаружено старое безусловное чтение несуществующего `File.status` в
общем metadata SELECT: неиспользуемое поле удалено; Suite Drive status filtering
сохранён, существующие Dokumente regression PASS.

Браузер на синтетических данных DE/RU: Customer → Dokumente → Posteingang → PDF →
Task → внешний срок/evidence → исходная страница; PDF download и ZIP подтверждены
историей загрузок Chrome. Дата получения показана без времени, неизвестная дата
помечена uploaded; письмо без Task видно. Внешний срок 20.11 отличался от внутреннего
18.11. Refresh source сохраняет несохранённый subject; отзыв Customer read очищает
источник и показывает нейтральный отказ. Карантинный исходник исключён, существующая
Completed замена видна. Полный новый correction flow проверен серверным suite;
повторно создавать исправление в браузере AP-08 не потребовалось.

Живой Mistral — NOT RUN. Основной сайт, реальные документы, глобальный scheduler,
полный CI image build, AP-09, commit/PR/deployment не затрагивались. Миграционная
приёмка не требуется: схемы данных не менялись. Изменения оставлены в текущей ветке.
