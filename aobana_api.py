from __future__ import annotations
'Client for a local Aobana instance (Flask on 127.0.0.1:5010, the add-on\'s own port).\n\nAobana gains no knowledge of this addon. It uses three read-only GETs\nthat the web UI uses too: `/api/search` for sentences, `/api/episodes` as the\nliveness probe, and `/api/context` for the context fill (3.3). That endpoint\'s\n`before`/`after` window and its `lines`/`match` fields were added for this\nclient in Aobana v6.7, as parameters with the web UI\'s old values as\ndefaults; nothing in it is addon-specific. `/api/locate` (also v6.7) is the one\nendpoint made for this addon - see `locate()` for why search could not do it.\n\nTwo properties of the server shape everything below.\n\n`/api/search` keys in-flight queries by `request.remote_addr` and interrupts the\nprevious one when a new request arrives from the same client, answering the\ninterrupted request with `{"aborted": true}` and HTTP 499. Every request from\nAnki arrives from 127.0.0.1, so concurrent fetches would kill each other and the\nsymptom would be silently skipped notes, not an error. `search()` therefore\nholds a lock: one request in flight at a time, always.\n\nA server this module starts is Aobana 1.7 or later, found by `discovery.resolve`, and\nis stopped by killing its process tree, not just the handle: search workers are\nchild processes of it.\n'
import os
import random
import re
import socket
import subprocess
import threading
import time
import urllib.parse as urllib_parse
from typing import Any, Callable, Dict, List, Optional
import requests
from .discovery import DEFAULT_PYTHON, DiscoveryError, resolve
DEFAULT_BASE_URL = 'http://127.0.0.1:5010'
_STARTUP_TIMEOUT_S = 90.0
_STARTUP_POLL_S = 0.05
_PORT_CHECK_S = 0.1
_PROBE_TIMEOUT_S = 2.0
_SEARCH_TIMEOUT_S = 120.0
_LONG_SEARCH_TIMEOUT_S = 3600.0
_CLIENT_TOKEN = 'reibun-%d' % os.getpid()
_PROGRESS_TIMEOUT_S = 5.0
_CREATE_NEW_CONSOLE = 16
_CREATE_NO_WINDOW = 134217728
_SW_SHOWMINNOACTIVE = 7

class AobanaError(Exception):
    pass

class SearchResults(list):

    def __init__(self, rows, outside_media=None, global_total=None) -> None:
        super().__init__(rows)
        self.outside_media = outside_media if isinstance(outside_media, list) else []
        self.global_total = global_total
_HIGHLIGHT_RE = re.compile('</?b\\s*>|<span class=\\"hl-[a-z-]+\\">|</span>', re.IGNORECASE)
_HL_TAIL_SPAN_RE = re.compile('<span class=\\"hl-tail\\">(.*?)</span>', re.IGNORECASE | re.DOTALL)
_HL_TAIL_P_SPAN_RE = re.compile('<span class=\\"hl-tail-p\\">(.*?)</span>', re.IGNORECASE | re.DOTALL)
_ADJACENT_BOLD_RE = re.compile('</b><b>', re.IGNORECASE)
_RUBY_CLASS_RE = re.compile('<ruby[^>]*>', re.IGNORECASE)
_RT_RE = re.compile('<rt>.*?</rt>', re.IGNORECASE | re.DOTALL)
_RUBY_CLOSE_RE = re.compile('</ruby>', re.IGNORECASE)
_ANY_TAG_RE = re.compile('<[^>]+>')

def render_sentence(display_line: str, furigana: bool=True, bold: bool=True) -> str:
    text = str(display_line or '')
    if bold:
        text = _HL_TAIL_P_SPAN_RE.sub('\\1', text)
        text = _HL_TAIL_SPAN_RE.sub('<b>\\1</b>', text)
        text = _ADJACENT_BOLD_RE.sub('', text)
    else:
        text = _HIGHLIGHT_RE.sub('', text)
    if furigana:
        return _RUBY_CLASS_RE.sub('<ruby>', text).strip()
    text = _RT_RE.sub('', text)
    text = _RUBY_CLASS_RE.sub('', text)
    text = _RUBY_CLOSE_RE.sub('', text)
    return text.strip()
_SPEAKER_TAG_RE = re.compile('(（[^（）]*）)')
_CUE_JOIN = '、'
_CUE_JOIN_BEFORE_3_5 = '・'

