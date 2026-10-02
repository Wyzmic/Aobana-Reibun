from __future__ import annotations
import os
import json
import random
import threading
import time
import types
from contextlib import contextmanager
from typing import Any, Dict, List, Optional
import re
from aqt.qt import QDialog, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit, QComboBox, QPushButton, QCheckBox, QSpinBox
from aqt.qt import QDialogButtonBox, QFormLayout, QFrame, QScrollArea, QWidget
from aqt.qt import QButtonGroup, QGridLayout, QTabBar, QGroupBox, QRadioButton, QSizePolicy, QStackedWidget
from aqt.qt import QFileDialog, QKeySequence, QKeySequenceEdit, QTabWidget, Qt
from aqt.qt import qconnect
from aqt import mw
from aqt.utils import openLink, showInfo, showWarning
from .logger import get_logger
from .anki_util import MENU_NAME, get_selected_note_ids, get_deck_note_ids, add_image_to_note, ensure_media_filename_safe, get_field_value, add_audio_to_note
from .nadeshiko_api import NadeshikoApiClient, NadeshikoApiError, NadeshikoCancelled
from . import immersionkit_api as ik
from .immersionkit_api import ImmersionKitClient, ImmersionKitError
from .aobana_api import AobanaClient, AobanaError, render_sentence, context_wrap, field_plain, held_sentences, sentence_key, sentence_window, split_field_lines, strip_context, plain_text, strip_speaker_tags, tag_sentence, untag_sentence
_NADE_SELECTION_CHOICES = [('Random from pool', 'random'), ('Longest', 'longest'), ('Shortest', 'smallest'), ('Median', 'median'), ('Best match (no variety)', 'none'), ('Random across corpus', 'corpus_random')]
_SUBS_SELECTION_CHOICES = [('Random from pool', 'random'), ('Longest', 'longest'), ('Shortest', 'smallest'), ('Median', 'median'), ('Best match (no variety)', 'none'), ('Random across all matches', 'corpus_random')]
_FALLBACK_SWITCH_OF = {key: prefix + '_fallback_enabled' for prefix in ('nadeshiko', 'subs', 'immersionkit') for key in (prefix + '_fallback_pool_size', prefix + '_fallback_min_length', prefix + '_fallback_max_length', prefix + '_fallback_selection') if key != 'immersionkit_fallback_pool_size'}
_IK_SELECTION_CHOICES = _NADE_SELECTION_CHOICES[:5]
_IK_FALLBACK_TIP = 'When no sentence fits the main limits, pick again from the same search with the\nfallback limits below. It costs no extra request: Immersion Kit returns the whole\npool at once, so this only re-filters what already arrived. On by default.'
_SUBS_LENGTH_TIP = "Counted as Aobana counts a sentence's length: the readings of furigana and\npunctuation are not counted, only the text itself."
_PROVIDER_NADESHIKO = 'Nadeshiko'
_PROVIDER_SUBS = 'Aobana'
_PROVIDER_IMMERSIONKIT = 'Immersion Kit'
_PROVIDERS = [_PROVIDER_SUBS, _PROVIDER_NADESHIKO, _PROVIDER_IMMERSIONKIT]
_PROVIDER_BLOCK = {_PROVIDER_NADESHIKO: 'nadeshiko', _PROVIDER_SUBS: 'subs', _PROVIDER_IMMERSIONKIT: 'immersionkit'}
_NO_FIELD = '(none)'
_SUBS_MAX_TAKE = 2000
_SUBS_CONTEXT_CAP = 10

def _addon_package_name() -> str:
    try:
        return os.path.basename(os.path.dirname(__file__))
    except Exception:
        return ''

def _read_config() -> Dict[str, Any]:
    try:
        pkg = _addon_package_name()
        if pkg:
            cfg = mw.addonManager.getConfig(pkg)
            if isinstance(cfg, dict) and cfg:
                return _normalize_config(cfg)
    except Exception:
        pass
    try:
        base_dir = os.path.dirname(__file__)
        config_path = os.path.join(base_dir, 'config.json')
        with open(config_path, 'r', encoding='utf-8') as f:
            data = json.load(f)
            return _normalize_config(data if isinstance(data, dict) else {})
    except Exception:
        return {}

