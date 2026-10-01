from __future__ import annotations
'\nAnki add-on: Aobana Reibun (例文, "example sentences")\n\nExample sentences for Anki notes from three sources: a local Aobana (subtitles,\nbooks and manga on this computer), the Nadeshiko API and Immersion Kit. Adds:\n- Tools -> Aobana Reibun -> Run (run over a deck), first among the add-ons\' entries\n- Tools -> Aobana Reibun -> Settings (edit config)\n- Browser -> Edit -> Aobana Reibun, and right-click -> Aobana Reibun\n  (run over selected notes), each under AnkiAutoImage\'s "Auto Images" when it is installed\n- Three reviewer hotkeys, one per source.\n\nConfiguration is read from config.json next to this file. Every name above is\nthis add-on\'s own, so it can sit beside AnkiAutoImage, the add-on it started from.\n'
from aqt import mw
from aqt.qt import QAction, QKeySequence, QMenu, QShortcut, qconnect, Qt
from .anki_util import BROWSER_NAME, MENU_NAME
DEFAULT_HOTKEY_AOBANA = 'Ctrl+Shift+W'
DEFAULT_HOTKEY_NADESHIKO = 'Ctrl+Shift+O'
DEFAULT_HOTKEY_IMMERSIONKIT = 'Ctrl+Shift+K'

def _open_tools_dialog() -> None:
    from .tools import BackfillImagesDialog
    dialog = BackfillImagesDialog(mw=mw, mode='deck', browser=None)
    dialog.exec()

def _open_settings_dialog() -> None:
    from .tools import SettingsDialog
    dialog = SettingsDialog(parent=mw)
    dialog.exec()

def _open_browser_dialog(browser) -> None:
    from .tools import BackfillImagesDialog
    dialog = BackfillImagesDialog(mw=mw, mode='browser', browser=browser)
    dialog.exec()
ORIGINAL_BROWSER_NAME = 'Auto Images'

def _text(action) -> str:
    text = getattr(action, 'text', '')
    return str(text() if callable(text) else text)

def _is_separator(action) -> bool:
    if action == '---':
        return True
    try:
        return bool(action.isSeparator())
    except Exception:
        return False

def _insert_after_builtins(tools, action) -> None:
    preferences = getattr(mw.form, 'actionPreferences', None)
    actions = tools.actions()
    if preferences in actions:
        i = actions.index(preferences) + 1
        if i < len(actions) and _is_separator(actions[i]):
            i += 1
        if i < len(actions):
            tools.insertAction(actions[i], action)
            return
    tools.addAction(action)

def _place_beside_original(menu, action) -> None:
    actions = menu.actions()
    if action in actions:
        menu.removeAction(action)
        actions = menu.actions()
    for i, other in enumerate(actions):
        if _text(other) == ORIGINAL_BROWSER_NAME:
            if i + 1 < len(actions):
                menu.insertAction(actions[i + 1], action)
            else:
                menu.addAction(action)
            return
    menu.addAction(action)

def _add_beside_original(menu, action) -> None:
    _place_beside_original(menu, action)
    signal = getattr(menu, 'aboutToShow', None)
    if signal is not None:
        qconnect(signal, lambda: _place_beside_original(menu, action))

def _setup_tools_menu() -> None:
    tools = mw.form.menuTools
    menu = QMenu(MENU_NAME, mw)
    run_action = QAction('Run', mw)
    qconnect(run_action.triggered, _open_tools_dialog)
    menu.addAction(run_action)
    settings_action = QAction('Settings', mw)
    qconnect(settings_action.triggered, _open_settings_dialog)
    menu.addAction(settings_action)
    mw._aobana_reibun_menu = menu
    _insert_after_builtins(tools, menu.menuAction())

def _browser_action(browser, name: str):
    action = QAction(name, browser)
    qconnect(action.triggered, lambda: _open_browser_dialog(browser))
    return action

