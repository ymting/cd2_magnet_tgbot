# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

CloudDrive2 Telegram Bot (cd2_magnet_tgbot) - A Telegram bot that manages offline downloads for CloudDrive2. It accepts magnet/http/ed2k links, submits them to CD2 for offline download, and provides automated cleanup functionality.

**Tech Stack**: Python 3.13 + python-telegram-bot (v22+) + gRPC + Docker

## Commands

### Development
```bash
# Run locally (requires environment variables set)
python main.py

# Build Docker image
docker build -t cd2-bot .

# Run with docker-compose
docker-compose up -d
```

### Dependencies
```bash
# Install dependencies
pip install -r requirements.txt

# Regenerate gRPC files (if clouddrive.proto changes)
python -m grpc_tools.protoc -I. --python_out=. --grpc_python_out=. clouddrive.proto
```
> `requirements.txt` is **unpinned**, but the proxy and `JobQueue` patterns require **python-telegram-bot v22+**. If a fresh install resolves an older major version, the bot will break (proxy API and JobQueue behavior differ).

### Tests
Unit tests live in `tests/` and need no network access:

```bash
python -m unittest tests.test_polling_watchdog tests.test_network_error_handling
```

Integration behavior still requires `python main.py` against a live CloudDrive2 instance (or watching `docker-compose` logs).

## Architecture

### Single-File Design
All business logic resides in `main.py` (~335 lines; version string in `__version__`). The code is organized into 4 commented sections (referenced by function name since line numbers drift):
1. **Variable Configuration**: env vars → module constants (note the renames, see Environment Variables below)
2. **Core Cleanup Logic**: `get_blacklist()`, `get_all_items_recursive()`, `is_directory_empty()`, `clean_task_folder()`, `run_auto_clean()`
3. **Telegram Handlers**: `error_handler()`, `handle_link()`, `cmd_clean()`, `cmd_blacklist()`, `post_init()`
4. **Entry Point** (`__main__`): proxy/`HTTPXRequest` setup, `ApplicationBuilder` wiring, `run_polling()`

### Key Components

| File | Purpose |
|------|---------|
| `main.py` | Main application with all handlers |
| `clouddrive_pb2.py` | gRPC protocol buffer generated code |
| `clouddrive_pb2_grpc.py` | gRPC service stub |
| `blacklist.txt` | Persistent blacklist keywords for cleanup |

### gRPC Communication Pattern
```python
async with grpc.aio.insecure_channel(CD2_IP_PORT) as channel:
    stub = clouddrive_pb2_grpc.CloudDriveFileSrvStub(channel)
    metadata = [('authorization', f'Bearer {CD2_TOKEN}')]
    # All gRPC calls require metadata and timeout (15-30s) to prevent hanging
```

### Telegram Bot v22+ Proxy Configuration
Both `request` and `get_updates_request` must be configured with proxy:
```python
from telegram.request import HTTPXRequest
q_request = HTTPXRequest(proxy=PROXY_URL, connection_pool_size=8, ...)
u_request = HTTPXRequest(proxy=PROXY_URL, ...)  # Required for getUpdates

builder = ApplicationBuilder().token(TG_BOT_TOKEN).request(q_request).get_updates_request(u_request)
```

### Scheduled Tasks
Use the built-in `JobQueue` instead of standalone `AsyncIOScheduler` to avoid event loop conflicts:
```python
application.job_queue.scheduler.add_job(
    run_auto_clean,
    CronTrigger.from_crontab(CLEAN_CRON)
)
```

### File Cleanup Logic (v1.1.4+)
Per-file deletion logic with recursive scanning:
```python
# Recursive scan of all files and directories
all_files, all_dirs = await get_all_items_recursive(stub, metadata, folder_path)

# Per-file deletion decision
for f in all_files:
    if f.size < threshold_bytes:
        # Size < threshold → delete
        files_to_delete.append(f.fullPathName)
    elif any(k.lower() in f.name.lower() for k in blacklist):
        # Size >= threshold but matches blacklist → delete
        files_to_delete.append(f.fullPathName)

# Clean empty directories (deepest first)
all_dirs.sort(key=lambda x: x.fullPathName.count('/'), reverse=True)
```

