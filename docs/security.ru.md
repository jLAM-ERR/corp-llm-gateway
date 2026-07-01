# Модель безопасности

Как corp-llm-gateway удерживает PII внутри корпоративного периметра, что он
санитизирует и куда смотреть при расследовании инцидента.

Читать вместе с: [`audit-schema.md`](audit-schema.ru.md) (источник истины по полям),
[`x-corp-auth.md`](x-corp-auth.ru.md), [`conversation-id.md`](conversation-id.ru.md),
[`ops/runbook.md`](ops/runbook.ru.md) и планом
[`plans/20260507-external-sanitizer-gateway-v1.md`](plans/20260507-external-sanitizer-gateway-v1.md)
(M4 fail-policy матрица — единственный источник истины по поведению при отказах).

## 1. Обзор и модель угроз

Шлюз располагается между инстансами Claude Code разработчиков и upstream LLM
API (`api.anthropic.com` / `api.openai.com`). На `pre_call` он направляет
контент запроса через корп-внутреннюю санитизирующую LLM, заменяя PII /
регулируемые термины плейсхолдерами `[LABEL_NNN]` **до того, как хоть один байт
покинет корпоративный периметр**. На `post_call` он разворачивает плейсхолдеры
обратно в оригиналы, используя per-conversation маппинг, который никогда не
покидает шлюз.

| Свойство | Поведение |
|---|---|
| Критерий успеха | **Ноль подтверждённых инцидентов утечки** за 90 дней после GA (не подлежит обсуждению) |
| Поведение при отказе | **Fail-closed** для пути санитизации: если corp-LLM не может отработать, запрос отклоняется (503 `E_CORP_LLM_DOWN`), а не пробрасывается без санитизации (`litellm_hook.py` `pre_call`) |
| BYOK `Authorization` | `Authorization: Bearer …` разработчика (ключ Anthropic/OpenAI) пробрасывается наверх (upstream) **без изменений** и **никогда не логируется** — это NEVER-поле в гейте аудита |
| `X-Corp-Auth` | Корп-токен поглощается в `pre_call` (`AuthMiddleware.strip_corp_token`), срезается из пробрасываемых заголовков и **никогда не попадает в аудит-конвейер** — NEVER-поле |

Эшелонированная защита (defense in depth): гарантия отсутствия утечек
обеспечивается на нескольких независимых уровнях (биекция плейсхолдеров,
внутрипроцессный NEVER-гейт, Vector VRL-гейт и инвариант-тест M1-14), так что
любая одиночная регрессия ловится дальше по цепочке.

## 2. Что санитизируется (покрытие контента)

Обходчик контента (content walker) `sanitizer/content_blocks.py` проходит каждую
форму запроса. `pre_call` вызывает его для каждого `content` сообщения плюс
верхнеуровневого поля Anthropic `system`; `collect_text` зеркалит тот же обход в
режиме только-чтение, чтобы пред-скан видел ровно то, что будет санитизировано.

### Покрыто (санитизируется на egress)

| Форма | Что санитизируется |
|---|---|
| Верхнеуровневая строка `content` | Вся строка |
| Блок `text` (`{"type":"text","text":…}`) | Значение `text` |
| Блок `tool_result` | Его `content`, **рекурсивно** (повторно входит в `sanitize_content`) |
| `tool_use.input` | Строковые **листья** входного JSON-дерева, рекурсивно; **ключи** словаря (имена аргументов тула) сохраняются; нестроковые скаляры проходят насквозь |
| Блок `document` | `title`, `context`; `source.data` при `source.type == "text"`; `source.content` рекурсивно при `source.type == "content"` |
| Верхнеуровневый `system` Anthropic | Всё поле (строка или список блоков) |
| Мультимодальные части `content` OpenAI | `text`-части (текстовые блоки в списке); прочие типы частей проходят насквозь |

### Не санитизируется / отложено

