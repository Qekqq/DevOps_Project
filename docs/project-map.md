# Карта проекта

Назначение файлов сверено с текущей структурой 9 сентября 2026 года.
[Архитектура и последовательность запросов](architecture.md) показывают, как эти
файлы работают вместе. Здесь пути указаны относительно корня проекта.

Не каждый файл исполняется: Python и JavaScript содержат программную логику,
YAML/JSON/INI/HCL задают настройки, SQL описывает БД, CSV хранит данные,
а Markdown — документацию. Тесты запускаются для проверки, не для обслуживания
обычного запроса пользователя.

## Структура, связанная с архитектурой

```text
DevOps_Project/
├── frontend/          Интерфейс браузера и Nginx → сервис frontend
├── src/
│   ├── app.py         HTTP-запросы → сервис diabetes-api
│   ├── db/            Общая работа с PostgreSQL
│   ├── kafka/         Отправка сообщений и сервис kafka-consumer
│   ├── secrets/       Клиент Vault для Python-сервисов
│   ├── telemetry.py   Метрики и события API / обработчика / экспортера
│   └── ...            Модели, обучение, исследования и качество
├── monitoring/        Настройки Alloy, Prometheus, Loki и Grafana
├── vault/             Настройки сервера Vault
├── db/                SQL-структура и аудит PostgreSQL
├── scripts/           Административные команды и запуск стенда
├── tests/             Проверки кода и связей между сервисами
├── notebooks/         Исследование данных и эксперименты с моделями
├── data/              Исходные данные и локальные снимки
├── models/            Сохранённые выпуски моделей, управляемые DVC
├── reports/           Метрики обучения и выбранные параметры
├── docs/              Схемы архитектуры и эта карта
├── .github/           CI и CD
└── docker-compose.yml Связывает контейнеры, сеть, тома и проверки готовности
```

## Корень и служебные настройки

| Файл | Назначение |
|---|---|
| `README.md` | Описание проекта, запуск и основные команды |
| `docker-compose.yml` | Все 11 сервисов, зависимости, настройки запуска, тома и healthcheck |
| `Dockerfile` | Общий Python-образ API, обработчика и экспортера; команды запуска различаются |
| `requirements.txt` | Зависимости Python |
| `pyproject.toml` | Настройки инструментов Python и проверок проекта |
| `Makefile` | Короткие команды для типовых операций |
| `config.ini` | Пути и настройки данных и разбиения выборки |
| `training.json` | Алгоритмы и параметры обучения |
| `dvc.yaml` | Этапы воспроизводимого конвейера данных и обучения |
| `dvc.lock` | Зафиксированное состояние зависимостей и результатов этапов DVC |
| `.dvc/config` | Настройка удалённого хранилища DVC |
| `.dvc/.gitignore` | Исключения служебных файлов DVC из Git |
| `.dvcignore` | Исключения из обхода DVC |
| `.gitignore` | Локальные данные, секреты, кеши и артефакты, не попадающие в Git |
| `.gitattributes` | Правила обработки файлов Git |
| `.dockerignore` | Что исключается из контекста сборки Python-образа |

## Браузер и Nginx

| Файл | Назначение |
|---|---|
| `frontend/index.html` | Начальный HTML-документ приложения |
| `frontend/app.js` | Навигация, состояние интерфейса, отправка прогноза, загрузка и обновление карточки |
| `frontend/js/api.js` | Общий механизм HTTP-запросов и обработки ответов API |
| `frontend/js/ui.js` | Общие помощники для HTML, иконок и элементов интерфейса |
| `frontend/js/views.js` | Представления страниц и результата прогноза |
| `frontend/js/history.js` | Представление истории исследований |
| `frontend/js/study-card.js` | Карточка исследования, строки прогнозов разных моделей |
| `frontend/js/monitoring.js` | Встраивание дашборда Grafana и ссылки на управление им |
| `frontend/styles.css` | Общая стилистика приложения |
| `frontend/nginx.conf` | Раздача интерфейса, проксирование API и Grafana, проверка доступа |
| `frontend/Dockerfile` | Образ контейнера frontend |
| `frontend/.dockerignore` | Исключения из его контекста сборки |
| `frontend/.prettierrc.json` | Правила форматирования HTML, CSS и JavaScript |
| `frontend/assets/donut.png` | Иллюстрация интерфейса |
| `frontend/assets/fonts/manrope.css` | Подключение локальных шрифтов |
| `frontend/assets/fonts/manrope-0.ttf`, `manrope-1.ttf`, `manrope-2.ttf`, `manrope-3.ttf`, `manrope-4.ttf` | Файлы шрифта Manrope |
| `frontend/assets/fonts/OFL.txt` | Лицензия шрифта |

