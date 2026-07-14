# Security policy

GNOME Desktop Bridge способен читать интерфейс и, при включённом ALL DESKTOP,
генерировать input events. Рассматривайте bearer token как секрет уровня активной
пользовательской сессии.

## Supported version

Пока проект находится в стадии MVP, security fixes применяются только к текущей ветке.

## Trust boundary

Bridge защищает от:

- сетевых клиентов вне loopback;
- случайных localhost requests без токена;
- web origins благодаря отсутствию CORS и обязательному Authorization header;
- повышения permission mode через HTTP API;
- глобального input без GNOME portal consent;
- продолжения portal input после revoke настроек.

Bridge не защищает от malicious process того же Linux UID, который уже может прочитать
token file, инспектировать user session или изменить настройки. Для такого процесса
нужна отдельная OS sandbox/broker architecture.

## Safe operation

- Держите mode `Off`, когда bridge не нужен.
- Используйте `Control` с точными application IDs вместо `ALL`.
- Не отключайте protected-text redaction.
- Не публикуйте token, settings, screenshots или journal без проверки.
- После ALL task вызывайте `stop_all`.
- Не запускайте daemon как root.
- Не привязывайте API к LAN interface; код намеренно запрещает это.
- Не добавляйте CORS wildcard.

`ALL DESKTOP` автоматически сбрасывается в `Off` при каждом запуске daemon, поэтому
не полагайтесь на restart как на способ сохранить повышенный доступ.

## Secret handling

Token и portal restore token создаются с mode `0600`, parent directories — `0700`.
`fill.text`, `type_text.text` и screenshot base64 исключены из in-memory audit. Однако
screenshots, visible text, application names и coordinates всё ещё могут быть sensitive.

Token rotation:

```bash
gnome-desktop-bridge-cli token --rotate
systemctl --user restart gnome-desktop-bridge
```

## Emergency response

```bash
gnome-desktop-bridge-cli stop-all
systemctl --user stop gnome-desktop-bridge
```

Если есть подозрение на компрометацию:

1. остановите service;
2. rotate token;
3. удалите portal restore token;
4. проверьте screenshots и journal;
5. отзовите screen/remote-desktop permission в GNOME Settings, если она отображается;
6. перезапустите service только после устранения причины.

## Reporting a vulnerability

Не прикладывайте действующий token, screenshots или личные UI snapshots к публичному
report. Укажите version/commit, GNOME version, portal backend version, воспроизводимые
шаги и ожидаемую границу доступа. До появления публичного repository report следует
передавать владельцу проекта приватно.
