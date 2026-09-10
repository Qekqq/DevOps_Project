.DEFAULT_GOAL := help

ifeq ($(OS),Windows_NT)
SHELL := cmd.exe
.SHELLFLAGS := /C
PYTHON ?= .venv/Scripts/python.exe
else
PYTHON ?= .venv/bin/python
endif

KEEPASS_DB ?=
export PYTHONUTF8 := 1
export PYTHONIOENCODING := utf-8

.PHONY: help install preprocess train test start runner setup-runner preview backup

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

preview: ## Обновить интерфейс из локальных файлов без сборки — затем Ctrl+F5
	"$(PYTHON)" -m scripts.preview_frontend

backup: ## Создать резервную копию PostgreSQL
	"$(PYTHON)" -m scripts.backup_database
