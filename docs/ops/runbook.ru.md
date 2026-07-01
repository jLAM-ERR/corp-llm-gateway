# Runbook эксплуатации

Ссылка на план: M8-2.

## Ежедневные операции

### Деплой новой версии

1. Пометьте релиз тегом: `git tag v0.x.y && git push origin v0.x.y`.
2. CI собирает wheel и Helm-артефакты на теге.
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

### Упал движок пред-пасса

Симптом: растёт `gateway_failure{component="pre_pass"}`; запросы проходят,
но медленнее (путь только через корп-LLM).

Поведение: continue (по матрице).

Действия:
1. Масштабируйте реплики пред-пасса: `kubectl scale -n corp-llm-gateway deploy/pre-pass --replicas=2`.
2. Разберитесь с нижележащим CPU-pod'ом (OOM? падение воркера? необычно
   большой payload, превышающий порог M1-11?).

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

Поведение (по умолчанию): fail-closed на 100% (по матрице).

Действия:
1. Проверьте нижележащие sink'и. Вероятно, Langfuse или SIEM
   лежит/тормозит.
2. Если лежит один sink: остальные продолжают работать. Определите, какой
   именно, по метрикам Vector.
3. Если буфер заполняется: запросы начинают возвращать 503. Пересмотрите
   переопределение fail-policy на уровне команды, если это критично для
   бизнеса.

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

```
kubectl -n corp-llm-gateway get pods
kubectl -n corp-llm-gateway logs deploy/gateway -c litellm
kubectl -n corp-llm-gateway logs deploy/gateway -c vector
kubectl -n corp-llm-gateway exec -it deploy/gateway -c litellm -- python -m corp_llm_gateway.cli.admin team --help
```
