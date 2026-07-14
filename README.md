# GNOME Desktop Bridge

Локальный, управляемый пользователем мост для наблюдения, отладки и автоматизации
GNOME Desktop на Wayland. Проект даёт локальному AI-клиенту или диагностическому
скрипту семантический доступ к интерфейсу приложений через AT-SPI и, только после
явного согласия пользователя в системном диалоге GNOME, управление указателем и
клавиатурой через XDG Desktop Portal.

> **Статус:** рабочий MVP, рассчитанный на GNOME 50 / Fedora 44 и Python 3.11+.
> По умолчанию всё выключено. Это мощный инструмент, а не граница безопасности
> между недоверенными процессами одного Linux-пользователя.

## Коротко о возможностях

- локальный HTTP/JSON API на `127.0.0.1:18766`;
- постоянный случайный bearer-токен с правами файла `0600`;
- четыре режима: `Off`, `Observe`, `Control selected apps`, `ALL DESKTOP`;
- allowlist приложений для обычного Control-режима;
- чтение семантического дерева окон через AT-SPI;
- поиск ролей, названий, состояния, bounds, текста и доступных действий элементов;
- вызов нативных AT-SPI actions, фокус и заполнение editable-полей;
- автоматическая маскировка AT-SPI password fields;
- скриншоты через XDG Screenshot portal;
- глобальные клики, движение мыши, скролл, клавиши и ввод текста через
  XDG RemoteDesktop portal;
- запуск `.desktop`-приложений через отдельный transient systemd unit;
- журнал команд в памяти и SSE-поток событий в реальном времени;
- нативная GTK/libadwaita панель управления;
- аварийный `STOP ALL`, который закрывает portal-сессию и возвращает режим `Off`;
- systemd user service с hardening-настройками;
- CLI для человека и автоматизированного клиента.

## Что проект принципиально не делает

- не обходит Wayland и системный диалог разрешений GNOME;
- не получает root и не выполняет команды shell через API;
- не управляет экраном входа, заблокированной сессией или другим Linux-пользователем;
- не читает произвольную память процессов;
- не получает внутренние логи других приложений или расширений автоматически —
  только то, что они показывают через AT-SPI, на экране или в обычных файлах/журналах,
  к которым уже имеет доступ текущий пользователь;
- не открывает API в LAN;
- не разрешает сайтам обращаться к API через CORS;
- не позволяет API-клиенту самостоятельно включить более опасный режим доступа.

## Модель доступа

| Режим | Чтение UI | Скриншот | AT-SPI actions | Область приложения | Глобальные input events |
|---|---:|---:|---:|---|---:|
| `off` | нет | нет | нет | — | нет |
| `observe` | да | по feature-gate | нет | любое видимое AT-SPI приложение | нет |
| `control` | да | по feature-gate | да | только `allowedApps` | нет |
| `all` | да | по feature-gate | да | весь desktop | только по отдельному feature-gate и portal consent |

Команды `ping`, `capabilities`, `get_state` и `stop_all` доступны при любом режиме,
но требуют токен. Единственный публичный endpoint — минимальный `/health`.

### Почему существуют и режим, и feature-gate

Разрешение проверяется в двух местах. Например, глобальный клик требует одновременно:

1. `accessMode: "all"`;
2. `allowPortalInput: true`;
3. активную RemoteDesktop-сессию, которую пользователь подтвердил в диалоге GNOME;
4. фактически выданное portal-разрешение на pointer.

Изменение настроек на более безопасные автоматически закрывает активную portal-сессию
не позднее чем через одну секунду. Это делает переключатель в Control Center настоящим
revoke-механизмом, а не только настройкой для следующего запроса.

`ALL DESKTOP` дополнительно является session-scoped: после crash, logout, reboot или
ручного restart daemon он автоматически сбрасывается в `Off`, а global-input gate
выключается. `Observe` и scoped `Control` могут сохраняться между перезапусками.

## Архитектура

```text
AI / CLI / diagnostic process
          |
          | HTTP + Bearer token, loopback only
          v
  gnome-desktop-bridge daemon
          |
          +-- Policy ------ settings.json (human-owned)
          +-- EventBuffer - JSON audit + SSE
          +-- AT-SPI ------ semantic UI tree/actions
          +-- XDG Portal -- screenshot + consented input
          +-- Gio/systemd - desktop app discovery/launch
          |
          v
  Active GNOME user session on Wayland

GNOME Desktop Bridge Control Center
          |
          +-- changes settings.json directly
          +-- starts/stops portal session through daemon
          +-- can always invoke STOP ALL
```

