# Aobana Reibun

An Anki add-on that fills your notes with **Japanese example sentences**, with **audio and
images**, automatically. It is for sentence mining and sentence cards: point it at the word
field of a deck (Yomitan cards, for example) and it backfills a sentence, its screenshot, its
audio clip, its translation and where it comes from, for every note at once or for the card
you are reviewing. *Reibun* (例文) is Japanese for "example sentence".

Three sources, each optional:

- **[Nadeshiko](https://nadeshiko.co)**: sentences with a screenshot and audio from anime,
  live action and YouTube, through the Nadeshiko API (an API key).
- **[Immersion Kit](https://www.immersionkit.com)**: sentences with a screenshot, audio and an
  English translation from anime, drama and games. No key.
- **[Aobana](https://github.com/Wyzmic/Aobana)**: your own subtitles, books and manga, searched on
  your computer. Sentences with furigana, their source title, context lines and a manga
  page's image.

Aobana Reibun started from the code of [AnkiAutoImage](https://github.com/swagercode/AnkiAutoImage)
by swagercode, with the author's permission, and has grown into its own add-on: it keeps
AnkiAutoImage's Nadeshiko source, reworks it for the current API, adds Immersion Kit and
Aobana, and drops image search and image generation. For those, install AnkiAutoImage as well:
the two add-ons have different names, folders, settings, logs, hotkeys and ports, so they sit
side by side.

What is different from AnkiAutoImage, item by item: the
[1.0 release notes](https://github.com/Wyzmic/Aobana-Reibun/releases/tag/1.0).

## Install

From [AnkiWeb](https://ankiweb.net/shared/info/1429349152): in Anki, open **Tools → Add-ons**,
press **Get Add-ons...**, enter the code **1429349152** and restart Anki. Or download
`AobanaReibun.ankiaddon` from the [release](https://github.com/Wyzmic/Aobana-Reibun/releases/latest)
and open it with Anki, then restart. Requires Anki with Qt 6 (tested on 26.9) and, for the
Aobana source, Aobana 1.7 or later.

## Repository layout

| path | what it is |
|---|---|
| `__init__.py` | entry point: menus, the browser action, reviewer hotkeys |
| `tools.py` | the run dialog, the Settings dialog, the three batches and the three hotkeys |
| `aobana_api.py` | the Aobana client: start/stop, search, context, locate, sentence rendering |
| `discovery.py` | finds the installed Aobana program and its Python (no Qt) |
| `nadeshiko_api.py` | the Nadeshiko API client |
| `immersionkit_api.py` | the Immersion Kit client (paced to one request every 2 s), context media URLs, and its sentence rendering: bold, furigana, cleaning |
| `anki_util.py`, `logger.py` | note/field helpers, the add-on's own log |
| `config.json`, `manifest.json` | shipped defaults (the developer's recommended settings: Nadeshiko's fallback off, Aobana's and Immersion Kit's on); the add-on's identity for Anki |

## Credits

- [AnkiAutoImage](https://github.com/swagercode/AnkiAutoImage) by swagercode: Aobana Reibun
  started from its code, with the author's permission.
- [Immersion Kit](https://www.immersionkit.com): the sentences, screenshots, audio,
  translations and readings on the Immersion Kit tab, through its free public API.
- [Nadeshiko](https://nadeshiko.co): the sentences, screenshots, audio and translations on the
  Nadeshiko tab, through the Nadeshiko API.
- [Aobana](https://github.com/Wyzmic/Aobana): the local search behind the Aobana tab.
- [backfill-anki-yomitan](https://github.com/Manhhao/backfill-anki-yomitan): the design the run
  dialog followed.
