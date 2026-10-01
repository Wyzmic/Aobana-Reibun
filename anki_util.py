from __future__ import annotations
import os
import re
import time
from dataclasses import dataclass
from typing import Iterable, List, Optional, Tuple
ADDON_NAME = 'Aobana Reibun'
MENU_NAME = 'Aobana Reibun'
BROWSER_NAME = 'Aobana Reibun'
from aqt import mw
from anki.notes import Note
from anki.collection import Collection

@dataclass
class NoteTarget:
    nid: int
    note_type_name: str

def get_selected_note_ids(browser) -> List[int]:
    try:
        if hasattr(browser, 'selected_notes'):
            nids = browser.selected_notes()
            return list(nids)
    except Exception:
        pass
    try:
        return list(browser.selectedNotes())
    except Exception:
        pass
    try:
        cids = []
        if hasattr(browser, 'selected_cards'):
            cids = list(browser.selected_cards())
        elif hasattr(browser, 'selectedCards'):
            cids = list(browser.selectedCards())
        nids: List[int] = []
        col = mw.col
        for cid in cids:
            try:
                card = col.get_card(cid)
                nids.append(card.nid)
            except Exception:
                continue
        return nids
    except Exception:
        return []

def get_deck_note_ids(col: Collection, deck_name: str) -> List[int]:
    if not deck_name:
        return list(col.find_notes(''))
    did = col.decks.id_for_name(deck_name)
    if did is None:
        return []
    ids = col.decks.deck_and_child_ids(did)
    query = ' or '.join((f'did:{deck_id}' for deck_id in ids))
    nids = col.find_notes(query)
    return list(nids)

def ensure_media_filename_safe(name: str) -> str:
    name = name.strip().replace(' ', '_')
    name = re.sub('[^A-Za-z0-9_.-]', '', name)
    return name or f'image_{int(time.time())}.jpg'

def add_image_to_note(note: Note, field_name: str, media_filename: str, replace: bool) -> bool:
    if field_name not in note:
        return False
    img_tag = f'<img src="{media_filename}">'
    cur = note[field_name]
    if cur and (not replace):
        return False
    note[field_name] = img_tag if replace or not cur else cur + '<br>' + img_tag
    return True

def add_audio_to_note(note: Note, field_name: str, media_filename: str, replace: bool) -> bool:
    if field_name not in note:
        return False
    audio_tag = f'[sound:{media_filename}]'
    cur = note[field_name]
    if cur and (not replace):
        return False
    note[field_name] = audio_tag if replace or not cur else cur + '\n' + audio_tag
    return True

def get_field_value(note: Note, field_name: str) -> str:
    try:
        return note[field_name]
    except Exception:
        return ''