## API, исследования и авторизация

| Файл | Назначение |
|---|---|
| `src/app.py` | FastAPI, проверки готовности, новый прогноз основной модели, публикация в Kafka |
| `src/auth.py` | Вход, выход, сессии и ограничения доступа |
| `src/passwords.py` | Создание и проверка хешей паролей |
| `src/schemas.py` | Структуры и проверка входных и выходных данных API |
| `src/studies.py` | Маршруты истории и карточки исследования |
| `src/study_editing.py` | Изменение исследования, обратная связь, пересчёт связанных моделей внутри API |
| `src/features.py` | Работа с набором признаков модели |
| `src/clinical_features_v1.py` | Общие определения и правила клинических признаков |
| `src/logger.py` | Настройка обычного журналирования Python |
| `src/telemetry.py` | Prometheus-метрики, измерение времени, структурированные события и идентификатор запроса |
| `src/__init__.py` | Обозначает пакет Python; не отдельный сервис |

## PostgreSQL

Эти модули используются несколькими сервисами. Один файл доступа к БД
не означает, что существует только один процесс, имеющий к ней доступ.

| Файл | Назначение |
|---|---|
| `src/db/database.py` | Получение параметров из Vault, создание подключения и сессий SQLAlchemy |
| `src/db/models.py` | Таблицы, поля, связи и ограничения в виде Python-моделей |
| `src/db/repositories.py` | Операции чтения и записи исследований, прогнозов, версий моделей и данных |
| `src/db/load_dataset.py` | Загрузка подготовленного набора данных |
| `src/db/load_raw_dataset.py` | Загрузка исходного датасета с проверками |
| `src/db/export_feedback.py` | Выгрузка данных с обратной связью для дальнейшего обучения |
| `src/db/__init__.py` | Пакет модулей БД |
| `db/01_schema.sql` | SQL-структура базы |
| `db/02_audit.sql` | Механизмы аудита изменений |

Главные таблицы: `users`, `user_sessions`, `studies`, `predictions`, `model_versions`,
`feedback`. Для происхождения данных и аудита: `datasets`, `dataset_rows`,
`training_runs`, `study_edits`, `feedback_history`, `model_role_history`.
Для незавершённых фоновых расчётов — `shadow_retries`; задачи удаляются после
успеха или выхода версии из роли challenger. Таблицу создаёт обновление БД при CD.

## Kafka и фоновые модели

| Файл | Назначение |
|---|---|
| `src/kafka/producer.py` | Публикация сообщения в Kafka и ожидание подтверждения приёма брокером |
| `src/kafka/consumer.py` | Постоянный цикл обработчика, сохранение основного прогноза, запуск фонового расчёта, подтверждение обработки |
| `src/kafka/shadow.py` | Чтение активных challenger из БД, расчёт и сохранение их прогнозов |
| `src/kafka/retries.py` | Запись неудачных фоновых задач в PostgreSQL и автоматические повторы |
| `src/kafka/__init__.py` | Пакет модулей Kafka |

## Обучение и загрузка моделей

