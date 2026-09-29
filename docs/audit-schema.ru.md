# Схема полей аудита

Источник истины по тому, что шлюз выдаёт в свой конвейер аудита.
Ссылка на план: M3-0. Читать вместе с: `docs/plans/20260507-external-sanitizer-gateway-v1.md`
и `docs/security.md` (поток конвейера, sink-и Langfuse/SIEM/S3, инварианты).

Шлюз выдаёт одну JSON-запись на каждый запрос (M3-1). Для запроса, прошедшего
pre-call, это итоговая запись запроса (ниже).
Vector (M3-3) парсит каждую запись и проверяет правила `NEVER` — любая запись,
содержащая поле `NEVER`, отбрасывается, а метрика `audit_drop` инкрементируется
(SIEM-алерт заведён в M3-9).

## Поля ALWAYS

Эти поля присутствуют в каждой выдаваемой записи. Отсутствует → запись
некорректна, и Vector её отбрасывает.

| Поле | Тип | Описание |
|---|---|---|
| `timestamp` | string (RFC3339, UTC) | Когда запись была написана |
| `request_id` | string (uuidv7) | Стабильный id на запрос; переживает стриминг |
| `user_id` | string | Определяется из токена `X-Corp-Auth` (M2-2) |
| `team_id` | string | Определяется из токена; управляет правилами по команде + retention |
| `provider` | string | `anthropic` или `openai` |
| `model` | string | Определяется из тела запроса к апстриму |
| `latency_ms` | int | Реальное время в миллисекундах; для итоговой записи — от начала pre-call до момента, когда записан её исход (конец ответа или закрытие тикета) |
| `prompt_token_count` | int | Из `usage` в ответе провайдера (ASGI-десанитайзер читает его по мере прохождения ответа, или его добавляет success-лог litellm, пока запись открыта); `0`, если не сообщил ни один |
| `completion_token_count` | int | Тот же источник, что у `prompt_token_count` |
| `redaction_count` | int | Число РАЗЛИЧНЫХ секретов, вымаранных в запросе (по одному на каждый уникальный оригинал — НЕ число вхождений) |
| `finding_label_counts` | object\<string, int\> | Формат `{"EMAIL": 2, "PERSON": 1}`; только гистограмма меток — без текста; всегда заполнено; `sum(values) == redaction_count` |
| `cache_a_hit` | bool | Попал ли запрос в кэш дедупликации |
| `gateway_version` | string | Версия приложения, обработавшего запрос |
| `status` | string | `ok` / `failed` / `degraded` / `cancelled`. `cancelled`: запрос закончился раньше, чем ответ, — `error_code` `E_CLIENT_DISCONNECTED` (клиент ушёл) или `E_SERVER_SHUTDOWN` (запрос отменил сервер, например при остановке); только счётчики, без `placeholder_list`. Как решается статус итоговой записи — ниже |

## Поля NEVER

Эти ключи НИКОГДА не должны появляться ни в одной выдаваемой записи. Они
проверяются структурно через Vector VRL (M3-3); их наличие считается
регрессией, и запись отбрасывается.

| Запрещённый ключ | Почему |
|---|---|
| `mapping` / `mapping_table` / `pairs` | Раскрывает пары оригинал ↔ placeholder |
| `original_content` / `unredacted_content` / `pre_sanitization` | Payload до санитизации |
| `replace_md` / `rule_values` | Значения правил по команде могут содержать регулируемые термины |
| `x_corp_auth` / `corp_token` / любой вариант регистра | Учётные данные аутентификации шлюза |
| `api_key` | Учётные данные провайдера (ключ Anthropic/OpenAI/корп-vLLM) |
| `authorization` / любое имя заголовка `*-bearer-*` | BYOK-ключ разработчика (ключ Anthropic/OpenAI) |
| `cookie` / `set_cookie` | Внеполосный аутентификационный материал |
| `extra_headers` | Произвольные заголовки от вызывающей стороны, которые могут содержать учётные данные |

Список распространяется на любой ключ, чьё имя намекает на учётные данные или
невымаранное содержимое. VRL-трансформ Vector использует явный allow-list
(таблица ALWAYS выше) — всё, чего в нём нет, отбрасывается, так что список
NEVER — это предохранитель эшелонированной защиты (defense-in-depth),
а не единственная линия обороны.

## Поля CONDITIONAL

Присутствуют только при указанных условиях; иначе отсутствуют.

