# Интеграция харнессов

Как AI-харнессы для кодинга на ноутбуке разработчика (Claude Code, Codex,
Cursor и т.д.) отправляют `X-Corp-Auth` и другие корп-специфичные заголовки
в шлюз.

## Проблема

Шлюз ожидает два заголовка в каждом запросе:

| Заголовок | Источник | Назначение |
|---|---|---|
| `X-Corp-Auth: <corp-token>` | `~/.corp-llm-gateway/token` | корп-идентичность / определение команды |
| `Authorization: Bearer <subscription-token>` | собственный вход харнесса: подписочный токен Anthropic или ChatGPT разработчика | креденшел для апстрима на подписочном провайдере, его переносит мост; на `corp` никогда не пробрасывается |

Ни один провайдер не принимает API-ключ клиента: с 1.1.0 клиент
аутентифицируется на шлюзе через `X-Corp-Auth`, а на провайдерах `anthropic` /
`codex` подписочный мост несёт токен из `Authorization` наверх
(`docs/security.ru.md` §13). Каждый харнесс уже умеет отправлять
`Authorization` — это его собственный вход. Хитрость в `X-Corp-Auth`. Работают три паттерна, по возрастанию
трения:

## Паттерн 1 — кастомные заголовки через env-переменную (только Claude Code)

Claude Code нативно поддерживает `ANTHROPIC_CUSTOM_HEADERS`:

```bash
export ANTHROPIC_BASE_URL='https://gateway.corp.lan'
export ANTHROPIC_CUSTOM_HEADERS="X-Corp-Auth: $(cat ~/.corp-llm-gateway/token)"
```

Оговорка: значение фиксируется снимком при инициализации шелла. После ротации
токена (установщик ротирует минимум каждые 30 дней) откройте новый шелл, чтобы
`$(cat …)` выполнился заново. `install.sh` пишет функцию, которая
переоценивается при каждом старте шелла, что покрывает типичный случай.

## Паттерн 2 — заголовки через файл конфигурации (Codex)

OpenAI Codex CLI читает `~/.codex/config.toml`:

```toml
[default]
api_base = "https://gateway.corp.lan/v1"

[default.headers]
X-Corp-Auth = "ct_xxxxxxxxxxxxxxxxxxxxxx"
```

Ограничение — статичное значение. Перезапустите `install.sh` после ротации
токена или настройте cron / launchd-задачу, которая еженедельно переписывает
этот файл из файла токена.

Для инструментов, использующих OpenAI Python SDK напрямую:

```python
from openai import OpenAI
client = OpenAI(
    base_url="https://gateway.corp.lan/v1",
    default_headers={"X-Corp-Auth": open("~/.corp-llm-gateway/token").read().strip()},
)
```

## Паттерн 3 — `corp-llm-gateway proxy` (универсальный)

Для харнессов без механизма кастомных заголовков запустите localhost
HTTP-прокси, который вставляет `X-Corp-Auth` в каждый запрос:

```bash
corp-llm-gateway proxy --listen 127.0.0.1:9999 --upstream https://gateway.corp.lan
```

Затем направьте харнесс на прокси:

```bash
export ANTHROPIC_BASE_URL='http://127.0.0.1:9999'
export OPENAI_BASE_URL='http://127.0.0.1:9999/v1'
```

Прокси:

- Перечитывает файл токена на каждом запросе (без перезапуска шелла при ротации).
- Пропускает SSE-ответы потоком без изменений.
- Форвардит `Authorization: Bearer <subscription-token>` разработчика на шлюз
  нетронутым.
- Ничего не логирует о телах запросов — граница аудита это шлюз,
  а не прокси.

Рекомендуется: добавьте прокси в автозапуск шелла как пользовательский сервис
launchd / systemd, чтобы он всегда работал. Пример launchd plist — в
`scripts/launchd-proxy.plist` (TODO: закоммитить по запросу).

## Матрица паттернов

| Харнесс | Рекомендуется | Запасной вариант |
|---|---|---|
| Claude Code | Паттерн 1 | Паттерн 3 |
| Codex CLI (OpenAI) | Паттерн 2 | Паттерн 3 |
| Cursor (IDE) | UI настроек приложения (поле кастомного заголовка) | Паттерн 3 |
| Continue (VS Code) | UI конфигурации | Паттерн 3 |
| `curl`, сырые скрипты | env-переменная + `--header` | Паттерн 3 |

## Что `install.sh` делает сегодня

```bash
ANTHROPIC_BASE_URL='https://gateway.corp.lan'
OPENAI_BASE_URL='https://gateway.corp.lan/v1'
CORP_GATEWAY_TOKEN_FILE='~/.corp-llm-gateway/token'
# Pattern 1: only useful for Claude Code; safe no-op for other harnesses
ANTHROPIC_CUSTOM_HEADERS="X-Corp-Auth: $(cat ~/.corp-llm-gateway/token)"
```

Для Codex / Cursor / прочих разработчик один раз редактирует соответствующий
файл конфигурации (токен ротируется лишь раз в 30 дней; трение ограничено).

## Что всё ещё утекает, а что нет

- `Authorization: Bearer <subscription-token>` — собственный подписочный токен
  Anthropic или ChatGPT разработчика. На провайдерах `anthropic` / `codex`
  подписочный мост переносит его в вызов апстрима как креденшел; на `corp` он
  не покидает шлюз. Ни один провайдер не принимает API-ключ клиента. Шлюз
  **никогда его не логирует**, и прокси его тоже не трогает.
- `X-Corp-Auth` — корп-токен, ограниченный по пользователю, истекает через
  30 дней, отзывается за ≤60 с. Шлюз срезает его перед пробросом на апстрим
  (`AuthMiddleware.strip_corp_token`) и никогда не логирует.
- Оба инварианта закреплены в `tests/invariants/test_no_originals_leak.py`.
