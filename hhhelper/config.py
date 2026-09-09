"""Конфигурация помощника: настройки, сопроводительные письма и фильтры вакансий.

Файл конфигурации — YAML (или JSON). Пример структуры лежит в `config.example.yaml`.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

try:  # PyYAML необязателен, если конфиг в формате JSON
    import yaml
except ImportError:  # pragma: no cover - зависит от окружения
    yaml = None  # type: ignore[assignment]

APP_NAME = "hhhelper"

#: Где ищем конфиг, если путь не задан явно.
CONFIG_SEARCH_PATHS = (
    "config.yaml",
    "config.yml",
    "config.json",
    "~/.config/hhhelper/config.yaml",
    "~/.config/hhhelper/config.yml",
    "~/.config/hhhelper/config.json",
)

#: Параметры, которые уходят в поисковый запрос GET /vacancies.
KNOWN_SEARCH_PARAMS = {
    "text",
    "search_field",
    "area",
    "metro",
    "professional_role",
    "industry",
    "employer_id",
    "experience",
    "employment",
    "schedule",
    "work_format",
    "salary",
    "currency",
    "only_with_salary",
    "period",
    "date_from",
    "date_to",
    "order_by",
    "label",
    "excluded_text",
    "per_page",
    "pages",
    "sort_point_lat",
    "sort_point_lng",
}


class ConfigError(Exception):
    """Ошибка в файле конфигурации."""


def config_dir() -> Path:
    """Каталог с пользовательскими данными (токен, база откликов)."""
    base = os.environ.get("HHHELPER_HOME")
    if base:
        return Path(base).expanduser()
    return Path.home() / ".config" / APP_NAME


@dataclass
class Letter:
    """Шаблон сопроводительного письма."""

    name: str
    text: str
    title: str = ""
    #: Ключевые слова: если хотя бы одно встречается в вакансии — письмо подходит.
    keywords: List[str] = field(default_factory=list)
    #: Имена фильтров, для которых письмо предназначено.
    filters: List[str] = field(default_factory=list)
    #: Письмо по умолчанию, если ничего не подошло.
    default: bool = False
    enabled: bool = True

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Letter":
        if not isinstance(data, dict):
            raise ConfigError("Каждое письмо в `letters` должно быть словарём")
        name = str(data.get("name") or "").strip()
        if not name:
            raise ConfigError("У письма отсутствует обязательное поле `name`")
        text = data.get("text")
        if text is None and data.get("file"):
            path = Path(str(data["file"])).expanduser()
            try:
                text = path.read_text(encoding="utf-8")
            except OSError as exc:
                raise ConfigError(f"Не удалось прочитать файл письма {path}: {exc}") from exc
        if not text or not str(text).strip():
            raise ConfigError(f"Письмо «{name}»: нужно задать `text` или `file`")
        match = data.get("match") or {}
        if not isinstance(match, dict):
            raise ConfigError(f"Письмо «{name}»: `match` должен быть словарём")
        return cls(
            name=name,
            text=str(text).strip("\n"),
            title=str(data.get("title") or name),
            keywords=_as_str_list(match.get("keywords") or data.get("keywords")),
            filters=_as_str_list(match.get("filters") or data.get("filters")),
            default=bool(data.get("default", False)),
            enabled=bool(data.get("enabled", True)),
        )

    def to_dict(self) -> Dict[str, Any]:
        data: Dict[str, Any] = {"name": self.name, "text": self.text}
        if self.title and self.title != self.name:
            data["title"] = self.title
        match = {}
        if self.keywords:
            match["keywords"] = list(self.keywords)
        if self.filters:
            match["filters"] = list(self.filters)
        if match:
            data["match"] = match
        if self.default:
            data["default"] = True
        if not self.enabled:
            data["enabled"] = False
        return data


@dataclass
class FilterRules:
    """Локальные правила отсева — применяются к уже найденным вакансиям."""

    title_include: List[str] = field(default_factory=list)
    title_exclude: List[str] = field(default_factory=list)
    text_include: List[str] = field(default_factory=list)
    text_exclude: List[str] = field(default_factory=list)
    employer_exclude: List[str] = field(default_factory=list)
    employer_include: List[str] = field(default_factory=list)
    employer_exclude_ids: List[str] = field(default_factory=list)
    salary_min: Optional[int] = None
    salary_max: Optional[int] = None
    require_salary: bool = False
    skip_with_test: bool = True
    skip_archived: bool = True
    skip_letter_required: bool = False
    skip_already_applied: bool = True
    #: Загружать полное описание вакансии (нужно для text_include/text_exclude).
    fetch_description: Optional[bool] = None
    max_applications: Optional[int] = None

    @classmethod
    def from_dict(cls, data: Optional[Dict[str, Any]], filter_name: str = "") -> "FilterRules":
        data = data or {}
        if not isinstance(data, dict):
            raise ConfigError(f"Фильтр «{filter_name}»: `rules` должен быть словарём")
        unknown = set(data) - {f.name for f in cls.__dataclass_fields__.values()}  # type: ignore[attr-defined]
        if unknown:
            raise ConfigError(
                f"Фильтр «{filter_name}»: неизвестные правила: {', '.join(sorted(unknown))}"
            )
        return cls(
            title_include=_as_str_list(data.get("title_include")),
            title_exclude=_as_str_list(data.get("title_exclude")),
            text_include=_as_str_list(data.get("text_include")),
            text_exclude=_as_str_list(data.get("text_exclude")),
            employer_exclude=_as_str_list(data.get("employer_exclude")),
            employer_include=_as_str_list(data.get("employer_include")),
            employer_exclude_ids=_as_str_list(data.get("employer_exclude_ids")),
            salary_min=_as_optional_int(data.get("salary_min"), "salary_min", filter_name),
            salary_max=_as_optional_int(data.get("salary_max"), "salary_max", filter_name),
            require_salary=bool(data.get("require_salary", False)),
            skip_with_test=bool(data.get("skip_with_test", True)),
            skip_archived=bool(data.get("skip_archived", True)),
            skip_letter_required=bool(data.get("skip_letter_required", False)),
            skip_already_applied=bool(data.get("skip_already_applied", True)),
            fetch_description=data.get("fetch_description"),
            max_applications=_as_optional_int(data.get("max_applications"), "max_applications", filter_name),
        )

    def to_dict(self) -> Dict[str, Any]:
        defaults = FilterRules()
        data: Dict[str, Any] = {}
        for name in self.__dataclass_fields__:  # type: ignore[attr-defined]
            value = getattr(self, name)
            if value != getattr(defaults, name):
                data[name] = value
        return data

    @property
    def needs_description(self) -> bool:
        """Нужно ли догружать полный текст вакансии для этих правил."""
        if self.fetch_description is not None:
            return bool(self.fetch_description)
        return bool(self.text_include or self.text_exclude)


@dataclass
class VacancyFilter:
    """Именованный фильтр: параметры поиска + локальные правила + письмо."""

    name: str
    search: Dict[str, Any] = field(default_factory=dict)
    rules: FilterRules = field(default_factory=FilterRules)
    letter: Optional[str] = None
    resume_id: Optional[str] = None
    enabled: bool = True
    pages: int = 1
    description: str = ""

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "VacancyFilter":
        if not isinstance(data, dict):
            raise ConfigError("Каждый фильтр в `filters` должен быть словарём")
        name = str(data.get("name") or "").strip()
        if not name:
            raise ConfigError("У фильтра отсутствует обязательное поле `name`")
        search = dict(data.get("search") or {})
        if not isinstance(search, dict):
            raise ConfigError(f"Фильтр «{name}»: `search` должен быть словарём")
        unknown = set(search) - KNOWN_SEARCH_PARAMS
        if unknown:
            raise ConfigError(
                f"Фильтр «{name}»: неизвестные параметры поиска: {', '.join(sorted(unknown))}. "
                f"Допустимые: {', '.join(sorted(KNOWN_SEARCH_PARAMS))}"
            )
        pages = int(search.pop("pages", data.get("pages", 1)) or 1)
        if pages < 1:
            raise ConfigError(f"Фильтр «{name}»: `pages` должен быть >= 1")
        return cls(
            name=name,
            search=search,
            rules=FilterRules.from_dict(data.get("rules"), name),
            letter=(str(data["letter"]) if data.get("letter") else None),
            resume_id=(str(data["resume_id"]) if data.get("resume_id") else None),
            enabled=bool(data.get("enabled", True)),
            pages=pages,
            description=str(data.get("description") or ""),
        )

    def to_dict(self) -> Dict[str, Any]:
        data: Dict[str, Any] = {"name": self.name}
        if self.description:
            data["description"] = self.description
        if not self.enabled:
            data["enabled"] = False
        search = dict(self.search)
        if self.pages != 1:
            search["pages"] = self.pages
        if search:
            data["search"] = search
        rules = self.rules.to_dict()
        if rules:
            data["rules"] = rules
        if self.letter:
            data["letter"] = self.letter
        if self.resume_id:
            data["resume_id"] = self.resume_id
        return data


@dataclass
class Settings:
    """Общие настройки откликов."""

    resume_id: Optional[str] = None
    candidate_name: str = ""
    email: str = ""
    user_agent: str = ""
    #: Пауза между откликами, секунды (нижняя и верхняя граница).
    delay_min: float = 4.0
    delay_max: float = 12.0
    max_per_run: int = 25
    max_per_day: int = 190
    database: str = ""
    currency_rates: Dict[str, float] = field(default_factory=dict)
    #: Подтверждать каждый отклик вручную.
    interactive: bool = True
    #: Настройки панели управления в Telegram (token, allowed_users).
    telegram: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: Optional[Dict[str, Any]]) -> "Settings":
        data = data or {}
        if not isinstance(data, dict):
            raise ConfigError("`settings` должен быть словарём")
        delay = data.get("delay_seconds")
        delay_min, delay_max = 4.0, 12.0
        if isinstance(delay, (list, tuple)) and len(delay) == 2:
            delay_min, delay_max = float(delay[0]), float(delay[1])
        elif isinstance(delay, (int, float)):
            delay_min = delay_max = float(delay)
        elif delay is not None:
            raise ConfigError("`settings.delay_seconds` — число или пара [min, max]")
        if delay_min < 0 or delay_max < delay_min:
            raise ConfigError("`settings.delay_seconds`: должно быть 0 <= min <= max")
        email = str(data.get("email") or "")
        return cls(
            resume_id=(str(data["resume_id"]) if data.get("resume_id") else None),
            candidate_name=str(data.get("candidate_name") or ""),
            email=email,
            user_agent=str(data.get("user_agent") or "") or f"hhhelper/1.0 ({email or 'candidate'})",
            delay_min=delay_min,
            delay_max=delay_max,
            max_per_run=int(data.get("max_per_run", 25)),
            max_per_day=int(data.get("max_per_day", 190)),
            database=str(data.get("database") or ""),
            currency_rates={str(k).upper(): float(v) for k, v in (data.get("currency_rates") or {}).items()},
            interactive=bool(data.get("interactive", True)),
            telegram=dict(data.get("telegram") or {}),
        )

    def to_dict(self) -> Dict[str, Any]:
        # resume_id пишем всегда: пустая строка — подсказка, что его нужно заполнить.
        data: Dict[str, Any] = {"resume_id": self.resume_id or ""}
        if self.candidate_name:
            data["candidate_name"] = self.candidate_name
        if self.email:
            data["email"] = self.email
        data["delay_seconds"] = [_compact_number(self.delay_min), _compact_number(self.delay_max)]
        data["max_per_run"] = self.max_per_run
        data["max_per_day"] = self.max_per_day
        data["interactive"] = self.interactive
        if self.database:
            data["database"] = self.database
        if self.currency_rates:
            data["currency_rates"] = self.currency_rates
        if self.telegram:
            data["telegram"] = self.telegram
        return data

    @property
    def database_path(self) -> Path:
        if self.database:
            return Path(self.database).expanduser()
        return config_dir() / "applications.db"


@dataclass
class AppConfig:
    """Полная конфигурация приложения."""

    settings: Settings = field(default_factory=Settings)
    letters: List[Letter] = field(default_factory=list)
    filters: List[VacancyFilter] = field(default_factory=list)
    path: Optional[Path] = None

    @classmethod
    def from_dict(cls, data: Dict[str, Any], path: Optional[Path] = None) -> "AppConfig":
        if not isinstance(data, dict):
            raise ConfigError("Конфигурация должна быть словарём (YAML/JSON объектом)")
        letters = [Letter.from_dict(item) for item in (data.get("letters") or [])]
        filters = [VacancyFilter.from_dict(item) for item in (data.get("filters") or [])]
        _ensure_unique([letter.name for letter in letters], "письма")
        _ensure_unique([flt.name for flt in filters], "фильтра")
        config = cls(
            settings=Settings.from_dict(data.get("settings")),
            letters=letters,
            filters=filters,
            path=path,
        )
        config.validate()
        return config

    def validate(self) -> None:
        """Проверяет перекрёстные ссылки между фильтрами и письмами."""
        letter_names = {letter.name for letter in self.letters}
        for flt in self.filters:
            if flt.letter and flt.letter not in letter_names:
                raise ConfigError(
                    f"Фильтр «{flt.name}» ссылается на несуществующее письмо «{flt.letter}»"
                )
        filter_names = {flt.name for flt in self.filters}
        for letter in self.letters:
            for ref in letter.filters:
                if ref not in filter_names:
                    raise ConfigError(
                        f"Письмо «{letter.name}» ссылается на несуществующий фильтр «{ref}»"
                    )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "settings": self.settings.to_dict(),
            "letters": [letter.to_dict() for letter in self.letters],
            "filters": [flt.to_dict() for flt in self.filters],
        }

    # -- доступ к элементам -------------------------------------------------

    def get_filter(self, name: str) -> VacancyFilter:
        for flt in self.filters:
            if flt.name == name:
                return flt
        known = ", ".join(f.name for f in self.filters) or "нет фильтров"
        raise ConfigError(f"Фильтр «{name}» не найден. Доступные: {known}")

    def get_letter(self, name: str) -> Letter:
        for letter in self.letters:
            if letter.name == name:
                return letter
        known = ", ".join(letter.name for letter in self.letters) or "нет писем"
        raise ConfigError(f"Письмо «{name}» не найдено. Доступные: {known}")

    def enabled_filters(self) -> List[VacancyFilter]:
        return [flt for flt in self.filters if flt.enabled]

    # -- сохранение ---------------------------------------------------------

    def save(self, path: Optional[Path] = None) -> Path:
        target = Path(path or self.path or CONFIG_SEARCH_PATHS[0]).expanduser()
        target.parent.mkdir(parents=True, exist_ok=True)
        data = self.to_dict()
        if target.suffix.lower() == ".json":
            text = json.dumps(data, ensure_ascii=False, indent=2)
        else:
            if yaml is None:
                raise ConfigError("Для сохранения YAML нужен PyYAML: pip install PyYAML")
            text = yaml.dump(
                data, Dumper=_BlockStringDumper, allow_unicode=True, sort_keys=False, width=100
            )
        target.write_text(text, encoding="utf-8")
        self.path = target
        return target


def load_config(path: Optional[str] = None) -> AppConfig:
    """Загружает конфиг по пути или из стандартных мест."""
    candidates = [Path(path).expanduser()] if path else [Path(p).expanduser() for p in CONFIG_SEARCH_PATHS]
    for candidate in candidates:
        if candidate.is_file():
            return AppConfig.from_dict(_read_file(candidate), candidate)
    if path:
        raise ConfigError(f"Файл конфигурации не найден: {path}")
    raise ConfigError(
        "Конфигурация не найдена. Создайте её командой `hhhelper init` "
        f"или положите файл рядом: {', '.join(CONFIG_SEARCH_PATHS[:3])}"
    )


def _compact_number(value: float) -> Any:
    """Целое число вместо 5.0 — так конфиг читается приятнее."""
    return int(value) if float(value).is_integer() else value


if yaml is not None:

    class _BlockStringDumper(yaml.SafeDumper):
        """Пишет многострочные строки блоком (|), а не в кавычках."""

    def _represent_str(dumper: "yaml.SafeDumper", data: str):
        if "\n" in data:
            return dumper.represent_scalar("tag:yaml.org,2002:str", data, style="|")
        return dumper.represent_scalar("tag:yaml.org,2002:str", data)

    _BlockStringDumper.add_representer(str, _represent_str)
else:  # pragma: no cover - окружение без PyYAML
    _BlockStringDumper = None  # type: ignore[assignment]


def _read_file(path: Path) -> Dict[str, Any]:
    text = path.read_text(encoding="utf-8")
    try:
        if path.suffix.lower() == ".json":
            return json.loads(text)
        if yaml is None:
            raise ConfigError(
                f"Для чтения {path.name} нужен PyYAML (pip install PyYAML) "
                "либо используйте конфиг в формате JSON"
            )
        return yaml.safe_load(text) or {}
    except ConfigError:
        raise
    except Exception as exc:  # ошибка синтаксиса YAML/JSON
        raise ConfigError(f"Не удалось разобрать {path}: {exc}") from exc


def _as_str_list(value: Any) -> List[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, (list, tuple, set)):
        return [str(item) for item in value]
    return [str(value)]


def _as_optional_int(value: Any, field_name: str, ctx: str = "") -> Optional[int]:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        where = f"Фильтр «{ctx}»: " if ctx else ""
        raise ConfigError(f"{where}`{field_name}` должен быть числом, получено {value!r}") from exc


def _ensure_unique(names: List[str], what: str) -> None:
    seen = set()
    for name in names:
        if name in seen:
            raise ConfigError(f"Дублируется имя {what}: «{name}»")
        seen.add(name)
