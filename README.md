# NFT Offer Bot — инструкция по запуску

Бот готов к деплою на бесплатные платформы (Render, Railway, Fly.io).

---

## ⚠️ Перед запуском

1. **Смени токен бота** — старый уже светился в коде.  
   Зайди в [@BotFather](https://t.me/BotFather) → `/revoke` или создай нового бота.

2. Узнай свой Telegram ID через [@userinfobot](https://t.me/userinfobot).

---

## Вариант 1: Render.com (рекомендую)

### Шаги:

1. Зайди на [render.com](https://render.com) и зарегистрируйся (через GitHub удобнее).

2. Создай **новый репозиторий** на GitHub и загрузи туда всю папку `bot/` (все файлы).

3. В Render:
   - **New +** → **Web Service**
   - Подключи свой GitHub-репозиторий
   - Настройки:
     | Поле | Значение |
     |------|----------|
     | Name | `nft-offer-bot` |
     | Runtime | `Docker` |
     | Instance Type | **Free** |

4. Добавь **Environment Variables**:

   | Key | Value |
   |-----|-------|
   | `BOT_TOKEN` | твой новый токен от BotFather |
   | `ADMIN_PASSWORD` | пароль для веб-панели |
   | `ADMIN_IDS` | твой Telegram ID (например `8304808772`) |

5. Нажми **Create Web Service**.

6. После деплоя веб-панель будет доступна по адресу вида:
   `https://nft-offer-bot.onrender.com`

> **Важно:** на бесплатном тарифе Render засыпает сервис после 15 минут бездействия.  
> Бот будет "просыпаться" при первом запросе (это нормально для бесплатного тарифа).  
> Для постоянной работы лучше взять самый дешёвый платный план (~$7/мес) или Railway.

---

## Вариант 2: Railway.app

1. Зайди на [railway.app](https://railway.app) → Login with GitHub.

2. **New Project** → **Deploy from GitHub repo**.

3. Выбери репозиторий с ботом.

4. Railway сам определит Dockerfile.

5. Добавь переменные окружения (те же, что выше):
   - `BOT_TOKEN`
   - `ADMIN_PASSWORD`
   - `ADMIN_IDS`

6. Deploy.

Railway даёт $5 бесплатных кредитов каждый месяц — для одного бота обычно хватает.

---

## Вариант 3: Локальный запуск (для теста)

```bash
# 1. Создай виртуальное окружение
python -m venv venv
source venv/bin/activate   # Linux/Mac
# или venv\Scripts\activate  # Windows

# 2. Установи зависимости
pip install -r requirements.txt

# 3. Установи переменные
export BOT_TOKEN="твой_токен"
export ADMIN_PASSWORD="твой_пароль"
export ADMIN_IDS="твой_id"

# 4. Запусти
python bot.py
```

Веб-панель: http://localhost:8080

---

## Переменные окружения

| Переменная | Обязательная | По умолчанию | Описание |
|------------|--------------|--------------|----------|
| `BOT_TOKEN` | ✅ | — | Токен от @BotFather |
| `ADMIN_PASSWORD` | ✅ | `admin123` | Пароль веб-панели |
| `ADMIN_IDS` | ✅ | — | Telegram ID админов (через запятую) |
| `PORT` | — | `8080` | Порт (Render/Railway ставят сами) |
| `SUBSCRIPTION_STARS` | — | `555` | Цена подписки в Stars |
| `SUBSCRIPTION_DAYS` | — | `30` | Дней подписки |
| `SUBSCRIPTION_GRAM` | — | `4.8` | Цена в GRAM |
| `GRAM_WALLET` | — | — | Кошелёк для приёма GRAM |

---

## После запуска

1. Напиши боту `/start` — должен ответить.
2. Подключи Business Mode в настройках Telegram Business.
3. Веб-панель: открой URL сервиса → введи пароль из `ADMIN_PASSWORD`.

---

## Ограничения бесплатных тарифов

- **Render Free**: засыпает через 15 мин простоя, просыпается ~30–50 сек.
- **Railway**: $5 кредитов/мес, обычно хватает.
- Данные (`users.json`) хранятся на диске контейнера — при пересоздании контейнера могут потеряться.  
  Для серьёзного использования позже можно перейти на PostgreSQL / SQLite + volume.