| Форма | Почему это допустимо / статус |
|---|---|
| `source` блока `document` с `type` `base64` / `url` | Бинарный или вне-скоуп контент; оставлен без изменений (намеренно) |
| Блоки `image` / `image_url` | Бинарная нагрузка или низкорисковый URL; проходит насквозь |
| Блоки `thinking` / `redacted_thinking` | **Намеренно** проходят без изменений — Anthropic подписывает thinking-блоки и отклоняет изменённые при multi-turn-переигрывании, поэтому их нельзя переписывать; модель в любом случае видит только плейсхолдеры (до неё не доходит ни один оригинал). Корректно by design, а не пробел. |

Де-санитизация на стороне ответа (обратный путь) восстанавливает оригиналы в
потоковом и унарном **text**, во **входе `tool_use`** (`input_json_delta`,
JSON-экранированный, чтобы пересобранный JSON оставался валидным) и в content
OpenAI; только `thinking` намеренно оставлен без изменений (см. строку выше).

Пропуск по размеру (M1-11): когда отдельный сегмент превышает
`guardrail.contentSizeThresholdBytes` (по умолчанию `102400`), оркестратор
**доставляет контент без санитизации и помечает его** (`SanitizeResult(skipped=True)`,
без пар), а `pre_call` логирует `litellm_pre_call_system_sanitize_skipped_size`.
Это компромисс «доставить и пометить» ради латентности, отражённый в логах, — не
тихое отбрасывание.

## 3. Модель плейсхолдеров

Corp-LLM возвращает пары `(original, placeholder)`, где каждый плейсхолдер — это
`[LABEL_NNN]` (например, `[EMAIL_001]`). Далее путь pre-call обеспечивает строгую
per-request **биекцию** через `RequestPlaceholderAllocator`
(`sanitizer/placeholder_allocator.py`), по одному экземпляру allocator на запрос:

- **Один и тот же оригинал → один токен.** Повторяющийся оригинал (даже в разных
  сегментах сообщений и поле `system`) переиспользует свой первый плейсхолдер, так
  что upstream-модель видит для него один согласованный токен.
- **Разные оригиналы → разные токены.** Corp-LLM нумерует плейсхолдеры каждого
  сегмента с `[LABEL_001]` независимо, поэтому два разных оригинала могут
  столкнуться на одном токене; при коллизии allocator **выпускает новый лейбл в
  том же семействе** (`placeholder_family`, например, другой `EMAIL_NNN`). Без
  этого де-санитизация (по ключу-плейсхолдеру) смогла бы восстановить только один
  из них.
- **Подстановка по убыванию длины (M1-9).** И прямой (`apply_pairs`), и обратный
  (`_apply_reverse_to_response`, `sort_placeholders_by_descending_length`) проходы
  сортируют длиннейшие-первыми, чтобы короткий токен не затенял более длинный.

### Пред-скан входа (запрет литералов, введённых пользователем)

Перед санитизацией `pre_call` сканирует вход (`collect_text` →
`find_placeholder_literals`) на любую подстроку формы `[LABEL_NNN]`, введённую
пользователем **буквально**. Каждый такой литерал передаётся в
`allocator.forbid(...)`, чтобы реальному редактированию никогда не был назначен
токен, который пользователь уже ввёл дословно, — иначе литерал пользователя был
бы развёрнут в несвязанный оригинал на обратном проходе. Сегодня
`conversation_id == request_id`, поэтому коллизия остаётся в пределах одного
запроса, но это стало бы cross-context утечкой, если `conversation_id`
расширится (см. [`conversation-id.md`](conversation-id.ru.md)). При обнаружении
любого литерала `pre_call` логирует **не содержащий контента** маркер:

```
litellm_pre_call_input_placeholder_literal_detected request_id=… count=N
```

что также является сигналом зондирования санитайзера (см. §10).

### Защита по глубине (fail-closed)

