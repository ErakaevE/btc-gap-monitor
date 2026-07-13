# BTC Gap Monitor — Deribit vs Polymarket

Сканер расхождений между risk-neutral вероятностями из опционов Deribit и ценами
BTC-рынков Polymarket. GitHub Actions запускает сканер каждые ~30 минут,
результаты сохраняются в репозиторий, дашборд отдаётся через GitHub Pages.

## Установка (5 минут, всё бесплатно)

1. **Создайте репозиторий.** На github.com нажмите **New repository**,
   имя например `btc-gap-monitor`, тип **Public** (Pages бесплатен только
   для публичных), создайте.

2. **Загрузите файлы.** В репозитории: **Add file → Upload files** —
   перетащите всё содержимое этой папки (включая скрытую папку `.github` —
   при загрузке через браузер проще перетащить папки целиком). Или через git:

   ```
   cd btc-gap-monitor
   git init && git add -A && git commit -m init
   git branch -M main
   git remote add origin https://github.com/ВАШ_ЛОГИН/btc-gap-monitor.git
   git push -u origin main
   ```

3. **Разрешите Actions писать в репозиторий.**
   Settings → Actions → General → Workflow permissions →
   **Read and write permissions** → Save.

4. **Включите Pages.** Settings → Pages → Source: **Deploy from a branch** →
   Branch: `main`, папка `/docs` → Save.

5. **Первый запуск.** Вкладка **Actions** → workflow `btc-gap-scan` →
   **Run workflow**. Через минуту-две данные закоммитятся.

Дашборд: `https://ВАШ_ЛОГИН.github.io/btc-gap-monitor/`
(первая публикация Pages занимает пару минут).

## Что на дашборде

- Таблица рынков: вероятность Polymarket vs Deribit, гэп, чистый edge
  (за вычетом taker-fee), рекомендуемая сторона и размер позиции (¼ Kelly,
  кэп 20% от $10k), ликвидность. Сортировка по клику на заголовок.
- Ползунок порога сигнала (строки с edge выше порога подсвечиваются).
- График истории: лучший edge и число сигналов по времени.

## Настройка

- Расписание: `.github/workflows/scan.yml`, строка `cron` (GitHub иногда
  задерживает запуски на 5–15 минут; чаще чем раз в 5 минут нельзя).
- Параметры сканера (порог, мин. ликвидность): там же, аргументы запуска
  `btc_gap_scanner.py`. Локальный запуск: `python3 btc_gap_scanner.py --help`.

## Дисклеймер

Модель упрощена (r=0, lognormal, непрерывный мониторинг барьера). У Polymarket
и Deribit разные reference-цены и время резолюции — это базисный риск.
Не финансовый совет.
