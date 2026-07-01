# `X-Corp-Auth` — как корп-токен реально перемещается

Как корп-токен попадает из Keycloak в сеть и в auth-middleware шлюза.
Это дополняет [`harness-integration.md`](harness-integration.md), который
отвечает на «какой паттерн я использую для каждого харнесса». Этот документ —
более глубокий «что читается, когда, и что харнесс отправляет в сеть».

## TL;DR

- **Заголовок отправляется в каждом HTTP-запросе** — так работают кастомные
  HTTP-заголовки, харнесс прикрепляет его к каждому вызову.
- **Значение не перечитывается с диска на каждый запрос** в дефолтной
  конфигурации Claude Code. Оно читается один раз при старте шелла (Паттерн 1)
  или один раз при старте харнесса (Паттерн 2). После ротации нужен новый
  шелл — *или* используйте localhost-прокси (Паттерн 3), который
  *действительно* перечитывает файл на каждый запрос.

## Полный жизненный цикл

```
[Keycloak device flow]   ──issue──►   ~/.corp-llm-gateway/token  (chmod 600, 30-day TTL)
        │                                        │
   install.sh                              read by ONE of:
                                                 │
                       ┌─────────────────────────┼──────────────────────────┐
                       ▼                         ▼                          ▼
            Pattern 1 (env var)        Pattern 2 (config.toml)     Pattern 3 (localhost proxy)
            $(cat ...) at rc init       static value, edited        re-read per request
                       │                         │                          │
                       ▼                         ▼                          ▼
         export ANTHROPIC_CUSTOM_HEADERS    [default.headers]    proxy injects header
         "X-Corp-Auth: <token>"             X-Corp-Auth = "..."  on each forwarded request
                       │                         │                          │
                       └─────────────────────────┼──────────────────────────┘
                                                 ▼
                       Harness HTTP client adds X-Corp-Auth to every request
                                                 │
                                                 ▼
                                gateway.corp.lan / LiteLLM proxy
                                                 │
                                                 ▼
                       AuthMiddleware.authenticate_headers(headers)
                          → ctx{user_id, team_id}                  (uses token)
                       AuthMiddleware.strip_corp_token(headers)
                          → header removed before egress           (never logged, never forwarded)
```

## Что где хранится

| Артефакт | Путь | Режим | Устанавливает |
|---|---|---|---|
| Корп-токен (30-дневный) | `~/.corp-llm-gateway/token` | `0600` | `install.sh` (Keycloak device flow → обмен на `/internal/issue-token`) |
| Указатель на файл токена | `$CORP_GATEWAY_TOKEN_FILE` | env | `install.sh` пишет его в ваш rc-файл |
| Привязка заголовка (Паттерн 1) | `$ANTHROPIC_CUSTOM_HEADERS` | env | rc-блок `install.sh`, вычисляется через `$(cat …)` при инициализации шелла |

`install.sh` идемпотентно переписывает rc-блок между маркерами
`# >>> corp-llm-gateway >>>` — повторный запуск ротирует токен *и* rc-блок.

## Свежесть по паттернам

| Паттерн | Когда читается файл токена | Когда отправляется заголовок | Эффект ротации |
|---|---|---|---|
| **1 — env-переменная** (Claude Code) | один раз, при инициализации шелла (`$(cat …)` делает снимок) | каждый запрос (HTTP-заголовок по умолчанию) | после ротации откройте новый шелл; старые шеллы используют старое значение до перезапуска |
| **2 — `config.toml`** (Codex) | один раз, при старте харнесса (парсинг TOML) | каждый запрос | перезапустите харнесс; для автоматической ротации перезапустите `install.sh`, чтобы переписать файл |
| **3 — localhost-прокси** | каждый запрос (`_read_token` вызывается внутри обработчика запроса — см. `cli/proxy.py:71-81`) | каждый запрос | вступает в силу уже на следующем запросе, без перезапуска |

Соответствующий код прокси:

```python
# src/corp_llm_gateway/cli/proxy.py
def _handle(self) -> None:
    try:
        corp_token = _read_token(self.token_file)   # ← per request
    except FileNotFoundError:
        self._send_error(401, "corp token file not found; run install.sh")
        return
    ...
    headers["X-Corp-Auth"] = corp_token             # ← injected fresh
```

