# BL-046 — Drive в навигации сотрудников

Дата: 14 октября 2026. Ветка `codex/drive-downloads`, поверх BL-044–045.

## Статус

Реализация и браузерная приёмка завершены. Обычный сотрудник без Customer access и System Manager открыли Drive из ERP в новой вкладке и вернулись через Zur Kanzlei в текущей вкладке. Обычный сотрудник создал личную Home-папку. Пункт остался единственным после перезагрузки Desk. Кнопка Drive из карточки Mandant открыла управляемую папку в новой вкладке. Полный CI окончательного образа прошёл с exit 0.

## Изменения

Kanzlei добавляет ровно один пункт Dateien / Files / Файлы при установленном Suite для non-Guest с сохранённым `User.user_type = System User`. System Manager и доступ к Customer не определяют видимость. Старые ссылки `/drive` удаляются перед добавлением; Files остаётся после Zeiterfassung, перед Einstellungen, когда эти пункты доступны пользователю. Остальные элементы, административный guard и пользовательские настройки сохраняются.

Drive показывает Zur Kanzlei / Back to Kanzlei / В Kanzlei в верхней группе Sidebar. Действие вызывает `window.location.assign('/desk/kanzlei-overview')` в текущей вкладке. ERP Files и кнопка из Mandant сохраняют открытие Drive в отдельной вкладке.

Существующий ответ `suite.api.account.get_logged_in_user` дополнен сохранённым `user_type`. Отдельный `useCurrentUser.isStaff` требует завершённого серверного запроса (`fetched`), активной сессии и совпадения `profile.name` с текущим пользователем. Пункт скрыт при loading/error, Guest, Website User, неизвестном типе и старом локально кэшированном профиле другого пользователя. Глобальные Apps/System Manager проверки не менялись. `suite_setup.configure`, вызываемый при migrate, использует существующий `forget_logged_in_users`: прежние профили без нового поля не задерживают появление ссылки.

Ревью выявило гонку переводов: router ожидал Drive bootstrap, но bootstrap не ожидал загрузку словаря; Sidebar мог сохранить английскую подпись. Теперь bootstrap ожидает словарь до монтирования Sidebar. Ошибка перевода сохраняет доступность Drive с исходными подписями.

Финальная проверка pinned Frappe UI выявила восстановление `User` из локального кэша с `fetched=false`: такой профиль не является подтверждением текущей сессии. Четыре дополнительные красные регрессии воспроизвели cached/unfetched, null/Guest session и профиль другого пользователя; после усиления `isStaff` они прошли.

Схема БД, роли и права доступа к содержимому не менялись. BL-047–049 и полная перестройка BL-038 остаются отдельными этапами.

## Проверки

До реализации воспроизведены отказы навигации обычного сотрудника/System Manager, отсутствие `user_type`/`isStaff` и кнопки Sidebar, отсутствие очистки профиля. Для переводов сначала получены красные DE/RU-тесты готовности и тест отказа сервиса; после исправления они прошли.

| Проверка | Результат |
| --- | --- |
| `kanzlei_erp.tests.test_navigation` | 22 теста прошли: staff/no-Customer, manager/Admin, Guest/Website/no-Suite, порядок, повторная обработка, переводы и существующие проверки меню. |
| `kanzlei_erp.tests.test_suite_setup` | 4 теста прошли, включая реальное удаление прежних профилей из Redis. |
| `suite.api.test_account` | 1 интеграционный и 12 модульных тестов прошли; профиль сообщает сохранённый тип независимо от ролей, Guest не получает профиль. |
| Suite/Vitest | 110 тестов в 16 файлах прошли: весь профильный Drive и session, включая регрессии BL-044, новый Sidebar, готовность переводов и подтверждение текущей сессии. |
| Root Node | Все 30 тестов прошли, включая BL-045 и realtime. |
| Deployment | Все 40 тестов прошли: clean/repeat, v1/v2/v3, конфликты, атомарный откат двух приложений и сохранение посторонних tracked/untracked изменений. |
| Реальные патчи | На чистом архиве закреплённого Suite прошли clean/v1/v2/v3 → итоговый patch и reverse-check. |
| HTTP / Socket.IO | Сценарий прошёл под четырьмя сотрудниками, отдельными manager/portal: настоящий Desk boot, уникальность/порядок Files, запрет Desk Guest/portal, Home-папка без Customer. Все проверки BL-044–045, параллельное создание, доступ/отзыв, Office SHA-256/ZIP, realtime, alias и WebDAV сохранены. |
| Assets | Локальная production-сборка Suite, Kanzlei assets и сборка итогового образа прошли. Для Suite использован также собственный `yarn build`: один `bench build` оставлял прежние SPA-assets в локальном checkout. |
| Статика / ревью | Python F/B, Node/Bash syntax, root/Suite diff checks прошли. Независимое ревью итогового кода не выявило новых замечаний. |

## Браузер и границы приёмки

На окончательной локальной сборке `development.localhost:8100` пройдены три профиля. Обычный System User без Customer access видел один Dateien, открыл Drive в новой вкладке, создал папку `BL046 browser check` в Home и нажал Zur Kanzlei: активная вкладка вернулась на `/desk/kanzlei-overview`. После перезагрузки Desk ссылка оставалась единственной. System Manager прошёл тот же переход в Drive и обратно. Administrator отдельно открыл Drive через ERP и вернулся по той же ссылке.

Из карточки доступного Mandant кнопка `Drive-Ordner anlegen` открыла созданную управляемую папку в новой вкладке. Возврат из этой вкладки снова открыл Kanzlei в её текущей вкладке. Сценарии выполнены на искусственных учетных записях, Customer и папках; локальные пользовательские вкладки не использовались для этого прохода.

После браузерной проверки выполнен штатный cleanup: временные пользователи, Customer, управляемая Mandant-папка и Home-папка удалены сценарием приёмки; очищен Desk cache. Локальные workers/scheduler возобновлены. Credentials-файл удалён.

## Патчи и выпуск

`deploy/suite-mandant-access-v3.patch` — точная копия предшественника из `ad1f339`, SHA-256 `5eda2093e0e3e5579a7e7b7257c1ec8b0ea80f61f4cbe1ef9a7aafeea9a4a065`. Текущий cumulative patch содержит BL-046; установщик и Docker context принимают v3 наряду с v1/v2. Frappe-патч этим этапом не изменялся.

Образ `kanzlei-navigation:local`, `linux/arm64`, ID `sha256:d5164c5e9cddb8d0fe6be706981be9ebb23f9d79fe7b3a5a89fa5914eb9de15a`. Suite закреплён на `2db13737524b271e32cae80a64a85b5ff112e55f`, ERPNext base — `v16.36.1`. `PLATFORM=linux/arm64 bash scripts/ci-image.sh kanzlei-navigation:local` завершился с exit 0. В образе прошли 216 интеграционных и 21 модульный тест Kanzlei; подтверждены 237 XML-результатов. Также прошли HTTP/Socket.IO с шестью профилями и регрессиями BL-044–045, 30 Node-тестов, fresh/repeat install/migrate, оба legacy-verify, PDF upload/download и реальный backup/restore базы и публичных/приватных файлов. Одноразовый CI-стек удалён штатно.

Production-выпуск выполняется отдельно: backup, согласованное обновление backend/Suite assets, migrate, build обоих приложений, clear-cache и перезапуск backend/worker/websocket. Migrate очищает кэш профиля. Новые поля и миграция пользовательских данных не требуются. Коммит, push и PR не выполнялись; локальные transactional-email изменения сохранены.
