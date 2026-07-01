# Идентичность диалога (`conversation_id`)

Как шлюз сегодня ограничивает состояние на уровне диалога и что
потребуется, чтобы ограничивать его в рамках реальной многошаговой
сессии.

## TL;DR

- `conversation_id` сегодня **не** читается ни из какого входящего
  заголовка или поля тела.
- Он фабрикуется внутри шлюза, заново на каждый HTTP-запрос:
  `conversation_id == request_id == uuid4()`.
- Это значит, что Cache A (дедуп по содержимому) работает, но Cache B
  (хранилище маппингов на уровне диалога) пишется и никогда не
  переиспользуется между запросами. Многошаговое восстановление *одной и
  той же* привязки original→placeholder между родственными запросами не
  происходит.

## Где он задаётся

`src/corp_llm_gateway/litellm_hook.py`:

```python
@staticmethod
def _ensure_request_id(data: dict[str, Any]) -> str:
    rid = data.get("_corp_gateway_request_id")
    if isinstance(rid, str) and rid:
        return rid
    rid = str(uuid.uuid4())
    data["_corp_gateway_request_id"] = rid
    return rid
```

```python
result = await self._orch.sanitize(
    content,
    team_id=ctx.team_id,
    conversation_id=request_id,   # same UUID, just renamed
)
```

`_corp_gateway_request_id` — внутренний служебный ключ, который хук
выставляет сам; он позволяет pre/post в рамках *одного и того же*
HTTP-запроса согласовать ID. Ничто не читает его из `headers`,
`metadata`, `proxy_server_request` или `litellm_session_id` от LiteLLM.

## Ключи кэшей

В `src/corp_llm_gateway/storage/mapping.py` определены два кэша:

| Кэш | Ключ | Назначение | Статус сегодня |
|---|---|---|---|
| **A — дедуп** | `sha256(team_id + rules + text)` | переиспользовать маппинг, когда один и тот же текст повторяется между запросами | ✅ работает — производный от содержимого, не от диалога |
| **B — на диалог** | `(conversation_id, original) ↔ placeholder` | сохранять `[EMAIL_001]` стабильным для одного и того же оригинала на всех шагах одного диалога | ⚠️ инертен — каждый запрос получает свежий `conversation_id`, поэтому записи никогда не читаются родственными запросами |

Десанитизация на post-call сейчас опирается на внутрипроцессный
`_RequestState.mapping`, а не на Cache B, поэтому инертность незаметна
*внутри* одного запроса. Она важна только между запросами в рамках одной
сессии.

## Конкретное следствие

```
Turn 1:  "email me at alice@corp.example"
         → sanitized: "email me at [EMAIL_001]"
         → Cache B[(uuid-A, "alice@corp.example")] = "[EMAIL_001]"

Turn 2:  "send the recap to alice@corp.example"     ← same string, new HTTP request
         → conversation_id = uuid-B  (fresh)
         → Cache B miss for (uuid-B, "alice@corp.example")
         → goes to Cache A, hits on content-hash
         → still gets "[EMAIL_001]" because the text hash is stable
         → so today, *placeholder stability* survives via Cache A even though Cache B is inert
```

Где это ломается: когда между шагами текст *перефразирует* одну и ту же
сущность («Alice's email» против «alice@corp.example»). Cache A ключуется
по сырым байтам, поэтому дедупа не будет. Cache B *смог бы* — если бы
существовал общий `conversation_id`, под которым можно найти оба
оригинала.

## Как подключить настоящий conversation ID

Три правдоподобных источника, ни один пока не используется:

1. **Метаданные запроса в стиле Anthropic.** Claude Code может заполнять
   `metadata.user_id` (или соседнее поле) на сессию. Pre_call читал бы
   `data["metadata"]["user_id"]` (или что там harness согласится
   отправлять) и предпочитал бы его UUID-фолбэку.
2. **Заголовок от установщика harness.** `install.sh` мог бы генерировать
   стабильный per-session ID и заставлять harness отправлять
   `X-Conversation-Id`. Localhost-прокси из `docs/harness-integration.md`
   (Pattern 3) — естественное место для его инъекции.
3. **Session ID от LiteLLM.** LiteLLM отдаёт `litellm_session_id` в
   `kwargs` для proxy-колбэков. Pre_call мог бы читать его через
   `data.get("litellm_session_id")`, если прокси настроен его
   пробрасывать.

Какую бы дорожку ни выбрали, изменение локально в `_ensure_request_id`
(или, чище, в соседнем `_resolve_conversation_id`, который откатывается к
UUID запроса, когда сверху ничего не приходит). `request_id` и
`conversation_id` в этот момент должны стать **отдельными полями** — один
идентифицирует HTTP-вызов, другой — сессию.

## Соображения приватности при подключении

- `conversation_id` становится ключом связывания (join key) между
  записями аудита. В него не должны быть зашиты ПДн (ни сырого email
  пользователя, ни IP). Случайный per-session UUID, сгенерированный
  harness, — безопасная форма.
- В аудите он разрешён (это join key, а не NEVER-поле), но при добавлении
  его нужно явно задокументировать, чтобы дашборды SIEM могли по нему
  разворачиваться.
- TTL у Cache B скользящий (`cache_b_ttl_seconds`, по умолчанию 1 ч).
  Долгоживущая сессия, ушедшая в простой, при возобновлении начнёт с
  чистого листа — это by design.

## Что изменится, когда это появится

- Cache B начинает окупаться → меньше вызовов корп-LLM в долгих сессиях
  со стабильными сущностями.
- Маппинги сохраняются при естественном разговорном перефразировании, а
  не только при буквальных дубликатах.
- Никаких изменений в инвариантах M1-14. `conversation_id` не является
  NEVER-полем; оригиналы/плейсхолдеры/учётные данные — по-прежнему
  являются.

## Статус

Не на критическом пути v1. Cache A достаточно для критерия успеха
запуска. Отслеживать как follow-up для v1.1, если ранние продовые данные
покажут, что многошаговое перефразирование — значимый источник пропусков.