## Environment Variables

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| CD2_ADDRESS | Yes | 127.0.0.1:19798 | CloudDrive2 gRPC address |
| CD2_TOKEN | Yes | - | CD2 API authorization token |
| TG_TOKEN | Yes | - | Telegram Bot API token |
| ADMIN_IDS | Yes | - | Allowed user IDs (comma-separated) |
| SAVE_PATH | No | /115/离线下载 | Download save path |
| SIZE_THRESHOLD | No | 300 | Files smaller than this (MB) are deleted |
| PROXY_URL | No | - | Proxy for Telegram (http/socks5) |
| CLEAN_CRON | No | 30 3 * * * | Cleanup cron expression |
| NETWORK_ERROR_RESET_SECONDS | No | 300 | Silence window before network-error counting restarts (log only) |
| WATCHDOG_INTERVAL_SECONDS | No | 60 | Polling watchdog check interval in seconds (0 disables it) |

**Gotcha — env var name ≠ internal constant name.** When grepping `main.py`, the Python constant differs from the Docker env var:
- `CD2_ADDRESS` → `CD2_IP_PORT`
- `TG_TOKEN` → `TG_BOT_TOKEN`
- `SIZE_THRESHOLD` → `SIZE_THRESHOLD_MB`
- `NETWORK_ERROR_RESET_SECONDS` / `WATCHDOG_INTERVAL_SECONDS` keep the same name as the env var

`SIZE_THRESHOLD` is interpreted in **MB** and converted to bytes (`SIZE_THRESHOLD_MB * 1024 * 1024`) inside `clean_task_folder()`.

## Critical Implementation Notes

1. **Proxy Configuration**: Both `request` and `get_updates_request` must have proxy configured, otherwise the bot won't receive messages (已读不回 issue)

2. **Scheduled Tasks**: Never use standalone `AsyncIOScheduler` - it causes event loop conflicts with gRPC/Telegram. Use the built-in `JobQueue` instead (the scheduler is registered in `post_init()`). Note: `AsyncIOScheduler` is still imported at the top of `main.py` but is intentionally **unused** — do not wire it up. The polling watchdog (`watchdog_check`) is also registered there via `job_queue.run_repeating`.

2b. **Polling Watchdog**: PTB maps HTTP 401/404 to `InvalidToken` and its `network_retry_loop` re-raises it without calling `on_err_cb`, so the polling task dies silently while the process keeps running. `watchdog_check` polls `updater._Updater__polling_task` (a private attribute, guarded with `getattr`) and calls `application.stop_running()` when it finds the task already done. Only treat it as a failure when `application.running` is still true, otherwise normal shutdowns would be misreported. This path exits with code 1, which is fine for `always` / `unless-stopped` / `on-failure` restart policies.

3. **gRPC Timeout**: All gRPC calls must have timeout (15-30s) to prevent hanging when CD2 mount points are stuck.

4. **Permission Control**: All handlers must check `update.effective_user.id in ADMIN_IDS` at the beginning.

5. **Error Handling**: The `error_handler` tracks network retry count and stops the application when `MAX_RETRIES` is exceeded. Non-network errors reset the counter.

6. **Cleanup Logic**: Files are deleted per-file, not per-folder. SIZE_THRESHOLD applies to each file individually. Blacklist only applies to files >= SIZE_THRESHOLD.

## Release Process

1. Update version in `main.py` (`__version__`) and `README.md` — these are **informational only** and kept in sync by hand.
2. Create and push a version tag: `git tag v1.x.x && git push origin v1.x.x`
3. `.github/workflows/docker-publish.yml` triggers on `v*` tags (or manual `workflow_dispatch`) and pushes to `ghcr.io/<owner>/cd2_magnet_tgbot` (i.e. `ghcr.io/ymting/...`), tagging both the **semver from the git tag** and `latest`.

The published image version comes from the **git tag**, not from `__version__` — the git tag is the source of truth.
