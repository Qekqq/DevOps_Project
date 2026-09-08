from configparser import ConfigParser
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = PROJECT_ROOT / "config.ini"


def load_config(config_path: Path = CONFIG_PATH) -> ConfigParser:
    """
    Загружает конфигурацию проекта из config.ini.
    """
    config = ConfigParser()
    config.read(config_path, encoding="utf-8")

    if not config.sections():
        raise FileNotFoundError(f"Config file was not found or is empty: {config_path}")

    return config


def get_project_root() -> Path:
    """
    Возвращает абсолютный путь к корневой директории проекта.
    """
    return PROJECT_ROOT


def get_path(config: ConfigParser, section: str, option: str) -> Path:
    """
    Читает относительный путь из config.ini и возвращает абсолютный путь.
    """
    relative_path = config.get(section, option)
    return PROJECT_ROOT / relative_path
