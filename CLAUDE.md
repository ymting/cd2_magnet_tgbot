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
python -m unittest tests.test_network_error_handling tests.test_polling_watchdog \
  tests.test_token_redaction tests.test_reject_message_format \
  tests.test_submit_error_attribution tests.test_batch_links
```
> `unittest discover -s tests` fails with "Start directory is not importable" because `tests/` has no
> `__init__.py` — list the modules explicitly.
> When working in the repo's own virtualenv on Windows, run them as
> `.venv/Scripts/python.exe -m unittest ...`.

Integration behavior still requires `python main.py` against a live CloudDrive2 instance (or watching `docker-compose` logs).

## Architecture

### Single-File Design
All business logic resides in `main.py` (~910 lines; version string in `__version__`). The code is organized into 4 commented sections (referenced by function name since line numbers drift):
1. **Variable Configuration**: env vars → module constants (note the renames, see Environment Variables below)
2. **Core Cleanup Logic**: `get_blacklist()`, `get_all_items_recursive()`, `is_directory_empty()`, `clean_task_folder()`, `run_auto_clean()`
3. **Telegram Handlers**: `error_handler()`, `_get_polling_task()`, `watchdog_check()`, `_mask_link()`, `_extract_links()`, `_shorten()`, `_grpc_error_raw()`, `_is_transport_failure()`, `_classify_reject_reason()`, `_friendly_reject_reason()`, `_describe_submit_failure()`, `_safe_send()`, `SubmitOutcome`, `_submit_offline_link()`, `_build_batch_report()`, `_reply_single_link()`, `_submit_batch_links()`, `handle_link()`, `cmd_clean()`, `cmd_blacklist()`, `post_init()`
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

### Failure Attribution (v1.1.8+)
**Never wrap a CD2/gRPC call and a Telegram reply in the same `except` block.** They are
independent links: gRPC talks to CloudDrive2, the reply talks to Telegram through the proxy.
An `httpx.ConnectError` can *only* come from the Telegram side — a gRPC failure surfaces as
`StatusCode.UNAVAILABLE`. Merging them produced the misleading
`❌ 提交失败，CD2 连接异常: httpx.ConnectError:` while the download had in fact been submitted,
and left no trace in `docker logs` (httpx only logs a request once a response arrives, so a
failed connection is completely silent).

Convention:
- Submit first, reply second. Only the submit step may report `CD2 连接异常` (include
  `type(e).__name__`, and log with `logger.exception`).
- Send replies through `_safe_send()`, which retries transient network errors, skips retrying
  non-network errors (e.g. Markdown parse failures) and always logs the final failure.
- If the reply cannot be delivered, log it — never tell the user the business action failed.
- Long links go into logs via `_mask_link()` (magnet `dn=` payloads are huge).

### User-Facing Rejection Messages (v1.1.9+, extended in v1.1.10)
Technical CD2 errors reach the user through **two independent paths** — both must be
classified, and handling only one is a real bug that shipped in v1.1.9:
- **(a) gRPC raises an exception.** Cloud-drive rejections are wrapped into gRPC status
  codes. Measured 2026-10-09: 115open answers a duplicate link with
  `StatusCode.INTERNAL` + `code: 10008` + `任务已存在，请勿输入重复的链接地址` — *not*
  `success=False`. Handle via `_describe_submit_failure()`.
- **(b) `FileOperationResult(success=False, errorMessage=...)`.** Developer-facing text that
  often carries raw cloud-drive API content (English, error codes, JSON). Handle via
  `_friendly_reject_reason()`.

Both share `_classify_reject_reason()`, which returns `DUPLICATE_REPLY` for duplicate hints
(`已存在` / `已在` / `重复` / `already exist` / `duplicate` …), a `CD2_TOKEN` hint for auth
failures, a "not supported" reply, or `None` when unclassified.

Rules:
- **`INTERNAL` is not a connection problem.** Only `UNAVAILABLE` / `DEADLINE_EXCEEDED`
  (`_TRANSPORT_STATUS_CODES`, with `_TRANSPORT_TEXT_HINTS` as fallback for non-gRPC
  exceptions) justify telling the user `CD2 连接异常`. Everything else says `提交失败`.
- **Never expose `AioRpcError`.** Its `str()` is a 4-line repr whose `debug_error_string`
  duplicates `details`. Always go through `_grpc_error_raw()` (prefers `details()`) and
  `_shorten()` (single-line + truncate to `_REJECT_TEXT_LIMIT`).
- The raw error must always stay in the log (`logger.exception` / `logger.warning`) —
  simplification applies to the Telegram reply only.
- Adding a category means adding keywords to the hint tuples and a test in
  `tests/test_reject_message_format.py` (plus an end-to-end case in
  `tests/test_submit_error_attribution.py` when the new path is submit-related).

### Batch Links (v1.1.10-5+)
A single message may carry **several links of mixed schemes** (magnet / ed2k / http(s)).

- `_extract_links(text)` regex-scans the whole message body instead of checking
  `text.startswith(...)`. The old prefix check silently dropped numbered lists
  (`1. magnet:... 2. ed2k://...`) — a real bug: nothing was submitted at all.
  It also deduplicates, lowercases the scheme only (never the rest of the URL, or magnet
  `dn=` payloads would change), and strips trailing punctuation.
