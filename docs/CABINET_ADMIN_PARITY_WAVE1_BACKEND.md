# Wave 1 — cabinet admin backend (frontend build spec & gaps)

11 new backend route files, 100 endpoints. New RBAC permissions to register: backup:manage, backup:read, blacklist:manage, blacklist:read, contests:manage, contests:read, faq:manage, faq:read, legal:edit, legal:manage, legal:read, logs:read, maintenance:manage, maintenance:read, monitoring:manage, monitoring:read, polls:manage, polls:read, user_messages:manage, user_messages:read, welcome_text:edit, welcome_text:read

## Gaps still needing backend work (no reusable service)

- **maintenance**: Manual status notification (bot handler send_manual_notification / RemnaWaveService.send_manual_status_notification) cannot be wrapped: it requires a live aiogram Bot instance, which the cabinet request context does not have. Porting this needs a new bot-less notification service or a way to access the running bot from the FastAPI process.
- **maintenance**: Detailed panel health endpoint (RemnaWaveService.check_panel_health, used by the bot's 'check_panel_status') is exposed indirectly via get_panel_status_summary; if the frontend needs raw fields (api_error, total_users, attempts_used) a separate endpoint over check_panel_health could be added, but the summary covers the bot's main panel-status card.
- **maintenance**: Editing maintenance configuration toggles (MAINTENANCE_MONITORING_ENABLED, MAINTENANCE_AUTO_ENABLE, MAINTENANCE_CHECK_INTERVAL, MAINTENANCE_RETRY_ATTEMPTS, MAINTENANCE_MESSAGE) is not part of the bot maintenance handler and has no maintenance_service mutator — these are read-only via status here; persistent config editing belongs to the existing admin_settings flow.
- **backup**: None for the required capabilities. Note on settings persistence: the bot handler toggles settings via backup_service.update_backup_settings(**kwargs), which only mutates the in-memory dataclass and is lost on bot restart. To persist correctly, the cabinet endpoint deliberately uses SystemSettingsService.set_value (writes BACKUP_* keys to DB and triggers backup_service.reload_settings_from_db() + scheduler restart) — the same path the existing cabinet settings UI uses. This is a real, existing service function, not invented. The 'create progress / live status' streaming the bot shows via message edits has no reusable service hook; the endpoint is synchronous-blocking until create/restore completes (frontend should rely on request completion + a spinner, not a progress stream).
- **monitoring**: Reconciliation 'link old YooKassa receipts to transactions by amount/date' (bot callback admin_mon_receipts_link_old) was NOT ported: it has no service — the handler inlines a Transaction query + NaloGoService.get_incomes() matching + direct DB writes. Porting it would require new CRUD/service work, so it is omitted. The read-only reconciliation REPORT (payments-without-receipts) IS provided via GET /nalogo/reconcile (log parsing replicated minimally).
- **monitoring**: Per-notification 'send test/preview' (bot callbacks admin_mon_notify_preview_*) is bot-only: it sends a Telegram message to the admin's chat via bot.send_message and has no reusable service — intentionally not exposed in the cabinet.
- **monitoring**: Traffic-monitoring controls (admin_mon_traffic_settings / toggle fast+daily checks / run_fast_check_now) were excluded: they belong to traffic_monitoring_service and persist via BotConfigurationService, and an admin_traffic router already exists. Out of scope for the monitoring permission domain.
- **legal_documents**: Service rules have no per-document enable/disable flag in the data model (ServiceRule.is_active is internal version bookkeeping, not a user-facing toggle), so unlike offer/privacy there is no 'toggle visibility' for rules — the bot itself only supports edit/clear. Offered clear-to-default instead.
- **legal_documents**: No reusable preview/pagination service is wrapped: PublicOfferService.split_content_into_pages exists but is bot-rendering concern; the API returns raw content and lets the frontend render/preview.
- **legal_documents**: No per-language listing/overview service exists; endpoints operate one language at a time via the ?language param (default-language fallback handled by services).
- **polls**: No reusable service/CRUD exists to EDIT/UPDATE an existing poll (title, description, reward, or its questions/options). The bot has no edit flow either — only create + delete. If the cabinet needs poll editing, a new update_poll CRUD function would be required.
- **polls**: No CRUD to list/paginate individual poll RESPONSES is wired into the admin flow as a thin call — get_poll_responses_with_answers(db, poll_id, limit, offset) DOES exist in crud/poll.py but the bot never exposes per-respondent detail, so I left it out of scope; it could be added later as a paginated responses endpoint if the frontend wants a respondent drill-down.
- **polls**: send_poll_to_users dispatches synchronously within the request (the bot runs it inside a callback). For very large audiences a background-task/queue wrapper would be needed to avoid long HTTP requests, but no such reusable async-job service exists for polls.
- **contests**: Bot's 'массовка' bulk ghost-creation (random-name mass virtual participants) has no dedicated service function — the bot generates random names inline in the handler. Exposed only as single-add server-side; bulk creation must be done client-side (loop addVirtual) or a new CRUD helper is needed.
- **contests**: Daily template CREATE/DELETE: no cabinet-safe create/delete is wrapped. Templates are seeded by contest_rotation_service._ensure_default_templates / upsert_template(slug-based); there is no per-template delete CRUD and no admin create handler in the bot, so creation/deletion are intentionally omitted.
- **contests**: Daily reset-attempts (bot's admin_daily_reset_attempts_ / admin_daily_reset_all_attempts) was NOT ported: it relies on clear_attempts(db, round_id) which exists, but resetting attempts mid-round is an operationally risky action with no read-only counterpart; left out of the cabinet scope. Could be added later as POST /daily/{id}/reset-attempts wrapping get_active_round_by_template + clear_attempts.
- **contests**: Daily 'manual/test round' start (bot manual_start_round) is functionally the same as start-round minus auto-enable; not exposed separately to avoid a confusing duplicate.
- **contests**: debug_contest_transactions (bot debug view) was NOT exposed — it is a developer-only raw transaction dump; the detailed /stats endpoint covers the admin-facing numbers. Can be added as GET /referral/{id}/debug wrapping app.database.crud.referral_contest.debug_contest_transactions if needed.
- **contests**: Round winners listing (crud.contest.list_winners exists) is not surfaced; could back a future GET /daily/{id}/round/{round_id}/winners endpoint.
- **blacklist_massban**: Toggle blacklist check on/off from the web: the bot's toggle_blacklist handler only displays a message instructing the admin to edit BLACKLIST_CHECK_ENABLED in .env; blacklist_service has no setter, so no write endpoint was created (exposed read-only in /blacklist/status).
- **blacklist_massban**: Set/change the blacklist GitHub URL from the web: the bot's process_blacklist_url handler does not persist the URL (it tells the admin to edit BLACKLIST_GITHUB_URL in .env); no setter exists on blacklist_service, so no write endpoint was created.
- **blacklist_massban**: Mass-ban admin summary notification: bulk_ban_service.ban_users_by_telegram_ids can notify an admin chat, but the cabinet has no admin-chat context, so notify_admin is forced to False (per-user ban notifications still work via notify_users).

## Wiring report / frontend spec

I now have everything. Here is the consolidated wiring spec.

---

# Cabinet Admin Routes — Wiring Spec (10 new domains)

## 1. `__init__.py` edits

### A) Import block — insert alphabetically (matches existing sort order)

```python
from .admin_apps import router as admin_apps_router
from .admin_audit_log import router as admin_audit_log_router
from .admin_backup import router as admin_backup_router
from .admin_ban_system import router as admin_ban_system_router
from .admin_blacklist import router as admin_blacklist_router
from .admin_broadcasts import router as admin_broadcasts_router
from .admin_bulk_actions import router as admin_bulk_actions_router
from .admin_button_styles import router as admin_button_styles_router
from .admin_campaigns import router as admin_campaigns_router
from .admin_channels import router as admin_channels_router
from .admin_contests import router as admin_contests_router
from .admin_email_templates import router as admin_email_templates_router
from .admin_faq import router as admin_faq_router
from .admin_info_pages import router as admin_info_pages_router
from .admin_landings import router as admin_landings_router
from .admin_legal_documents import router as admin_legal_documents_router
from .admin_maintenance import router as admin_maintenance_router
from .admin_menu_layout import router as admin_menu_layout_router
from .admin_monitoring import router as admin_monitoring_router
# ... admin_news* unchanged ...
from .admin_partners import router as admin_partners_router
from .admin_payment_methods import router as admin_payment_methods_router
from .admin_payments import router as admin_payments_router
from .admin_pinned_messages import router as admin_pinned_messages_router
from .admin_policies import router as admin_policies_router
from .admin_polls import router as admin_polls_router
# ... existing ...
from .admin_stats import router as admin_stats_router
from .admin_system_logs import router as admin_system_logs_router
# ... existing ...
from .admin_users import router as admin_users_router
from .admin_user_messages import router as admin_user_messages_router
from .admin_welcome_text import router as admin_welcome_text_router
from .admin_wheel import router as admin_wheel_router
```

The exact 10 new import lines (drop wherever your isort lands them):

```python
from .admin_backup import router as admin_backup_router
from .admin_blacklist import router as admin_blacklist_router
from .admin_contests import router as admin_contests_router
from .admin_faq import router as admin_faq_router
from .admin_legal_documents import router as admin_legal_documents_router
from .admin_maintenance import router as admin_maintenance_router
from .admin_monitoring import router as admin_monitoring_router
from .admin_polls import router as admin_polls_router
from .admin_system_logs import router as admin_system_logs_router
from .admin_user_messages import router as admin_user_messages_router
from .admin_welcome_text import router as admin_welcome_text_router
```
(Note: `admin_welcome_text` exists once in imports but appears twice above by accident — it is one line. There are exactly **10 new files / 11 import lines**? No — 10 files, but `admin_user_messages` and `admin_welcome_text` are distinct: maintenance, backup, monitoring, system_logs, welcome_text, user_messages, legal_documents, polls, contests, faq, blacklist = **11 routers across 10 domains**. `admin_polls.py` is one file; recount: there are **11 route files** listed. All 11 import lines above are correct.)

### B) `include_router` block — append into the existing admin section (after line 160, before the WebSocket include on line 163)

```python
router.include_router(admin_maintenance_router)
router.include_router(admin_monitoring_router)
router.include_router(admin_backup_router)
router.include_router(admin_system_logs_router)
router.include_router(admin_blacklist_router)
router.include_router(admin_welcome_text_router)
router.include_router(admin_user_messages_router)
router.include_router(admin_legal_documents_router)
router.include_router(admin_faq_router)
router.include_router(admin_polls_router)
router.include_router(admin_contests_router)
```

### Ordering constraints (verified, not assumed)

- **No collisions.** The existing `polls_router` (`/cabinet/polls`) and `contests_router` (`/cabinet/contests`) are user-facing prefixes; the new admin routers are `/cabinet/admin/polls` and `/cabinet/admin/contests`. Different namespaces — safe.
- **Static-before-dynamic is already handled inside each file** (e.g. contests' `/referral/virtual/{participant_id}` vs `/referral/{contest_id}` — the int converter on `{contest_id}` won't match the literal `virtual`). No cross-router ordering needed among the 11; include order is cosmetic.
- WebSocket include must remain last (unchanged).
- Full prefixes after the `/cabinet` aggregator: `/cabinet/admin/{maintenance,monitoring,backup,system-logs,blacklist-massban,welcome-text,user-messages,legal-documents,faq,polls,contests}`.

---

## 2. NEW permission strings to register

Add these sections to `PERMISSION_REGISTRY` in `app/services/permission_service.py` (line 40). They are **not** yet in the registry — confirmed against the current dict. Legacy config admins bypass checks via `_is_legacy_admin`, and `PermissionService` uses fnmatch `*:*` wildcards, so granular roles work immediately once registered.

```python
    'maintenance': ['read', 'manage'],
    'monitoring': ['read', 'manage'],
    'backup': ['read', 'manage'],
    'logs': ['read'],
    'blacklist': ['read', 'manage'],
    'welcome_text': ['read', 'edit'],
    'user_messages': ['read', 'manage'],
    'legal': ['read', 'edit', 'manage'],
    'faq': ['read', 'manage'],
    'polls': ['read', 'manage'],
    'contests': ['read', 'manage'],
```

Flat list (24 strings): `maintenance:read`, `maintenance:manage`, `monitoring:read`, `monitoring:manage`, `backup:read`, `backup:manage`, `logs:read`, `blacklist:read`, `blacklist:manage`, `welcome_text:read`, `welcome_text:edit`, `legal:read`, `legal:edit`, `legal:manage`, `faq:read`, `faq:manage`, `user_messages:read`, `user_messages:manage`, `polls:read`, `polls:manage`, `contests:read`, `contests:manage`.

Inconsistency flags worth normalizing before merge:
- **`logs` section name** ≠ route domain `system_logs`. The file uses permission string `logs:read`. Pick one (suggest registry section `logs`, matches the string).
- **Verb drift:** most write-domains use `:manage`, but `welcome_text` and `legal` use `:edit` (and `legal` adds a third `:manage`). Confirm whether `legal:manage` is actually checked anywhere — frontend spec only references `legal:read`/`legal:edit`; the third tier may be dead. Either wire `legal:manage` to the toggle/clear actions or drop it.

---

## 3. FLAGGED — still needs backend work

**py_compile failures:** none. All 11 files report `py_compile_ok: true`. (Each notes that a full module import fails only on the shared `app.config` requiring `BOT_TOKEN` + a writable `/app` dir — environmental, affects every existing route module identically, not a defect.)

**Gaps requiring new service/CRUD work (cannot be wired as thin wrappers):**

- **maintenance** — (a) Manual status notification needs a live aiogram `Bot` the cabinet process doesn't have → needs a bot-less notification service. (b) Editing maintenance config toggles (`MAINTENANCE_*`) is read-only here; persistent editing belongs to `admin_settings`. (c) Raw panel-health fields beyond the summary would need a new endpoint over `check_panel_health`.
- **monitoring** — (a) "Link old YooKassa receipts to transactions" (bot `admin_mon_receipts_link_old`) has **no service** (handler inlines query + match + DB writes) → omitted; only the read-only reconcile report is exposed. (b) Per-notification test/preview send is bot-only (sends Telegram msg) → omitted. (c) Traffic-monitoring controls deliberately out of scope (already covered by `admin_traffic`).
- **backup** — none for required capabilities. Caveat: create/restore are **synchronous-blocking** (no progress-stream service hook) — frontend must use long timeouts + spinner, not a progress stream.
- **polls** — (a) **No update/edit CRUD** for an existing poll (bot has none either) → editing needs a new `update_poll`. (b) Per-respondent response listing not exposed (CRUD exists, never surfaced). (c) `send_poll_to_users` runs synchronously in-request → large audiences need a background-job wrapper that doesn't exist.
- **contests** — (a) Bulk "массовка" ghost creation has no service (random names inline in handler) → only single-add exposed; bulk must loop client-side or get a new CRUD helper. (b) Daily template **create/delete** intentionally omitted (seeded by rotation service, no per-template delete CRUD). (c) Daily reset-attempts NOT ported (risky, no read counterpart) — could wrap `clear_attempts` later. (d) Round-winners listing not surfaced (`crud.contest.list_winners` exists).
- **blacklist_massban** — (a) Toggle blacklist check on/off and (b) set/change GitHub URL: bot handlers only tell admin to edit `.env`; `blacklist_service` has **no setters** → exposed read-only, no write endpoint. (c) Mass-ban admin-summary notification forced off (no admin-chat context in cabinet).
- **legal_documents** — (a) Rules have no user-facing enable/disable flag in the model → no toggle for rules (clear-to-default offered instead). (b/c) No preview/pagination or multi-language overview service — single-language-at-a-time, raw content returned.
- **system_logs, welcome_text, user_messages, faq** — **no gaps**; clean thin wrappers over existing services/CRUD.

**Net new backend tickets (priority order):** poll edit CRUD; poll/contest send → background-job wrapper; bot-less notification service (unblocks maintenance manual-notify + monitoring previews); blacklist `.env` setters (or accept read-only); contest bulk-ghost + daily template create/delete + reset-attempts; monitoring receipt-linking service.

---

## 4. Per-domain frontend build spec (compact)

> Common to all: base `/cabinet`; every call sends `Authorization: Bearer <jwt>` + `X-Telegram-Init-Data`. Errors arrive as `{ detail: string }` — surface in toast. Money in kopeks unless noted. Datetimes ISO-8601 UTC.

**maintenance** — `/cabinet/admin/maintenance` · Page: status card + panel-health card + actions row. Methods: `getStatus()` GET `/status` (primary poll, re-fetch after every mutation; poll every `check_interval`s) → `{is_active, enabled_at, reason, monitoring_active, api_status, consecutive_failures, check_interval, bot_connected, ...}`; `getPanelStatus()` GET `/panel-status` (slow, lazy; 400 if RemnaWave unconfigured) → `{status, description, response_time, nodes_status, users_online, has_issues, recommendation}`; `enable({reason?≤200})` / `disable()` POST → `{success,is_active,message}` (confirm — blocks all non-admin users); `startMonitoring`/`stopMonitoring` POST (drive one toggle off `monitoring_active`); `checkApi()` POST `/check-api` → `{api_available, response_time, consecutive_failures, error}` (spinner, then re-fetch status). Perms: read=`maintenance:read`, actions=`maintenance:manage`.

**monitoring** — `/cabinet/admin/monitoring` · Page: 4 sections (loop / logs / nalogo / notification-settings). `getStatus()` GET `/status` (poll 15-30s) → `{is_running, last_update, interval_minutes, nalogo_enabled, stats_24h, recent_events[]}`; `start()` (409 if bot not attached), `stop()`, `forceCheck()` → `{expired,expiring,autopay_ready}`. Logs: `getLogs({page,per_page≤100,event_type?})` → `{logs[], total, event_types[]}`; `clearLogs()` DELETE (confirm). NaloGO section render only when `status.nalogo_enabled` (else 400): `getQueue`, `forceProcessQueue`, `getPending`, `verifyPending({payment_id,receipt_uuid?})`, `retryPending({payment_id})` (`success:false` at HTTP 200 = not-found/unavailable, not error), `clearPending()` (confirm), `reconcile()` (read-only report, `log_found` empty-state). Notification settings: `getNotificationSettings()` / `updateNotificationSettings(partial)` (`globally_enabled` read-only banner; ranges: discount 0-100, hours 1-168, trigger_days 2-60). Perms: read=`monitoring:read`, writes=`monitoring:manage`.

**backup** — `/cabinet/admin/backup` · Page: backup table (newest first) + Settings panel + header Create/Upload actions. `listBackups(page,per_page≤200)` → `{items: BackupListItem[], total, total_pages}` (`corrupted:true` → grey out, disable restore/download, allow delete); `getSettings`/`updateSettings(partial)` (`backup_interval_hours` 1-720, `backup_time` `^([01]\d|2[0-3]):[0-5]\d$`, `max_backups_keep` 1-1000; `backup_location` read-only); `createBackup({compress?,include_logs?})` 201 (long-running, spinner); `downloadBackup(filename)` → blob/anchor download; `deleteBackup(filename)` (confirm, 404 if missing); `restoreBackup(filename,{clear_existing})` (long + destructive: merge=false / **wipe-and-restore**=true with extra-scary confirm); `uploadAndRestore(file, ?clear_existing query)` multipart `file`, accept `.json/.json.gz/.tar.gz/.tar` ≤50MB (415/413/400). Perms: read=`backup:read`, mutations=`backup:manage`.

**system_logs** — `/cabinet/admin/system-logs` · Page: metadata header card + line-count selector + Refresh + monospace scrollable viewer + truncation banner + download. `getMeta()` GET `/meta` → `{path, exists, size_bytes, last_modified}`; `tail(lines 1-5000, default 200)` GET `/tail` → `{meta, lines[] (oldest→newest), returned_lines, requested_lines, truncated}` (truncated when file >1MB → "download full file"); `download()` GET `/download` → FileResponse `text/plain` (fetch with auth headers → blob → anchor, filename from Content-Disposition, fallback `bot.log`; 404 if missing). Empty-state when `exists=false`. Perm: all three = `logs:read`.

**welcome_text** — `/cabinet/admin/welcome-text` · Page: textarea + char counter (10-4000) + placeholder legend (click-to-insert) + enabled toggle + preview panel + reset. `get()` → `{id, text, is_enabled, is_default, placeholders[{key,description}]}`; `getPlaceholders()` (optional); `update({text})` PUT → refreshed obj (422 with RU `detail` on HTML/length errors — show inline); `toggle()` POST → `{is_enabled, message}`; `reset()` POST (confirm); `preview({first_name?,username?})` POST → `{is_enabled, preview (HTML), first_name, username}` (preview = SAVED text; empty when disabled). Allowed tags: b/strong/i/em/u/ins/s/strike/del/code/pre/a (href http/https/tg). Perms: read=`welcome_text:read`, write=`welcome_text:edit`.

**user_messages** — `/cabinet/admin/user-messages` · Page: stats cards (total/active/inactive) + paginated table + create/edit modal. `list({offset,limit,include_inactive=true})` → `{items: UserMessageItem[], total}`; `stats()`; `get(id)`; `create({message_text,is_active=true,sort_order=0})` 201; `update(id,{message_text?,sort_order?})` (≥1 field else 422; is_active NOT here); `toggle(id)` POST → `{id,is_active,message}`; `delete(id)` (confirm). `UserMessageItem={id,message_text (raw→edit textarea),safe_html (→dangerouslySetInnerHTML preview, never render raw),is_active,sort_order,created_by,created_at,updated_at}`. maxlength 4000; tag allow-list incl. blockquote/tg-spoiler/tg-emoji/span. Perms: read=`user_messages:read`, write=`user_messages:manage`.

**legal_documents** — `/cabinet/admin/legal-documents` · Page: 3 tabs (Offer / Privacy / Rules), optional `?language=` selector (ru,en,ua,zh,fa; backend normalizes). Offer/Privacy: `get*`, `update*({content})` PUT (1-4000 chars, 400 `Invalid HTML:`/`Content too long`), `toggle*()` → `{is_enabled, message}`; response `{document, language, content, has_content, is_enabled, is_visible_to_users, updated_at}` — warn when `is_enabled && !has_content`. Rules: `getRules`/`updateRules({content})`/`clearRules()`; response `{content (always populated: custom OR default), is_custom, updated_at}` — **no visibility toggle**, offer reset-to-default (confirm). Perms: read=`legal:read`, edit=`legal:edit` (see §2 note on unused `legal:manage`).

**faq** — `/cabinet/admin/faq` · Page: "FAQ (Bot)" (distinct from cabinet info_pages), `language` query **required everywhere** (UI lang selector; reload overview on switch). `getOverview(lang)` → `{language, is_enabled, has_setting, total_pages, active_pages, pages: FaqPage[]}`; `getPage(id,lang)`; `setEnabled(lang,{enabled})` / `toggleGlobal(lang)` → `{language,is_enabled}`; `createPage(lang,{title,content,is_active})` 201; `updatePage(id,lang,{title?,content?,is_active?})`; `togglePage(id,lang)` → `{page}`; `deletePage(id,lang)` 204 (auto re-sequences → refetch); `movePage(id,lang,{direction:'up'|'down'})` → full overview (disable up on first/down on last; 400 at edge); `getHtmlHelp()` (no lang) → `{help_html}`. Validation: title ≤255 non-empty, content ≤6000 non-empty + HTML subset; 400 RU `detail`. Global toggle and per-page toggle are independent. Perms: read=`faq:read`, write=`faq:manage`.

**polls** — `/cabinet/admin/polls` · Page: catalog + create wizard + detail + statistics + send dialog. Money in **kopeks**. `listPolls()` → `{polls: PollListItem[], total}`; `getAudienceOptions()` → `{standard[], custom[]}` (`{value,label}`; send `value` verbatim as `target`); `createPoll({title 1-255, description? (HTML, 400 on bad tags), reward_enabled, reward_amount_kopeks, questions[{text 1-1000, options[≥2 non-empty]}]})` 201 (order = array index+1); `getPoll(id)` → full structure; `getPollStatistics(id)` → `{total_responses(invited), completed_responses, reward_sum_kopeks, questions[{...,options[{count}]}]}`; `previewAudience(id,target)` → `{label, user_count}`; `sendPoll(id,{target})` (confirm w/ label+count; long-running spinner; auto-skips no-chat/already-responded) → `{sent,failed,skipped,total}`; `deletePoll(id)` (destructive confirm — cascades). Perms: read=`polls:read`, write=`polls:manage`. **No edit endpoint** (see gaps).

**contests** — `/cabinet/admin/contests` · Page: 2 tabs (Referral / Daily); whole section 404s when `CONTESTS_ENABLED` off → hide menu. Money in kopeks+rubles (display rubles). **Referral:** `listReferralContests(page,page_size,contest_type?)`; `createReferralContest({title,description?,prize_text?,contest_type('referral_paid'|'referral_registered'),start_at,end_at>start,daily_summary_time?,daily_summary_times? CSV,timezone})` 201; `getReferralContest(id)` (incl. leaderboard + virtual_participants + `can_delete`); `updateReferralContest(id,{daily_summary_time?,daily_summary_times? '' to clear})` (only summary scheduling editable); `toggle`; `delete` (only when `can_delete`, else 409, confirm); `getLeaderboard(id,limit)`; `getStats(id)`; `syncContest(id)` (heavy/destructive-ish, confirm+spinner → recompute summary). Virtual ghosts: `listVirtual`/`addVirtual({display_name,referral_count≥1,total_amount_kopeks})` 201 (bulk = loop client-side)/`updateVirtual(pid,{referral_count})`/`deleteVirtual(pid)`. **Daily:** `listDaily(enabled_only?)`; `getDaily(id)` (+ `payload` JSON, `active_round?`); `updateDaily(id,partial)` (`prize_type days|balance|custom`, payload JSON editor; edit-only, no create/delete); `toggle`; `startRound` (409 if active, auto-enables); `closeRound` (404 if none); `closeAllRounds`/`startAllRounds` (confirm). Disable Start when `has_active_round`, Close when not. Perms: read=`contests:read`, write=`contests:manage`. 404=feature off/missing, 409=lifecycle guard.

**Note:** `admin_contests` is the only domain shipping a separate schema file: `/Users/c0mrade/PycharmProjects/remnawave-bedolaga-telegram-bot/app/cabinet/schemas/admin_contests.py` (all others have inline Pydantic v2 schemas). No shared/existing files were edited by any of the 11 route files — `__init__.py` and `permission_service.py` are the only two files the lead must touch.