from __future__ import annotations
import re
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import quote
import requests
DEFAULT_BASE_URL = 'https://apiv2.immersionkit.com'
_API_USER_AGENT = 'AobanaReibun/1.0 (+https://www.immersionkit.com)'
PACE_S = 2.0
RATE_LIMIT_WAIT_S = 10.0
RATE_LIMIT_RETRIES = 3
CONTEXT_CAP = 10
CATEGORIES = ('anime', 'drama', 'games')

class ImmersionKitError(Exception):

    def __init__(self, message: str, fatal: bool=False) -> None:
        super().__init__(message)
        self.fatal = fatal

class Pacer:

    def __init__(self, interval: float=PACE_S, clock: Callable[[], float]=time.monotonic, sleep: Callable[[float], Any]=time.sleep) -> None:
        self.interval = interval
        self._clock = clock
        self._sleep = sleep
        self._lock = threading.Lock()
        self._last: Optional[float] = None

    def wait(self) -> None:
        with self._lock:
            if self._last is not None:
                delay = self._last + self.interval - self._clock()
                if delay > 0:
                    self._sleep(delay)
            self._last = self._clock()
_PACER = Pacer()

def _prettify_title(title_id: str) -> str:
    words = str(title_id or '').replace('_', ' ').split()
    return ' '.join((w[:1].upper() + w[1:] for w in words))

class ImmersionKitClient:

    def __init__(self, base_url: str=DEFAULT_BASE_URL, pacer: Optional[Pacer]=None, sleep: Callable[[float], Any]=time.sleep, session=None) -> None:
        self._base_url = (base_url or DEFAULT_BASE_URL).rstrip('/')
        self._pacer = pacer or _PACER
        self._sleep = sleep
        self._session = session or requests.Session()
        try:
            self._session.headers.update({'User-Agent': _API_USER_AGENT, 'Accept': 'application/json'})
        except Exception:
            pass
        self._titles: Optional[Dict[str, Any]] = None
        self._search_cache: Dict[tuple, List[Dict[str, Any]]] = {}

    def _get(self, path: str, params: Dict[str, Any], timeout: float=30.0) -> Any:
        url = self._base_url + path
        for attempt in range(RATE_LIMIT_RETRIES + 1):
            self._pacer.wait()
            try:
                resp = self._session.get(url, params=params, timeout=timeout)
            except requests.RequestException as exc:
                raise ImmersionKitError('Immersion Kit: %s' % exc) from exc
            if resp.status_code == 200:
                try:
                    return resp.json()
                except ValueError as exc:
                    raise ImmersionKitError('Immersion Kit sent an unreadable answer') from exc
            if resp.status_code == 429:
                if attempt < RATE_LIMIT_RETRIES:
                    if self._sleep(RATE_LIMIT_WAIT_S):
                        raise ImmersionKitError('Immersion Kit search cancelled.')
                    continue
                raise ImmersionKitError('Immersion Kit is rate-limiting; wait a minute and run again.', fatal=True)
            raise ImmersionKitError('Immersion Kit: HTTP %d' % resp.status_code)
        return None

    def search(self, q: str, exact: bool=False, category: Optional[str]=None, per_title: int=5, shortest: bool=False) -> List[Dict[str, Any]]:
        key = (q, bool(exact), category or '', int(per_title), bool(shortest))
        if key in self._search_cache:
            return self._search_cache[key]
        params: Dict[str, Any] = {'q': q, 'showUrlInMedia': 'true', 'limit': int(per_title), 'sort': 'sentence_length:asc' if shortest else 'sentence_length:desc'}
        if exact:
            params['exactMatch'] = 'true'
        if category in CATEGORIES:
            params['category'] = category
        data = self._get('/search', params) or {}
        examples = [e for e in data.get('examples') or [] if isinstance(e, dict)]
        self._search_cache[key] = examples
        return examples

    def context(self, sentence_id: str) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
        data = self._get('/sentence_with_context', {'sentenceId': sentence_id}) or {}
        before = [s for s in data.get('pretext_sentences') or [] if isinstance(s, dict)]
        after = [s for s in data.get('posttext_sentences') or [] if isinstance(s, dict)]
        return (before, after)

    def titles(self) -> Dict[str, Any]:
        if self._titles is None:
            try:
                data = self._get('/index_meta', {}) or {}
                self._titles = data.get('data') or {}
            except ImmersionKitError as exc:
                if exc.fatal:
                    raise
                self._titles = {}
        return self._titles

    def title_of(self, title_id: str) -> str:
        meta = self.titles().get(title_id) or {}
        name = str(meta.get('title', '') or '').strip() if isinstance(meta, dict) else ''
        return name or _prettify_title(title_id)

    def download(self, url: str, timeout: float=60.0) -> Optional[bytes]:
        if not url:
            return None
        try:
            resp = requests.get(url, headers={'User-Agent': _API_USER_AGENT}, timeout=timeout)
            resp.raise_for_status()
            return resp.content
        except Exception:
            return None