## Что Claude Code реально делает в сети

Claude Code **не** переоценивает `$ANTHROPIC_CUSTOM_HEADERS` на каждое
сообщение. Харнесс читает его один раз при инициализации HTTP-клиента и
регистрирует разобранные строки `Header: Value` как заголовки по умолчанию.
Каждый последующий вызов `/v1/messages` несёт их в составе запроса —
включая переподключения в рамках потоковой сессии.

Вот почему Паттерну 1 нужен свежий шелл после ротации токена: работающая
пара шелл-и-харнесс держит старый снимок в памяти процесса, даже если файл
токена на диске уже изменился.

## Что шлюз делает с заголовком

```python
# pre_call (litellm_hook.py)
ctx = await self._auth.authenticate_headers(_extract_headers(data))
data["headers"] = self._auth.strip_corp_token(_extract_headers(data))
```

1. `authenticate_headers` валидирует токен, определяет user_id и team_id и
   бросает типизированное исключение при сбое (`MissingTokenError`,
   `ExpiredTokenError`, `RevokedTokenError`, `InvalidTokenError`). Каждое
   маппится в стабильный `error_code`, записываемый в аудит
   (`E_MISSING_TOKEN`, `E_TOKEN_EXPIRED`, `E_TOKEN_REVOKED`,
   `E_TOKEN_INVALID`, `E_AUTH`).
2. `strip_corp_token` удаляет заголовок из словаря, который будет проброшен
   на апстрим. Токен никогда не достигает Anthropic / OpenAI и никогда не
   появляется в конвейере аудита (инвариант #4).

`Authorization: Bearer <byok-key>` разработчика *не* трогается — он проходит
на апстрим нетронутым (инвариант #3).

## Типичные сбои

| Симптом | Причина | Решение |
|---|---|---|
| `401 E_MISSING_TOKEN` | шелл не был перезапущен, или `ANTHROPIC_CUSTOM_HEADERS` не установлен | откройте новый шелл или `source ~/.zshrc`; проверьте через `echo $ANTHROPIC_CUSTOM_HEADERS` |
| `401 E_TOKEN_EXPIRED` | токен старше 30 дней, шелл держит старое значение | перезапустите `install.sh` (ротирует), затем откройте новый шелл |
| `401 E_TOKEN_REVOKED` | админ отозвал токен (распространение ≤60 с) | перезапустите `install.sh` для повторной аутентификации через Keycloak |
| файл токена отсутствует | установщик не запускался или файл удалён | `install.sh` |

Для пользователей Паттерна 3 совет «откройте новый шелл» не применим —
прокси перечитывает на каждом запросе, так что ротация вступает в силу на
следующем вызове.

## Эксплуатационные гарантии

- Файл токена имеет режим `0600` (`install.sh` выставляет его явно).
- Токен никогда не покидает ноутбук (Паттерн 1, 2) или localhost-прокси
  (Паттерн 3) ни в какой форме, кроме заголовка `X-Corp-Auth`.
- Шлюз никогда не логирует `X-Corp-Auth` — закреплено в
  `tests/invariants/test_no_originals_leak.py`.
- Прокси в Паттерне 3 игнорирует `HTTP_PROXY` и родственные
  (`ProxyHandler({})` в `cli/proxy.py:32`), так что токен нельзя
  перенаправить через системный прокси.

## Быстрая проверка: доходит ли мой заголовок до шлюза?

```bash
echo "$ANTHROPIC_CUSTOM_HEADERS"
# → X-Corp-Auth: ct_xxxxxxxxxxxxxxxxxxxxxx

cat "$CORP_GATEWAY_TOKEN_FILE"
# → ct_xxxxxxxxxxxxxxxxxxxxxx   (must match)

corp-llm-gateway status
# → Validates the token round-trip against the gateway's /healthz/auth.
```

Если `echo $ANTHROPIC_CUSTOM_HEADERS` пуст после ротации токена — это
проблема «протухшего шелла», новый шелл её исправит.
