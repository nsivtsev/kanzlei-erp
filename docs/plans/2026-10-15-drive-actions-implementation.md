# BL-048 — действия Drive по ограничениям Mandant

Согласованный план от 15 октября 2026. Источник: утверждённый пользователем план в чате.

Цель: серверный пакетный hook `drive_entity_actions_batch` выдаёт `mandant_policy`
после проверки чтения; Vue пересекает штатные разрешения с политикой.
Запрещённые команды скрываются; контекст объясняет управление правами через ERP.
Корень нельзя переименовать, переместить или удалить; встроенные редакторы и
Drive Share отсутствуют во всём поддереве. Reader не получает команд изменения.
Обычный Drive использует прежние правила.

1. TDD: серверная матрица root/folder/file/link/trash, reader/writer/stranger/revoke,
   настоящий marker, повреждённая связь, batch и одиночные/списочные API/поиск.
2. Общие проверки UI: меню, selection, клавиатура, emitter, inline rename,
   создание/загрузка, sharing, корзина; блокировка до загрузки текущих прав.
   Обновление на focus/visible, защита от поздних ответов маршрута/сессии.
3. MoveDialog и drag-and-drop: только одна область Mandant, допустимые writable
   назначения, защита от self/descendant, память отдельно по user/scope, обновление
   UI только после ответа сервера; отказ сохраняет выбор и файлы.
4. Точный предыдущий Suite patch сохранить как v5, обновить cumulative patch,
   installer, clean/v1–v5/repeat/atomic rollback tests. Suite commit сохраняется.
5. Bench, Vitest, Node, deployment, реальные HTTP/Socket.IO, DE/RU desktop/mobile,
   Suite/Kanzlei builds и полный CI окончательного образа. Затем отчёт, README,
   BL-048 checkbox и профильные сценарии BL-050.

Не требуются поля/миграция данных. Backend и assets обновляются согласованно.
BL-049, полный BL-050 и production-выпуск отдельно. Существующие изменения
transactional email сохраняются.

## Выполнение

Код и упаковка выполнены: Suite/Vitest 154, Node 30, deployment 70, профильные
Bench и полный CI окончательного образа прошли. DE/RU desktop/mobile browser
приёмка завершена; writer загрузил DOCX/PDF/XML, reader открыл и скачал PDF с
проверкой SHA-256. BL-048 закрыт. Подробности и итоговый образ:
[отчёт приёмки](../reviews/2026-10-15-drive-actions-verification.md).