_LEAD_TAG_RE = re.compile('^\\s*\\[[A-Za-z0-9 ]{1,12}\\][\\s\u3000]*')
_KANJI_RE = re.compile('[㐀-䶿一-鿿豈-\ufaff々〆ヶ]')
_KANA_RE = re.compile('[ぁ-ゖァ-ヶー]')
_ASCII_TAG_RE = re.compile('\\(([^()]*)\\)')
_TAG_RE = re.compile('<[^>]+>')
_RT_RE = re.compile('<rt>.*?</rt>', re.DOTALL)
_TRIM = ' \t\r\n\u3000'
SUBS_STR_REPLACEMENTS = [('>>', ' '), ('?\u3000', '？'), ('? ', '？'), ('?', '？'), ('？\u3000', '？'), ('？ ', '？'), ('!\u3000', '！'), ('! ', '！'), ('!', '！'), ('！\u3000', '！'), ('！ ', '！'), ('｡', '。'), ('。\u3000', '。'), ('。 ', '。'), (' 。', '。'), ('､', '、'), ('、 ', '、'), (' 、', '、'), ('……', '…'), ('...', '…'), ('････', '…'), ('･･･', '…'), ('･･', '：'), ('…\u3000', '…'), ('… ', '…'), (' …', '…'), ('\u3000…', '…'), ('➡\u3000', '――'), ('➡ ', '――'), ('➡', '――'), ('➨\u3000', '――'), ('➨ ', '――'), ('➨', '――'), (' ―', '――'), ('\u3000―', '――'), (' —', '――'), ('\u3000—', '――'), ('｢', '「'), ('｣', '」'), ('「\u3000', '「'), (' 「', '「'), ('」\u3000', '」'), ('」 ', '」'), ('\u3000(', '('), (' ( ', '('), ('( ', '('), ('(\u3000', '('), (' )', ')'), ('\u3000)', ')'), ('[', '（'), (']', '）'), ('<', '＜'), ('>', '＞'), ('＞ ', '＞'), ('＞\u3000', '＞'), ('＜ ', '＜'), ('＜\u3000', '＜'), ('）\u3000', '）'), ('） ', '）'), ('\u3000（', '（'), (' （', '（'), ('~ ', '~'), (' ~', '~'), ('〜', '～'), (' : ', '：'), (':', '：'), (' ･ ', '･'), (' ･', '･'), ('･ ', '･'), ('･', '・'), (' ・', '・'), ('・ ', '・'), ('"', '”'), ('→', '')]

def _hira(text: str) -> str:
    return ''.join((chr(ord(c) - 96) if 'ァ' <= c <= 'ヶ' else c for c in text))

def _cleaned_chars(sentence: str) -> List[str]:
    out = list(sentence)
    m = _LEAD_TAG_RE.match(sentence)
    if m:
        for i in range(m.end()):
            out[i] = ''
    for i, c in enumerate(sentence):
        if c == '→' and out[i]:
            nxt = sentence[i + 1] if i + 1 < len(sentence) else ''
            out[i] = '' if not nxt or nxt.isspace() else '\u3000'
    return _normalized(out)

def _normalized(chars: List[str]) -> List[str]:
    stream = [(ch, i) for i, piece in enumerate(chars) for ch in piece]
    for src, dst in SUBS_STR_REPLACEMENTS:
        text = ''.join((ch for ch, _ in stream))
        if src not in text:
            continue
        out = []
        pos = 0
        while True:
            hit = text.find(src, pos)
            if hit < 0:
                break
            out.extend(stream[pos:hit])
            out.extend(((ch, stream[hit][1]) for ch in dst))
            pos = hit + len(src)
        out.extend(stream[pos:])
        stream = out
    result = [''] * len(chars)
    for ch, i in stream:
        result[i] += ch
    return result

def bold_offsets(example: Dict[str, Any]) -> set:
    words = [str(w) for w in example.get('word_list') or []]
    sentence = str(example.get('sentence', '') or '')
    if ''.join(words) != sentence:
        return set()
    out = set()
    for item in example.get('matched_indexes') or []:
        try:
            index, length = (int(item['index']), int(item['length']))
        except Exception:
            continue
        start = len(''.join(words[:index]))
        out.update(range(start, min(start + length, len(sentence))))
    return out