Рекурсивный обход JSON ограничивает вложенность на `_MAX_JSON_DEPTH = 64`. На
пути **санитизации** превышение поднимает `ContentTooDeepError`, который
`pre_call` отображает в **`400 E_BAD_REQUEST`** («request content nesting too
deep») — то есть срабатывает **fail-closed**, никогда не пробрасывая контент,
который обходчик не смог полностью пройти. (Обратный обход/де-санитизация просто
прекращает спуск за пределом и возвращает значение как есть, поскольку к этому
моменту всё уже плейсхолдеры.)

## 4. Аудит-конвейер

Поток на запрос:

```
pre/post hooks build AuditEvent (audit/event.py — NEVER fields are not even
  constructible as attributes)
   ↓
AuditLogger.emit() → _serialize() → assert_no_never_fields()  [in-process gate]
   ↓
StdoutSink writes ONE JSON line to pod stdout (audit/sinks.py)
   ↓
Vector tails it  (prod: stdin source; demo: docker_logs source)
   ↓
parse JSON → NEVER-fields VRL gate (defense in depth)
   ↓
keep only AuditEvent-shaped records (have request_id AND redaction_count)
   ↓
reshape → sinks (Langfuse, S3; SIEM designed, see §6)
```

### ALWAYS-поля (эмитятся в каждой записи)

Точный набор из `audit/logger.py::_serialize`:

`timestamp`, `request_id`, `user_id`, `team_id`, `provider`, `model`,
`latency_ms`, `prompt_token_count`, `completion_token_count`,
`redaction_count`, `finding_label_counts`, `cache_a_hit`, `gateway_version`,
`status`.

> `gateway_version` инъецируется логгером (аргумент конструктора), а не
> переносится на `AuditEvent`. Отдельный `LangfuseSink._event_to_record` его
> **не** устанавливает, поэтому запись, поданная напрямую в этот sink (не через
> `AuditLogger`), не имеет `gateway_version`.

### CONDITIONAL-поля (присутствуют только когда применимо)

| Поле | Присутствует когда |
|---|---|
| `placeholder_list` | `redaction_count > 0` (уникальный + отсортированный список только строк-плейсхолдеров) |
| `error_code` | `status != "ok"` |
| `corp_llm_latency_ms` | был задействован путь corp-LLM |
| `pre_pass_latency_ms` | был задействован путь pre-pass |
| `audit_buffer_full` | присутствует сигнал буфера Vector |

См. [`audit-schema.md`](audit-schema.ru.md) для полной схемы и типов (это источник
истины по полям).

### Семантика счётчиков

- `redaction_count` = число **РАЗЛИЧНЫХ** секретов (по одному на каждый различный
  оригинал, подсчитанных как различные канонические плейсхолдеры в
  `_merge_into_state`) — **не** счётчик вхождений.
- `finding_label_counts` = гистограмма по семействам (`{"EMAIL": 2, "PERSON": 1}`),
  построенная `_label_counts` по различным плейсхолдерам, так что
  `sum(values) == redaction_count`.
- `placeholder_list` = различные токены-плейсхолдеры, `sorted(...)` — только
  строки-токены, **никогда** оригиналы.

## 5. Интеграция с Langfuse

Два пути кода производят **одинаковую** форму Langfuse:

- **Vector (по умолчанию в проде)** — трансформ `to_langfuse` в
  `helm/.../templates/configmap.yaml`, плюс demo-пайплайн
  `docker/demo-vector/vector.yaml`.
- **Внутрипроцессный `LangfuseSink`** — `audit/langfuse_sink.py`, для тестов,
  низкого объёма или debug-подов, которые отказываются от Vector.

Каждая аудит-запись отображается в **одно `trace-create` + одно
`generation-create`** событие, отправляемое POST на
`{base}/api/public/ingestion` (сверено с `langfuse_sink.py` `_records_to_batch` и
трансформом в configmap):

**Тело `trace-create`**

