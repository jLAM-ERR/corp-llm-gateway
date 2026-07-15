# Пилот (разбор на ноутбуке)

## Аудитория и длительность

Это ~15-минутный разбор в терминале для коллег — из ИБ, эксплуатации, смежных команд — которые хотят увидеть шлюз в действии, не читая код. Пилот запускается на вашем ноутбуке: Claude Code слева, настоящий UI аудита Langfuse OSS в браузере справа. Трафик идёт через настоящую корп-LLM (через пилот-прокси LiteLLM), и каждый запрос проходит аудит и виден в Langfuse.

## Предварительные требования

- Docker 24+ и Docker Compose v2
- `jq` и `curl` (использует `scripts/pilot.sh` для опроса healthcheck и настройки Langfuse; `brew install jq` на macOS, `apt install jq` на Debian/Ubuntu)
- ~3 GB свободной RAM
- Подключённый корп-VPN (нужен, чтобы достучаться до реального эндпоинта корп-LLM)
- Установленный на ноутбуке Claude Code

## Подготовка

1. Склонируйте репозиторий (или `git pull`, если уже склонирован):
   ```bash
   git clone https://git.corp.lan/<group>/corp-llm-gateway.git
   cd corp-llm-gateway
   ```

2. Скопируйте и настройте пилот-файл окружения:
   ```bash
   cp .env.pilot.example .env.pilot
   # Edit CORP_LLM_ENDPOINT to point at the actual corp LLM URL
   # (e.g., https://corp-llm.corp.lan)
   ```

   TLS до корп-GLM **проверяется**, а не обходится. Корп-LLM предъявляет
   сертификат, подписанный внутренним корпоративным CA, поэтому пилот доверяет этой
   цепочке (`crt/corp-ca-bundle.pem`, закоммичен в репозиторий), а не
   отключает проверку: httpx-клиент шлюза проверяет против `CORP_LLM_CA_BUNDLE`,
   а контейнер LiteLLM собирает комбинированный бандл certifi-плюс-корп-CA и
   указывает на него `SSL_CERT_FILE` для своего `aiohttp`-апстрима. Оба варианта
   прописаны в `docker-compose.pilot.yml` — менять `.env.pilot` не нужно. (Старого
   обхода `SSL_VERIFY=False` больше нет.)

3. Холодный старт стенда (~3–5 минут при первом запуске):
   ```bash
   scripts/pilot.sh up
   ```
   Это скачивает образы, создаёт тома, инициализирует Langfuse пилот-учётками и печатает URL-ы.

4. Запомните напечатанные URL: http://localhost:8080 (шлюз через nginx), http://localhost:3000 (Langfuse, тоже через nginx).

5. (Опционально, удобно в третьем шелле) Смотрите поток вымарывания в реальном времени:
   ```bash
   scripts/pilot.sh logs
   ```
   Это следит за логами контейнера LiteLLM, отфильтрованными до потока
   санитизации/десанитизации и строк аудита, с подавлением спама access-логов
   от healthcheck — терминальный вид той же активности, которую вы увидите в Langfuse.

## Схема с двумя шеллами

**Левый шелл:** здесь запускается Claude Code. Отсюда вы отправляете промпты.

**Правый шелл/браузер:** откройте вкладку браузера на http://localhost:3000. Войдите с учётными данными, напечатанными командой `up` (или посмотрите в `.env.pilot` значения `LANGFUSE_PUBLIC_KEY` / `LANGFUSE_SECRET_KEY`).

**Чтобы направить Claude Code на пилот-прокси**, выполните это в левом шелле:
```bash
scripts/pilot.sh presenter-env
```
Скопируйте экспортируемые переменные (`ANTHROPIC_BASE_URL`, `ANTHROPIC_CUSTOM_HEADERS`) в свою сессию Claude Code. Или вручную:
```bash
export ANTHROPIC_BASE_URL=http://localhost:8080
export ANTHROPIC_CUSTOM_HEADERS='X-Corp-Auth: demo-team-token'
```

## 7 промптов

Каждый промпт задействует свою часть шлюза. Полный текст промптов и детали настройки — в [`scripts/pilot-prompts.md`](../scripts/pilot-prompts.md).