def _is_tag(part: str) -> bool:
    return part.startswith('（') and part.endswith('）')

def strip_speaker_tags(display_line: str):
    text = str(display_line or '')
    parts = [p for p in _SPEAKER_TAG_RE.split(text) if p]
    if not any((_is_tag(p) for p in parts)):
        return (text, True)
    if all((_is_tag(p) or not p.strip() for p in parts)):
        return (_CUE_JOIN.join((p[1:-1].strip() for p in parts if _is_tag(p))), True)
    segments = []
    current = ''
    has_content = False
    for i, part in enumerate(parts):
        if not _is_tag(part):
            current += part
            has_content = has_content or bool(part.strip())
            continue
        rest = parts[i + 1] if i + 1 < len(parts) else ''
        if rest and (not _is_tag(rest)) and rest.strip():
            if has_content:
                segments.append(current.strip())
                current, has_content = ('', False)
        else:
            current += part
    if current.strip():
        segments.append(current.strip())
    if len(segments) == 1:
        return (segments[0], True)
    return (''.join(('「%s」' % seg for seg in segments)), True)

def plain_text(display_line: str) -> str:
    return _ANY_TAG_RE.sub('', str(display_line or '')).strip()

class AobanaClient:

    def __init__(self, base_url: str=DEFAULT_BASE_URL, project_dir: str='', python_exe: str=DEFAULT_PYTHON, window: str='hidden', logger: Any=None) -> None:
        self.base_url = (base_url or DEFAULT_BASE_URL).rstrip('/')
        self.project_dir = str(project_dir or '').strip()
        self.python_exe = str(python_exe or DEFAULT_PYTHON).strip() or DEFAULT_PYTHON
        self.started_from = ''
        self.window = window if window in ('visible', 'minimized', 'hidden') else 'hidden'
        self.logger = logger
        self._proc: Optional[subprocess.Popen] = None
        self._owns_server = False
        self._request_lock = threading.Lock()
        self.context_missing_reason = ''
        self._searching: Optional[Dict[str, Any]] = None
        self._media_sets: Optional[bool] = None
        self._cancelled = threading.Event()

    @property
    def owns_server(self) -> bool:
        return self._owns_server

    def probe(self, timeout: float=_PROBE_TIMEOUT_S) -> bool:
        try:
            resp = requests.get(self.base_url + '/api/episodes', timeout=timeout)
            return resp.status_code == 200
        except Exception:
            return False

    def port_is_open(self, timeout: float=0.5) -> bool:
        parsed = urllib_parse.urlsplit(self.base_url)
        host = parsed.hostname or '127.0.0.1'
        port = parsed.port or (443 if parsed.scheme == 'https' else 80)
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        try:
            return sock.connect_ex((host, port)) == 0
        except Exception:
            return False
        finally:
            try:
                sock.close()
            except Exception:
                pass

    def ensure_running(self, autostart: bool=True, tick: Optional[Callable[[], None]]=None) -> bool:
        self._check_cancelled()
        port_open = self.port_is_open(timeout=_PORT_CHECK_S)
        if port_open and self.probe():
            return True
        if not autostart:
            raise AobanaError('Aobana is not running at %s, and auto-start is off.\nStart it yourself, or turn on "Start Aobana automatically" in Settings.' % self.base_url)
        try:
            install = resolve(self.project_dir, self.python_exe)
        except DiscoveryError as exc:
            raise AobanaError(str(exc))
        if port_open:
            raise AobanaError('Something is already listening on %s but is not answering as Aobana.\nClose it, or start Aobana yourself, and try again.' % self.base_url)
        self._launch(install)
        deadline = time.time() + _STARTUP_TIMEOUT_S
        while time.time() < deadline:
            if self._cancelled.is_set():
                self.shutdown()
                self._check_cancelled()
            proc = self._proc
            if proc is not None and proc.poll() is not None:
                self._owns_server = False
                self._proc = None
                raise AobanaError('Aobana exited while starting up. Run it by hand to see why.')
            if self.port_is_open(timeout=_PORT_CHECK_S) and self.probe(timeout=1.0):
                return True
            if tick:
                try:
                    tick()
                except Exception:
                    pass
            self._cancelled.wait(_STARTUP_POLL_S)
        self.shutdown()
        raise AobanaError('Aobana did not answer within %d seconds of starting.' % int(_STARTUP_TIMEOUT_S))

    def _check_cancelled(self) -> None:
        if self._cancelled.is_set():
            raise AobanaError('Cancelled.')

    @staticmethod
    def server_argv(python: str) -> List[str]:
        return [python, '-m', 'aobana.server.app']

    def _launch(self, install) -> None:
        parsed = urllib_parse.urlsplit(self.base_url)
        env = dict(os.environ, AOBANA_DEBUG='0', AOBANA_CLIENT='reibun')
        for key in ('PYTHONHOME', 'PYTHONPATH', 'PYTHONSTARTUP', 'PYTHONSAFEPATH', 'VIRTUAL_ENV'):
            env.pop(key, None)
        if parsed.port:
            env['AOBANA_PORT'] = str(parsed.port)
        argv = self.server_argv(install.python)
        kwargs: Dict[str, Any] = {'cwd': install.program_dir, 'env': env}
        if os.name == 'nt':
            if self.window == 'hidden':
                kwargs['creationflags'] = _CREATE_NO_WINDOW
            else:
                kwargs['creationflags'] = _CREATE_NEW_CONSOLE
                if self.window == 'minimized':
                    si = subprocess.STARTUPINFO()
                    si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                    si.wShowWindow = _SW_SHOWMINNOACTIVE
                    kwargs['startupinfo'] = si
        else:
            kwargs['start_new_session'] = True
        try:
            self._proc = subprocess.Popen(argv, **kwargs)
        except Exception as exc:
            raise AobanaError("Could not start Aobana with '%s':\n%s" % (install.python, exc))
        self._owns_server = True
        self.started_from = install.program_dir
        self._log('info', 'Started Aobana (pid %s) from %s (%s), with %s' % (self._proc.pid, install.program_dir, install.where, install.python))

    def shutdown(self) -> None:
        proc, self._proc = (self._proc, None)
        owned, self._owns_server = (self._owns_server, False)
        if proc is None or not owned:
            return
        if proc.poll() is not None:
            return
        try:
            if os.name == 'nt':
                subprocess.run(['taskkill', '/PID', str(proc.pid), '/T', '/F'], creationflags=_CREATE_NO_WINDOW, capture_output=True)
            else:
                try:
                    os.killpg(os.getpgid(proc.pid), 15)
                except Exception:
                    proc.terminate()
        except Exception as exc:
            self._log('error', 'Failed to stop Aobana (pid %s): %s' % (proc.pid, exc))
        try:
            proc.wait(timeout=10)
        except Exception:
            pass
        self._log('info', 'Stopped Aobana (pid %s)' % proc.pid)

    def _log(self, level: str, message: str) -> None:
        if self.logger is None:
            return
        try:
            getattr(self.logger, level)(message)
        except Exception:
            pass

    def search(self, query: str, *, exact: bool=False, media: str='all', limit: int=100, sort: str='recommended', folder: str='') -> SearchResults:
        params = {'q': query, 'limit': max(1, int(limit)), 'sort': sort, 'media': media, 'client': _CLIENT_TOKEN}
        if exact:
            params['exact'] = 'on'
        if folder:
            params['folder'] = folder
        if sort == 'random':
            params['seed'] = random.randrange(1, 2 ** 31)
        self._check_cancelled()
        with self._request_lock:
            self._check_cancelled()
            self._searching = params
            try:
                resp = requests.get(self.base_url + '/api/search', params=params, timeout=_LONG_SEARCH_TIMEOUT_S)
            except requests.Timeout:
                raise AobanaError('Aobana did not finish this search within an hour.')
            except requests.ConnectionError:
                raise AobanaError('Aobana stopped answering during the search.')
            except Exception as exc:
                raise AobanaError('Aobana request failed: %s' % exc)
            finally:
                self._searching = None
        if resp.status_code == 499:
            raise AobanaError('Aobana cancelled the query — something else is searching from this machine at the same time.')
        if resp.status_code != 200:
            raise AobanaError('Aobana returned HTTP %s' % resp.status_code)
        try:
            payload = resp.json()
        except Exception:
            raise AobanaError('Aobana returned a response that was not JSON.')
        if payload.get('aborted'):
            raise AobanaError('Aobana cancelled the query.')
        results = payload.get('results')
        return SearchResults(results if isinstance(results, list) else [], payload.get('outside_media'), payload.get('global_total'))

    def media_sets(self) -> bool:
        if self._media_sets is None:
            try:
                resp = requests.get(self.base_url + '/api/capabilities', timeout=_PROGRESS_TIMEOUT_S)
                data = resp.json() if resp.status_code == 200 else {}
                self._media_sets = isinstance(data, dict) and data.get('media_sets') is True
            except Exception:
                self._media_sets = False
        return self._media_sets

    def cancel(self) -> None:
        self._cancelled.set()
        if self._owns_server:
            self.shutdown()
            return
        try:
            requests.post(self.base_url + '/api/search/cancel', params={'client': _CLIENT_TOKEN}, headers={'X-Aobana': '1'}, json={}, timeout=_PROGRESS_TIMEOUT_S)
        except Exception as exc:
            self._log('error', 'Could not cancel the Aobana search: %s' % exc)

    def progress(self) -> Optional[float]:
        params = self._searching
        if not params:
            return None
        try:
            resp = requests.get(self.base_url + '/api/search/progress', params=params, timeout=_PROGRESS_TIMEOUT_S)
            data = resp.json()
        except Exception:
            return None
        if not isinstance(data, dict) or not data.get('running'):
            return None
        try:
            return max(0.0, float(data.get('remaining')))
        except (TypeError, ValueError):
            return None

    def titles(self) -> List[str]:
        with self._request_lock:
            try:
                resp = requests.get(self.base_url + '/api/search', params={'q': '', 'limit': 1, 'media': 'all'}, timeout=_SEARCH_TIMEOUT_S)
                resp.raise_for_status()
                payload = resp.json()
            except Exception as exc:
                raise AobanaError("Could not get Aobana's titles: %s" % exc)
        names = payload.get('all_folders') if isinstance(payload, dict) else None
        return [str(name) for name in names or [] if name]

    def _library_get(self, path: str) -> Dict[str, Any]:
        with self._request_lock:
            try:
                resp = requests.get(self.base_url + path, timeout=_SEARCH_TIMEOUT_S)
                resp.raise_for_status()
                data = resp.json()
                if not isinstance(data, dict):
                    raise ValueError('not an object')
                return data
            except Exception as exc:
                raise AobanaError("Could not check Aobana's library: %s" % exc)

    def empty_search_reason(self, diagnostics: List[Dict[str, Any]], selected_media: List[str]) -> str:
        state = self._library_get('/api/library/outdated')
        if state.get('has_database') is False:
            return 'Aobana has no database. Index the library in Aobana.'
        settings = self._library_get('/api/library/settings')
        media = settings.get('media') or {}
        names = {'subs': 'Subtitles', 'books': 'Books', 'manga': 'Manga'}
        disabled = [names[kind] for kind in selected_media if media.get(kind) is False]
        outside = list(dict.fromkeys((str(name) for d in diagnostics for name in d.get('outside_media', []))))
        messages = []
        if disabled:
            messages.append('Turned off in Aobana: ' + ', '.join(disabled) + '.')
        if outside:
            messages.append('Folder or title unavailable in Aobana: ' + ', '.join(outside) + '.')
        if messages:
            return '\n'.join(messages)
        if state.get('has_database') is not True or not all((kind in media for kind in selected_media)):
            return 'Aobana returned no results; its library status could not be confirmed.'
        return 'No matches in the selected Aobana corpora.'

    def context(self, row: Dict[str, Any], before: int, after: int) -> Optional[tuple]:
        params = {'rowid': int(row.get('rowid')), 'file': str(row.get('file') or ''), 'q': '', 'media': str(row.get('media_type') or ''), 'before': max(0, int(before)), 'after': max(0, int(after))}
        with self._request_lock:
            try:
                resp = requests.get(self.base_url + '/api/context', params=params, timeout=_SEARCH_TIMEOUT_S)
            except Exception as exc:
                raise AobanaError('Aobana context request failed: %s' % exc)
        if resp.status_code != 200:
            raise AobanaError('Aobana context returned HTTP %s' % resp.status_code)
        try:
            payload = resp.json()
        except Exception:
            raise AobanaError('Aobana context returned a response that was not JSON.')
        lines, match = (payload.get('lines'), payload.get('match'))
        self.context_missing_reason = ''
        if not isinstance(lines, list):
            self.context_missing_reason = 'old_server'
            return None
        if not isinstance(match, int) or not 0 <= match < len(lines):
            self.context_missing_reason = 'outside_window'
            return None
        text = payload.get('text')
        return (lines, match, text if isinstance(text, str) else None)

    def page_image(self, rowid: int) -> Optional[bytes]:
        with self._request_lock:
            try:
                resp = requests.get('%s/manga/page/%d' % (self.base_url, int(rowid)), timeout=_SEARCH_TIMEOUT_S)
            except Exception as exc:
                raise AobanaError('Aobana page image request failed: %s' % exc)
        if resp.status_code == 404:
            return None
        if resp.status_code != 200:
            raise AobanaError('Aobana page image returned HTTP %s' % resp.status_code)
        if not str(resp.headers.get('Content-Type', '')).startswith('image/'):
            return None
        return resp.content or None

    def locate(self, pieces: List[str], media: str='') -> List[Dict[str, Any]]:
        params = {'text': list(pieces)}
        if media:
            params['media'] = media
        with self._request_lock:
            try:
                resp = requests.get(self.base_url + '/api/locate', params=params, timeout=_SEARCH_TIMEOUT_S)
            except Exception as exc:
                raise AobanaError('Aobana locate request failed: %s' % exc)
        if resp.status_code == 404:
            raise AobanaError('This Aobana is too old for Add context (needs /api/locate). Restart it.')
        if resp.status_code != 200:
            raise AobanaError('Aobana locate returned HTTP %s' % resp.status_code)
        try:
            rows = resp.json().get('rows')
        except Exception:
            raise AobanaError('Aobana locate returned a response that was not JSON.')
        return rows if isinstance(rows, list) else []
