# Deploying mail-cadence with systemd

A single Linux host runs two units under a dedicated, unprivileged user:

| Unit | What it does |
|---|---|
| `mail-cadence-dispatch.timer` → `mail-cadence-dispatch.service` | Runs a dispatch every 10 minutes (`Type=oneshot`). Window-level idempotency sends each task at most once per active window, so frequent runs are safe. |
| `mail-cadence-web.service` (optional) | Web panel on `127.0.0.1:8000`, behind nginx with HTTPS. |

The files are in [`deploy/`](../deploy): systemd units, an environment file template, an nginx site and a fail2ban filter.

## Layout

| Path | Owner / mode | Contents |
|---|---|---|
| `/opt/mail-cadence/venv` | `root`, read-only for the service | Python virtual environment with `mail-cadence` |
| `/etc/mail-cadence/mail-cadence.env` | `root:mail-cadence` `0640` | SMTP and panel credentials (read by systemd and the service) |
| `/etc/mail-cadence/templates/` | `root:mail-cadence` `0750` | Task templates (`--template-dir`) |
| `/var/lib/mail-cadence/` | `mail-cadence` `0700` (created by systemd, `StateDirectory=`) | SQLite database (`mail_cadence.db` plus WAL files) |

The service can write only to `/var/lib/mail-cadence` (`ProtectSystem=strict`); the code, configuration and templates are read-only to it.

## Install

```bash
# 1. System user (no login shell, no home directory)
sudo useradd --system --user-group --no-create-home --shell /usr/sbin/nologin mail-cadence

# 2. Package in its own virtual environment (Python 3.12+)
sudo python3 -m venv /opt/mail-cadence/venv
sudo /opt/mail-cadence/venv/bin/pip install mail-cadence==X.Y.Z   # pin the version you tested

# 3. Configuration and templates
sudo install -d -o root -g mail-cadence -m 0750 /etc/mail-cadence /etc/mail-cadence/templates
sudo install -o root -g mail-cadence -m 0640 deploy/systemd/mail-cadence.env.example /etc/mail-cadence/mail-cadence.env
sudoedit /etc/mail-cadence/mail-cadence.env        # SMTP and ADMIN_* values
sudo cp -r your-templates/* /etc/mail-cadence/templates/
sudo chgrp -R mail-cadence /etc/mail-cadence/templates && sudo chmod -R g+rX,o-rwx /etc/mail-cadence/templates

# 4. Units
sudo install -m 0644 deploy/systemd/mail-cadence-*.service deploy/systemd/mail-cadence-dispatch.timer /etc/systemd/system/
sudo systemctl daemon-reload
```

The SMTP settings in the environment file must use TLS or STARTTLS when a password is set (see `SMTP_ALLOW_INSECURE_AUTH` in the README); otherwise every run stops with exit code `1`.

## Load tasks and schedules

Import the XML configuration as the service user, so the database is created with the right owner:

```bash
sudo systemd-run --pty --wait --collect --uid=mail-cadence --gid=mail-cadence \
    --property=StateDirectory=mail-cadence --property=UMask=0077 \
    --property=EnvironmentFile=/etc/mail-cadence/mail-cadence.env \
    --property=Environment=MAIL_CADENCE_DB=/var/lib/mail-cadence/mail_cadence.db \
    /opt/mail-cadence/venv/bin/mail-cadence import-xml \
    --tasks-xml /etc/mail-cadence/tasks.xml --schedule-xml /etc/mail-cadence/schedules.xml
```

(Copy `tasks.xml` and `schedules.xml` to `/etc/mail-cadence/` first, mode `0640`, group `mail-cadence`.) Tasks can also be managed in the web panel.

## Test, then enable

