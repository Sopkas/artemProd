# Контракт хранения проверки — S2-01

Предложение A на согласование B до реализации SQLite и миграций. Это общие типы: `domain.analysis` и интерфейс `application.analysis_repository`. Эталонная реализация в памяти — `infrastructure.memory.analysis.InMemoryAnalysisRepository`; реализация на SQLite/SQLAlchemy/Alembic появится вторым PR после согласования и должна пройти те же контрактные тесты `tests/unit/test_analysis_repository.py` без изменений.

## Что хранится

| Тип | Поля | Правила |
| --- | --- | --- |
| `AnalysisRun` | `id`, `owner_id` (Telegram ID > 0), `analysis_date` (календарная дата), `mode` (`demo`/`live`), `status`, `created_at`, `updated_at` (UTC), `files` | Все файлы принадлежат этой проверке; `updated_at ≥ created_at` |
| `UploadedFile` | `id`, `run_id`, `kind`, `checksum` (SHA-256, hex в нижнем регистре), `size_bytes` > 0, `stored_path` (относительный путь внутри хранилища, без `..` и абсолютных путей), `uploaded_at` (UTC), `coverage` (`Period` или `None`) | `coverage` — период, который пользователь подтвердил для платежей/взаимодействий; для контрагентов обычно `None` |
| `FileKind` | `counterparties`, `payments`, `interactions`, `debt_history` | Листы из [контрактов данных](data-contracts.md); тип называет пользователь при загрузке |
| `RunStatus` | `draft → queued → running → completed \| partial \| failed` | Как в [архитектуре](architecture.md); финальные состояния без выходов; `can_transition()` — единственный источник правил |

Содержимое файлов и результаты разбора строк в этом контракте **не хранятся**: файл лежит на диске по `stored_path`, ошибки строк — результат импорта (S2-04) и добавятся отдельным типом при S2-03. Результаты шагов по ключу `(run_id, inn, step, version)` и очередь заданий — S2-02/S3-03, отдельным PR.

## Интерфейс `AnalysisRepository`

Асинхронный, как `CompanyDataProvider`. Каждый вызов принимает `owner_id`: проверка другого владельца и несуществующая проверка дают одинаковый `RunNotFound` без ID в сообщении.

| Метод | Результат | Ошибки |
| --- | --- | --- |
| `create_run(owner_id, analysis_date, mode)` | Новый `draft` без файлов | — |
| `get_run(owner_id, run_id)` | Снимок проверки с файлами | `RunNotFound` |
| `list_runs(owner_id)` | Проверки владельца, новые первыми | — |
| `add_file(owner_id, run_id, NewFile)` | Сохранённый `UploadedFile` | `RunNotFound`; `RunLocked`, если проверка уже не `draft` |
| `transition(owner_id, run_id, target)` | Снимок с новым статусом и `updated_at` | `RunNotFound`; `InvalidTransition(current, requested)`, состояние не меняется |

- Повтор одного файла (тот же `kind` и `checksum`) возвращает уже сохранённый файл и не создаёт дубликат — «повтор файла в пакете не дублирует строки». `coverage` при этом берётся от первой загрузки: одинаковое содержимое с другим объявленным периодом не меняет сохранённый период; чтобы сменить период, нужна новая проверка.
- Уникальность `stored_path` контракт не гарантирует: репозиторий доверяет вызывающему. Файловый адаптер (S2-03) обязан выдавать путь вида `<run_id>/<file_id>.xlsx` и дополнительно проверять, что разрешённый путь (после симлинков) остаётся внутри корня хранилища.
- `analysis_date` в будущем контракт не отклоняет: это проверка сценария при создании проверки в Telegram, а не инвариант хранения (репозиторий должен уметь сохранить то, что уже было принято).
- Пакет замораживается при переходе в `queued`: новые файлы требуют новой проверки.
- Возвращаемые объекты — неизменяемые снимки; последующие изменения в хранилище их не трогают.
- `RepositoryError` — техническая неисправность хранилища; реализация обязана бросать её, а не возвращать пустой успешный результат.
- `NewFile` проверяет те же инварианты, что `UploadedFile`, до обращения к хранилищу.

