# AP-03 — приём PDF, идемпотентность и точные дубли: Implementation Plan

> Для реализации использовать `superpowers:executing-plans`.

**Цель:** приватный приём одного или нескольких PDF с отдельным результатом по файлу, восстановлением потерянного ответа и защитой от конкурентных дублей.

**Архитектура:** multipart endpoint → ограниченная структурная проверка PDF в отдельном процессе → конструктор AP-02 → атомарная квитанция. Отдельные advisory locks сериализуют запрос и hash в пределах сайта. AI, Run, подтверждение и Tasks не запускаются.

**Основа:** HEAD `1f6bd6b` и незакоммиченная реализация AP-02. Существующая работа сохраняется. Коммит, PR и deployment — только по отдельному поручению.

## Согласованное поведение

- Доступный точный дубль возвращает существующее письмо без новых File/Letter/Event и без изменения даты получения. Недоступный дубль даёт нейтральный отказ без ссылки, имени или Customer.
- Дата получения задаётся отдельно для каждого файла; общая дата применяется явно. По умолчанию дата неизвестна.
- Успешный новый приём создаёт Received/revision=1/generation=0. Валидный растровый скан принимается без утверждения о читаемости или отсутствии действий.
- Перезапуск браузера восстанавливает результаты по scoped request keys. Байты и имена файлов в localStorage не сохраняются.

## Последовательность реализации

1. Добавить отрицательные тесты параметров и PDF; реализовать отдельный parser с pypdf 6.15.0 из закреплённого Frappe. Максимум 20 MiB/50 страниц, уменьшенные Settings учитываются. Процесс ограничен 10 s wall/5 s CPU/512 MiB AS; без Frappe, OCR, БД или сетевых вызовов.
2. Добавить закрытый DocType Incoming Letter Intake Receipt: hash scoped key в name, actor, payload_fingerprint, letter, outcome, result_json, created_at. Закрыть CRUD, списки, export и DocShare. Узкий required_administrator_permission hook запрещает только квитанции до Administrator shortcut framework; поведение остальных DocTypes AP-02 сохраняется.
3. Реализовать POST receive/get_intake_result и get_intake_options. Receive принимает один multipart file, request_key, received_date_known и received_date; служебный cmd допускается только с именем самой операции. Сервер определяет автора, source, hash и число страниц.
4. Проверять replay до новых ограничений функции; новый ключ с доступным hash создаёт duplicate receipt без парсинга PDF. Для нового файла parser выполняется без advisory/row locks, затем проверки повторяются под locks. Квитанция и Letter/File/Event атомарны; hash не уникален.
5. Добавить общую загрузочную логику и кнопки в списке/форме Incoming Letter. Файлы отправляются последовательно, ошибка одного не отменяет остальные. Ключ и параметры фиксируются до отправки. Результаты не сохраняются в браузере; открытие ссылки повторно проверяет квитанцию.
6. Проверить реальные независимые DB/HTTP-сессии, откат/commit, disk/MemoryS3, повтор миграции, клиентские тесты и DE/RU браузерный сценарий. Обновить roadmap по фактическим результатам.

## Технические решения

- Fingerprint: SHA-256 canonical JSON с версией 1.0, source, hash байтов, известностью/датой получения; имя файла не входит.
- MariaDB locks: scoped request key, затем hash, с site namespace и ожиданием до 5 секунд. Успешная операция удерживает их до commit/rollback. Первый проход не использует FOR UPDATE; перед авторитетными HTTP-проверками сбрасывается read-only snapshot и локальные permission caches.
- HTTP receive сам завершает транзакцию внутри обработчика ошибок. Сбой до commit очищает новые локальные/S3 bytes. Если исход commit неизвестен из-за недоступной БД, bytes не удаляются вслепую: S3 intent остаётся для существующего reconciliation. Успешный SQL commit с ошибкой after_commit callback распознаётся по durable receipt.
- Внешний сбор S3 intents откладывает очистку до положительного подтверждения rollback и отсутствия квитанции; вложенные isolated_writes передают intents родителю.
- Private File возвращается с исходными байтами; нет presigned/public URL. Структурная проверка PDF не является OCR или проверкой рендеринга каждой страницы.
- Proxy production уже имеет CLIENT_MAX_BODY_SIZE=100m; изменять его не требуется. Локальный HTTP boundary проверяется реальной отправкой 20 MiB.

## Приёмка

