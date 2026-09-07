# DevOps HW 4 — Diabetes Prediction API with Apache Kafka

## 1. Описание проекта

Проект выполнен в рамках лабораторной работы №4 по дисциплине «Devops».

Тема лабораторной работы: **«Интеграция Apache Kafka сервиса»**.

Цель работы — интегрировать брокер сообщений Apache Kafka в сервис и реализовать асинхронную передачу результатов работы модели с последующим сохранением в базу данных.

В качестве брокера сообщений используется **Apache Kafka** (режим KRaft, без ZooKeeper), а для хранения секретов — **Hashicorp Vault**.

Проект основан на лабораторной работе №3. В предыдущей версии был реализован FastAPI-сервис машинного обучения, PostgreSQL-база данных, хранение секретов в Hashicorp Vault и CI/CD pipeline. В лабораторной работе №4 проект был расширен: после выполнения прогноза API публикует результат в Apache Kafka (роль Producer), а отдельный сервис-потребитель (Consumer) читает сообщения из топика и сохраняет результат в PostgreSQL.

## 2. Ссылки

GitHub repository:

```text
https://github.com/Qekqq/devops_hw_4
```

DockerHub image:

```text
https://hub.docker.com/repository/docker/qekqq/devops_hw_4_api/general
```

Docker image name:

```text
qekqq/devops_hw_4_api
```

## 3. Основная функциональность

В проекте реализовано:

* FastAPI API-сервис для инференса ML-модели;
* публикация результата прогноза в Apache Kafka (Kafka Producer);
* отдельный сервис-потребитель `kafka-consumer` (Kafka Consumer);
* PostgreSQL-база данных для хранения данных проекта;
* Hashicorp Vault для хранения секретов подключения к БД и Kafka;
* init-контейнер `vault-init` для записи секретов PostgreSQL и Kafka в Vault;
* получение параметров подключения к PostgreSQL и Kafka через Vault;
* асинхронное сохранение результата работы модели в таблицу `prediction_history` сервисом-потребителем;
* загрузка обработанного датасета в таблицы `datasets` и `dataset_samples`;
* Docker Compose для запуска API, Kafka Consumer, Apache Kafka, PostgreSQL, Vault и vault-init;
* CI pipeline для тестирования, сборки Docker image и публикации в DockerHub;
* CD pipeline для запуска контейнеров и функционального тестирования с Kafka;
* автоматизированные тесты через pytest.

## 4. Стек технологий

В проекте использовались:

* Python 3.11;
* FastAPI;
* Uvicorn;
* Pydantic;
* pandas;
* numpy;
* scikit-learn;
* Apache Kafka;
* kafka-python;
* SQLAlchemy;
* psycopg2-binary;
* hvac;
* PostgreSQL 16;
* Hashicorp Vault;
* Docker;
* Docker Compose;
* pytest;
* GitHub Actions;
* DockerHub.

## 5. Архитектура проекта

Архитектура лабораторной работы №4:

```text
              Hashicorp Vault
            (секреты Kafka и БД)
                    ↑   ↑
                    │   │
Client → FastAPI (/predict) ──[Producer]──→ Apache Kafka
                                          (topic: prediction-results)
                                                    │
                                                    ▼
                                            kafka-consumer
                                              [Consumer]
                                                    │
                                                    ▼
                                               PostgreSQL
```

Логика работы:

1. PostgreSQL и Apache Kafka запускаются как отдельные контейнеры.
2. Vault запускается как отдельный контейнер в dev-режиме.
3. Контейнер `vault-init` записывает в Vault параметры подключения к PostgreSQL и Kafka.
4. FastAPI-сервис выполняет прогноз и публикует результат в топик Kafka (роль Producer), читая параметры подключения из Vault.
5. Сервис-потребитель `kafka-consumer` читает сообщения из топика и сохраняет результат в PostgreSQL.

## 6. Структура проекта

