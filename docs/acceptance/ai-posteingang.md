# AI Posteingang — приёмка AP-10

**25.10.2026: AP-10A завершён. AP-10B NOT RUN; весь AP-10 остаётся открытым.**
**Обновление 27.10.2026:** технический live smoke OCR/extraction PASS после
development-настройки prompt; качество/holdout/время/billing ещё не подтверждены.
**Последний результат AP-10B:** live holdout v4 выполнен, автоматический verdict
**NO_GO**. Человеческую проверку и замер времени владелец отложил; весь AP-10 открыт.
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

## AP-10B: первый live smoke — 27.10.2026

**Smoke выполнен, итог FAIL на извлечении. Holdout и пилот NOT RUN.**
Владелец разрешил бюджет до 5 € и максимум два запроса без повторов на одном
development PDF AP01-01. Использован внутренний адаптер AP-04 на отмеченном
синтетическом сайте; это отдельная проверка адаптера, не новый quality collector
и не переиспользование прежнего fake Run как live-результата. Run/Letter/Task/ToDo
не создавались и не изменялись. Граница DB-чтения обновлялась перед каждым шагом.

| Шаг | Фактический результат |
|---|---|
| Предварительная проверка | Первый вспомогательный запуск остановился до HTTP из-за неверного вызова `Document.update` в локальном harness. Исправлен на передачу dict; платных запросов в нём 0 |
| OCR | HTTP 200, ответ `mistral-ocr-4-1`, одна полная страница; структурная валидация OCR PASS, около 1.79 с |
| Извлечение | HTTP 200, ответ `mistral-small-2603`; адаптер вернул `INVALID_RESULT`, около 5.77 с. Невалидный результат не принят |
| Usage извлечения | 532 prompt / 853 completion / 1385 total tokens; не заменяет billing export |
| Побочные эффекты | Ровно 2 HTTP-запроса, без повторов. Валидный OCR сохранён отдельно; бизнес-записи не изменились. Enabled/external/auto/retry настройки восстановлены; внешняя обработка выключена, ключ сохранён |

Локальные артефакты: `artifacts/posteingang-evaluation/ap10-live-smoke-20261027/`:
`report.json`, `preflight-report.json`, `ocr.json`; секретов/HTTP headers в них нет.
Начальный harness сохранял только безопасную ошибку и usage, поэтому точное поле,
вызвавшее `INVALID_RESULT`, по первому ответу установить нельзя. Не делать вывод,
что причина уже доказана как ошибка модели или адаптера. Для следующей диагностики
подготовить один отдельный extraction-запрос с сохранённым OCR и безопасной
структурной диагностикой; превышение первоначального лимита двух запросов требует
отдельного разрешения владельца. Повторный OCR не нужен.