Тесты: новый intake suite, independent-process concurrency suite, standalone PDF tests, Node client suite, четыре AP-02 suite, затронутые Drive security/Mandant Drive/Object Storage suites и patch installer tests. Расширение регрессий необходимо из-за изменения обязательного Administrator permission guard и вложенной очистки S3.

Проверить: одинаковые/разные request keys и payload; два оператора; старый DB snapshot; неизвестный результат; отзыв доступа/роли; выключенную функцию; уменьшенные лимиты; невалидный/зашифрованный PDF; 50/51 страниц; ровно 20 MiB; отказ квитанции и commit; неопределённый COMMIT; повтор миграции с сохранением данных. DE/RU UI: загрузка, дубль, смешанная пачка, отдельные/общая дата, восстановление.

Полный CI image build, рабочий сайт, Mistral, AP-04, PR и deployment не выполняются.

## Фактические результаты

Подтверждено на одноразовых сайтах (рабочий сайт не менялся):

- Intake disk/MemoryS3: 20 runtime tests PASS; итоговый прогон после строгого запрета пустой даты также PASS. Дубль не меняет исходную дату и не запускает parser после уменьшения лимита страниц.
- Независимые процессы/DB-сессии: одинаковый key/payload, разные bytes, два оператора с одним hash, старый snapshot и сбой before_commit — PASS. Отдельный реальный COMMIT с потерянным подтверждением и недоступным rollback сохраняет доступный S3-оригинал — PASS.
- Без Suite: 10 PASS, 10 ожидаемых skip; миграция нового DocType — PASS.
- Standalone PDF: 4 PASS; Node client: 3 PASS.
- Реальный HTTP: create/replay/duplicate/recovery, конфликт ключа, запрет лишних полей/нескольких файлов/GET, два оператора, приватность квитанций, ровно 20 MiB и превышение на 1 byte, шифрование — PASS.
- DE/RU браузер: загрузка, дубль, ошибка в середине пачки и продолжение, применение общей даты, неизвестная дата, сохранённая форма и её кнопка, восстановление незавершённого запроса после reload, разделение pending по пользователю — PASS. Потеря успешного HTTP-ответа проверена сочетанием Node recovery test и серверной квитанции; искусственный обрыв сети в браузере не выполнялся.
- Asset build, Ruff, DocType JSON, новые строки CSV DE/RU и diff — PASS; patch installer — 21 PASS.
- Независимое статическое ревью после исправлений: оставшихся конкретных P1/P2 нет.

Итоговая приёмка — **AP-03 завершён 20.10.2026**:

| Проверка | Результат |
|---|---|
| AP-02 model / permissions / files / setup | 11 / 11 / 15 / 4 PASS, всего 41 |
| AP-03 intake / independent-process concurrency и commit | 20 / 5 PASS, всего 25 |
| Drive security / Mandant Drive / Object Storage | 26 / 41 / 19 PASS, всего 86 |
| PDF / Node client / patch installer | 4 / 3 / 21 PASS |
| Без Suite: intake и закрытие Admin/DocShare | 10 PASS, 10 ожидаемых skip; после узкого Admin hook отдельный receipt case PASS |
| Установка Suite на второй одноразовый сайт | PASS |
| Две последовательные миграции с контрольным снимком | PASS: Customer/Task/File/Letter/Action/Run/Event/Receipt, Settings и Operator roles сохранены |
| HTTP, граница 20 MiB, DE/RU UI, asset build, lint/JSON/CSV/diff | PASS |

Runtime model/permissions и контроль миграций проверены на `posteingang-ap02.localhost`; оставшиеся suites — на `posteingang-ap02-fresh.localhost` после установки Suite. Длительный permission suite с накопленными синтетическими письмами также завершился PASS (11 tests); промежуточный прогон с более широким Admin guard не учитывается как успешная приёмка. Новое расширение framework ограничено server-only квитанциями.

Технические логи итоговых suites: `/tmp/ap03-final-test_*.log` внутри Bench container; строгая дата — `/tmp/ap03-final-intake-strict-date.log`, HTTP — `/tmp/ap03-http.log`. Прикладные данные и production-зависимости AP-01 не менялись. Использованы только синтетические документы и одноразовые сайты; S3-логика проверялась через MemoryS3; живой S3 не проверялся, Mistral не вызывался. Временные DE/RU пользователи после приёмки отключены. Полный CI image build, рабочий сайт, AP-04, commit, PR и deployment не запускались.