def _setup_browser_menu_with_gui_hooks() -> bool:
    try:
        from aqt import gui_hooks

        def on_browser_menus_init(browser):
            _add_beside_original(browser.form.menuEdit, _browser_action(browser, MENU_NAME))

        def on_browser_context_menu(browser, menu):
            _add_beside_original(menu, _browser_action(browser, BROWSER_NAME))
        gui_hooks.browser_menus_did_init.append(on_browser_menus_init)
        try:
            gui_hooks.browser_will_show_context_menu.append(on_browser_context_menu)
        except Exception:
            pass
        return True
    except Exception:
        return False

def _setup_browser_menu_with_legacy_hook() -> None:
    try:
        from anki.hooks import addHook

        def on_browser_setup_menus(browser):
            _add_beside_original(browser.form.menuEdit, _browser_action(browser, MENU_NAME))
        addHook('browser.setupMenus', on_browser_setup_menus)
    except Exception:
        pass

def _ensure_user_files_dir() -> None:
    import os
    base_dir = os.path.dirname(__file__)
    user_files_dir = os.path.join(base_dir, 'user_files')
    try:
        os.makedirs(user_files_dir, exist_ok=True)
    except Exception:
        pass

def init_addon() -> None:
    _ensure_user_files_dir()
    _setup_tools_menu()
    try:
        mw.addonManager.setConfigAction(__name__, _open_settings_dialog)
    except Exception:
        pass
    if not _setup_browser_menu_with_gui_hooks():
        _setup_browser_menu_with_legacy_hook()
    try:
        import json, os
        base_dir = os.path.dirname(__file__)
        cfg_path = os.path.join(base_dir, 'config.json')
        hotkey_subs = DEFAULT_HOTKEY_AOBANA
        hotkey_nade = DEFAULT_HOTKEY_NADESHIKO
        hotkey_ik = DEFAULT_HOTKEY_IMMERSIONKIT
        try:
            try:
                pkg = os.path.basename(os.path.dirname(__file__))
                cfg = mw.addonManager.getConfig(pkg) or {}
            except Exception:
                cfg = {}
            if not cfg:
                with open(cfg_path, 'r', encoding='utf-8') as f:
                    cfg = json.load(f)
            if isinstance(cfg, dict):
                if 'reviewer_hotkey_subs' in cfg:
                    hotkey_subs = str(cfg.get('reviewer_hotkey_subs') or '').strip()
                if 'reviewer_hotkey_nadeshiko' in cfg:
                    hotkey_nade = str(cfg.get('reviewer_hotkey_nadeshiko') or '').strip()
                if 'reviewer_hotkey_immersionkit' in cfg:
                    hotkey_ik = str(cfg.get('reviewer_hotkey_immersionkit') or '').strip()
        except Exception:
            pass
        from .tools import quick_add_immersionkit_for_current_card, quick_add_nadeshiko_for_current_card, quick_add_subs_for_current_card

        def _bind_hotkey(key: str, sequence: str, callback):
            shortcut = QShortcut(QKeySequence(sequence), mw)
            shortcut._aobana_reibun_config_key = key
            shortcut.setEnabled(bool(sequence))
            qconnect(shortcut.activated, callback)
            try:
                shortcut.setContext(Qt.ShortcutContext.WindowShortcut)
            except Exception:
                pass
            return shortcut
        shortcuts = [_bind_hotkey('reviewer_hotkey_nadeshiko', hotkey_nade, lambda: quick_add_nadeshiko_for_current_card(mw)), _bind_hotkey('reviewer_hotkey_subs', hotkey_subs, lambda: quick_add_subs_for_current_card(mw)), _bind_hotkey('reviewer_hotkey_immersionkit', hotkey_ik, lambda: quick_add_immersionkit_for_current_card(mw))]
        try:
            if not hasattr(mw, '_aobana_reibun_shortcuts'):
                mw._aobana_reibun_shortcuts = []
            mw._aobana_reibun_shortcuts.extend((shortcut for shortcut in shortcuts if shortcut is not None))
        except Exception:
            pass
    except Exception:
        pass
init_addon()
