# BL-046 — Drive в навигации ERP для всех сотрудников

**Статус:** реализация в `codex/drive-downloads` поверх BL-044–045 готова; браузерная приёмка частична. BL-046 не закрыт. [Результаты и оставшиеся проверки](../reviews/2026-10-14-drive-navigation-verification.md).

**Цель:** каждый System User при установленном Suite видит Files в Kanzlei, создаёт обычную папку без Mandant и возвращается в ERP явной ссылкой.

## Изменения

- ERP boot: Suite installed, non-Guest и сохранённый user_type System User; роль System Manager и Customer access не определяют видимость. Files после Timesheet, перед settings; очистка старых /drive перед добавлением предотвращает дубли и сохраняет остальные пункты.
- ERP → /drive сохраняет новую вкладку. Drive → Back to Kanzlei ведёт на /desk/kanzlei-overview в текущей вкладке из верхней группы Sidebar.
- Собственный профиль Suite добавляет user_type. Отдельный isStaff использует загруженный серверный профиль и скрывает возврат при Guest/Website/unknown/loading/error. Существующие Apps/admin checks не меняются.
- suite_setup.configure после обновления сбрасывает кэш профиля existing forget_logged_in_users. Новых БД-полей и разрешений нет.
- Точный текущий Suite patch сохраняется как v3; установщик поддерживает v3/v2/v1, repeat и атомарный откат без сброса посторонних изменений.

## Приёмка

Сначала красные тесты, затем реализация. ERP nav: ordinary/no-Customer staff, manager/Admin, no-Suite, Guest/Website, order/uniqueness/translations. Profile/Sidebar: saved type, loading/error, current-tab return. HTTP: реальные Desk boots, обычная Home-папка и прежние отказы доступа/Office/ZIP/form/security. Browser: ERP → Files → folder → Back; reload без дублей, manager и Customer button. Полный Kanzlei/Node, профильный Suite/Vitest, deployment tests, assets и image CI.

## Завершение

Отчёт приёмки от 14 октября и BL-046 checkbox после browser/CI. BL-047–049 и полный BL-038 остаются отдельно; BL-050 расширен только этим этапом. Backend/assets обновляются согласованно, кэш профиля/Desk очищается. Existing transactional-email сохраняется; commit/push/PR/production отдельными действиями.
