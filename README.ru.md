# corp-llm-gateway

[English](README.md) · **Русский**

Корпоративный LLM-шлюз. Санитизирует трафик между экземплярами Claude Code у разработчиков и Anthropic / OpenAI до того, как он покинет корпоративный периметр.

## Статус

**Готов к GA.** Реализованы: local-first каскад детекции, полный проход по укреплению безопасности (11 исправлений поверхностей утечки, каждое repro-first — oversize, fail-open NER, `tool_calls` OpenAI, покрытие сегментатора, срезание заголовков, dev-прокси, TLS/RBAC), слой **profile-плагинов** по стране / подразделению / регуляторному режиму (декларативные бандлы + in-tree реестр детекторов + изоляция кэша между юрисдикциями) и эксплуатационные поверхности (composition root, реальный `gateway-admin`, production Helm-чарт, подключаемые метрики, обслуживаемый healthz, ops-документация). Некомпромиссный критерий успеха: **ноль подтверждённых инцидентов утечки** за 90 дней после GA.

**Новое:** **production-развёртывание на docker compose** для хостов без Kubernetes ([`compose/`](compose/)) — полный data plane плюс self-hosted Langfuse v3 и конвейер аудита на Vector, два взаимоисключающих режима аутентификации (корпоративные API-ключи или проброс **подписки** самого разработчика через OAuth), скрипты подготовки сервера и развёртывания в одну команду, а также опциональный детектор на **сервисе корп-NER**. Начинать здесь: [`docs/ops/deployment-modes.ru.md`](docs/ops/deployment-modes.ru.md).

## Оглавление

