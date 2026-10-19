# AP-01 — Implementation Plan и результат

> **Для исполнителя:** использовать `superpowers:executing-plans` для пошаговой работы. Этот этап уже выполнен; дальнейшие этапы требуют собственного задания и плана.

**Goal:** подготовить проверяемые контракты и синтетический эталонный набор для AI Posteingang.

**Architecture:** оригинал, OCR, предложения AI, смысловой ground truth, кандидаты ERP и решения человека имеют разные форматы и источники. Автономные инструменты проверяют схемы, файлы и согласованность разметки; ERP endpoints и workers этим этапом не создаются.

**Tech Stack:** JSON Schema Draft 2020-12, Python >=3.10 для инструментов, jsonschema, pypdf, ReportLab, Pillow, Poppler. Production Frappe/ERPNext v16 и его зависимости не изменены.

## Выполненные задачи

- [x] Сверить ветку `feature/ai-posteingang`, чистое исходное дерево и интеграции Customer/Task/File/Dokumente.
- [x] Зафиксировать [контракт v1.0](ai-posteingang/contracts.md): модель хранения/индексы, лимиты, права, ручную проверку, идемпотентность, revision/generation и correction.
- [x] Создать шесть локальных схем: common, extraction, review, manifest, semantic annotations, supporting fixtures.
- [x] Описать восемь состояний, все 64 пары переходов и 11 сценариев повторов/отказов.
- [x] Создать 16 синтетических PDF / 18 страниц, текст/OCR, semantic annotations, ожидаемые extraction/review/Task результаты и синтетический справочник.
- [x] Разделить набор 12 development / 4 holdout; оригинал и исправленный скан оставить в одной source_family/split.
- [x] Добавить воспроизводимый PDF-генератор, который не переписывает эталонные JSON и hash manifest.
- [x] Добавить автономный validator и 43 проверки существенных инвариантов и негативных случаев.
- [x] Отрендерить и визуально проверить все страницы; подтвердить отсутствие скрытого текста у raster scan.
- [x] Зафиксировать пороги пилота и расширение до 42 писем: 12 development + 30 holdout.
- [x] Выполнить независимое ревью, исправить найденные несоответствия и обновить roadmap.

## Проверки 18.10.2026

Из корня репозитория в подготовленном инструментальном окружении:

```sh
python scripts/validate_posteingang_fixtures.py
python -m unittest discover -s tests/posteingang -p 'test_*.py' -v
ruff check scripts/generate_posteingang_fixtures.py scripts/validate_posteingang_fixtures.py tests/posteingang/test_contracts.py
git diff --check
```

Validator: PASS, 16 писем / 18 страниц, splits 12/4. Unittest: 43 PASS; отдельный тест повторно генерирует все PDF и сравнивает SHA-256, не изменяя gold. Ruff 0.14.0: PASS по pyproject.toml. Проверены также whitespace новых файлов. Все 18 страниц отрендерены `pdftoppm` и просмотрены; плохое качество AP01-11 намеренно. Скриншоты проверки остаются временными, в git не добавляются.

Для текущей локальной проверки использован bundled Python 3.12.14 с ReportLab 4.4.9, Pillow 12.3.0 и pypdf 6.10.0; jsonschema 4.23.0 и Ruff установлены только во временный каталог. Независимая установка описана в контракте и `scripts/requirements-posteingang.txt`. Полный локальный CI image build и браузерные сценарии не запускались: production/UI не менялись.

Во время ревью дополнительно зафиксированы: лимиты sender/letter_type согласованы с Data255; semantic gold плохого скана содержит реальное действие/дату отдельно от unreadable fallback; candidate provenance проверяется по адресату, а не только по правам; worker publication проверяет переключатель анализа; correction не требует ещё не выбранного нового Customer. Исправления имеют регрессионные тесты.

## Вход для следующих этапов

AP-02 использует модель DocTypes, indexes, defaults и permission matrix из контракта. AP-04 использует canonical extraction, evidence и ограничения; из сырого ответа модели нельзя принимать server-only verification. AP-06 использует типизированные идентификаторы и candidate provenance. AP-07 использует review/receipt, correction и уникальные ключи происхождения Task.

Смысловые annotations предназначены для оценки качества; ожидаемый fake/fallback extraction не заменяет ground truth. AP01-11 остаётся в знаменателе recall, даже когда ожидается ручной разбор. Ни автономные проверки, ни declarative сценарии не доказывают runtime permissions, конкурентность БД или качество Mistral. Реальные документы, ключ/контур провайдера и пилот относятся к AP-04/AP-10.

PR, deployment и автоматический переход к AP-02 не выполнялись. Изменения оставлены в рабочей ветке владельцу задачи.
