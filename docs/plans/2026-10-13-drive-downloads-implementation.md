# BL-044 — скачивание загруженных файлов и полный ZIP

Исполнять с `superpowers:executing-plans`, TDD и проверкой результатов перед завершением.

**Цель:** все загруженные файлы скачиваются и входят в ZIP с исходными байтами; Office открывается на странице файла с кнопкой скачивания.

**Архитектура:** сервер классифицирует сохранённое содержимое File независимо от отображаемого file_type, отдаёт can_download и формирует проверяемый manifest ZIP. Клиент использует эти признаки и существующие permission-checked API. Изменения Suite сохраняются воспроизводимым патчем.

**Статус:** реализовано и проверено 13 октября 2026; [отчёт приёмки](../reviews/2026-10-13-drive-downloads-verification.md). BL-045–049, экспорт встроенных документов и production-деплой не входят в этап.

## Сервер

- Общий классификатор: content_doctype/content_docname, is_folder, file_url; Writer/Sheets/Slides не считаются blob даже при наличии пути.
- Прямое скачивание и ZIP используют классификатор и сохраняют storage/Customer/token/ERP checks BL-042–043.
- can_download вычисляется до скрытия storage URL; mime_type доступен локальным preview. Загруженные презентации не раскрывают путь.
- ZIP включает все доступные uploaded bytes рекурсивно, сохраняет папки, устраняет повторы и коллизии имён. Ошибка чтения даёт failed, частичный ZIP не выдаётся.
- download_folder/download_status добавляют skipped_counts={hosted_content,links}; недоступные элементы не раскрываются, повторная выдача перепроверяет права.
- Manifest/cache-key версионируются; старые архивы требуют повторной сборки и истекают штатно.

## Клиент

- Оба openEntity, copy-link и new-tab выбирают редакторы по hosted content; загруженные Office идут на File page.
- Download в списке/navbar использует can_download, папки создают ZIP; смешанное выделение показывает предупреждение о пропусках.
- Microsoft Office preview отключён: локальная карточка с именем/типом/размером/download. Office-token не создаётся; внешних запросов нет.
- PDF/изображения/текст/CSV сохраняют локальный preview.

## Проверки

Сначала красные тесты. Реальные минимальные DOCX/PPTX/XLSX/PDF/XML: SHA-256 source/direct/ZIP; обычная папка, Mandant, ERP attachment, nested/duplicate/overlapping selections. Hosted/link omissions, revoked access before/after build, old manifest, missing bytes, aliases, S3 mocks. Vue routing/actions/preview/warnings и браузерное скачивание. Полный Kanzlei/Suite regression, frontend build и image CI.

## Патчи и выпуск

Текущий Suite security-патч сохранён как v2. Установщик поддерживает exact v1/v2 → current и атомарный rollback обоих приложений без сброса чужих изменений. Новых DB fields и миграции File нет. Backup, согласованное обновление backend/assets, migrate/build/cache/restart. Закрыть BL-044 после приёмки и записать фактические результаты в отчёт.
