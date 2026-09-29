# Runbook эксплуатации

Ссылка на план: M8-2.

## Ежедневные операции

### Деплой новой версии

1. Пометьте релиз тегом: `git tag v0.x.y && git push origin v0.x.y`.
2. GitHub Actions собирает и публикует образ шлюза на теге (`.github/workflows/build-image.yml`). Wheel и Helm-чарт пока собираются локально (эти CI-джобы ещё не портированы).
3. Примените в staging: `helm upgrade --install gw helm/corp-llm-gateway -f values-staging.yaml --version v0.x.y`.
4. Дождитесь зелёного `/healthz/ready` на всех 3 pod'ах.
5. Запустите deep-check: `curl https://gateway-staging.corp.lan/healthz/sanitization`.
6. Промоутните в prod той же командой против `values-prod.yaml`.

### Откат

```
helm rollback gw <revision>
```

Список ревизий: `helm history gw`. По умолчанию Helm хранит последние 10.

### Пиннинг версии LiteLLM

`values.yaml: litellm.versionPin`. Поднимайте только после прохождения
staging-гейта апгрейда (согласно задаче M0-7 в плане).

## Плейбук инцидентов

Матрица fail-policy в плане (M4) — источник истины о том, что «должно»
происходить при отказе каждого компонента. Когда реальность расходится с
ней — это баг.

### Корп-LLM недоступна

Симптом: растёт `gateway_failure{component="corp_llm"}`; запросы
возвращают 503 с `error_code="E_CORP_LLM_DOWN"`.

Поведение: fail-closed (по матрице). Шлюз здоров; нездорова зависимость.