```bash
# One dry run: renders every eligible task and logs what would be sent, sends nothing.
sudo systemd-run --pty --wait --collect --uid=mail-cadence --gid=mail-cadence \
    --property=StateDirectory=mail-cadence --property=UMask=0077 \
    --property=EnvironmentFile=/etc/mail-cadence/mail-cadence.env \
    --property=Environment=MAIL_CADENCE_DB=/var/lib/mail-cadence/mail_cadence.db \
    --property=Environment=MAIL_CADENCE_TEMPLATE_DIR=/etc/mail-cadence/templates \
    /opt/mail-cadence/venv/bin/mail-cadence --dry-run

# One real run through the unit itself, then look at its log
sudo systemctl start mail-cadence-dispatch.service
journalctl -u mail-cadence-dispatch.service -e

# Schedule it, and start the panel
sudo systemctl enable --now mail-cadence-dispatch.timer
sudo systemctl enable --now mail-cadence-web.service
systemctl list-timers mail-cadence-dispatch.timer
```

Exit codes of a run: `0` success, `1` configuration or database error, `2` one or more tasks failed or were delivered only partially, `3` the outcome of a delivery is unknown and needs review (never resent automatically), `4` the tasks succeeded but the status report was not sent. Every non-zero code marks the unit as failed, so they show up in `systemctl --failed`; add an `OnFailure=` unit if you want a notification.

## Web panel behind nginx

`deploy/nginx/mail-cadence.conf` terminates HTTPS and forwards to `127.0.0.1:8000`. Two headers are required:

- `proxy_set_header Host $host;` — the panel rejects form posts whose `Origin` does not match `Host` (`403 Cross-site request rejected.`).
- `proxy_set_header X-Forwarded-For $remote_addr;` — the real client IP for the login limit. It overwrites any client-supplied value. uvicorn trusts the header only from `127.0.0.1`/`::1`; if nginx runs on another machine, set `FORWARDED_ALLOW_IPS=<nginx IP>` in the environment file.

Keep the panel on `127.0.0.1` and reachable only through the proxy.

## fail2ban (optional)

The panel answers `429` after `ADMIN_LOGIN_MAX_FAILURES` wrong logins and logs `Too many failed admin logins from <ip>`. To also block such clients at the firewall:

```bash
sudo cp deploy/fail2ban/filter.d/mail-cadence.conf /etc/fail2ban/filter.d/
sudo cp deploy/fail2ban/jail.d/mail-cadence.local /etc/fail2ban/jail.d/
sudo systemctl reload fail2ban
sudo fail2ban-client status mail-cadence
```

## Sandboxing

Both units run with no capabilities and a restricted view of the system: `ProtectSystem=strict`, `ProtectHome`, `PrivateTmp`, `PrivateDevices`, `NoNewPrivileges`, kernel and cgroup protection, `MemoryDenyWriteExecute`, the `@system-service` system call set, IPv4/IPv6/Unix sockets only and `UMask=0077`. Check the result with:

```bash
systemd-analyze security mail-cadence-web.service mail-cadence-dispatch.service
```

If a later dependency needs something these settings block (for example writable and executable memory), the service fails at start with a clear error in `journalctl`; relax only that one setting in a drop-in (`systemctl edit mail-cadence-web.service`) rather than editing the shipped unit.

## Upgrades and backups

```bash
sudo systemctl stop mail-cadence-dispatch.timer mail-cadence-web.service
sudo -u mail-cadence sqlite3 /var/lib/mail-cadence/mail_cadence.db ".backup '/var/lib/mail-cadence/backup-$(date +%F).db'"
sudo /opt/mail-cadence/venv/bin/pip install mail-cadence==X.Y.Z
sudo systemctl start mail-cadence-web.service mail-cadence-dispatch.timer
```

Upgrading to the release with durable execution state (schema version 6, ADR 0001): stop the timer and the web panel first, so that no run of the old version sends while the new one claims tasks, take the backup above, then start the new version. Migration 6 turns the old `last_run`/`last_status` into executions: a task sent in the current window stays blocked for that window; a task that failed or was delivered partially is marked for review (exit code `3` until the window ends). Rolling back to the backup forgets everything sent after it; check the SMTP server's log before sending again.

The first start of a new version migrates the database in place (see "Database Upgrades" in the README); a database written by a newer version is refused. Use `sqlite3 .backup` (or stop the services first) rather than copying the file, so the WAL contents are included.
