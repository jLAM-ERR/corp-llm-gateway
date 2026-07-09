# corp-llm-gateway

[English](README.md) · **Русский**

Корпоративный LLM-шлюз. Санитизирует трафик между экземплярами Claude Code у разработчиков и Anthropic / OpenAI до того, как он покинет корпоративный периметр.

## Статус

**Готов к GA.** Реализованы: local-first каскад детекции, полный проход по укреплению безопасности (11 исправлений поверхностей утечки, каждое repro-first — oversize, fail-open NER, `tool_calls` OpenAI, покрытие сегментатора, срезание заголовков, dev-прокси, TLS/RBAC), слой **profile-плагинов** по стране / подразделению / регуляторному режиму (декларативные бандлы + in-tree реестр детекторов + изоляция кэша между юрисдикциями) и эксплуатационные поверхности (composition root, реальный `gateway-admin`, production Helm-чарт, подключаемые метрики, обслуживаемый healthz, ops-документация). Некомпромиссный критерий успеха: **ноль подтверждённых инцидентов утечки** за 90 дней после GA.

## Оглавление

- [Обзор](#обзор)
- [Возможности](#возможности)
- [Архитектура](#архитектура)
- [Структура репозитория](#структура-репозитория)
- [Быстрый старт для разработчика (ноутбук)](#быстрый-старт-для-разработчика-ноутбук)
- [Быстрый старт для оператора (k8s)](#быстрый-старт-для-оператора-k8s)
- [Правила команды (`replace.md`)](#правила-команды-replacemd)
- [Идентификация и поток токена](#идентификация-и-поток-токена)
- [Расширение шлюза](#расширение-шлюза)
- [Разработка](#разработка)
- [На чём построено](#на-чём-построено)

## Обзор

Харнесс на ноутбуке (Claude Code, Codex, Cursor) общается по HTTP с `gateway.corp.lan`. Шлюз — это прокси LiteLLM с кастомным guardrail (`corp_llm_gateway.litellm_hook.CorpLlmGuardrail`), зарегистрированным как callback. Каждый запрос санитизируется в `pre_call`, форвардится в Anthropic / OpenAI с сохранённым BYOK-ключом разработчика, де-санитизируется в `post_call` и аудируется. В сетевом трафике важны два заголовка:

| Заголовок | Источник | Назначение |
|---|---|---|
| `X-Corp-Auth` | `~/.corp-llm-gateway/token` (ноутбук) | корп-идентичность / определение команды; **срезается** перед egress |
| `Authorization: Bearer …` | Anthropic / OpenAI-ключ разработчика | passthrough BYOK; форвардится **без изменений** |

## Возможности

### Детекция

- **Чек-суммы российских сущностей** — ИНН (10/12), КПП, ОГРН (13/15), БИК, СНИЛС, р/счёт с валидируемыми по алгоритму чек-суммами; почти нулевой уровень ложных срабатываний
- **Двуязычный NER** — Natasha/Slovnet RU + spaCy `en_core_web_md` EN, run-both-union; покрывает ФИО, организации, адреса в смешанных по языку запросах
- **Лемма-газеттир** — кодовые имена продуктов, регулируемые термины ПОД-ФТ / AML-CFT, грифы конфиденциальности (`Коммерческая тайна`, `ДСП`, `Confidential`, `NDA`), сопоставляемые по лемме, а не по точной строке
- **Сплиттер идентификаторов кода** — разбивает camel/snake-идентификаторы (`CompanynameabcService`) и сканирует сегменты по газеттиру
- **Allowlist тестовых данных** — детерминированное исключение для тестовых фикстур; не может подавить настоящие секреты
- **Паттерны секретов** — JWT, приватный ключ PEM, значения `sk-` / `AKIA` / `ghp_` / обобщённый `password=` / `Bearer`

### Блокировка

- **Блокировка до egress (Stage 0)** — сигнатуры `.env`, kubeconfig, nginx.conf, лог-дампов → HTTP 422 с `block_reason`; upstream не вызывается
- **Stage 5 DLP egress guard** — независимый пере-скан вторым слоем санитизированного payload на canary-строки и высоконадёжные секреты; блокирует всё, что уцелело

### Аутентификация и соответствие требованиям

- **X-Corp-Auth + хранилище токенов на Postgres** — `AuthMiddleware` валидирует токены против `PostgresTokenStore` (asyncpg); верхняя граница распространения отзыва — 60 s
- **RBAC `gateway:operator`** — команды admin CLI закрыты гейтом по JWT-claim `gateway:operator`; проверяется через PyJWT против ролей realm в Keycloak
- **Конвейер аудита** — богатая схема `AuditEvent` (уровни полей ALWAYS / CONDITIONAL) + гейт NEVER-полей: логгер отклоняет записи, содержащие `mapping`, `original` или `credentials`
- **SIEM-sink** — HTTP-sink Vector с унаследованным NEVER-гейтом + Helm-алерты (`AuditVectorDropHigh`, `LeakAttemptDetected`)
- **Блокировка egress** — `NetworkPolicy` (egress подов ограничен upstream + корп-CIDR) + CoreDNS-sinkhole (блокирует прямое разрешение `api.anthropic.com` / `api.openai.com` из кластера), обе включены в `values-prod.yaml`

Детекция покрывает набор корп-требований ИБ: чек-суммы структурных сущностей, газеттиры помеченной конфиденциальности и ПОД-ФТ, паттерны секретов, блокировки egress для конфигов/логов. Разделение Tier-1 (детерминированный) и Tier-2 (best-effort оракул) описано в [`docs/security.md`](docs/security.ru.md).

## Архитектура

**Архитектура B — сборка из лучших в своём классе.** Единственный кастомный Python-guardrail (`CorpLlmGuardrail`), встроенный в прокси LiteLLM; аудит, аутентификация и наблюдаемость — на эксплуатируемом open-source, а не написаны внутри. Каждый запрос проходит детерминированный local-first каскад (~6 ms p50 на CPU) — классификатор payload → правила `replace.md` → regex+checksum → dual-NER → лемма-газеттир → сплиттер кода — при этом корп-vLLM-оракул вызывается только по попаданию в газеттир, затем DLP egress guard перед upstream.

**→ Полная диаграмма и жизненный цикл запроса: [`docs/architecture.md`](docs/architecture.ru.md).**

## Структура репозитория

```
src/corp_llm_gateway/   Python-guardrail (кастомные хуки LiteLLM + движок санитайзера)
  auth/                 провайдер аутентификации corp-LLM (по умолчанию Noop; Bearer/mTLS/OIDC) + фабрика
  audit/                AuditEvent + Logger + Sinks + фабрика + генератор retention + гейт NEVER-полей
  bootstrap.py          production composition root — build_guardrail() из конфига; ленивый синглтон `guardrail`
  cli/                  gateway-admin (team/token/extensions/config check), corp-llm-gateway status, proxy
  config.py/settings.py загрузчик конфига (env→файл→default) + типизированный реестр single-source-of-truth + validate()
  corp_llm/             httpx-клиент, говорящий с vLLM /v1/chat/completions
  detectors/            PIIDetector + RegexChecksumDetector + DualNerDetector (RU+EN); fail-closed при отсутствии NER
  extensions/           ExtensionRegistry (виды audit-sink / provider / detector / …); fail-closed register + гейт api-version
  healthz/              проверки live / ready / sanitization / extensions + ASGI-сервер (build_health_router)
  metrics/              подключаемый экспортер (noop / prometheus) — blocked_requests_total + gateway_failure
  payload/              порог размера + gzip + квота на команду + политика oversize
  profiles/             плагин-бандлы: ProfileBundle/PolicyKnobs + resolver + DETECTOR_REGISTRY + hash-integrity + defaults/
  providers/            ProviderRegistry + исполняемый v1-guard (anthropic / openai / corp-vllm)
  rules/                парсер replace.md + газеттир + кэширующий загрузчик файлов
  sanitizer/            local-first движок + сегментатор + StreamingDesanitizer + DLP guard + оркестратор + ProfileAwareOrchestrator
  storage/              MappingStore (in-memory + Redis)
  team_config/          TeamConfig (+ profile_ids) + хранилище (in-memory + Postgres) + schema.sql
  tokens/               schema.sql + AuthMiddleware + TokenIssuer + хранилища
  litellm_hook.py       CorpLlmGuardrail — адаптер callback-ов LiteLLM (вкл. OpenAI tool_calls + streaming)
helm/corp-llm-gateway/  Helm-чарт (образ шлюза + callback guardrail, Secret, HPA/PDB/SA, ServiceMonitor, config-check initContainer, NetworkPolicy, CoreDNS sinkhole)
docs/                   architecture + security + audit-schema + ops/* (install/configuration/admin-cli/upgrade/profiles/runbook/capacity) + rbac-matrix + harness-integration + x-corp-auth
scripts/install.sh      установщик для ноутбука (bash/zsh/fish, macOS/Linux)
tests/                  pytest, pytest-asyncio mode=auto (~1392 passed / 91 skipped; 3.14 грациозный NER, полный на 3.12/CI)
```

## Быстрый старт для разработчика (ноутбук)

### Установка

```bash
curl -fsSL https://raw.githubusercontent.com/jLAM-ERR/corp-llm-gateway/main/scripts/install.sh | bash
```

Что он делает ([`scripts/install.sh`](scripts/install.sh)):

1. Определяет shell (bash / zsh / fish), пишет `ANTHROPIC_BASE_URL`, `OPENAI_BASE_URL`, `CORP_GATEWAY_TOKEN_FILE` и (для Claude Code) `ANTHROPIC_CUSTOM_HEADERS` в ваш rc-файл между маркерами `# >>> corp-llm-gateway >>>`.
2. Выполняет OAuth device-flow через Keycloak и пишет 30-дневный корп-токен в `~/.corp-llm-gateway/token` (`0600`).
3. Прогоняет smoke-тест шлюза строкой, подлежащей маскированию, и проверяет round-trip.

Повторный запуск установщика идемпотентен — он ротирует токен и перезаписывает rc-блок.

Опциональный диагностический CLI `corp-llm-gateway` (используется в *Проверке* ниже) ставится из репозитория:

```bash
pipx install "git+https://github.com/jLAM-ERR/corp-llm-gateway.git"   # или: pip install "git+https://…"
```

### Проверка

```bash
exec $SHELL -l           # подхватить новое окружение
corp-llm-gateway status  # → token_present=yes, live=yes, healthy=yes
```

### Повседневное использование

Три паттерна интеграции в зависимости от вашего харнесса — полные рецепты в [`docs/harness-integration.md`](docs/harness-integration.ru.md):

| Харнесс | Рекомендуется | Резервный вариант |
|---|---|---|
| Claude Code | переменная окружения (`ANTHROPIC_CUSTOM_HEADERS`, задаётся `install.sh`) | localhost-прокси |
| Codex CLI | `~/.codex/config.toml` `[default.headers]` | localhost-прокси |
| Cursor / Continue | поле кастомных заголовков в настройках приложения | localhost-прокси |
| `curl`, сырые скрипты | `--header 'X-Corp-Auth: …'` | localhost-прокси |

Localhost-прокси (Паттерн 3, `corp-llm-gateway-proxy`) универсален — он инъецирует `X-Corp-Auth` в каждый запрос и перечитывает файл токена при каждом вызове, поэтому ротация токена вступает в силу немедленно:

```bash
corp-llm-gateway-proxy --listen 127.0.0.1:9999 --upstream https://gateway.corp.lan
export ANTHROPIC_BASE_URL='http://127.0.0.1:9999'
export OPENAI_BASE_URL='http://127.0.0.1:9999/v1'
```

### Ротация токена

Токены истекают каждые 30 дней. При настройке по умолчанию (Паттерн 1) значение читается с диска **один раз при старте shell** (снимок `$(cat …)`) — поэтому после ротации:

- **Паттерн 1 / 2:** откройте новый shell (или перезапустите харнесс).
- **Паттерн 3 (прокси):** ничего — следующий запрос автоматически подхватит новый токен.

Чтобы ротировать вручную до истечения срока, повторно запустите `install.sh`.

### Попробовать демо

Параллельный демо-стек показывает полный round-trip — маскирование, конвейер аудита, подсвеченный в Langfuse, fail-closed-поведение — на вашем ноутбуке: `scripts/demo.sh up` (наблюдать поток можно через `scripts/demo.sh logs`). Настройка, набор промптов и разбор проблем: [`docs/demo.md`](docs/demo.ru.md).

## Быстрый старт для оператора (k8s)

### Что разворачивается

Helm-чарт ([`helm/corp-llm-gateway/`](helm/corp-llm-gateway/)) поставляет:

| Нагрузка | Контейнер(ы) | Назначение |
|---|---|---|
| `Deployment/gateway` | `litellm` (прокси + guardrail) + `vector` (sidecar конвейера аудита) | путь запроса + egress аудита |
| `Service/gateway` | — | ClusterIP перед deployment |
| `Ingress/gateway` | — | терминация TLS на `ingress.host` (по умолчанию `gateway.corp.lan`) |
| `ConfigMap/*-vector` | — | конвейер Vector + VRL-фильтр NEVER-полей |
| `NetworkPolicy` (опционально) | — | ограничивает egress до upstream + корп-внутренних CIDR |
| CoreDNS sinkhole (опционально) | — | блокирует прямое разрешение `api.anthropic.com` / `api.openai.com` из кластера |

Внешние зависимости (не провижинятся чартом): кластер Redis, Postgres, endpoint корп-vLLM, sink-и Vector (Langfuse / S3 / SIEM).

### Установка / обновление

```bash
# staging
helm upgrade --install gw helm/corp-llm-gateway \
  -f values-staging.yaml --version v0.x.y -n corp-llm-gateway

# дождаться готовности всех реплик
kubectl -n corp-llm-gateway rollout status deploy/gateway

# глубокая проверка sanitization, затем промоут в prod с values-prod.yaml
curl https://gateway-staging.corp.lan/healthz/sanitization
```

Откат: `helm rollback gw <revision>` (Helm хранит последние 10). Полный процесс релиза: [`docs/ops/upgrade.md`](docs/ops/upgrade.md).

### Проверки состояния

| Endpoint | Используется | Проверяет |
|---|---|---|
| `/healthz/live` | k8s livenessProbe | процесс жив |
| `/healthz/ready` | k8s readinessProbe | зависимости (Redis, Postgres, corp-LLM) доступны |
| `/healthz/sanitization` | smoke-тест после деплоя | сквозной round-trip pre→post со строкой, подлежащей маскированию |

### Конфигурация (значения Helm)

Значения по умолчанию — в [`helm/corp-llm-gateway/values.yaml`](helm/corp-llm-gateway/values.yaml). Наиболее часто используемые ключи:

| Ключ | По умолчанию | Что контролирует |
|---|---|---|
| `replicaCount` | `3` | поды gateway (3 = удобно для кворума redis) |
| `litellm.versionPin` | `1.40` | тег образа LiteLLM — поднимать только после гейта обновления на staging |
| `corpLlm.endpoint` | `""` | URL корп-vLLM, обеспечивающего оракул редактирования в пред-пассе |
| `corpLlm.authProvider` | `"noop"` | переключить на реальный провайдер, когда у corp-LLM появится аутентификация (config-only, без изменений кода) |
| `guardrail.contentSizeThresholdBytes` | `102400` | порог пропуска слишком больших payload (M1-11) |
| `guardrail.cacheA.ttlSeconds` | `36000` | TTL дедупликации по содержимому |
| `guardrail.cacheB.slidingTtlSeconds` | `3600` | TTL per-conversation маппинга (скользящий) |
| `audit.sinks.{langfuse,s3,siem}.enabled` | все `true` | включение отдельных sink-ов аудита |
| `token.ttlDays` / `token.revocationCacheSeconds` | `30` / `60` | срок действия корп-токена / верхняя граница распространения отзыва |
| `failPolicy.*` | см. файл | поведение fail-closed / continue по каждому компоненту (матрица M4) — **источник истины**, никаких ad-hoc fail-open путей в коде |
| `coreDnsSinkhole.enabled` / `networkPolicy.enabled` | `false` | блокировка egress (включены в `values-prod.yaml`) |

У каждого значения есть резервный property-файл TOML (`$CORP_LLM_GATEWAY_CONFIG_FILE` → `~/.corp-llm-gateway/config.toml` → `/etc/corp-llm-gateway/config.toml`, разрешается после переменных окружения). Полный справочник ключей: [`docs/ops/configuration.md`](docs/ops/configuration.md); шаблон: [`config.example.toml`](config.example.toml).

### Admin CLI (`gateway-admin`)

CLI оператора, обычно запускается через `kubectl exec` против развёртывания. Закрыт гейтом по JWT-claim `gateway:operator`.

| Группа команд | Назначение |
|---|---|
| `gateway-admin team …` | создание / обновление / список команд + конфиг retention |
| `gateway-admin token …` | выпуск / отзыв / список корп-токенов |
| `gateway-admin extensions …` | список / инспекция / health / включение зарегистрированных расширений |
| `gateway-admin config check` | валидация разрешённого конфига против типизированного реестра настроек |

Полный справочник: [`docs/ops/admin-cli.md`](docs/ops/admin-cli.md).

### Day-2 эксплуатация

Текущая эксплуатация после установки — плейбук инцидентов, матрица fail-policy, масштабирование и рутинные admin-задачи — в runbook: [`docs/ops/runbook.md`](docs/ops/runbook.ru.md). Расчёт мощностей по фазам раскатки (alpha → GA при 1000 разработчиков / 50 RPS суммарно): [`docs/ops/capacity.md`](docs/ops/capacity.ru.md).

## Правила команды (`replace.md`)

Каждая команда ведёт файл `replace.md` по пути `<rules-dir>/<team_id>.md`. Эти правила выполняются **первыми** в локальном каскаде и **переопределяют** авто-детекцию — указанный здесь термин заменяется всегда, независимо от того, что нашли детекторы.

Формат — одно правило на строку, разделитель `=` (легаси `→` U+2192 по-прежнему принимается); правила применяются длиннейшими вперёд (инвариант #5). Оборачивайте в кавычки любое значение, содержащее `=`:

```markdown
- `Project Polaris` = `[CONFIDENTIAL_PROJECT]`
- `acme-internal-crm.corp.lan` = `[INTERNAL_HOST]`
- `dr.smith@partnerlab.com` = `[PARTNER_CONTACT]`
```

Полная спецификация и советы по написанию: [`docs/replace-md-authoring.md`](docs/replace-md-authoring.ru.md).

## Идентификация и поток токена

**Токен `X-Corp-Auth`** — корп-токен лежит на диске по пути `~/.corp-llm-gateway/token` (выпускается `install.sh` через device flow Keycloak, TTL 30 дней, `0600`). Отправляется в каждом запросе для определения идентичности/команды и **срезается перед egress** — никогда не форвардится upstream и не логируется. Значение читается один раз при инициализации shell/харнесса, кроме Паттерна 3 (прокси), который перечитывает его на каждый запрос. Полный жизненный цикл (хранение, свежесть по паттернам, режимы отказа): [`docs/x-corp-auth.md`](docs/x-corp-auth.ru.md).

**Идентификация диалога** — шлюз выпускает `conversation_id` на каждый HTTP-запрос (равен UUID запроса). Cache A (дедуп по содержимому) работает; Cache B (per-conversation маппинг) пишется, но пока не переиспользуется между родственными запросами, потому что ни один харнесс не поставляет стабильный session ID. Поведение и как подключить настоящий session ID: [`docs/conversation-id.md`](docs/conversation-id.ru.md).

Кто что может (разработчики / тимлиды / операторы / безопасность): [`docs/rbac-matrix.md`](docs/rbac-matrix.ru.md).

## Расширение шлюза

Расширения **in-tree и декларативны** — бандл данных (профиль), слоями наложенный на ядро, плюс закрытый набор прошедших security-ревью алгоритмов, выбираемых **по имени**. Шлюз никогда не загружает сторонний код на egress-пути (air-gapped, ревью CODEOWNERS, hash-запечатано, fail-closed), поэтому добавление возможности — небольшое проверяемое изменение, а не runtime-плагин.

| Расширение | Стиль | Что вы добавляете |
|---|---|---|
| **Детектор** | in-tree name registry | `detectors/<name>.py` (`PIIDetector`) + одна строка `DETECTOR_REGISTRY` + выбор по имени в профиле |
| **Провайдер** | in-tree name registry | `ProviderSpec` в `register_builtins` (v1 = anthropic/openai/corp-vllm; v2 за `CORP_ALLOW_V2_PROVIDERS`) |
| **Sink аудита / метрики** | config factory | реализация ABC + одна запись в фабричном словаре; выбор через `CORP_AUDIT_SINK` / `CORP_METRICS_EXPORTER` |
| **Провайдер аутентификации** | config factory | запись в `_PROVIDER_FACTORIES`; выбор через `CORP_LLM_AUTH_PROVIDER` |
| **Бандл профиля** (страна / подразделение / режим) | декларативные данные | составить `profile.toml` + файлы терминов, пере-запечатать — см. [`docs/ops/profiles.md`](docs/ops/profiles.md) |

Пример — **добавить детектор**: (1) `src/corp_llm_gateway/detectors/my_rule.py`, реализующий `PIIDetector` (`async detect(text) -> list[Finding]`); (2) ре-экспорт в `detectors/__init__.py`; (3) одна строка в `DETECTOR_REGISTRY` (`profiles/registry.py`); (4) контракт-тест в `tests/detectors/`; (5) выбрать в `detectors = [...]` профиля и пере-запечатать.

Полное руководство по каждому seam (sinks, провайдеры, реестр расширений, правила безопасности, CODEOWNERS): [`docs/extending.md`](docs/extending.ru.md).

## Разработка

Требует Python 3.12+.

```bash
pip install -e ".[dev]"
pre-commit install
PYTHONPATH=src .venv/bin/pytest tests/ -q     # ~1392 passed / 91 skipped, ~23с (3.14 грациозный NER; полный NER + RS256 crypto на 3.12/CI)
PYTHONPATH=src .venv/bin/ruff check src tests
```

Соглашения, инварианты и «чего НЕ делать» закреплены в [`CLAUDE.md`](CLAUDE.md). CI — GitHub Actions (`.github/workflows/`).

## На чём построено

Open-source-компоненты, из которых собран шлюз (Архитектура B — лучшие в своём классе):

- **Прокси и serving** — [LiteLLM](https://github.com/BerriAI/litellm) (мультипровайдерный прокси + guardrail-хуки) · [vLLM](https://github.com/vllm-project/vllm) (бэкенд корп-оракула пред-пасса)
- **Двуязычный NER и морфология** — RU: [Natasha](https://github.com/natasha/natasha) · [Slovnet](https://github.com/natasha/slovnet) · [Navec](https://github.com/natasha/navec) · [Razdel](https://github.com/natasha/razdel) · [pymorphy3](https://pypi.org/project/pymorphy3/); EN: [spaCy](https://spacy.io) + [`en_core_web_md`](https://spacy.io/models/en). Альтернативы ([Presidio](https://github.com/microsoft/presidio), [DeepPavlov](https://github.com/deeppavlov/DeepPavlov)) рассмотрены и отклонены из-за латентности на CPU
- **Состояние и хранилища** — [Redis](https://redis.io) (кэши маппинга / дедупа) · [PostgreSQL](https://www.postgresql.org) через [asyncpg](https://github.com/MagicStack/asyncpg) (хранилище токенов)
- **Аудит и наблюдаемость** — [Vector](https://vector.dev) → [Langfuse](https://langfuse.com) + S3 + SIEM
- **Доставка и клиенты** — [Helm](https://helm.sh) (чарт) · [CoreDNS](https://coredns.io) (egress-sinkhole) · [httpx](https://www.python-httpx.org) (клиент корп-LLM)