| Поле | Условие | Описание |
|---|---|---|
| `placeholder_list` | `redaction_count > 0` | Только уникальный отсортированный список строк-placeholder (напр. `["[EMAIL_001]", "[NAME_002]"]`) — НИКОГДА не включает оригиналы |
| `error_code` | `status != "ok"` | Стабильный код ошибки; без текста исключения. Среди них: коды route gate `E_ROUTE_BLOCKED`, `E_ROUTE_GATE_UNARMED`, `E_ROUTE_GATE_ERROR`; коды лимита одновременных запросов `E_CAPACITY`, `E_BODY_TIMEOUT`, `E_OVERSIZE_BLOCKED`; `E_CLIENT_DISCONNECTED` и `E_SERVER_SHUTDOWN` (со `status` `cancelled`); `E_STORE_UNAVAILABLE` (хранилище токенов не ответило); `E_PROFILE_UNAVAILABLE` (сломанный профиль или хранилище конфигурации команд, которое не ответило). Маршрут выдачи токенов аудит-записей не пишет |
| `block_reason` | Сработала точка блокировки | Короткий код причины отказа. Stage 0 (контент-политика): `config:env`, `config:kube`, `config:nginx`, `config:ini`, `log:dump`. Stage 5 (DLP-гейт на выходе): `dlp:canary`, `dlp:secret_leak`. Размер контента и политика запроса: `oversize:blocked`, `request:ambiguous_shape`, `provider:not_allowed`. Route gate (до роутера litellm, `route_gate/classify.py`): `route_gate_listed` (таблица отклоняет этот маршрут), `route_gate_unlisted` (записи в таблице нет), `route_gate_websocket` (рукопожатие на любом пути), `route_gate_malformed` (закодированный или обходящий путь), `route_gate_unarmed` (guardrail-callback не зарегистрирован) и `route_gate_error` (гейт не смог классифицировать). Лимит одновременных запросов (после того как route gate допустил переписываемый маршрут, `route_gate/inflight.py`): `capacity` (заняты все слоты `CORP_LLM_MAX_INFLIGHT` пода или все места чтения тела `CORP_LLM_MAX_DRAINING`, либо тело превысило бы бюджет `CORP_LLM_MAX_DRAINING_BYTES`; 429 `E_CAPACITY`) и `body_timeout` (тело не пришло целиком за `CORP_LLM_BODY_READ_SECONDS`; 408 `E_BODY_TIMEOUT`); тело больше 25 MiB там — `oversize:blocked`, полное тело с ключом `policies` верхнего уровня — `route_gate_body_policies` (403 `E_ROUTE_BLOCKED`, до того как litellm его разберёт), а тело не в UTF-8 JSON (не `application/json`, `charset` не `utf-8`, BOM, UTF-16/32) — `route_gate_body_not_json` (415 `E_ROUTE_BLOCKED`). Никогда не содержит сырое содержимое payload, путь или заголовок. |
| `corp_llm_latency_ms` | Был выбран путь через корп-LLM | Латентность под-стадии для тюнинга ёмкости |
| `pre_pass_latency_ms` | Был выбран путь pre-pass | Латентность под-стадии |
| `audit_buffer_full` | Буфер Vector на ≥50% | Эксплуатационный сигнал |

## Итоговая запись

Запрос, прошедший pre-call, получает ровно одну запись. Её пишет
`route_gate/terminal_audit.py` из фактов без контента, которые pre-call оставляет
на тикете запроса, — никогда не callback litellm. Запрос, отклонённый route gate,
лимитером или pre-call, сохраняет запись, написанную при отказе. Обоснование —
[`security.ru.md`](security.ru.md) §15.

| `status` | `error_code` | Когда |
|---|---|---|
| `ok` | — | финальное тело 2xx-ответа ушло (восстановленным, если в запросе были плейсхолдеры) |
| `failed` | код pre-call или нет | не-2xx ответ, поток, в котором было событие ошибки, или исключение приложения litellm посреди ответа |
| `failed` | `E_INTERNAL` | ASGI-десанитайзер не смог восстановить ответ (плюс `gateway_failure{component="desanitize"}`), или запрос закончился без финального тела, и никто его не отменял |
| `cancelled` | `E_CLIENT_DISCONNECTED` | клиент ушёл до конца ответа |
| `cancelled` | `E_SERVER_SHUTDOWN` | запрос отменил сервер (остановка, drain пода) |

