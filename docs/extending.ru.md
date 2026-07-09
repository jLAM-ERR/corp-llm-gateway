# Расширение шлюза

[English](extending.md) · **Русский**

Как добавить возможности в corp-llm-gateway — новый детектор, sink аудита,
экспортер метрик, провайдер или профиль юрисдикции/подразделения.

## Философия — почему in-tree, а не runtime-плагины

Расширения **in-tree и декларативны**: бандл данных (*профиль*), слоями наложенный на ядро, плюс
*закрытый набор* прошедших security-ревью алгоритмов, выбираемых **по имени**. Шлюз никогда не
загружает сторонний исполняемый код на egress-пути.

Это осознанно. Критерий успеха — *ноль подтверждённых инцидентов утечки*, развёртывание
air-gapped, а каждое изменение на пути утечки должно проходить ревью CODEOWNERS и быть аудируемым.
Python `entry_points` / pip-плагины поместили бы произвольный сторонний код между данными
пользователя и upstream-API — дыру в supply-chain и аудите, которую мы не откроем. Поэтому
«расширение» здесь — это **небольшое проверяемое in-tree изменение**, а не runtime-плагин.

## Три стиля расширения

Почти всё, что можно добавить, укладывается в одну из трёх форм. Выберите верный стиль — и механика
следует за ним.

| Стиль | Вы реализуете | Подключаете через | Выбор через |
|---|---|---|---|
| **1. Config-factory backend** | ABC (`Sink`, `MetricsExporter`, `CorpLlmAuthProvider`) | одну запись в фабричном словаре | переменную окружения |
| **2. In-tree name registry** | ABC (`PIIDetector`, provider spec) | одну строку в реестре | **по имени** (в профиле / по модели) |
| **3. Общий `extensions.REGISTRY`** | — | composition root, не вы | только инспекция/health |

Стиль 3 — это *обвязка*: `extensions.REGISTRY` — это keyed-поверхность инспекции/health, которую
наполняет `bootstrap.build_guardrail()` (например, адаптирует активный sink аудита в реестр). Вы
редко вызываете его напрямую — так style-1/2 компоненты становятся видны в `gateway-admin
extensions`, а не так вы их добавляете.

