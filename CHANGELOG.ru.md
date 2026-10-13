# История изменений

Все значимые изменения в corp-llm-gateway задокументированы здесь.
Формат следует [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

---

## [1.1.0]

### Безопасность — заголовки клиента больше не доходят до провайдера без проверки (затронута 1.0.0; ломающее изменение)

- **1.0.0 отправляла наверх все входящие заголовки клиента, без санитизации, на всех
  провайдерах.** litellm отправляет провайдеру `data["headers"]`, а guardrail клал туда
  логируемую копию litellm со всеми входящими заголовками и убирал только wire-заголовки
  (`_WIRE_HEADERS_TO_DROP`), при `CORP_LLM_STRIP_INBOUND_HEADERS=1`. Значения заголовков не
  проходят ни санитайзер, ни скан Stage 5, так что нарушался инвариант 1: произвольный заголовок
  с email доходил до upstream Anthropic, Codex и corp без изменений; cookie приходила как
  `***REDACTED***`; на corp клиентский bearer `sk-ant-oat…` вместе с `x-litellm-api-key`
  приходил слитым с собственным креденшелом маршрута как
  `Authorization: ***REDACTED***, Bearer fake-api-key`. Подтверждено на реальном образе против
  перехватывающего upstream. Обновите каждое развёртывание 1.0.0.
- **`data["headers"]` теперь проекция по allowlist (`_UPSTREAM_HEADER_ALLOWLIST`), и этот
  allowlist пуст.** Мосты подписочной аутентификации несут токен разработчика в `api_key`, а
  свои allowlist-ы — в `extra_headers` (Anthropic: `anthropic-beta`, `anthropic-version`,
  `user-agent`; Codex: `chatgpt-account-id`, `originator`, `session-id`, `thread-id`,
  `user-agent`, `x-codex-beta-features`, `x-openai-internal-codex-responses-lite`); маршрут corp
  получает только собственный `CORP_LLM_AUTH_TOKEN`. На маршруте без моста никакая копия
  клиентского `Authorization` до upstream не доходит: в 1.0.0 доходила редактированная копия
  litellm рядом с собственным креденшелом маршрута. Список по провайдерам — в
  `docs/security.ru.md` §13; контейнерный набор тестов фиксирует точный набор заголовков,
  который получает каждый провайдер.
- **Ключи тела запроса, которые litellm превращает в заголовки upstream или в креденшел
  upstream, удаляются** (`extra_headers`, `provider_specific_header`, `api_key`,
  `organization`), молча; запрос идёт дальше. **Ломающее изменение** для клиента, который
  рассчитывал, что такой ключ или свой заголовок дойдёт до провайдера: ошибки он не получит,
  значение просто не дойдёт. В 1.0.0 клиент мог через `extra_headers` или
  `provider_specific_header` поставить любой заголовок на каждый вызов наверх, а `api_key` в
  теле на маршруте corp становился `Authorization` наверху и оставался в роутере для
  последующих запросов других клиентов.
- **Заголовки мостов пробрасываются по точному имени и только со значением в формате
  заголовка** (иначе молча отбрасываются). 1.0.0 пробрасывала любое имя `x-codex-*` и любое
  значение, так что `anthropic-version`, `user-agent` или `thread-id` с email доходили до
  провайдера. Для `chatgpt-account-id` и `x-codex-beta-features` нет записанного значения, из
  которого вывести формат, и они по-прежнему уходят как присланы. Неизвестные поля тела
  запроса не санитизируются: это принятый риск, описанный в `docs/security.ru.md` §2.

### Безопасность — мастер-ключ litellm отклоняется (ломающее изменение); у litellm нет базы данных

- **Любой `LITELLM_MASTER_KEY` не даёт стартовать**, с подписочным мостом или
  без, пустое значение в том числе. `build_guardrail()` останавливает запуск
  (served entrypoint завершается с кодом 3) строкой, начинающейся с
  `LITELLM_MASTER_KEY is set`, и `gateway-admin config check` о нём сообщает.
  1.0.0 отклоняла его, только пока был включён мост. **Ломающее изменение** для
  любого развёртывания, где он ещё задан: значение Helm, собственный образ или
  развёртывание, файл конфигурации шлюза.
  `MASTER_KEY_VS_FORWARD_AUTH_MESSAGE` переименован в
  `MASTER_KEY_REFUSED_MESSAGE`.
- **Мастер-ключ в собственном конфиге litellm тоже отклоняется:**
  `general_settings.master_key` и `environment_variables.LITELLM_MASTER_KEY` с
  любым значением (ссылка `os.environ/` в том числе) — exit 78 при запуске и
  проблема `config check`; значение не печатается. litellm берёт первый раньше
  окружения и экспортирует второй при старте, так что в 1.0.0
  virtual-key-аутентификация могла подняться из YAML.
- **У litellm нет базы данных на compose-стеке** (`docs/security.ru.md` §14,
  hazard 14c теперь закрыт в поставляемых сетапах; Helm её и не давал). Нет
  `DATABASE_URL`, нет `STORE_MODEL_IN_DB`, поэтому litellm никогда не
  перечитывает свою таблицу `LiteLLM_Config`, строки которой могли добавить
  callback-и логирования или включить промпты в spend-log. Риск возвращается,
  только если оператор добавит `DATABASE_URL`.

### Изменено — compose: по шлюзу на провайдера (ломающее изменение)

- **Сервис `litellm` заменён на `gateway-anthropic`, `gateway-codex` и
  `gateway-corp`**: каждый включается своим профилем compose и собран из одного
  общего определения. У каждого свой конфиг litellm, флаг моста и loopback-порт:
  `GATEWAY_ANTHROPIC_PORT` (4000), `GATEWAY_CODEX_PORT` (4001),
  `GATEWAY_CORP_PORT` (4002) вместо `GATEWAY_PORT`. По контейнеру на провайдера,
  потому что два подписочных моста не уживаются в одном процессе. Все три делят
  базу `gateway`, так что corp-токен работает на каждом.
- **Конфиги litellm по провайдерам**, в каждом один маршрут:
  `config.anthropic.yaml` (переименован из `config.oauth.yaml`; `claude-*` →
  `anthropic/`), `config.codex.yaml` (новый; `"*"` → `openai/` на литеральный
  `https://chatgpt.com/backend-api/codex`, так что никакое значение окружения
  не отправит токен ChatGPT разработчика в другое место), `config.corp.yaml`
  (новый; `corp-*` → `hosted_vllm/`). Production compose-стек теперь обслуживает
  Codex.
- **`COMPOSE_PROFILES` в `.env` сервера — единственный переключатель**
  провайдеров (`anthropic`, `codex`, `corp`) и фронта; `--profile` в командной
  строке не поддерживается. В `.env.example` строка закомментирована.
- **`profile-guard`**: one-shot на образе шлюза, которого ждёт `postgres`. Если
  в `COMPOSE_PROFILES` нет точного токена провайдера (обрезанного так же, как
  его обрезает compose; `corporate` — не `corp`), он называет ключ и завершается
  с кодом 64, так что `up` падает, а не поднимает стек, который ничего не
  обслуживает.
- **nginx-фронт проксирует на один шлюз, `NGINX_GATEWAY_UPSTREAM`** (по
  умолчанию `gateway-anthropic`). Его entrypoint завершается с кодом 64 на любое
  другое имя и на шлюз, чей провайдер выключен. Остальные провайдеры остаются на
  своих loopback-портах. `vector` и `nginx` зависят от шлюзов с
  `required: false`.
- **Оверлеи выдачи токенов и сборки нацелены на все три шлюза**; оверлей сборки
  собирает образ один раз, какие бы провайдеры ни были включены.
- **`deploy.sh up`** отказывает стеку, в `config --services` которого нет ни
  одного `gateway-*`; удаляет контейнер шлюза или фронта, которого `.env` больше
  не выбирает; выполняет `up -d --remove-orphans`, который удаляет контейнер
  `litellm` от 1.0 и освобождает `127.0.0.1:4000`. `down` тоже удаляет
  оставшиеся контейнеры шлюзов и фронтов. После синхронизации `up` выполняет голый
  `docker compose config` юнита автозапуска: отказывает, если тот не может
  разрезолвить стек (`COMPOSE_FILE` от 1.0 с `docker-compose.oauth.yml`), и
  предупреждает, если после перезагрузки поднимутся другие шлюзы или фронты либо
  те же с другой конфигурацией (`config --hash`, только хэши). Отказ после
  синхронизации сообщает, что `compose/` синхронизирован, а ничего не скачано,
  не запущено и не удалено. Скрипт по-прежнему никогда не читает `.env` сервера.
- **Шаги обновления:** `docs/ops/upgrade.md`, «1.0 → 1.1.0». Отредактируйте
  `.env` сервера до первого `deploy.sh up` версии 1.1.

### Удалено (ломающие изменения)

- **Режим A** (мастер-ключ и виртуальные ключи litellm): `LITELLM_MASTER_KEY`,
  `UI_USERNAME`, `UI_PASSWORD`, `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` на
  стороне шлюза и guard режима A в entrypoint compose. С 1.0.0 он был только
  тестовой позой.
- **Собственная база данных litellm на compose:** `DATABASE_URL`,
  `STORE_MODEL_IN_DB` и `postgres/initdb/00-create-litellm-db.sh`. База
  `litellm` в существующем томе инертна; как её удалить — в
  `docs/ops/upgrade.md`.
- **`compose/docker-compose.oauth.yml`** (его мост Anthropic — теперь провайдер
  `anthropic` базового файла) и **`compose/litellm/config.yaml`** (маршруты с
  API-ключами).
- **`deploy.sh --mode`** (**ломающее изменение** для любого скрипта или
  регламента, который его передаёт): любое значение отклоняется до того, как
  скрипт коснётся сервера, с упоминанием 1.1.0. `--issuance` не изменился.

### Известные ограничения

- **За фронтом — один шлюз.** nginx обслуживает только шлюз
  `NGINX_GATEWAY_UPSTREAM`; до остальных провайдеров добираются по их
  loopback-портам или через SSH-туннель.
- **`_corp_gateway_request_id` попадает и в тело запроса к корп-vLLM** на
  провайдере `corp`, как и в тело Anthropic на `/v1/chat/completions`
  (`docs/security.ru.md` §11 (e)). Это id запроса, а не контент.

### Добавлено — `gateway-admin`: начальная настройка с фиксированным токеном команды

- **`token issue --value VALUE`** сохраняет выбранное вами значение вместо случайного токена
  `ct_…` (для фиксированного токена команды, например в Local-сетапе). Значение не попадает ни
  в вывод команды, ни в её логи: в обычном выводе нет строки `token:`, `--json` его не содержит.
  Но оно есть в argv процесса, а опечатка во флаге заставляет argparse его повторить (см.
  `docs/ops/admin-cli.md`). Значение — 16-256 печатных ASCII-символов (0x21-0x7E) и не
  начинается с `ct_` (префикс зарезервирован за сгенерированными токенами); иначе это ошибка
  использования (exit 2), сообщение называет флаг и причину, но не значение. Повторная выдача
  того же значения тому же пользователю и команде заменяет `expires_at`, `issued_at` и
  `scopes` (без `--scopes` они сбрасываются к умолчанию — пустому набору). Значение,
  принадлежащее другому пользователю или команде, и отозванное значение отклоняются (exit 2);
  проверка и запись — два шага, а не одна транзакция (см. `docs/ops/admin-cli.md`). Глобальный
  `--token` (JWT оператора) и RBAC не изменились.
- **`gateway-admin db init`** применяет схемы токенов и настроек команд к базе из
  `CORP_LLM_PG_DSN`. Идемпотентна, требует RBAC. Каждый файл схемы применяется в своей
  транзакции под транзакционной advisory-блокировкой, так что параллельные запуски идут по
  очереди, и команда работает через пулер в transaction-режиме (PgBouncer). Как и пулы шлюза,
  она передаёт startup-параметры TCP keepalive, поэтому PgBouncer должен перечислить их в
  `ignore_startup_parameters` (`docs/ops/runbook.md`), иначе — `StartupParameterRejectedError`.
  `lock_timeout` ограничивает каждое ожидание блокировки: 60 с на другой `db init`
  (`another db init holds the lock (LockNotAvailableError)`) и 1,5 с на каждую блокировку
  таблицы (`LockNotAvailableError`). Ожидания блокировок могут задержать поиск токенов
  работающими шлюзами не больше чем примерно на 3 с (два ожидания таблиц) — меньше
  5-секундного таймаута поиска; на занятой базе команда быстро завершается с exit 2, и её
  безопасно перезапустить. Работа после получения блокировки не ограничена и тоже блокирует
  поиск: добавление OIDC-колонок и построение индексов при обновлении со схемы без OIDC или
  медленный `COMMIT` при синхронной репликации (см. `docs/ops/admin-cli.md`). Без DSN или без extra `postgres` — exit 2 с именованным сообщением; при сбое
  соединения или SQL — exit 2 только с типом ошибки. DSN не печатается.
- **`team create --if-absent`** при существующей команде завершается с exit 0 и ничего не
  меняет (без флага — по-прежнему exit 2).

### Изменено — `gateway-admin`

- **`token list` полностью маскирует выбранное значение как `***`**; у сгенерированного
  токена `ct_` по-прежнему видны первые 8 символов.
- **`--ttl-days` проверяется в каждом `token issue`**, случайном и с `--value`: меньше 1 или
  дальше 9999 года — ошибка использования (exit 2).
- **`team create` — одна атомарная вставка**, так что параллельный `create` не перезаписывает
  настройки существующей команды.

## [1.0.0] — GA (2026-10-09)

Первый GA-релиз — **цикл local-first детекции** (ниже) плюс сборка **GA-readiness /
безопасность и расширяемость**. Некомпромиссный критерий: ноль подтверждённых инцидентов утечки
за 90 дней после GA. Релиз-кандидаты v1.0.0-rc.1 – rc.6 выпускались с 2026-07-10 по
2026-08-04.

### Безопасность — поверхности логирования litellm, DEBUG и тела с политиками

Найдено планом перехода на guardrail API litellm (`docs/security.ru.md` §15, hazards 1-19).
Hazards 17, 17b и 18 **существовали и на `release/1.0.x`** (тот же litellm 1.101.0). Они зависят
от конфигурации: эти поверхности получает только callback, зарегистрированный в litellm, а
поставляемые конфиги регистрируют лишь собственный callback шлюза.

- **Logging payload `/v1/responses` содержал исходный ввод** (hazard 17): litellm снимает копию
  запроса в свой logging object до любого pre-call хука. Теперь pre-call передаёт logging
  object переписанный запрос, для любой формы `input`.
- **Отказ Stage 5 (DLP) передавал failure-хуку каждого callback исходный запрос** (hazard 17b).
  Теперь content-ключи снимка тела переписываются до скана. Отказ Stage 0 по-прежнему передаёт
  оригинал — он отказывает до любой перезаписи; наш callback этот хук не переопределяет.
- **Значение `X-Corp-Auth` попадало в поверхности логирования litellm** (hazard 18, инвариант 4):
  копия заголовков в `metadata.requester_metadata` на `/v1/chat/completions` и failure-хуки при
  401. Теперь pre-call срезает токен первым, из этой копии и из logging object litellm.
- **DEBUG litellm не даёт взвести шлюз** (exit 70, `litellm_debug_logging` /
  `litellm_set_verbose`): DEBUG-строки litellm печатают исходный запрос до любого pre-call хука,
  а две из них — ещё и сырой корп-токен и BYOK `Authorization`. `CORP_LLM_ALLOW_LITELLM_DEBUG=1`
  разрешает DEBUG вне prod, только для тестов; в prod это exit 78. При взведении также
  отклоняются `apply_guardrail` в MRO guardrail, `scan_raw_request` / `run_in_parallel` и
  компрессор ответов в приложении litellm (`response_compressor`).
- **Тела запросов с политиками отклоняются на route gate**: JSON-тело с ключом `policies`
  верхнего уровня — 403 `E_ROUTE_BLOCKED` (`route_gate_body_policies`), тело не
  `application/json` — 415 (`route_gate_body_not_json`), оба до разбора тела litellm.
- **415 получает любое тело не в UTF-8 JSON**: `charset` не `utf-8`/`utf8`, BOM, UTF-16/32 или
  байты, которые не декодируются как UTF-8. Проверка `policies` читает байты, а тело в UTF-16
  записывает ключ байтами, которые она не находила, и гейт его пропускал. litellm 1.101.0
  превращает такое тело в пустое (с BOM или байты не в UTF-8) или отвечает 400, так что утечки не
  было; стандартный `json.loads(bytes)` декодировал бы каждое из них.

### Исправлено — стриминг chat-completions возвращал плейсхолдеры

- **Стриминг OpenAI chat-completions восстанавливается.** Обратная подстановка в callback
  пропускала чанки `ModelResponseStream` litellm без изменений, и клиенты OpenAI SDK получали
  плейсхолдеры. Дефект есть на `release/1.0.x`.

### Изменено — ответы восстанавливаются вне litellm

- **Единственная обратная подстановка ответа шлюза — теперь ASGI middleware** перед приложением
  litellm, внутри лимитера (`route_gate/desanitize_middleware.py`); litellm и все callback-и
  внутри него видят только плейсхолдеры, клиент получает оригиналы. Сбой восстановления — 500
  `E_INTERNAL` без содержимого (или закрытый поток) и `gateway_failure{component="desanitize"}`.
  Замеренные накладные расходы: меньше 0.75 мс на каждом потоке (p50); в пределах +10 % на
  каждом SSE-потоке и каждом unary-потоке, кроме chat unary (+13.4 % в одном из двух прогонов,
  +6.4 % в другом); разброс между прогонами на одной базе — до 1.2 мс.
- **Одна аудит-запись на запрос, когда ответ заканчивается** (`route_gate/terminal_audit.py`),
  никогда не callback-ом litellm; новый код `E_SERVER_SHUTDOWN` — запрос отменил сервер
  (`docs/audit-schema.ru.md`, «Итоговая запись»).
- **Счётчики токенов берутся из самого ответа**; chat-поток всегда запрашивает usage-чанк и
  отбрасывает его, если клиент его не просил.
- **Поставляемые конфиги litellm закрепляют `general_settings.supported_db_objects: ["models"]`**
  (compose и Helm). `enforces_request_content = True` на guardrail; он остаётся обычным
  `CustomLogger` (`docs/security.ru.md` §15).

### Известные ограничения — граница ответа

- Чередующиеся фрагменты двух вызовов инструментов в chat-потоке возвращаются плейсхолдерами,
  никогда не оригиналами.
- `sequence_number` в восстановленном потоке `/v1/responses` перенумеровывается и может
  отличаться от номеров провайдера.
- Chat `messages[].name` и `prediction.content`, Responses `prompt.variables` и `citations[]`
  Anthropic ещё не покрыты (`docs/security.ru.md` §2, «Не санитизируется / отложено»).
- Таблица конфигурации litellm (hazard 14c) и ключ провайдера в kwargs логирования (hazard 19b)
  охарактеризованы, но не закрыты (`docs/security.ru.md` §11 (j), §15).

### Добавлено — `guardrail_information` без контента в logging payload litellm

- **Pre-call пишет одну запись `guardrail_information`** (`corp-llm-sanitizer`, `pre_call`) в
  `StandardLoggingPayload` litellm собственным writer'ом litellm, так что каждый логгер из
  `litellm.callbacks` видит результат guardrail: `guardrail_status`, выведенный из
  `block_reason`, время, `redaction_count`, `finding_label_counts` и `block_reason`, если он
  задан. Без контента и по allow-list (`assert_guardrail_information_allowed`); гейт NEVER-полей
  теперь проверяет такие записи везде, где запись их несёт (`docs/audit-schema.ru.md`).
- **Новое событие лога `litellm_guardrail_information_failed request_id=… error=<type>`**, когда
  запись не записана; запрос и его аудит-запись продолжаются (`docs/security.ru.md` §8,
  `guardrailInformationWriteFailed`).
- **`gateway_failure{component="audit"}` расширен**: он считает и это событие, наряду с
  событием лога litellm без состояния запроса (`docs/ops/runbook.ru.md`).

### Добавлено — HTTPS-фронт для compose-стека (nginx, опционально)

- **Профили `nginx` / `nginx-ports`** в `compose/docker-compose.yml`, выключены, пока `.env`
  сервера не задаёт `COMPOSE_PROFILES`, — единственный переключатель, который читают и
  развёртывание, и перезагрузка. `nginx` маршрутизирует `gateway.<GATEWAY_DOMAIN>` /
  `langfuse.<GATEWAY_DOMAIN>` по имени; `nginx-ports` — запасной вариант без DNS, на двух портах.
  Публикуется на `NGINX_BIND_ADDR`, по умолчанию loopback. Без профиля стек не меняется.
- **Только HTTPS, два режима TLS, без умолчания.** `NGINX_TLS_MODE=terminate` (nginx отдаёт свой
  сертификат из `compose/nginx/certs/`, TLS 1.2+, HSTS) или `behind-proxy` (TLS терминирует
  балансировщик администраторов; любой пир вне `NGINX_TRUSTED_PROXIES` не получает ответа).
  Проверяющий entrypoint отклоняет плохой ключ одной строкой лога и кодом 64-69.
- **Allow-list по точным путям**: `POST /v1/messages`, `/v1/chat/completions`, `/v1/responses`,
  `GET /v1/models`, `GET /healthz/live`, `POST /internal/issue-token`; всё остальное — 404 на
  периметре: эшелонированная защита поверх route gate, и nginx не пропускает ничего, что гейт
  отклоняет.
- **Лимиты на токен на периметре** (`NGINX_TOKEN_RATE`, `NGINX_TOKEN_BURST`, `NGINX_TOKEN_CONN`,
  `NGINX_ISSUE_RATE`): 429 `E_RATE_LIMITED` до шлюза (`docs/ops/capacity.ru.md`, «Лимиты на
  границе»).
- **Развёртывание:** `deploy.sh` отказывает, если `.env` включает оба профиля, сразу падает на
  мёртвом фронте и никогда не синхронизирует `nginx/certs/`; сертификаты кладутся на сервер
  (`docs/ops/deploy-handoff.ru.md`, шаг 4a). `scripts/deploy/make-selfsigned-certs.sh` создаёт
  одноразовый CA и лист для пилотов и тестов.
- **Логи:** JSON access-лог без заголовков с учётными данными, тела и строки запроса;
  `error_log` на уровне `crit`, потому что на `error` и `warn` nginx дописывает строку запроса.

### Добавлено — production-развёртывание на compose (`compose/`)

- **Второй боевой вариант развёртывания**, для хостов без Kubernetes, наряду с Helm-чартом:
  data plane (`litellm` + `redis` + `postgres`), **self-hosted Langfuse v3** (web/worker,
  ClickHouse, MinIO, отдельный ограниченный Redis — ни один из них не публикует хостовый порт)
  и **конвейер аудита** (`vector`, читающий bind-монтированный только на чтение каталог логов
  контейнеров, а не docker-сокет; трансформы `never_fields_gate` / `audit_only` побайтово
  совпадают с configmap Helm-чарта). Каждый секрет приходит из `.env`; четыре ключа без значений по
  умолчанию заставляют `docker compose up` отказаться стартовать, а не подняться наполовину
  настроенным.
- **Два взаимоисключающих режима аутентификации.** Режим A — корп-API-ключи, у разработчика
  виртуальный ключ LiteLLM (отзыв и учёт расходов на человека). Режим B
  (`docker-compose.oauth.yml`) — OAuth-токен подписки Anthropic самого разработчика уходит
  наверх, а корпоративного `ANTHROPIC_API_KEY` **не существует вовсе**; обслуживается только
  `claude-*`, и это несущий контроль, а не упрощение (litellm выбирает фактический upstream уже
  после хука, поэтому единственное доказательство, куда уйдёт запрос, — Anthropic-only таблица
  маршрутов). Мастер-ключ и мост несовместимы — `build_guardrail()` отказывается стартовать и
  называет причину.
- **Скрипты подготовки сервера и развёртывания** — `scripts/deploy/bootstrap-server.sh`
  (идемпотентная подготовка хоста в день 0 + опциональный systemd-юнит) и
  `scripts/deploy/deploy.sh` (`up`/`down`/`restart`/`logs`/`status`, `--mode oauth`,
  `--dry-run`, `--yes`). Локальный `.env` наверх не уезжает, серверный не читается и не
  перезаписывается.
- **`docker-compose.build.yml`** — сборка текущей ветки вместо опубликованного тега, с
  запинованным NER-профилем `ru-en` (в профиле `base` нет английской модели, и при
  `CORP_LLM_REQUIRE_NER=1` такой образ отвечал бы 503 на каждый запрос).

### Добавлено — сервис корп-NER (опционально, выключен по умолчанию)

- **`CorpNerDetector` + клиент `corp_ner/`** — удалённый NER-детектор в конце local-first
  каскада; выключен, пока не задан `CORP_NER_ENABLED=1`, и требует `CORP_NER_ENDPOINT` при
  включении (включено без endpoint'а — отказ на старте, а не тихий пропуск). Тюнинг:
  `CORP_NER_TIMEOUT_S` / `CORP_NER_MAX_TEXTS` / `CORP_NER_MAX_INPUT_CHARS` / `CORP_NER_CA_BUNDLE`.
- **Сетевые детекторы исключены из сегментов `CODE`** — именно эту утечку (отправку исходников
  во внешний сервис) это предотвращает. Все *локальные* детекторы код сканировать продолжают;
  граница проходит по «локальный против сетевого», а не по «безопасный для кода или нет».
- **Вызов NER несёт сырой пользовательский контент**, поэтому проверка TLS для него не
  отключается никогда; внутренний CA — через `CORP_NER_CA_BUNDLE`.
- Проба готовности сервиса и покрытие новых ключей в `gateway-admin config check`.

### Добавлено — детекция

- **Правило `BANK_CARD` по Луну** — правдоподобные по IIN и валидные по Луну PAN (13–19 цифр,
  с допуском на группировку пробелами и дефисами); длина и смещение проверяются до вычисления
  Луна, чтобы более длинное случайное совпадение не вытесняло настоящий PAN.

### Изменено — граница ключа Cache A

- В ключ Cache A теперь входит **отпечаток политики детекторов** вдобавок к константе версии
  покрытия, поэтому записи, сделанные при разном покрытии (корп-NER вкл/выкл, расширенный
  профиль, другая capability NER-движка, другой лемматизатор газеттира), не могут быть отданы
  друг другу. Переключение любого сетевого флага **не требует очистки кэша**.

### Документация

- `docs/ops/deployment-modes.md` + `.ru.md` — матрица режимов, оба сетевых переключателя, все
  режимы отказа.
- `docs/ops/deploy-handoff.md` + `.ru.md` — сжатая пошаговая инструкция для того, кто
  разворачивает.
- `compose/README.md` + `.ru.md` — полный справочник по стеку (маршрутизация, виртуальные ключи,
  почему здесь нет BYOK, Langfuse, конвейер аудита и процедуры восстановления, TLS до корп-vLLM,
  настройки окружения).
- README (EN/RU) описывает варианты развёртывания, compose-стек и переключатель корп-NER;
  русский README догнал английский по разделу локального compose, семантике сопоставления
  `replace.md` и разделу лицензии.

### Известные ограничения compose-варианта

- **Без профиля TLS нет** — если `COMPOSE_PROFILES` не задан, единственный опубликованный порт —
  `127.0.0.1:4000` (HTTPS-фронт — выше).
- **В режиме B management-эндпоинты litellm без аутентификации** (`/key/*`, `/model/*`,
  `/user/*`, UI) — без мастер-ключа его proxy-auth пропускается, чего режим и требует.
  LLM-маршруты по-прежнему закрывает `X-Corp-Auth`. Закрыть на nginx до вывода порта за
  loopback.
- **Аудит буферизуется, но не fail-closed** — задокументированное отступление от значения
  `vectorBufferFull` по умолчанию из `docs/security.ru.md` §8. Долговечность ограничена
  ротацией логов docker.
- **Никакого недоверенного `docker run` на хосте** — фильтр Vector по метке контейнера это
  защита от ошибки конфигурации, а не граница безопасности (`docs/security.ru.md` §8.2).

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
- Создано внутреннее git-зеркало; open Q#1 закрыт.

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
