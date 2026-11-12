# Billing monitor: расходы OpenAI по клиентам

Каждый день проверяет, сколько из предоплаты клиента ушло на токены OpenAI, и пишет в Slack (#ru_payments), когда бюджет подходит к концу.

## Как считается

```
оплачено клиентом            26 000 ₽
− наша комиссия 10%          −2 600 ₽
= бюджет на токены           23 400 ₽
потрачено = расходы проекта OpenAI в $ с даты оплаты × (курс ЦБ × 1,08 ≈ курс Райфа)
остаток   = бюджет − потрачено
```

- Остаток ≤ 5 000 ₽ → сообщение «пора выставить новый счёт» с тегом.
- Остаток ≤ 0 ₽ → сообщение «бюджет закончился».
- Каждое сообщение уходит один раз за период оплаты. Что уже отправлено, хранится в `state.json` (бот сам коммитит его в репозиторий).
- Расходы клиента разделяются через отдельный **Project** в OpenAI: у бота клиента должен быть ключ из его проекта.

## Настройка (один раз)

1. **Admin-ключ OpenAI.** platform.openai.com → Settings → Organization → Admin keys → Create (Restricted, Read на Usage). Ключ начинается с `sk-admin-`. Создать может только владелец организации.
2. **Slack-вебхук** в канал #ru_payments: api.slack.com/apps → Create New App → Incoming Webhooks → Add New Webhook → выбрать #ru_payments.
3. **Секреты репозитория:** Settings → Secrets and variables → Actions → New repository secret:
   - `OPENAI_ADMIN_KEY` — Admin-ключ
   - `SLACK_WEBHOOK_URL` — вебхук
4. **clients.json:** вписать `project_id` клиента, сумму и дату оплаты, свой Slack Member ID в `slack_mention` (профиль в Slack → «⋮» → Copy member ID).
5. **Проверка:** Actions → Billing monitor → Run workflow → `status`. В #ru_payments придёт текущий остаток.

## Обычная работа

- Запуск сам, каждый день в 09:07 UTC (12:07 МСК). Время меняется в `.github/workflows/billing-monitor.yml`.
- **Клиент снова заплатил** → в `clients.json` поменять `prepaid_rub` и `period_start` на новую дату. Уведомления сбросятся сами.
- **Новый клиент** → создать ему Project в OpenAI, перевести его бота на ключ из этого проекта, добавить блок в `clients` в `clients.json`.
- **Курс** → раз в месяц-два сверять коэффициент `cbr_multiplier` с реальным курсом Райфа и обновлять. Можно задать фиксированный курс клиенту полем `rub_per_usd`.
- Если запуск упал, в Slack придёт сообщение со ссылкой на лог.

## Поля clients.json

| Поле | Что значит |
| --- | --- |
| `name` | Название клиента в сообщениях |
| `project_id` | ID проекта в OpenAI (`proj_…`) |
| `prepaid_rub` | Сколько клиент заплатил, ₽ |
| `period_start` | Дата этой оплаты, `ГГГГ-ММ-ДД` |
| `commission_pct` | Наша комиссия, % (по умолчанию 10) |
| `cbr_multiplier` | Коэффициент к курсу ЦБ (1,08 ≈ Райф) |
| `rub_per_usd` | Фиксированный курс вместо ЦБ × коэффициент |
| `alert_thresholds_rub` | Пороги уведомлений, ₽ (по умолчанию `[5000, 0]`) |
| `slack_mention` | Slack Member ID (или список), кого тегать |
| `enabled` | `false` — временно не проверять клиента |
| `message_low` / `message_exhausted` | Свой текст сообщений. Подстановки: `{mention} {name} {paid} {commission} {budget} {spent} {spent_usd} {rate} {remaining} {percent} {period_start}` |

Поля из `defaults` действуют для всех клиентов, поле у клиента их перекрывает.

## Локальный запуск

```
OPENAI_ADMIN_KEY=sk-admin-... DRY_RUN=1 python3 billing_monitor.py clients.json
```

`DRY_RUN=1` ничего не отправляет и не сохраняет, только печатает отчёт.
