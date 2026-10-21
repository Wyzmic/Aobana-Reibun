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
- **[Aobana](https://github.com/Wyzmic/Aobana)**: your own subtitles, books, manga and Custom
  files, searched on your computer. Sentences with furigana, their source title, context lines
  and a manga page's image; with *i+1 only*, only sentences where the word is the one you don't
  know yet, by the known words Aobana imported from your decks.

On every source, a *Reading field*, or furigana in the word field (`的[まと]`), gives only
sentences where the word is read that way: 的 read まと, not 目的 or 的 read てき.

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
Aobana source, Aobana 2.0 or later (1.9 works without Custom, the reading field and i+1 only).

## Setup

1. Open `Tools → Aobana Reibun → Settings`, and paste your Nadeshiko API key
   (`Get key` opens https://nadeshiko.co/user/developer).
2. Aobana needs no setup when it is installed: the add-on finds it. For a portable or source
   copy, set `Aobana folder` to the folder that holds the `aobana` folder.
3. Set default fields in Settings, or choose them in the run dialog.

## Running

- `Tools → Aobana Reibun → Run` fills a deck. In the Browser, `Edit → Aobana Reibun` or
  right-click `Aobana Reibun` fills the selected notes. The Browser entries sit under
  AnkiAutoImage's `Auto Images` when that add-on is installed.
- Pick the source with the tabs at the top, then the `Query field` holding the word.
  - `Nadeshiko`: `Anime`, `Live Action`, `YouTube` and `Req. Image & Audio`; sentence, image and
    audio fields, and optionally a `Translation` field for the sentence's English (or Spanish)
    translation. `Bold` marks the word, and `Bold ending` (on) marks a verb's or adjective's ending
    with it, 食べました, not only 食べ; the particles after it never are.
  - `Aobana`: `Subtitles`, `Books`, `Manga` and `Custom` (your own `.txt` and `.md` files in
    Aobana); a sentence field, and optionally a source field for the episode or book title and
    an image field for a manga sentence's page.
    `Context` puts the lines before and after the sentence into the sentence field with it.
    `i+1 only` (off by default) picks only a sentence where the searched word is the only one you
    don't know, by the known words you imported in Aobana (Settings → Known words, from your
    decks or a file). With none imported, the run stops before any note is searched and says
    why; `If none found` does not fill those notes either. A note with no i+1 sentence gets
    nothing, and the summary says why: the word is already known, the query is not one word, or
    the word has sentences but none is i+1. The reading field is ignored under i+1 only, and
    `Exact search` is grayed out.
    `No note numbers` (on) drops a leading note number from a book sentence: （27）さやうにて…
    becomes さやうにて…. Context lines keep theirs, and a line that is only a number is never
    picked.
  - `Immersion Kit`: `Anime`, `Drama`, `Games` and `Req. Image`; sentence, image, audio,
    translation and source fields (each may be `(none)`), context, furigana, bold and
    `No (names)`. With `Context` on, the lines around the sentence go into the sentence field,
    and their images and audio go into the image and audio fields with the sentence's, in
    reading order. Untick `Images` or `Audio` under it to keep only the sentence's own. A progress window shows the run and can cancel it.
- `Reading field` (under the query field on every tab, `(none)` by default) names a field holding
  the word's reading: kana, katakana, Anki furigana (`的[まと]`) or ruby. Each source then gives
  only sentences where the word is read that way: 好く with すく skips 好く read よく, and 的 with
  まと skips 目的 and 的 read てき. Aobana searches by the reading; Nadeshiko and Immersion Kit
  keep the sentences their own furigana reads so, a compound the word begins included (日本 with
  にほん finds 日本語 read にほんご). A word field in furigana (`的[まと]`) gives its
  reading too, and its brackets are never searched; the Reading field wins when both have one.
  Katakana counts as hiragana (half-width too), pitch marks and dots are dropped (たべ↓る, たꜜべる
  and た・べる read たべる), and a note in brackets is passed over (`（的のみ）まと` reads まと). When no sentence reads the word that way, or the reading fits no form of the word
  (辛い with たべる), the note is left as it is and the summary counts it: "No sentence with the
  word in that reading". Each tab keeps its own Reading field, and a source under `If none found`
  uses its own tab's.
- `If filled`: Skip a field that already has content, Replace it, or Append to it. Append never
  adds a sentence the note already has. Aobana also offers `Add context`,
  which keeps the sentence already on the card, finds it in your library and puts its context
  around it. Running it again replaces that context instead of stacking it.
- Replace clears a chosen image or audio field when the new sentence has no file for it.
  The old file stays in Anki's media folder. A run is one undo step (Edit → Undo Aobana Reibun).
- `If none found`: tick the other sources to try them in turn when the tab's own finds nothing
  for a note, and `⇄` swaps their order. Settings can set it too. Each writes to the fields on its own tab. One summary
  and one undo step for the whole run. The tab's hotkey follows the same order.
- `No repeats` (Aobana): no sentence goes to two notes in one run.
- `Add context` ticks `Include context` and locks it while chosen; another choice puts your own tick back.
- The dialog remembers each source's choices and stays open after a run.
- The summary counts what was written and, when there are any, the notes left alone: skipped,
  already filled (no search sent); skipped, the note type has none of the fields; found, but
  nothing to write; deleted during the run.

Review hotkeys (change them in Settings → General; on a Mac, Cmd instead of Ctrl):
- Aobana sentence: `Ctrl+Shift+W`
- Nadeshiko sentence, image and audio: `Ctrl+Shift+O`
- Immersion Kit sentence, image and audio: `Ctrl+Shift+K`

Each fills the current card with the fields and options of its tab's last run (corpora or
categories, furigana, bold, Exact search, the Reading field, the context window, and Aobana's
sentences per note), or Settings' before any run. A hotkey always replaces what the card had; each
press is one undo step (Edit → Undo Aobana Reibun). If the search fails, a
source ticked under `If none found` is tried next and the error is shown with its result, except
when i+1 only cannot run (no known words in Aobana): then nothing else runs. They
work in Anki's main window, not in the Browser.
Saved hotkey changes take effect at once; a blank shortcut disables it. A field the last run
named that the card's note type lacks is taken from Settings instead, then guessed by name
(Sentence1, Sentence, Text). The sentence field must differ from the query field. If no
suitable field is on the card, set the default sentence field in Settings.