Конкретные модели/endpoints сверены с официальной документацией 27.10.2026:
[OCR 4.1](https://docs.mistral.ai/models/ocr-4-1),
[Small 4](https://docs.mistral.ai/models/mistral-small-4-0-26-03),
[OCR API](https://docs.mistral.ai/api/endpoint/ocr),
[Chat API](https://docs.mistral.ai/api/endpoint/chat).
Расчёт по опубликованным тарифам для записанного usage — примерно $0.0046;
это оценка, **фактические расходы NOT MEASURED** до billing export.

### Разрешённая диагностика и prompt v3

Владелец расширил количество запросов в пределах прежнего общего бюджета 5 €.
С сохранённым валидным OCR выполнены ещё три extraction-вызова, без повторного OCR
и без автоматических retries. Итого **5 HTTP-запросов: 1 OCR + 4 extraction**.

| Prompt | Результат |
|---|---|
| v1, диагностика | HTTP200, `INVALID_RESULT`: `recipients[0].identifiers[0].authority=null` при `type=other`. Контракт требует authority и other_type; сервер правильно отказал |
| v2 | Schema PASS. Инструкция запрещает выдумывать контекст и требует предупреждение для неполного номера. Однако модель опустила номер и выдала неверные предупреждения о плохом скане/неоднозначном Customer из-за synthetic-маркировки |
| v3, текущий | Schema PASS, readable/actions_found, одно действие, явная дата 20.11.2026, 12/12 цитат matched. Уточнено сохранение неполных номеров в incomplete_text и отсутствие негативного вывода из synthetic-маркировки |

**Содержательное ограничение v3 остаётся:** модель всё ещё опустила SYN-0007 из
предупреждения и вернула необоснованный `ambiguous_customer` о синтетическом
документе. Schema/evidence PASS не выдаётся за semantic quality PASS. Нельзя
переносить идеальные показатели fake на модель или объявлять готовность пилота
по одному development письму. Точечную настройку на этом примере остановили;
holdout не использовался для tuning и не запускался.

Схемы AP-01 и gold не менялись. Runtime-validator не исправляет ответ молча.
Версия prompt теперь `ap04-extraction-v3`; оценщик принимает известные v1/v2/v3
и сохраняет их отдельную provenance. Существующий guard AP-05 останавливает Run
с другой версией prompt до обращения к провайдеру; новый запуск только явно.

Артефакты дополнены `extra-extraction*/`, raw синтетическим JSON (без headers/ключа),
результатами проверки и `summary.json`. Usage всех extraction-вызовов:
2510 prompt / 3044 completion tokens. Оценка по опубликованным тарифам —
**$0.0062029** за весь smoke; фактический billing по-прежнему NOT MEASURED.
Все вызовы восстановили выключенные feature/external/auto; ключ сохранён,
Letter/Run/Action/Event/Task/ToDo/File counts неизменны.

Профильные проверки: автономные166 PASS; AI20 OK/1 ожидаемый skip S3 без Suite;
jobs27 PASS, включая смену prompt без скрытого запуска. Негативные тесты версии
воспроизведены до правки; Ruff0.14.0 и `git diff --check` PASS. Старый системный
Ruff оказался экспериментальным и повредил форматируемые файлы; только эти пять
файлов восстановлены из HEAD, текущие изменения применены заново и перепроверены.
UI/storage/permission-код не менялся; browser/build/Drive suites не повторялись.
Коммит, PR и deployment этой диагностической правки не выполнялись.

## AP-10B: live development и holdout — 27.10.2026

Владелец поручил сейчас оценить модель, а независимую человеческую часть выполнить
позже. Общий разрешённый бюджет остаётся 5 €. Сбор выполнен штатными intake/Run/RQ
на двух новых отмеченных сайтах без Suite. Основной сайт и общие workers/Redis/
scheduler не менялись; собственные очереди удалены после сбора.

**Development:** первый полноценный v3-прогон — 4/12 Succeeded, 8/12 INVALID_RESULT.
Восьми development-письмам отдельно повторили только extraction с сохранённым OCR;
повторилась причина `other` без authority. Provider projection теряла условие
if/then, хотя серверный raw validator сохранял его. После RED добавлены две
структурные anyOf-ветви: обычные типы с прежними nullable-полями и other с
обязательными authority/other_type. Canonical/raw схемы AP-01 не изменены.
С prompt/projection v4 повторное extraction на 12 development OCR прошло 12/12.
Это отдельный tuning-артефакт, не переписанные v3 Runs и не holdout-результаты.

**Holdout:** конфигурация заморожена до первого запроса, после начала не менялась;
все 30 писем обработаны один раз, без retries. Порог допуска не достигнут.

| Проверка по текущей frozen-разметке | Результат |
|---|---|
| Технически принятые extraction | 28/30; AP01-32 и AP01-33 отклонены INVALID_RESULT, не повторялись и не исключались |
| Top-1 Customer | 24/26 = 92.3%, FAIL при ≥95%; ошибки AP01-32/AP01-33 |
| Top-3 Customer | 26/28 = 92.9%, FAIL при ≥95%; те же ошибки |
| Неоднозначность / несколько адресатов | 1/3, FAIL; AP01-04/AP01-37 не дали multiple_matches |
| Проверка помеченных matched цитат | 380/380 PASS относительно реального OCR. Ещё 5 цитат явно unverified; это не semantic evidence coverage |
| Полнота / точность действий | NOT MEASURED без смысловых judgments. Даже оптимистический верхний предел полноты по количествам — 32/34 = 94.1%, ниже 95% |
| Явные даты | Все 25 gold-дат присутствуют в соответствующих письмах как мультимножества; это верхний предел, не подтверждение связи даты с правильным действием |
| Relative dates / evidence coverage | NOT MEASURED; rejected raw и смысловые соответствия не подменяются нулём нарушений |
| Независимые gold/judgments, активное время, billing | Отложены / NOT MEASURED по решению владельца |

Новые 26 holdout показаны отдельно: 24/26 технически успешны, Top-1 21/23,
Top-3 22/24, ambiguity 1/2, matched quotes 329/329. Ошибки остаются в знаменателях.
Положительные смысловые соответствия агентом не выставлялись; все judgments pending.
CLI построил отчёт с exit0/NO_GO; `--require-gates` вернул exit1.

**Ограничение gold:** AP01-04/AP01-37 ожидают точное совпадение пользовательского
идентификатора с контекстом `synthetic_client_reference / Fiktive Beispielstelle /
SYNTHETIC`. Орган `Fiktive Beispielstelle` отсутствует в OCR этих PDF. Поэтому
ожидание exact matching требует отдельной проверки разметки/контекста, а не
передачи каталога в prompt или угадывания органа. Gold задним числом не меняли.
В development модель также принимала отправителя за орган номера и делала ложные
предупреждения из synthetic-маркировки: schema PASS не доказывает верность фактов.

| Provenance holdout | Значение |
|---|---|
| Сайт / actor | `posteingang-ap10-live-holdout.localhost` / `ap10-live-holdout-20261027@example.invalid` |
| Run оценки / freeze UTC | `c15801959d88e30e4aef9b6a` / `2026-10-27T12:48:31.436798+00:00` |
| Модели / prompt | `mistral-ocr-4-1`, `mistral-small-2603` / `ap04-extraction-v4` |
| Code revision | `5f8748443a77+local.e679ada19b8bd6f2` (изменения v4 ещё не закоммичены) |
| Dataset / gold hashes | те же `f3fd42…7825` / `0b495b…40d` из AP-10A; dataset1.1 неизменён |
| Observations SHA-256 | `3c90593f8379cc1895e20d9f73c5dd2d5dc9dc053a67be8e40ce2256514c2d81` |

Игнорируемые артефакты: `artifacts/posteingang-evaluation/ap10b-20261027/`.
Есть отдельные freezes/execution metadata, первичные development observations,
development diagnostics v3/v4, holdout observations, pending judgments и отчёт.
`holdout/automatic-diagnostics.json` содержит только количественные верхние пределы
и очередь проверки; это не автоматическая смысловая разметка.

Сборщики ограничены 24 и 60 запросами, retries=0. Дополнительно измерены 25 HTTP-
вызовов adapter-диагностики вместе с предыдущим smoke; их usage даёт около $0.0191
по опубликованным тарифам. Верхняя тарифная оценка для двух collector-прогонов по
лимиту request192000 bytes / max_tokens32768 и 46 страницам — $2.2194; суммарный
консервативный расчёт около $2.24. Это **не фактический billing** и не число запросов,
выведенное из attempt_count. Реальный счёт/валюта/налоги требуют usage/billing export.

Все три использованных сайта проверены: enabled/external/auto=0, ключи сохранены,
Completed писем и Tasks нет. На новых сайтах 12 и 30 Letter/Run; созданные оригиналы
остаются приватными. Реальные документы и deployment не использовались.
Регрессия: Linux168 PASS, AI20 OK/skip1, targeted jobs prompt guard1 PASS,
Ruff0.14.0/diff PASS. UI/storage/permissions не менялись; соответствующие старые
suites повторно не запускались. Коммит/PR/deployment этой работы не выполнялись.

**Дальше:** исправления готовить на development-примерах. Этот live holdout уже
просмотрен и не должен после tuning выдаваться за новую независимую оценку.
До повторного допуска нужны проверенная новая/frozen-разметка, корректный протокол
новой оценки, отложенная человеческая часть и решение владельца. AP-10B не закрыт.

## Исправления после NO_GO — 27.10.2026

Отдельная диагностика AP01-32/AP01-33 на сохранённом OCR воспроизвела нарушение:
`explicit_date` с `date=null` и `wording=null`. При v4 один повтор прошёл, второй
снова отказал; это не основание автоматически повторять INVALID_RESULT.
Provider projection v5 переводит все существующие enum-условия v1 в структурные
anyOf-альтернативы: identifier context, четыре вида срока, action_assessment и
unreadable. Canonical/raw/review схемы и серверная строгость не менялись.
RED: 14 недопустимых сочетаний принимались проекцией; GREEN: они отвергаются,
все42 исходных эталона принимаются. Оба проблемных письма с v5 дали корректный
not_found. Исторические Runs/observations/NO_GO не переписаны.

Добавлен отдельный development-набор `posteingang_remediation.json`: 10 новых
документов с наблюдаемым контекстом, двумя случаями неоднозначности, неполным
номером, sender ID и всеми видами срока. Набор не является holdout. Новый флаг
`validate_posteingang_fixtures.py --audit-identifier-context` проверяет наличие
терминов authority/jurisdiction в source text; старый gold дал ожидаемый FAIL:
82 отсутствующих значения в39 письмах. Это необходимая проверка наблюдаемости,
а не доказательство отношения «выдано этим органом». Исходный dataset1.1 не менялся.

| Development-проверка | Результат |
|---|---|
| Small v5, исходные12 OCR | schema12/12; выдуманный контекст/ложные warnings ещё встречаются |
| Small v5, новые10 PDF | schema10/10; проверки количества/видов дат, кандидатов и предупреждений4/10 |
| Small v5, контроль на том же OCR | те же параметры4096/none, schema10/10, проверки4/10 |
| Medium3.5 v5, тот же OCR | schema10/10, проверки10/10; оба multiple_matches и неполный номер корректны |
| Medium3.5 v5, исходные12 OCR | schema12/12; для неполных custom references орган не выдуман |

Сравнение Small/Medium использовало одинаковые OCR/prompt/schema,
max_tokens4096 и reasoning_effort=none, без tools/retries/fallback. Проверки
структурных полей не выдаются за смысловую полноту/точность действий. Конкретный
кандидат следующей оценки — `mistral-medium-3-5`; он выбран только на одноразовом
`posteingang-ap10-final.localhost`, enabled/external/auto остаются0. Основной сайт
и defaults приложения не менялись. У модели есть structured outputs; тарифы
проверены по [официальной странице Medium3.5](https://docs.mistral.ai/models/mistral-medium-3-5-26-04).

Артефакты диагностики, freezes и сравнения — в прежнем игнорируемом каталоге;
сводка `remediation-summary.json`. Дополнительно68 прямых запросов: 10 OCR и58
extraction, без автоматических повторов. Тарифная оценка этой работы около$0.213;
вместе с прежней консервативной оценкой около$2.45, фактический billing NOT MEASURED.
Ключи сохранены; новые10 оригиналов приватны, Run/Action/Task для них не создавались.

Проверки: Linux173 PASS; AI20 OK/skip1(no Suite S3); новая DB-проверка matching1
PASS; jobs prompt guard1 PASS; Ruff и diff PASS. UI, permissions, storage и
зависимости не менялись, их старые suites повторно не запускались.

**Статус:** технический отказ исправлен, более подходящая модель найдена на
разработочных примерах. Новый frozen holdout с Medium ещё NOT RUN; старый NO_GO
сохраняется. Следующий независимый прогон требует новой версии наблюдаемого gold
и новых source families; эти10 примеров нельзя переименовать в holdout. Человеческая
часть остаётся отложенной. AP-10 и допуск реальных документов не закрыты.

## Новый holdout Medium — 27.10.2026

Dataset1.2 хранится отдельно в `posteingang_v2`: 42 письма /44страницы,
12 development (включая10 прежних regression cases) и30 новых holdout source
families /32страницы. Все26 явных сроков включены в denominator, включая плохой
растровый скан. Source/gold/context audit PASS; все44страницы отрендерены и
просмотрены до freeze. Старые dataset1.1/gold/NO_GO и их hashes сохранены.
Gold нового набора — pending_independent_review, не человеческая независимая разметка.

До первого запроса исправлен обнаруженный review дефект fingerprint: hash v2
теперь связывает именно профиль v2 (RED→GREEN). В адаптере явно установлен
reasoning_effort=none; текст prompt v5 сохранён, версия исполнения v6 отражает
изменение HTTP-параметра. Canonical/raw/review схемы v1.0 неизменны.

Freeze UTC `2026-10-27T20:00:09.320296+00:00`, run `8a2404f71c8493accf2ed5a7`;
сайт `posteingang-ap10-medium-v2.localhost`, без Suite, scheduler выключен.
Через штатные intake/Run/RQ выполнены60 HTTP200 (32 OCR-страницы +30 extraction):
`mistral-ocr-4-1` → `mistral-medium-3-5`, prompt/execution v6, max_tokens32768,
retries0/fallback0. В ходе измерения код/данные/конфигурация не менялись и запросы не повторялись;
после завершения выключатели восстановлены.

| Автоматически измерено | Результат |
|---|---|
| OCR + schema/extraction | 30/30 Succeeded, attempt_count1 |
| Customer top-1 / top-3 | 27/27 /28/28, PASS |
| Неоднозначность / несколько адресатов | 2/2, PASS |
| Отображаемые matched цитаты | 334/334, PASS; ещё3 явно unverified |
| Количество действий | 32 предложено /32 gold; это верхний предел полноты, не смысловое сопоставление |
| Значения явных дат по письмам | 26/26 присутствуют; связь даты с правильным действием NOT MEASURED |
| Медианное время коллектора на письмо | 8.77с; не активное время сотрудника |

Два расхождения с frozen gold сохранены: AP01-64 выбрал document_date18.10 из
технического футера вместо Ausfertigung27.10; AP01-84 классифицировал «möglichst
zeitnah» как not_found вместо uncertain. Kind/date-мультимножества совпали в29/30
письмах. Три unverified: AP01-55 summary, AP01-63 summary, AP01-68 action evidence.
Это не исправлялось по результатам holdout.

Итог оценщика **INCOMPLETE**: измеренные автоматические пороги PASS, но семантические
соответствия, gold review, evidence coverage, relative-date semantics, активное
время, фактический billing и полный runtime-safety пакет остаются NOT MEASURED.
Все30 judgments pending, без LLM-as-judge. Обычный CLI exit0, --require-gates exit1.

Budget observer резервировал максимум перед каждым вызовом (при неизвестном
исходе стоимость не освобождается), дополнительный предел$1 не достигнут.
Usage: 32040 prompt /26353 completion tokens +32 OCR-страницы; тарифная оценка
$0.3737075, суммарная с прежней консервативной оценкой около$2.83. Это не billing;
разрешённый владельцем общий бюджет5 € не увеличивался.

Доказательства: игнорируемый `medium-v2-holdout/` в прежнем каталоге artifacts;
freeze/journal/raw/HTTP usage/observations/pending judgments/report/diagnostics.
Dataset hash `749928…7fffb`, gold `27c587…25f1`, code `8d2529…b1901`, observations
file SHA256 `d54f0c…109a`; после завершения все freeze hashes проверены неизменными.
30 приватных оригиналов,30 Needs Review,30 Succeeded,0 Completed/Task; feature/
external/auto выключены, ключ сохранён, собственная RQ-очередь удалена.

Проверки до/после подготовки: Linux190 PASS; после финальных изменений targeted
budget5 /evaluation51 PASS; AI20 OK/skip1, jobs prompt guard1 PASS; Ruff/diff/JSON
PASS. UI/storage/permissions не менялись. AP-10 остаётся открыт: новый процедурный
holdout измерен, человеческая часть отложена; допуск реальных документов и релиз
не выполнялись. Этот holdout уже просмотрен и не становится новым независимым
после дальнейшего tuning. Коммит/PR/deployment не выполнялись.

## Решение о выпуске кода — 27.10.2026

Владелец поручил сделать коммит и выпустить текущую реализацию. Это решение о
выпуске кода; AP-10 сохраняет INCOMPLETE и отложенные человеческие метрики.
Выключенные defaults enabled/external/auto не меняются, тестовый API key и данные
не переносятся в production. Выпуск проходит штатные PR/main CI и digest-based
deployment; после него проверяются здоровье приложения и поведение выключенной
функции. Полный AI-сценарий остаётся проверенным на синтетическом одноразовом сайте.

Release preflight: production Dockerfile теперь устанавливает объявленную
jsonschema-зависимость и проверяет её версию. Новый AP-10 suite допускает только
явно помеченные локальные test sites, включая ci.localhost при allow_tests=true;
quality collector по-прежнему отвергает ci.localhost и production. Git отмечает
PDF как binary, их frozen bytes/hashes не меняются. Deployment unit tests125,
collector guards35, полный AP-10 suite8 на новом локальном ci.localhost,
pip check/Ruff/bash syntax/diff — PASS. Тестовые переключатели снова выключены;
полный image build/upgrade/backup-restore выполняет GitHub CI.
