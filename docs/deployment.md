# Запуск на Linux-сервере — S6-01

Состояние: 20.09.2026. Файлы и инструкция подготовлены и проверены локально (синтаксис юнитов и скрипта, `--check` на Windows-окружении разработчика); **на реальном сервере не выполнялись** — провайдер и оплата сервера согласуются отдельно (roadmap S6-01). Пока сервера нет, размещение и непрерывная работа выполненными не считаются.

## Что нужно

- Linux с systemd: Ubuntu 22.04/24.04 или Debian 12. 1 vCPU, 1 ГБ памяти, 10 ГБ диска хватает: бот один, БД — SQLite, файлы — до 10 МБ на загрузку.
- Python 3.12+ (`apt install python3.12 python3.12-venv git`). Исходящий доступ в интернет к `api.telegram.org`, `api.checko.ru` и, позже, к провайдеру ИИ; входящих портов не нужно — бот работает через long polling.
- Отдельный бот у @BotFather для сервера (не тот же токен, что у разработчиков: два polling-процесса на одном токене мешают друг другу).

## Раскладка

| Что | Где | Владелец, права |
| --- | --- | --- |
| Код | `/opt/claims-assistant/app` (git checkout) | `root`, служба только читает |
| Окружение Python | `/opt/claims-assistant/venv` | `root`, служба только читает |
| Настройки | `/etc/claims-assistant/env` | `root:claims`, `0640` — токен читает только служба |
| База, загрузки, копии | `/var/lib/claims-assistant/{claims.sqlite3,uploads,backups}` | `claims`, `0750` — единственное место, куда служба пишет |
| Журнал | journald (`journalctl -u claims-assistant`) | — |

Служба работает от системного пользователя `claims` без входа; `ProtectSystem=strict` и `ReadWritePaths` не дают ей писать куда-либо кроме данных.

## Установка

```bash
sudo apt install -y python3.12 python3.12-venv git
sudo git clone https://github.com/Sopkas/artemProd.git /opt/claims-assistant/app   # или scp репозитория
sudo bash /opt/claims-assistant/app/deploy/install.sh main
sudoedit /etc/claims-assistant/env      # TELEGRAM_BOT_TOKEN, ALLOWED_TELEGRAM_IDS, при необходимости DATA_PROVIDER/CHECKO_API_KEY
sudo bash /opt/claims-assistant/app/deploy/install.sh main   # второй запуск: проверка настроек и старт
```

`deploy/install.sh` идемпотентен: создаёт пользователя и каталоги, клонирует или обновляет код до указанного ref (тег выпуска или `main`), ставит зависимости из `requirements.txt` в отдельный venv, кладёт юниты, выполняет `python -m claims_assistant --check` через `systemd-run` от имени службы с тем же `EnvironmentFile` (файл настроек разбирает systemd, а не shell — проверка и служба видят одни и те же значения) и запускает её. Код и venv принадлежат root — служба их только читает. Данные и `/etc/claims-assistant/env` он не перезаписывает.

`--check` (та же команда стоит в `ExecStartPre`) — проверка без сети: настройки читаются, БД открывается и мигрирует, каталоги данных и копий доступны на запись. Печатает `config_ok data_provider=… ai_provider=…`; служба не стартует, пока проверка не прошла — сломанная настройка не доходит до пользователей.

## Работа

- Состояние: `systemctl status claims-assistant`; журнал: `journalctl -u claims-assistant -f` (ищите `bot_started`, `run_finished`, `retention_sweep`; токены и ключи из журнала вырезаются).
- Остановка/запуск: `systemctl stop|start|restart claims-assistant`.
- **Автоперезапуск**: `Restart=always`, пауза 10 с, не больше 5 стартов за 5 минут. **Если лимит исчерпан, служба остаётся в состоянии `failed` и сама не поднимется** — это заметно только снаружи: `systemctl is-failed claims-assistant` (cron-пинг раз в 5 минут с уведомлением, пока нет мониторинга) или `OnFailure=` с юнитом-уведомлением, когда появится куда слать; поднять — `systemctl reset-failed claims-assistant && systemctl start claims-assistant` после разбора журнала. Проверки, прерванные перезапуском, возвращаются в очередь при старте (S3-03), незавершённые после трёх прерываний — останавливаются.
- **Копии**: `claims-assistant-backup.timer` — ежедневно в 03:30 по времени сервера (`systemctl list-timers`), снимки в `/var/lib/claims-assistant/backups`, хранится `BACKUP_KEEP`. Копии — те же данные должников: вынести на другой диск или в хранилище с тем же сроком хранения. Проверка/восстановление — [operations.md](operations.md).
- **Ровно один экземпляр**: юнит не запускает второй, но если бот запущен ещё где-то (у разработчика с тем же токеном) — Telegram будет отдавать обновления то одному, то другому. Серверу — отдельный бот.

## Обновление и откат

По [operations.md](operations.md): снимок → `systemctl stop claims-assistant` → `install.sh <тег>` (сам обновит код и зависимости, проверит настройки и запустит) → ручная проверка `/start`, «Статус». Откат — `install.sh <предыдущий тег>` и, если менялась схема БД, действия из operations.md.

## Что не сделано без сервера

- Не проверено: установка на чистой ОС, работа юнитов, **таймер копий и что `claims-assistant-backup.service` пишет в тот же каталог под теми же `ReadWritePaths`**, перезапуск после `kill -9`, состояние `failed` после пяти падений, права `ProtectSystem`. Первый реальный запуск — с этим списком в руках; результат записать в карточку S6-01.
- Не настроено: мониторинг снаружи (нужен хотя бы cron-пинг `systemctl is-active` с уведомлением), ротация journald сверх умолчаний, обновления ОС.
- Провайдер сервера и оплата — согласовать отдельно (roadmap).
