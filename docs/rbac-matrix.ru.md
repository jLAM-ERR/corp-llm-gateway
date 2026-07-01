# Матрица RBAC

Ссылка на план: M3-8 (роли Langfuse), M2-5 (admin CLI), M8-5.

Три роли, отображаемые через claim'ы Keycloak. Согласно плану rev-3,
схема намеренно плоская — Keycloak Authorization Services (UMA) отложены
до v2.

## Роли

| Роль | Claim в Keycloak | Кому выдаётся |
|---|---|---|
| **Operator** | `gateway:operator` | Дежурная ротация platform engineering |
| **Auditor** | `gateway:auditor` | Security, compliance, SRE-лиды |
| **Developer** | (без claim — по умолчанию) | Все аутентифицированные корп-разработчики |

## Права

| Возможность | Developer | Auditor | Operator |
|---|---|---|---|
| Отправлять запросы через шлюз | ✓ | ✓ | ✓ |
| `gateway-admin team create` | – | – | ✓ |
| `gateway-admin team set-rules` | – | – | ✓ |
| `gateway-admin team set-retention` | – | – | ✓ |
| `gateway-admin token revoke` | – | – | ✓ |
| Читать собственные записи аудита (self-trace в Langfuse) | ✓ | ✓ | ✓ |
| Читать записи аудита всех команд (Langfuse) | – | ✓ | – |
| Читать холодное хранилище аудита в S3 | – | ✓ (только чтение) | ✓ (чтение/запись для ops) |
| Просматривать алерты SIEM | – | ✓ | ✓ |
| Изменять конфигурацию Vector / Helm | – | – | ✓ |
| Изменять NetworkPolicy / CoreDNS sinkhole | – | – | ✓ |
| Раскрытие маппинга (break-glass) | – | – | – (отложено до v2) |

## «Без раскрытия маппинга» в v1

Согласно списку отложенного-до-v2 и таблице рисков в плане: в v1 даже
Operator и Auditor не могут восстановить оригинал из плейсхолдера.

Что делать, когда раскрытие всё же нужно (редко): успеть в пределах окна
10-часового Cache A TTL — маппинг на стороне шлюза ещё в Redis, и
оператор с прямым доступом к Redis может его посмотреть. После 10 ч
маппинг исчезает, и раскрытие невозможно.

Процесс раскрытия break-glass / dual-control появится в v2 в репозитории
`corp-llm-gateway-breakglass`.

## Как выдаются claim'ы

Администратор Keycloak выдаёт claim `gateway:operator` или
`gateway:auditor` учётным записям пользователей. Auth-middleware шлюза
считывает эти claim'ы из OIDC-токена во время device-flow обмена (M2-3) и
сохраняет их в `corp_tokens.scopes`.

CLI `gateway-admin` (M2-5) гейтит каждую подкоманду по наличию
`gateway:operator` в `scopes`. Аутентификация на уровне CLI использует
тот же корп-токен, что и аутентификация на уровне запроса, — отдельного
admin-токена нет.

## Обеспечение соблюдения

Обеспечивается на трёх уровнях:

1. **CLI gateway-admin** — проверяет scope перед выполнением. При отказе
   аутентификации возвращает ненулевой код выхода.
2. **Langfuse** — маппинг claim'ов Keycloak настроен согласно M3-8.
3. **S3** — IAM-политика привязана к членству в группах Keycloak.

## Логирование аудита-над-аудитом

Каждое действие Operator (token revoke, set-rules, set-retention) само
логируется в pipeline аудита с полями `command_name` и `actor_user_id`.
Auditor'ы могут читать этот след. Структурно это хук
«аудита-над-аудитами»; отдельный Object-Locked бакет, упомянутый в списке
отложенного-до-v2, — улучшение следующей итерации.