Два особых случая — вне таблицы: **профили** (чистые декларативные данные — см.
[docs/ops/profiles.md](ops/profiles.md)) и **хранилище** (переключатель `REDIS_URL`, не фабрика — см.
[Config-only backends](#config-only-backends)).

---

## Добавить детектор (стиль 2)

Детектор находит участки для маскирования. Реестр — `DETECTOR_REGISTRY` в
`src/corp_llm_gateway/profiles/registry.py`; встроенные — `regex_checksum`, `dual_ner`, `ner_ru`,
`ner_en`.

1. **Реализуйте `PIIDetector`** (`detectors/base.py`) в `src/corp_llm_gateway/detectors/my_rule.py`:

   ```python
   from corp_llm_gateway.detectors.base import Finding, PIIDetector

   class MyRuleDetector(PIIDetector):
       async def detect(self, text: str) -> list[Finding]:
           # верните по одному Finding(text, label, start, end, score) на совпадение
           ...
   ```

2. **Ре-экспортируйте** его в `detectors/__init__.py` `__all__` (конвенция репозитория — ABC + impls
   ре-экспортируются из `__init__` каждого пакета).

3. **Зарегистрируйте по имени** — одна строка в `DETECTOR_REGISTRY` (`profiles/registry.py`);
   значения — фабрики `lambda cfg: Detector()`:

   ```python
   "my_rule": lambda cfg: MyRuleDetector(),
   ```

4. **Добавьте контракт-тест** в `tests/detectors/` (шаблон — в существующих тестах детекторов).

5. **Выберите по имени** в `profile.toml` профиля — `detectors = ["regex_checksum", "my_rule"]` —
   затем **пере-запечатайте** бандл (его `content_hash` изменился):
   `python -m corp_llm_gateway.profiles.seal src/corp_llm_gateway/profiles/defaults`.

`build_detectors(names, cfg)` собирает выбранный набор; неизвестное имя — жёсткая ошибка.

## Добавить sink аудита (стиль 1)

Sink — это место, куда идут записи аудита. Выбор config-only через `CORP_AUDIT_SINK`.

1. **Реализуйте `Sink`** (`audit/sinks.py`): `async def write(self, record: dict[str, Any]) -> None`.
2. **Добавьте фабрику + имя** в `audit/factory.py`: запись `_make_<name>()` в `_SINK_FACTORIES` **и**
   запись тип→имя в `_SINK_NAMES` (обратная карта держит имя зарегистрированного расширения
   совпадающим с живым объектом).
3. **Выберите** через `CORP_AUDIT_SINK=<name>` (по умолчанию `stdout`; встроенные
   `stdout`/`langfuse`/`list`).

Вы **ничего** не регистрируете сами: `get_sink()` собирает выбранный sink, а composition root
адаптирует его в `extensions.REGISTRY` через `register_sink(REGISTRY, sink, name)`
(`bootstrap.py:250`), чтобы он появился в `gateway-admin extensions`. Гейт NEVER-полей оборачивает
каждый sink в любом случае.

## Добавить экспортер метрик (стиль 1)

1. **Реализуйте `MetricsExporter`** (`metrics/base.py`): `record_block(block_reason)`,
   `record_failure(component)`, `observe_request_latency(seconds, *, status)`, плюс `render()` /
   `content_type()` для scrape-эндпоинта.
2. **Добавьте запись фабрики** в `metrics/__init__.py` `_EXPORTER_FACTORIES` (встроенные `noop`,
   `prometheus`).
3. **Выберите** через `CORP_METRICS_EXPORTER=<name>` (по умолчанию `noop`); собирается
   `get_exporter()`.

## Добавить провайдера (стиль 2)

Провайдеры — это egress-цели. Они используют **собственный** `ProviderRegistry`
(`providers/registry.py`), не `extensions.REGISTRY`. v1 намеренно ограничен:
`V1_ALLOWED = {anthropic, openai, corp-vllm}`, и CLAUDE.md запрещает не-OpenAI/Anthropic-провайдера в
v1 (Bedrock / Gemini / Azure — явный v2).

- Встроенные объявлены как `ProviderSpec` (добавляет `role`, `wire_format`, `health_url`) в
  `register_builtins()`; маршрутизация выбирает `anthropic` vs `openai` по имени модели.
- v2-провайдер остаётся за гейтом `CORP_ALLOW_V2_PROVIDERS=1` — не убирайте этот гейт, чтобы его
  выпустить.

## Общий реестр расширений (стиль 3 — обвязка)

`extensions.REGISTRY` (`extensions/registry.py`) — это keyed-поверхность инспекции/health, а не точка
входа для контрибьютора. Его примитивы:

- `register(spec, factory, *, replace=False)` — `factory: Callable[[], Extension]`. Дубликат
  `(kind, name)` **fail-closed** (бросает), если не `replace=True`, — чтобы поздняя регистрация не
  могла молча затенить NEVER-gated sink или детектор на egress-пути.
- `validate_api_version(EXTENSION_API_VERSION)` — каждый `ExtensionSpec.api_version` должен равняться
  ядровому (`EXTENSION_API_VERSION = "1"`), иначе загрузка fail-closed.
- `ExtensionSpec(name, kind, version, api_version, capabilities=frozenset(), fail_policy="fail-closed")`
  — обратите внимание, `version` **обязателен**; `fail_policy` по умолчанию fail-closed.

7 `ExtensionKind`: `audit_sink, metrics, tracing, provider, detector, rules, payload_policy`.
Оговорки: `detector` обслуживается отдельным `DETECTOR_REGISTRY` (выше), а
`tracing` / `rules` / `payload_policy` объявлены, но ещё не подключены (нет фабрики/impl) — не
опирайтесь на них пока.

Посмотреть, что активно:

```bash
gateway-admin extensions list      # зарегистрированные пары (kind:name)
gateway-admin extensions inspect   # спеки
gateway-admin extensions health    # health() по каждому расширению
```

`gateway-admin extensions enable|disable` — RBAC-gated **заглушки** (им нужно хранилище состояния
расширений — follow-up); состояние они пока не меняют.

## Составить бандл профиля

Профиль — декларативная половина: способ варьировать детекцию/политику по **стране / подразделению /
регуляторному режиму** без правки ядра. Бандл — это `profile.toml` (манифест: `extends`, `detectors`,
`[policy]`) плюс опциональные `replace.md` / `*.txt` файлы терминов, hash-запечатанный и наслаиваемый
монотонно-ужесточающим `PolicyKnobs.merge` (композиция только *добавляет* маскирование). Команда
выбирает профили через `TeamConfig.profile_ids`.

Полное руководство — раскладка бандла, композиция/приоритет, запечатывание, выбор:
**[docs/ops/profiles.md](ops/profiles.md)**.

## Config-only backends

Без правки кода — переключите переменную окружения / значение Helm:

- **Провайдер аутентификации** — `get_auth_provider()` + `_PROVIDER_FACTORIES` (`auth/factory.py`),
  выбор через `CORP_LLM_AUTH_PROVIDER` (по умолчанию `noop`; `bearer`/`mtls`/`oidc`).
- **Хранилище** — исключение: `MappingStore` (`storage/mapping.py`) — это ABC **без фабричного
  словаря**. `bootstrap.build_mapping_store()` выбирает `RedisMappingStore`, когда задан `REDIS_URL`,
  иначе `InMemoryMappingStore`. Новый бэкенд правит эту функцию — селектора по имени нет.

## Правила безопасности, которые обеспечивает код

Каждый seam выше построен fail-safe:

- **Fail-closed регистрация** — дубликат `(kind, name)` бросает; молчаливой перезаписи нет.
- **Гейт api-version** — несовпадающий `api_version` роняет загрузку, а не молча деградирует.
- **Fail-closed по умолчанию** — `ExtensionSpec.fail_policy` по умолчанию `fail-closed`; неизвестные
  имена детектора / sink / провайдера — жёсткие ошибки, никогда не no-op.
- **Hash-запечатанные бандлы** — правка запечатанного профиля требует пере-запечатывания;
  подделанный бандл ловится fail-closed на загрузке.
- **Никакого стороннего runtime-кода** на egress-пути — алгоритмы in-tree и именованы.

## Governance

CODEOWNERS делит ревью по радиусу поражения: `profiles/**` (бандлы данных) → compliance;
`detectors/**` + реестры (`profiles/registry.py`, `extensions/`, `providers/`) → security-eng.
Новый алгоритм на пути утечки — это security-ревью; новый бандл юрисдикции — compliance-ревью.