## Что просить у B

1. Достаточно ли `coverage` на файле для S2-04/S2-05, или период покрытия нужен и на уровне проверки.
2. Нужен ли `analysis_date` в `UploadedFile` (сейчас — только у проверки, «пакет имеет единую дату анализа»).
3. Ошибки строк импорта: отдельный тип `ImportIssue(file_id, sheet, row, column, reason)` в S2-03 — согласовать форму заранее, чтобы парсер S2-04 возвращал её же.

## Очередь заданий — контракт S2-02

Очередь — та же таблица проверок; отдельного объекта задания нет: «одна проверка — одно задание». Интерфейс `application.analysis_queue.AnalysisQueue` реализуют те же репозитории (`memory`, `sqlite`). Вызовы системные, без владельца: обработчик действует для всех, но не читает и не раскрывает содержимое пакетов.

| Метод | Результат | Правила |
| --- | --- | --- |
| `claim_next()` | Самая старая `queued` проверка, переведённая в `running` с `attempts + 1`, либо `None` | Порядок: `updated_at`, при равенстве — порядок создания (`sequence`), одинаково в памяти и SQLite. Атомарно: с двумя обработчиками одну проверку получит только один (обновление с проверкой статуса) |
| `finish(run_id, RunOutcome)` | Проверка в финальном статусе | `RunOutcome(status ∈ {completed, partial, failed}, failure)`; `failure` — безопасный текст только для `failed`/`partial`; `InvalidTransition`, если проверка не `running` |
| `recover_interrupted()` | Проверки, застрявшие в `running` | При старте: обратно в `queued` с сохранением `attempts`; после `MAX_ATTEMPTS = 3` прерываний — `failed` с причиной «обработка прерывалась несколько раз» |

Изменения общих типов: `AnalysisRun.attempts` (≥ 0) и `AnalysisRun.failure` (непустой текст, только при `failed`/`partial`; без ИНН, содержимого файлов и сообщений провайдера с ключами); переход `running → queued` разрешён только как восстановление после перезапуска. Миграция `0002` добавляет колонки и индекс по `(status, updated_at)`.

### Обработчик

`application/worker.py` — `RunWorker(queue, processor, poll_interval, notifier=None)`: `recover()` при старте, затем `run_forever()` как задача asyncio рядом с опросом Telegram; `process_one()` берёт одну проверку, вызывает `RunProcessor.process(run) -> RunOutcome`, исключение процессора превращается в `failed` с общим текстом (тип ошибки — только в журнал), сбой хранилища — пауза и повтор цикла. `application/package_processor.py` — тело шага спринта 2: перечитать файл «Контрагенты» из хранилища и записать `completed` / `partial` (число строк с ошибками) / `failed` (файл отсутствует или непригоден). S3-01 заменяет процессор конвейером импорт → провайдер → скоринг → отчёт.

## Шаги и артефакт отчёта — контракт S3-03

Архитектура: «результат шага сохраняется по ключу `(run_id, inn, step, version)`; завершённые сохранённые шаги повторно не выполняются; состояние доставки отчёта хранится отдельно от результата анализа». Типы — `domain/steps.py`, интерфейсы — `application/step_store.py`, реализуют те же репозитории (`memory`, `sqlite`, миграция `0003`).

