# Another VPN Bot (@davinchiyybot)

Telegram-бот продаёт VPN-доступ за Telegram Stars и выдаёт ссылку-подписку с QR-кодом.

- Цена: 50 Stars за 30 дней
- Бесплатный бессрочный пробный доступ (1 раз на пользователя)
- Выдача ключа после оплаты, по кнопке «Мой ключ» и «Пробный доступ»
- QR-код прикрепляется к сообщению с ключом
- Админ-команды: `/stats`, `/balance`

## Переменные окружения

| Переменная | Обязательно | Значение |
|---|---|---|
| `BOT_TOKEN` | да | токен от @BotFather |
| `STARS_PRICE` | нет | `50` |
| `ADMIN_ID` | да | Telegram ID владельца |
| `ISSUE_KEY` | да | ссылка-подписка, которая выдаётся покупателям |
| `PROXY_URL` | нет | на сервере не нужен |
| `PORT` | да (на хостинге) | выдаётся Render автоматически |

## Рабочий деплой

- Сервис: `srv-dbp2tu56e07s73ammd70` (Render, Free, Frankfurt)
- URL: https://another-vpn-bot.onrender.com
- Health: https://another-vpn-bot.onrender.com/health
- Auto-deploy выключен: чтобы передеплоить после изменений, нужен ручной деплой
  через API/дашборд (иначе ежедневный коммит `keepalive.txt` перезапускал бы бота).

## Деплой на Render (бесплатно)

1. https://dashboard.render.com → **New → Web Service**.
2. **Public Git repository**: `https://github.com/danYx1/another-vpn-bot`.
3. Runtime: **Docker**, Branch: `main`.
4. Instance Type: **Free**.
5. Environment variables:
   - `BOT_TOKEN`
   - `ISSUE_KEY`
   - `ADMIN_ID`
   - `STARS_PRICE=50`
6. Health Check Path: `/health`.
7. **Create Web Service**.

Бот слушает `$PORT` (health-сервер) и параллельно делает long polling Telegram.

## Чтобы бесплатный сервис не засыпал

Render Free останавливает сервис после 15 минут без входящих запросов.
GitHub Actions workflow `.github/workflows/keepalive.yml` пингует `/health`
каждые 5 минут, поэтому контейнер не спит.

GitHub отключает scheduled workflows в публичных репозиториях после 60 дней
без активности, поэтому `.github/workflows/heartbeat.yml` раз в сутки обновляет
`keepalive.txt` (коммит от `GITHUB_TOKEN`). Это держит расписание включённым.

Если GitHub задержит пинг больше 15 минут, Render усыпит контейнер; следующий
пинг разбудит его за ~30-60 секунд. Бот снова начнёт принимать сообщения сам.

## Данные

На бесплатном тарифе диск эфемерный: `data/users.json` и `data/payments.json`
сбрасываются при передеплое. На доступ это не влияет — ключ один и тот же
для всех, но история платежей и сроки подписок теряются.
