# gdebenz monitor

Сбор сырых snapshot'ов АЗС (Лукойл / Газпром / Газпромнефть) вдоль автомаршрута
Екатеринбург → Новосибирск через публичный API [gdebenz.ru](https://gdebenz.ru/).

Анализ JSON выполняется отдельно. Этот репозиторий только сохраняет данные.

## Локальный запуск

```bat
python collect_snapshot.py
```

Результат:

- `reports/history/YYYY-MM-DD/HHMMZ.json`
- `reports/latest.json`
- `reports/index.json`

## GitHub Actions

Workflow `.github/workflows/gdebenz-monitor.yml`:

- cron: `17 */6 * * *` (UTC)
- `workflow_dispatch`
- коммитит только файлы в `reports/`

Секреты / cookies / `X-RT` в репозиторий не сохраняются.
