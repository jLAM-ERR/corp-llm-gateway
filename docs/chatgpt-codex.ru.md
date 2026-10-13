# Профиль ChatGPT Codex

Этот профиль позволяет Codex обращаться к `corp-llm-gateway` через OpenAI
Responses API, а шлюзу использовать OAuth-аутентификацию действующей подписки
ChatGPT. API-ключ OpenAI для профиля не нужен.

Поток запроса (демо-стек ниже слушает 4000; на production compose-стеке шлюз
codex — на 4001, см. «В продакшене»):

```text
Codex (ChatGPT OAuth)
  -> http://127.0.0.1:4000/v1/responses   (production: 4001, GATEWAY_CODEX_PORT)
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

> **Для production-хоста сначала отредактируйте копию.**
> `docker/chatgpt-codex/chatgpt-codex.config.toml` — демо-профиль: в нём
> захардкожены `base_url = "http://127.0.0.1:4000/v1"` и
> `X-Corp-Auth = "demo-team-token"`. На production compose-стеке порт 4000 —
> это `gateway-anthropic`; шлюз codex слушает `GATEWAY_CODEX_PORT` (по
> умолчанию 4001). Поставьте `base_url` в `http://127.0.0.1:4001/v1` (через
> SSH-туннель к серверу) или в origin фронта + `/v1`, если
> `NGINX_GATEWAY_UPSTREAM=gateway-codex`, и впишите в `X-Corp-Auth` собственный
> corp-токен разработчика. С 4000 Codex попадёт на шлюз Anthropic, у которого
> нет маршрута для его модели.

```bash
cp docker/chatgpt-codex/chatgpt-codex.config.toml \
  ~/.codex/chatgpt-codex.config.toml
# production: в копии поправьте base_url (порт 4001) и X-Corp-Auth
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

На production compose-стеке (`compose/`) это провайдер `codex`.
`COMPOSE_PROFILES=codex` (отдельно или вместе с другими провайдерами) поднимает
`gateway-codex` на `127.0.0.1:${GATEWAY_CODEX_PORT:-4001}` с
`CORP_LLM_FORWARD_CHATGPT_AUTH=1` и `compose/litellm/config.codex.yaml`: один
catch-all маршрут на литеральный `https://chatgpt.com/backend-api/codex`, так
что никакая настройка не отправит токен на другой хост. Там работает
production-guardrail (хранилище токенов в Postgres, цепочка аудита), поэтому
`X-Corp-Auth` несёт настоящий corp-токен, а не `demo-team-token`. Направьте
`base_url` профиля на этот порт или на фронт, если
`NGINX_GATEWAY_UPSTREAM=gateway-codex`. См.
[`ops/deployment-modes.ru.md`](ops/deployment-modes.ru.md).

`CORP_LLM_FORWARD_CHATGPT_AUTH` — обычный флаг из `settings.KEYS`:
`bootstrap.build_guardrail()` резолвит его из окружения или TOML-конфига,
как и любую другую настройку шлюза (см. [`config.example.toml`](../config.example.toml)).
Мост не проверяет, куда litellm направит запрос, поэтому включайте его только
там, где каждый маршрут ведёт в бэкенд ChatGPT Codex, как в `config.codex.yaml`.
Helm-чарт в этом релизе маршрутизирует `"*"` в корп-vLLM: не включайте там
флаг, иначе токен ChatGPT разработчика уйдёт в корп-vLLM.

Где бы вы его ни включали, действуют ещё два ограничения:

- **`LITELLM_MASTER_KEY` должен быть не установлен.** Любой master key не даёт
  стартовать, с мостом или без: proxy-аутентификация litellm (режим A) удалена
  в 1.1.0. При включённом мосте litellm к тому же читал бы входящий
  `Authorization` как один из своих virtual key и отвечал `401` ещё до
  `pre_call`, так что мост никогда не увидел бы токен Codex. Считается
  наличие, а не «истинность» — удалите строку, а не обнуляйте её.
- **Взаимоисключающий с `CORP_LLM_FORWARD_ANTHROPIC_AUTH`.** Оба моста читают
  один и тот же входящий bearer; оба сразу — отказ на старте и провал
  `gateway-admin config check`. Поэтому compose-стек запускает по шлюзу на
  провайдера.

До ChatGPT доходят только собственные заголовки моста, по точному имени и только
со значением в формате заголовка; никакой другой заголовок клиента наверх не
уходит ([`security.md`](security.ru.md) §13).

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