Действия:
1. Подтвердите, что корп-LLM действительно лежит (curl её endpoint из
   pod'а шлюза).
2. Если да: поднимите по пейджеру команду корп-LLM. Шлюз восстановится
   автоматически, когда корп-LLM восстановится.
3. Если нет: разбирайтесь со связностью на стороне шлюза (NetworkPolicy,
   DNS).

### Деградация каскада детекции

Отдельного Deployment'а пред-пасса **не существует**. Детекция работает
**внутри процесса** шлюза (локальный каскад: regex+checksum, dual-NER,
газеттир — см. ADR-003); корп-LLM-оракул — только условный fallback. Два
режима отказа:

- **Модель NER отсутствует или сама себя отключила.** При
  `CORP_LLM_REQUIRE_NER=1` (prod) это fail-**closed**: запросы возвращают
  503 `E_NER_UNAVAILABLE` — это не «тихий медленный путь».
  `/healthz/ready` при этом краснеет на NER-пробе. Почините NER-стек
  (extra `ner` + wheel'ы моделей), и pod восстановится. С выключенным
  флагом (dev) NER деградирует молча, не находя ничего, — в prod так
  работать нельзя.
- **Оракул (корп-LLM) недоступен.** Проявляется как `E_CORP_LLM_DOWN` —
  см. раздел выше.

Действия:
1. Добавляйте мощность детекции масштабированием Deployment'а **шлюза**
   (детекция идёт в его процессе), а не pod'а пред-пасса:
   `kubectl scale -n corp-llm-gateway deploy/gw-corp-llm-gateway --replicas=N`, либо
   включите/поднимите HPA (`autoscaling` в values.yaml).
2. Разберитесь с pod'ом (OOM? ошибка загрузки модели NER? необычно
   большой payload — им управляют порог размера M1-11 /
   `CORP_LLM_OVERSIZE_POLICY`).

### Кластер Redis недоступен

Симптом: запросы возвращают 503 с `error_code="E_REDIS_DOWN"`.

Поведение: fail-closed (по матрице). Нет маппингов = нет десанитизации =
отдавать небезопасно.

Действия:
1. `kubectl -n redis get pods` — минимум 2 из 3 должны быть подняты. Если
   упал 1: кластер в порядке; временный сбой.
2. Если все лежат или split-brain: failover через Redis sentinel.

### Буфер Vector на 50% (алерт)

Симптом: алерт SIEM «vector_buffer_50pct».

Поведение: **риск потери аудита, а НЕ блокировка запросов.** В матрице M4
`vectorBufferFull` значится как fail-closed, но ни одна развёрнутая топология
сегодня этого обеспечить не может: `CORP_AUDIT_SINK` не задан, поэтому
единственное аудит-действие шлюза — запись строки в собственный stdout
(`audit/factory.py`, по умолчанию `StdoutSink`), а она всегда успешна. Vector
читает этот лог-файл уже потом, из другого контейнера. Обратного сигнального
пути от состояния буфера Vector в `pre_call`/`post_call` нет, поэтому вставший
аудит **не может** вернуть 503 — запросы продолжают уходить наружу. См.
`compose/README.md` «Audit buffering is not fail-closed».

Действия:
1. Проверьте нижележащие sink'и. Вероятно, Langfuse или SIEM лежит/тормозит.
2. Если лежит один sink: остальные продолжают работать. Определите, какой
   именно, по метрикам Vector.
3. Если буфер заполнится, Vector включает back-pressure и перестаёт читать;
   записи остаются в лог-файлах контейнера. Реальную границу сохранности
   задаёт **ротация логов**, а не буфер: как только docker удалит файл за
   пределами `LITELLM_LOG_MAX_FILE`, эти аудит-записи пропадут навсегда.
   Подберите `LITELLM_LOG_MAX_SIZE` × `LITELLM_LOG_MAX_FILE` под самый долгий
   простой, который вы намерены пережить.
4. `docker compose logs vector | grep "Events dropped"` — неверный или
   провёрнутый ключ `CORP_LANGFUSE_*` даёт 401, а его Vector **не**
   повторяет. Это тихая потеря аудита: чинить надо ключ, а не размер буфера.

### Непредвиденная внутренняя ошибка (страховка F8)

Симптом: растёт `gateway_failure{component="internal"}`; запросы возвращают
500 с `error_code="E_INTERNAL"` и без каких-либо подробностей.

Поведение: fail-closed (по матрице). Это перехватчик для исключения, которого
шлюз не ожидал (ошибка БД, баг, отказ audit-sink'а): `pre_call` сводит его к
этому непрозрачному ответу и никогда не отдаёт текст исключения ни клиенту, ни в
лог, ни в аудит-запись. `litellm_pre_call_unexpected_error` логирует только ТИП
исключения, никогда его сообщение. Сбой восстановления ответа (ASGI-десанитайзер)
даёт тот же 500 `E_INTERNAL` (или закрывает уже начатый поток), строку
`gateway_desanitize_failed request_id=… phase=… error=<тип>` и
`gateway_failure{component="desanitize"}`.

Действия:
1. Найдите в логах pod'а шлюза соответствующую строку `*_unexpected_error` и её
   `exc_type=` — она называет класс исключения, не раскрывая сообщение.
2. Если `exc_type` указывает на известную зависимость (Postgres, Redis,
   audit-sink), разбирайте это как инцидент того компонента — этот путь
   страховка, а не первопричина.
3. Запрос, уже заблокированный конкретным компонентом (например,
   `E_DLP_BLOCKED`), в `internal` **не** попадает: обёртка не увеличивает
   счётчик, если по этому запросу уже зафиксирован отказ конкретного
   компонента.
4. Отказ провайдера/транспорта посреди стрима (например,
   `httpx.RemoteProtocolError`) тоже **не** считается `internal` —
   `post_call_stream` оборачивает только свою десанитизацию, а получение
   следующего чанка из upstream-итератора намеренно вынесено за эту защиту.
   Рост `internal` означает баг в собственном pre/post-call коде шлюза, а не
   отказ провайдера и не дубль блокировки конкретного компонента.

### Растёт `gateway_failure{component="audit"}`

Симптом: растёт `gateway_failure{component="audit"}`; в логе шлюза строки
`litellm_audit_orphan_event request_id=… status=…`.

Поведение: само по себе не инцидент. Событие лога litellm пришло для запроса,
по которому у guardrail нет состояния: итоговая аудит-запись запроса уже
записана через его ticket (или pre-call не выполнялся), поэтому событие
отбрасывается и вторая запись «unknown» не пишется. Ничего не теряется, запросы
не затронуты.

Действия: при ровном или редком счётчике — никаких. Если он стабильно растёт
вместе с трафиком, сверьте `request_id` с аудит-записями: у каждого уже должна
быть ровно одна итоговая запись. Запрос без записи вовсе — это инцидент полноты
аудита (см. ниже).

Второй источник — `litellm_guardrail_information_failed request_id=… error=<type>`:
guardrail не смог записать свою запись без контента в `guardrail_information`
litellm (`docs/audit-schema.ru.md`). Запрос и его аудит-запись не затронуты; в
payload и OTEL span litellm для этого запроса записи нет.
`error=GuardrailInformationShapeError` после обновления litellm означает, что
writer litellm собирает запись другой формы: прежде всего сверьте её с allow-list.

### 429 `E_CAPACITY` / 408 `E_BODY_TIMEOUT`

Симптом: клиенты получают 429 с `Retry-After: 1` или 408; растёт
`corp_llm_gateway_blocked_requests_total{block_reason="capacity"}` или
`{block_reason="body_timeout"}`.

Поведение: лимит одновременных запросов (`capacity.ru.md`). 429 — на этом pod'е
заняты все слоты (`CORP_LLM_MAX_INFLIGHT`), все места чтения тела
(`CORP_LLM_MAX_DRAINING`) или бюджет байтов тел
(`CORP_LLM_MAX_DRAINING_BYTES`); 408 — тело не пришло целиком за
`CORP_LLM_BODY_READ_SECONDS`. До litellm и провайдера ничего не дошло.

Действия:
1. `gateway_inflight_requests` на пределе на всех pod'ах: реальная нагрузка.
   Добавьте реплики; не поднимайте лимит выше того, что тянут CPU и память
   одного pod'а.
2. `gateway_draining_bytes` у бюджета при свободных слотах: большие тела.
   Поднимайте `CORP_LLM_MAX_DRAINING_BYTES`, только если позволяет лимит памяти.
3. Постоянные 408 от одного источника при свободных слотах: медленный или
   зависший клиент либо зондирование. На шлюзе чинить нечего.
4. Рядом `gateway_failure{component="route_gate"}`: отменённый запрос не
   завершился за свой бюджет; слот всё равно освобождён. Заведите задачу со
   строкой лога `route_gate_cancel_*`.

### 503 `E_STORE_UNAVAILABLE` / 503 `E_PROFILE_UNAVAILABLE` (Postgres)

Симптом: каждый LLM-запрос отвечает 503; растёт
`gateway_failure{component="token_store"}` (`E_STORE_UNAVAILABLE`) или
`gateway_failure{component="team_config"}` (`E_PROFILE_UNAVAILABLE`).

Поведение: fail-closed. Каждый переписываемый запрос ищет свой corp-токен
(граница 6 с) и читает конфигурацию своей команды (граница 5 с), даже если у
команды нет профилей; хранилище, которое не может ответить, отклоняет запрос, а
не пропускает его без аутентификации или без профиля. `E_PROFILE_UNAVAILABLE`
без `component="team_config"` — это сломанный профиль.

Действия:
1. Проверьте доступность Postgres (и PgBouncer, если он есть) из pod'а; строка
   лога несёт только класс исключения драйвера.
2. За PgBouncer убедитесь, что `ignore_startup_parameters` перечисляет
   параметры keepalive (`configuration.md`, «Backends»).
3. Как только хранилище отвечает, всё восстанавливается само; сбрасывать кэш
   не нужно.

### Выдача токена разработчику не удаётся

Симптом: `scripts/install.sh` печатает HTTP-статус и код ошибки
`POST /internal/issue-token`.

| Код | Статус | Значение / действие |
|---|---|---|
| `E_ISSUE_DISABLED` | 404 | выдача выключена (`CORP_GATEWAY_ISSUE_OIDC_ISSUER` не задан) |
| `E_OIDC_*` | 401 | токен Keycloak не прошёл проверку — клиент, audience или groups-маппер (`install.md`, «Keycloak realm and client») |
| `E_ISSUE_NO_TEAM` / `E_ISSUE_UNKNOWN_TEAM` | 403 | нет группы из карты / команды из карты нет (`gateway-admin team create`) |
| `E_ISSUE_RATE` | 403 | выдача раньше `CORP_GATEWAY_ISSUE_MIN_INTERVAL_SECONDS`; подождите — отзыв это не сбрасывает |
| `E_ISSUE_REPLAY` | 403 | этот токен Keycloak уже использован; запустите установщик ещё раз |
| `E_ISSUE_INFLIGHT` / `E_ISSUE_THROTTLED` | 429 | собственные лимиты маршрута; повторите |
| `E_ISSUE_BUSY` | 503 | блокировка субъекта или запрос к базе не уложились (5 с / 8 с); повторите |
| `E_JWKS_UNAVAILABLE` | 503 | pod не может загрузить JWKS Keycloak — NetworkPolicy (`networkPolicy.keycloak`), CA bundle, сам Keycloak |
| `E_ISSUE_STORE_TIMEOUT` / `E_ISSUE_STORE_UNAVAILABLE` | 503 | Postgres медленный или недоступен (см. выше) |
| `E_ISSUE_SCHEMA` | 503 | pod стартовал при недоступном Postgres и ещё не увидел `corp_tokens` актуальной; `/healthz/ready` называет проблему — примените `tokens/schema.sql` (`upgrade.md`); readiness и маршрут перепроверяют не чаще раза в 15 с, повторите после этого |

### Отзыв токена не подействовал сразу

Симптом: `gateway-admin token revoke --user alice` выполнена, но трафик
Alice ещё идёт ≤ 60 с.

Поведение: 60-секундный кэш отзыва (согласно `AuthMiddleware`).
Задокументированная задержка offboarding.

Действия: подождите 60 с. Если через 60 с трафик всё ещё идёт —
эскалируйте, это настоящий баг.

### Тест инварианта аудита падает в CI

Симптом: `tests/invariants/test_no_originals_leak.py` красный.

Поведение: сборка блокируется. M1-14 — регрессионного уровня, никогда не
обходите.

Действия:
1. Файл перечисляет шесть поверхностей утечки. Найдите, какой assert
   сработал.
2. Отследите регрессию до причины. Чаще всего: кто-то где-то добавил
   `logger.info("...%s", finding.text)`.
3. Устраните утечку; тест фиксирует поверхность.

### Полнота аудита < 100% в ежемесячной проверке

Симптом: месячное число строк в S3 < числа неупавших запросов за месяц.

Поведение: нарушает не подлежащий обсуждению критерий приёмки.

Действия:
1. Продиффьте недостающие записи: какой team_id, какое временное окно?
2. Проверьте метрики Vector в этом окне — заполнение буфера, ошибки
   sink'ов.
3. Если необъяснимо: это уровень инцидента. Поднимите по пейджеру
   security + DRI.

## Частые операции

### Добавить новую команду

```
gateway-admin team create --team-id team-x --name "Team X"
gateway-admin team set-rules --team-id team-x --from-file team-x.replace.md
gateway-admin team set-retention --team-id team-x --hot-days 90 --cold-years 7
```

### Отозвать токены уволенного сотрудника

```
gateway-admin token revoke --user alice
```

Эффект ограничен ≤ 60 с кэшем отзыва. В пределах этого окна токены Alice
остаются валидными.

### Посмотреть, что в `replace.md` команды

Путь — в `team_config.replace_md_path`. Читайте напрямую из файла или
запросите таблицу `team_config`.

## Полезные команды kubectl

> **Имя Deployment'а.** Чарт рендерит `<release>-corp-llm-gateway`
> (`fullname` в `_helpers.tpl`), поэтому для `helm install gw ...` объект
> называется `deploy/gw-corp-llm-gateway`, а не `deploy/gw` или
> `deploy/gateway`. Не зависящая от имени релиза форма — селектор по метке:
> `kubectl -n corp-llm-gateway -l app.kubernetes.io/name=corp-llm-gateway ...`.
> Задайте `fullnameOverride`, если нужно фиксированное имя.


```
kubectl -n corp-llm-gateway get pods
kubectl -n corp-llm-gateway logs deploy/gw-corp-llm-gateway -c litellm
kubectl -n corp-llm-gateway logs deploy/gw-corp-llm-gateway -c vector
kubectl -n corp-llm-gateway exec -it deploy/gw-corp-llm-gateway -c litellm -- python -m corp_llm_gateway.cli.admin team --help
```