CONTEXT_JOINER = '\u3000'
CONTEXT_CLASS = 'subs-context'
SENTENCE_CLASS = 'subs-sentence'
_CONTEXT_SPAN_RE = re.compile('<span class="%s">.*?</span>' % CONTEXT_CLASS, re.DOTALL)
_SENTENCE_ATTRS_RE = re.compile('<span\\b[^>]*\\bclass="[^"]*\\b%s\\b[^"]*"[^>]*>' % SENTENCE_CLASS, re.IGNORECASE)
_DATA_MEDIA_RE = re.compile('\\bdata-media="([^"]+)"', re.IGNORECASE)
_DATA_ROWID_RE = re.compile('\\bdata-rowid="(\\d+)"', re.IGNORECASE)
_DATA_FILE_RE = re.compile('\\bdata-file="([^"]*)"', re.IGNORECASE)
_SPAN_TOKEN_RE = re.compile('<span\\b[^>]*>|</span\\s*>', re.IGNORECASE)
_READING_RE = re.compile('<(rt|rp)\\b[^>]*>.*?</\\1\\s*>', re.IGNORECASE | re.DOTALL)
_BR_RE = re.compile('<br\\s*/?>', re.IGNORECASE)

def tag_sentence(sentence_html: str, rowid: int, media_type: str='subs') -> str:
    if not sentence_html:
        return ''
    if _SENTENCE_ATTRS_RE.search(sentence_html):
        return sentence_html
    return '<span class="%s" data-media="%s" data-rowid="%s">%s</span>' % (SENTENCE_CLASS, media_type or 'subs', int(rowid), sentence_html)