Приоритет, побеждает первое совпадение: сбой восстановления стоит, что бы ни
случилось потом; исход, опубликованный в конце ответа, стоит против более поздней
отмены (клиент получил ответ), кроме опубликованного после ухода клиента — это
`cancelled`; иначе решает закрытие тикета. Неудачная запись пути ответа
повторяется один раз — закрытием, с тем же исходом; у записи, исход которой решает
закрытие (ничего не опубликовано: `cancelled` или `failed` + `E_INTERNAL` без
финального тела), одна попытка и нет повтора; запись, которая могла дойти, не
повторяется; запись, потерянная
после последней попытки, логируется
(`gateway_terminal_audit_lost request_id=… outcome=… error=<type>`) и считается в
`gateway_failure{component="desanitize"}`.

## `guardrail_information` litellm (не поле этой записи)

Запись выше — терминальная аудит-запись запроса; поля `guardrail_information` в ней
нет. Это поле собственного `StandardLoggingPayload` litellm, который получает любой
логгер из `litellm.callbacks` (Langfuse, S3, OTEL, …). Pre-call guardrail пишет туда
одну запись собственным writer'ом litellm и синхронизирует её в logging-объект litellm.
Запись проходит allow-list (`audit/invariants.py`,
`assert_guardrail_information_allowed`): ключ NEVER или любой свободный текст
отклоняются до того, как litellm их увидит, а запись, которую litellm собрал иначе,
удаляется из метаданных запроса. До OTEL guardrail span litellm это удаление не
доходит: writer litellm уже отправил его (`emit_guardrail_span`, litellm 1.101.0
`custom_guardrail.py:1209-1217`) до того, как шлюз проверил собранную им запись, так
что этот span может нести ключи, которые litellm сгенерировал вокруг нашей записи из
allow-list. В обоих случаях запрос продолжается; шлюз пишет в лог
`litellm_guardrail_information_failed request_id=… error=<type>` и считает
`gateway_failure{component="audit"}`.

| Ключ | Значение |
|---|---|
| `guardrail_name` | `corp-llm-sanitizer` |
| `guardrail_mode` | `pre_call` |
| `guardrail_status` | выводится из `block_reason`, таблица ниже |
| `start_time` / `end_time` / `duration` | начало и конец pre-call (секунды epoch) и его длительность в секундах |
| `guardrail_response` | `redaction_count` и `finding_label_counts` (как в этой записи), плюс `block_reason`, если он задан |
| `guardrail_provider` / `masked_entity_count` | всегда `null` (litellm пишет оба ключа) |

`block_reason` → `guardrail_status`. Источник — наши коды причин; статус litellm
выводится из них, никогда не наоборот:

| `block_reason` | `guardrail_status` |
|---|---|
| нет | `success` |
| `oversize:delivered` | `guardrail_flagged` |
| любая причина Stage 0, Stage 5 и политики размера / запроса | `guardrail_intervened` |

Route gate и лимит одновременных запросов отказывают до того, как запускается litellm:
записи нет.

Где видна запись: в success- и failure-payload запроса, который pre-call пропустил
(статус `success` или `guardrail_flagged` для `oversize:delivered`). Для запроса,
который pre-call отклонил, litellm payload не строит; тогда запись попадает только в
запрос, который litellm передаёт каждому `async_post_call_failure_hook`, и в OTEL
guardrail span litellm, если OTEL настроен. В тело запроса к провайдеру она не попадает
никогда.

Pre-call, который падает, а не отклоняет запрос, — 401 `E_PROVIDER_AUTH`, 503
`E_NER_UNAVAILABLE`, `E_STORE_UNAVAILABLE` или `E_PROFILE_UNAVAILABLE`, `E_BAD_REQUEST`
— выбрасывает исключение до записи: записи нет, и статус litellm
`guardrail_failed_to_respond` не используется.

## Инварианты

Проверяются в коде:

1. **Нет оригиналов (M1-14)**: на всём тестовом корпусе оригиналы не должны появляться в сериализованной форме ни одной записи аудита.
2. **Нет учётных данных (M2-7)**: значение заголовка BYOK Authorization не должно появляться ни в одной записи аудита.
3. **Vector отбрасывает по NEVER (M3-10)**: подсунутая запись с ключом NEVER не должна достичь Langfuse, S3 или SIEM.
4. **Полнота аудита (критерий приёмки)**: 100% неупавших запросов появляются в S3 в течение 24 ч; измеряется ежемесячно.
5. **`guardrail_information` без контента**: payload litellm, его failure-хуки и его OTEL span несут нашу запись ровно с ключами выше и без оригинала, placeholder или текста исключения (`tests/litellm_hook/test_guardrail_information.py`, `tests/audit/test_guardrail_information_gate.py`).

## Версионирование схемы

Записи несут неявную версию, равную `gateway_version`. Добавление полей —
не ломающее изменение; удаление полей требует поднятия мажорной версии и плана
миграции, согласованного с аудиторами.