| Поле | Значение |
|---|---|
| `id` | `request_id` |
| `name` | `corp-llm-gateway` (внутрипроцессный sink) / `gateway-request` (demo Vector) |
| `userId` | `user_id` |
| `metadata` | `team_id`, `redaction_count`, `cache_a_hit`, `finding_label_counts`, `gateway_version`, `status`, `error_code` |
| `tags` | `["team:<team_id>", "provider:<provider>"]` |

**Тело `generation-create`**

| Поле | Значение |
|---|---|
| `model` | `model` |
| `usage` | `{input: prompt_token_count, output: completion_token_count, total: input+output, unit: "TOKENS"}` |
| `metadata` | `latency_ms`, `corp_llm_latency_ms`, `pre_pass_latency_ms` |

**Аутентификация и транспорт**

- HTTP **Basic**-аутентификация: `LANGFUSE_PUBLIC_KEY` : `LANGFUSE_SECRET_KEY`
  (`base64(public:secret)` в Python-sink; Vector `auth.strategy: basic` с теми же
  env-переменными).
- `POST {base}/api/public/ingestion`, `Content-Type: application/json`.
- Буфер (prod Vector langfuse sink): **диск, 1 GiB** (`max_size:
  1073741824`).

**КРИТИЧЕСКИЙ момент дизайна — только metadata, без контента.** Trace в Langfuse
хранит **только metadata**; текст промпта или ответа не отправляется. Тела
`trace-create`/`generation-create` несут счётчики токенов, латентности,
статистику редактирования и лейблы плейсхолдеров — никогда текст сообщений. Как
следствие, панели **Input / Output** трейса намеренно **ПУСТЫ**. В
аудит-хранилище нет оригиналов.

> Замечание по реализации: demo-пайплайн Vector устанавливает `metadata` трейса в
> целую аудит-запись (`metadata: audit`), что всё равно исключает оригиналы,
> потому что сама аудит-запись их никогда не содержит (NEVER-гейт).
> Внутрипроцессный sink и prod-configmap используют курируемое подмножество
> metadata выше.

**Чтение трейсов для безопасности.** Фильтруйте по тегу `team:<id>` /
`provider:<p>`; смотрите `redaction_count` + `finding_label_counts` +
`placeholder_list`. Помните: **пробный запрос закономерно показывает
`redaction_count = 0`** — отсутствие редактирований не есть отсутствие
активности.

## 6. Интеграция с SIEM

Гейт NEVER-полей существует в **двух** местах — внутрипроцессный
`assert_no_never_fields` (`audit/invariants.py`) и Vector VRL-фильтр (prod
`enforce_audit_schema`; demo `never_fields_gate`) — **эшелонированная защита**.
Запись, содержащая любой NEVER-ключ, **отбрасывается**; согласно плану M3-3 это
инкрементирует метрику `audit_drop`, которая **должна поднимать SIEM-алерт**
(M3-9).

Что SIEM должен мониторить (согласно плану M3-9):

| Сигнал | Значение |
|---|---|
| `audit_drop > 0` | NEVER-поле достигло аудит-конвейера — попытка утечки/регрессия; расследовать немедленно |
| Fail-closed `503`-и | `E_CORP_LLM_DOWN`, `E_REDIS_DOWN`, fail-closed S3/буфера Vector и т.д. (доступность + возможная атака) |
| `litellm_pre_call_input_placeholder_literal_detected` | Пользователь ввёл литералы `[LABEL_NNN]` — возможное зондирование санитайзера |
| Аномалии редактирования | Всплеск редактирований (3σ), отказы в обходе, всплески отказов аутентификации |

**NEVER_FIELDS** (точно, из `audit/invariants.py`; сравнение регистронезависимо и
трактует `-` как `_`, поэтому `X-Corp-Auth` / `Set-Cookie` матчатся):