def untag_sentence(sentence_html: str) -> str:
    s = str(sentence_html or '')
    m = _SENTENCE_ATTRS_RE.search(s)
    if not m:
        return s
    depth = 1
    for t in _SPAN_TOKEN_RE.finditer(s, m.end()):
        depth += -1 if t.group(0).startswith('</') else 1
        if depth == 0:
            return s[:m.start()] + s[m.end():t.start()] + s[t.end():]
    return s[:m.start()] + s[m.end():]

def extract_sentence_id(sentence_html: str):
    import html as html_mod
    m = _SENTENCE_ATTRS_RE.search(sentence_html)
    if not m:
        return None
    tag_str = m.group(0)
    m_rowid = _DATA_ROWID_RE.search(tag_str)
    if not m_rowid:
        return None
    rowid = int(m_rowid.group(1))
    m_media = _DATA_MEDIA_RE.search(tag_str)
    media = m_media.group(1) if m_media else 'subs'
    m_file = _DATA_FILE_RE.search(tag_str)
    file = html_mod.unescape(m_file.group(1)) if m_file else ''
    return {'rowid': rowid, 'media_type': media, 'file': file}

def context_wrap(sentence_html: str, lines: List[str], match: int, furigana: bool=True, strip_names: bool=False, media: str='subs') -> str:
    before, after = ([], [])
    for i, html in enumerate(lines or []):
        if i == match:
            continue
        text = _MARK_RE.sub('', str(html or ''))
        if strip_names:
            text = strip_speaker_tags(text)[0]
        text = render_sentence(text, furigana=furigana, bold=False)
        if text:
            (before if i < match else after).append(text)
    joiner = '' if media == 'epub' else CONTEXT_JOINER
    out = ''
    if before:
        out += '<span class="%s">%s%s</span>' % (CONTEXT_CLASS, joiner.join(before), joiner)
    out += sentence_html
    if after:
        out += '<span class="%s">%s%s</span>' % (CONTEXT_CLASS, joiner, joiner.join(after))
    return out

