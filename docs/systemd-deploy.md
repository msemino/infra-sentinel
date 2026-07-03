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

## Install

```bash
sudo cp -r sentinel /opt/sentinel/sentinel
sudo cp .env.example /opt/sentinel/.env    # then edit, chmod 600
sudo cp docs/sentinel.service docs/sentinel.timer /etc/systemd/system/  # (extract the blocks above)
sudo systemctl daemon-reload
sudo systemctl enable --now sentinel.timer
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
