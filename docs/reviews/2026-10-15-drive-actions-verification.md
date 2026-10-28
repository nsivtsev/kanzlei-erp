# BL-048 — действия Drive по правам Mandant

Дата: 15 октября 2026. Ветка `codex/drive-actions-bl048`, поверх BL-047.

## Статус

**BL-048 реализован и принят.** Автоматические проверки, полный CI итогового
образа и ручная DE/RU browser-приёмка writer/reader на desktop/mobile прошли.

## Реализация

Kanzlei выдаёт `mandant_policy` через пакетный hook `drive_entity_actions_batch`.
Suite добавляет политику после проверки чтения в detail, списки и поиск.
Источник — сохранённый File marker, проверенная связь Customer с корнем и
текущие права Customer. File/Customer загружаются пакетно; права вычисляются
один раз на Customer в пределах вызова. Обычные сущности не получают политику.
Повреждённая управляемая связь запрещает изменения и не раскрывает корень.

| Сущность | Writer | Reader |
| --- | --- | --- |
| Корень | Загрузка, папка, HTTP(S)-ссылка; без rename/move/trash/restore/delete | Просмотр и скачивание |
| Подпапка | Rename, move внутри Mandant, trash; загрузка/создание | Просмотр и скачивание |
| Файл/ссылка | Rename, move внутри Mandant, trash | Просмотр и скачивание |
| Корзина, кроме корня | Restore/delete согласно состоянию и серверным проверкам | Без изменений |
| Всё управляемое поддерево | Без Drive Share и Writer/Sheets/Slides | Без Drive Share и Writer/Sheets/Slides |

`write` корня сохранён для загрузки. Прежние проверки скачивания, избранного и
копирования ссылки действуют отдельно. Серверные запреты обязательны для API.

Общий клиентский модуль пересекает штатные права Suite с политикой. Проверки
подключены к меню, selection, горячим клавишам/emitter, inline rename/крошкам,
созданию, загрузке/повтору, paste, sharing, корзине и drag-and-drop. Массовая
операция проверяет каждый элемент. При неизвестном контексте загрузка и создание
закрыты. DE/RU пояснение ведёт в Customer ERP в новой вкладке; меню создания
объясняет поддержку папок и HTTP(S)-ссылок.

MoveDialog ограничивает управляемые источники одним Mandant, исключает смешанные
выборки, self/descendant и неподходящие назначения. Обычный режим сохраняет
Home/Site и навигацию через читаемые родительские папки. Память назначения
разделена по пользователю и области, повторно проверяется. Перед перемещением
источники и назначение перечитываются; отказ сохраняет список и выбор.
Drag payload разрешается только через проверенные текущие строки.

Смена маршрута/сессии инвалидирует старые grants и продолжения запросов;
focus/visibility обновляют detail и список. Upload проверяет права после выбора
файла и заново при retry. Смена сессии и отмена не позволяют отложенному retry
поставить файл в очередь. Поздний ответ не обновляет прежний маршрут.

## Автоматическая проверка

| Проверка | Результат |
| --- | --- |
| TDD | Красные тесты воспроизвели отсутствие политики, разрешённые обходные UI-действия, потерю restore/delete из-за ранней фильтрации, устаревшие назначения/ответы, отказ upload после возврата OS picker и retry после смены прав/отмены. После исправлений зелёные. |
| Bench | `test_mandant_drive`: первоначальные 40 тестов + отдельная новая регрессия HTTP-link rename/move; итоговые 41 включены в полный CI. `test_drive_storage_security`: 26, `test_drive_downloads`: 18, `test_drive_realtime`: 3 — прошли. |
| Политика | Reader/writer/stranger/revoke, Suite Admin и System Manager без Customer access, portal/Guest, root/folder/file/link/trash, подставные поля, повреждённая связь, batch, detail/list/search/favourites/recents. |
| Suite | `yarn test src/apps/drive src/boot/session.test.ts`: **154 теста в 25 файлах прошли**. Общие gates, Navbar/Trash, MoveDialog/controller, поколения route/session, upload/retry/cancel и регрессии предыдущих этапов. |
| Репозиторий | `node --test tests/*.test.cjs`: **30 прошли**. `python3 -m unittest discover -s tests/deploy -v`: **70 прошли**. |
| Патч | Чистый архив закреплённого Suite: clean/v1/v2/v3/v4/v5 → current, reverse-check/reverse — прошли. Installer: repeat, unrelated edits, атомарный rollback обоих патчей при конфликте. Новые Suite-файлы включены в cumulative patch. |
| HTTP/Socket.IO | Четыре staff-сессии и manager/portal: 12 групп прошли. Новые root/reader/child/link policies, обычный Home, same-Mandant rename/move, прямые запреты root/native/share/cross-boundary и отзыв доступа. Сохранены Office/ZIP SHA-256, tokens, aliases, стандартный File API, WebDAV и realtime. |
| Assets | Suite `yarn build`, Kanzlei assets/переводы/clear-cache и production image build прошли. |
| Ревью/статика | Независимое ревью выявило и подтвердило исправления обходных handlers, async upload/retry и MoveDialog. Python/Node/Bash syntax и `git diff --check` прошли. |

