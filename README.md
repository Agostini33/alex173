# System
Вы — Codex-ассистент, программист уровня Senior, невероятно креативный и работоспособный. 
Ваша задача — дорабатывать и развивать SaaS-проект WB6.
Репозиторий: github.com/Agostini33/alex173. Ветки: main (prod), dev (working).
Backend: FastAPI (Docker), Frontend: static HTML (Vercel).
Задачи создавать как Pull-Request в ветку dev, покрывать код базовыми тестами.

##Короткое описание (TL;DR)
WB6 — SaaS-сервис, который за 🚀 5 секунд перезаписывает карточку Wildberries (или Ozon): генерирует SEO-заголовок ≤ 100 симв, 6 буллитов ≤ 120 симв и 20 CSV-ключей.
Схема MVP:
front (Vercel Static) → форма запроса
api (Railway/FastAPI) → обрабатывает /rewrite, вызывает GPT-4o-mini по PROMPT v0.3 и возвращает JSON
Robokassa ResultURL → начисляет кредиты (JWT‐token)
учёт кредитов (3 free / 15 / 60 / 200) без БД — в JWT

## Общая логика
- endpoint /rewrite получает JSON {"supplierId","prompt"} и возвращает JSON с title/bullets/keywords + JWT token.
- при NO_CREDITS front должен редиректить на /pay.html
- Robokassa ResultURL начисляет 15, 60 или 200 кредитов.

## TODO-лист 
1. Исправить ошибку: "backend (https://api.wb6.ru) не разрешает запросы от http://wb6.ru, потому что в CORS-настройках не указан этот origin"
2. Добавить поддержку Ozon marketplace (выбор в запросе).
3. Валидация Robokassa CRC в tests.

Работайте инкрементально, каждый PR ≤ 200 строк.


## 🎯 Цель проекта WB6

WB6 — это веб-сервис, который с помощью OpenAI переписывает карточки товаров (title, bullets, keywords) по описанию. Поддерживает Wildberries и Ozon.  
Фронтенд — на Vercel, бэкенд — FastAPI на Railway, оплата — Robokassa, лимиты — JWT токенами.

## 🧠 Функциональность:

- Ввод описания или ссылки на карточку
- Генерация SEO-заголовка, буллитов, ключей через GPT
- 3 бесплатных запроса (quota)
- Платное пополнение через Robokassa
- Хостинг: frontend (`/frontend`) на Vercel, backend (`/backend`) на Railway
- Поддержка Ozon и Wildberries

## 🔐 Авторизация и токены:

- JWT выдаётся при первом заходе
- Хранится в `localStorage`
- Лимит запросов (`quota`) — 3 или больше в зависимости от тарифа
- После `NO_CREDITS` — переход на `pay.html`

## Оплата

IsTest сейчас не используется: тестирование идёт в боевом режиме.

Настройка Robokassa:
- **Result URL:** `https://api.wb6.ru/payhook` (POST)
- **Success URL:** `https://wb6.ru/pay.html` (GET)
- **Fail URL:** `https://wb6.ru/pay.html` (GET)

## 🧪 CI/CD:

- GitHub Actions для backend и frontend
- Railway + Vercel деплой
- Secrets: OPENAI_API_KEY, JWT_SECRET, ROBOKASSA_PASS2 и др.

## 📌 Тех. стек:

- FastAPI
- JS + HTML 
- OpenAI GPT-4 (via `acreate`)
- Railway + Vercel + Robokassa

## ✅ Подзадачи (Issues):

- [ ] Task#1: "backend (https://api.wb6.ru) не разрешает запросы от http://wb6.ru, потому что в CORS-настройках не указан этот origin"
- [ ] Task#2: migrate to async OpenAI client
- [ ] Task#3: unit test for Robokassa signature
- [ ] Task#4: cron daily-report → Telegram
- [ ] Task#5: add usage endpoint `/usage`

## Сбор контактов v2

1. Сначала ищем продавцов:

```bash
python utils/search_scraper.py --query "носки" --pages 1 --output raw_sellers.csv
```

2. Затем собираем контакты:

```bash
python utils/social_scraper.py --input raw_sellers.csv --output socials.csv
```

`socials.csv` содержит колонки `telegram`, `whatsapp`, `email`, `phone`, `site` для каждого продавца.

### Сбор контактов v2

```bash
# Быстрый (только описания товаров)
python social_scraper.py --input raw.csv --output socials.csv
# Полный (до 100 JS-рендеров)
python social_scraper.py --input raw.csv --output socials.csv --render-limit 100
```

## Сбор контактов v3

Новая версия использует ИНН продавца. Для каждого `supplier_id` сначала
считывается страница продавца и извлекается ИНН, затем выполняется запрос к
`api-fns.ru` и при отсутствии данных парсится страница
`zachestnyibiznes.ru`. Результат сохраняется в `socials.csv` с колонками
`inn`, `email`, `phone`, `telegram`, `whatsapp`, `site`.

```bash
python utils/social_scraper.py --input raw_sellers.csv --output socials.csv
```
