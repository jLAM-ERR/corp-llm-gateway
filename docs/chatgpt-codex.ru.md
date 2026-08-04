# Профиль ChatGPT Codex

Этот профиль позволяет Codex обращаться к `corp-llm-gateway` через OpenAI
Responses API, а шлюзу использовать OAuth-аутентификацию действующей подписки
ChatGPT. API-ключ OpenAI для профиля не нужен.

Поток запроса:

```text
Codex (ChatGPT OAuth)
  -> http://127.0.0.1:4000/v1/responses
  -> corp-llm-gateway: policy + анонимизация
  -> https://chatgpt.com/backend-api/codex/responses
  -> обратная замена в Responses/SSE
  -> Codex
```

## Что требуется

- Docker Desktop с Compose.
- Рабочий `CORP_LLM_ENDPOINT` в `.env.demo`: это вспомогательная модель
  анонимизации, а не внешняя ChatGPT-модель.
- Выполненный вход Codex в ChatGPT: `codex login status`. При необходимости
  выполните `codex login` и выберите вход через ChatGPT.

OAuth access token и `ChatGPT-Account-Id` берет сам Codex. Не переносите
`~/.codex/auth.json` в контейнер и не добавляйте его в `.env.demo`.

## Запуск шлюза

```bash
cd /path/to/corp-llm-gateway

docker compose \
  -f docker-compose.demo.yml \
  -f docker-compose.chatgpt-codex.yml \
  up -d --build redis postgres litellm

curl -fsS http://127.0.0.1:4000/health/liveliness
```

Overlay включает `CORP_LLM_FORWARD_CHATGPT_AUTH=1`, маршрутизирует выбранную
Codex модель как `openai/*` в ChatGPT Codex backend и оставляет основной GLM
demo-профиль без изменений.

## Установка профиля Codex

```bash
cp docker/chatgpt-codex/chatgpt-codex.config.toml \
  ~/.codex/chatgpt-codex.config.toml
```

Запуск:

```bash
codex --profile chatgpt-codex
```

Проверка одним запросом:

```bash
codex exec --profile chatgpt-codex \
  --skip-git-repo-check \
  "Ответь одним словом: работает?"
```

Профиль использует:

- `wire_api = "responses"`;
- `requires_openai_auth = true`, поэтому Codex добавляет OAuth Bearer и ID
  ChatGPT-аккаунта;
- `X-Corp-Auth = "demo-team-token"` для локального demo auth;
- SSE вместо WebSocket, поскольку шлюз выполняет обратную замену в потоке.

## Что изменено в шлюзе

- `input`, `instructions`, `input_text`, function-call arguments и tool output
  проходят тот же fail-closed pipeline, что и `messages`.
- В upstream передается только разрешенный набор Codex-заголовков. Внутренний
  `X-Corp-Auth` удаляется до внешнего запроса.
- `response.output_text.delta`, terminal Responses events и tool arguments
  деанонимизируются, включая placeholder, разделенный между SSE-чанками.
- Профиль отклоняет запрос с `401 E_PROVIDER_AUTH`, если Codex не прислал
  корректный Bearer OAuth.

## В продакшене

`CORP_LLM_FORWARD_CHATGPT_AUTH` — обычный флаг из `settings.KEYS`:
`bootstrap.build_guardrail()` резолвит его из окружения или TOML-конфига,
как и любую другую настройку шлюза (см. [`config.example.toml`](../config.example.toml)),
а не только из demo-overlay compose выше. Установка через Helm value / env
var в k8s включает тот же header bridge на реальном кластере — без изменений
кода.

Где бы вы его ни включали, действуют два ограничения:

- **`LITELLM_MASTER_KEY` должен быть не установлен.** С master key litellm
  читает входящий `Authorization` как один из своих virtual key и отвечает
  `401` ещё до `pre_call`, так что мост никогда не увидит токен Codex. Теперь
  шлюз **отказывается стартовать** на такой комбинации, вместо того чтобы
  отдавать необъяснимые 401. Считается наличие, а не «истинность» — удалите
  строку, а не обнуляйте её.
- **Взаимоисключающий с `CORP_LLM_FORWARD_ANTHROPIC_AUTH`.** Оба моста читают
  один и тот же входящий bearer; оба сразу — отказ на старте и провал
  `gateway-admin config check`.

Учтите также, что litellm удерживает по одному деплойменту на каждое отдельное
значение per-request `api_key` — вместе с сырым значением — всё время жизни
процесса прокси, без вытеснения. Очистить их можно только перезапуском
процесса. См. [`security.md`](security.ru.md) §13.

## Диагностика

```bash
docker compose \
  -f docker-compose.demo.yml \
  -f docker-compose.chatgpt-codex.yml \
  ps

docker compose \
  -f docker-compose.demo.yml \
  -f docker-compose.chatgpt-codex.yml \
  logs -f --tail=100 litellm
```

Типовые ошибки:

| Ошибка | Причина | Действие |
|---|---|---|
| `401 E_PROVIDER_AUTH` | Codex не передал ChatGPT OAuth | Повторить `codex login`, проверить `requires_openai_auth = true` |
| `401 E_MISSING_TOKEN` | Нет `X-Corp-Auth` | Проверить секцию `http_headers` профиля |
| `503 E_CORP_LLM_DOWN` | Недоступна вспомогательная Gemma | Проверить `CORP_LLM_ENDPOINT` и VPN/DNS |
| upstream `401/403` | Подписка/аккаунт не принимает модель | Проверить модель в базовом Codex-профиле и повторить вход |

Остановка только рабочего контура:

```bash
docker compose \
  -f docker-compose.demo.yml \
  -f docker-compose.chatgpt-codex.yml \
  stop litellm redis postgres
```