```text
.
├── .github/
│   └── workflows/
│       ├── ci.yml
│       └── cd.yml
├── data/
│   ├── raw/
│   │   └── diabetes.csv
│   └── processed/
│       ├── train.csv
│       ├── valid.csv
│       └── test.csv
├── db/
│   ├── 01_schema.sql
│   └── 02_seed.sql
├── experiments/
│   └── best_model/
│       ├── model.joblib
│       └── validation_metrics.json
├── src/
│   ├── db/
│   │   ├── database.py
│   │   ├── load_dataset.py
│   │   ├── models.py
│   │   └── repositories.py
│   ├── kafka/
│   │   ├── __init__.py
│   │   ├── producer.py
│   │   └── consumer.py
│   ├── secrets/
│   │   ├── __init__.py
│   │   └── vault_client.py
│   ├── app.py
│   ├── config.py
│   ├── data_preprocessing.py
│   ├── features.py
│   ├── logger.py
│   ├── predict.py
│   ├── schemas.py
│   └── train.py
├── tests/
│   ├── test_api.py
│   ├── test_data_preprocessing.py
│   ├── test_kafka.py
│   ├── test_predict.py
│   └── test_train.py
├── vault/
│   └── init-vault.sh
├── .dockerignore
├── .env.example
├── .gitignore
├── config.ini
├── Dockerfile
├── docker-compose.yml
├── README.md
└── requirements.txt
```

## 7. Конфигурация окружения

В проекте используется `.env.example` как шаблон локальной конфигурации.

Пример `.env.example`:

```env
POSTGRES_HOST=db
POSTGRES_PORT=5432
POSTGRES_DB=diabetes
POSTGRES_USER=diabetes_owner
POSTGRES_PASSWORD=change_me

VAULT_ADDR=http://vault:8200
VAULT_TOKEN=change_me
VAULT_KV_MOUNT=secret
VAULT_DB_SECRET_PATH=database/postgres
VAULT_KAFKA_SECRET_PATH=kafka/config

KAFKA_BOOTSTRAP_SERVERS=kafka:9092
KAFKA_PREDICTION_TOPIC=prediction-results
KAFKA_CONSUMER_GROUP=prediction-results-consumer
```

Файл `.env` создаётся локально и не должен попадать в Git.

В GitHub Actions значения передаются через Repository Secrets:

```text
DOCKERHUB_USERNAME
DOCKERHUB_TOKEN
POSTGRES_DB
POSTGRES_USER
POSTGRES_PASSWORD
VAULT_TOKEN
```

## 8. Hashicorp Vault

Vault запускается в `docker-compose.yml` как отдельный контейнер:

```text
devops_hw_4_vault
```

Для инициализации Vault используется отдельный контейнер:

```text
devops_hw_4_vault_init
```

Он выполняет скрипт:

```text
vault/init-vault.sh
```

Скрипт записывает параметры подключения к PostgreSQL и Kafka в Vault по путям:

```text
secret/data/database/postgres
secret/data/kafka/config
```

В Vault записываются следующие параметры:

```text
POSTGRES_HOST
POSTGRES_PORT
POSTGRES_DB
POSTGRES_USER
POSTGRES_PASSWORD

KAFKA_BOOTSTRAP_SERVERS
KAFKA_PREDICTION_TOPIC
KAFKA_CONSUMER_GROUP
```

После успешной записи секретов init-контейнер завершает работу со статусом `Exited (0)`.

## 9. Получение секретов в приложении

Для взаимодействия с Vault реализован файл:

```text
src/secrets/vault_client.py
```

Он использует библиотеку `hvac` и читает секреты из Vault. Функция `get_database_secrets()` возвращает параметры подключения к PostgreSQL, а функция `get_kafka_secrets()` — параметры подключения к Apache Kafka.

Файл:

```text
src/db/database.py
```

получает параметры подключения к PostgreSQL через функцию `get_database_secrets()`, формирует SQLAlchemy connection URL, создаёт engine и открывает сессию для работы с PostgreSQL.

Таким образом, исходный код не содержит логин, пароль, адрес, порт базы данных, адреса брокера Kafka или токены доступа.

## 10. Kafka Producer

Kafka Producer реализован в файле:

```text
src/kafka/producer.py
```

Producer создаётся один раз (ленивая инициализация), параметры подключения (`bootstrap_servers`, топик) читаются из Vault, значения сериализуются в JSON. Функция `send_prediction_message()` публикует результат прогноза в топик `prediction-results`.

В файле `src/app.py` endpoint `/predict` после выполнения прогноза вызывает публикацию результата в Kafka. Публикация обёрнута в обработку ошибок: при недоступности брокера API всё равно возвращает результат прогноза клиенту.

## 11. Kafka Consumer

Kafka Consumer реализован в файле:

```text
src/kafka/consumer.py
```

Consumer запускается как отдельный сервис в `docker-compose.yml` командой:

```text
python -m src.kafka.consumer
```

