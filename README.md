# WarpFlow backend

## Установка

```powershell
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements-dev.txt
```

## Запуск

```powershell
.venv\Scripts\python -m uvicorn --factory app.main:create_app --reload
```

API: http://localhost:8000/docs

Сервер должен работать **одним процессом** (без `--workers`): объём идущих загрузок считается в памяти, а сессия Telegram — одна.
За reverse proxy добавьте `--proxy-headers`, иначе лимиты будут считаться по IP прокси.

Если после обновления сервер пишет `outdated schema` — удалите `storage/warpflow.db` (и папку `storage/files`).

## Настройки (переменные окружения)

| Переменная | По умолчанию | Описание |
|---|---|---|
| `WARPFLOW_STORAGE_DIR` | `storage` | Папка для файлов и SQLite |
| `WARPFLOW_PUBLIC_URL` | `http://localhost:5173` | Адрес сайта для ссылок в письмах и сообщениях (`https://warpflow.ru`) |
| `WARPFLOW_CORS_ORIGINS` | `http://localhost:5173` | Разрешённые origin через запятую |
| `UNISENDER_API_KEY` | — | Ключ Unisender Go; без него отправка по почте отключена |
| `UNISENDER_URL` | `https://goapi.unisender.ru/...` | Эндпоинт `email/send.json` |
| `WARPFLOW_MAIL_FROM` | `no-reply@warpflow.ru` | Адрес отправителя (домен подтверждён в Unisender Go) |
| `TELEGRAM_API_ID`, `TELEGRAM_API_HASH` | — | С my.telegram.org; без них отправка в Telegram отключена |
| `TELEGRAM_SESSION` | `telegram.session` | Файл сессии Telegram-аккаунта |
| `WARPFLOW_STATS_PASSWORD` | — | Пароль сайта статистики (логин любой) |
| `WARPFLOW_APP_URL` | `http://127.0.0.1:8000` | Где статистика находит основной сайт (`/api/health`) |
| `WARPFLOW_TIMEZONE` | `Europe/Moscow` | Часовой пояс для «сегодня» и графиков |

## Статистика (st.warpflow.ru)

Отдельное приложение над той же базой: состояние сервера (процессор, память, диск, сеть), работают ли сайт, почта и Telegram, что сегодня отправили, загрузили и скачали, графики по дням и часам. Закрыто паролем (`WARPFLOW_STATS_PASSWORD`; без него не открывается).

```powershell
.venv\Scripts\python -m uvicorn --factory app.stats.main:create_stats_app --port 8001
```

Цифры берутся из журнала событий (таблица `events`, хранится 90 дней): в нём нет IP, имён и текстов — посетитель считается по хэшу IP с датой.

## Вход в Telegram

Один раз, с заданными `TELEGRAM_API_ID` и `TELEGRAM_API_HASH`:

```powershell
.venv\Scripts\python -m app.tg_login
```

Введите номер телефона, код из Telegram и пароль 2FA (если есть). Файл сессии даёт полный доступ к аккаунту — не коммитьте его и не передавайте.

## Тесты

```powershell
.venv\Scripts\python -m pytest
```

Почта и Telegram в тестах подменены заглушками — реальные письма и сообщения не отправляются.
