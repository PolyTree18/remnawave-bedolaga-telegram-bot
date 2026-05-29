"""Smoke/regression test for the Wave-1 cabinet↔bot admin parity routers.

Importing app.cabinet.routes assembles every cabinet sub-router, so this test
fails loudly if any of the new admin_* route modules has a bad import or
references a service/CRUD function that does not exist. It also asserts each new
admin domain is actually mounted under /cabinet/admin/.
"""

from __future__ import annotations

from app.cabinet.routes import router


def _all_paths() -> set[str]:
    return {getattr(r, 'path', '') for r in router.routes}


def test_wave1_admin_domains_are_mounted():
    paths = _all_paths()
    joined = '\n'.join(sorted(paths))

    # Substrings that must appear in at least one mounted route path. Kept loose
    # (substring, not exact) so it survives small prefix choices per domain.
    required = [
        '/admin/maintenance',
        '/admin/backup',
        '/admin/monitoring',
        '/admin/system-logs',
        '/admin/welcome-text',
        '/admin/user-messages',
        '/admin/legal-documents',
        '/admin/polls',
        '/admin/contests',
        '/admin/faq',
        '/admin/blacklist-massban',
    ]
    missing = [p for p in required if not any(p in path for path in paths)]
    assert not missing, f'Wave-1 admin routes not mounted: {missing}\nMounted:\n{joined}'


def test_router_has_routes():
    assert len(router.routes) > 0