- The regex stops at **CJK punctuation but not at CJK ideographs** — ed2k filenames are
  commonly unencoded Chinese (`ed2k://|file|某部电影.avi|...`), so breaking on Han characters
  would truncate the link. ASCII `)`/`]`/`}` are intentionally kept so URLs such as
  `https://zh.wikipedia.org/wiki/Foo_(bar)` survive intact.
- **Newline-separated lists need no special handling** — `\s` already covers LF, CRLF, tabs and
  the ideographic space, so "one link per line" (the main way people paste) just works.
- Tail cleaning is two layers, in this order: `_LINK_TRAILING_CHARS` (punctuation) then
  `_WRAPPER_PAIRS` (paired wrappers `` ` `` `[ ]` `( )` `{ }`), looped until stable because they
  stack (`[link]。`). A wrapper is only stripped when its opener is **absent** from the link body —
  that single rule is what keeps `Foo_(bar)` and `[::1]` intact while cleaning `[magnet:...]`.
  See `LinkPasteFormatTests` before touching it.
- Zero-width characters (`_INVISIBLE_CHARS`) are deleted as noise, never treated as separators —
  treating them as separators would split a link in half.
- **Each link is submitted in its own `AddOfflineFiles` call** (one shared channel/stub for
  the whole batch), so success/failure is attributable per link and one rejection cannot
  abort the rest. Do not "optimize" this back into one joined `urls` string.
- Results go through `_build_batch_report()` as **plain text, no `parse_mode`** — link text
  contains `_`, `*`, `[` which break legacy Markdown, and a parse failure would cost the
  whole report. The report is also truncated below `_BATCH_REPORT_LIMIT` (3500) because
  Telegram caps messages at 4096 and an over-long send fails with a non-retryable BadRequest.
- `_safe_send()` returns the send result (normalized to a truthy value) instead of a bare
  `True`, so the batch path can `edit_text()` the progress message into the final report.

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

5. **Error Handling**: Network errors are only counted and logged (windowed by `NETWORK_ERROR_RESET_SECONDS`); they never stop the application anymore (v1.1.5+). Liveness is guarded by the polling watchdog instead. Non-network errors are logged on their own.

6. **Cleanup Logic**: Files are deleted per-file, not per-folder. SIZE_THRESHOLD applies to each file individually. Blacklist only applies to files >= SIZE_THRESHOLD.

## Branch & Versioning Convention (dev → master)

Development happens on the **`dev`** branch; production releases happen on **`master`**. The two
environments are isolated purely by **image tags**, so a busy dev branch never forces a production
version bump.

| Branch | `__version__` in `main.py` | Image tags built automatically | GitHub Release |
| --- | --- | --- | --- |
| `dev` | `1.1.10-1`, `1.1.10-2`, … (prod version + 1, with `-n` suffix) | `dev-latest`, `dev-1.1.10-1` | never |
| `master` + `v*` tag | `1.1.10` (drop the `-n`) | `1.1.10`, `latest` | yes |

Hard rules:
- **`latest` is written only by a `v*` tag build. dev pushes must never touch it** — the tag
  rules in `docker-publish.yml` gate each entry with `enable=` for exactly this reason.
- The CI reads `__version__` straight out of `main.py` (`sed` on the
  `__version__ = "x.y.z"` line) and feeds it into the dev image tag. **That line must keep its
  exact format** — the workflow fails fast with `::error::` if it cannot be parsed.
- `workflow_dispatch` builds `manual-<version>` so a manual run is reproducible and still cannot
  overwrite `latest`.
- Concurrency cancels superseded builds per ref, except for `v*` tag builds, which are never
  cancelled.

Promoting to production:
1. `git switch master && git pull && git merge --no-ff dev`
2. Rewrite `main.py` `__version__` to the plain release version (`1.1.10-2` → `1.1.10`) and sync
   `README.md` (version line + changelog).
3. `git commit && git push origin master`, then `git tag v1.1.10 && git push origin v1.1.10`.
4. Back on `dev`, bump the base to the next pre-release line (`1.1.11-1`) so the two branches
   cannot collide.

## Release Process

1. Update version in `main.py` (`__version__`) and `README.md` — these are kept in sync by hand.
   On `dev` the value carries a `-n` suffix; the release itself uses the plain version.
2. Create and push a version tag: `git tag v1.x.x && git push origin v1.x.x`
3. `.github/workflows/docker-publish.yml` triggers on `v*` tags, on pushes to `dev`, and on manual
   `workflow_dispatch`, pushing to `ghcr.io/<owner>/cd2_magnet_tgbot` (i.e. `ghcr.io/ymting/...`).
   Tag pushes produce the **semver from the git tag** plus `latest`; dev pushes produce
   `dev-latest` plus `dev-<__version__>`.

The published **production** image version comes from the **git tag**, not from `__version__` —
the git tag is the source of truth there. The dev image version comes from `__version__`, which is
why the two must stay consistent.