```
mapping, mapping_table, pairs, original_content, unredacted_content,
pre_sanitization, replace_md, rule_values, x_corp_auth, corp_token,
authorization, cookie, set_cookie
```

**Текущий статус (2026-07-01).** Vector SIEM-sink **и** алерты
`AuditVectorDropHigh` / `LeakAttemptDetected` теперь **подключены** (CP-3:
`helm/.../templates/configmap.yaml` siem-sink + `templates/siem-alerts.yaml`,
через тот же VRL-гейт NEVER-полей). Остаётся **единственный** пункт —
подтвердить реальный SIEM-эндпоинт (open question #3); настроенный эндпоинт пока
placeholder. Актуальный статус — в `docs/requirements-compliance.md` (R15).

## 7. Долговременное аудит-хранилище S3

Prod Helm `s3`-sink (`templates/configmap.yaml`, питается от выхода
`enforce_audit_schema` после гейта):

| Настройка | Значение |
|---|---|
| Type | `aws_s3` |
| Bucket | `values.audit.sinks.s3.bucket` → `corp-audit` |
| Префикс ключа | `{{ team_id }}/dt=%Y-%m-%d/` (по командам, партиционировано по дате) |
| Сжатие | `gzip` |
| Кодирование | `json` |
| Буфер | диск, **5 GiB** (`max_size: 5368709120`) |

S3 — **долговременный** sink и работает **fail-closed** (§8). Retention по
командам генерируется `audit/retention.py` (`lifecycle_configuration`): одно S3
lifecycle-правило на команду, ограниченное префиксом `<team_id>/`, переходящее в
GLACIER после `retention_hot_days` и истекающее через `+ retention_cold_years *
365` дней.

## 8. Fail-policy матрица

Из `helm/.../values.yaml` `failPolicy` (**M4-матрица** плана — **источник
истины** — не добавляйте ad-hoc fail-open пути):

| Компонент | Поведение |
|---|---|
| `corpLlmDown` | **fail-closed** (503) |
| `prePassDown` | **продолжить** (только corp-LLM; метрика инкрементится) |
| `redisClusterDown` | **fail-closed** (503) |
| `postgresDown` | **fail-closed** (503) |
| `vectorBufferFull` | **fail-closed** (503) по умолчанию; команда может выбрать `audit_buffer_full=continue` |
| `s3SinkDown` | **fail-closed** (503) — S3 это долговременный sink |

См. §M4 плана для полной матрицы (ретрай при транзиентном сбое Redis,
проваливание при промахе Cache A/C, отказ одного аудит-sink) и колонок per-team
override.

## 9. Инварианты — никогда не ослабляйте их

| ID | Инвариант | Обеспечивается |
|---|---|---|
| M1-14 | **Оригиналы не утекают** через шесть поверхностей: (i) эмиссии логгера, (ii) тела ошибок, (iii) трейсы исключений, (iv) лейблы метрик, (v) пробрасываемые заголовки, (vi) stdout пода | `tests/invariants/test_no_originals_leak.py` |
| M2-7 | **Никаких BYOK-кредов в аудите**: значение `Authorization` никогда не появляется ни на одной аудит-поверхности | тот же корпус тестов + NEVER-гейт |
| M3-10 | **Vector отбрасывает NEVER**: внедрённая запись с NEVER-ключом не достигает ни одного sink | интеграционная проверка |
| M1-9 | **Подстановка по убыванию длины** (прямая + обратная) | `placeholder.py`, `litellm_hook.py` |
| — | **Per-request биекция плейсхолдеров** (один оригинал → один токен; разные оригиналы → разные токены) | `placeholder_allocator.py` |
| — | **Depth-guard fail-closed** (`_MAX_JSON_DEPTH=64` → `400 E_BAD_REQUEST` при санитизации) | `content_blocks.py`, `litellm_hook.py` |
| — | **NEVER-гейт, внутрипроцессный + Vector** (эшелонированная защита) | `audit/invariants.py` + Vector VRL |

## 10. Форензик-следы (расследование инцидента)

Куда смотреть в первую очередь и что каждый след может и не может рассказать:

| След | О чём говорит | Никогда не содержит |
|---|---|---|
| `finding_label_counts` | Какие ВИДЫ секретов были отредактированы (гистограмма по семействам) | Любой текст |
| `placeholder_list` | КАКИЕ токены были выпущены (`[EMAIL_001]`, …) | Оригиналы |
| `redaction_count` | Сколько РАЗЛИЧНЫХ секретов | — |
| `litellm_pre_call_input_placeholder_literal_detected` (лог) | Пользователь ввёл литералы `[LABEL_NNN]` — возможное зондирование; **не содержит контента** (только счётчик) | Текст литерала |
| Логи жизненного цикла pre/post (`litellm_pre_call_*`, `litellm_post_call_*`, `litellm_audit_emitted`) | Поток на запрос, размеры в байтах, суммарные редактирования, латентности | Тела контента |

Маркеры отложенных пробелов в коде (ищите их, чтобы подтвердить, что поведение —
известный пробел, а не регрессия): комментарии `SECURITY` в
`sanitizer/content_blocks.py` (потоковая де-санитизация tool_use,
thinking/redacted_thinking, бинарные/url источники document) и заметка памяти
`project_tool_use_input_unsanitized`.

**Во время инцидента:** начинайте с долговременного хранилища S3 (по командам,
партиционировано по дате) и Langfuse (фильтр по тегу `team:`/`provider:`).
Убедитесь, что `audit_drop` равен нулю — ненулевое значение означает, что
NEVER-поле достигло конвейера, и это первое, за чем гнаться. Ни одна из этих
поверхностей не может содержать оригинал по построению; если кажется, что
содержит, — это регрессия M1-14.

## 11. Известные пробелы / доработки

| # | Пробел | Серьёзность |
|---|---|---|
| (a) | ~~В prod Helm `templates/configmap.yaml` был ДУБЛИРУЮЩИЙ ключ `transforms:`, который отбрасывал `parse` + `enforce_audit_schema`.~~ **ИСПРАВЛЕНО** — теперь один блок `transforms:` (`parse` → `enforce_audit_schema` → `audit_only` → `to_langfuse`); `parse` нестрогий (терпит plain-text строки uvicorn), фильтр `audit_only` держит не-аудитные события вне обоих sink-ов, а NEVER-гейт на стороне Vector теперь зеркалит полный внутрипроцессный список (13 ключей + варианты регистра `-`/`_`). `langfuse` ← `to_langfuse`, `s3` ← `audit_only`. | **Решено** |
| (b) | **SIEM-sink включён в values, но не определён в configmap.** У `audit.sinks.siem.enabled: true` нет соответствующего `sinks.siem` в Vector-configmap; алертинг по `audit_drop` (M3-9) также в ожидании. | **Средняя** — SIEM-мониторинг (включая алерты попыток утечки) ещё не активен |
| (c) | ✅ **ИСПРАВЛЕНО** — потоковый `tool_use` `input_json_delta` теперь де-санитизируется (JSON-экранированный) в `sanitizer/streaming.py`, так что инструмент разработчика получает реальные значения, а не токены `[LABEL_NNN]`. | **Решено** |
| (d) | ✅ **By design (не пробел)** — `thinking` / `redacted_thinking` проходят БЕЗ ИЗМЕНЕНИЙ: Anthropic подписывает thinking-блоки и отклоняет изменённые при multi-turn-переигрывании, а модель в любом случае видит только плейсхолдеры (до неё не доходит ни один оригинал). | **Решено (by design)** |

**(a) и (c) исправлены; (d) корректно by design.** Единственный оставшийся
открытый пункт — **(b)** — подключение SIEM-sink (зависит от SIEM-таргета). См.
[`remaining-steps.md`](remaining-steps.md).