| Тип / метод | Правила |
| --- | --- |
| `StepResult(run_id, inn, step, version, status, completed_at, payload, error)` | `inn` — 10 цифр или `RUN_SCOPE` (`""`) для шагов уровня проверки (отчёт); `step` — стабильное snake_case-имя (`external_fetch`, `scoring`, `explanation`, `report`); `version` — версия правил/адаптера/инструкции; `payload` — строка (JSON шага), непрозрачная для хранилища; `status ∈ {ok, failed}`, у `failed` — безопасный `error` и нет `payload` |
| `save_step(result)` | Идемпотентно: существующий результат с тем же ключом сохраняется и возвращается, новый не перезаписывает его — так «ровно один вызов» не гарантируется, но повторный дорогой шаг не переписывает уже сохранённый. `RunNotFound` для неизвестной проверки |
| `get_step(run_id, inn, step, version)` / `list_steps(run_id)` | Чтение по ключу и весь список в порядке сохранения; системные вызовы (без владельца) |
| `ReportArtifact(run_id, stored_path, created_at, delivery, delivered_at, delivery_error)` | Один артефакт на проверку; `delivery ∈ {pending, delivered, failed}`, время доставки только у `delivered`, ошибка только у `failed` |
| `save_report(run_id, stored_path)` | Новый отчёт заменяет прежний и снова `pending` |
| `get_report(owner_id, run_id)` | Owner-scoped: чужая проверка → `RunNotFound`; нет отчёта → `None` |
| `mark_delivery(run_id, status, error)` | Фиксирует результат отправки, не трогая статус проверки; повторная выдача готового отчёта не ставит проверку в очередь |

Смена версии (`version`) правил или адаптера означает новый ключ: старые результаты остаются в истории, шаг выполняется заново.

### Payload шагов `external_fetch` и `scoring` (B)

Сериализация доменных типов — `domain/serialization.py`; хранилище payload не разбирает.

| Шаг | Запись | Чтение |
| --- | --- | --- |
| `external_fetch` (один раздел одного ИНН) | `json.dumps(snapshot_to_dict(snapshot), ensure_ascii=False)` | `snapshot_from_dict(json.loads(payload))` |
| `scoring` (один ИНН) | `json.dumps(assessment_to_dict(assessment), ensure_ascii=False)` | `assessment_from_dict(json.loads(payload))` |

- В каждом payload есть `"schema": 1`; другое значение отклоняется (`PayloadError`), поэтому старый формат после изменения не читается молча. Смена формата = новая схема и новая `version` шага.
- Значения фактов хранятся с типом (`decimal` — строкой, `date` — ISO, `bool`, `int`, `str`, `company_status`, `none`): `Decimal` не становится `float`, `True` — `1`. Время — ISO с часовым поясом.
- Чтение идёт через обычные конструкторы, все проверки доменных типов срабатывают повторно; нарушение даёт `PayloadError`. Сообщения ошибок не содержат payload (там могут быть ИНН и тексты источника).
- Шаг `report` хранит файл через `ReportStore`, модель отчёта в payload не кладётся.

### Использование в конвейере (S3-01, S3-03)

- `application/analysis_pipeline.py` — `AnalysisPipeline(files, reader, provider, repository, mode=, build_report=, limits=, sections=, clock=)`, реализует `RunProcessor`. Шаги и ключи: `("", "import", "counterparties-v2")` — строки и замечания импорта; `(inn, "external_fetch", "sections-v1")` — снимки разделов по ИНН; `("", "report", "xlsx-v1-rules-<RULES_VERSION>")` — сводка (`RunSummary`: организаций, проверено полностью, ошибок строк, приоритеты, исчерпан ли бюджет) после `save_report`. Перед каждым шагом — `get_step`; сохранённый `ok` не выполняется заново, сохранённый `failed` импорта даёт `failed` проверки. Шаг `external_fetch` **не сохраняется**, если среди разделов есть заглушка guard'а «бюджет исчерпан» или временная ошибка источника (`timeout`, `network_error`, `http_error`, `rate_limited`) — новая попытка запросит организацию снова; окончательные ответы (`ok`, `not_found`, `unauthorized`, `invalid_response`) сохраняются. Отчёт, записанный до падения между `save_report` и шагом `report`, при возобновлении заменяется, старый файл удаляется. `checked_at` отчёта — самый ранний `fetched_at` среди разделов (кэш guard'а хранит своё время): все внешние данные не старее него. Payload'ы — `application/step_payloads.py`: `dump_/load_import` (строки и замечания, `"schema": 1`) и `dump_/load_snapshots` — список разделов одного ИНН, каждый в формате `domain/serialization.snapshot_to_dict`; нечитаемый payload → `failed` с безопасной причиной.
- `presentation/telegram/notifier.py` — `TelegramRunNotifier(bot, repository, files)`: после `finish` обработчик зовёт `notify(run)`; владельцу уходит сводка и файл отчёта, доставка фиксируется через `report_delivery`; ошибка отправки логируется типом исключения и не меняет статус проверки.
- `application/report_delivery.py`: `fetch_report(owner, repository, files)` — **новейший отчёт владельца** (`latest_report`: по `list_runs` от новых к старым до первой проверки с `get_report`; начатая позже черновая или очередная проверка отчёт не прячет) → `FileStorage.read`, `confirm_delivery` — `mark_delivery`. Отсутствующий файл фиксируется как `failed` доставка, проверка не трогается. Команда `/report` в Telegram ничего не ставит в очередь.