## Settings

`Tools → Aobana Reibun → Settings`, or the add-on's `Config` button. Four tabs, each
one page in sections. The run dialog and the hotkeys remember your last run; an option you
change in Settings and save replaces that remembered choice, and options you leave alone keep it.

- `General`: *Run dialog* (whether If filled starts on Replace, and the default Reading field for
  every source) and *Reviewer hotkeys* (the three shortcuts).
- `Aobana`: *Server*: where Aobana is (blank finds the installed one; `Browse…` picks a folder),
  its URL, auto-start and console window. *Search*: the Subtitles, Books, Manga and Custom
  corpora (Aobana searches only those ticked), i+1 only, a title limit (`Choose…` lists the
  titles in your Aobana), length limits, pool size and pick strategy. *Fallback*: its switch,
  limits and strategy, and If none found. *Fields & output*: furigana, bold, `No （names）`, No
  note numbers, sentences per note, No repeats, the context window and default fields.
  *Known words for Aobana*: see below.
- `Nadeshiko`: *Account*: API key and URL. *Search*: categories, Require Image & Audio, length
  limits, pool size, pick strategy and requests per note for a reading. *Fallback*: its switch
  (`Search again with the fallback settings`), limits and strategy, and If none found. *Fields &
  output*: bold, bold ending, furigana, the translation language and default fields.
- `Immersion Kit`: *Search*: categories, Req. Image, length limits, examples per title and pick
  strategy. *Fallback*: its switch (on), limits and strategy, and If none found. *Fields & output*:
  default fields, furigana, bold, No (names), and the context window with its lines' images and audio.

## Shipped settings

The add-on ships with the developer's recommended settings. Change any of them in Settings, and
`Restore Defaults` brings these back.

| | Nadeshiko | Aobana | Immersion Kit |
|---|---|---|---|
| sentence length | 27 to 50 | 6 to 50 | 6 to 50 |
| pool, pick | 25, random from pool | 500, random from pool | 5 per title, random from pool |
| fallback | **off** (25, 2 to 200, random when on) | **on** (500, 2 to 200, random; on your computer) | **on** (2 to 200, random; no extra request) |
| sources | anime, live action, YouTube | Subtitles, Books, Manga, Custom | anime, drama, games |
| options | bold; furigana off | furigana, bold, No note numbers; i+1 only off | bold; furigana off |

Aobana counts sentence length without furigana readings or most punctuation. Fields left blank in
Settings are chosen in the run dialog.

## How Aobana is used

- **Found automatically.** On Windows through its installer's registration, on Linux in
  `~/.local/share/aobana-app`, on macOS in `Aobana.app`, and started with Aobana's own Python.
  If there are two installs, or the folder in Settings is missing, the add-on says so.
  Aobana 1.9 still gives sentences, but Custom, the Reading field and i+1 only need 2.0:
  the summary says when one of them was not used.