| # | Промпт | Что демонстрирует | Tier / Наблюдаемое |
|---|--------|----------------------|-------------------|
| 1 | "What's the capital of France?" | База — без PII, без санитизации | Нет; `redactions=0` |
| 2 | "Draft an email to jane.doe@example.com…" | Вымарывание и восстановление email | Regex; `cache_a_miss` |
| 3 | Проверить JSON с AWS API-ключом | Детекция структурированного API-ключа | тир JSON |
| 4 | Вызов функции со встроенным email | Вымарывание внутри аргументов инструмента | тир FunctionCall |
| 5 | Промпт 2 снова (точный повтор) | Дедупликация по содержимому (Cache A) | Regex; `cache_a_hit: true` |
| 6 | Вставить ≥101 KB логов с одним email | Пропуск по размеру (порог M1-11) | Пропущено; `redaction_count: 0` несмотря на PII |
| 7A | Как промпт 2, но сначала остановить Vector | Fail-closed конвейер аудита | HTTP 503 `audit pipeline unhealthy` |
| 7B | Перезапустить Vector, повторить 7A | Восстановление и нормальный поток | Regex; трейс появляется |

См. [`scripts/pilot-prompts.md`](../scripts/pilot-prompts.md) для точного текста, ожидаемых наблюдений в Langfuse и того, как интерпретировать каждый трейс.

## Остановка и очистка

```bash
# Stop containers but preserve state (volumes stay, fast re-run next time)
scripts/pilot.sh down

# OR completely reset (nuke volumes for a clean slate)
scripts/pilot.sh reset
```

Обе команды безопасны; они не трогают рабочее дерево git и исходный код.

## Диагностика проблем

- **Корп-LLM недоступна** — проверьте связь по VPN. Команда `scripts/pilot.sh up` предупреждает, но не падает; стенд будет здоров, даже если корп-LLM лежит. Реальная отправка промптов упадёт с ошибкой шлюза. Проверка: `curl -v https://<your-corp-llm-endpoint>/health`.

- **Трейсы в Langfuse не появляются** — шаг seed-langfuse мог упасть (редко, обычно сетевые таймауты). Запустите `scripts/pilot.sh seed-langfuse` вручную для идемпотентного повтора. Затем перезапустите сервис vector: `docker compose -f docker-compose.pilot.yml restart vector`.

- **HTTP 503 с телом «audit pipeline unhealthy»** — это **намеренно** в промпте #7 (демонстрирует fail-closed-поведение при остановке Vector). Если появляется неожиданно, проверьте статус Vector: `docker compose -f docker-compose.pilot.yml ps vector` и логи: `docker compose -f docker-compose.pilot.yml logs vector`.

- **Контейнер LiteLLM не стартует** — обычно сетевая проблема во время `pip install -e /pkg`. Проверьте логи: `docker compose -f docker-compose.pilot.yml logs litellm`. Повторите `scripts/pilot.sh up`.

- **Первый запуск занимает >5 минут** — ожидаемо при первом старте; образы ClickHouse и Langfuse большие. Последующие запуски (~30 с) тёплые.

- **«Claude Code напечатал мой реальный email / API-ключ — это утечка?»** — Нет. Ответ Claude Code показывает **восстановленное оригинальное** значение, потому что `post_call`-десанитайзер восстанавливает его на обратном пути; placeholder существовал только на участке шлюз↔корп-LLM, и корп-LLM никогда не видела секрет. Чтобы *увидеть* вымарывание, используйте хелпер `sanitize` из Stage 0 (до→после) или `placeholder_list` в Langfuse, а не отрисованный ответ. Детали по каждому промпту — в [`scripts/pilot-prompts.md`](../scripts/pilot-prompts.md).

- **Лишние трейсы с `redaction_count: 0`** — Claude Code сам шлёт небольшие прогревочные / зондирующие запросы; они попадают как отдельные трейсы и не являются ни вашим промптом, ни сбоем вымарывания. Определяйте трейс своего промпта по `prompt_token_count` и ожидаемому `redaction_count`, а не по «последнему трейсу». См. [`scripts/pilot-prompts.md`](../scripts/pilot-prompts.md).

## Вне рамок пилота

Пилот намеренно **не** покрывает:

- ❌ Реальный апстрим Anthropic/OpenAI (только корп-LLM)
- ❌ mTLS- или OIDC-аутентификацию корп-LLM (провайдер остаётся `noop`)
- ❌ CoreDNS-sinkhole или NetworkPolicy (конструкции только для k8s)
- ❌ S3-sink или SIEM-sink аудита (в пилоте единственный sink — Langfuse)
- ❌ Изоляцию нескольких команд (одна команда: `demo-team`)
- ❌ Правила `replace.md` по командам (один файл правил по умолчанию)
- ❌ CI-джобу `e2e:langfuse` или существующий `docker-compose.yml` (пилот-стенд параллельный, независимый)

Это продовые заботы; пилот фокусируется на ключевом потоке вымарывание→аудит→восстановление.