HTTP-проверка выявила реальную ошибку переименования ссылки: Suite пытался
переместить URL через storage manager. Rename теперь пропускает физическую
операцию для сущностей вне диска; регрессия проверяет сохранение URL и отсутствие
storage move. Тестовые HTTP move-запросы используют JSON, как браузер; ZIP
ожидает пропуск новой link-фикстуры. Итоговый локальный HTTP-прогон прошёл.

## Браузер

Chrome Incognito, `development.localhost:8100`, искусственные записи; DE writer
и RU reader, desktop и Responsive 400 × 842.

- DE writer: у корня нет rename/move/trash/share и встроенных редакторов. Доступны
  Folder/Link/Upload; пояснение ERP читаемо на desktop/mobile.
- Созданы подпапка и HTTPS-ссылка; подпапка переименована через inline rename.
  XML перемещён внутри Mandant через MoveDialog без Home/Site. Загружен DOCX
  через системный picker, показан успешный результат. В повторной браузерной
  сессии PDF и XML выбраны через системный picker; Drive подтвердил `2 files
  uploaded`, обе записи появились в Mandant. Все пять форматов
  DOCX/PPTX/XLSX/PDF/XML отдельно проверены настоящими HTTP upload/download/ZIP
  с совпадением SHA-256.
- Обычный Home сохранил создание Document/Presentation/Spreadsheet и штатные
  Folder/Link/Upload; заголовок Home не получил меню изменения корня.
- RU reader: отсутствуют Create и команды изменения в корне/подпапке и меню
  DOCX/PDF; видны просмотр, скачивание, ссылка и избранное. Русский интерфейс
  читаем на мобильной ширине. PDF открылся во встроенном просмотрщике на одну
  страницу с содержимым `Drive invoice PDF`. Reader скачал PDF через системное
  меню в `/tmp`; SHA-256 совпал с оригиналом:
  `9f47c4b953c76299352e9ee4195f357f26d82a3e765e9f704090a7c7ccd9d89e`.

Во время параллельной локальной HTTP-приёмки первый browser move получил
MariaDB 1020. Интерфейс показал ошибку и сохранил источник; повтор после
завершения HTTP успешно переместил файл. После исправления uploader повтор
через OS picker подтвердил загрузку. Искусственные browser-фикстуры и файл
временных credentials удалены штатным cleanup; временные upload/download-файлы
удалены.

## Упаковка и итоговый образ

Suite остаётся закреплён на `2db13737524b271e32cae80a64a85b5ff112e55f`.
Точный предыдущий патч сохранён как `deploy/suite-mandant-access-v5.patch`, SHA-256
`4a26b79378e0819f217a6991273316811ed55a277b63feabe7050f9b341c3b21`.
Current patch SHA-256:
`152d3d6144e2745f4eafc41088852f755f224c15dba03a21e70f1a9afd63b086`.
Frappe patch не изменялся. DB schema и миграция пользовательских данных не нужны.

Итоговый образ `kanzlei-drive-actions:final`, `linux/arm64`, ID
`sha256:497107de8ff41debda6cfc299def8c7eec2d9a7fe8e6f675f816b47efcbdd2e9`.
Полный `PLATFORM=linux/arm64 bash scripts/ci-image.sh kanzlei-drive-actions:final`
завершился с **exit 0**: application — 378.067 с, upgrade — 113.892 с.
Прошли 230 интеграционных + 21 модульный тест (251 XML-результат), HTTP/Socket.IO,
30 Node-тестов, fresh/repeat install/migrate, backup/restore базы и обоих файловых
архивов, повторная проверка после восстановления, legacy upgrade и прерванная
установка Suite. Одноразовые CI-стеки удалены штатно.

BL-049, интеграция встроенных редакторов, полный BL-050 и production-выпуск
остаются отдельными этапами. Согласованное обновление backend/assets выполняется
существующим процессом backup/migrate/build/clear-cache/restart.
Существующие transactional-email изменения сохранены.
Изменения оставлены локально в указанной ветке, без commit/push/PR.
