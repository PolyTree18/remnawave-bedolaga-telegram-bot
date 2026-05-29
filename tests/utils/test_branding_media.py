from app.config import settings
from app.utils import message_patch


def test_context_logo_uses_specific_existing_file(monkeypatch, tmp_path):
    default_logo = tmp_path / 'default.png'
    main_menu_logo = tmp_path / 'main-menu.jpg'
    default_logo.write_bytes(b'default')
    main_menu_logo.write_bytes(b'main-menu')

    monkeypatch.setattr(settings, 'LOGO_FILE', str(default_logo))
    monkeypatch.setattr(settings, 'MAIN_MENU_LOGO_FILE', str(main_menu_logo))

    assert message_patch.get_logo_path(message_patch.LOGO_CONTEXT_MAIN_MENU) == main_menu_logo


def test_context_logo_falls_back_to_default_file(monkeypatch, tmp_path):
    default_logo = tmp_path / 'default.png'
    missing_support_logo = tmp_path / 'missing-support.jpg'
    default_logo.write_bytes(b'default')

    monkeypatch.setattr(settings, 'LOGO_FILE', str(default_logo))
    monkeypatch.setattr(settings, 'SUPPORT_LOGO_FILE', str(missing_support_logo))

    assert message_patch.get_logo_path(message_patch.LOGO_CONTEXT_SUPPORT) == default_logo