## Реализация на SQLite — шаг 2

`infrastructure/persistence/sqlite.py`: `open_sqlite_repository(path)` создаёт каталог и файл, применяет миграции Alembic до `head` и возвращает `SqliteAnalysisRepository`. Блокирующая работа с БД выполняется в рабочем потоке (`asyncio.to_thread`), поэтому цикл Telegram не блокируется; сетевых вызовов внутри транзакций нет.

- Схема: `analysis_runs` (id, owner_id, analysis_date, mode, status, created_at, updated_at, sequence) и `uploaded_files` (id, run_id → analysis_runs, kind, checksum, size_bytes, stored_path, uploaded_at, coverage_start/end, sequence) с уникальностью `(run_id, kind, checksum)`. Технические времена — ISO 8601 в UTC текстом; даты — ISO-даты; `sequence` задаёт порядок при равных временах.
- Миграции лежат внутри пакета (`persistence/migrations/`), чтобы работать и из установленного wheel; `alembic.ini` в корне — для `alembic revision --autogenerate` и `alembic check` (дрейф схемы относительно `schema.py`).
- Все методы owner-scoped на уровне запроса (`WHERE id = ? AND owner_id = ?`); `SQLAlchemyError` и обращение к закрытому хранилищу → `RepositoryError`.
- Приложение открывает хранилище при старте до опроса Telegram (`DATABASE_PATH`); сбой БД останавливает запуск с кодом 1, а не превращается в пустые ответы.

## Проверка

`tests/unit/test_analysis_domain.py` (инварианты типов и переходы), `tests/unit/test_analysis_repository.py` (контракт, параметризован по реализациям `memory` и `sqlite` — один и тот же набор), `tests/integration/test_sqlite_storage.py` (переживает переоткрытие файла, повторная миграция, создание каталога, сбой → `RepositoryError`, изоляция владельцев после перезапуска). Зависимости: `sqlalchemy`, `alembic` (закреплены в `requirements.txt`).


## Ответ B по контракту — 18.09.2026

1. `coverage` достаточно на уровне файла: разные выгрузки покрывают разные периоды. Это подтверждённый пользователем период входа, не гарантия полноты строк или внешней проверки. Покрытие фактов и разделов остаётся отдельным.
2. `analysis_date` хранится только у проверки; все файлы пакета анализируются на одну дату. Даты платежей и событий остаются в строках будущего импорта.
3. Форма `ImportIssue(file_id, sheet, row, column, reason)` подходит как основа. В S2-03 выделить также стабильный `code` и `severity` (ошибка/предупреждение); `row` — номер строки Excel с 1, `row`/`column` могут быть `None` для ошибки листа/файла. `reason` не должен включать содержимое ячейки или переписки. Конкретный тип вводить отдельным PR до парсера.

В ревью усилены инварианты: дата без времени, типизированные режим/статус/вид файла, неизменяемый список файлов, запрет путей Windows с диском, потоками и нулевым байтом, а также пути к корню. Будущий файловый адаптер дополнительно проверяет разрешённый путь после разрешения симлинков; одна строковая проверка этого не гарантирует.

Это согласование формы хранения; SQLite, миграции и подключение к Telegram ещё не реализованы. Изменения B в этом PR требуют просмотра A до объединения общих типов.