Он подключается к брокеру с повторными попытками, подписывается на топик `prediction-results` и в бесконечном цикле читает сообщения. Для каждого сообщения результат прогноза сохраняется в таблицу `prediction_history` через репозитории (`require_champion_model`, `save_prediction_history`).

## 12. Запуск проекта через Docker Compose

### Отдельное локальное окружение DevOps_Project

Текущий `docker-compose.yml` использует имя проекта `devops_project`.
Контейнеры получают имена `devops_project-diabetes-api-1`,
`devops_project-db-1`, `devops_project-kafka-consumer-1` и аналогичные.
Создаются отдельные тома `devops_project_pgdata` и
`devops_project_kafka_data`; тома старого проекта `devops_hw_4` не используются.

Локальные адреса:

| Сервис | Адрес |
|---|---|
| API | http://127.0.0.1:8001 |
| Swagger | http://127.0.0.1:8001/docs |
| PostgreSQL | 127.0.0.1:5433 |
| Vault | http://127.0.0.1:8201 |

Kafka доступна внутри Compose-сети по `kafka:9092`, порт на хост не публикуется.
Внутренний адрес PostgreSQL остаётся `db:5432`; адрес Vault — `http://vault:8200`.
Файл `.env` создаётся локально по `.env.example` с собственными секретами.
Запускать команды нужно из корня `DevOps_Project`.
Ниже в исторических примерах лабораторной №4 встречаются старые имена
контейнеров и порты; для нового локального окружения используются адреса выше.

Запуск контейнеров:

```powershell
docker compose up -d --build
```

Проверка контейнеров:

```powershell
docker compose ps
```

Ожидаемые сервисы:

```text
devops_hw_4_api
devops_hw_4_kafka_consumer
devops_hw_4_kafka
devops_hw_4_db
devops_hw_4_vault
devops_hw_4_vault_init
```

Контейнер `devops_hw_4_vault_init` после выполнения может быть завершён со статусом `Exited (0)`. Это нормальное поведение, так как он нужен только для записи секретов в Vault.

Остановка контейнеров:

```powershell
docker compose down
```

Остановка контейнеров с удалением volumes:

```powershell
docker compose down -v
```

## 13. Проверка Vault init

Посмотреть логи init-контейнера:

```powershell
docker compose logs vault-init
```

Ожидаемый результат:

```text
Waiting for Vault...
Vault is available
Writing PostgreSQL secrets to Vault...
PostgreSQL secrets were written to Vault
Writing Kafka settings to Vault...
Kafka settings were written to Vault
```

Эти логи подтверждают, что параметры PostgreSQL и Kafka были записаны в Hashicorp Vault.

## 14. API endpoints

### GET `/health`

Проверяет состояние API-сервиса.

Пример запроса:

```powershell
Invoke-RestMethod http://localhost:8000/health
```

Пример ответа:

```json
{
  "status": "ok",
  "service": "diabetes-prediction-api"
}
```

### GET `/db/health`

Проверяет подключение API к PostgreSQL.

Этот endpoint подтверждает, что сервис смог получить параметры подключения к БД из Vault и подключиться к PostgreSQL.

Пример запроса:

```powershell
Invoke-RestMethod http://localhost:8000/db/health
```

Пример ответа:

```json
{
  "status": "ok",
  "database": "connected"
}
```

### POST `/predict`

Выполняет прогноз риска диабета и публикует результат в Apache Kafka.

Пример входных данных:

```json
{
  "patient_code": "PAT001",
  "pregnancies": 6,
  "glucose": 148,
  "blood_pressure": 72,
  "skin_thickness": 35,
  "insulin": 0,
  "bmi": 33.6,
  "diabetes_pedigree_function": 0.627,
  "age": 50
}
```

Пример ответа:

```json
{
  "prediction": 1,
  "probability": 0.8146575280180618,
  "label": "detected"
}
```

После выполнения запроса результат публикуется в топик Kafka `prediction-results`, а сервис-потребитель `kafka-consumer` сохраняет его в таблицу `prediction_history`.

## 15. Проверка записи прогноза в PostgreSQL

Перед проверкой можно посмотреть логи сервиса-потребителя:

```powershell
docker compose logs kafka-consumer
```

Подключение к PostgreSQL:

```powershell
docker exec -it devops_hw_4_db psql -U diabetes_owner -d diabetes
```

SQL-запрос:

```sql
SELECT
    id,
    patient_code_snapshot,
    prediction,
    probability,
    label,
    request_source,
    response_time_ms,
    created_at
FROM prediction_history
ORDER BY created_at DESC
LIMIT 5;
```

