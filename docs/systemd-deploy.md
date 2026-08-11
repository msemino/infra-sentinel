# Production deploy with systemd (alternative to the demo loop)

The `docker compose` setup loops the one-shot cycle for convenience. In production
the cleaner model is **one short-lived process per cycle**, driven by a systemd
timer — no long-lived daemon, and each run gets a clean slate. This is how the
original system this project is based on runs.

Sentinel is pure standard library, so no virtualenv or `pip` is required on the host.

## `sentinel.service`

```ini
[Unit]
Description=Sentinel - infrastructure watchdog (one monitoring cycle)
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
User=sentinel
WorkingDirectory=/opt/sentinel
EnvironmentFile=/opt/sentinel/.env
ExecStart=/usr/bin/python3 -m sentinel.cli
```

## `sentinel.timer`

```ini
[Unit]
Description=Sentinel - run a monitoring cycle every 15 minutes

[Timer]
OnBootSec=5min
OnUnitActiveSec=15min
RandomizedDelaySec=60
Persistent=true

[Install]
WantedBy=timers.target
```

> **On the interval.** With the event window (v2) a skipped round is covered by the next one,
> because the window stretches back to the last round that actually finished. `Type=oneshot`
> plus a timer will not start a run while the previous one is still going, which used to mean
> a slow round silently lost whatever happened during it. That is why this can now run at
> 5 minutes as easily as at 15.

## `sentinel-deadman.service` and `.timer`

The cycle cannot report its own death. This is the unit that can, and **it has to be a
separate unit** — sharing a process, a network path or a credential with the thing it watches
means the two die together and the check only ever says everything is fine.

```ini
[Unit]
Description=Sentinel - dead-man switch (is the cycle still running?)

[Service]
Type=oneshot
User=sentinel
WorkingDirectory=/opt/sentinel
EnvironmentFile=/opt/sentinel/.env
ExecStart=/usr/bin/python3 -m sentinel.deadman
```

```ini
[Unit]
Description=Sentinel - check the watchdog heartbeat every 10 minutes

[Timer]
OnBootSec=10min
OnUnitActiveSec=10min
Persistent=true

[Install]
WantedBy=timers.target
```

Set `HEARTBEAT_MAX_AGE_SEC` comfortably **above** the cycle interval — equal to it means a
single delayed round pages you, and a check that cries wolf gets muted, which is the same as
not having it.

## Install

```bash
sudo cp -r sentinel /opt/sentinel/sentinel
sudo cp .env.example /opt/sentinel/.env    # then edit, chmod 600
sudo cp docs/sentinel*.service docs/sentinel*.timer /etc/systemd/system/  # (extract the blocks above)
sudo systemctl daemon-reload
sudo systemctl enable --now sentinel.timer sentinel-deadman.timer
```

## Operate

```bash
systemctl list-timers sentinel.timer            # when is the next cycle
journalctl -u sentinel.service -n 30 --no-pager # recent cycles
sudo systemctl start sentinel.service           # force a cycle now
sudo systemctl disable --now sentinel.timer     # stop Sentinel
rm /opt/sentinel/state.json                     # re-seed the baseline next cycle
```

Keep secrets (`TELEGRAM_TOKEN`, `ZABBIX_PASS`) only in `/opt/sentinel/.env` with
`chmod 600` — never in the repo.
