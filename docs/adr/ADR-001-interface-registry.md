# ADR-001. Реестр интерфейсов для корпоративного LLM-шлюза

## Статус

**Принято.** План v1, ревизия 5 (2026-05-07). Поправка 1.1 (2026-05-08): правило #5 ослаблено — допустим TOML-fallback при отсутствии переменной окружения (см. историю).

## Контекст

Шлюз `corp-llm-gateway` стоит на пути всего корпоративного трафика к Anthropic / OpenAI и обязан санитизировать содержимое запросов и ответов до пересечения границы корпоративной сети. Архитектура B (см. план в `docs/plans/20260507-external-sanitizer-gateway-v1.md`) предусматривает:

- LiteLLM как точку входа и форвардер,
- единый Python-страж (custom guardrail), реализующий санитизацию,
- ряд внешних зависимостей: Redis (двухуровневый кэш), Postgres (токены и конфигурация команд), corp LLM (применение правил `replace.md`), пред-фильтр (модель PII на CPU), аудит-конвейер (Vector → Langfuse + S3 + SIEM), Keycloak (OIDC).

Каждая из этих зависимостей подвержена изменениям по независимым траекториям:

- **corp LLM** сегодня без аутентификации, в будущем — Bearer / mTLS / OIDC / API-key (см. ADR-002, если будет создан);
- **Postgres** появится во время M0-5; до этого нужны in-memory заглушки для локальной разработки и юнит-тестов;
- **SIEM** ещё не выбран (Splunk / Elastic / Sumologic / Sentinel — открытый вопрос #3);
- **Pre-pass** изначально планировался на GPU, но был переведён на CPU (ревизия 5);
- **Anthropic / OpenAI** провайдеры — в v1 закреплены, но v2 предполагает Bedrock / Gemini / Azure без переписывания шлюза.

Прямое связывание с конкретной реализацией каждой зависимости породило бы:

1. **Refactor-cliff** при включении аутентификации corp LLM посреди раскатки (Phase 2/3);
2. **Невозможность юнит-тестирования** без поднятого Postgres / Redis / corp LLM;
3. **Тесная связь** аудит-конвейера с конкретным SIEM-продуктом до того, как он выбран;
4. **Барьер для расширения** провайдерами (v2): каждое добавление трогало бы код strategies и client.

## Решение

Для каждой внешней зависимости и для каждой пользовательской политики, где допустимы несколько реализаций, используется **реестр интерфейсов**: абстрактный класс (`abc.ABC`) — контракт, плюс набор конкретных реализаций, плюс единая точка сборки (фабрика или явная инъекция).

### Канонический шаблон

```text
src/corp_llm_gateway/<module>/
    __init__.py        # реэкспорт ABC + всех реализаций + фабрики
    <base>.py          # ABC с минимальным контрактом
    <impl_a>.py        # реальная имплементация
    <impl_b>.py        # вторая реальная имплементация (или заглушка)
    factory.py         # опционально: выбор реализации по env-переменной
```

Сопутствующие тесты `tests/<module>/` параметризованы по реализациям, чтобы единый contract-suite гонялся против каждой (см. `tests/storage/test_mapping_store.py`).

### Сводная таблица реестров

| Реестр | ABC | Реализации (текущие) | Реализации (заглушки) | Точка переключения |
|---|---|---|---|---|
| Аутентификация к corp LLM | `auth.providers.CorpLlmAuthProvider` | `NoopAuthProvider` | `BearerAuthProvider`, `MtlsAuthProvider`, `OidcAuthProvider`, `ApiKeyHeaderAuthProvider` | env: `CORP_LLM_AUTH_PROVIDER` |
| Хранилище маппингов (Cache A + Cache B) | `storage.mapping.MappingStore` | `InMemoryMappingStore`, `RedisMappingStore` | — | DI в `SanitizationOrchestrator` |
| Хранилище правил (`replace.md`) | `rules.loader.RulesLoader` | `FileRulesLoader`, `CachedRulesLoader` | — | DI в `SanitizationOrchestrator` |
| Стратегии разбора ответа corp LLM | `sanitizer.strategies.SanitizerStrategy` | `FunctionCallStrategy`, `JsonStrategy`, `RegexStrategy` | — | список передаётся в `CorpLlmSanitizer` |
| Детектор PII | `detectors.base.PIIDetector` | `ShadowDetector` (композиция) | `OpenaiPrivacyFilterDetector`, `PresidioDetector` | DI в guardrail |
| Хранилище токенов | `tokens.store.TokenStore` | `InMemoryTokenStore` | `PostgresTokenStore` | DI в `AuthMiddleware` / `TokenIssuer` |
| Хранилище конфигурации команд | `team_config.store.TeamConfigStore` | `InMemoryTeamConfigStore` | `PostgresTeamConfigStore` | DI в admin-CLI |
| Sink аудит-журнала | `audit.sinks.Sink` | `StdoutSink`, `ListSink` | — (внешние sinks Vector) | DI в `AuditLogger` |

### Правила использования

1. **Контракт** — минимально достаточный: только методы, которые действительно зовут потребители. Никакой утечки деталей реализации (например, в `MappingStore` нет упоминания Redis).
2. **Все вызовы — через ABC.** Прямые ссылки на конкретный класс допустимы только в `factory.py` и в тестовых helpers.
3. **Заглушка → `NotImplementedError` с указанием блокера.** Пример: `PostgresTokenStore.lookup` бросает `NotImplementedError("PostgresTokenStore stub — implement after M0-5 Postgres is provisioned")`. Это превращает попытку использования незавершённой реализации в очевидный отказ, а не в тихую ошибку.
4. **Контракт-тесты параметризованы.** Любая новая реализация ABC обязана пройти тот же тест-сьют, что и существующие; см. `tests/storage/test_mapping_store.py` как эталон.
5. **Фабрика читает настройки в порядке: env → TOML-файл → caller default.** Переменная окружения побеждает значение из файла; файл — опциональный fallback (`src/corp_llm_gateway/config.py`, см. `config.example.toml`). Поиск файла: `$CORP_LLM_GATEWAY_CONFIG_FILE` → `~/.corp-llm-gateway/config.toml` → `/etc/corp-llm-gateway/config.toml`. Неизвестное значение `CORP_LLM_AUTH_PROVIDER` (или любого другого discriminator-ключа) по-прежнему — `ValueError` с перечислением допустимых.

### Пример: переключение аутентификации к corp LLM

> [!IMPORTANT] 
> **Сегодняшнее состояние**
> Corp LLM не требует аутентификации. В production выбран `NoopAuthProvider`. Семантика — `artifacts()` возвращает пустой набор заголовков и нулевой client cert.
> 

Для включения Bearer-токена в будущем:

1. Реализовать `BearerAuthProvider.artifacts()` (сейчас — заглушка).
2. Поместить токен в k8s Secret и смонтировать как переменную `CORP_LLM_BEARER_TOKEN`.
3. Изменить Helm value: `corpLlm.authProvider: bearer`.
4. Перевыкатить deployment.

> [!NOTE]
> Никаких изменений в коде `SanitizationOrchestrator`, `CorpLlmClient`, `litellm_hook` или тестах. Это и есть инвариант реестра — переключение реализации остаётся config-only.
>

## Последствия

### Положительные

- **Юнит-тестируемость без инфраструктуры.** 546 тестов исполняются за ≈16 секунд без Redis, Postgres, corp LLM или k8s.
- **Параллельная разработка.** Команды могут писать `PostgresTokenStore` и `PostgresTeamConfigStore` независимо от M0-5 готовности Postgres-кластера; контракт уже зафиксирован.
- **Безопасный rollout.** Переключения в режимах "fail-policy matrix" (см. milestone M4 в плане) делаются через DI или конфигурацию team_config, без релиза кода.
- **Прозрачность для аудита.** Список реализаций каждого ABC — это явный реестр в `__init__.py`; security-review видит, какие пути исполнения возможны.

### Отрицательные / стоимостные

- **Накладные расходы на ABC.** Для крайне простых модулей (например, `payload/size_threshold.py`) контракт через ABC был бы overkill — поэтому в шлюзе такие места реализованы как чистые функции, без реестра. Решение принимается пер-модуль.
- **Риск "интерфейсного зоопарка".** Если протокол ABC расширяется новыми методами, все реализации (включая заглушки) должны быть обновлены. Mitigation: контракт держится минимальным; дополнительная функциональность выносится в композирующие классы (как `ShadowDetector` поверх `PIIDetector`, или `CachedRulesLoader` поверх `RulesLoader`).
- **Возможность "обхода" контракта.** Программист может импортировать конкретную имплементацию напрямую и обойти ABC. Mitigation: соглашение в `CLAUDE.md`; ревью PR проверяет.

### Нейтральные

- Контракт-тесты обязательны при добавлении новой реализации — это и плюс (страховка), и обязанность.

## Реализация (метрика готовности)

| Реестр | Готовность | Где смотреть код |
|---|---|---|
| `CorpLlmAuthProvider` | ✅ контракт + Noop + 4 заглушки + фабрика | `src/corp_llm_gateway/auth/` |
| `MappingStore` | ✅ контракт + InMemory + Redis | `src/corp_llm_gateway/storage/` |
| `RulesLoader` | ✅ контракт + FileRulesLoader + CachedRulesLoader | `src/corp_llm_gateway/rules/` |
| `SanitizerStrategy` | ✅ контракт + 3 реализации против vLLM API | `src/corp_llm_gateway/sanitizer/strategies.py` |
| `PIIDetector` | ⚠️ контракт + ShadowDetector; заглушки openai-privacy-filter и Presidio | `src/corp_llm_gateway/detectors/` |
| `TokenStore` | ⚠️ контракт + InMemory; Postgres-заглушка ждёт M0-5 | `src/corp_llm_gateway/tokens/` |
| `TeamConfigStore` | ⚠️ контракт + InMemory; Postgres-заглушка ждёт M0-5 | `src/corp_llm_gateway/team_config/` |
| `Sink` (audit) | ✅ контракт + StdoutSink + ListSink | `src/corp_llm_gateway/audit/sinks.py` |

Легенда:
- ✅ — все запланированные на v1 реализации завершены и покрыты тестами;
- ⚠️ — контракт и хотя бы одна реализация готовы; остальные — осознанные заглушки до момента появления внешней зависимости.

## Связанные документы

- `docs/plans/20260507-external-sanitizer-gateway-v1.md` — основной план v1 (ревизия 5).
- `docs/audit-schema.md` — поля аудит-журнала (источник истины для `Sink`).
- `docs/ops/capacity.md` — sizing для CPU pre-pass (см. ADR в части "no GPU").
- `docs/rbac-matrix.md` — роли Operator / Auditor / Developer.
- `CLAUDE.md` — раздел "Conventions for new modules" (как добавить новую реализацию ABC).

## История

| Дата | Ревизия | Изменение |
|---|---|---|
| 2026-05-07 | 1.0 | Принято в составе плана v1 (ревизии 1–5). |
| 2026-05-08 | 1.1 | Правило #5 ослаблено: фабрика читает env → TOML-файл (`config.py`) → default. Env по-прежнему побеждает; для существующих deployment'ов поведение не меняется. Мотивация: убрать жёсткое требование передавать секреты через env-переменные на лэптопах разработчиков и при администрировании через `gateway-admin`. |