Пример результата:

```text
id | patient_code_snapshot | prediction | probability | label    | request_source | response_time_ms
1  | PAT001               | 1          | 0.81466     | detected | api            | 21
```

### Код пациента и повторные прогнозы

Реестр `model_versions` включает контрольную сумму `artifact_sha256` и
`train_medians` для воспроизводимой загрузки. `02_seed.sql` регистрирует текущую
champion `lab2-1.0.0` из `best_model` и три фоновых варианта: дерево,
настроенное дерево и обычную логистическую регрессию. Старый файл
`logistic_regression_tuned` не регистрируется дополнительно к текущей champion.
Повторное применение seed сохраняет роли существующих версий.
Для существующей БД поля добавляет `004_model_artifacts.sql`; заполнение и
проверка NOT NULL выполняются отдельно до подключения нового загрузчика.

Исследования хранятся в `studies`: код пациента, дата и исходные показатели.
Пара код + дата уникальна. Каждый прогноз связан через `study_id` с исследованием;
пара `study_id` + версия модели уникальна. Обратная связь в `prediction_feedback`
также ссылается на `study_id`, поэтому одна фактическая метка относится ко всем
прогнозам этого исследования, включая добавленные позже.
Прежние поля показателей в `prediction_history` пока сохранены как снимки прогноза.

Для существующей БД после `002_prediction_study.sql` и заполнения дат подготовлена
`003_studies_and_feedback.sql`. Она переносит связи обратной связи и останавливается
при противоречивых показателях либо нескольких метках одного исследования.
Эти миграции ещё требуют проверки на существующей БД перед обновлением контейнеров.
Выбор champion из БД и вычисление фоновых моделей находятся на следующем этапе;
добавление таблицы исследований само по себе их не включает.

Код вводится вручную: три латинские буквы и три цифры, например `PAT001`.
API удаляет пробелы по краям и приводит буквы к верхнему регистру.
Выбор из справочника не требуется: техническая запись в `patients` создаётся
при сохранении первого прогноза. Начальные данные содержат пример `PAT001`
без заранее созданного прогноза.

Обязательная `study_date` передаётся как `YYYY-MM-DD`, без времени.
Уникальность прогноза: код пациента + дата исследования + версия модели.
Точный повтор сохранённого запроса возвращает прежний результат (`200`).
Изменение показателей на ту же дату, в том числе для другой версии модели,
возвращает `409`. Другая дата либо другая версия модели с прежними показателями
допускают новый прогноз. При недоступности проверки истории возвращается `503`.

Сохранение через Kafka асинхронное: до появления первой записи несколько
запросов могут получить успешный ответ. Уникальный индекс
`uq_prediction_history_study_model` разрешает сохранить одну запись на тройку;
последующие сообщения не перезаписывают её. Репозиторий блокирует параллельное
сохранение для одного пациента до завершения транзакции и повторно проверяет
показатели. Эта проверка действует для записей через репозиторий, а не для
произвольных SQL-вставок. Код сравнивается без учёта
регистра и пробелов по краям. Повторная доставка и отказоустойчивость Kafka
требуют отдельной проверки.

Для новой БД индекс создаёт `db/01_schema.sql`. Для существующей подготовлен
`db/migrations/002_prediction_study.sql`. Применение выполняется вручную при
остановленных API и consumer. Затем нужно явно заполнить даты старых исследований
и установить `study_date NOT NULL`, как указано в миграции, до запуска приложения.
Записи не удаляются; дата записи `created_at` не подменяет дату исследования.
Поле `inference_payload` сохраняет исходные показатели и результат без округления
числовых SQL-столбцов. Для старых строк используется прежняя сохранённая точность.
Версия модели передаётся через Kafka из `config.ini` и должна быть зарегистрирована
в `model_versions`; изменение champion не меняет принадлежность сообщения.

## 16. Загрузка датасета в PostgreSQL

Для загрузки подготовленного датасета используется скрипт:

```text
src/db/load_dataset.py
```

Команда запуска:

```powershell
docker compose exec diabetes-api python -m src.db.load_dataset
```

Проверка количества строк:

```sql
SELECT split, COUNT(*) AS rows_count
FROM dataset_samples
GROUP BY split
ORDER BY split;
```

Ожидаемый результат:

```text
train | 536
valid | 116
test  | 116
```

## 17. Локальные тесты

Запуск тестов:

```powershell
pytest
```

Ожидаемый результат:

```text
24 passed
```

