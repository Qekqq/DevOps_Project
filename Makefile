.DEFAULT_GOAL := help

ifeq ($(OS),Windows_NT)
SHELL := cmd.exe
.SHELLFLAGS := /C
PYTHON ?= .venv/Scripts/python.exe
else
PYTHON ?= .venv/bin/python
endif

KEEPASS_DB ?=
BACKUP_SOURCE ?=
BACKUP_TARGET ?=
RELEASE ?=
export PYTHONUTF8 := 1
export PYTHONIOENCODING := utf-8

.PHONY: help install preprocess train test start runner setup-runner preview backup security-plan security-migrate security-restore backup-encrypt backup-decrypt

help: ## Показать список команд
	@"$(PYTHON)" -c "from pathlib import Path; print('\n'.join('make {:<13} - {}'.format(line.split(':', 1)[0], line.split('##', 1)[1].strip()) for line in Path('Makefile').read_text(encoding='utf-8').splitlines() if not line.startswith((' ', '\t')) and ': ## ' in line))"

install: ## Установить зависимости Python в .venv
	"$(PYTHON)" -m pip install -r requirements.txt

preprocess: ## Подготовить данные для обучения
	"$(PYTHON)" -m src.data_preprocessing

train: ## Обучить модели
	"$(PYTHON)" -m src.train

test: ## Запустить тесты
	"$(PYTHON)" -m pytest tests -v

start: ## Запустить приложение, разблокировать Vault и включить runner
	"$(PYTHON)" -m scripts.deploy_release --resume --keepass-db "$(KEEPASS_DB)"
ifeq ($(OS),Windows_NT)
	@if exist ".local-history\actions-runner\.runner" powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts/manage_runner.ps1
endif

ifeq ($(OS),Windows_NT)
runner: ## Включить зарегистрированный runner отдельно
	powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts/manage_runner.ps1

setup-runner: ## Установить и зарегистрировать runner — первичная настройка
	powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts/manage_runner.ps1 -Install
else
runner setup-runner:
	@echo This runner is configured for Windows.
	@exit 1
endif

preview: ## Предпросмотр локального интерфейса на localhost:8081 — затем Ctrl+F5
	"$(PYTHON)" -m scripts.preview_frontend

backup: ## Создать резервную копию PostgreSQL
	"$(PYTHON)" -m scripts.backup_database

security-plan: ## Проверить установленный выпуск и подготовить план переноса без изменений приложения
	"$(PYTHON)" -m scripts.security_migration_plan $(if $(strip $(RELEASE)),--release "$(RELEASE)",)

security-migrate: ## Однократно перенести старую установку на проверенный выпуск (RELEASE); требует KeePass
	"$(PYTHON)" -m scripts.security_migration --apply --release "$(RELEASE)" --keepass-db "$(KEEPASS_DB)"

security-restore: ## Вручную вернуть исходную установку после незавершённого переноса; требует KeePass
	"$(PYTHON)" -m scripts.security_migration --apply --restore --keepass-db "$(KEEPASS_DB)"

backup-encrypt: ## Зашифровать готовый бэкап для внешнего хранения (BACKUP_SOURCE, BACKUP_TARGET)
	"$(PYTHON)" -m scripts.encrypted_backup "$(BACKUP_SOURCE)" "$(BACKUP_TARGET)"

backup-decrypt: ## Расшифровать бэкап в новый файл; БД не восстанавливается автоматически
	"$(PYTHON)" -m scripts.encrypted_backup --decrypt "$(BACKUP_SOURCE)" "$(BACKUP_TARGET)"