def strip_context(field_line: str) -> str:
    return _CONTEXT_SPAN_RE.sub('', str(field_line or ''))

def split_field_lines(value: str) -> List[str]:
    return _BR_RE.split(str(value or ''))

def field_plain(html: str) -> str:
    import html as html_mod
    text = _READING_RE.sub('', str(html or ''))
    text = _ANY_TAG_RE.sub('', text)
    return html_mod.unescape(text).replace('\xa0', ' ').strip()
_LOCATE_SPLIT_RE = re.compile('[（）「」、・\\s]')
_WS_RE = re.compile('\\s+')
_PAREN_FOLD = str.maketrans('()', '（）')

def _stripped_forms(clean: str) -> set:
    s = strip_speaker_tags(clean)[0]
    forms = {_WS_RE.sub('', s)}
    parts = [p for p in _SPEAKER_TAG_RE.split(clean) if p]
    if parts and all((_is_tag(p) or not p.strip() for p in parts)):
        forms.add(_WS_RE.sub('', _CUE_JOIN_BEFORE_3_5.join((p[1:-1].strip() for p in parts if _is_tag(p)))))
    return forms

def _match_kind(want: str, clean_text: str) -> int:
    want = str(want or '').translate(_PAREN_FOLD)
    clean = str(clean_text or '').translate(_PAREN_FOLD)
    if _WS_RE.sub('', clean) == want:
        return 2
    return 1 if want in _stripped_forms(clean) else 0