| Файл | Назначение |
|---|---|
| `src/config.py` | Чтение конфигурации проекта |
| `src/datasets.py` | Чтение и проверка исходных данных |
| `src/data_preprocessing.py` | Подготовка и разбиение выборок |
| `src/pipelines.py` | Предобработка и алгоритмы в единых pipeline |
| `src/train.py` | Обучение, подбор, оценка и сохранение выпуска |
| `src/predict.py` | Расчёт прогноза с загруженной моделью |
| `src/model_registry.py` | Проверка артефактов и загрузка моделей с кешированием |
| `src/register_release.py` | Проверка и регистрация выпуска в БД |
| `src/feedback_dataset.py` | Формирование воспроизводимого набора по обратной связи |
| `data/raw/diabetes.csv` | Исходные данные; это не программный код |
| `reports/metrics.json` | Метрики обучения и оценки |
| `reports/selected_parameters.json` | Выбранные параметры моделей |
| `notebooks/01_eda_preprocessing_split.ipynb` | Анализ данных и разбиение выборок |
| `notebooks/02_logistic_regression.ipynb` | Эксперименты с логистической регрессией |
| `notebooks/03_decision_tree.ipynb` | Эксперименты с деревом решений |
| `notebooks/04_hyperparameter_tuning.ipynb` | Подбор гиперпараметров |
| `notebooks/05_model_comparison.ipynb` | Сравнение моделей |

`models/current.json` и `models/<release>/` — локально доступные артефакты,
которые управляются DVC и не перечисляются как обычный код Git. В выпуске хранятся
обученные pipeline и сведения о результатах и происхождении обучения.

## Vault и мониторинг

| Файл | Назначение |
|---|---|
| `vault/server.hcl` | Хранилище, адреса и настройки сервера Vault |
| `src/secrets/vault_client.py` | AppRole-вход и чтение секретов PostgreSQL/Kafka |
| `src/secrets/__init__.py` | Пакет доступа к секретам |
| `src/metrics_exporter.py` | Процесс экспортера: периодическое чтение БД и публикация метрик |
| `src/monitoring.py` | Расчёт качества моделей на данных БД |
| `src/model_health.py` | SQL-агрегаты качества и drift за заданный интервал; train-эталон версии |
| `src/model_health_history.py` | Динамика по дням/месяцам, итог за период и фильтр модели; API в `src/monitoring.py` |
| `src/model_health_exporter.py` | Публикация месячных агрегатов в Prometheus |
| `monitoring/alloy/config.alloy` | Сбор CPU/памяти контейнеров и JSONL-журналов |
| `monitoring/prometheus/prometheus.yml` | Адреса и интервалы сбора метрик |
| `monitoring/loki/config.yml` | Хранение и обработка журналов Loki |
| `monitoring/grafana/provisioning/datasources/prometheus.yml` | Источник метрик в Grafana |
| `monitoring/grafana/provisioning/datasources/loki.yml` | Источник журналов в Grafana |

Панели, которые редактируются через Grafana, сейчас находятся в `grafana_data`.
Новый дашборд «Метрики модели» пока не создавался: подготовлены его расчёты.
Пользовательский дашборд «Работа приложения» остаётся в томе Grafana.

## Административные команды

| Файл | Назначение |
|---|---|
| `scripts/start_stack.py` | Первоначальная подготовка и одноразовый стенд CI/CD; локальная сборка только с `--local-build` |
| `scripts/deploy_release.py` | Выкладка проверенных образов, обязательная копия БД; возобновление установленного приложения через `make start` |
| `scripts/package_release.py` | Пакет конфигурации и образов по digest из проверенного коммита |
| `scripts/manage_runner.ps1` | Установка и запуск Windows runner через `make setup-runner` и `make runner` |
| `scripts/preview_frontend.py` | Временный просмотр локальных изменений интерфейса через `make preview` |
| `scripts/seed_demo_history.py` | Воспроизводимая помеченная история через расчёт API и Kafka, без перезаписи существующих данных |
| `scripts/export_model_history.py` | Ретроспективные агрегаты всех трёх блоков по датам исследования |
| `scripts/update_database.py` | Приведение БД к ожидаемой структуре |
| `scripts/generate_schema.py` | Формирование SQL-схемы из моделей |
| `scripts/create_user.py` | Создание пользователя приложения |
| `scripts/activate_model_release.py` | Активация выпуска и выбор основной версии |
| `scripts/backup_database.py` | Создание резервной копии БД |
| `scripts/verify_database_backup.py` | Проверка резервной копии через восстановление |
| `scripts/configure_dvc_yandex.py` | Настройка доступа к удалённому хранилищу DVC |

## CI/CD и тесты