Проверяется:

```text
tests/test_api.py
tests/test_data_preprocessing.py
tests/test_kafka.py
tests/test_predict.py
tests/test_train.py
```

Модуль `tests/test_kafka.py` содержит юнит-тесты Kafka Producer и Consumer (публикация сообщения в топик, инициализация продьюсера из Vault, сохранение сообщения в БД и повторные попытки подключения Consumer к брокеру).

## 18. CI pipeline

CI pipeline описан в файле:

```text
.github/workflows/ci.yml
```

CI выполняет:

1. Checkout repository.
2. Установку Python 3.11.
3. Установку зависимостей из `requirements.txt`.
4. Запуск unit-тестов через pytest.
5. Сборку Docker image.
6. Публикацию Docker image в DockerHub.

Docker image:

```text
qekqq/devops_hw_4_api
```

Теги:

```text
latest
commit_sha
```

## 19. CD pipeline

CD pipeline описан в файле:

```text
.github/workflows/cd.yml
```

CD pipeline выполняет:

1. Checkout repository.
2. Авторизацию в DockerHub.
3. Проверку обязательных GitHub Secrets.
4. Pull Docker image из DockerHub.
5. Создание `.env` файла на runner (включая параметры Kafka).
6. Создание временного `docker-compose.cd.yml` (с сервисами Kafka и Kafka Consumer).
7. Запуск API, Kafka Consumer, Apache Kafka, PostgreSQL, Vault и vault-init.
8. Вывод логов `vault-init`.
9. Проверку `/health`.
10. Проверку `/db/health`.
11. Функциональный тест `/predict` (публикация результата в Kafka).
12. Ожидание асинхронного сохранения прогноза сервисом-потребителем.
13. Проверку записей в `prediction_history`.
14. Загрузку processed-датасета в PostgreSQL.
15. Проверку записей в `dataset_samples`.
16. Вывод логов Kafka Consumer, API и Vault.
17. Остановку контейнеров.

CD pipeline подтверждает, что сервис работает в контейнерной инфраструктуре, публикует результаты прогноза в Apache Kafka, а сервис-потребитель сохраняет их в PostgreSQL, используя секреты из Hashicorp Vault.

## 20. DockerHub

Docker image публикуется в DockerHub:

```text
qekqq/devops_hw_4_api
```

Команда pull:

```powershell
docker pull qekqq/devops_hw_4_api:latest
```

## 21. Безопасность

В исходном коде отсутствуют явно прописанные:

```text
логин БД
пароль БД
адрес БД
порт БД
адреса брокера Kafka
токены доступа
```

Секреты передаются через переменные окружения и записываются в Hashicorp Vault. API-сервис и сервис-потребитель получают параметры подключения к PostgreSQL и Apache Kafka из Vault.

Файл `.env` используется только локально и не добавляется в Git. В GitHub Actions секреты хранятся в Repository Secrets.

## 22. Результаты работы

В результате лабораторной работы №4 было реализовано:

* интеграция Apache Kafka в проект (режим KRaft, без ZooKeeper);
* публикация результата прогноза в Kafka (Kafka Producer);
* отдельный сервис-потребитель для сохранения результата в PostgreSQL (Kafka Consumer);
* хранение секретов Kafka и PostgreSQL в Hashicorp Vault;
* запуск API, Kafka Consumer, Apache Kafka, PostgreSQL и Vault через Docker Compose;
* переиспользование и доработка CI/CD pipeline под Kafka;
* юнит-тесты Kafka Producer и Consumer;
* публикация Docker image в отдельный DockerHub-репозиторий ЛР4.

## 23. Вывод

В ходе лабораторной работы №4 ML-сервис Diabetes Prediction API был расширен интеграцией с брокером сообщений Apache Kafka. После выполнения прогноза API публикует результат в топик Kafka, а отдельный сервис-потребитель асинхронно сохраняет его в PostgreSQL. Секреты Kafka и базы данных хранятся в Hashicorp Vault, поэтому исходный код не содержит данных авторизации.

Система запускается через Docker Compose и включает FastAPI API, Kafka Consumer, Apache Kafka, PostgreSQL, Hashicorp Vault и init-контейнер для записи секретов. CI/CD pipeline подтверждает корректность проекта: тесты проходят успешно, Docker image публикуется в DockerHub, а CD pipeline запускает контейнеры с Kafka и проверяет `/health`, `/db/health`, `/predict`, асинхронную запись прогнозов в БД и загрузку датасета.