def _normalize_config(cfg: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(cfg)
    out.pop('ddg_locale', None)
    if str(out.get('nadeshiko_sentence_selection', '') or '').strip().lower() == 'no sorting':
        out['nadeshiko_sentence_selection'] = 'random'
    for key in ('nadeshiko_api_key',):
        value = str(out.get(key, '') or '').strip()
        if value.upper().startswith('REPLACE_'):
            out[key] = ''
    return out

def _read_default_config() -> Dict[str, Any]:
    try:
        base_dir = os.path.dirname(__file__)
        config_path = os.path.join(base_dir, 'config.json')
        with open(config_path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}

def _write_config(data: Dict[str, Any]) -> bool:
    try:
        pkg = _addon_package_name()
        if pkg:
            mw.addonManager.writeConfig(pkg, data)
            return True
    except Exception:
        pass
    try:
        base_dir = os.path.dirname(__file__)
        config_path = os.path.join(base_dir, 'config.json')
        with open(config_path, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
            f.write('\n')
        return True
    except Exception:
        return False

def _user_files_dir() -> str:
    base_dir = os.path.dirname(__file__)
    user_files_dir = os.path.join(base_dir, 'user_files')
    os.makedirs(user_files_dir, exist_ok=True)
    return user_files_dir

def _last_settings_path() -> str:
    return os.path.join(_user_files_dir(), 'last_settings.json')

def _read_last_settings() -> Dict[str, Any]:
    try:
        with open(_last_settings_path(), 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return {}

def _write_last_settings(data: Dict[str, Any]) -> None:
    try:
        with open(_last_settings_path(), 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
    except Exception:
        pass

def _chain_choices(provider: str) -> List[tuple]:
    a, b = [name for name in _PROVIDERS if name != provider]
    return [('Nothing (leave the note)', ''), (a, a), (b, b), ('%s, then %s' % (a, b), '%s,%s' % (a, b)), ('%s, then %s' % (b, a), '%s,%s' % (b, a))]

def _chain_saved(provider: str, last: Optional[Dict[str, Any]]=None, cfg: Optional[Dict[str, Any]]=None) -> tuple:
    if last is None:
        last = _read_last_settings() or {}
    key = _PROVIDER_BLOCK.get(provider, '')
    block = last.get(key)
    block = block if isinstance(block, dict) else {}
    others = [name for name in _PROVIDERS if name != provider]
    saved = block.get('chain_order')
    ticked = block.get('chain_on')
    if not isinstance(saved, list) and (not isinstance(ticked, list)):
        if cfg is None:
            cfg = _read_config()
        default = [name.strip() for name in str(cfg.get(key + '_if_none_found', '') or '').split(',')]
        saved = ticked = [name for name in default if name in others]
    order = [name for name in saved if name in others] if isinstance(saved, list) else []
    order = list(dict.fromkeys(order + others))
    on = [name for name in order if isinstance(ticked, list) and name in ticked]
    return (order, on)

def _settings_to_last(changed: Dict[str, Any]) -> None:
    if not changed:
        return
    data = _read_last_settings() or {}
    touched = False
    for provider, block_name in _PROVIDER_BLOCK.items():
        toggles, spins = BackfillImagesDialog._options_of(block_name)
        block = data.get(block_name) if isinstance(data.get(block_name), dict) else {}
        prefix = block_name + '_'
        for name in toggles + spins:
            if prefix + name in changed:
                value = changed[prefix + name]
                block[name] = bool(value) if name in toggles else int(value)
                touched = True
        if prefix + 'if_none_found' in changed:
            order, on = _chain_saved(provider, {}, {prefix + 'if_none_found': changed[prefix + 'if_none_found']})
            block['chain_order'], block['chain_on'] = (order, on)
            touched = True
        if block:
            data[block_name] = block
    if touched:
        _write_last_settings(data)
_NADE_NO_KEY = 'Nadeshiko needs an API key: paste it in Tools → Aobana Reibun → Settings → Nadeshiko.'
_NADE_BOLD_TAIL_TIP = "With Bold: a verb's or adjective's ending is bold with it, 食べました, not only 食べ.\nThe particles after it never are."
_CHAIN_TIP = "The other sources to search, in order, for the notes this one found nothing for,\neach writing to the fields on its own tab of the Run dialog. The Run dialog's\nIf none found row starts from this, and the hotkey follows it, until a run there\nsaves its own choice."
_EVERY_TITLE = '(everything)'

def _pick_title(parent, titles: List[str], current: str) -> Optional[str]:
    from aqt.qt import QListWidget
    dialog = QDialog(parent)
    dialog.setWindowTitle('Limit to title')
    dialog.setMinimumWidth(420)
    dialog.setMinimumHeight(480)
    layout = QVBoxLayout(dialog)
    if current and current not in titles:
        warning = QLabel('“%s” is not a title in Aobana, so a search limited to it finds nothing. Choose another one.' % current, dialog)
        warning.setWordWrap(True)
        layout.addWidget(warning)
    search = QLineEdit(dialog)
    search.setPlaceholderText('Type to filter')
    layout.addWidget(search)
    listing = QListWidget(dialog)
    listing.addItems([_EVERY_TITLE] + list(titles))
    layout.addWidget(listing)
    start = titles.index(current) + 1 if current in titles else 0
    listing.setCurrentRow(start)

    def filter_titles(text: str) -> None:
        needle = text.strip().casefold()
        for row in range(listing.count()):
            item = listing.item(row)
            item.setHidden(row > 0 and bool(needle) and (needle not in item.text().casefold()))
    qconnect(search.textChanged, filter_titles)
    buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
    qconnect(buttons.accepted, dialog.accept)
    qconnect(buttons.rejected, dialog.reject)
    qconnect(listing.itemDoubleClicked, lambda _item: dialog.accept())
    layout.addWidget(buttons)
    search.setFocus()
    if not dialog.exec():
        return None
    item = listing.currentItem()
    if item is None or item.isHidden():
        return None
    return '' if listing.row(item) == 0 else item.text()

class SettingsDialog(QDialog):
    _TAB_ORDER = [('General', [('Run dialog', ['default_replace']), ('Reviewer hotkeys', ['reviewer_hotkey_subs', 'reviewer_hotkey_nadeshiko', 'reviewer_hotkey_immersionkit'])]), ('Aobana', [('Server', ['subs_base_url', 'subs_project_dir', 'subs_python', 'subs_autostart', 'subs_terminal']), ('Search', ['subs_cat_subs', 'subs_cat_epub', 'subs_cat_manga', 'subs_folder', 'subs_min_length', 'subs_max_length', 'subs_pool_size', 'subs_sentence_selection']), ('Fallback', ['subs_fallback_enabled', 'subs_fallback_pool_size', 'subs_fallback_min_length', 'subs_fallback_max_length', 'subs_fallback_selection', 'subs_if_none_found']), ('Fields && output', ['subs_furigana', 'subs_bold', 'subs_strip_names', 'subs_multi_count', 'subs_no_repeats', 'subs_context', 'subs_context_before', 'subs_context_after', 'subs_sentence_field', 'subs_source_field', 'subs_image_field'])]), ('Nadeshiko', [('Account', ['nadeshiko_api_key', 'nadeshiko_base_url']), ('Search', ['nadeshiko_sentence_lang', 'nadeshiko_cat_anime', 'nadeshiko_cat_live', 'nadeshiko_cat_yt', 'nadeshiko_require_media', 'nadeshiko_min_length', 'nadeshiko_max_length', 'nadeshiko_pool_size', 'nadeshiko_sentence_selection']), ('Fallback', ['nadeshiko_fallback_enabled', 'nadeshiko_fallback_pool_size', 'nadeshiko_fallback_min_length', 'nadeshiko_fallback_max_length', 'nadeshiko_fallback_selection', 'nadeshiko_if_none_found']), ('Fields && output', ['nadeshiko_bold', 'nadeshiko_bold_tail', 'nadeshiko_furigana', 'nadeshiko_sentence_en_lang', 'nadeshiko_sentence_field', 'nadeshiko_image_field', 'nadeshiko_audio_field', 'nadeshiko_sentence_en_field'])]), ('Immersion Kit', [('Search', ['immersionkit_cat_anime', 'immersionkit_cat_drama', 'immersionkit_cat_games', 'immersionkit_require_image', 'immersionkit_min_length', 'immersionkit_max_length', 'immersionkit_per_title', 'immersionkit_sentence_selection']), ('Fallback', ['immersionkit_fallback_enabled', 'immersionkit_fallback_min_length', 'immersionkit_fallback_max_length', 'immersionkit_fallback_selection', 'immersionkit_if_none_found']), ('Fields && output', ['immersionkit_furigana', 'immersionkit_bold', 'immersionkit_strip_names', 'immersionkit_context', 'immersionkit_context_before', 'immersionkit_context_after', 'immersionkit_context_images', 'immersionkit_context_audio', 'immersionkit_sentence_field', 'immersionkit_image_field', 'immersionkit_audio_field', 'immersionkit_translation_field', 'immersionkit_source_field'])])]
    _LABELS = {'default_replace': 'If filled: Replace by default (all sources)', 'nadeshiko_api_key': 'Nadeshiko API key', 'nadeshiko_base_url': 'Nadeshiko base URL', 'nadeshiko_sentence_lang': 'Sentence language', 'nadeshiko_min_length': 'Minimum sentence length', 'nadeshiko_max_length': 'Maximum sentence length', 'nadeshiko_pool_size': 'Candidate pool size', 'nadeshiko_sentence_selection': 'Sentence selection', 'nadeshiko_fallback_enabled': 'Search again with the fallback settings', 'nadeshiko_fallback_pool_size': 'Fallback pool size', 'nadeshiko_fallback_min_length': 'Fallback minimum length', 'nadeshiko_fallback_max_length': 'Fallback maximum length', 'nadeshiko_fallback_selection': 'Fallback selection', 'nadeshiko_sentence_field': 'Default sentence field', 'nadeshiko_image_field': 'Default image field', 'nadeshiko_audio_field': 'Default sentence audio field', 'nadeshiko_sentence_en_field': 'Default translation field', 'nadeshiko_sentence_en_lang': 'Translation language', 'nadeshiko_cat_anime': 'Category: Anime', 'nadeshiko_cat_live': 'Category: Live Action', 'nadeshiko_cat_yt': 'Category: YouTube', 'nadeshiko_require_media': 'Require Image & Audio', 'nadeshiko_bold': 'Bold the target word', 'nadeshiko_bold_tail': "Bold the word's ending too", 'nadeshiko_furigana': 'Furigana', 'subs_base_url': 'Aobana URL', 'subs_project_dir': 'Aobana folder', 'subs_python': 'Python command', 'subs_autostart': 'Start Aobana automatically', 'subs_terminal': 'Aobana terminal window', 'subs_min_length': 'Minimum sentence length', 'subs_max_length': 'Maximum sentence length', 'subs_pool_size': 'Candidate pool size', 'subs_sentence_selection': 'Sentence selection', 'subs_fallback_pool_size': 'Fallback pool size', 'subs_fallback_min_length': 'Fallback minimum length', 'subs_fallback_max_length': 'Fallback maximum length', 'subs_fallback_selection': 'Fallback selection', 'subs_cat_subs': 'Subtitles corpus', 'subs_cat_epub': 'Books corpus', 'subs_cat_manga': 'Manga corpus', 'subs_fallback_enabled': 'Search again with the fallback settings', 'subs_image_field': 'Default image field (Manga page)', 'subs_furigana': 'Furigana', 'subs_strip_names': 'No （names）: strip speaker tags from subtitle lines', 'subs_multi_count': 'Sentences per note (1-100)', 'subs_bold': 'Bold the target word', 'subs_folder': 'Limit to title', 'subs_sentence_field': 'Default sentence field', 'subs_source_field': 'Default source field', 'subs_no_repeats': 'No repeats within a run', 'subs_context': 'Include context', 'subs_context_before': 'Context lines before', 'subs_context_after': 'Context lines after', 'reviewer_hotkey_subs': 'Reviewer hotkey: Aobana', 'reviewer_hotkey_nadeshiko': 'Reviewer hotkey: Nadeshiko', 'reviewer_hotkey_immersionkit': 'Reviewer hotkey: Immersion Kit', 'immersionkit_cat_anime': 'Category: Anime', 'immersionkit_cat_drama': 'Category: Drama', 'immersionkit_cat_games': 'Category: Games', 'immersionkit_require_image': 'Require an image', 'immersionkit_min_length': 'Minimum sentence length', 'immersionkit_max_length': 'Maximum sentence length', 'immersionkit_per_title': 'Examples per title', 'immersionkit_sentence_selection': 'Sentence selection', 'immersionkit_fallback_enabled': 'Pick again with the fallback settings', 'immersionkit_fallback_min_length': 'Fallback minimum length', 'immersionkit_fallback_max_length': 'Fallback maximum length', 'immersionkit_fallback_selection': 'Fallback selection', 'immersionkit_sentence_field': 'Default sentence field', 'immersionkit_image_field': 'Default image field', 'immersionkit_audio_field': 'Default sentence audio field', 'immersionkit_translation_field': 'Default translation field', 'immersionkit_source_field': 'Default source field', 'immersionkit_furigana': 'Furigana', 'immersionkit_bold': 'Bold the target word', 'immersionkit_strip_names': 'No (names): strip speaker tags', 'immersionkit_context': 'Include context', 'immersionkit_context_before': 'Context lines before', 'immersionkit_context_after': 'Context lines after', 'immersionkit_context_images': "Context lines' images", 'immersionkit_context_audio': "Context lines' audio", 'subs_if_none_found': 'If none found, then try', 'nadeshiko_if_none_found': 'If none found, then try', 'immersionkit_if_none_found': 'If none found, then try'}
    _CHOICES = {'nadeshiko_sentence_en_lang': (False, [('English', 'en'), ('Spanish', 'es')]), 'nadeshiko_sentence_selection': (False, _NADE_SELECTION_CHOICES), 'nadeshiko_fallback_selection': (False, _NADE_SELECTION_CHOICES), 'subs_terminal': (False, [('Show it', 'visible'), ('Start minimized', 'minimized'), ('Never show it', 'hidden')]), 'subs_sentence_selection': (False, _SUBS_SELECTION_CHOICES), 'subs_fallback_selection': (False, _SUBS_SELECTION_CHOICES), 'immersionkit_sentence_selection': (False, _IK_SELECTION_CHOICES), 'subs_if_none_found': (False, _chain_choices(_PROVIDER_SUBS)), 'nadeshiko_if_none_found': (False, _chain_choices(_PROVIDER_NADESHIKO)), 'immersionkit_if_none_found': (False, _chain_choices(_PROVIDER_IMMERSIONKIT)), 'immersionkit_fallback_selection': (False, _IK_SELECTION_CHOICES)}
    _SPIN_RANGES = {'nadeshiko_min_length': (0, 5000, ''), 'nadeshiko_max_length': (0, 5000, 'No maximum'), 'nadeshiko_pool_size': (1, 50, ''), 'nadeshiko_fallback_pool_size': (1, 50, ''), 'nadeshiko_fallback_min_length': (0, 5000, ''), 'nadeshiko_fallback_max_length': (0, 5000, 'No maximum'), 'subs_multi_count': (1, 100, ''), 'subs_min_length': (0, 5000, ''), 'subs_max_length': (0, 5000, 'No maximum'), 'subs_pool_size': (1, _SUBS_MAX_TAKE, ''), 'subs_fallback_pool_size': (1, _SUBS_MAX_TAKE, ''), 'subs_fallback_min_length': (0, 5000, ''), 'subs_fallback_max_length': (0, 5000, 'No maximum'), 'subs_context_before': (0, _SUBS_CONTEXT_CAP, ''), 'subs_context_after': (0, _SUBS_CONTEXT_CAP, ''), 'immersionkit_min_length': (0, 5000, ''), 'immersionkit_max_length': (0, 5000, 'No maximum'), 'immersionkit_per_title': (5, 50, ''), 'immersionkit_fallback_min_length': (0, 5000, ''), 'immersionkit_fallback_max_length': (0, 5000, 'No maximum'), 'immersionkit_context_before': (0, ik.CONTEXT_CAP, ''), 'immersionkit_context_after': (0, ik.CONTEXT_CAP, '')}
    _HOTKEY_KEYS = {'reviewer_hotkey_subs', 'reviewer_hotkey_nadeshiko', 'reviewer_hotkey_immersionkit'}
    _PLACEHOLDERS = {'nadeshiko_api_key': 'Paste Nadeshiko API key', 'nadeshiko_base_url': 'https://api.nadeshiko.co/v1', 'nadeshiko_sentence_field': 'Blank = choose in Run dialog', 'nadeshiko_image_field': 'Blank = choose in Run dialog', 'nadeshiko_audio_field': 'Blank = choose in Run dialog', 'nadeshiko_sentence_en_field': 'Blank = choose in Run dialog', 'subs_base_url': 'http://127.0.0.1:5010', 'subs_project_dir': 'Blank = the installed Aobana (1.7 or later)', 'subs_python': 'python', 'subs_folder': 'Blank = search everything', 'subs_sentence_field': 'Blank = choose in Run dialog', 'subs_source_field': 'Blank = choose in Run dialog', 'subs_image_field': 'Blank = choose in Run dialog', 'reviewer_hotkey_subs': 'Ctrl+Shift+W', 'reviewer_hotkey_nadeshiko': 'Ctrl+Shift+O', 'reviewer_hotkey_immersionkit': 'Ctrl+Shift+K', 'immersionkit_sentence_field': 'Blank = choose in Run dialog', 'immersionkit_image_field': 'Blank = choose in Run dialog', 'immersionkit_audio_field': 'Blank = choose in Run dialog', 'immersionkit_translation_field': 'Blank = choose in Run dialog', 'immersionkit_source_field': 'Blank = choose in Run dialog'}
    _TOOLTIPS = {'nadeshiko_sentence_en_field': "The sentence's translation, from Nadeshiko, goes into this field.\nThe Run dialog can choose another field, or (none), each run.", 'nadeshiko_fallback_enabled': 'When the main search finds no sentence, search once more with the\nfallback settings below. Untick to leave the note as it is instead.\nEach fallback is a second request, counted against the monthly quota.', 'subs_fallback_enabled': 'When the main search finds no sentence, search once more with the\nfallback settings below. Untick to leave the note as it is instead.', 'immersionkit_fallback_enabled': _IK_FALLBACK_TIP, 'subs_if_none_found': _CHAIN_TIP, 'nadeshiko_if_none_found': _CHAIN_TIP, 'immersionkit_if_none_found': _CHAIN_TIP, 'immersionkit_context_images': "With context on, add the context lines' screenshots after the sentence's.\nOff: only the sentence's own. The Run dialog's Context media row starts from this.", 'immersionkit_context_audio': "With context on, add the context lines' audio after the sentence's.\nOff: only the sentence's own. The Run dialog's Context media row starts from this.", 'immersionkit_per_title': "Immersion Kit returns this many examples from each title (about 95 titles) in one\nrequest; they are the pool every pick is made from. 5 is the API's smallest.", 'nadeshiko_require_media': 'Only pick a sentence that has both a screenshot and audio. When none in the pool\nhas both, the note is left as it is and the summary says why.', 'immersionkit_require_image': 'Only pick a sentence that has a screenshot (7 of 33 sampled for 病人 had none).\nEvery Immersion Kit sentence has audio.', 'immersionkit_furigana': 'Readings from Immersion Kit, as <ruby> over the kanji. Off by default.', 'immersionkit_strip_names': "Drop a (speaker) tag when words follow it, by the same rule as Aobana's No （names）.\nImmersion Kit also uses parentheses for sound cues and glosses, so it is off by default.", 'immersionkit_context': "Put the lines before and after the sentence around it, in the sentence field, and\ntheir images and audio with the sentence's, in reading order.\nOne more request per note, so a run takes about twice as long; the media adds none.", 'nadeshiko_bold_tail': _NADE_BOLD_TAIL_TIP, 'nadeshiko_furigana': "Readings from Nadeshiko's word analysis, as <ruby> over the kanji.\nA sentence without that analysis is written as before, without readings.", 'subs_min_length': _SUBS_LENGTH_TIP, 'subs_max_length': _SUBS_LENGTH_TIP, 'subs_fallback_min_length': _SUBS_LENGTH_TIP, 'subs_fallback_max_length': _SUBS_LENGTH_TIP, 'subs_folder': 'Only sentences from this title in your Aobana library: a show, a book,\na manga series. Choose… lists them; blank searches everything.', 'subs_image_field': "A Manga sentence's page image goes into this field.\nSubtitle and book sentences have no image and leave it alone.", 'subs_project_dir': 'Leave blank to use the installed Aobana 1.7 or later, found automatically.\nSet it for a portable or source copy, or to choose between two installs:\nthe folder that holds the aobana folder.', 'subs_python': 'At python, an installed Aobana runs with its own Python.\nChange it only for a source copy run with a particular interpreter.', 'subs_terminal': 'Only applies when this add-on starts Aobana itself. If a server already\nanswers at the Aobana URL (port 5010 by default), it is used as it is.\n"Start minimized" is best-effort: Windows Terminal may ignore it.\nWindows only - elsewhere the server always starts in its own session.'}
    _BROWSE_KEYS = ('subs_project_dir',)
    _TAB_NOTES = {'General': [('What each source needs', ['<b>Aobana</b>: Aobana 1.7 or later on this computer, found automatically. No key and no quota. One search at a time; its fallback is a second search on your computer, which costs nothing.', '<b>Nadeshiko</b>: an API key. Each key may send 150 requests a minute and 5,000 a month, and each note is one request. <b>Its fallback sends a second request</b> for every note the main search left empty, which counts against the monthly quota.', "<b>Immersion Kit</b>: no key. It allows about one request every 2 seconds, so a run goes one note at a time: about 3.5 minutes per 100 notes, twice that with context (one more request per note; the context lines' images and audio add none). <b>Its fallback sends no request</b>: the one search already returned every candidate, and the fallback picks again from them. If it still rate-limits, the run stops and keeps what it found."])]}
    _HELP_LINKS = {'nadeshiko_api_key': ('Get key', 'https://nadeshiko.co/user/developer')}

    def __init__(self, parent=None) -> None:
        super().__init__(parent or mw)
        self.setWindowTitle(MENU_NAME + ' Settings')
        self.defaults = _read_default_config()
        current = _read_config()
        self.extra_config = {k: v for k, v in current.items() if k not in self.defaults}
        self.values = dict(self.defaults)
        self.values.update({k: v for k, v in current.items() if k in self.defaults})
        self.widgets: Dict[str, Any] = {}
        self.labels: Dict[str, Any] = {}
        self._build_ui()

    def _build_ui(self) -> None:
        self.setMinimumWidth(720)
        self.setMinimumHeight(520)
        layout = QVBoxLayout(self)
        tabs = QTabWidget(self)
        added: set[str] = set()
        for title, sections in self._TAB_ORDER:
            tab, page = self._make_page()
            labels = []
            for section, keys in sections:
                if section:
                    box = QGroupBox(section, tab)
                    form = QFormLayout(box)
                    page.addWidget(box)
                else:
                    form = QFormLayout()
                    page.addLayout(form)
                form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
                labels.extend(self._add_rows(form, keys, added))
            for section, lines in self._TAB_NOTES.get(title, []):
                box = QGroupBox(section, tab)
                box_layout = QVBoxLayout(box)
                for line in lines:
                    note = QLabel(line, box)
                    note.setWordWrap(True)
                    box_layout.addWidget(note)
                page.addWidget(box)
            page.addStretch(1)
            self._align_labels(labels)
            tabs.addTab(tab, title)
        advanced_keys = [key for key in self.defaults if key not in added]
        if advanced_keys:
            tab, page = self._make_page()
            form = QFormLayout()
            page.addLayout(form)
            for key in advanced_keys:
                self._add_setting_row(form, key)
            page.addStretch(1)
            tabs.addTab(tab, 'Advanced')
        layout.addWidget(tabs)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel | QDialogButtonBox.StandardButton.RestoreDefaults, self)
        qconnect(buttons.accepted, self._save)
        qconnect(buttons.rejected, self.reject)
        restore = buttons.button(QDialogButtonBox.StandardButton.RestoreDefaults)
        if restore is not None:
            qconnect(restore.clicked, self._restore_defaults)
        layout.addWidget(buttons)

    def _add_rows(self, form: QFormLayout, keys: List[str], added: set) -> list:
        labels = []
        for key in keys:
            if key in self.defaults:
                self._add_setting_row(form, key)
                added.add(key)
                labels.append(self.labels[key])
        return labels

    def _make_page(self):
        tab = QWidget(self)
        layout = QVBoxLayout(tab)
        scroll = QScrollArea(tab)
        scroll.setWidgetResizable(True)
        try:
            scroll.setFrameShape(QFrame.Shape.NoFrame)
        except Exception:
            pass
        body = QWidget(scroll)
        page = QVBoxLayout(body)
        scroll.setWidget(body)
        layout.addWidget(scroll)
        return (tab, page)

    @staticmethod
    def _align_labels(labels: list) -> None:
        try:
            width = max((label.sizeHint().width() for label in labels))
        except Exception:
            return
        for label in labels:
            label.setMinimumWidth(width)

    def _add_setting_row(self, form: QFormLayout, key: str) -> None:
        default = self.defaults[key]
        value = self.values.get(key, default)
        widget = self._make_widget(key, default, value)
        self.widgets[key] = widget
        label = QLabel(self._label_for(key))
        tip = self._TOOLTIPS.get(key, key)
        label.setToolTip(tip)
        widget.setToolTip(tip)
        if key == 'subs_terminal' and os.name != 'nt':
            label.setEnabled(False)
            widget.setEnabled(False)
        form.addRow(label, self._wrap_widget_with_help(key, widget))
        self.labels[key] = label
        switch_key = _FALLBACK_SWITCH_OF.get(key)
        if switch_key:
            switch = self.widgets.get(switch_key)
            if isinstance(switch, QCheckBox):
                on = switch.isChecked()
                label.setEnabled(on)
                widget.setEnabled(on)
                qconnect(switch.toggled, label.setEnabled)
                qconnect(switch.toggled, widget.setEnabled)

    def _label_for(self, key: str) -> str:
        if key in self._LABELS:
            return self._LABELS[key]
        return key.replace('_', ' ').capitalize()

    def _make_widget(self, key: str, default: Any, value: Any):
        value = self._display_value(key, default, value)
        if key in self._CHOICES:
            editable, choices = self._CHOICES[key]
            return self._make_choice_widget(value, choices, editable)
        if key in self._HOTKEY_KEYS:
            return self._make_hotkey_widget(value)
        if isinstance(default, bool):
            widget = QCheckBox(self)
            widget.setChecked(bool(value))
            return widget
        if isinstance(default, int) and (not isinstance(default, bool)):
            widget = QSpinBox(self)
            min_val, max_val, special_text = self._SPIN_RANGES.get(key, (-1000000, 1000000, ''))
            widget.setRange(min_val, max_val)
            if special_text:
                widget.setSpecialValueText(special_text)
            try:
                widget.setValue(int(value))
            except Exception:
                widget.setValue(default)
            return widget
        if isinstance(default, list):
            widget = QLineEdit(self)
            if isinstance(value, list):
                widget.setText(', '.join((str(item) for item in value)))
            else:
                widget.setText(str(value or ''))
            self._apply_placeholder(key, widget)
            return widget
        widget = QLineEdit(self)
        widget.setText('' if value is None else str(value))
        self._apply_placeholder(key, widget)
        return widget

    def _wrap_widget_with_help(self, key: str, widget):
        if key in self._BROWSE_KEYS:
            return self._with_button(widget, 'Browse…', 'Choose the folder in Explorer', lambda _checked=False, k=key: self._browse(k))
        if key == 'subs_folder':
            return self._with_button(widget, 'Choose…', 'Choose from the titles in Aobana', lambda _checked=False: self._choose_title())
        link_info = self._HELP_LINKS.get(key)
        if not link_info:
            return widget
        button_text, url = link_info
        container = QWidget(self)
        layout = QHBoxLayout(container)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(widget)
        button = QPushButton(button_text, container)
        button.setToolTip(url)
        qconnect(button.clicked, lambda _checked=False, link=url: openLink(link))
        layout.addWidget(button)
        return container

    def _with_button(self, widget, text: str, tip: str, on_click):
        container = QWidget(self)
        layout = QHBoxLayout(container)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(widget)
        button = QPushButton(text, container)
        button.setToolTip(tip)
        qconnect(button.clicked, on_click)
        layout.addWidget(button)
        return container

    def _browse(self, key: str) -> None:
        field = self.widgets.get(key)
        if not isinstance(field, QLineEdit):
            return
        current = field.text().strip()
        start = current if os.path.isdir(current) else os.path.expanduser('~')
        picked = QFileDialog.getExistingDirectory(self, 'Aobana folder', start)
        if picked:
            field.setText(os.path.normpath(picked))

    def _choose_title(self) -> None:
        field = self.widgets.get('subs_folder')
        if not isinstance(field, QLineEdit):
            return
        cfg = dict(self.values)
        for key in ('subs_base_url', 'subs_project_dir', 'subs_python', 'subs_terminal', 'subs_autostart'):
            if key in self.widgets:
                cfg[key] = self._value_from_widget(key, self.defaults.get(key))
        client = _subs_make_client(cfg, get_logger())
        try:
            from aqt.qt import QApplication
            client.ensure_running(autostart=bool(cfg.get('subs_autostart', True)), tick=QApplication.processEvents)
            titles = client.titles()
        except AobanaError as exc:
            showWarning(str(exc))
            return
        finally:
            client.shutdown()
        picked = _pick_title(self, titles, field.text().strip())
        if picked is not None:
            field.setText(picked)

    def _display_value(self, key: str, default: Any, value: Any) -> Any:
        if isinstance(default, str) and _is_placeholder_config_value(str(value)) and str(value).strip().upper().startswith('REPLACE_'):
            return ''
        return value

    def _apply_placeholder(self, key: str, widget: QLineEdit) -> None:
        placeholder = self._PLACEHOLDERS.get(key, '')
        if placeholder:
            widget.setPlaceholderText(placeholder)

    def _make_choice_widget(self, value: Any, choices: List[tuple[str, str]], editable: bool) -> QComboBox:
        widget = QComboBox(self)
        widget.setEditable(editable)
        for label, data in choices:
            widget.addItem(label, data)
        self._set_combo_value(widget, str(value or ''))
        return widget

    def _make_hotkey_widget(self, value: Any) -> QKeySequenceEdit:
        widget = QKeySequenceEdit(self)
        widget.setKeySequence(QKeySequence(str(value or '')))
        try:
            widget.setClearButtonEnabled(True)
        except Exception:
            pass
        try:
            widget.setMaximumSequenceLength(1)
        except Exception:
            pass
        return widget

    def _restore_defaults(self) -> None:
        for key, default in self.defaults.items():
            self._set_widget_value(self.widgets[key], default)

    def _set_widget_value(self, widget, value: Any) -> None:
        value = '' if isinstance(value, str) and value.strip().upper().startswith('REPLACE_') else value
        if isinstance(widget, QCheckBox):
            widget.setChecked(bool(value))
        elif isinstance(widget, QSpinBox):
            try:
                widget.setValue(int(value))
            except Exception:
                widget.setValue(0)
        elif isinstance(widget, QKeySequenceEdit):
            widget.setKeySequence(QKeySequence('' if value is None else str(value)))
        elif isinstance(widget, QLineEdit):
            if isinstance(value, list):
                widget.setText(', '.join((str(item) for item in value)))
            else:
                widget.setText('' if value is None else str(value))
        elif isinstance(widget, QComboBox):
            self._set_combo_value(widget, str(value or ''))

    def _set_combo_value(self, widget: QComboBox, value: str) -> None:
        for idx in range(widget.count()):
            if str(widget.itemData(idx) or '') == value:
                widget.setCurrentIndex(idx)
                return
        if widget.isEditable():
            widget.setEditText(value)

    def _value_from_widget(self, key: str, default: Any) -> Any:
        widget = self.widgets[key]
        if isinstance(widget, QKeySequenceEdit):
            return str(widget.keySequence().toString()).strip()
        if isinstance(widget, QComboBox):
            idx = widget.currentIndex()
            text = widget.currentText().strip()
            if idx >= 0 and text == widget.itemText(idx):
                data = widget.itemData(idx)
                if data is not None:
                    return data
            return '' if text.upper().startswith('REPLACE_') else text
        if isinstance(default, bool):
            return bool(widget.isChecked())
        if isinstance(default, int) and (not isinstance(default, bool)):
            return int(widget.value())
        if isinstance(default, list):
            raw = widget.text().strip()
            return [part.strip() for part in raw.split(',') if part.strip()]
        raw = widget.text()
        if raw.strip().upper().startswith('REPLACE_'):
            return ''
        if default is None:
            raw = raw.strip()
            return raw if raw else None
        return raw

    def _save(self) -> None:
        data = {k: v for k, v in self.extra_config.items() if k != 'ddg_locale'}
        for key, default in self.defaults.items():
            data[key] = self._value_from_widget(key, default)
        if _write_config(data):
            _settings_to_last({key: value for key, value in data.items() if key in self.defaults and value != self.values.get(key)})
            for shortcut in getattr(mw, '_aobana_reibun_shortcuts', []):
                key = getattr(shortcut, '_aobana_reibun_config_key', '')
                if key in data:
                    shortcut.setKey(QKeySequence(str(data[key] or '').strip()))
                    shortcut.setEnabled(bool(str(data[key] or '').strip()))
            showInfo('Settings saved.')
            self.accept()
        else:
            showWarning('Could not save the settings.')

class BackfillImagesDialog(QDialog):

    def __init__(self, mw, mode: str, browser=None) -> None:
        super().__init__(browser or mw)
        self.mw = mw
        self.mode = mode
        self.browser = browser
        self.logger = get_logger()
        self.cfg = _read_config()
        self.setWindowTitle(MENU_NAME)
        self._build_ui()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        self.provider_combo = QComboBox(self)
        self.provider_combo.addItems(_PROVIDERS)
        self.provider_combo.hide()
        _last_prov_raw = str(_read_last_settings().get('last_provider', '')).strip()
        self.provider_combo.setCurrentText(_last_prov_raw if _last_prov_raw in _PROVIDERS else _PROVIDER_SUBS)
        switch = QHBoxLayout()
        self.provider_tabs = QTabBar(self)
        self.provider_tabs.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.provider_tabs.setExpanding(False)
        self.provider_tabs.setDrawBase(False)
        for name in _PROVIDERS:
            self.provider_tabs.addTab(name)
        self.provider_tabs.setCurrentIndex(_PROVIDERS.index(self.provider_combo.currentText()))
        qconnect(self.provider_tabs.currentChanged, lambda i: self.provider_combo.setCurrentText(_PROVIDERS[i]) if 0 <= i < len(_PROVIDERS) else None)
        switch.addWidget(self.provider_tabs)
        switch.addStretch(1)
        layout.addLayout(switch)
        notes_box = QGroupBox('Notes', self)
        notes = QFormLayout(notes_box)
        if self.mode == 'deck':
            self.deck_combo = QComboBox(self)
            try:
                items = list(self.mw.col.decks.all_names_and_ids())
            except Exception:
                items = []
            for item in items:
                name = getattr(item, 'name', None)
                if not name and isinstance(item, (list, tuple)) and (len(item) >= 1):
                    name = item[0] if isinstance(item[0], str) else None
                if name:
                    self.deck_combo.addItem(name)
            try:
                active_deck_name = self.mw.col.decks.name(self.mw.col.decks.get_current_id())
                idx = self.deck_combo.findText(active_deck_name)
                if idx >= 0:
                    self.deck_combo.setCurrentIndex(idx)
            except Exception:
                pass
            notes.addRow('Deck', self.deck_combo)
        self.query_field = QComboBox(self)
        notes.addRow('Query field', self.query_field)
        layout.addWidget(notes_box)
        self.lbl_target = QLabel('Target Field', self)
        self.lbl_target.hide()
        self.target_field = QComboBox(self)
        self.target_field.hide()
        self._chains: Dict[str, Dict[str, Any]] = {}
        self.provider_pages = QStackedWidget(self)
        pages = {_PROVIDER_NADESHIKO: self._build_nadeshiko_page, _PROVIDER_SUBS: self._build_subs_page, _PROVIDER_IMMERSIONKIT: self._build_immersionkit_page}
        for name in _PROVIDERS:
            self.provider_pages.addWidget(pages[name]())
        layout.addWidget(self.provider_pages)
        shared = QHBoxLayout()
        self.exact_chk = QCheckBox('Exact search', self)
        self.exact_chk.setChecked(False)
        shared.addWidget(QLabel('If filled:', self))
        self.fill_skip = QRadioButton('Skip', self)
        self.fill_replace = QRadioButton('Replace', self)
        self.fill_append = QRadioButton('Append', self)
        self.fill_context = QRadioButton('Add context', self)
        self.fill_context.setToolTip('For notes that already have a sentence, find that sentence in the corpus\nand add the before/after context lines without replacing the sentence.\nEmpty notes get a new sentence with context. Running it again replaces the context.')
        self.fill_group = QButtonGroup(self)
        for rb in (self.fill_skip, self.fill_replace, self.fill_append, self.fill_context):
            self.fill_group.addButton(rb)
            shared.addWidget(rb)
        (self.fill_replace if bool(self.cfg.get('default_replace', False)) else self.fill_skip).setChecked(True)
        qconnect(self.fill_context.toggled, self._lock_subs_context)
        shared.addStretch(1)
        layout.addLayout(shared)
        self.replace_chk = self.fill_replace
        self.append_chk = self.fill_append
        bottom = QHBoxLayout()
        self.settings_btn = QPushButton('Settings…', self)
        qconnect(self.settings_btn.clicked, self._open_settings)
        bottom.addWidget(self.settings_btn)
        self.note_count_label = QLabel('', self)
        bottom.addWidget(self.note_count_label)
        bottom.addStretch(1)
        buttons = QDialogButtonBox(self)
        self.run_btn = buttons.addButton('Run', QDialogButtonBox.ButtonRole.AcceptRole)
        self.cancel_btn = buttons.addButton(QDialogButtonBox.StandardButton.Cancel)
        self.run_btn.setDefault(True)
        bottom.addWidget(buttons)
        layout.addLayout(bottom)
        qconnect(self.cancel_btn.clicked, self.reject)
        qconnect(self.run_btn.clicked, lambda: _on_run(self))
        try:
            qconnect(self.provider_combo.currentTextChanged, lambda _=None: self._switch_provider())
        except Exception:
            pass
        try:
            _refresh_field_dropdowns(self)
        except Exception:
            pass
        if hasattr(self, 'deck_combo'):
            try:
                qconnect(self.deck_combo.currentTextChanged, lambda _=None: (_refresh_field_dropdowns(self), self._update_note_count()))
            except Exception:
                pass
        self._restore_toggles()
        for other in _PROVIDERS:
            if other != _selected_provider(self):
                self._apply_last_settings_for_provider(other, query=False)
        self._apply_last_settings_for_provider()
        self._update_note_count()
        _toggle_provider_fields(self)
        self._shown_provider = _selected_provider(self)

    def _query_text(self) -> str:
        combo = self.query_field
        return (combo.currentText() if hasattr(combo, 'currentText') else str(combo.text())).strip()

    def _switch_provider(self) -> None:
        old = getattr(self, '_shown_provider', None)
        if old and old != _selected_provider(self):
            _remember_run_settings(self, old, self._query_text(), fields=False)
        self._shown_provider = _selected_provider(self)
        self._apply_last_settings_for_provider()
        self._restore_shared(self._shown_provider)
        _toggle_provider_fields(self)

    def done(self, result) -> None:
        try:
            _remember_run_settings(self, _selected_provider(self), self._query_text(), fields=False)
        except Exception:
            pass
        super().done(result)

    def _build_nadeshiko_page(self):
        page = QWidget(self)
        col = QVBoxLayout(page)
        col.setContentsMargins(0, 0, 0, 0)
        source_box = QGroupBox('Source', page)
        source = QHBoxLayout(source_box)
        self.nade_cat_anime = QCheckBox('Anime', page)
        self.nade_cat_anime.setChecked(bool(self.cfg.get('nadeshiko_cat_anime', True)))
        self.nade_cat_live = QCheckBox('Live Action', page)
        self.nade_cat_live.setChecked(bool(self.cfg.get('nadeshiko_cat_live', True)))
        self.nade_cat_yt = QCheckBox('YouTube', page)
        self.nade_cat_yt.setChecked(bool(self.cfg.get('nadeshiko_cat_yt', True)))
        self.nade_req_media = QCheckBox('Req. Image && Audio', page)
        self.nade_req_media.setChecked(bool(self.cfg.get('nadeshiko_require_media', False)))
        self.nade_req_media.setToolTip('Only pick a sentence that has both a screenshot and audio.')
        for w in (self.nade_cat_anime, self.nade_cat_live, self.nade_cat_yt, self.nade_req_media):
            source.addWidget(w)
        source.addStretch(1)
        col.addWidget(source_box)
        col.addWidget(self._build_chain_box(page, _PROVIDER_NADESHIKO))
        write_box = QGroupBox('Write to', page)
        write = QFormLayout(write_box)
        self.lbl_nade_img = QLabel('Image', page)
        self.nade_image_field = QComboBox(page)
        write.addRow(self.lbl_nade_img, self.nade_image_field)
        self.lbl_nade_audio = QLabel('Audio', page)
        self.nade_audio_field = QComboBox(page)
        write.addRow(self.lbl_nade_audio, self.nade_audio_field)
        self.lbl_nade_sentence = QLabel('Sentence', page)
        self.nade_sentence_field = QComboBox(page)
        write.addRow(self.lbl_nade_sentence, self.nade_sentence_field)
        self.lbl_nade_translation = QLabel('Translation', page)
        self.nade_translation_field = QComboBox(page)
        self.nade_translation_field.setToolTip("The sentence's English (or Spanish, in Settings) translation.")
        write.addRow(self.lbl_nade_translation, self.nade_translation_field)
        col.addWidget(write_box)
        opts_box = QGroupBox('Options', page)
        opts = QGridLayout(opts_box)
        self.nade_bold = QCheckBox('Bold', page)
        self.nade_bold.setChecked(bool(self.cfg.get('nadeshiko_bold', True)))
        opts.addWidget(self.nade_bold, 0, 0)
        self.nade_furigana = QCheckBox('Furigana', page)
        self.nade_furigana.setChecked(bool(self.cfg.get('nadeshiko_furigana', False)))
        self.nade_furigana.setToolTip("Add readings over the kanji, from Nadeshiko's word analysis.\nA sentence without that analysis is written without them.")
        opts.addWidget(self.nade_furigana, 0, 1)
        self.nade_bold_tail = QCheckBox('Bold ending', page)
        self.nade_bold_tail.setChecked(bool(self.cfg.get('nadeshiko_bold_tail', True)))
        self.nade_bold_tail.setToolTip(_NADE_BOLD_TAIL_TIP)
        self.nade_bold_tail.setEnabled(self.nade_bold.isChecked())
        qconnect(self.nade_bold.toggled, self.nade_bold_tail.setEnabled)
        opts.addWidget(self.nade_bold_tail, 1, 1)
        self._nade_opts = opts
        col.addWidget(opts_box)
        return page

    def _build_immersionkit_page(self):
        page = QWidget(self)
        col = QVBoxLayout(page)
        col.setContentsMargins(0, 0, 0, 0)
        source_box = QGroupBox('Source', page)
        source = QHBoxLayout(source_box)
        self.ik_cat_anime = QCheckBox('Anime', page)
        self.ik_cat_anime.setChecked(bool(self.cfg.get('immersionkit_cat_anime', True)))
        self.ik_cat_drama = QCheckBox('Drama', page)
        self.ik_cat_drama.setChecked(bool(self.cfg.get('immersionkit_cat_drama', True)))
        self.ik_cat_games = QCheckBox('Games', page)
        self.ik_cat_games.setChecked(bool(self.cfg.get('immersionkit_cat_games', True)))
        self.ik_req_image = QCheckBox('Req. Image', page)
        self.ik_req_image.setChecked(bool(self.cfg.get('immersionkit_require_image', False)))
        self.ik_req_image.setToolTip('Only pick a sentence that has a screenshot. Every one has audio.')
        for w in (self.ik_cat_anime, self.ik_cat_drama, self.ik_cat_games, self.ik_req_image):
            source.addWidget(w)
        source.addStretch(1)
        col.addWidget(source_box)
        col.addWidget(self._build_chain_box(page, _PROVIDER_IMMERSIONKIT))
        write_box = QGroupBox('Write to', page)
        write = QFormLayout(write_box)
        self.ik_sentence_field = QComboBox(page)
        write.addRow(QLabel('Sentence', page), self.ik_sentence_field)
        self.ik_image_field = QComboBox(page)
        write.addRow(QLabel('Image', page), self.ik_image_field)
        self.ik_audio_field = QComboBox(page)
        write.addRow(QLabel('Audio', page), self.ik_audio_field)
        self.ik_translation_field = QComboBox(page)
        self.ik_translation_field.setToolTip("The sentence's English translation.")
        write.addRow(QLabel('Translation', page), self.ik_translation_field)
        self.ik_source_field = QComboBox(page)
        self.ik_source_field.setToolTip('The title the sentence comes from, e.g. Lucky Star.')
        write.addRow(QLabel('Source', page), self.ik_source_field)
        ctx_row = QHBoxLayout()
        self.ik_context_chk = QCheckBox('Include context', page)
        self.ik_context_chk.setChecked(bool(self.cfg.get('immersionkit_context', False)))
        self.ik_context_chk.setToolTip("Put the lines before and after the sentence around it, in the sentence field, and\n(with Images and Audio) their images and audio with the sentence's, in reading order.\nOne more request per note, so a run takes about twice as long; the media adds none.")
        ctx_row.addWidget(self.ik_context_chk, 1)
        self.ik_ctx_images = QCheckBox('Images', page)
        self.ik_ctx_images.setChecked(bool(self.cfg.get('immersionkit_context_images', True)))
        self.ik_ctx_images.setToolTip("Add the context lines' screenshots too. Off: only the sentence's.")
        self.ik_ctx_audio = QCheckBox('Audio', page)
        self.ik_ctx_audio.setChecked(bool(self.cfg.get('immersionkit_context_audio', True)))
        self.ik_ctx_audio.setToolTip("Add the context lines' audio too. Off: only the sentence's.")
        ctx_media = QHBoxLayout()
        for w in (self.ik_ctx_images, self.ik_ctx_audio):
            ctx_media.addWidget(w)
        ctx_media.addStretch(1)
        self._ik_context_media_enabled()
        qconnect(self.ik_context_chk.toggled, lambda _=None: self._ik_context_media_enabled())
        ctx_row.addWidget(QLabel('before', page))
        self.ik_context_before = QSpinBox(page)
        self.ik_context_before.setRange(0, ik.CONTEXT_CAP)
        self.ik_context_before.setValue(_ik_context_span(self.cfg, 'immersionkit_context_before', 2))
        ctx_row.addWidget(self.ik_context_before)
        ctx_row.addWidget(QLabel('after', page))
        self.ik_context_after = QSpinBox(page)
        self.ik_context_after.setRange(0, ik.CONTEXT_CAP)
        self.ik_context_after.setValue(_ik_context_span(self.cfg, 'immersionkit_context_after', 1))
        ctx_row.addWidget(self.ik_context_after)
        write.addRow(QLabel('Context', page), ctx_row)
        write.addRow(QLabel('Context media', page), ctx_media)
        col.addWidget(write_box)
        opts_box = QGroupBox('Options', page)
        opts = QGridLayout(opts_box)
        self.ik_furigana = QCheckBox('Furigana', page)
        self.ik_furigana.setChecked(bool(self.cfg.get('immersionkit_furigana', False)))
        self.ik_bold = QCheckBox('Bold', page)
        self.ik_bold.setChecked(bool(self.cfg.get('immersionkit_bold', True)))
        self.ik_strip_names = QCheckBox('No (names)', page)
        self.ik_strip_names.setChecked(bool(self.cfg.get('immersionkit_strip_names', False)))
        self.ik_strip_names.setToolTip("Drop a (speaker) tag when words follow it, as Aobana's No （names） does.\nImmersion Kit also puts sound cues and glosses in parentheses.")
        opts.addWidget(self.ik_furigana, 0, 0)
        opts.addWidget(self.ik_bold, 0, 1)
        opts.addWidget(self.ik_strip_names, 1, 1)
        self._ik_opts = opts
        col.addWidget(opts_box)
        return page

    def _build_subs_page(self):
        page = QWidget(self)
        col = QVBoxLayout(page)
        col.setContentsMargins(0, 0, 0, 0)
        source_box = QGroupBox('Source', page)
        source = QHBoxLayout(source_box)
        self.subs_cat_subs = QCheckBox('Subtitles', page)
        self.subs_cat_subs.setChecked(bool(self.cfg.get('subs_cat_subs', True)))
        self.subs_cat_epub = QCheckBox('Books', page)
        self.subs_cat_epub.setChecked(bool(self.cfg.get('subs_cat_epub', True)))
        self.subs_cat_manga = QCheckBox('Manga', page)
        self.subs_cat_manga.setChecked(bool(self.cfg.get('subs_cat_manga', True)))
        source.addWidget(self.subs_cat_subs)
        source.addWidget(self.subs_cat_epub)
        source.addWidget(self.subs_cat_manga)
        source.addStretch(1)
        col.addWidget(source_box)
        col.addWidget(self._build_chain_box(page, _PROVIDER_SUBS))
        write_box = QGroupBox('Write to', page)
        write = QFormLayout(write_box)
        self.lbl_subs_sentence = QLabel('Sentence', page)
        self.subs_sentence_field = QComboBox(page)
        write.addRow(self.lbl_subs_sentence, self.subs_sentence_field)
        self.lbl_subs_source = QLabel('Source', page)
        self.subs_source_field = QComboBox(page)
        write.addRow(self.lbl_subs_source, self.subs_source_field)
        self.lbl_subs_image = QLabel('Image', page)
        self.subs_image_field = QComboBox(page)
        self.subs_image_field.setToolTip("A Manga sentence's page image goes here. Subtitles and Books have none.")
        write.addRow(self.lbl_subs_image, self.subs_image_field)
        ctx_row = QHBoxLayout()
        self.subs_context_chk = QCheckBox('Include context', page)
        self.subs_context_chk.setChecked(bool(self.cfg.get('subs_context', False)))
        self.subs_context_chk.setToolTip('Include surrounding context lines when writing a sentence to a note,\njoined with a full-width space.\nTo add context to sentences already on your cards, use If filled: Add context.')
        ctx_row.addWidget(self.subs_context_chk, 1)
        ctx_row.addWidget(QLabel('before', page))
        self.subs_context_before = QSpinBox(page)
        self.subs_context_before.setRange(0, _SUBS_CONTEXT_CAP)
        self.subs_context_before.setValue(_subs_context_span(self.cfg, 'subs_context_before', 2))
        ctx_row.addWidget(self.subs_context_before)
        ctx_row.addWidget(QLabel('after', page))
        self.subs_context_after = QSpinBox(page)
        self.subs_context_after.setRange(0, _SUBS_CONTEXT_CAP)
        self.subs_context_after.setValue(_subs_context_span(self.cfg, 'subs_context_after', 1))
        ctx_row.addWidget(self.subs_context_after)
        self.lbl_subs_context = QLabel('Context', page)
        write.addRow(self.lbl_subs_context, ctx_row)
        col.addWidget(write_box)
        opts_box = QGroupBox('Options', page)
        opts = QGridLayout(opts_box)
        self.subs_furigana = QCheckBox('Furigana', page)
        self.subs_furigana.setChecked(bool(self.cfg.get('subs_furigana', True)))
        self.subs_bold = QCheckBox('Bold', page)
        self.subs_bold.setChecked(bool(self.cfg.get('subs_bold', True)))
        self.subs_strip_names = QCheckBox('No （names）', page)
        self.subs_strip_names.setChecked(bool(self.cfg.get('subs_strip_names', False)))
        self.subs_strip_names.setToolTip('Drop a （speaker） tag when words follow it; keep sound cues such as （足音） that\nstand before another tag or end the line. Two speakers on one line become\n「line one」「line two」. Books are never touched.')
        self.subs_no_repeats = QCheckBox('No repeats', page)
        self.subs_no_repeats.setChecked(bool(self.cfg.get('subs_no_repeats', False)))
        self.subs_no_repeats.setToolTip("Within one run, never give the same corpus sentence to two notes.\nWhen a word's pool runs out, a repeat is allowed and counted in the summary.")
        self.subs_multi_label = QLabel('Sentences per note', page)
        self.subs_multi = QSpinBox(page)
        self.subs_multi.setRange(1, _SUBS_MULTI_CAP)
        self.subs_multi.setValue(_subs_multi_count(self.cfg))
        self.subs_multi.setToolTip('How many example sentences to put in the sentence field, one per line.\nEvery one contains the word. They are not filtered by which other words\nyou know (not i+1).')
        multi = QHBoxLayout()
        multi.addWidget(self.subs_multi_label)
        multi.addWidget(self.subs_multi)
        multi.addStretch(1)
        opts.addWidget(self.subs_furigana, 0, 0)
        opts.addWidget(self.subs_bold, 0, 1)
        opts.addWidget(self.subs_strip_names, 1, 0)
        opts.addWidget(self.subs_no_repeats, 1, 1)
        opts.addLayout(multi, 2, 1)
        self._subs_opts = opts
        col.addWidget(opts_box)
        return page
    _NADE_TOGGLES = [('cat_anime', 'nade_cat_anime'), ('cat_live', 'nade_cat_live'), ('cat_yt', 'nade_cat_yt'), ('require_media', 'nade_req_media'), ('bold', 'nade_bold'), ('furigana', 'nade_furigana'), ('bold_tail', 'nade_bold_tail')]
    _SUBS_TOGGLES = [('cat_subs', 'subs_cat_subs'), ('cat_epub', 'subs_cat_epub'), ('cat_manga', 'subs_cat_manga'), ('furigana', 'subs_furigana'), ('bold', 'subs_bold'), ('strip_names', 'subs_strip_names'), ('no_repeats', 'subs_no_repeats'), ('context', 'subs_context_chk')]
    _SUBS_SPINS = [('multi_count', 'subs_multi'), ('context_before', 'subs_context_before'), ('context_after', 'subs_context_after')]
    _IK_TOGGLES = [('cat_anime', 'ik_cat_anime'), ('cat_drama', 'ik_cat_drama'), ('cat_games', 'ik_cat_games'), ('require_image', 'ik_req_image'), ('furigana', 'ik_furigana'), ('bold', 'ik_bold'), ('strip_names', 'ik_strip_names'), ('context', 'ik_context_chk'), ('context_images', 'ik_ctx_images'), ('context_audio', 'ik_ctx_audio')]
    _IK_SPINS = [('context_before', 'ik_context_before'), ('context_after', 'ik_context_after')]

    @classmethod
    def _options_of(cls, block: str) -> tuple:
        toggles, spins = {'nadeshiko': (cls._NADE_TOGGLES, []), 'subs': (cls._SUBS_TOGGLES, cls._SUBS_SPINS), 'immersionkit': (cls._IK_TOGGLES, cls._IK_SPINS)}[block]
        return ([name for name, _attr in toggles], [name for name, _attr in spins])

    def _restore_toggles(self) -> None:
        last = _read_last_settings() or {}
        for block, toggles, spins in (('nadeshiko', self._NADE_TOGGLES, []), ('subs', self._SUBS_TOGGLES, self._SUBS_SPINS), ('immersionkit', self._IK_TOGGLES, self._IK_SPINS)):
            saved = last.get(block, {}) if isinstance(last.get(block), dict) else {}
            for key, attr in toggles:
                if isinstance(saved.get(key), bool):
                    getattr(self, attr).setChecked(saved[key])
            for key, attr in spins:
                if isinstance(saved.get(key), int) and (not isinstance(saved.get(key), bool)):
                    getattr(self, attr).setValue(saved[key])
        self._restore_shared(_selected_provider(self))

    def _restore_shared(self, provider: str) -> None:
        last = _read_last_settings() or {}
        prov = _PROVIDER_BLOCK[provider]
        shared = last.get(prov, {}) if isinstance(last.get(prov), dict) else {}
        self.exact_chk.setChecked(shared.get('exact') is True)
        fill = shared.get('if_filled')
        if fill in ('skip', 'replace', 'append', 'context'):
            {'skip': self.fill_skip, 'replace': self.fill_replace, 'append': self.fill_append, 'context': self.fill_context}[fill].setChecked(True)

    def _ik_field_combos(self):
        return [('sentence_field', self.ik_sentence_field), ('image_field', self.ik_image_field), ('audio_field', self.ik_audio_field), ('translation_field', self.ik_translation_field), ('source_field', self.ik_source_field)]

    def _lock_subs_context(self, checked: bool) -> None:
        chk = getattr(self, 'subs_context_chk', None)
        if chk is None:
            return
        if checked:
            if getattr(self, '_subs_context_own', None) is None:
                self._subs_context_own = chk.isChecked()
            chk.setChecked(True)
            chk.setEnabled(False)
        else:
            chk.setEnabled(True)
            own = getattr(self, '_subs_context_own', None)
            if own is not None:
                chk.setChecked(own)
            self._subs_context_own = None

    def _subs_context_choice(self) -> bool:
        own = getattr(self, '_subs_context_own', None)
        return bool(self.subs_context_chk.isChecked()) if own is None else bool(own)

    def _ik_context_media_enabled(self) -> None:
        on = self.ik_context_chk.isChecked()
        self.ik_ctx_images.setEnabled(on)
        self.ik_ctx_audio.setEnabled(on)

    def _build_chain_box(self, page, provider: str):
        order, on = _chain_saved(provider)
        box = QGroupBox('If none found', page)
        row = QHBoxLayout(box)
        own = QCheckBox('1. ' + provider, page)
        own.setChecked(True)
        own.setEnabled(False)
        own.setToolTip("This tab's source always runs first.")
        row.addWidget(own)
        checks = {}
        for name in order:
            chk = QCheckBox(name, page)
            chk.setChecked(name in on)
            chk.setToolTip('Search %s for the notes the sources before it found nothing for,\nwriting to the fields on its own tab.' % name)
            checks[name] = chk
            row.addWidget(chk)
        swap = QPushButton('⇄', page)
        swap.setToolTip('Swap the order of the other two.')
        swap.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        qconnect(swap.clicked, lambda: self._swap_chain(provider))
        row.addWidget(swap)
        row.addStretch(1)
        self._chains[provider] = {'order': order, 'checks': checks, 'row': row}
        self._number_chain(provider)
        return box

    def _number_chain(self, provider: str) -> None:
        chain = self._chains[provider]
        for i, name in enumerate(chain['order']):
            chain['checks'][name].setText('%d. %s' % (i + 2, name))

    def _swap_chain(self, provider: str) -> None:
        chain = self._chains[provider]
        chain['order'].reverse()
        row = chain['row']
        for i, name in enumerate(chain['order']):
            row.removeWidget(chain['checks'][name])
            row.insertWidget(1 + i, chain['checks'][name])
        self._number_chain(provider)

    def _chain_after(self, provider: str) -> List[str]:
        chain = self._chains.get(provider)
        if not chain:
            return []
        return [name for name in chain['order'] if chain['checks'][name].isChecked()]

    def _open_settings(self) -> None:
        try:
            _remember_run_settings(self, _selected_provider(self), self._query_text(), fields=False)
        except Exception:
            pass
        dlg = SettingsDialog(self)
        if dlg.exec():
            self.cfg = _read_config()
            self._reload_saved_options()

    def _reload_saved_options(self) -> None:
        locked = getattr(self, '_subs_context_own', None) is not None
        if locked:
            self._lock_subs_context(False)
        self._restore_toggles()
        if getattr(self, 'fill_context', None) is not None and self.fill_context.isChecked() and (getattr(self, '_subs_context_own', None) is None):
            self._lock_subs_context(True)
        last = _read_last_settings() or {}
        for provider in list(getattr(self, '_chains', {})):
            order, on = _chain_saved(provider, last)
            self._set_chain(provider, order, on)

    def _set_chain(self, provider: str, order: List[str], on: List[str]) -> None:
        chain = self._chains[provider]
        chain['order'] = list(order)
        row = chain['row']
        for i, name in enumerate(chain['order']):
            row.removeWidget(chain['checks'][name])
            row.insertWidget(1 + i, chain['checks'][name])
            chain['checks'][name].setChecked(name in on)
        self._number_chain(provider)

    def _update_note_count(self) -> None:
        try:
            if self.mode == 'browser' and self.browser is not None:
                n = len(get_selected_note_ids(self.browser))
            else:
                deck_name = self.deck_combo.currentText() if hasattr(self, 'deck_combo') else ''
                n = len(get_deck_note_ids(self.mw.col, deck_name))
            self.note_count_label.setText(f"{n:,} note{('s' if n != 1 else '')}")
        except Exception:
            self.note_count_label.setText('')

    def _apply_last_settings_for_provider(self, provider: Optional[str]=None, query: bool=True) -> None:
        provider = provider or _selected_provider(self)
        try:
            last = _read_last_settings() or {}
            fields: List[str] = []
            for i in range(self.query_field.count()):
                fields.append(self.query_field.itemText(i))

            def _index_of(name: str, default_idx: int=0) -> int:
                if not name:
                    return default_idx
                try:
                    return fields.index(name)
                except Exception:
                    return default_idx
            if provider == _PROVIDER_SUBS:
                ls = last.get('subs', {}) if isinstance(last.get('subs'), dict) else {}
                qf = str(ls.get('query_field', ''))
                sentf = str(ls.get('sentence_field', ''))
                srcf = str(ls.get('source_field', ''))
                if 'image_field' in ls:
                    imgf = str(ls.get('image_field') or '')
                    idx = self.subs_image_field.findText(imgf) if imgf else 0
                    self.subs_image_field.setCurrentIndex(idx if idx >= 0 else 0)
                if qf and query:
                    self.query_field.setCurrentIndex(_index_of(qf, self.query_field.currentIndex()))
                if sentf:
                    self.subs_sentence_field.setCurrentIndex(_index_of(sentf, self.subs_sentence_field.currentIndex()))
                if srcf:
                    idx = self.subs_source_field.findText(srcf)
                    self.subs_source_field.setCurrentIndex(idx if idx >= 0 else 0)
                return
            if provider == _PROVIDER_IMMERSIONKIT:
                li = last.get('immersionkit', {}) if isinstance(last.get('immersionkit'), dict) else {}
                qf = str(li.get('query_field', ''))
                if qf and query:
                    self.query_field.setCurrentIndex(_index_of(qf, self.query_field.currentIndex()))
                for name, combo in self._ik_field_combos():
                    if name in li:
                        chosen = str(li.get(name) or '')
                        idx = combo.findText(chosen) if chosen else 0
                        combo.setCurrentIndex(idx if idx >= 0 else 0)
                return
            ln = last.get('nadeshiko', {}) if isinstance(last.get('nadeshiko'), dict) else {}
            qf = str(ln.get('query_field', ''))
            sentf = str(ln.get('sentence_field', ''))
            transf = str(ln.get('translation_field', ''))
            if qf and query:
                self.query_field.setCurrentIndex(_index_of(qf, self.query_field.currentIndex()))
            for key, combo in (('image_field', self.nade_image_field), ('audio_field', self.nade_audio_field)):
                if key in ln:
                    chosen = str(ln.get(key) or '')
                    idx = combo.findText(chosen) if chosen else 0
                    combo.setCurrentIndex(idx if idx >= 0 else 0)
            if sentf:
                self.nade_sentence_field.setCurrentIndex(_index_of(sentf, self.nade_sentence_field.currentIndex()))
            if 'translation_field' in ln:
                idx = self.nade_translation_field.findText(transf) if transf else 0
                self.nade_translation_field.setCurrentIndex(idx if idx >= 0 else 0)
        except Exception:
            pass

def _strip_tags(text: str) -> str:
    try:
        return re.sub('<[^>]+>', '', text or '')
    except Exception:
        return text or ''

def _image_extension_from_bytes(content: bytes) -> str:
    if content.startswith(b'\xff\xd8\xff'):
        return '.jpg'
    if content.startswith(b'\x89PNG\r\n\x1a\n'):
        return '.png'
    if content.startswith(b'GIF87a') or content.startswith(b'GIF89a'):
        return '.gif'
    if content.startswith(b'RIFF') and content[8:12] == b'WEBP':
        return '.webp'
    return '.jpg'

def _image_filename_from_url(url: str, fallback_stem: str, content: bytes) -> str:
    tail = url.split('/')[-1].split('?')[0]
    safe_tail = ensure_media_filename_safe(tail)
    _, ext = os.path.splitext(safe_tail)
    if ext.lower() in {'.jpg', '.jpeg', '.png', '.gif', '.webp'} and len(safe_tail) <= 120:
        return safe_tail
    return ensure_media_filename_safe(f'{fallback_stem}{_image_extension_from_bytes(content)}')

def _is_placeholder_config_value(value: str) -> bool:
    text = str(value or '').strip()
    return not text or text.upper().startswith('REPLACE_')

def _field_or_blank(name: str) -> str:
    name = str(name or '').strip()
    return '' if name == _NO_FIELD else name

def _nade_translation_lang(cfg: Dict[str, Any]) -> str:
    lang = str(cfg.get('nadeshiko_sentence_en_lang', 'en') or 'en').strip().lower()
    return lang if lang in ('en', 'es') else 'en'

def _nade_translation(segment: Optional[Dict[str, Any]], lang: str) -> str:
    if not isinstance(segment, dict):
        return ''
    obj = segment.get('textEs' if lang == 'es' else 'textEn') or {}
    return str(obj.get('content', '') or '').strip() if isinstance(obj, dict) else ''
_NADE_HL_TAG_RE = re.compile('</?(?:em|mark|b)>')
_NADE_TAIL_RE = re.compile('<span class="highlight-tail">(.*?)</span>', re.S)
_NADE_ANY_TAG_RE = re.compile('<[^>]+>')
_NADE_TAIL_STOP = ('助詞', '補助記号', '記号', '空白')

def _nade_highlight(hl: str, tokens: Any=None, tail: bool=True) -> str:
    stops = []
    for tok in tokens if isinstance(tokens, list) else []:
        try:
            if str(tok.get('p', '') or '').startswith(_NADE_TAIL_STOP):
                stops.append(int(tok['b']))
        except (AttributeError, KeyError, TypeError, ValueError):
            continue

    def one(m):
        text = m.group(1)
        if not tail:
            return text
        start = len(_NADE_ANY_TAG_RE.sub('', hl[:m.start()]))
        keep = min([b - start for b in stops if start <= b < start + len(text)] + [len(text)])
        return ('<em>%s</em>' % text[:keep] if keep else '') + text[keep:]
    return _NADE_TAIL_RE.sub(one, hl).replace('</em><em>', '')

def _nade_furigana(text_obj: Dict[str, Any], bold: bool, tail: bool=True) -> Optional[str]:
    content = str(text_obj.get('content', '') or '')
    tokens = text_obj.get('tokens')
    if not content or not isinstance(tokens, list) or (not tokens):
        return None
    spans: Dict[int, tuple] = {}
    try:
        for tok in sorted(tokens, key=lambda t: int(t['b'])):
            start, end = (int(tok['b']), int(tok['e']))
            surface = tok.get('s')
            if not 0 <= start <= end <= len(content) or (surface is not None and content[start:end] != surface):
                return None
            pos = start
            for run in tok.get('f') or []:
                t, r = (str(run.get('t', '') or ''), str(run.get('r', '') or ''))
                if content[pos:pos + len(t)] != t:
                    return None
                if t and r:
                    spans[pos] = (pos + len(t), r)
                pos += len(t)
    except (KeyError, TypeError, ValueError, AttributeError):
        return None
    if not spans:
        return None
    bolds: set = set()
    hl = _nade_highlight(str(text_obj.get('highlight', '') or ''), tokens, tail)
    if bold and hl:
        pos, inside = (0, False)
        for piece in re.split('(</?(?:em|mark|b)>)', hl):
            if _NADE_HL_TAG_RE.fullmatch(piece or ''):
                inside = not piece.startswith('</')
                continue
            if inside:
                bolds.update(range(pos, pos + len(piece)))
            pos += len(piece)
        if _NADE_HL_TAG_RE.sub('', hl) != content:
            return None
    return ik.markup(list(content), spans, bolds).strip()

def _nade_format_sentence(segment: Dict[str, Any], lang_code: str, bold: bool=True, furigana: bool=False, query: str='', tail: bool=True) -> str:
    try:
        lc = (lang_code or 'jp').lower()
        text_key = f"text{('En' if lc == 'en' else 'Es' if lc == 'es' else 'Ja')}"
        text_obj = segment.get(text_key) or {}
        query = str(query or '').strip()
        content_ja = str(text_obj.get('content', '') or '') if isinstance(text_obj, dict) else ''
        if bold and query and (text_key == 'textJa') and (not text_obj.get('highlight')) and (query in content_ja) and ('<' not in content_ja):
            text_obj = dict(text_obj, highlight=content_ja.replace(query, '<em>%s</em>' % query))
        if furigana and text_key == 'textJa':
            with_ruby = _nade_furigana(text_obj, bold, tail)
            if with_ruby:
                return with_ruby
        hl = _nade_highlight(str(text_obj.get('highlight', '') or ''), text_obj.get('tokens'), tail).strip()
        content = str(text_obj.get('content', '') or '').strip()
        if hl:
            if not bold:
                return content or re.sub('</?(em|mark|b)>', '', hl)
            for tag in ('em', 'mark'):
                hl = hl.replace(f'<{tag}>', '<b>').replace(f'</{tag}>', '</b>')
            return hl.replace('</b><b>', '')
        return content
    except Exception:
        return str((segment.get('textJa') or {}).get('content', '') or '').strip()
_SENTENCE_REPLACEMENTS: List[tuple[str, str]] = [('?\u3000', '？'), ('? ', '？'), ('?', '？'), ('!\u3000', '！'), ('! ', '！'), ('!', '！'), ('-\u3000', '――'), ('- ', '――'), ('-', '――'), ('...\u3000', '…'), ('... ', '…'), ('...', '…'), ('➨\u3000', '――'), ('➨ ', '――'), ('➨', '――'), ('\\', '')]
_WORD_HYPHEN_RE = re.compile('(?<=[A-Za-z0-9])-(?=[A-Za-z0-9])')
_HELD_HYPHEN = '\ue000'
_MARKUP_RE = re.compile('(<[^>]*>)')

def _postprocess_sentence(text: str) -> str:
    pieces = _MARKUP_RE.split(text)
    for k in range(0, len(pieces), 2):
        piece = _WORD_HYPHEN_RE.sub(_HELD_HYPHEN, pieces[k])
        for src, dst in _SENTENCE_REPLACEMENTS:
            piece = piece.replace(src, dst)
        pieces[k] = piece.replace(_HELD_HYPHEN, '-')
    return ''.join(pieces)
_NADE_SELECTION_MODES = {'none', 'longest', 'random', 'smallest', 'median', 'corpus_random'}
_NADE_MAX_TAKE = 50

def _nadeshiko_selection_mode(cfg: Dict[str, Any], key: str='nadeshiko_sentence_selection', default: str='random') -> str:
    mode = str(cfg.get(key, default) or default).strip().lower()
    aliases = {'short': 'smallest', 'shortest': 'smallest', 'small': 'smallest', 'min': 'smallest', 'minimum': 'smallest', 'middle': 'median', 'no sorting': 'none', 'best': 'none', 'best match': 'none', 'corpus': 'corpus_random'}
    mode = aliases.get(mode, mode)
    return mode if mode in _NADE_SELECTION_MODES else default

def _nadeshiko_pool_size(cfg: Dict[str, Any], key: str, default: int) -> int:
    try:
        size = int(cfg.get(key, default) or default)
    except Exception:
        size = default
    return max(1, min(size, _NADE_MAX_TAKE))

def _nadeshiko_build_kwargs(cfg, exact, query, min_len, max_len):
    selection_mode, search_take, search_sort = _nadeshiko_search_options(cfg)
    kwargs = {'query': query, 'take': search_take, 'sort_mode': search_sort, 'exact_match': bool(exact), 'min_length': min_len, 'max_length': max_len}
    cat = _nadeshiko_categories(cfg)
    if cat:
        kwargs['category'] = cat
    return (kwargs, selection_mode)

def _nadeshiko_categories(cfg: Dict[str, Any]) -> Optional[List[str]]:
    cat = []
    if cfg.get('nadeshiko_cat_anime', True):
        cat.append('ANIME')
    if cfg.get('nadeshiko_cat_live', True):
        cat.append('JDRAMA')
    if cfg.get('nadeshiko_cat_yt', True):
        cat.append('YOUTUBE')
    return cat if 0 < len(cat) < 3 else None

def _nadeshiko_search_options(cfg: Dict[str, Any]) -> tuple[str, int, str]:
    mode = _nadeshiko_selection_mode(cfg)
    take = _nadeshiko_pool_size(cfg, 'nadeshiko_pool_size', 25)
    return (mode, take, 'RANDOM' if mode == 'corpus_random' else 'RELEVANCE')

def _nadeshiko_fallback_options(cfg: Dict[str, Any]) -> tuple[str, int, str, int, Optional[int]]:
    mode = _nadeshiko_selection_mode(cfg, 'nadeshiko_fallback_selection', 'random')
    take = _nadeshiko_pool_size(cfg, 'nadeshiko_fallback_pool_size', 50)
    try:
        min_len = max(0, int(cfg.get('nadeshiko_fallback_min_length', 2) or 0))
    except Exception:
        min_len = 2
    try:
        max_len = int(cfg.get('nadeshiko_fallback_max_length', 200) or 0) or None
    except Exception:
        max_len = 200
    return (mode, take, 'RANDOM' if mode == 'corpus_random' else 'RELEVANCE', min_len, max_len)

def _nadeshiko_segment_length(segment: Dict[str, Any], lang_code: str) -> int:
    try:
        return len(_strip_tags(_nade_format_sentence(segment, lang_code)))
    except Exception:
        return 0

def _nadeshiko_pick_segment(segments: List[Dict[str, Any]], mode: str, lang_code: str='jp') -> Optional[Dict[str, Any]]:
    candidates = [seg for seg in segments or [] if isinstance(seg, dict)]
    if not candidates:
        return None
    if mode not in _NADE_SELECTION_MODES:
        mode = 'random'
    if mode in ('none', 'corpus_random'):
        return candidates[0]
    if mode == 'random':
        try:
            return random.choice(candidates)
        except Exception:
            return candidates[0]
    ranked = sorted(candidates, key=lambda seg: _nadeshiko_segment_length(seg, lang_code))
    if mode == 'smallest':
        return ranked[0]
    if mode == 'median':
        return ranked[len(ranked) // 2]
    return ranked[-1]

def _nadeshiko_has_media(segment: Any) -> bool:
    urls = (segment.get('urls') if isinstance(segment, dict) else None) or {}
    return bool(str(urls.get('imageUrl', '') or '').strip() and str(urls.get('audioUrl', '') or '').strip())

def _nadeshiko_fetch_segment(client: NadeshikoApiClient, cfg: Dict[str, Any], query: str, exact: bool, min_len: Optional[int], max_len: Optional[int], lang_code: str='jp', misses: Optional[List[str]]=None, held: Optional[set]=None) -> Optional[Dict[str, Any]]:
    kwargs, selection_mode = _nadeshiko_build_kwargs(cfg, exact, query, min_len, max_len)
    require_media = bool(cfg.get('nadeshiko_require_media', False))

    def _pick(segments: List[Dict[str, Any]], mode: str) -> Optional[Dict[str, Any]]:
        if held:
            segments = [seg for seg in segments if field_plain(_postprocess_sentence(_nade_format_sentence(seg, lang_code, bold=False))) not in held]
        if require_media:
            with_media = [seg for seg in segments if _nadeshiko_has_media(seg)]
            if segments and (not with_media) and (misses is not None) and ('no_media' not in misses):
                misses.append('no_media')
            segments = with_media
        return _nadeshiko_pick_segment(segments, mode, lang_code)
    segments = (client.search(**kwargs) or {}).get('segments') or []
    segment = _pick(segments, selection_mode)
    if segment:
        return segment
    if not bool(cfg.get('nadeshiko_fallback_enabled', True)):
        return None
    fb_mode, fb_take, fb_sort, fb_min, fb_max = _nadeshiko_fallback_options(cfg)
    fb_kwargs = dict(kwargs)
    fb_kwargs.update({'take': fb_take, 'sort_mode': fb_sort, 'min_length': fb_min, 'max_length': fb_max})
    segments = (client.search(**fb_kwargs) or {}).get('segments') or []
    return _pick(segments, fb_mode)

def _subs_selection_mode(cfg: Dict[str, Any], key: str='subs_sentence_selection', default: str='random') -> str:
    return _nadeshiko_selection_mode(cfg, key, default)

def _subs_pool_size(cfg: Dict[str, Any], key: str, default: int) -> int:
    try:
        size = int(cfg.get(key, default) or default)
    except Exception:
        size = default
    return max(1, min(size, _SUBS_MAX_TAKE))

def _subs_sort_mode(mode: str) -> str:
    if mode == 'corpus_random':
        return 'random'
    if mode == 'longest':
        return 'desc'
    if mode == 'smallest':
        return 'asc'
    return 'recommended'
_SUBS_CORPORA = (('subs_cat_subs', 'subs'), ('subs_cat_epub', 'epub'), ('subs_cat_manga', 'manga'))

def _subs_wanted_media(cfg: Dict[str, Any]) -> set:
    wanted = {media for key, media in _SUBS_CORPORA if bool(cfg.get(key, True))}
    return wanted or {media for _key, media in _SUBS_CORPORA}

def _subs_media_param(cfg: Dict[str, Any], sets: bool=False) -> str:
    wanted = _subs_wanted_media(cfg)
    if len(wanted) == 1:
        return next(iter(wanted))
    if sets and len(wanted) < len(_SUBS_CORPORA):
        return ','.join((media for _key, media in _SUBS_CORPORA if media in wanted))
    return 'all'

def _subs_keep_media(rows, cfg: Dict[str, Any]):
    wanted = _subs_wanted_media(cfg)
    if len(wanted) == len(_SUBS_CORPORA):
        return rows
    return [row for row in rows or [] if isinstance(row, dict) and str(row.get('media_type') or 'subs') in wanted]

def _subs_row_length(row: Dict[str, Any]) -> int:
    try:
        return int(row.get('char_count') or 0)
    except Exception:
        return len(plain_text(row.get('display_line') or ''))

def _subs_filter_by_length(rows: List[Dict[str, Any]], min_len: Optional[int], max_len: Optional[int]) -> List[Dict[str, Any]]:
    out = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        n = _subs_row_length(row)
        if min_len and n < int(min_len):
            continue
        if max_len and n > int(max_len):
            continue
        out.append(row)
    return out

def _subs_pick_row(rows: List[Dict[str, Any]], mode: str) -> Optional[Dict[str, Any]]:
    candidates = [row for row in rows or [] if isinstance(row, dict)]
    if not candidates:
        return None
    if mode not in _NADE_SELECTION_MODES:
        mode = 'random'
    if mode in ('none', 'corpus_random'):
        return candidates[0]
    if mode == 'random':
        try:
            return random.choice(candidates)
        except Exception:
            return candidates[0]
    ranked = sorted(candidates, key=_subs_row_length)
    if mode == 'smallest':
        return ranked[0]
    if mode == 'median':
        return ranked[len(ranked) // 2]
    return ranked[-1]
_SUBS_MULTI_CAP = 100

def _subs_multi_count(cfg: Dict[str, Any]) -> int:
    try:
        value = int(cfg.get('subs_multi_count', 1) or 1)
    except Exception:
        value = 1
    return max(1, min(_SUBS_MULTI_CAP, value))

def _subs_context_span(cfg: Dict[str, Any], key: str, default: int) -> int:
    try:
        value = int(cfg.get(key, default))
    except Exception:
        value = default
    return max(0, min(_SUBS_CONTEXT_CAP, value))

def _subs_pick_rows(rows, mode: str, count: int):
    pool = [row for row in rows or [] if isinstance(row, dict)]
    picked = []
    seen = set()
    while pool and len(picked) < count:
        row = _subs_pick_row(pool, mode)
        if row is None:
            break
        key = (row.get('media_type'), row.get('rowid'))
        if key not in seen:
            seen.add(key)
            picked.append(row)
        pool = [r for r in pool if r is not row]
    return picked

def _subs_search_options(cfg: Dict[str, Any]) -> tuple[str, int, str]:
    mode = _subs_selection_mode(cfg)
    take = _subs_pool_size(cfg, 'subs_pool_size', 500)
    return (mode, take, _subs_sort_mode(mode))

def _subs_fallback_options(cfg: Dict[str, Any]) -> tuple[str, int, str, int, Optional[int]]:
    mode = _subs_selection_mode(cfg, 'subs_fallback_selection', 'random')
    take = _subs_pool_size(cfg, 'subs_fallback_pool_size', 500)
    try:
        min_len = max(0, int(cfg.get('subs_fallback_min_length', 2) or 0))
    except Exception:
        min_len = 2
    try:
        max_len = int(cfg.get('subs_fallback_max_length', 200) or 0) or None
    except Exception:
        max_len = 200
    return (mode, take, _subs_sort_mode(mode), min_len, max_len)

def _subs_apply_tag_mode(rows, enabled: bool):
    if not enabled:
        return rows
    kept = []
    for row in rows:
        if str(row.get('media_type') or '') != 'subs':
            kept.append(row)
            continue
        original = row.get('display_line') or ''
        text, usable = strip_speaker_tags(original)
        if not usable or not text.strip():
            continue
        if '<b>' in original and '<b>' not in text:
            continue
        new_row = dict(row)
        new_row['display_line'] = text
        if isinstance(row.get('char_count'), int):
            removed = _subs_display_length(original) - _subs_display_length(text)
            new_row['char_count'] = max(0, row['char_count'] - removed)
        kept.append(new_row)
    return kept

def _subs_display_length(text: str) -> int:
    return len(re.sub('[^\\w、\\.,]', '', field_plain(text)))

def _subs_strip_names(cfg) -> bool:
    return bool(cfg.get('subs_strip_names', False))

def _subs_fetch_rows(client: AobanaClient, cfg: Dict[str, Any], query: str, exact: bool, min_len: Optional[int], max_len: Optional[int], count: int=1, used: Optional[set]=None, held: Optional[tuple]=None, held_hits: Optional[list]=None, diagnostics: Optional[list]=None) -> List[Dict[str, Any]]:
    count = max(1, int(count or 1))
    media = _subs_media_param(cfg)
    if media == 'all' and len(_subs_wanted_media(cfg)) < len(_SUBS_CORPORA):
        media = _subs_media_param(cfg, sets=bool(getattr(client, 'media_sets', lambda: False)()))
    folder = str(cfg.get('subs_folder', '') or '').strip()
    strip_names = _subs_strip_names(cfg)

    def search(limit, sort):
        rows = client.search(query, exact=exact, media=media, limit=limit, sort=sort, folder=folder)
        if diagnostics is not None:
            diagnostics.append({'has_results': bool(rows), 'outside_media': getattr(rows, 'outside_media', []), 'global_total': getattr(rows, 'global_total', None)})
        return _subs_apply_tag_mode(_subs_keep_media(rows, cfg), strip_names)
    picked: List[Dict[str, Any]] = []
    seen = set()
    stages = []

    def on_note(row):
        if held is None:
            return False
        keys, texts = held
        if (row.get('media_type'), row.get('rowid')) in keys or sentence_key(render_sentence(row.get('display_line') or '', furigana=False, bold=False)) in texts:
            if held_hits is not None:
                held_hits.append(row.get('rowid'))
            return True
        return False

    def take_from(rows, mode, lo, hi, allow_used=False):
        if not allow_used:
            stages.append((rows, mode, lo, hi))
        wanted = count - len(picked)
        if wanted <= 0:
            return
        pool = [row for row in _subs_filter_by_length(rows, lo, hi) if (row.get('media_type'), row.get('rowid')) not in seen and (allow_used or used is None or (row.get('media_type'), row.get('rowid')) not in used) and (not on_note(row))]
        for row in _subs_pick_rows(pool, mode, wanted):
            seen.add((row.get('media_type'), row.get('rowid')))
            picked.append(row)

    def repeats_if_short():
        if used is not None and len(picked) < count:
            for rows, mode, lo, hi in list(stages):
                take_from(rows, mode, lo, hi, allow_used=True)
    mode, take, sort = _subs_search_options(cfg)
    take_from(search(take, sort), mode, min_len, max_len)
    if len(picked) >= count:
        return picked
    if mode in ('longest', 'smallest'):
        take_from(search(take, 'recommended'), mode, min_len, max_len)
        if len(picked) >= count:
            return picked
    if bool(cfg.get('subs_fallback_enabled', True)):
        fb_mode, fb_take, fb_sort, fb_min, fb_max = _subs_fallback_options(cfg)
        take_from(search(fb_take, fb_sort), fb_mode, fb_min, fb_max)
    repeats_if_short()
    return picked

def _subs_is_manga(row: Dict[str, Any]) -> bool:
    return str(row.get('media_type') or '') == 'manga'

def _subs_context_html(client, row, before: int, after: int, furigana: bool, bold: bool, strip_names: bool) -> Optional[str]:
    got = client.context(row, before, after)
    if got is None:
        return None
    sentence = render_sentence(row.get('display_line') or '', furigana=furigana, bold=bold)
    tagged_sentence = tag_sentence(sentence, row.get('rowid'), row.get('media_type', 'subs'))
    return context_wrap(tagged_sentence, got[0], got[1], furigana=furigana, strip_names=strip_names and row.get('media_type') == 'subs', media=row.get('media_type', 'subs'))

def _subs_rendered(rows, furigana: bool, bold: bool) -> tuple:
    rendered = []
    titles = []
    for row in rows:
        one = row.get('_with_context')
        if not one:
            raw_sent = render_sentence(row.get('display_line') or '', furigana=furigana, bold=bold)
            one = tag_sentence(raw_sent, row.get('rowid'), row.get('media_type', 'subs')) if raw_sent else ''
        if not one:
            continue
        rendered.append(one)
        titles.append(str(row.get('title') or '').strip())
    return ('<br>'.join(rendered), '<br>'.join((t for t in titles if t)))

def _subs_write_page_images(col, note, field: str, images: List[bytes], rowids: List[int], replace: bool, append: bool) -> bool:
    if field not in note or not images:
        return False
    cur = note[field]
    if cur and (not replace) and (not append):
        return False
    tags = []
    for content, rowid in zip(images, rowids):
        name = ensure_media_filename_safe('aobana_manga_%d%s' % (int(rowid), _image_extension_from_bytes(content)))
        tags.append('<img src="%s">' % col.media.write_data(name, content))
    html = ''.join(tags)
    note[field] = cur + html if append and cur and (not replace) else html
    return True

def _subs_make_client(cfg: Dict[str, Any], logger: Any=None) -> AobanaClient:
    return AobanaClient(base_url=str(cfg.get('subs_base_url', '') or '').strip(), project_dir=str(cfg.get('subs_project_dir', '') or '').strip(), python_exe=str(cfg.get('subs_python', 'python') or 'python').strip(), window=str(cfg.get('subs_terminal', 'hidden') or 'hidden').strip().lower(), logger=logger)

def _save_note(col, note) -> None:
    if _UNDO.begin is not None:
        _UNDO.begin()
    try:
        col.update_note(note)
    except AttributeError:
        note.flush()
    finally:
        if _UNDO.merge is not None:
            _UNDO.merge()
_UNDO = types.SimpleNamespace(begin=None, merge=None)

@contextmanager
def _batch_undo(mw):
    if _UNDO.begin is not None:
        yield
        return
    col = mw.col
    add = getattr(col, 'add_custom_undo_entry', None)
    merge = getattr(col, 'merge_undo_entries', None)
    opened: List[Any] = []

    def begin() -> None:
        if not opened:
            opened.append(add('Aobana Reibun'))

    def merge_now() -> None:
        if opened:
            merge(opened[0])
    usable = callable(add) and callable(merge)
    _UNDO.begin = begin if usable else None
    _UNDO.merge = merge_now if usable else None
    try:
        yield
    finally:
        _UNDO.begin = None
        _UNDO.merge = None
        if opened:
            refresh = getattr(mw, 'update_undo_actions', None)
            if callable(refresh):
                refresh()

def _clear_missing_media(note, field: str, replace: bool) -> bool:
    if replace and field and (field in note) and note[field]:
        note[field] = ''
        return True
    return False
_WINDOW_AFTER_S = 1.0
_ESTIMATE_AFTER_S = 3.0
_ESTIMATE_EVERY_S = 1.0
_TICK_MS = 250
_TOOLTIP_PERIOD_MS = 10 * 60 * 1000
_BAR_STEPS = 1000

class _Estimate:

    def __init__(self, poll=None) -> None:
        self.poll = poll
        self.reset()

    def reset(self) -> None:
        self.started = time.monotonic()
        self.remaining: Optional[float] = None
        self.answered = 0.0
        self.asking = False
        self.last_ask = 0.0
        self.fraction = 0.0
        self.token = object()

    def tick(self) -> None:
        now = time.monotonic()
        if self.poll is None or self.asking or now - self.started < _ESTIMATE_AFTER_S or (now - self.last_ask < _ESTIMATE_EVERY_S):
            return
        self.asking = True
        self.last_ask = now
        token, poll = (self.token, self.poll)

        def ask() -> None:
            try:
                remaining = poll()
            except Exception:
                remaining = None
            if self.token is not token:
                return
            if remaining is not None:
                self.remaining = float(remaining)
                self.answered = time.monotonic()
            self.asking = False
        threading.Thread(target=ask, daemon=True).start()

    def left(self) -> Optional[float]:
        if self.remaining is None:
            return None
        return max(0.0, self.remaining - (time.monotonic() - self.answered))

    def share(self) -> Optional[float]:
        left = self.left()
        if left is None:
            return None
        elapsed = time.monotonic() - self.started
        total = elapsed + left
        self.fraction = max(self.fraction, min(elapsed / total if total > 0 else 1.0, 0.99))
        return self.fraction

    def text(self) -> str:
        left = self.left()
        return ' about %d s left' % int(round(left)) if left is not None and left >= 1 else ''

class _Busy:

    def __init__(self) -> None:
        self.generation = 0
        self.reset()

    def reset(self) -> None:
        self.label = ''
        self.started = 0.0
        self.hotkey = False
        self.shown = ''
        self.still = False
        self.estimate = _Estimate()
        self.cancel = None
        self.timer = None
        self.window = None
        self.blocker = None

    @property
    def active(self) -> bool:
        return bool(self.label)
_BUSY = _Busy()

def _show_tooltip(text: str) -> None:
    try:
        from aqt.utils import tooltip
        tooltip(text, period=_TOOLTIP_PERIOD_MS, parent=mw)
    except Exception:
        pass

def _close_tooltip() -> None:
    try:
        from aqt.utils import closeTooltip
        closeTooltip()
    except Exception:
        pass

def _busy_refuse() -> bool:
    if not _BUSY.active:
        return False
    _BUSY.still = True
    if _BUSY.window is not None:
        _window_update(_busy_text(), _BUSY.estimate.share())
    else:
        _BUSY.shown = 'Still searching…'
        _show_tooltip(_BUSY.shown)
    return True

def _busy_text() -> str:
    text = 'Still searching…' if _BUSY.still else 'Searching %s…' % _BUSY.label
    return text + _BUSY.estimate.text()

def _busy_begin(label: str, hotkey: bool=True, poll=None, cancel=None) -> None:
    _BUSY.reset()
    _BUSY.generation += 1
    _BUSY.label = label
    _BUSY.started = time.monotonic()
    _BUSY.hotkey = hotkey
    _BUSY.estimate = _Estimate(poll)
    _BUSY.cancel = cancel
    if not hotkey:
        return
    _BUSY.window, _BUSY.blocker = _open_window()
    try:
        from aqt.qt import QTimer
        timer = QTimer(mw)
        qconnect(timer.timeout, _busy_tick)
        timer.start(_TICK_MS)
        _BUSY.timer = timer
    except Exception:
        _BUSY.timer = None

def _busy_end() -> None:
    timer, window, blocker, tip = (_BUSY.timer, _BUSY.window, _BUSY.blocker, _BUSY.shown)
    _BUSY.reset()
    if timer is not None:
        try:
            timer.stop()
            timer.deleteLater()
        except Exception:
            pass
    _close_window(window, blocker)
    if tip:
        _close_tooltip()

def _busy_cancel() -> None:
    if not _BUSY.active:
        return
    cancel = _BUSY.cancel
    _BUSY.generation += 1
    _busy_end()
    _call_quietly(cancel)

def _call_quietly(fn) -> None:
    if fn is None:
        return

    def run() -> None:
        try:
            fn()
        except Exception:
            pass
    threading.Thread(target=run, daemon=True).start()

def _busy_tick() -> None:
    if not _BUSY.active or not _BUSY.hotkey:
        return
    try:
        if _BUSY.window is not None and _BUSY.window.wasCanceled():
            _busy_cancel()
            return
    except Exception:
        pass
    _BUSY.estimate.tick()
    if time.monotonic() - _BUSY.started >= _WINDOW_AFTER_S or _BUSY.still:
        _window_update(_busy_text(), _BUSY.estimate.share())

def _open_window():
    try:
        from aqt.qt import QApplication, QEvent, QObject, QProgressDialog, QPushButton, QWidget as _QWidget
    except Exception:
        return (None, None)
    try:
        window = QProgressDialog('Searching %s…' % _BUSY.label, 'Cancel', 0, 0, mw)
        window.setWindowTitle(MENU_NAME)
        button = window.findChild(QPushButton)
        button.setAutoDefault(False)
        button.setDefault(False)
        button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        window.setAutoClose(False)
        window.setAutoReset(False)
        window.setMinimumDuration(24 * 3600 * 1000)
        _stop_timers(window)
        window.setMinimumWidth(320)
        window.setWindowModality(Qt.WindowModality.ApplicationModal)
        window.cancel_button = button
        qconnect(window.canceled, _busy_cancel)
        qconnect(window.rejected, _busy_cancel)
    except Exception:
        return (None, None)
    blocked = {QEvent.Type.KeyPress, QEvent.Type.KeyRelease, QEvent.Type.ShortcutOverride, QEvent.Type.Shortcut, QEvent.Type.MouseButtonPress, QEvent.Type.MouseButtonRelease, QEvent.Type.MouseButtonDblClick, QEvent.Type.Wheel, QEvent.Type.ContextMenu, QEvent.Type.InputMethod}

    class Blocker(QObject):

        def eventFilter(self, obj, event):
            try:
                if event.type() not in blocked:
                    return False
                if obj is window.windowHandle():
                    return False
                if isinstance(obj, _QWidget):
                    return obj.window() is not window
                owner = obj.parent() if hasattr(obj, 'parent') else None
                return owner is not window
            except Exception:
                return False
    blocker = Blocker()
    try:
        QApplication.instance().installEventFilter(blocker)
    except Exception:
        blocker = None
    return (window, blocker)

def _window_update(text: str, share: Optional[float]) -> None:
    window = _BUSY.window
    if window is None:
        return
    try:
        if share is None:
            window.setRange(0, 0)
        else:
            window.setRange(0, _BAR_STEPS)
            window.setValue(int(share * _BAR_STEPS))
        window.setLabelText(text)
        if not window.isVisible():
            window.show()
    except Exception:
        pass

def _close_window(window, blocker) -> None:
    if blocker is not None:
        try:
            from aqt.qt import QApplication
            QApplication.instance().removeEventFilter(blocker)
        except Exception:
            pass
    if window is not None:
        for signal in (window.canceled, window.rejected):
            try:
                signal.disconnect()
            except Exception:
                pass
        _stop_timers(window)
        try:
            window.hide()
            window.deleteLater()
        except Exception:
            pass

def _stop_timers(window) -> None:
    try:
        from aqt.qt import QTimer
        for timer in window.findChildren(QTimer):
            timer.stop()
    except Exception:
        pass

def _start_worker(run) -> None:
    threading.Thread(target=run, daemon=True, name='AobanaReibun').start()

def _in_background(fn, on_done) -> None:
    on_main = getattr(getattr(mw, 'taskman', None), 'run_on_main', None)
    if callable(on_main):

        def run() -> None:
            try:
                result, error = (fn(), None)
            except Exception as exc:
                result, error = (None, exc)
            on_main(lambda: on_done(result, error))
        _start_worker(run)
        return
    try:
        result = fn()
    except Exception as exc:
        on_done(None, exc)
        return
    on_done(result, None)

def _no_answer(exc: BaseException) -> bool:
    try:
        import requests
        kinds = (requests.Timeout, requests.ConnectionError, TimeoutError, ConnectionError)
    except Exception:
        kinds = (TimeoutError, ConnectionError)
    seen = 0
    while exc is not None and seen < 5:
        if isinstance(exc, kinds):
            return True
        exc = exc.__cause__ or exc.__context__
        seen += 1
    return False

def _error_text(source: str, exc: BaseException) -> str:
    if isinstance(exc, NadeshikoApiError):
        if exc.rate_limited:
            return 'Nadeshiko is rate-limiting. Try again in a minute.'
        if exc.code == 'QUOTA_EXCEEDED':
            return "This month's Nadeshiko quota is spent."
        if exc.status in (401, 403):
            return 'Nadeshiko refused the API key. Check it in Settings.'
    if isinstance(exc, AobanaError):
        return str(exc)
    if _no_answer(exc):
        return '%s did not answer. Check the connection and try again.' % source
    if isinstance(exc, ImmersionKitError):
        return str(exc)
    return '%s: %s' % (source, exc)

def _hotkey_search(label: str, fetch, apply, poll=None, cancel=None, on_error=None) -> None:
    _busy_begin(label, poll=poll, cancel=cancel)
    generation = _BUSY.generation

    def done(result, error) -> None:
        if _BUSY.generation != generation:
            return
        _busy_end()
        if error is not None:
            text = _error_text(label, error)
            if on_error is not None:
                on_error(text)
            else:
                showWarning(text)
            return
        apply(result)
    _in_background(fetch, done)

def _hotkey_save(mw_, col, note) -> None:
    with _batch_undo(mw_):
        _save_note(col, note)

def _hotkey_note(mw_, nid):
    col = getattr(mw_, 'col', None)
    if col is None:
        return (None, None)
    try:
        return (col, col.get_note(nid))
    except Exception:
        return (col, None)

def _run_left_text(done_elapsed: float, count: int, total: int, since_done: float=0.0, note_left: Optional[float]=None) -> str:
    if count < 1 or count >= total or done_elapsed <= 0:
        return ''
    average = done_elapsed / count
    current = note_left if note_left is not None else max(0.0, average - since_done)
    left = current + average * (total - count - 1)
    if left < 60:
        return 'about %d s left' % max(1, int(round(left)))
    minutes = int(round(left / 60))
    if minutes < 60:
        return 'about %d min left' % minutes
    return 'about %d h %d min left' % divmod(minutes, 60)

def _batch_label(source: str, count: int, total: int, run_left: str, note_left: str) -> str:
    text = 'Searching %s… %d of %d notes' % (source, count, total)
    if run_left:
        text += ', ' + run_left
        if note_left:
            text += ' (this note: %s)' % note_left
    elif note_left:
        text += ' (this note: about %s left)' % note_left
    return text

def _run_with_progress(self, source: str, total: int, work, stop, done_count, cancel=None, poll=None) -> bool:
    from aqt.qt import QApplication, QProgressDialog
    steps = max(1, total) * _BAR_STEPS
    progress = QProgressDialog('Searching %s…' % source, 'Cancel', 0, steps, self)
    progress.setWindowTitle(MENU_NAME)
    try:
        progress.setWindowModality(Qt.WindowModality.WindowModal)
    except Exception:
        pass
    progress.setMinimumDuration(0)
    try:
        widest = _batch_label(source, total, total, 'about 99 h 59 min left', '9999 s')
        progress.setMinimumWidth(progress.fontMetrics().horizontalAdvance(widest) + 60)
    except Exception:
        pass
    progress.setValue(0)
    estimate = _Estimate(poll)
    seen = 0
    run_started = time.monotonic()
    done_at = run_started
    cancelled = False
    finished = threading.Event()
    failed: List[BaseException] = []

    def run() -> None:
        try:
            work()
        except BaseException as exc:
            failed.append(exc)
        finally:
            finished.set()
    try:
        threading.Thread(target=run, daemon=True, name='AobanaReibun batch').start()
        while not finished.is_set():
            QApplication.processEvents()
            if progress.wasCanceled():
                cancelled = True
                stop.set()
                _call_quietly(cancel)
                break
            count = min(done_count(), total)
            if count != seen:
                seen = count
                done_at = time.monotonic()
                estimate.reset()
            estimate.tick()
            share = estimate.share() if count < total else None
            progress.setValue(min(steps - 1, int((count + (share or 0.0)) * _BAR_STEPS)))
            left = estimate.left()
            note_left = '%d s' % int(round(left)) if left is not None and left >= 1 else ''
            now = time.monotonic()
            run_left = _run_left_text(done_at - run_started, count, total, now - done_at, left)
            progress.setLabelText(_batch_label(source, count, total, run_left, note_left))
            time.sleep(0.05)
        if not cancelled and failed:
            raise failed[0]
    finally:
        progress.setValue(steps)
        progress.close()
    return cancelled

class _Kept:

    def __init__(self, stop, keep_after_stop=lambda: False) -> None:
        self._stop = stop
        self._keep_after_stop = keep_after_stop
        self._lock = threading.Lock()
        self._items: List[Any] = []

    def add(self, item) -> bool:
        with self._lock:
            if self._stop.is_set() and (not self._keep_after_stop()):
                return False
            self._items.append(item)
            return True

    def __len__(self) -> int:
        return len(self._items)

    def snapshot(self) -> List[Any]:
        with self._lock:
            return list(self._items)

def _refresh_after_batch(mw, browser=None) -> None:
    try:
        ed = getattr(browser, 'editor', None) if browser is not None else None
        if ed is not None and getattr(ed, 'note', None) is not None:
            reload_fn = getattr(ed, 'loadNoteKeepingFocus', None) or getattr(ed, 'loadNote', None)
            if callable(reload_fn):
                reload_fn()
    except Exception:
        pass
    try:
        mw.reset()
    except Exception:
        pass

def _batch_report(chain, msg: str, missed=(), cancelled: bool=False, show=None) -> None:
    if chain is None:
        (show or showInfo)(msg)
        return
    chain.update(msg=msg, missed=list(missed), cancelled=bool(cancelled))

def _run_chain(self, chain: List[str], nids, query_field, target_field, replace) -> None:
    runners = {_PROVIDER_NADESHIKO: lambda ids, out: _run_nadeshiko_batch(self, ids, query_field, target_field, replace, chain=out), _PROVIDER_SUBS: lambda ids, out: _run_subs_batch(self, ids, query_field, replace, chain=out), _PROVIDER_IMMERSIONKIT: lambda ids, out: _run_immersionkit_batch(self, ids, query_field, replace, chain=out)}
    pending = list(nids)
    parts: List[str] = []
    with _batch_undo(self.mw):
        for i, name in enumerate(chain):
            if not pending:
                break
            out: Dict[str, Any] = {'msg': '', 'missed': [], 'cancelled': False}
            runners[name](pending, out)
            head = name if i == 0 else 'Then %s, for %d note%s with no result yet' % (name, len(pending), '' if len(pending) == 1 else 's')
            parts.append(head + '\n' + out['msg'])
            if out['cancelled']:
                break
            pending = out['missed']
    _refresh_after_batch(self.mw, self.browser)
    if pending and (not out['cancelled']) and (len(parts) == len(chain)):
        parts.append('Nothing found in any source: %d' % len(pending))
    _subs_show_summary('\n\n'.join(parts), self)

def _subs_show_summary(msg: str, parent=None) -> None:
    try:
        from aqt.qt import QMessageBox, Qt
        box = QMessageBox(parent or mw)
        box.setWindowTitle(MENU_NAME)
        box.setIcon(QMessageBox.Icon.Information)
        box.setText(msg)
        box.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, True)
        box.raise_()
        box.activateWindow()
        box.exec()
    except Exception:
        showInfo(msg)

def _collect_field_names(self, nids: List[int]) -> List[str]:
    col = self.mw.col
    seen: Dict[str, None] = {}
    for nid in (nids or [])[:1000]:
        try:
            note = col.get_note(nid)
            try:
                for name in list(note.keys()):
                    if isinstance(name, str) and name and (name not in seen):
                        seen[name] = None
            except Exception:
                try:
                    model = note.note_type()
                    for fld in (model or {}).get('flds', []):
                        name = fld.get('name')
                        if isinstance(name, str) and name and (name not in seen):
                            seen[name] = None
                except Exception:
                    pass
        except Exception:
            continue
    return list(seen.keys())

def _refresh_field_dropdowns(self) -> None:
    col = self.mw.col
    if self.mode == 'browser' and self.browser is not None:
        nids = get_selected_note_ids(self.browser)
    else:
        deck_name = self.deck_combo.currentText() if hasattr(self, 'deck_combo') else ''
        nids = get_deck_note_ids(col, deck_name)
    fields = _collect_field_names(self, nids) if nids else []
    if not fields:
        fields = ['Front', 'Back', 'Expression', 'Picture']

    def _setting_first(key: str, preferred: List[str]) -> List[str]:
        chosen = str(self.cfg.get(key, '') or '').strip()
        return [chosen] + preferred if chosen else preferred

    def _pick_default(candidates: List[str], preferred: List[str]) -> int:
        for pref in preferred:
            if pref in candidates:
                return candidates.index(pref)
        return 0
    self.query_field.blockSignals(True)
    self.target_field.blockSignals(True)
    self.nade_image_field.blockSignals(True)
    self.nade_audio_field.blockSignals(True)
    self.nade_sentence_field.blockSignals(True)
    self.nade_translation_field.blockSignals(True)
    self.query_field.clear()
    self.target_field.clear()
    self.nade_image_field.clear()
    self.nade_audio_field.clear()
    self.nade_sentence_field.clear()
    self.nade_translation_field.clear()
    self.query_field.addItems(fields)
    self.target_field.addItems(fields)
    self.nade_image_field.addItem(_NO_FIELD)
    self.nade_image_field.addItems(fields)
    self.nade_audio_field.addItem(_NO_FIELD)
    self.nade_audio_field.addItems(fields)
    self.nade_sentence_field.addItems(fields)
    self.nade_translation_field.addItem(_NO_FIELD)
    self.nade_translation_field.addItems(fields)
    self.query_field.setCurrentIndex(_pick_default(fields, ['Front', 'Expression', 'Word', 'Term']))
    self.target_field.setCurrentIndex(_pick_default(fields, ['Picture', 'Image', 'Images', 'Back']))
    for combo, key, guesses in ((self.nade_image_field, 'nadeshiko_image_field', ['Image1', 'Picture', 'Image', 'Images']), (self.nade_audio_field, 'nadeshiko_audio_field', ['Audio1', 'Audio', 'Sound', '音声'])):
        guess = next((f for f in _setting_first(key, guesses) if f in fields), '')
        combo.setCurrentIndex(combo.findText(guess) if guess else 0)
    self.nade_sentence_field.setCurrentIndex(_pick_default(fields, _setting_first('nadeshiko_sentence_field', ['Sentence1', 'Sentence', 'Text', 'Front', 'Expression'])))
    self.query_field.blockSignals(False)
    self.target_field.blockSignals(False)
    self.nade_image_field.blockSignals(False)
    self.nade_audio_field.blockSignals(False)
    cfg_translation = str(self.cfg.get('nadeshiko_sentence_en_field', '') or '').strip()
    self.nade_translation_field.setCurrentIndex(self.nade_translation_field.findText(cfg_translation) if cfg_translation in fields else 0)
    self.nade_sentence_field.blockSignals(False)
    self.nade_translation_field.blockSignals(False)
    self.subs_sentence_field.blockSignals(True)
    self.subs_source_field.blockSignals(True)
    self.subs_image_field.blockSignals(True)
    self.subs_sentence_field.clear()
    self.subs_source_field.clear()
    self.subs_image_field.clear()
    self.subs_sentence_field.addItems(fields)
    self.subs_source_field.addItem(_NO_FIELD)
    self.subs_source_field.addItems(fields)
    self.subs_image_field.addItem(_NO_FIELD)
    self.subs_image_field.addItems(fields)
    self.subs_sentence_field.setCurrentIndex(_pick_default(fields, _setting_first('subs_sentence_field', ['Sentence1', 'Sentence', 'Text', 'Front', 'Expression'])))
    for combo, key in ((self.subs_source_field, 'subs_source_field'), (self.subs_image_field, 'subs_image_field')):
        chosen = str(self.cfg.get(key, '') or '').strip()
        combo.setCurrentIndex(combo.findText(chosen) if chosen in fields else 0)
    self.subs_sentence_field.blockSignals(False)
    self.subs_source_field.blockSignals(False)
    self.subs_image_field.blockSignals(False)
    ik_guess = {'sentence_field': ['Sentence1', 'Sentence', 'Text', 'Front', 'Expression'], 'image_field': ['Image1', 'Picture', 'Image', 'Images'], 'audio_field': ['Audio1', 'Audio', 'Sound', '音声']}
    for name, combo in self._ik_field_combos():
        combo.blockSignals(True)
        combo.clear()
        combo.addItem(_NO_FIELD)
        combo.addItems(fields)
        chosen = str(self.cfg.get('immersionkit_' + name, '') or '').strip()
        if chosen in fields:
            combo.setCurrentIndex(combo.findText(chosen))
        else:
            guess = [f for f in ik_guess.get(name, []) if f in fields]
            combo.setCurrentIndex(combo.findText(guess[0]) if guess else 0)
        combo.blockSignals(False)

def _toggle_provider_fields(self) -> None:
    name = _selected_provider(self)
    is_subs = name == _PROVIDER_SUBS
    try:
        current = _PROVIDERS.index(name)
        for i in range(self.provider_pages.count()):
            pol = QSizePolicy.Policy.Preferred if i == current else QSizePolicy.Policy.Ignored
            self.provider_pages.widget(i).setSizePolicy(pol, pol)
        self.provider_pages.setCurrentIndex(current)
        if is_subs:
            self._subs_opts.addWidget(self.exact_chk, 2, 0)
        elif name == _PROVIDER_IMMERSIONKIT:
            self._ik_opts.addWidget(self.exact_chk, 1, 0)
        else:
            self._nade_opts.addWidget(self.exact_chk, 1, 0)
        self.exact_chk.show()
        self.fill_context.setVisible(is_subs)
        if not is_subs and self.fill_context.isChecked():
            self.fill_skip.setChecked(True)
        if self.provider_tabs.currentIndex() != current:
            self.provider_tabs.setCurrentIndex(current)
        self.adjustSize()
    except Exception:
        pass

def _fill_mode(self) -> str:
    if self.fill_replace.isChecked():
        return 'replace'
    if self.fill_append.isChecked():
        return 'append'
    if getattr(self, 'fill_context', None) is not None and self.fill_context.isChecked():
        return 'context'
    return 'skip'

def _nothing_to_fill(note, fields, fill_mode: str) -> Optional[str]:
    present = [f for f in fields if f and f in note]
    if not present:
        return 'no_fields'
    if fill_mode == 'skip' and all((str(note[f] or '').strip() for f in present)):
        return 'filled'
    return None

def _skip_lines(filled: int, no_fields: int, deleted: int, unwritten: int) -> str:
    msg = ''
    if filled:
        msg += f'\nSkipped, already filled (no search sent): {filled}'
    if no_fields:
        msg += f'\nSkipped, the note type has none of the fields: {no_fields}'
    if unwritten:
        msg += f'\nFound, but nothing to write: {unwritten}'
    if deleted:
        msg += f'\nDeleted during the run: {deleted}'
    return msg

def _live_note(col, nid):
    try:
        return col.get_note(nid)
    except Exception:
        return None

def _selected_provider(self) -> str:
    try:
        name = str(self.provider_combo.currentText()).strip()
    except Exception:
        return _PROVIDER_NADESHIKO
    return name if name in _PROVIDERS else _PROVIDER_NADESHIKO

def _run_nadeshiko_batch(self, nids, query_field, target_field, replace, chain=None):
    from aqt.utils import showInfo
    import concurrent.futures
    col = self.mw.col
    empty_queries = 0
    nade_no_result = 0
    nade_no_media = 0
    nade_media_errors = 0
    nade_errors = 0
    first_error = ''
    updated = 0
    media = self.mw.col.media
    min_len = int(self.cfg.get('nadeshiko_min_length', 27))
    max_len = int(self.cfg.get('nadeshiko_max_length', 0)) or None
    _exact_search = bool(self.exact_chk.isChecked()) if hasattr(self, 'exact_chk') else False
    _append_mode = bool(self.append_chk.isChecked()) if hasattr(self, 'append_chk') else False
    if hasattr(self, 'nade_cat_anime'):
        self.cfg['nadeshiko_cat_anime'] = self.nade_cat_anime.isChecked()
        self.cfg['nadeshiko_cat_live'] = self.nade_cat_live.isChecked()
        self.cfg['nadeshiko_cat_yt'] = self.nade_cat_yt.isChecked()
        self.cfg['nadeshiko_require_media'] = self.nade_req_media.isChecked()
        self.cfg['nadeshiko_bold'] = self.nade_bold.isChecked()
        self.cfg['nadeshiko_furigana'] = self.nade_furigana.isChecked()
        if hasattr(self, 'nade_bold_tail'):
            self.cfg['nadeshiko_bold_tail'] = self.nade_bold_tail.isChecked()
    key = str(self.cfg.get('nadeshiko_api_key', '')).strip()
    base_url = str(self.cfg.get('nadeshiko_base_url', 'https://api.nadeshiko.co/v1')).strip() or 'https://api.nadeshiko.co/v1'
    img_field = _field_or_blank(self.nade_image_field.currentText()) if hasattr(self, 'nade_image_field') else target_field
    aud_field = _field_or_blank(self.nade_audio_field.currentText()) if hasattr(self, 'nade_audio_field') else target_field
    sent_field = self.nade_sentence_field.currentText().strip() if hasattr(self, 'nade_sentence_field') else query_field
    trans_field = _field_or_blank(self.nade_translation_field.currentText()) if hasattr(self, 'nade_translation_field') else ''
    trans_lang = _nade_translation_lang(self.cfg)
    lang = str(self.cfg.get('nadeshiko_sentence_lang', 'jp')).lower()
    fill_mode = 'replace' if replace else 'append' if _append_mode else 'skip'
    skipped = {'filled': 0, 'no_fields': 0}
    deleted = unwritten = 0
    tasks = []
    for nid in nids:
        note = col.get_note(nid)
        q = get_field_value(note, query_field)
        if not q:
            empty_queries += 1
            continue
        why = _nothing_to_fill(note, (sent_field, img_field, aud_field, trans_field), fill_mode)
        if why:
            skipped[why] += 1
            continue
        held = None
        if _append_mode and sent_field in note and note[sent_field].strip():
            held = {field_plain(strip_context(line)) for line in split_field_lines(note[sent_field])}
        tasks.append((nid, q, q.strip(), held))
    if not tasks:
        if empty_queries:
            self.logger.info(f'Skipped {empty_queries} notes with empty query fields.')
        _batch_report(chain, f'Updated 0 notes.\nSkipped empty: {empty_queries}' + _skip_lines(skipped['filled'], skipped['no_fields'], 0, 0))
        return
    stop = threading.Event()
    fatal_error: List[str] = []
    download_errors = []

    def fetch_nade(task):
        nid, q, q_text, held = task
        if stop.is_set():
            return None
        client = NadeshikoApiClient(key, base_url=base_url, stop=stop)
        try:
            misses: List[str] = []
            segment = _nadeshiko_fetch_segment(client, self.cfg, q_text, _exact_search, min_len, max_len, lang, misses=misses, held=held)
            if not segment:
                return (nid, q, False, 'no_media' if misses else None, None, None, None, None)
            text = _nade_format_sentence(segment, lang, bold=bool(self.cfg.get('nadeshiko_bold', True)), furigana=bool(self.cfg.get('nadeshiko_furigana', False)), query=q_text, tail=bool(self.cfg.get('nadeshiko_bold_tail', True)))
            urls = segment.get('urls') or {}
            img_url = str(urls.get('imageUrl', '') or '').strip()
            aud_url = str(urls.get('audioUrl', '') or '').strip()

            def download(url):
                if not url:
                    return None
                try:
                    return client.download(url)
                except Exception as exc:
                    download_errors.append((nid, str(exc)))
                    return None
            img_bytes = download(img_url)
            aud_bytes = download(aud_url)
            return (nid, q, True, segment, text, img_bytes, aud_bytes, None)
        except NadeshikoCancelled:
            return None
        except NadeshikoApiError as e:
            if e.fatal and (not stop.is_set()):
                fatal_error.append('Nadeshiko is rate-limiting; what was found is kept. Wait a minute and run again.' if e.rate_limited else str(e))
                stop.set()
            return (nid, q, False, None, None, None, None, str(e))
        except Exception as e:
            return (nid, q, False, None, None, None, None, str(e))
    kept = _Kept(stop, keep_after_stop=lambda: bool(fatal_error))

    def work():
        with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
            futures = [executor.submit(fetch_nade, t) for t in tasks]
            for future in concurrent.futures.as_completed(futures):
                res = future.result()
                if res is not None:
                    kept.add(res)
    cancelled = _run_with_progress(self, 'Nadeshiko', len(tasks), work, stop, lambda: len(kept))
    results = kept.snapshot()
    not_searched = len(tasks) - len(results)
    nade_media_errors += len(download_errors)
    found = {r[0] for r in results}
    missed = [] if cancelled else [t[0] for t in tasks if t[0] not in found]
    with _batch_undo(self.mw):
        for res in results:
            nid, q, success, segment, text, img_bytes, aud_bytes, err = res
            if err:
                self.logger.error(f"Nadeshiko batch error for '{q}': {err}")
                nade_errors += 1
                first_error = first_error or err
                missed.append(nid)
                continue
            if not success:
                missed.append(nid)
                if segment == 'no_media':
                    nade_no_media += 1
                else:
                    nade_no_result += 1
                continue
            note = _live_note(col, nid)
            if note is None:
                deleted += 1
                continue
            note_changed = False
            if not img_bytes:
                note_changed = _clear_missing_media(note, img_field, replace) or note_changed
            if not aud_bytes:
                note_changed = _clear_missing_media(note, aud_field, replace) or note_changed
            changed_sentence = False
            if sent_field in note:
                if replace:
                    note[sent_field] = _postprocess_sentence(text)
                    changed_sentence = True
                elif _append_mode and note[sent_field]:
                    note[sent_field] = note[sent_field] + '<br>' + _postprocess_sentence(text)
                    changed_sentence = True
                elif not note[sent_field]:
                    note[sent_field] = _postprocess_sentence(text)
                    changed_sentence = True
            translation = _nade_translation(segment, trans_lang)
            if trans_field and trans_field != sent_field and (trans_field in note) and translation:
                if replace or not note[trans_field]:
                    note[trans_field] = translation
                    changed_sentence = True
                elif _append_mode:
                    note[trans_field] = note[trans_field] + '<br>' + translation
                    changed_sentence = True
            if img_bytes and img_field in note:
                try:
                    tail = 'nade_%s%s' % (nid, _image_extension_from_bytes(img_bytes))
                    media_name_img = media.write_data(ensure_media_filename_safe(tail), img_bytes)
                    if _append_mode and note[img_field] and (not replace):
                        note[img_field] += f'<img src="{media_name_img}">'
                        note_changed = True
                    elif add_image_to_note(note, img_field, media_name_img, replace=replace):
                        note_changed = True
                except Exception as e:
                    nade_media_errors += 1
                    self.logger.error(f"Nadeshiko image save failed for '{q}': {e}")
            if aud_bytes and aud_field in note:
                try:
                    tail = f'nade_{nid}.mp3'
                    media_name_aud = media.write_data(ensure_media_filename_safe(tail), aud_bytes)
                    if _append_mode and note[aud_field] and (not replace):
                        note[aud_field] += f'[sound:{media_name_aud}]'
                        note_changed = True
                    elif add_audio_to_note(note, aud_field, media_name_aud, replace=replace):
                        note_changed = True
                except Exception as e:
                    nade_media_errors += 1
                    self.logger.error(f"Nadeshiko audio save failed for '{q}': {e}")
            if changed_sentence or note_changed:
                _save_note(col, note)
                updated += 1
            else:
                unwritten += 1
    msg = f'Updated {updated} notes.'
    if empty_queries:
        msg += f'\nSkipped empty: {empty_queries}'
    msg += _skip_lines(skipped['filled'], skipped['no_fields'], deleted, unwritten)
    if nade_no_result > 0:
        msg += f'\nNo results: {nade_no_result}'
    if nade_no_media > 0:
        msg += f'\nNo sentence with image and audio (Req. Image & Audio is on): {nade_no_media}'
    if nade_media_errors > 0:
        msg += f'\nMedia download or save errors: {nade_media_errors}'
    if nade_errors > 0:
        msg += f'\nNadeshiko errors: {nade_errors}\n{(fatal_error[0] if fatal_error else first_error)}'
    if not_searched and (cancelled or fatal_error):
        msg += f"\nNot searched ({('cancelled' if cancelled and (not fatal_error) else 'stopped')}): {not_searched}"
    self.logger.info(f'Nadeshiko batch: updated={updated} empty={empty_queries} no_result={nade_no_result} no_media={nade_no_media} media_errors={nade_media_errors} errors={nade_errors}')
    _refresh_after_batch(self.mw, self.browser)
    _batch_report(chain, msg, missed, cancelled)

def _run_subs_batch(self, nids, query_field, replace, chain=None):
    from aqt.utils import showInfo
    col = self.mw.col
    empty_queries = 0
    no_result = 0
    updated = 0
    errors = 0
    try:
        min_len = int(self.cfg.get('subs_min_length', 6))
    except Exception:
        min_len = 6
    try:
        max_len = int(self.cfg.get('subs_max_length', 0)) or None
    except Exception:
        max_len = None
    _exact_search = bool(self.exact_chk.isChecked()) if hasattr(self, 'exact_chk') else False
    _append_mode = bool(self.append_chk.isChecked()) if hasattr(self, 'append_chk') else False
    if hasattr(self, 'subs_cat_subs'):
        self.cfg['subs_cat_subs'] = self.subs_cat_subs.isChecked()
        self.cfg['subs_cat_epub'] = self.subs_cat_epub.isChecked()
        self.cfg['subs_cat_manga'] = self.subs_cat_manga.isChecked()
    if hasattr(self, 'subs_strip_names'):
        self.cfg['subs_strip_names'] = self.subs_strip_names.isChecked()
    if hasattr(self, 'subs_multi'):
        self.cfg['subs_multi_count'] = int(self.subs_multi.value())
    multi_count = _subs_multi_count(self.cfg)
    furigana = bool(self.subs_furigana.isChecked()) if hasattr(self, 'subs_furigana') else True
    bold = bool(self.subs_bold.isChecked()) if hasattr(self, 'subs_bold') else True
    sent_field = self.subs_sentence_field.currentText().strip()
    source_field = self.subs_source_field.currentText().strip()
    if source_field == _NO_FIELD:
        source_field = ''
    image_field = _field_or_blank(self.subs_image_field.currentText()) if hasattr(self, 'subs_image_field') else ''
    images_added = 0
    images_missing = 0
    fill_mode = _fill_mode(self)
    add_context = fill_mode == 'context' or (bool(self.subs_context_chk.isChecked()) if hasattr(self, 'subs_context_chk') else False)
    strip_names = _subs_strip_names(self.cfg)
    ctx_before = int(self.subs_context_before.value()) if hasattr(self, 'subs_context_before') else 2
    ctx_after = int(self.subs_context_after.value()) if hasattr(self, 'subs_context_after') else 1
    no_repeats = bool(self.subs_no_repeats.isChecked()) if hasattr(self, 'subs_no_repeats') else False
    used = set() if no_repeats else None
    repeats = 0
    context_missing = 0
    context_outside_window = 0
    search_diagnostics = []
    context_added = 0
    not_found = 0
    ambiguous = 0
    nothing_new = set()
    skipped = {'filled': 0, 'no_fields': 0}
    deleted = unwritten = 0
    tasks = []
    for nid in nids:
        note = col.get_note(nid)
        if fill_mode == 'context' and sent_field in note and note[sent_field].strip():
            hints = split_field_lines(note[source_field]) if source_field and source_field in note else []
            tasks.append((nid, '', '', (split_field_lines(note[sent_field]), hints), None))
            continue
        q = get_field_value(note, query_field)
        if not q:
            empty_queries += 1
            continue
        why = _nothing_to_fill(note, (sent_field, source_field, image_field), fill_mode)
        if why:
            skipped[why] += 1
            continue
        held = None
        if fill_mode == 'append' and sent_field in note and note[sent_field].strip():
            held = held_sentences(note[sent_field])
        tasks.append((nid, q, q.strip(), None, held))
    if not tasks:
        if empty_queries:
            self.logger.info(f'Skipped {empty_queries} notes with empty query fields.')
        _batch_report(chain, f'Updated 0 notes.\nSkipped empty: {empty_queries}' + _skip_lines(skipped['filled'], skipped['no_fields'], 0, 0))
        return
    client = _subs_make_client(self.cfg, self.logger)
    stop = threading.Event()
    start_error: List[str] = []
    try:

        def wrap_existing(existing):
            nonlocal context_missing, context_outside_window, context_added, not_found, ambiguous
            lines, hints = existing
            out = []
            for i, line in enumerate(lines):
                bare = strip_context(line)
                if not bare.strip():
                    out.append(line)
                    continue
                row, amb, got = sentence_window(client, bare, hints[i] if i < len(hints) else '', ctx_before, ctx_after)
                if row is None:
                    not_found += 1
                    out.append(line)
                    continue
                ambiguous += int(amb)
                if got is None:
                    if getattr(client, 'context_missing_reason', '') == 'outside_window':
                        context_outside_window += 1
                    else:
                        context_missing += 1
                    out.append(line)
                    continue
                tagged_bare = tag_sentence(untag_sentence(bare), row['rowid'], row.get('media_type', 'subs'))
                out.append(context_wrap(tagged_bare, got[0], got[1], furigana=furigana, strip_names=strip_names and row.get('media_type', 'subs') == 'subs', media=row.get('media_type', 'subs')))
                context_added += 1
            return '<br>'.join(out)

        def fetch_subs(task):
            nonlocal repeats, context_missing, context_outside_window, context_added
            nid, q, q_text, existing, held = task
            try:
                if existing is not None:
                    return (nid, q, None, None, wrap_existing(existing))
                held_hits = []
                rows = _subs_fetch_rows(client, self.cfg, q_text, _exact_search, min_len, max_len, count=multi_count, used=used, held=held, held_hits=held_hits, diagnostics=search_diagnostics)
                if not rows and held_hits:
                    nothing_new.add(nid)
                if used is not None:
                    keys = [(r.get('media_type'), r.get('rowid')) for r in rows]
                    repeats += sum((1 for k in keys if k in used))
                    used.update(keys)
                if add_context:
                    for row in rows:
                        wrapped = _subs_context_html(client, row, ctx_before, ctx_after, furigana, bold, strip_names)
                        if wrapped is None:
                            if getattr(client, 'context_missing_reason', '') == 'outside_window':
                                context_outside_window += 1
                            else:
                                context_missing += 1
                            continue
                        row['_with_context'] = wrapped
                        context_added += 1
                if image_field:
                    for row in rows:
                        if _subs_is_manga(row):
                            row['_page_image'] = client.page_image(int(row.get('rowid')))
                return (nid, q, rows, None, None)
            except Exception as exc:
                return (nid, q, None, str(exc), None)
        kept = _Kept(stop)

        def work():
            try:
                client.ensure_running(autostart=bool(self.cfg.get('subs_autostart', True)))
            except AobanaError as exc:
                start_error.append(str(exc))
                return
            for task in tasks:
                if stop.is_set():
                    return
                if not kept.add(fetch_subs(task)):
                    return
        cancelled = _run_with_progress(self, 'Aobana', len(tasks), work, stop, lambda: len(kept), cancel=client.cancel, poll=client.progress)
        results = kept.snapshot()
        if start_error:
            if chain is None:
                showWarning(start_error[0])
            _batch_report(chain, start_error[0], [t[0] for t in tasks], show=lambda _text: None)
            return
        not_searched = len(tasks) - len(results)
        missed = []
        with _batch_undo(self.mw):
            for nid, q, rows, err, rewritten in results:
                if err:
                    self.logger.error(f"Aobana error for '{q}': {err}")
                    errors += 1
                    missed.append(nid)
                    continue
                if rewritten is not None:
                    note = _live_note(col, nid)
                    if note is None:
                        deleted += 1
                    elif rewritten != note[sent_field]:
                        note[sent_field] = rewritten
                        _save_note(col, note)
                        updated += 1
                    continue
                if not rows:
                    if nid not in nothing_new:
                        no_result += 1
                        missed.append(nid)
                    continue
                text, title = _subs_rendered(rows, furigana, bold)
                if not text:
                    no_result += 1
                    missed.append(nid)
                    continue
                note = _live_note(col, nid)
                if note is None:
                    deleted += 1
                    continue
                changed = False
                if sent_field and sent_field in note:
                    if replace:
                        note[sent_field] = text
                        changed = True
                    elif _append_mode and note[sent_field]:
                        note[sent_field] = note[sent_field] + '<br>' + text
                        changed = True
                    elif not note[sent_field]:
                        note[sent_field] = text
                        changed = True
                if source_field and title and (source_field in note):
                    if replace:
                        note[source_field] = title
                        changed = True
                    elif _append_mode and note[source_field]:
                        note[source_field] = note[source_field] + '<br>' + title
                        changed = True
                    elif not note[source_field]:
                        note[source_field] = title
                        changed = True
                manga = [row for row in rows if _subs_is_manga(row)]
                if not any((row.get('_page_image') for row in manga)):
                    changed = _clear_missing_media(note, image_field, replace) or changed
                if image_field and image_field in note and manga:
                    found = [row for row in manga if row.get('_page_image')]
                    images_missing += len(manga) - len(found)
                    if _subs_write_page_images(col, note, image_field, [r['_page_image'] for r in found], [r.get('rowid') for r in found], replace=replace, append=_append_mode):
                        images_added += len(found)
                        changed = True
                if changed:
                    _save_note(col, note)
                    updated += 1
                else:
                    unwritten += 1
        msg = f'Updated {updated} notes.'
        if empty_queries:
            msg += f'\nSkipped empty: {empty_queries}'
        msg += _skip_lines(skipped['filled'], skipped['no_fields'], deleted, unwritten)
        if no_result:
            msg += f'\nNo results: {no_result}'
        if nothing_new:
            msg += f'\nNothing new (every match is already on the note): {len(nothing_new)}'
        if repeats:
            msg += f'\nRepeated sentences (pool ran out): {repeats}'
        if context_added:
            msg += f'\nContext added: {context_added}'
        if images_added:
            msg += f'\nManga page images added: {images_added}'
        if images_missing:
            msg += f'\nManga page image missing (the page has none): {images_missing}'
        if not_found:
            msg += f'\nSentence not found in Aobana (left as it was): {not_found}'
        if ambiguous:
            msg += f'\nSame sentence in several episodes, first one used: {ambiguous}'
        if context_outside_window:
            msg += f'\nNo context (sentence outside the returned window): {context_outside_window}'
        if context_missing:
            msg += f'\nNo context (Aobana too old for it): {context_missing}'
        if search_diagnostics and (not errors) and (not cancelled) and all((not d['has_results'] for d in search_diagnostics)):
            selected_media = [kind for kind, key in (('subs', 'subs_cat_subs'), ('books', 'subs_cat_epub'), ('manga', 'subs_cat_manga')) if self.cfg.get(key, True)]
            try:
                msg += '\n' + client.empty_search_reason(search_diagnostics, selected_media or ['subs', 'books', 'manga'])
            except AobanaError as exc:
                msg += '\n' + str(exc)
        if errors:
            msg += f'\nErrors: {errors}'
        if cancelled and not_searched:
            msg += f'\nNot searched (cancelled): {not_searched}'
        self.logger.info(f'Subs batch: updated={updated} empty={empty_queries} no_result={no_result} errors={errors} field={sent_field!r} source={source_field!r} replace={replace}')
        _refresh_after_batch(self.mw, self.browser)
        _batch_report(chain, msg, missed, cancelled, show=lambda text: _subs_show_summary(text, self))
    finally:
        client.shutdown()

def _ik_int(cfg: Dict[str, Any], key: str, default: int) -> int:
    try:
        return int(cfg.get(key, default))
    except Exception:
        return default

def _ik_context_span(cfg: Dict[str, Any], key: str, default: int) -> int:
    return max(0, min(_ik_int(cfg, key, default), ik.CONTEXT_CAP))

def _ik_selection_mode(cfg: Dict[str, Any], key: str) -> str:
    mode = _nadeshiko_selection_mode(cfg, key, 'random')
    return 'random' if mode == 'corpus_random' else mode

def _ik_categories(cfg: Dict[str, Any]) -> List[str]:
    cats = [c for c in ik.CATEGORIES if cfg.get('immersionkit_cat_' + c, True)]
    return cats or list(ik.CATEGORIES)

def _ik_pick(examples: List[Dict[str, Any]], mode: str, lo: int, hi: Optional[int]) -> Optional[Dict[str, Any]]:
    pool = [e for e in examples if ik.length(e) >= lo and (not hi or ik.length(e) <= hi)]
    if not pool:
        return None
    if mode == 'random':
        return random.choice(pool)
    if mode == 'none':
        return pool[0]
    ranked = sorted(pool, key=ik.length)
    if mode == 'smallest':
        return ranked[0]
    if mode == 'median':
        return ranked[len(ranked) // 2]
    return ranked[-1]

def _ik_fetch_example(client: ImmersionKitClient, cfg: Dict[str, Any], query: str, exact: bool, held: Optional[set]=None, misses: Optional[List[str]]=None) -> Optional[Dict[str, Any]]:
    cats = _ik_categories(cfg)
    mode = _ik_selection_mode(cfg, 'immersionkit_sentence_selection')
    examples = client.search(query, exact=exact, category=cats[0] if len(cats) == 1 else None, per_title=max(5, min(_ik_int(cfg, 'immersionkit_per_title', 5), 50)), shortest=mode == 'smallest')
    if len(cats) == 2:
        examples = [e for e in examples if ik.category_of(e) in cats]
    strip = bool(cfg.get('immersionkit_strip_names', False))
    if strip:
        examples = [e for e in examples if not ik.match_only_in_tag(e, query)]
    if held:
        examples = [e for e in examples if ik.plain(ik.render(e, False, False, strip)) not in held]
    if cfg.get('immersionkit_require_image', False):
        with_image = [e for e in examples if str(e.get('image', '') or '').strip()]
        if examples and (not with_image) and (misses is not None):
            misses.append('no_image')
        examples = with_image
    lo = max(0, _ik_int(cfg, 'immersionkit_min_length', 6))
    hi = _ik_int(cfg, 'immersionkit_max_length', 50) or None
    picked = _ik_pick(examples, mode, lo, hi)
    if picked or not cfg.get('immersionkit_fallback_enabled', True):
        return picked
    return _ik_pick(examples, _ik_selection_mode(cfg, 'immersionkit_fallback_selection'), max(0, _ik_int(cfg, 'immersionkit_fallback_min_length', 2)), _ik_int(cfg, 'immersionkit_fallback_max_length', 200) or None)

def _ik_context(client: ImmersionKitClient, example: Dict[str, Any], cfg: Dict[str, Any]) -> tuple:
    if not cfg.get('immersionkit_context', False):
        return ([], [])
    n_before = _ik_context_span(cfg, 'immersionkit_context_before', 2)
    n_after = _ik_context_span(cfg, 'immersionkit_context_after', 1)
    if not n_before and (not n_after):
        return ([], [])
    before, after = client.context(str(example.get('id', '')))
    return (before[-n_before:] if n_before else [], after[:n_after])

def _ik_media(client: ImmersionKitClient, example: Dict[str, Any], context: tuple, kind: str, cfg: Optional[Dict[str, Any]]=None) -> List[tuple]:
    if cfg is not None and (not cfg.get('immersionkit_context_images' if kind == 'image' else 'immersionkit_context_audio', True)):
        context = ([], [])
    before, after = context
    lines = [(item, ik.media_url(example, item, kind)) for item in list(before) + [example] + list(after)]
    lines = [(item, url) for item, url in lines if url]
    if not lines:
        return []
    if len(lines) == 1:
        return [(lines[0][0], client.download(lines[0][1]))]
    import concurrent.futures
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(6, len(lines))) as pool:
        data = list(pool.map(lambda line: client.download(line[1]), lines))
    return [(item, got) for (item, _url), got in zip(lines, data)]

def _ik_sentence_html(client: ImmersionKitClient, example: Dict[str, Any], cfg: Dict[str, Any], context: Optional[tuple]=None, query: str='') -> str:
    furigana = bool(cfg.get('immersionkit_furigana', False))
    strip = bool(cfg.get('immersionkit_strip_names', False))

    def one(e, bold):
        return ik.render(e, furigana=furigana, bold=bold, strip_names=strip, query=query)
    sentence = one(example, bool(cfg.get('immersionkit_bold', True)))
    before, after = context if context is not None else _ik_context(client, example, cfg)
    if not before and (not after):
        return sentence
    before = [one(e, False) for e in before]
    after = [one(e, False) for e in after]
    return context_wrap(sentence, before + [sentence] + after, len(before), furigana=True)

def _run_immersionkit_batch(self, nids, query_field, replace, chain=None):
    col = self.mw.col
    media = col.media
    cfg = self.cfg
    for key, attr in (('cat_anime', 'ik_cat_anime'), ('cat_drama', 'ik_cat_drama'), ('cat_games', 'ik_cat_games'), ('require_image', 'ik_req_image'), ('furigana', 'ik_furigana'), ('bold', 'ik_bold'), ('strip_names', 'ik_strip_names'), ('context', 'ik_context_chk'), ('context_images', 'ik_ctx_images'), ('context_audio', 'ik_ctx_audio')):
        cfg['immersionkit_' + key] = bool(getattr(self, attr).isChecked())
    cfg['immersionkit_context_before'] = int(self.ik_context_before.value())
    cfg['immersionkit_context_after'] = int(self.ik_context_after.value())
    exact = bool(self.exact_chk.isChecked())
    append = bool(self.append_chk.isChecked())
    fields = {name: _field_or_blank(combo.currentText()) for name, combo in self._ik_field_combos()}
    sent_field = fields['sentence_field']
    empty_queries = 0
    fill_mode = 'replace' if replace else 'append' if append else 'skip'
    skipped = {'filled': 0, 'no_fields': 0}
    deleted = unwritten = 0
    tasks = []
    for nid in nids:
        note = col.get_note(nid)
        q = get_field_value(note, query_field)
        if not q:
            empty_queries += 1
            continue
        why = _nothing_to_fill(note, list(fields.values()), fill_mode)
        if why:
            skipped[why] += 1
            continue
        held = None
        if append and sent_field and (sent_field in note) and note[sent_field].strip():
            held = {field_plain(strip_context(line)) for line in split_field_lines(note[sent_field])}
        tasks.append((nid, q.strip(), held))
    if not tasks:
        _batch_report(chain, f'Updated 0 notes.\nSkipped empty: {empty_queries}' + _skip_lines(skipped['filled'], skipped['no_fields'], 0, 0))
        return
    stop = threading.Event()
    client = ImmersionKitClient(sleep=stop.wait)
    kept = _Kept(stop)
    fatal: List[str] = []

    def keep(result) -> None:
        kept.add(result)

    def work():
        for nid, q, held in tasks:
            if stop.is_set():
                return
            misses: List[str] = []
            try:
                example = _ik_fetch_example(client, cfg, q, exact, held=held, misses=misses)
                if example is None:
                    keep((nid, q, None, 'no_image' if misses else '', None))
                    continue
                wants = sent_field or fields['image_field'] or fields['audio_field']
                context = _ik_context(client, example, cfg) if wants else ([], [])
                got = {'sentence': _ik_sentence_html(client, example, cfg, context, q) if sent_field else ''}
                if fields['source_field']:
                    got['source'] = client.title_of(str(example.get('title', '') or ''))
                for kind, key in (('image', 'image_field'), ('sound', 'audio_field')):
                    if fields[key] and (not stop.is_set()):
                        got[kind] = _ik_media(client, example, context, kind, cfg)
                keep((nid, q, example, '', got))
            except ImmersionKitError as exc:
                if exc.fatal:
                    fatal.append(str(exc))
                    stop.set()
                    return
                if stop.is_set():
                    return
                keep((nid, q, None, 'error:' + str(exc), None))
            except Exception as exc:
                keep((nid, q, None, 'error:' + str(exc), None))
    cancelled = _run_with_progress(self, 'Immersion Kit', len(tasks), work, stop, lambda: len(kept))
    results = kept.snapshot()
    updated = no_result = no_image = media_errors = context_media_errors = errors = 0
    first_error = ''

    def write(note, field, value, sep='<br>') -> bool:
        if not field or field not in note or (not value):
            return False
        if replace or not note[field]:
            note[field] = value
        elif append:
            note[field] = note[field] + sep + value
        else:
            return False
        return True
    found = {r[0] for r in results}
    missed = [] if cancelled else [t[0] for t in tasks if t[0] not in found]
    with _batch_undo(self.mw):
        for nid, q, example, why, got in results:
            if example is None:
                missed.append(nid)
                if why == 'no_image':
                    no_image += 1
                elif why.startswith('error:'):
                    errors += 1
                    first_error = first_error or why[6:]
                    self.logger.error(f"Immersion Kit error for '{q}': {why[6:]}")
                else:
                    no_result += 1
                continue
            note = _live_note(col, nid)
            if note is None:
                deleted += 1
                continue
            changed = write(note, sent_field, got.get('sentence', ''))
            changed = write(note, fields['translation_field'], str(example.get('translation', '') or '').strip()) or changed
            changed = write(note, fields['source_field'], got.get('source', '')) or changed
            for kind, key, tag in (('image', 'image_field', '<img src="%s">'), ('sound', 'audio_field', '[sound:%s]')):
                field = fields[key]
                names = []
                for item, data in got.get(kind) or []:
                    if data is None:
                        if item is example:
                            media_errors += 1
                        else:
                            context_media_errors += 1
                        continue
                    if not field or field not in note:
                        continue
                    try:
                        names.append(media.write_data(ensure_media_filename_safe(ik.media_name(item, kind)), data))
                    except Exception as exc:
                        media_errors += 1
                        self.logger.error(f"Immersion Kit media save failed for '{q}': {exc}")
                if not names:
                    changed = _clear_missing_media(note, field, replace) or changed
                if names:
                    changed = write(note, field, ''.join((tag % name for name in names)), sep='') or changed
            if changed:
                _save_note(col, note)
                updated += 1
            else:
                unwritten += 1
    not_searched = len(tasks) - len(results)
    msg = f'Updated {updated} notes.'
    if empty_queries:
        msg += f'\nSkipped empty: {empty_queries}'
    msg += _skip_lines(skipped['filled'], skipped['no_fields'], deleted, unwritten)
    if no_result:
        msg += f'\nNo results: {no_result}'
    if no_image:
        msg += f'\nNo sentence with an image (Req. Image is on): {no_image}'
    if media_errors:
        msg += f'\nMedia download or save errors: {media_errors}'
    if context_media_errors:
        msg += f"\nContext lines' media not downloaded: {context_media_errors}"
    if errors:
        msg += f'\nImmersion Kit errors: {errors}\n{first_error}'
    if fatal:
        msg += f'\n{fatal[0]}'
    if not_searched and (cancelled or fatal):
        msg += f"\nNot searched ({('cancelled' if cancelled and (not fatal) else 'stopped')}): {not_searched}"
    self.logger.info(f'Immersion Kit batch: updated={updated} empty={empty_queries} no_result={no_result} no_image={no_image} media_errors={media_errors} errors={errors} not_searched={not_searched}')
    _refresh_after_batch(self.mw, self.browser)
    _batch_report(chain, msg, missed, cancelled)

def _write_field_on_query(self, provider: str, query_field: str) -> str:
    if provider == _PROVIDER_SUBS:
        combos = [('Sentence', self.subs_sentence_field), ('Source', self.subs_source_field), ('Image', self.subs_image_field)]
    elif provider == _PROVIDER_IMMERSIONKIT:
        combos = [(name.replace('_field', '').capitalize(), combo) for name, combo in self._ik_field_combos()]
    else:
        combos = [('Sentence', self.nade_sentence_field), ('Image', self.nade_image_field), ('Audio', self.nade_audio_field), ('Translation', self.nade_translation_field)]
    for label, combo in combos:
        if _field_or_blank(combo.currentText()) == query_field:
            return label
    return ''

def _on_run(self) -> None:
    query_field = self.query_field.currentText().strip() if hasattr(self.query_field, 'currentText') else str(self.query_field.text()).strip()
    target_field = self.target_field.currentText().strip() if hasattr(self.target_field, 'currentText') else str(self.target_field.text()).strip()
    replace = bool(self.replace_chk.isChecked())
    provider = _selected_provider(self)
    chain = [provider] + (self._chain_after(provider) if hasattr(self, '_chain_after') else [])
    if not query_field:
        showWarning('Please specify a Query Field.')
        return
    for name in chain:
        where = '' if name == provider else f' ({name} runs if nothing is found; its fields are on its tab.)'
        if name == _PROVIDER_NADESHIKO:
            key_check = str(self.cfg.get('nadeshiko_api_key', '')).strip()
            if not key_check:
                showWarning(_NADE_NO_KEY + where)
                return
        elif name == _PROVIDER_IMMERSIONKIT:
            if not any((_field_or_blank(combo.currentText()) for _n, combo in self._ik_field_combos())):
                showWarning('Choose at least one field to write to.' + where)
                return
        elif not self.subs_sentence_field.currentText().strip():
            showWarning('Please specify a Sentence Field.' + where)
            return
        clash = _write_field_on_query(self, name, query_field)
        if clash:
            showWarning(f'The {clash} field is the same as the Query field ({query_field}). Choose another field, or (none).{where}')
            return
    col = self.mw.col
    if self.mode == 'browser' and self.browser is not None:
        nids = get_selected_note_ids(self.browser)
        if not nids:
            showWarning('No notes selected. Please select notes in the Browser and try again.')
            return
    else:
        deck_name = self.deck_combo.currentText() if hasattr(self, 'deck_combo') else ''
        nids = get_deck_note_ids(col, deck_name)
        self.logger.info(f"Searching deck: '{deck_name}' -> found {len(nids)} note ids")
    if not nids:
        showInfo('No notes found to update.')
        return
    if _busy_refuse():
        return
    for name in chain[1:]:
        _remember_run_settings(self, name, query_field, shared=False)
    _remember_run_settings(self, provider, query_field)

    def _dispatch():
        if _busy_refuse():
            return
        _busy_begin(provider, hotkey=False)
        try:
            if len(chain) > 1:
                _run_chain(self, chain, nids, query_field, target_field, replace)
            elif provider == _PROVIDER_SUBS:
                _run_subs_batch(self, nids, query_field, replace)
            elif provider == _PROVIDER_IMMERSIONKIT:
                _run_immersionkit_batch(self, nids, query_field, replace)
            else:
                _run_nadeshiko_batch(self, nids, query_field, target_field, replace)
        finally:
            _busy_end()
    ed = getattr(self.browser, 'editor', None) if self.browser is not None else None
    save_first = getattr(ed, 'call_after_note_saved', None) if ed is not None else None
    if callable(save_first):
        save_first(_dispatch)
    else:
        _dispatch()

def _remember_run_settings(self, provider: str, query_field: str, fields: bool=True, shared: bool=True) -> None:
    try:
        data = _read_last_settings() or {}
        data['last_provider'] = provider
        fill = _fill_mode(self)
        if provider == _PROVIDER_SUBS:
            source = self.subs_source_field.currentText().strip()
            block = {'query_field': query_field, 'sentence_field': self.subs_sentence_field.currentText().strip(), 'source_field': '' if source == _NO_FIELD else source, 'image_field': _field_or_blank(self.subs_image_field.currentText())}
            toggles, spins = (BackfillImagesDialog._SUBS_TOGGLES, BackfillImagesDialog._SUBS_SPINS)
            key = 'subs'
        elif provider == _PROVIDER_IMMERSIONKIT:
            block = {'query_field': query_field}
            for name, combo in self._ik_field_combos():
                block[name] = _field_or_blank(combo.currentText())
            toggles, spins = (BackfillImagesDialog._IK_TOGGLES, BackfillImagesDialog._IK_SPINS)
            key = 'immersionkit'
        else:
            block = {'query_field': query_field, 'image_field': _field_or_blank(self.nade_image_field.currentText()), 'audio_field': _field_or_blank(self.nade_audio_field.currentText()), 'sentence_field': self.nade_sentence_field.currentText().strip(), 'translation_field': _field_or_blank(self.nade_translation_field.currentText())}
            toggles, spins = (BackfillImagesDialog._NADE_TOGGLES, [])
            key = 'nadeshiko'
        for name, attr in toggles:
            block[name] = bool(getattr(self, attr).isChecked())
        for name, attr in spins:
            block[name] = int(getattr(self, attr).value())
        if key == 'subs' and hasattr(self, '_subs_context_choice'):
            block['context'] = self._subs_context_choice()
        block['exact'] = bool(self.exact_chk.isChecked())
        block['if_filled'] = fill
        saved_block = data.get(key) if isinstance(data.get(key), dict) else {}
        if not shared:
            for name in ('exact', 'if_filled'):
                if name in saved_block:
                    block[name] = saved_block[name]
                else:
                    block.pop(name, None)
        chain = getattr(self, '_chains', {}).get(provider)
        if chain:
            block['chain_order'] = list(chain['order'])
            block['chain_on'] = self._chain_after(provider)
        if not fields:
            saved = data.get(key) if isinstance(data.get(key), dict) else {}
            kept = {name: value for name, value in saved.items() if name == 'query_field' or name.endswith('_field')}
            block = {name: value for name, value in block.items() if not (name == 'query_field' or name.endswith('_field'))}
            block.update(kept)
        data[key] = block
        _write_last_settings(data)
    except Exception:
        pass

def _off_the_word(query_field: str, *write_fields: str) -> List[str]:
    taken = {query_field}
    out = []
    for name in write_fields:
        out.append('' if name in taken else name)
        if name:
            taken.add(name)
    return out

def _hotkey_options(cfg: Dict[str, Any], block: Dict[str, Any], prefix: str, toggles, spins=()) -> None:
    for key in toggles:
        if isinstance(block.get(key), bool):
            cfg[prefix + key] = block[key]
    for key in spins:
        if isinstance(block.get(key), int) and (not isinstance(block.get(key), bool)):
            cfg[prefix + key] = block[key]

def _hotkey_notes(notes) -> str:
    return ''.join(('\n(%s)' % text for _name, text in notes))

def _hotkey_none(mw_, name: str, then, tried, text: str, notes=(), failed: bool=False) -> None:
    if then is None:
        then = _chain_saved(name)[1]
    tried = tuple(tried) + (name,)
    notes = tuple(notes) + (((name, text),) if failed else ())
    if then:
        _QUICK_ADD[then[0]](mw_, then=list(then[1:]), tried=tried, notes=notes)
        return
    failed_names = {n for n, _t in notes}
    empty = [n for n in tried[:-1] if n not in failed_names]
    if empty:
        text += '\n(Nothing in %s either.)' % ', '.join(empty)
    text += _hotkey_notes(notes[:-1] if failed else notes)
    (showWarning if failed else showInfo)(text)

def _hotkey_refuse(mw_, name: str, then, tried, notes, text: str) -> None:
    if tried:
        _hotkey_none(mw_, name, then, tried, text, notes, failed=True)
    else:
        showWarning(text)

def quick_add_nadeshiko_for_current_card(mw, then=None, tried=(), notes=()) -> None:
    if _busy_refuse():
        return
    try:
        if getattr(mw, 'state', '') != 'review' or not getattr(getattr(mw, 'reviewer', None), 'card', None):
            showWarning('No active card to update.')
            return
        col = mw.col
        card = mw.reviewer.card
        note = col.get_note(card.nid)
        cfg = _read_config()
        last = _read_last_settings() or {}

        def _field_names(n) -> List[str]:
            try:
                return list(n.keys())
            except Exception:
                return []
        fields = _field_names(note)
        last_nade = last.get('nadeshiko', {}) if isinstance(last.get('nadeshiko'), dict) else {}
        query_field = str(last_nade.get('query_field') or ('Expression' if 'Expression' in fields else 'Front' if 'Front' in fields else fields[0] if fields else '')).strip()

        def media_field(name: str, guesses: List[str]) -> str:
            chosen = str((last_nade.get(name) if name in last_nade else cfg.get('nadeshiko_' + name)) or '').strip()
            if chosen:
                return chosen if chosen in fields else ''
            if name in last_nade:
                return ''
            return next((g for g in guesses if g in fields), '')
        image_field = media_field('image_field', ['Image1', 'Picture', 'Image', 'Images'])
        audio_field = media_field('audio_field', ['Audio1', 'Audio', 'Sound', '音声'])
        sentence_field = str(last_nade.get('sentence_field') or cfg.get('nadeshiko_sentence_field') or '').strip()
        if not sentence_field:
            for cand in ['Sentence1', 'Sentence', 'Text']:
                if cand in fields:
                    sentence_field = cand
                    break
        translation_field = str((last_nade.get('translation_field') if 'translation_field' in last_nade else cfg.get('nadeshiko_sentence_en_field')) or '').strip()
        if not sentence_field or sentence_field not in fields or sentence_field == query_field:
            _hotkey_refuse(mw, _PROVIDER_NADESHIKO, then, tried, notes, 'Run the batch once, or set the sentence field in Settings.')
            return
        _sentence, image_field, audio_field, translation_field = _off_the_word(query_field, sentence_field, image_field, audio_field, translation_field)
        if not query_field:
            _hotkey_refuse(mw, _PROVIDER_NADESHIKO, then, tried, notes, 'Could not determine fields to update.')
            return
        q_text = get_field_value(note, query_field).strip()
        if not q_text:
            showInfo(f"Query field '{query_field}' is empty; nothing to do.")
            return
        key = str(cfg.get('nadeshiko_api_key', '')).strip()
        if not key:
            _hotkey_refuse(mw, _PROVIDER_NADESHIKO, then, tried, notes, _NADE_NO_KEY)
            return
        base_url = str(cfg.get('nadeshiko_base_url', 'https://api.nadeshiko.co/v1')).strip() or 'https://api.nadeshiko.co/v1'
        stop = threading.Event()
        client = NadeshikoApiClient(key, base_url=base_url, stop=stop)
        query_text = q_text
        min_len = int(cfg.get('nadeshiko_min_length', 27))
        max_len = int(cfg.get('nadeshiko_max_length', 0)) or None
        lang = str(cfg.get('nadeshiko_sentence_lang', 'jp')).lower()
        _hotkey_options(cfg, last_nade, 'nadeshiko_', [key for key, _attr in BackfillImagesDialog._NADE_TOGGLES])
        exact = bool(last_nade.get('exact', False))
        nid = card.nid
    except Exception as e:
        showWarning(f'Failed to add Nadeshiko media: {e}')
        return

    def fetch():
        misses: List[str] = []
        segment = _nadeshiko_fetch_segment(client, cfg, query_text, exact, min_len, max_len, lang, misses=misses)
        if not segment:
            return (None, misses, None, None, '')
        urls = segment.get('urls') or {}
        img_url = str(urls.get('imageUrl', '') or '').strip()
        audio_url = str(urls.get('audioUrl', '') or '').strip()
        img_bytes = client.download(img_url) if img_url else None
        aud_bytes = client.download(audio_url) if audio_url else None
        return (segment, misses, img_bytes, aud_bytes, audio_url)

    def apply(result):
        try:
            segment, misses, img_bytes, aud_bytes, audio_url = result
            if not segment:
                _hotkey_none(mw, _PROVIDER_NADESHIKO, then, tried, 'No Nadeshiko sentence with image and audio (Req. Image & Audio is on).' if misses else 'No Nadeshiko results found.', notes)
                return
            col, note = _hotkey_note(mw, nid)
            if note is None:
                return
            updated = False
            text = _nade_format_sentence(segment, lang, bold=bool(cfg.get('nadeshiko_bold', True)), furigana=bool(cfg.get('nadeshiko_furigana', False)), query=query_text, tail=bool(cfg.get('nadeshiko_bold_tail', True)))
            if sentence_field and sentence_field in note:
                note[sentence_field] = _postprocess_sentence(text)
                updated = True
            translation = _nade_translation(segment, _nade_translation_lang(cfg))
            if translation_field and translation_field != sentence_field and (translation_field in note) and translation:
                note[translation_field] = translation
                updated = True
            media = col.media
            if not img_bytes:
                updated = _clear_missing_media(note, image_field, True) or updated
            if not aud_bytes:
                updated = _clear_missing_media(note, audio_field, True) or updated
            if img_bytes and image_field:
                img_tail = 'nade_%s%s' % (nid, _image_extension_from_bytes(img_bytes))
                img_media_name = media.write_data(ensure_media_filename_safe(img_tail), img_bytes)
                if add_image_to_note(note, image_field, img_media_name, replace=True):
                    updated = True
            if aud_bytes and audio_field:
                aud_tail = audio_url.split('/')[-1].split('?')[0] or 'nadeshiko.mp3'
                aud_media_name = media.write_data(ensure_media_filename_safe(aud_tail), aud_bytes)
                if add_audio_to_note(note, audio_field, aud_media_name, replace=True):
                    updated = True
            if updated:
                _hotkey_save(mw, col, note)
                mw.reset()
                showInfo('Nadeshiko media (and sentence) added to current card.' + _hotkey_notes(notes))
            else:
                showInfo('Nothing was updated.' + _hotkey_notes(notes))
        except Exception as e:
            showWarning(f'Failed to add Nadeshiko media: {e}')
    _hotkey_search('Nadeshiko', fetch, apply, cancel=stop.set, on_error=lambda text: _hotkey_none(mw, _PROVIDER_NADESHIKO, then, tried, text, notes, failed=True))

def quick_add_subs_for_current_card(mw, then=None, tried=(), notes=()) -> None:
    if _busy_refuse():
        return
    try:
        if getattr(mw, 'state', '') != 'review' or not getattr(getattr(mw, 'reviewer', None), 'card', None):
            showWarning('No active card to update.')
            return
        col = mw.col
        note = col.get_note(mw.reviewer.card.nid)
        cfg = _read_config()
        last = _read_last_settings() or {}
        last_subs = last.get('subs', {}) if isinstance(last.get('subs'), dict) else {}
        _hotkey_options(cfg, last_subs, 'subs_', ('cat_subs', 'cat_epub', 'cat_manga', 'furigana', 'bold', 'strip_names', 'context'), ('multi_count', 'context_before', 'context_after'))
        exact = bool(last_subs.get('exact', False))
        try:
            fields = list(note.keys())
        except Exception:
            fields = []
        query_field = str(last_subs.get('query_field') or '').strip()
        if not query_field:
            for cand in ['Front', 'Expression', 'Word', 'Term']:
                if cand in fields:
                    query_field = cand
                    break
        sentence_field = str(last_subs.get('sentence_field') or cfg.get('subs_sentence_field') or '').strip()
        if not sentence_field:
            for cand in ['Sentence1', 'Sentence', 'Text']:
                if cand in fields:
                    sentence_field = cand
                    break
        source_field = str((last_subs.get('source_field') if 'source_field' in last_subs else cfg.get('subs_source_field')) or '').strip()
        if not sentence_field or sentence_field not in fields or sentence_field == query_field:
            _hotkey_refuse(mw, _PROVIDER_SUBS, then, tried, notes, 'Run the batch once, or set the sentence field in Settings.')
            return
        if not query_field or not sentence_field:
            _hotkey_refuse(mw, _PROVIDER_SUBS, then, tried, notes, 'Could not determine fields to update.')
            return
        q_text = get_field_value(note, query_field).strip()
        if not q_text:
            showInfo(f"Query field '{query_field}' is empty; nothing to do.")
            return
        try:
            min_len = int(cfg.get('subs_min_length', 6))
        except Exception:
            min_len = 6
        try:
            max_len = int(cfg.get('subs_max_length', 0)) or None
        except Exception:
            max_len = None
        image_field = str((last_subs.get('image_field') if 'image_field' in last_subs else cfg.get('subs_image_field')) or '').strip()
        _sentence, source_field, image_field = _off_the_word(query_field, sentence_field, source_field, image_field)
        wants_image = bool(image_field and image_field in note)
        nid = mw.reviewer.card.nid
        client = _subs_make_client(cfg, get_logger())
    except Exception as exc:
        showWarning(f'Failed to add an Aobana sentence: {exc}')
        return

    def fetch():
        try:
            client.ensure_running(autostart=bool(cfg.get('subs_autostart', True)))
            diagnostics: List[Dict[str, Any]] = []
            rows = _subs_fetch_rows(client, cfg, q_text, exact, min_len, max_len, count=_subs_multi_count(cfg), diagnostics=diagnostics)
            if cfg.get('subs_context', False):
                for row in rows:
                    wrapped = _subs_context_html(client, row, _subs_context_span(cfg, 'subs_context_before', 2), _subs_context_span(cfg, 'subs_context_after', 1), bool(cfg.get('subs_furigana', True)), bool(cfg.get('subs_bold', True)), _subs_strip_names(cfg))
                    if wrapped:
                        row['_with_context'] = wrapped
            if wants_image:
                for row in rows:
                    if _subs_is_manga(row):
                        row['_page_image'] = client.page_image(int(row.get('rowid')))
            outside = list(dict.fromkeys((str(name) for d in diagnostics for name in d.get('outside_media', []))))
            return (rows, outside)
        finally:
            client.shutdown()

    def apply(result):
        try:
            rows, outside = result
            text, title = _subs_rendered(rows or [], bool(cfg.get('subs_furigana', True)), bool(cfg.get('subs_bold', True)))
            if not text:
                _hotkey_none(mw, _PROVIDER_SUBS, then, tried, 'Title unavailable in Aobana: %s.' % ', '.join(outside) if outside else 'No Aobana results found.', notes)
                return
            col, note = _hotkey_note(mw, nid)
            if note is None:
                return
            updated = False
            if sentence_field in note:
                note[sentence_field] = text
                updated = True
            if source_field and title and (source_field in note):
                note[source_field] = title
                updated = True
            image_note = ''
            manga = [row for row in rows if _subs_is_manga(row)]
            found = [row for row in manga if row.get('_page_image')]
            if wants_image and found:
                updated = _subs_write_page_images(col, note, image_field, [r['_page_image'] for r in found], [r.get('rowid') for r in found], replace=True, append=False) or updated
            elif wants_image:
                updated = _clear_missing_media(note, image_field, True) or updated
            if wants_image and len(found) < len(manga):
                image_note = '\n(This manga page has no image.)'
            if updated:
                _hotkey_save(mw, col, note)
                mw.reset()
                showInfo('Aobana sentence added to current card.' + image_note + _hotkey_notes(notes))
            else:
                showInfo('Nothing was updated.' + _hotkey_notes(notes))
        except Exception as exc:
            showWarning(f'Failed to add an Aobana sentence: {exc}')
    _hotkey_search('Aobana', fetch, apply, poll=client.progress, cancel=client.cancel, on_error=lambda text: _hotkey_none(mw, _PROVIDER_SUBS, then, tried, text, notes, failed=True))

def quick_add_immersionkit_for_current_card(mw, then=None, tried=(), notes=()) -> None:
    if _busy_refuse():
        return
    try:
        if getattr(mw, 'state', '') != 'review' or not getattr(getattr(mw, 'reviewer', None), 'card', None):
            showWarning('No active card to update.')
            return
        col = mw.col
        note = col.get_note(mw.reviewer.card.nid)
        cfg = _read_config()
        last = _read_last_settings() or {}
        block = last.get('immersionkit', {}) if isinstance(last.get('immersionkit'), dict) else {}
        _hotkey_options(cfg, block, 'immersionkit_', [key for key, _attr in BackfillImagesDialog._IK_TOGGLES], [key for key, _attr in BackfillImagesDialog._IK_SPINS])
        try:
            names = list(note.keys())
        except Exception:
            names = []

        def field(name: str, guesses: List[str]) -> str:
            chosen = str((block.get(name) if name in block else cfg.get('immersionkit_' + name)) or '').strip()
            if chosen:
                return chosen if chosen in names else ''
            if name in block:
                return ''
            return next((g for g in guesses if g in names), '')
        query_field = str(block.get('query_field') or '').strip() or next((g for g in ['Front', 'Expression', 'Word', 'Term'] if g in names), '')
        sentence_field = field('sentence_field', ['Sentence1', 'Sentence', 'Text'])
        image_field = field('image_field', ['Image1', 'Picture', 'Image', 'Images'])
        audio_field = field('audio_field', ['Audio1', 'Audio', 'Sound', '音声'])
        translation_field = field('translation_field', [])
        source_field = field('source_field', [])
        sentence_field, image_field, audio_field, translation_field, source_field = _off_the_word(query_field, sentence_field, image_field, audio_field, translation_field, source_field)
        q_text = get_field_value(note, query_field).strip() if query_field else ''
        if not q_text:
            showInfo(f"Query field '{query_field}' is empty; nothing to do." if query_field else 'Could not determine the query field.')
            return
        nid = mw.reviewer.card.nid
        exact = bool(block.get('exact', False))
        stop = threading.Event()
        client = ImmersionKitClient(sleep=stop.wait)
    except Exception as exc:
        showWarning(f'Failed to add an Immersion Kit sentence: {exc}')
        return

    def fetch():
        misses: List[str] = []
        example = _ik_fetch_example(client, cfg, q_text, exact, misses=misses)
        if not example or stop.is_set():
            return (None, misses, {})
        context = _ik_context(client, example, cfg) if sentence_field or image_field or audio_field else ([], [])
        got: Dict[str, Any] = {}
        if sentence_field:
            got['sentence'] = _ik_sentence_html(client, example, cfg, context, q_text)
        if source_field and (not stop.is_set()):
            got['source'] = client.title_of(str(example.get('title', '') or ''))
        for kind, target in (('image', image_field), ('sound', audio_field)):
            if target and (not stop.is_set()):
                got[kind] = _ik_media(client, example, context, kind, cfg)
        return (example, misses, got)

    def apply(result):
        try:
            example, misses, got = result
            if not example:
                _hotkey_none(mw, _PROVIDER_IMMERSIONKIT, then, tried, 'No Immersion Kit sentence with an image (Req. Image is on).' if misses else 'No Immersion Kit results found.', notes)
                return
            col, note = _hotkey_note(mw, nid)
            if note is None:
                return
            updated = False
            if sentence_field and sentence_field in note:
                note[sentence_field] = got.get('sentence', '')
                updated = True
            translation = str(example.get('translation', '') or '').strip()
            if translation_field and translation_field != sentence_field and (translation_field in note) and translation:
                note[translation_field] = translation
                updated = True
            if source_field and source_field in note:
                note[source_field] = got.get('source', '')
                updated = True
            for kind, target, tag in (('image', image_field, '<img src="%s">'), ('sound', audio_field, '[sound:%s]')):
                if not target or target not in note:
                    continue
                names = [col.media.write_data(ensure_media_filename_safe(ik.media_name(item, kind)), data) for item, data in got.get(kind) or [] if data]
                if names:
                    note[target] = ''.join((tag % name for name in names))
                    updated = True
                else:
                    updated = _clear_missing_media(note, target, True) or updated
            if updated:
                _hotkey_save(mw, col, note)
                mw.reset()
                showInfo('Immersion Kit sentence added to current card.' + _hotkey_notes(notes))
            else:
                showInfo('Nothing was updated.' + _hotkey_notes(notes))
        except Exception as exc:
            showWarning(f'Failed to add an Immersion Kit sentence: {exc}')
    _hotkey_search('Immersion Kit', fetch, apply, cancel=stop.set, on_error=lambda text: _hotkey_none(mw, _PROVIDER_IMMERSIONKIT, then, tried, text, notes, failed=True))
_QUICK_ADD = {_PROVIDER_NADESHIKO: quick_add_nadeshiko_for_current_card, _PROVIDER_SUBS: quick_add_subs_for_current_card, _PROVIDER_IMMERSIONKIT: quick_add_immersionkit_for_current_card}