def _furigana_pairs(furigana: str) -> Optional[Tuple[str, List[Tuple[int, int, str]]]]:
    stream: List[str] = []
    pairs: List[Tuple[int, int, str]] = []
    boundary = 0
    i = 0
    while i < len(furigana):
        c = furigana[i]
        if c == '[':
            j = furigana.find(']', i)
            if j < 0 or boundary == len(stream):
                return None
            pairs.append((boundary, len(stream), furigana[i + 1:j]))
            boundary = len(stream)
            i = j + 1
            continue
        if c.isspace():
            boundary = len(stream)
        else:
            stream.append(c)
        i += 1
    return (''.join(stream), pairs)

def ruby_spans(example: Dict[str, Any]) -> Dict[int, Tuple[int, str]]:
    sentence = str(example.get('sentence', '') or '')
    parsed = _furigana_pairs(str(example.get('sentence_with_furigana', '') or ''))
    if not parsed:
        return {}
    stream, pairs = parsed
    positions = [k for k, c in enumerate(sentence) if not c.isspace()]
    if ''.join((sentence[k] for k in positions)) != stream:
        return {}
    spans: Dict[int, Tuple[int, str]] = {}
    for s, e, reading in pairs:
        base = stream[s:e]
        if not reading or not _KANJI_RE.search(base):
            continue
        while base and reading and _KANA_RE.match(base[0]) and (_hira(base[0]) == _hira(reading[0])):
            base, reading, s = (base[1:], reading[1:], s + 1)
        while base and reading and _KANA_RE.match(base[-1]) and (_hira(base[-1]) == _hira(reading[-1])):
            base, reading, e = (base[:-1], reading[:-1], e - 1)
        if not base or not reading or (not _KANJI_RE.search(base)):
            continue
        start, last = (positions[s], positions[e - 1])
        if last - start != e - 1 - s:
            continue
        spans[start] = (last + 1, reading)
    return spans

def render(example: Dict[str, Any], furigana: bool=False, bold: bool=True, strip_names: bool=False) -> str:
    sentence = str(example.get('sentence', '') or '')
    html = markup(_cleaned_chars(sentence), ruby_spans(example) if furigana else {}, bold_offsets(example) if bold else set()).strip(_TRIM)
    if strip_names:
        html = strip_names_html(html)
    return html

def markup(chars: List[str], spans: Dict[int, Tuple[int, str]], bolds: set) -> str:
    units: List[Tuple[str, bool]] = []
    i = 0
    while i < len(chars):
        if i in spans:
            end, reading = spans[i]
            base = ''.join(chars[i:end])
            units.append(('<ruby>%s<rt>%s</rt></ruby>' % (base, reading), any((k in bolds for k in range(i, end)))))
            i = end
            continue
        if chars[i]:
            units.append((chars[i], i in bolds))
        i += 1
    out = []
    in_bold = False
    for text, is_bold in units:
        if is_bold != in_bold:
            out.append('<b>' if is_bold else '</b>')
            in_bold = is_bold
        out.append(text)
    if in_bold:
        out.append('</b>')
    return ''.join(out)

def strip_names_html(html: str) -> str:
    from .aobana_api import strip_speaker_tags
    return strip_speaker_tags(_ASCII_TAG_RE.sub('（\\1）', html))[0]

def plain(html: str) -> str:
    return _TAG_RE.sub('', _RT_RE.sub('', str(html or ''))).strip(_TRIM)

def length(example: Dict[str, Any]) -> int:
    return len(plain(render(example, furigana=False, bold=False)))

def category_of(example: Dict[str, Any]) -> str:
    return str(example.get('id', '') or '').split('_', 1)[0]

def media_url(example: Dict[str, Any], item: Dict[str, Any], kind: str) -> str:
    name = str(item.get(kind, '') or '').strip()
    if not name or name.startswith(('http://', 'https://')):
        return name
    base = str(example.get(kind, '') or '').strip()
    if '/' not in base:
        return ''
    return base.rsplit('/', 1)[0] + '/' + quote(name)

def media_name(example: Dict[str, Any], kind: str) -> str:
    ext = '.jpg' if kind == 'image' else '.mp3'
    stem = re.sub('[^A-Za-z0-9_.-]', '_', str(example.get('id', '') or 'example'))
    return 'ik_%s%s' % (stem, ext)