def _card_text(sentence_html: str) -> str:
    return field_plain(strip_context(untag_sentence(sentence_html)))

def sentence_key(sentence_html: str) -> str:
    return _WS_RE.sub('', _card_text(sentence_html))

def held_sentences(field_value: str):
    keys, texts = (set(), set())
    for line in split_field_lines(field_value):
        ident = extract_sentence_id(line)
        if ident:
            keys.add((ident['media_type'], ident['rowid']))
        text = sentence_key(line)
        if text:
            texts.add(text)
    return (keys, texts)

def locate_sentence(client: 'AobanaClient', sentence_html: str, title_hint: str=''):
    target = _card_text(sentence_html)
    if not target:
        return (None, False)
    pieces = [p.strip() for p in _LOCATE_SPLIT_RE.split(target) if p.strip()]
    if not pieces:
        return (None, False)
    want = _WS_RE.sub('', target)
    exact, stripped = ([], [])
    for row in client.locate(pieces):
        kind = _match_kind(want, row.get('clean_text'))
        if kind == 2:
            exact.append(row)
        elif kind == 1:
            stripped.append(row)
    hits = exact or stripped
    if not hits:
        return (None, False)
    hint = field_plain(title_hint)
    if hint:
        for row in hits:
            if field_plain(str(row.get('title') or '')) == hint:
                return (row, False)
    return (hits[0], len(hits) > 1)

def sentence_window(client: 'AobanaClient', sentence_html: str, title_hint: str, before: int, after: int):
    tagged = extract_sentence_id(sentence_html)
    if tagged is not None:
        row = {'rowid': tagged['rowid'], 'media_type': tagged['media_type'], 'file': ''}
        try:
            got = client.context(row, before, after)
        except AobanaError:
            got = None
        if got is not None:
            text = got[2]
            want = _WS_RE.sub('', _card_text(sentence_html))
            if text is None or (want and _match_kind(want, text)):
                return (row, False, got)
    row, amb = locate_sentence(client, sentence_html, title_hint)
    if row is None:
        return (None, False, None)
    return (row, amb, client.context(row, before, after))
_MARK_RE = re.compile('</?mark\\s*>', re.IGNORECASE)