| Файл | Что проверяет или запускает |
|---|---|
| `.github/workflows/ci.yml` | Проверки кода и сборка образов |
| `.github/workflows/cd.yml` | После успешного CI в main: одноразовая проверка, затем обновление постоянного приложения через Windows runner |
| `.github/actions/pull-models/action.yml` | Получение артефактов моделей через DVC |
| `tests/test_api.py` | Поведение API и нового прогноза |
| `tests/test_auth.py` | Авторизация и доступ |
| `tests/test_passwords.py` | Пароли и хеши |
| `tests/test_studies.py` | История и карточки исследований |
| `tests/test_study_editing.py` | Исправления, пересчёт и аудит |
| `tests/test_repositories.py` | Правила операций с данными |
| `tests/test_kafka.py` | Отправка и обработка сообщений |
| `tests/test_shadow.py` | Фоновые прогнозы |
| `tests/test_vault_client.py` | Клиент секретов |
| `tests/test_monitoring.py` | Метрики и расчёт качества |
| `tests/test_model_health.py` | Окно месяца, отсутствующие замеры и смещение распределений |
| `tests/test_model_health_history.py` | Границы периода, группировка, фильтр модели, пустые точки и доступ к API |
| `tests/test_demo_history.py` | Распределение дат, генерация и воспроизводимость демонстрационной партии |
| `tests/test_model_registry.py` | Реестр и загрузка артефактов |
| `tests/test_register_release.py` | Регистрация выпусков |
| `tests/test_data_preprocessing.py` | Подготовка и разбиение данных |
| `tests/test_feedback_dataset.py` | Набор данных по обратной связи |
| `tests/test_pipelines.py` | Pipeline моделей |
| `tests/test_raw_dataset.py` | Исходный датасет |
| `tests/test_train.py` | Обучение и артефакты |
| `tests/integration/conftest.py` | Общая подготовка интеграционных проверок |
| `tests/integration/test_database_contract.py` | Реальная схема и контракт БД |
| `tests/integration/test_prediction_flow.py` | Связь API, Kafka, обработчика и БД; сохранение и повтор фоновых задач |
| `tests/integration/test_monitoring_stack.py` | Работа компонентов мониторинга вместе |
| `tests/integration/test_vault_lifecycle.py` | Перезапуск Vault и восстановление доступа |
| `tests/__init__.py` | Пакет тестов |
| `tests/test_shadow_retries.py` | Повторы, паузы после ошибок и отмена задачи при смене роли модели |

## Документация и локальные данные

| Путь | Что это |
|---|---|
| `docs/architecture.md` | Общая архитектура, путь нового прогноза, редактирование, мониторинг и обучение |
| `docs/c4-model.md` | Схемы C4: контекст системы, контейнеры, компоненты API и обработчика |
| `docs/model-monitoring.md` | Период и группировка метрик, API динамики, DVC/MLflow и план переобучения |
| `docs/demo-history.md` | Происхождение искусственной партии, результаты импорта и ограничения исторических графиков |
| `reports/demo-model-history-2026-09-09.csv` | Агрегаты по 60 датам и двум моделям для проверки динамики |
| `docs/project-map.md` | Эта карта файлов |
| `.venv/` | Локальная среда Python и установленные библиотеки |
| `__pycache__/`, `.pytest_cache/`, `.ruff_cache/` | Восстанавливаемые кеши инструментов |
| `logs/` | Локальные журналы |
| `backups/` | Резервные копии; не временный мусор |
| `data/feedback/` | Снимки данных по обратной связи; не исходный код |
| `.env` и локальные настройки доступа, если созданы | Конфигурация конкретного стенда, не для публикации |
| `vault-keys.kdbx` вне проекта | KeePass-база для управления Vault |

Docker-тома хранят данные отдельно от исходников: `pgdata` — PostgreSQL,
`vault_data` — Vault, `kafka_data` — Kafka, `grafana_data` — настройки и панели Grafana,
`prometheus_data` — метрики, `loki_data` — журналы, `alloy_data` — состояние Alloy,
`application_logs` — файлы событий приложения. Они не являются файлами исходного
кода в VS Code и не сохраняются обычным коммитом Git.