- [Обзор](#обзор)
- [Возможности](#возможности)
- [Архитектура](#архитектура)
- [Структура репозитория](#структура-репозитория)
- [Быстрый старт для разработчика (ноутбук)](#быстрый-старт-для-разработчика-ноутбук)
- [Куда можно развернуть](#куда-можно-развернуть)
- [Запуск на сервере (docker compose)](#запуск-на-сервере-docker-compose)
- [Запуск локально (docker compose)](#запуск-локально-docker-compose)
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
- **Сервис корп-NER** (опционально, выключен) — удалённый NER-детектор, добавляемый в локальный каскад (`CORP_NER_ENABLED` + `CORP_NER_ENDPOINT`). Ходит по сети, поэтому это единственный детектор, исключённый из сегментов `CODE` — иначе исходники ушли бы во внешний сервис; локальные детекторы код сканировать продолжают. Включён без endpoint'а — отказ на старте, а не тихий пропуск

### Блокировка

- **Блокировка до egress (Stage 0)** — сигнатуры `.env`, kubeconfig, nginx.conf, лог-дампов → HTTP 422 с `block_reason`; upstream не вызывается
- **Stage 5 DLP egress guard** — независимый пере-скан вторым слоем санитизированного payload на canary-строки и высоконадёжные секреты; блокирует всё, что уцелело

### Аутентификация и соответствие требованиям

- **X-Corp-Auth + хранилище токенов на Postgres** — `AuthMiddleware` валидирует токены против `PostgresTokenStore` (asyncpg); верхняя граница распространения отзыва — 60 s
- **Два режима upstream-креденшела** — корпоративные API-ключи (у разработчика персональный виртуальный ключ LiteLLM: есть отзыв и учёт расходов) либо **проброс подписки**, когда OAuth-токен подписки самого разработчика уходит наверх без изменений, а корпоративного `ANTHROPIC_API_KEY` не существует вовсе. Режимы взаимоисключающие, выбираются при развёртывании; санитизация, идентичность команды и аудит в обоих одинаковы — [`docs/ops/deployment-modes.ru.md`](docs/ops/deployment-modes.ru.md)
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
  corp_ner/             httpx-клиент + фабрика для опционального удалённого сервиса корп-NER (/v1/analyze)
  detectors/            PIIDetector + RegexChecksumDetector + DualNerDetector (RU+EN) + CorpNerDetector; fail-closed при отсутствии NER
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
compose/                production-развёртывание на одном хосте — data plane (litellm + redis + postgres) +
                        self-hosted Langfuse v3 + конвейер аудита на Vector; оверлеи docker-compose.oauth.yml
                        (режим подписки) и docker-compose.build.yml (сборка из исходников)
examples/compose/       лёгкий локальный санитизирующий прокси (один контейнер, оракул выключен) — не боевой вариант
docs/                   architecture + security + audit-schema + ops/* (install/configuration/admin-cli/deployment-modes/deploy-handoff/upgrade/profiles/runbook/capacity) + rbac-matrix + harness-integration + x-corp-auth
scripts/install.sh      установщик для ноутбука (bash/zsh/fish, macOS/Linux)
scripts/deploy/         подготовка сервера (bootstrap-server.sh + systemd-юнит) + deploy.sh (развёртывание/обновление хоста)
tests/                  pytest, pytest-asyncio mode=auto (~2274 passed / 107 skipped; 3.14 грациозный NER, полный на 3.12/CI)
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

Для работы Codex через подписку ChatGPT используйте отдельный Responses-профиль:
[`docs/chatgpt-codex.ru.md`](docs/chatgpt-codex.ru.md).

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

## Куда можно развернуть

Шлюз в этом репозитории запускается четырьмя способами. Боевых из них — два первых.

| Вариант | Где | Когда |
|---|---|---|
| **Один хост, docker compose** | [`compose/`](compose/) | production на хосте без Kubernetes — полный data plane + self-hosted Langfuse + конвейер аудита |
| **Kubernetes** | [`helm/corp-llm-gateway/`](helm/corp-llm-gateway/) | production в кластере — см. [Быстрый старт для оператора](#быстрый-старт-для-оператора-k8s) |
| Локальный санитизирующий прокси | [`examples/compose/`](examples/compose/) | один контейнер перед Anthropic/OpenAI на ноутбуке, оракул выключен — **не** боевой вариант |
| Демо на ноутбуке | `docker-compose.demo.yml` (`scripts/demo.sh`) | показать round-trip и аудит — токены в памяти, захардкоженный командный токен, без конвейера аудита |

## Запуск на сервере (docker compose)

[`compose/`](compose/) — боевой вариант для хостов без Kubernetes. В него входят **data plane** (`litellm` — прокси со встроенным guardrail — плюс `redis` под Cache B и `postgres` под токены/конфиг команд), **self-hosted Langfuse v3** (`langfuse-web`/`-worker`, ClickHouse, MinIO, отдельный Redis) и **конвейер аудита** (`vector`, читает stdout шлюза и доставляет в Langfuse; VRL-гейт NEVER-полей побайтово совпадает с тем, что в Helm-чарте).

### Сначала — выбрать режим аутентификации

Режима два, оба production, **взаимоисключающие** — решение принимается до заполнения `.env`. Стек, каскад детекции и цепочка аудита у них одинаковые; отличается только креденшел, с которым идёт запрос наверх.

| | **Режим A** — корп-API-ключи | **Режим B** — подписка (OAuth) |
|---|---|---|
| Запуск | `docker compose up -d` | `-f docker-compose.yml -f docker-compose.oauth.yml` |
| Креденшел наверх | `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` шлюза | OAuth-токен подписки разработчика, пробрасывается без изменений |
| Разработчик шлёт | `Authorization: Bearer <виртуальный ключ litellm>` | `Authorization: Bearer <sk-ant-oat…>` |
| Идентичность команды | `X-Corp-Auth: <токен команды>` | так же |
| `LITELLM_MASTER_KEY` | **обязателен** | **должен отсутствовать** (пустая строка считается заданной — удалять строку целиком) |
| Обслуживаемые маршруты | `claude-*`, `gpt-*`, `corp-*` | только `claude-*`, и это несущий контроль безопасности, а не упрощение |
| Отзыв / учёт расходов на человека | да, через админ-UI LiteLLM | нет |

Полная матрица, все режимы отказа и почему режимы не сосуществуют: [`docs/ops/deployment-modes.ru.md`](docs/ops/deployment-modes.ru.md) · EN: [`docs/ops/deployment-modes.md`](docs/ops/deployment-modes.md).

### Быстрый старт

```bash
# день 0, на сервере (поставит docker, если его нет, создаст /opt/corp-llm-gateway
# и .env с правами 0600, затем выйдет с кодом 1, чтобы вы заполнили .env — так и задумано)
sudo scripts/deploy/bootstrap-server.sh

# положить схему хранилища токенов — init-скрипты postgres отрабатывают только на пустом томе
cp src/corp_llm_gateway/tokens/schema.sql compose/postgres/initdb/01-schema.sql

cd compose && docker compose up -d          # режим A
docker compose ps                           # healthy через ~30-60 с; langfuse на чистом томе ~2 мин
curl -fsS http://127.0.0.1:4000/health/liveliness
```

Пропустить шаг со схемой — тихая ловушка, а не заметный сбой: все сервисы отчитываются healthy, `/health/liveliness` отвечает, но каждый реальный запрос падает с 500, потому что `corp_tokens` / `team_config` не существуют.

Секреты без значения по умолчанию (`GATEWAY_IMAGE_TAG`, `POSTGRES_PASSWORD`, инфраструктурные `LANGFUSE_*`, `CORP_LANGFUSE_PUBLIC_KEY` / `CORP_LANGFUSE_SECRET_KEY`) заставляют `docker compose up` отказаться стартовать с указанием переменной, а не подняться наполовину настроенным. Полный прокомментированный шаблон: [`compose/.env.example`](compose/.env.example).

### Развёртывание с ноутбука оператора

```bash
scripts/deploy/deploy.sh --host user@server up               # режим A
scripts/deploy/deploy.sh --host user@server --mode oauth up  # режим B
```

Скрипт подкладывает SQL-схему, синхронизирует `compose/`, делает `pull`, поднимает стек и ждёт healthcheck'ов. Локальный `.env` наверх не уезжает никогда, серверный `.env` не читается, не печатается и не перезаписывается; ключи и сертификаты из синхронизации исключены. Остальные подкоманды: `down` (тома сохраняются, спрашивает подтверждение), `restart`, `logs`, `status`; полезные флаги `--dry-run`, `--yes`, `--dir`, `--force-unlock`. **Тот же `--mode` нужно передавать во все последующие запуски по этому хосту** — `logs`/`status`/`down` резолвят стек через тот же список файлов, и запуск без него покажет (или пересоздаст) другой стек.

Для автозапуска после перезагрузки в режиме B дополнительно раскомментируйте `COMPOSE_FILE=docker-compose.yml:docker-compose.oauth.yml` в `.env`: systemd-юнит выполняет голый `docker compose up -d`, который иначе резолвит только базовый файл.

### Две опциональные сетевые зависимости

Обе **выключены по умолчанию**, и включение любой из них **безопасно для Cache A** — в ключ кэша входит отпечаток эффективной политики детекторов, поэтому записи с разными настройками не пересекаются. Чистить кэш не нужно.

| Переключатель | Выключено (по умолчанию) | Включено |
|---|---|---|
| `CORP_LLM_ORACLE_ENABLED` | вызов оракула не делается никогда; `CORP_LLM_ENDPOINT` для детекции не нужен | требует доступного `CORP_LLM_ENDPOINT` — включение без него роняет запросы fail-closed на **всех** маршрутах, а не только на `corp-*` |
| `CORP_NER_ENABLED` | ни детектора, ни пробы готовности — так, будто фичи не существует | требует `CORP_NER_ENDPOINT` (базовый URL, клиент сам добавит `/v1/analyze`) и **сборки из исходников** — опубликованный тег образа старше этой работы |

```bash
# запустить код текущей ветки вместо опубликованного тега (оверлей намеренно собирает
# NER-профиль ru-en: в профиле по умолчанию нет английской модели, и такой образ отвечал бы 503)
docker compose -f docker-compose.yml -f docker-compose.build.yml up -d --build
```

Не путайте `CORP_NER_ENABLED` (удалённый сервис) с `CORP_LLM_REQUIRE_NER` (внутрипроцессные RU/EN-движки) — второй в production остаётся `1` в любом случае.

### Что знать до ввода в эксплуатацию

- **TLS перед стеком ещё нет.** Единственный опубликованный порт — `127.0.0.1:4000`; фронт на nginx появится в следующей ревизии. До этого разработчики ходят через SSH-туннель, а не по сети.
- **В режиме B management-эндпоинты litellm без аутентификации** (`/key/*`, `/model/*`, `/user/*`, UI) — без мастер-ключа его proxy-auth пропускается целиком, а это ровно то, чего режим требует. LLM-маршруты по-прежнему закрывает проверка `X-Corp-Auth` в guardrail. Сегодня доступно только с loopback; закрыть на nginx **до** вывода порта наружу. В режиме A такого разрыва нет.
- **Никакого недоверенного `docker run` на этом хосте.** Vector отбирает записи аудита по публичной метке контейнера, поэтому любой, кто может запускать там контейнеры, может подделать записи аудита. Это требование модели развёртывания, а не рекомендация.
- **Аудит буферизуется, но не fail-closed** — осознанное отступление от значения `vectorBufferFull` по умолчанию из [`docs/security.ru.md`](docs/security.ru.md) §8. Остановка доставки аудита не останавливает egress; реальная граница долговечности — ротация логов docker'а, а не дисковый буфер Vector.

**Полный справочник по стеку** (маршрутизация, виртуальные ключи, почему здесь нет BYOK, Langfuse, конвейер аудита, TLS до корп-vLLM, процедуры восстановления): [`compose/README.ru.md`](compose/README.ru.md) · EN: [`compose/README.md`](compose/README.md). **Пошаговая инструкция** для того, кто разворачивает: [`docs/ops/deploy-handoff.ru.md`](docs/ops/deploy-handoff.ru.md) · EN: [`docs/ops/deploy-handoff.md`](docs/ops/deploy-handoff.md).

## Запуск локально (docker compose)

Без корп-vLLM и без Kubernetes: `CORP_LLM_ORACLE_ENABLED=0` запускает шлюз как
локальный санитизирующий прокси прямо перед Anthropic/OpenAI, на опубликованном
образе из GHCR. Локальный каскад (regex+checksum, двуязычный NER, газеттир,
сплиттер) по-прежнему отрабатывает на каждом запросе — пропускается только
уточняющий проход LLM-оракула. BYOK в этом режиме — общий ключ на стороне
шлюза, а не ключ каждого разработчика (нативная маршрутизация anthropic/openai
не умеет пробрасывать клиентский ключ — полный разбор в README примера).

Требуется опубликованный образ `≥ v1.0.0-rc.5` (первый тег с переключателем
оракула) — либо соберите локально: `docker build -f Dockerfile.gateway
--build-arg NER_PROFILE=en -t corp-llm-gateway:local .`

```bash
cd examples/compose && cp .env.example .env   # вписать dev-токен + ключ(и) провайдера
docker compose up -d
```

Это удобство для ноутбука, а не боевой вариант — здесь нет ни конвейера аудита, ни Langfuse. Для сервера используйте [`compose/`](#запуск-на-сервере-docker-compose).

Полный разбор, находка про BYOK и путь обратного включения оракула:
[`examples/compose/README.md`](examples/compose/README.md).

## Быстрый старт для оператора (k8s)

Вариант для кластера. Для одного хоста без k8s см. [Запуск на сервере](#запуск-на-сервере-docker-compose) — тот же guardrail, та же цепочка аудита, другая упаковка.

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

Каждая команда ведёт файл `replace.md` по пути `<rules-dir>/<team_id>.md`. Эти правила выполняются **первыми** в локальном каскаде; совпадение правила и находка детектора/NER/корп-LLM состязаются за один и тот же span — побеждает более длинный, а правило побеждает при равенстве только если его span совпадает со span находки ТОЧНО.

Формат — одно правило на строку, разделитель `=` (легаси `→` U+2192 по-прежнему принимается). Сопоставление — обычная подстрока **без учёта регистра** (раньше регистр учитывался), одинаково для одного слова и для фразы; требования к границам идентификатора нет, поэтому `kdir = [X]` совпадёт и с `kdir` внутри `mkdir`, а не только с `KdirService`. При пересечении побеждает **более длинный span** — правило или находка NER/оракула, безразлично; правило больше не перебивает автоматически более длинную пересекающуюся находку (при точном совпадении span оно по-прежнему выигрывает). Регистронезависимое сопоставление — это изменение поведения относительно прошлого релиза; прочитайте [`docs/replace-md-authoring.ru.md`](docs/replace-md-authoring.ru.md), прежде чем считать, что существующее правило срабатывает там же, где раньше. Оборачивайте в кавычки любое значение, содержащее `=`:

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
PYTHONPATH=src .venv/bin/pytest tests/ -q     # ~2274 passed / 107 skipped, ~76с (3.14 грациозный NER; полный NER + RS256 crypto на 3.12/CI)
PYTHONPATH=src .venv/bin/ruff check src tests
```

Соглашения, инварианты и «чего НЕ делать» закреплены в [`CLAUDE.md`](CLAUDE.md). CI — GitHub Actions (`.github/workflows/`).

## На чём построено

Open-source-компоненты, из которых собран шлюз (Архитектура B — лучшие в своём классе):

- **Прокси и serving** — [LiteLLM](https://github.com/BerriAI/litellm) (мультипровайдерный прокси + guardrail-хуки) · [vLLM](https://github.com/vllm-project/vllm) (бэкенд корп-оракула пред-пасса)
- **Двуязычный NER и морфология** — RU: [Natasha](https://github.com/natasha/natasha) · [Slovnet](https://github.com/natasha/slovnet) · [Navec](https://github.com/natasha/navec) · [Razdel](https://github.com/natasha/razdel) · [pymorphy3](https://pypi.org/project/pymorphy3/); EN: [spaCy](https://spacy.io) + [`en_core_web_md`](https://spacy.io/models/en). Альтернативы ([Presidio](https://github.com/microsoft/presidio), [DeepPavlov](https://github.com/deeppavlov/DeepPavlov)) рассмотрены и отклонены из-за латентности на CPU
- **Состояние и хранилища** — [Redis](https://redis.io) (кэши маппинга / дедупа) · [PostgreSQL](https://www.postgresql.org) через [asyncpg](https://github.com/MagicStack/asyncpg) (хранилище токенов)
- **Аудит и наблюдаемость** — [Vector](https://vector.dev) → [Langfuse](https://langfuse.com) + S3 + SIEM
- **Доставка и клиенты** — [Helm](https://helm.sh) (чарт) · [Docker Compose](https://docs.docker.com/compose/) (развёртывание на одном хосте) · [CoreDNS](https://coredns.io) (egress-sinkhole) · [httpx](https://www.python-httpx.org) (клиент корп-LLM)

## Лицензия

Copyright (c) 2026 Artem Likhomanenko.

**Ядро** шлюза (этот репозиторий) распространяется под
[Apache License 2.0](LICENSE) — свободно для любого использования, включая
коммерческое. **Enterprise-плагины, готовые enterprise-сборки и коммерческая
поддержка** — отдельные проприетарные предложения, см.
[`LEGAL/COMMERCIAL-LICENSING.md`](LEGAL/COMMERCIAL-LICENSING.md).
Вклады принимаются на условиях [`LEGAL/CLA.md`](LEGAL/CLA.md).