Решение намеренно не является GNOME Shell extension. Основная логика работает как
обычный user service: это проще тестировать, обновлять и изолировать. Shell indicator
можно добавить позже как необязательный визуальный frontend.

### Backends

- **AT-SPI 2** — семантическое дерево, roles, states, text, actions, focus, editable text.
- **XDG Screenshot portal** — снимок выбранного/разрешённого системной политикой экрана.
- **XDG RemoteDesktop + ScreenCast portals** — системный consent, pointer/keyboard events,
  stream geometry для нескольких мониторов.
- **Gio AppInfo** — список desktop launchers.
- **systemd-run + gtk-launch** — запуск приложения вне sandbox namespace демона.

Официальные спецификации:

- [RemoteDesktop portal](https://flatpak.github.io/xdg-desktop-portal/docs/doc-org.freedesktop.portal.RemoteDesktop.html)
- [ScreenCast portal](https://flatpak.github.io/xdg-desktop-portal/docs/doc-org.freedesktop.portal.ScreenCast.html)
- [libei](https://libinput.pages.freedesktop.org/libei/)
- [AT-SPI Accessible](https://gnome.pages.gitlab.gnome.org/at-spi2-core/libatspi/class.Accessible.html)
- [AT-SPI Action](https://gnome.pages.gitlab.gnome.org/at-spi2-core/libatspi/iface.Action.html)
- [AT-SPI EditableText](https://gnome.pages.gitlab.gnome.org/at-spi2-core/libatspi/iface.EditableText.html)

## Требования

Типичная Fedora Workstation с GNOME уже содержит всё необходимое:

- GNOME + Wayland;
- Python 3.11+;
- PyGObject;
- `at-spi2-core`;
- `xdg-desktop-portal` и `xdg-desktop-portal-gnome`;
- systemd user manager;
- `gtk-launch`;
- GTK 4 и libadwaita для Control Center.

Проверка на Fedora:

```bash
python3 -c 'import gi; gi.require_version("Atspi", "2.0")'
rpm -q at-spi2-core xdg-desktop-portal xdg-desktop-portal-gnome python3-gobject
```

## Установка

Из корня проекта:

```bash
chmod +x scripts/*.sh
./scripts/install-user.sh
```

Installer:

1. создаёт безопасные config/data directories;
2. создаёт настройки в режиме `Off`;
3. один раз генерирует токен;
4. создаёт symlink-команды в `~/.local/bin`;
5. устанавливает systemd user unit и desktop entry;
6. запускает daemon.

Повторный запуск installer также выполняет fail-closed `access off`, поэтому update не
может незаметно сохранить ранее включённый `ALL DESKTOP`.

Открыть панель:

```bash
gnome-desktop-bridge-control
```

Проверить daemon:

```bash
systemctl --user status gnome-desktop-bridge
gnome-desktop-bridge-cli status
```

Удаление без удаления токена и настроек:

```bash
./scripts/uninstall-user.sh
```

Полное удаление локальных данных, screenshots и токена:

```bash
./scripts/uninstall-user.sh --purge
```

## Первый безопасный запуск

1. Откройте **GNOME Desktop Bridge**.
2. Выберите `Observe`.
3. Оставьте `Redact protected text` включённым.
4. Нажмите **Apply**.
5. Проверьте `list_apps` и один snapshot.
6. Для действий выберите `Control selected apps` и укажите allowlist, например
   `org.mozilla.firefox, Firefox`.
7. Используйте `ALL DESKTOP` только на время задачи.
8. Для глобальных кликов включите `Global pointer and keyboard`, сохраните настройки,
   нажмите **Start session** и подтвердите системный диалог GNOME.
9. После задачи нажмите **STOP ALL**.

## Локальные файлы

По умолчанию:

| Назначение | Путь | Права |
|---|---|---:|
| Настройки | `~/.config/gnome-desktop-bridge/settings.json` | `0600` |
| Bearer token | `~/.local/share/gnome-desktop-bridge/token` | `0600` |
| Portal restore token | `~/.local/share/gnome-desktop-bridge/portal-restore-token` | `0600` |
| Скриншоты | `~/.local/share/gnome-desktop-bridge/screenshots/` | dir `0700`, files `0600` |
| Runtime lock | `$XDG_RUNTIME_DIR/gnome-desktop-bridge/daemon.lock` | `0600` |

Учитываются `XDG_CONFIG_HOME`, `XDG_DATA_HOME` и `XDG_RUNTIME_DIR`.

### Постоянство токена

Токен генерируется только при отсутствии token file. Перезапуск daemon, GNOME или всего
компьютера его не меняет. Он изменится только после ручной rotation или удаления файла.

Показать токен:

```bash
gnome-desktop-bridge-cli token
```

Rotate и затем перезапустить daemon:

```bash
gnome-desktop-bridge-cli token --rotate
systemctl --user restart gnome-desktop-bridge
```

Не помещайте токен в Git, screenshots, issue reports или shell history. Для `curl` лучше
прочитать его из файла в переменную текущего shell:

```bash
TOKEN="$(<~/.local/share/gnome-desktop-bridge/token)"
```

### Формат settings.json

```json
{
  "version": 1,
  "accessMode": "control",
  "allowedApps": ["org.mozilla.*", "Firefox"],
  "allowScreenshots": true,
  "allowPortalInput": false,
  "allowLaunchApps": false,
  "persistPortalSession": false,
  "redactProtectedText": true,
  "maxEvents": 2000,
  "host": "127.0.0.1",
  "port": 18766
}
```

`host` принимает только loopback (`127.0.0.1`, `::1`, `localhost`). Изменение host/port
требует restart, остальные значения daemon подхватывает автоматически.

`allowedApps` — case-insensitive shell globs, сопоставляемые с AT-SPI application name,
application ID, toolkit name и PID. Рекомендуется desktop/application ID, а не PID.

## HTTP API

### Transport

- base URL: `http://127.0.0.1:18766`;
- JSON UTF-8;
- максимальное тело POST: 1 MiB;
- `Content-Type: application/json` обязателен;
- неизвестные top-level fields и action arguments отклоняются, а не игнорируются;
- все `/api/*` требуют `Authorization: Bearer <token>`;
- CORS намеренно отсутствует;
- responses содержат `Cache-Control: no-store`.

Успех:

```json
{"ok": true, "result": {}}
```

Ошибка:

```json
{
  "ok": false,
  "error": {
    "code": "access_denied",
    "message": "pointer_click is available only in ALL DESKTOP mode",
    "details": {"requiredMode": "all", "currentMode": "control"}
  }
}
```

Типичные error codes: `unauthorized`, `invalid_request`, `access_denied`, `not_found`,
`stale_reference`, `backend_unavailable`, `portal_cancelled`, `portal_denied`,
`device_not_granted`, `internal_error`.

### Endpoints

| Method | Path | Auth | Назначение |
|---|---|---:|---|
| `GET` | `/health` | нет | минимальный liveness check |
| `GET` | `/api/status` | да | режим, feature gates, portal state |
| `POST` | `/api/command` | да | выполнить одну команду |
| `GET` | `/api/events?after=0&limit=200` | да | получить audit events |
| `GET` | `/api/events/stream?after=0` | да | Server-Sent Events stream |

Форма команды:

```json
{
  "action": "snapshot",
  "args": {
    "ref": "g12:n3",
    "depth": 8,
    "maxNodes": 1000,
    "includeText": true
  }
}
```

Пример `curl`:

```bash
curl --fail-with-body \
  -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  --data '{"action":"get_state","args":{}}' \
  http://127.0.0.1:18766/api/command
```

## Полный каталог команд

### Всегда разрешённые после authentication

#### `ping`

Проверка request path и версии daemon.

```json
{"action":"ping","args":{}}
```

#### `capabilities`

Возвращает версии и доступность AT-SPI/portal, режимы и список actions. Это первая
команда, которую должен вызывать новый AI-клиент.

```json
{"action":"capabilities","args":{}}
```

#### `get_state`

Возвращает текущую policy и RemoteDesktop state, но никогда не возвращает токен.

```json
{"action":"get_state","args":{}}
```

#### `stop_all`

Fail-closed emergency operation:

- закрывает RemoteDesktop portal session;
- устанавливает `accessMode: off`;
- устанавливает `allowPortalInput: false`;
- добавляет `security.stop_all` в audit.

Result содержит `settingsPersisted`. Даже если filesystem не позволяет сохранить файл,
daemon немедленно удерживает fail-closed `Off` в памяти и сообщает `false` вместо того,
чтобы снова разрешить команды по старой policy.

```json
{"action":"stop_all","args":{}}
```

### Observe или выше

#### `list_apps`

Возвращает AT-SPI applications и refs:

```json
{"action":"list_apps","args":{}}
```

Сокращённый результат:

```json
{
  "generation": 4,
  "apps": [
    {
      "ref": "g4:n1",
      "name": "Firefox",
      "appId": "org.mozilla.firefox",
      "toolkitName": "Gecko",
      "pid": 12345,
      "role": "application",
      "childCount": 2
    }
  ]
}
```

#### `snapshot`

Читает дерево выбранного application/window/widget.

Arguments:

- `ref` — required ref из текущей generation;
- `depth` — `0..30`, default `8`;
- `maxNodes` — `1..5000`, default `1000`;
- `includeText` — default `true`.

```json
{
  "action":"snapshot",
  "args":{"ref":"g4:n1","depth":10,"maxNodes":1500,"includeText":true}
}
```

Каждый node может содержать:

- `ref`, `name`, `role`, `description`;
- `states`, `interfaces`, `childCount`;
- `bounds: {x,y,width,height}`;
- `actions: [{index,name,description,keyBinding}]`;
- `text`, `value`;
- `protected: true` и `text: "[REDACTED]"` для password role;
- nested `children`.

Для защиты daemon один text node ограничен 4096 characters, а общий text budget одного
snapshot — 256 000 characters; усечённый node получает `textTruncated: true`. Names,
descriptions, interfaces и action metadata также имеют консервативные limits.

При включённом `redactProtectedText` bridge вообще не запрашивает `name`, `description`
или `Text` contents у password-role widget: в ответ сразу помещается `[REDACTED]`.

#### `screenshot`

Arguments:

- `interactive` — попросить portal показать interactive selector, default `false`;
- `includeBase64` — вернуть PNG base64 вместе с локальным path, default `false`, limit 25 MiB.

```json
{"action":"screenshot","args":{"interactive":false,"includeBase64":false}}
```

Результат:

```json
{
  "path":"/home/user/.local/share/gnome-desktop-bridge/screenshots/screenshot-....png",
  "size":321456,
  "mimeType":"image/png"
}
```

#### `list_launchers`

Ищет visible desktop entries. Arguments: `query` (substring), `limit` (`1..1000`).

```json
{"action":"list_launchers","args":{"query":"terminal","limit":20}}
```

### Control selected apps или ALL

#### `invoke`

Вызывает нативный AT-SPI action. Предпочтительный способ нажать кнопку.

```json
{"action":"invoke","args":{"ref":"g5:n42","action":"click"}}
```

`args.action` может быть exact action name или index, default `0`.

#### `click`

Convenience command с двумя методами:

- `method: "action"` — AT-SPI Action, доступен в scoped Control;
- `method: "coordinates"` — center bounds + portal click, требует ALL DESKTOP,
  global-input gate и активную portal session.

```json
{
  "action":"click",
  "args":{"ref":"g5:n42","method":"action","action":0}
}
```

Coordinates fallback:

```json
{
  "action":"click",
  "args":{"ref":"g5:n42","method":"coordinates","button":"left"}
}
```

#### `focus`

```json
{"action":"focus","args":{"ref":"g5:n51"}}
```

Вызывает `Atspi.Component.grab_focus()`.

#### `fill`

```json
{"action":"fill","args":{"ref":"g5:n51","text":"hello@example.com"}}
```

Вызывает `Atspi.EditableText.set_text_contents()`. Максимум 100 000 символов.
Текст никогда не записывается в audit; сохраняются только length и redaction marker.

#### `launch_app`

Требует отдельный `allowLaunchApps`. В Control desktop ID/name также должен совпасть
с `allowedApps`.

```json
{"action":"launch_app","args":{"desktopId":"org.gnome.Terminal.desktop"}}
```

### Только ALL DESKTOP + global-input gate

#### `start_remote_desktop`

```json
{"action":"start_remote_desktop","args":{}}
```

Daemon последовательно создаёт RemoteDesktop session, запрашивает keyboard+pointer,
добавляет один monitor ScreenCast source и вызывает Start. Пользователь выбирает экран
и подтверждает доступ в системном диалоге GNOME. Без согласия команда завершается
`portal_cancelled` или `portal_denied`.

Если `persistPortalSession` включён и portal поддерживает version 2, restore token
сохраняется локально. Это не отменяет возможность GNOME или пользователя отозвать доступ.

#### `stop_remote_desktop`

Закрывает только portal session. Для полного отключения используйте `stop_all`.

```json
{"action":"stop_remote_desktop","args":{}}
```

#### `pointer_move`

Global logical screen coordinates:

```json
{"action":"pointer_move","args":{"x":1280,"y":720}}
```

Daemon выбирает shared stream по portal `position`/`size`, переводит global coordinates
в stream-local и вызывает `NotifyPointerMotionAbsolute`.

#### `pointer_click`

Optional `x` и `y` сначала перемещают указатель. Они должны быть переданы вместе.

```json
{"action":"pointer_click","args":{"button":"left","x":1280,"y":720}}
```

Buttons: `left`, `right`, `middle`, `side`, `extra` или numeric Linux evdev code.

#### `scroll`

Continuous scroll deltas:

```json
{"action":"scroll","args":{"dx":0,"dy":-120}}
```

#### `key`

`key` — GDK keysym name, один Unicode character или numeric keysym. `event` — `tap`,
`press`, `release`.

```json
{"action":"key","args":{"key":"Return","event":"tap"}}
```

Для shortcut modifier нужно press/release явно:

```json
{"action":"key","args":{"key":"Control_L","event":"press"}}
{"action":"key","args":{"key":"l","event":"tap"}}
{"action":"key","args":{"key":"Control_L","event":"release"}}
```

Клиент обязан отпускать modifiers даже после ошибки.

#### `type_text`

```json
{"action":"type_text","args":{"text":"Hello, GNOME!","intervalMs":10}}
```

Максимум 10 000 characters, interval `0..1000` ms. Plaintext исключён из audit.
Для обычных editable fields предпочтителен `fill`: он быстрее и не зависит от layout.

## Семантические refs и generation

Refs вроде `g12:n84` намеренно короткоживущие.

- `list_apps` начинает новую generation;
- `snapshot` начинает следующую generation и возвращает новые refs;
- следующий discovery call делает старые refs stale;
- stale action возвращает HTTP 409 `stale_reference`;
- refs не следует сохранять между задачами или restart daemon.

Это уменьшает риск нажать не тот элемент после изменения UI. Правильный цикл:

1. `list_apps`;
2. выбрать application ref;
3. `snapshot`;
4. выбрать element ref из этого snapshot;
5. немедленно выполнить одно действие;
6. получить новый snapshot перед следующим важным действием.

## Audit и realtime events

Events хранятся в памяти daemon. Максимум задаётся `maxEvents`; после restart история
обнуляется. Постоянный operational log находится в systemd journal.

Получить batch:

```bash
gnome-desktop-bridge-cli events --after 0 --limit 200
```

Следить в реальном времени:

```bash
gnome-desktop-bridge-cli events --follow
```

Raw SSE:

```bash
curl -N -H "Authorization: Bearer $TOKEN" \
  http://127.0.0.1:18766/api/events/stream?after=0
```

Основные event types:

- `daemon.started`, `daemon.stopping`;
- `command.started`, `command.completed`, `command.failed`;
- `portal.session.started`, `portal.session.stopped`, `portal.session.revoked`;
- `security.stop_all`, `security.startup_downgrade`.

Audit не содержит `fill.text`, `type_text.text` или screenshot base64. Он может содержать
названия приложений, refs, coordinates, key names и локальные screenshot paths.

## CLI

```bash
# State and capabilities
gnome-desktop-bridge-cli status
gnome-desktop-bridge-cli capabilities

# Generic action
gnome-desktop-bridge-cli call list_apps
gnome-desktop-bridge-cli call snapshot --args '{"ref":"g1:n1","depth":6}'

# Human-controlled modes
gnome-desktop-bridge-cli access observe
gnome-desktop-bridge-cli access control --app 'org.mozilla.*' --app 'Firefox'

# ALL requires interactive phrase unless an explicit acknowledgement is supplied
gnome-desktop-bridge-cli access all --portal-input
gnome-desktop-bridge-cli access all --portal-input --yes-i-understand

# Feature gates
gnome-desktop-bridge-cli feature screenshots on
gnome-desktop-bridge-cli feature launch-apps off

# Emergency stop
gnome-desktop-bridge-cli stop-all
```

`access` и `feature` изменяют human-owned settings локально. В HTTP API намеренно нет
`set_access`: AI, получивший токен, не может самостоятельно повысить свой уровень.

## Инструкция для AI-клиента

AI, который впервые видит этот проект, должен следовать этому protocol:

1. Не предполагать, что bridge запущен: проверить `/health`.
2. Прочитать токен из указанного пользователем файла, не печатать его в ответе/log.
3. Вызвать `capabilities`, затем `get_state`.
4. Если режим недостаточен, попросить человека изменить его в Control Center. Не менять
   `settings.json` самостоятельно.
5. Начинать с AT-SPI: `list_apps` → `snapshot` → semantic action.
6. Выбирать приложение по `appId`; name использовать как fallback.
7. Перед необратимым действием обновить snapshot и сверить name/role/state.
8. Предпочитать `invoke`/`fill` координатным событиям.
9. Не читать password fields; считать `[REDACTED]` окончательным значением.
10. Для global input убедиться в `accessMode == all`, feature gate и active portal session.
11. Не начинать portal session без прямой задачи пользователя: диалог требует внимания.
12. После задачи вызвать `stop_all`, если ALL DESKTOP больше не нужен.
13. Если был pressed modifier, гарантированно отправить release в `finally`-логике.
14. При `stale_reference` повторить discovery, а не угадывать новый ref.
15. При `portal_cancelled` не повторять dialog бесконечно.
16. Никогда не выполнять purchases, sends, submissions, deletes или другие необратимые
    действия без отдельного, актуального подтверждения пользователя.

Рекомендуемый machine workflow:

```text
health
  -> capabilities
  -> get_state
  -> list_apps
  -> select exact appId
  -> snapshot
  -> identify exact role/name/action
  -> (human confirmation if consequential)
  -> invoke/fill
  -> snapshot to verify outcome
  -> stop_all when elevated access is no longer needed
```

### Минимальный Python client

```python
import json
import pathlib
import urllib.request

token = pathlib.Path.home().joinpath(
    ".local/share/gnome-desktop-bridge/token"
).read_text().strip()

payload = json.dumps({"action": "get_state", "args": {}}).encode()
request = urllib.request.Request(
    "http://127.0.0.1:18766/api/command",
    data=payload,
    method="POST",
    headers={
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    },
)
with urllib.request.urlopen(request) as response:
    print(json.load(response)["result"])
```

## Security model

Подробности — в [SECURITY.md](SECURITY.md).

Основные свойства:

- default policy `Off`;
- random 256-bit token;
- token/config mode `0600`, directories `0700`;
- constant-time token comparison;
- loopback-only bind и Host validation;
- no CORS;
- request size limit;
- central allow/deny policy перед backend call;
- human-only elevation path;
- separate global-input gate;
- GNOME portal consent;
- fail-closed revocation watcher;
- password-role redaction;
- text redaction in audit;
- bounded event buffer;
- systemd hardening;
- no shell command endpoint;
- no API token disclosure.

### Важная граница доверия

Любой процесс, уже выполняющийся под тем же Linux user и способный прочитать token file,
обычно обладает широким доступом к пользовательским данным и может impersonate API client.
Токен защищает от случайных обращений, сайтов и других users, но не создаёт sandbox между
двумя недоверенными процессами одного UID.

Для недоверенного AI-runtime нужен дополнительный OS sandbox и broker с отдельным
per-request approval, а не только этот bearer token.

### Redaction не абсолютна

AT-SPI password role маскируется. Но приложение может ошибочно представить секрет как
обычный text widget; screenshot также способен содержать секреты. Поэтому Observe уже
является чувствительным разрешением.

## Troubleshooting

### Daemon не подключается

```bash
systemctl --user status gnome-desktop-bridge
journalctl --user -u gnome-desktop-bridge -n 200 --no-pager
curl http://127.0.0.1:18766/health
```

Проверьте, что команда запускается внутри активной GNOME user session, где доступны
`DBUS_SESSION_BUS_ADDRESS`, `XDG_RUNTIME_DIR`, accessibility bus и portals.

### `401 unauthorized`

- перечитайте token file;
- не добавляйте newline к header value;
- после rotation перезапустите daemon;
- убедитесь, что клиент и daemon работают от одного Linux user.

### `AT-SPI backend unavailable`

Проверьте пакеты и accessibility bus:

```bash
busctl --user status org.a11y.Bus
python3 -c 'import gi; gi.require_version("Atspi","2.0"); from gi.repository import Atspi; print(Atspi.get_desktop_count())'
```

Не запускайте daemon через `sudo`: root окажется в другой D-Bus/session environment.

### Snapshot пустой или неполный

- некоторые apps плохо реализуют accessibility;
- увеличьте `depth`/`maxNodes`;
- Electron/Chromium app может требовать включённую accessibility support;
- canvas/game/remote-video UI часто не имеет полезного semantic tree;
- используйте screenshot только после явного разрешения.

### `stale_reference`

Другой `list_apps` или `snapshot` уже начал новую generation. Повторите discovery и
не используйте старый ref.

### Portal dialog отменён или permission denied

Это ожидаемый user decision. Не зацикливайте запрос. Убедитесь, что:

- режим `ALL DESKTOP` сохранён;
- `Global pointer and keyboard` включён;
- daemon работает в GNOME session;
- `xdg-desktop-portal-gnome` запущен.

```bash
systemctl --user status xdg-desktop-portal xdg-desktop-portal-gnome
```

### Клик попадает не туда

- получите новый snapshot непосредственно перед click;
- проверьте bounds;
- убедитесь, что выбран правильный monitor в portal dialog;
- fractional scaling и приложение с неверными AT-SPI bounds могут давать расхождение;
- сначала используйте `invoke`, а coordinates только как fallback.

### Режим Control не разрешает приложение

Посмотрите identity из `list_apps`. Добавьте точный `appId` или безопасный glob в
Control Center. Не используйте `*`, если не хотите фактически получить почти ALL scope.

### Port занят

Измените `port` в settings, затем:

```bash
systemctl --user restart gnome-desktop-bridge
```

### Emergency stop без GUI

```bash
gnome-desktop-bridge-cli stop-all
```

Если daemon завис, выключите service; portal session закроется при исчезновении клиента:

```bash
systemctl --user stop gnome-desktop-bridge
```

## Разработка

Запуск из source tree без установки:

```bash
PYTHONPATH=. python3 -m gnome_desktop_bridge.server --verbose
PYTHONPATH=. python3 -m gnome_desktop_bridge.cli status
PYTHONPATH=. python3 -m gnome_desktop_bridge.control_center
```

Tests:

```bash
python3 -m unittest discover -v
python3 -m compileall -q gnome_desktop_bridge tests
desktop-file-validate data/io.github.local.GnomeDesktopBridge.desktop
systemd-analyze --user verify systemd/gnome-desktop-bridge.service
```

Tests не нажимают UI и не открывают portal dialog. Live smoke test выполняется отдельно
в активной GNOME session с человеком у экрана.

### Структура проекта

```text
gnome_desktop_bridge/
  app_backend.py       # Gio launchers and isolated launching
  atspi_backend.py     # semantic tree and safe actions
  cli.py               # local client and human settings commands
  commands.py          # dispatcher, policy enforcement, audit redaction
  config.py            # settings, XDG paths, persistent token
  control_center.py    # GTK/libadwaita UI
  errors.py            # stable API errors
  events.py            # bounded audit + wait support
  policy.py            # access-mode matrix and app allowlist
  portal_backend.py    # Screenshot/RemoteDesktop/ScreenCast D-Bus APIs
  server.py            # authenticated HTTP + SSE daemon
data/                  # desktop entry
scripts/               # launch/install/uninstall helpers
systemd/               # hardened user service
tests/                 # unit tests
```

## Известные ограничения и roadmap

MVP использует совместимые `NotifyPointer*`/`NotifyKeyboard*` methods RemoteDesktop
portal. Следующий backend должен использовать `ConnectToEIS` + libei; это официальный
современный transport и лучше подходит для сложных input sequences.

Другие возможные улучшения:

- PipeWire frame capture для consented realtime visual debugging;
- optional GNOME Shell indicator с постоянно видимым mode/session state;
- Unix domain socket и peer-credential authentication;
- per-client tokens/scopes и rotation без restart;
- persistent encrypted audit с retention policy;
- action confirmation broker для consequential operations;
- better multi-monitor/fractional-scale calibration;
- semantic search endpoint без передачи полного tree;
- rate limits и per-command deadlines;
- package/RPM/Flatpak-friendly distribution;
- libei Python binding или небольшой Rust helper;
- clipboard portal support как отдельный feature-gate;
- automated accessibility event subscriptions вместо polling snapshots.

## Лицензия

MIT — см. [LICENSE](LICENSE).
