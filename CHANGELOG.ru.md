# История изменений

Все значимые изменения в corp-llm-gateway задокументированы здесь.
Формат следует [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

---

## [Unreleased]

## [1.0.0] — GA (2026-07-09)

Первый GA-релиз — **цикл local-first детекции** (ниже) плюс сборка **GA-readiness /
безопасность и расширяемость**. Некомпромиссный критерий: ноль подтверждённых инцидентов утечки
за 90 дней после GA.

### Добавлено — GA-readiness, безопасность и расширяемость
- **Слой плагинов / профилей** — декларативные бандлы `profiles/` (страна / подразделение / режим),
  монотонно-ужесточающий `PolicyKnobs.merge`, hash-запечатанная целостность, SHA-256 изоляция кэша
  между юрисдикциями, выбор через `TeamConfig.profile_ids`.
- **Seam-ы расширений** — keyed-реестры `extensions/` + `providers/` (fail-closed регистрация +
  гейт api-version; v1 anthropic / openai / corp-vllm, v2 за гейтом), `DETECTOR_REGISTRY`,
  подключаемый экспортер метрик, composition root `bootstrap.build_guardrail()`; руководство
  контрибьютора `docs/extending.md`.
- **Укрепление безопасности** — 11 repro-first исправлений поверхностей утечки (oversize + NER
  fail-closed, OpenAI `tool_calls` + streaming, покрытие сегментатора, срезание `X-Corp-Auth` во всех
  расположениях заголовка, host-pin dev-прокси, тело ошибки, TLS/RBAC, рекурсивный NEVER-гейт,
  RS256 + aud/iss).
- **Ops** — реальный `gateway-admin` (team / token / extensions / config check), production Helm-чарт
  (образ guardrail + callback, config-check initContainer, NetworkPolicy, CoreDNS sinkhole),
  обслуживаемый healthz, ops-документация.
- **`replace.md`** — `=` теперь канонический разделитель правил (легаси `→` по-прежнему парсится).

### Цикл local-first детекции (2026-06-30)

> План: `docs/plans/20260630-bilingual-local-first-detection.md`
> ADR: `docs/adr/ADR-003-ner-orchestration.md` — hand-roll dual-NER (Natasha RU + spaCy EN)
> вместо Presidio-как-оркестратора и DeepPavlov/BERT (отклонены: «kill-shot» на этапе установки на CPU,
> модель 1.44 GB, нет колёс для torch<1.14 на современных платформах).
> Дельта соответствия: ✅ 2 / 🟡 8 / ❌ 5 → **✅ 11 / 🟡 3 / ⚪ 1** из 15 требований ИБ.

### Добавлено — Детекция (Track 1, задачи DP-0…DP-9)

- `RegexChecksumDetector` (`detectors/regex_checksum.py`) — валидируемые по алгоритму ИНН (10/12),
  КПП, ОГРН (13/15), БИК, СНИЛС, р/счёт, плюс JWT, приватный ключ PEM, `sk-`/`AKIA`/`ghp_`/
  обобщённый `password=`, IPv4/6 (через `ipaddress`), CIDR, внутренние hostname
  (`*.corp.internal/.lan/.local`), DB-URL. Почти нулевой уровень ложных срабатываний за счёт checksum. (DP-1)
- Двуязычный `DualNerDetector` (`detectors/dual_ner.py`) — Natasha/Slovnet RU + spaCy
  `en_core_web_md` EN, run-both-union с де-overlap по длиннейшему спану и провенанс-лейблами;
  покрывает ФИО, организации, адреса в смешанных по языку запросах. (DP-2)
- Проход local-first детекции слит с оракулом в `sanitizer/engine.py` — аддитивно; оракул
  остаётся включённым безусловно на DP-3, сужается на DP-4. (DP-3)
- Лемма-газеттир (`rules/gazetteer.py`) со встроенными словарями продуктов/кодовых имён
  (`rules/defaults/products.txt`), регулируемых терминов ПОД-ФТ/AML-CFT (`rules/defaults/regulated.txt`)
  и грифов конфиденциальности (`rules/defaults/markings.txt`). Матчинг по лемме, поэтому словоформы
  (`легализации`) попадают. Оракул вызывается только по попаданию в газеттир. (DP-4)
- Сегментатор, понимающий код, + сплиттер идентификаторов (`sanitizer/segmenter/`) — разбивает camel/snake-
  идентификаторы (`CompanynameabcService` → `Companynameabc`) и сканирует сегменты по
  газеттиру. (DP-5)
- Классификатор payload до egress на Stage 0 (`payload/classifier.py`) — сигнатуры `.env`, kubeconfig,
  nginx.conf, лог-дампов/stack-trace → HTTP 422 `block_reason`; upstream не вызывается.
  `block_reason` — CONDITIONAL-поле аудита, провозится в Langfuse. (DP-6)
- Stage 5 DLP egress guard (`sanitizer/dlp_guard.py`) — независимый пере-скан вторым слоем
  санитизированного исходящего payload на canary-строки и высоконадёжные секреты; блокирует всё уцелевшее
  с HTTP 422. (DP-7)
- Allowlist тестовых данных (`sanitizer/allowlist.py`) — детерминированное исключение для тестовых фикстур;
  спроектирован так, что не может подавить настоящие секреты. (DP-8)
- Импорты NER ленивые; Natasha + spaCy в опциональном extra `[ner]`. Python 3.14 деградирует
  грациозно (нет NER-колёс); авторитетный прогон тестов — на Python 3.12 (875 passed). (DP-2, DP-9)
- Вынос локального NER в отдельный поток с async event loop (`asyncio.get_event_loop().run_in_executor`),
  чтобы не блокировать callback-корутину LiteLLM. (DP-9)
- Образ LiteLLM для демо собран с extra `[ner]` — двуязычный NER работает в демо-стеке.

### Добавлено — Соответствие требованиям (Track 2, задачи CP-1…CP-4)

- `PostgresTokenStore` (`tokens/postgres_store.py`) — персистентное хранилище токенов на asyncpg;
  `make_auth_middleware()` выбирает его, когда задан `CORP_LLM_PG_DSN`; контракт-тесты
  параметризованы по backend-ам in-memory + Postgres. (CP-1)
- RBAC-гейт `gateway:operator` на admin CLI — `verify_operator()` в `auth/rbac.py` проверяет
  JWT-claim через PyJWT; `_enforce_rbac()` вызывается на каждой мутирующей подкоманде `gateway-admin`;
  отказ → stderr + код выхода 2. (CP-2)
- SIEM-sink заведён в Vector configmap (HTTP-sink под `audit.sinks.siem.enabled`, наследует
  NEVER-VRL-гейт). Helm-алерты `AuditVectorDropHigh` + `LeakAttemptDetected` в
  `helm/.../templates/siem-alerts.yaml` с CI-ассертами рендера. Endpoint остаётся placeholder
  до закрытия open Q#3. (CP-3)
- `NetworkPolicy` + CoreDNS sinkhole включены в `helm/.../values-prod.yaml`; egress ограничен
  upstream + корп-CIDR. (CP-4)

### Исправлено

- Аудит для блокировок Stage-0/Stage-5 теперь эмитится инлайн через `async_log_failure_event` (идемпотентно);
  `block_reason` появляется во всех sink-ах аудита, включая Langfuse.
- Отказы в Pre_call (сбой аутентификации, некорректный запрос, corp-LLM-down) — все аудируются инлайн.

---

## [0.0.2] — ядро санитизации v1 + эксплуатация (2026-05-07, план rev 7)

> План: `docs/plans/20260507-external-sanitizer-gateway-v1.md` (вехи M0–M8).
> Вехи M1–M6 + M8 завершены по коду. Остаются: провижининг M0, применение на кластере M5,
> фазы раскатки и подписания (заблокированы инфраструктурой и процессами).

### Добавлено

**M0 — Основы**

- Каркас репозитория: пакет `corp_llm_gateway`, точки входа `pyproject.toml`, pre-commit-хуки,
  скелет CI.
- Helm-чарт (`helm/corp-llm-gateway/`) — шаблоны Deployment (litellm + vector sidecar), Service,
  Ingress, ConfigMap, NetworkPolicy, CoreDNS sinkhole.
- Контракт Corp-LLM (vLLM) закрыт; `CorpLlmClient` (`corp_llm/`), говорящий с
  `/v1/chat/completions`.

**M1 — Ядро санитизации**

- ABC `PIIDetector` + реестр `ShadowDetector` (`detectors/`); паттерн interface-registry из
  ADR-001.
- `MappingStore` (`storage/`) с backend-ами in-memory и Redis; параметризация контракт-тестов.
- `CorpLlmSanitizer` с исходной трёхуровневой стратегией: `FunctionCallStrategy → JsonStrategy →
  RegexStrategy` (побеждает первая сработавшая; regex — это пол).
- Инвариант подстановки плейсхолдеров по убыванию длины (#5, M1-9).
- `StreamingDesanitizer` (`sanitizer/`) со скользящим SSE-осведомлённым буфером для стриминга Anthropic и OpenAI.
- `RequestPlaceholderAllocator` — биекция на уровне запроса, предотвращающая межсегментные коллизии плейсхолдеров.
- Обходчик content-блоков: санитизирует блоки `tool_use.input`, `tool_result`, `document`, `system`;
  де-санитизация стримингового `tool_use`; блоки `thinking` пробрасываются by design (подписаны Anthropic).
- `litellm_hook.py` `CorpLlmGuardrail` — `async_pre_call_hook`, `async_post_call_success_hook`,
  хук стримингового итератора, audit-callback-и `async_log_*`. (M1-7)
- Парсер `replace.md` + кэширующий на 5 минут загрузчик файлов (M1-10, M1-15).
- Хелперы порога размера payload + gzip + квоты на команду (`payload/`). (M1-11)

**M2 — Аутентификация и мультитенантность**

- `tokens/schema.sql` + `AuthMiddleware` с кэшем отзыва на 60 s.
- `TokenIssuer` с подключаемым OIDC-верификатором (M2-3).
- `TeamConfigStore` с конфигом retention на команду + переопределениями fail-policy (M2-4).
- Скелет CLI `gateway-admin`: `team create/update/delete`, `token issue/revoke` (M2-5).
- Инвариант passthrough BYOK `Authorization: Bearer` (#3).

**M3 — Конвейер аудита**

- Схема `AuditEvent` с уровнями полей ALWAYS / CONDITIONAL / NEVER; `docs/audit-schema.md`.
- Структурированный логгер аудита + гейт NEVER-полей (`audit/invariants.py`); эшелонированная защита
  Vector VRL для того же набора полей.
- Langfuse-sink + e2e интеграционный тест + задача CI (M3-4).
- Генератор lifecycle-политики S3 из конфига retention команды (M3-7).
- `finding_label_counts` + счётчики уникальных секретов в событиях аудита.

**M4 — Режимы отказа и здоровье**

- Endpoint-ы `/healthz/live`, `/healthz/ready`, `/healthz/sanitization` (глубокая проверка).
- Матрица fail-policy (M4) как источник истины; 503 `E_CORP_LLM_DOWN` + fail-closed пути;
  никаких ad-hoc fail-open путей в коде.

**M5 — Egress / CoreDNS**

- Helm-шаблоны для блокировки egress через `NetworkPolicy` + CoreDNS sinkhole.
- TLS корп-LLM проверяется через `CORP_LLM_CA_BUNDLE` (CA-бандл корпоративного CA; `SSL_CERT_FILE` для
  aiohttp-пути LiteLLM).

**M6 — Онбординг**

- `scripts/install.sh` — bash/zsh/fish, macOS/Linux, OAuth device-flow через Keycloak, идемпотентный
  апдейтер rc-блока, round-trip smoke-тест.
- CLI `corp-llm-gateway status` (диагностика для разработчика — наличие токена, живость шлюза, версия,
  проверка обновлений).
- `corp-llm-gateway-proxy` — localhost-прокси, инъецирующий заголовки (Паттерн 3, перечитывает файл токена
  на каждый запрос).
- Проверка авто-обновления + задача релиза в CI (M6-6…M6-8).

**M8 — Документация**

- `docs/ops/runbook.md`, `docs/ops/capacity.md` (расчёт мощностей alpha → GA при 1000 разработчиков / 50 RPS).
- `docs/replace-md-authoring.md`, `docs/rbac-matrix.md`, ADR-001 (interface-registry).
- `docs/security.md` — покрытие sanitization, гарантии конвейера аудита, известные пробелы в конфигурации.
- Резервный TOML property-файл для всех переменных окружения (`config.py`, `config.example.toml`).
- Создано зеркало internal git host (`corp-llm-gateway`); open Q#1 закрыт.

### Исправлено

- Утечка через content-блоки Anthropic — обходчик контента теперь санитизирует списки блоков, `tool_result`,
  `system`.
- Межсегментная коллизия плейсхолдеров — биекция `RequestPlaceholderAllocator`.
- Предотвращена коллизия с литеральным плейсхолдером, введённым пользователем (упрочнение case-4).
- SSE-осведомлённая стриминговая де-санитизация для проводных форматов и Anthropic, и OpenAI.
- Атрибуция аудита по ключу `litellm_call_id`; записи аудита сохраняют реальную идентичность +
  `redaction_count` при передаче между pre/post.
- Production Vector configmap: исправлен дублирующийся ключ `transforms:`; NEVER-гейт завершён;
  добавлен путь `audit_only`.
- Corp-LLM fail-closed 503 на `E_CORP_LLM_DOWN`; восстановлена корректная атрибуция аудита.

---

## [0.0.1] — первоначальный каркас (2026-05-07)

### Добавлено

- Каркас репозитория, скелет CI, `pyproject.toml` с точками входа CLI
  (`corp-llm-gateway`, `corp-llm-gateway-proxy`, `gateway-admin`).
- Подключаемый интерфейс аутентификации `CorpLlmAuthProvider` (`auth/`) — по умолчанию Noop; заглушки
  Bearer/mTLS/OIDC бросают `NotImplementedError` с указанием блокирующей задачи.
- ABC `PIIDetector` + заглушка `ShadowDetector`.