- **Started for the run, then stopped.** If Aobana already answers on the add-on's port (5010),
  it is used as it is and left running. Otherwise the add-on starts it and stops it when you
  close the summary. It stops only what it started. The console can be shown, started
  minimized, or never shown (`Aobana terminal window`). The summary is always kept above it.
  The terminal is hidden by default. The review hotkey starts and stops its server per press.
- **One request at a time.** Aobana cancels an earlier search from the same computer, so
  parallel requests would silently skip notes.
- **One search at a time, with a window that cancels.** A hotkey holds Anki until its sentence
  arrives (the card can't be answered meanwhile): nothing shows for the first second, then a small
  window, "Searching…", whose bar for Aobana follows Aobana's own estimate from 3 seconds. It
  closes itself when done; **Cancel** or Esc stops the search and leaves the card as it was. A run
  shows a progress window with the time left for the whole run, from the average per note so
  far, and whose Cancel stops at once. A hotkey or a run started while another
  search is going starts nothing and says "Still searching…". Errors are a message naming the
  cause. A long Aobana search may run for up to an hour.
- **Sentences as Aobana renders them.** Furigana is real `<ruby>`; bold marks the matched word.
- **Pool size** is how deep the add-on looks before picking: default 500, maximum 2000.
- **Manga page images.** With an `Image` field chosen, a sentence from a manga brings its page
  image (Aobana keeps it beside the `.mokuro` file). A page without one is counted in the
  summary.
- **Text, not markup.** A line that quotes Anki's own syntax, `[sound:…]` or `{{c1::…}}`, reaches
  the card as text: it plays nothing and makes no cloze.

## Known words for Aobana

While Anki is open, the add-on answers Aobana on `127.0.0.1:5020`, so Aobana's Settings → Known
words can list your decks and import the words of the decks you pick (for i+1 only). It is on by
default. It reads your collection and never changes it, and only programs on this computer can
ask; Aobana sends a header no web page can. Turn it off, or move it to another port, under
Settings → Aobana → *Known words for Aobana*. Aobana has no field for the port: set `reibun_port`
to the same number in Aobana's `config.json`. If another program already holds the port, the
add-on says so when Anki starts and in Settings.

## How Nadeshiko is used

- Each key may send 150 requests a minute and 5,000 a month. Searches are spaced at least
  0.4 seconds apart across the add-on. A rate-limited request waits `Retry-After`, or 20 then
  40 seconds, before retrying. Repeated limits stop the run and keep what was found. A spent
  quota or a rejected key also stops the run, and the summary says why.
- A search draws a pool of candidates and picks from it. If none fits the length limits and
  the fallback is on, a second search with looser limits runs: one more request from the
  monthly quota.
- With a reading, Nadeshiko is asked for 50 sentences at a time until one reads the word that
  way, at most `Requests per note for a reading` (4 by default), each from the monthly quota.
  Most words find one in the first 50, so they cost one request; a rare reading, such as 的 read
  まと, can cost more.
- `Req. Image & Audio` (off by default) picks only a sentence that has both a screenshot and
  audio, in the fallback too. When none has both, the note is left as it is and the summary
  counts it.

## How Immersion Kit is used

- No key or account. The free API allows about one request every 2 seconds, so a run goes one
  note at a time, about 3.5 minutes per 100 notes (twice that with context, which is one more
  request per note). Media downloads, the context lines' included, come from another host:
  they cost no request and are not slowed.
- One search returns every title's top examples (`Examples per title`, 5 to 50), the shortest
  ones when the pick is Shortest and the longest otherwise; the length limits and the pick are
  applied to that pool. The fallback picks again from the same pool,
  so it sends no request, which is why it is on by default.
- With a reading, when no example in that pool reads the word that way, the add-on asks once
  more, at 50 examples per title: one more request, which can take up to 90 seconds.
- If Immersion Kit refuses with its rate limit anyway, the add-on waits 10 seconds and tries
  again, up to three times, then stops the run and says so; what was found is kept.

## Files

- Log: `user_files/aobana-reibun.log`
- Last run's choices: `user_files/last_settings.json`

## Repository layout

| path | what it is |
|---|---|
| `__init__.py` | entry point: menus, the browser action, reviewer hotkeys |
| `tools.py` | the run dialog, the Settings dialog, the three batches and the three hotkeys |
| `aobana_api.py` | the Aobana client: start/stop, search, context, locate, sentence rendering |
| `discovery.py` | finds the installed Aobana program and its Python (no Qt) |
| `known_server.py` | answers Aobana's known-words import on 127.0.0.1:5020 while Anki is open: your decks, read-only |
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
