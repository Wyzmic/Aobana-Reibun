from __future__ import annotations
import json
import os
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Dict, Iterable, List, Optional
PROTOCOL = 1
DEFAULT_PORT = 5020
MAX_BODY = 1024 * 1024
MAX_DECKS = 2000
MAIN_THREAD_TIMEOUT_S = 60.0
RULES = ('mature', 'reviewed', 'all')
DEFAULT_IVL = 21
APP = 'aobana-reibun'
_SERVER = None
_LOCK = threading.Lock()

class RequestError(Exception):

    def __init__(self, status: int, code: str) -> None:
        super().__init__(code)
        self.status = status
        self.code = code

def _deck_rows(col) -> List[Any]:
    return list(col.decks.all_names_and_ids())

def _is_filtered(col, did: int) -> bool:
    try:
        deck = col.decks.get(did, default=False)
    except TypeError:
        deck = col.decks.get(did)
    return bool(deck and deck.get('dyn'))

def list_decks(col) -> List[Dict[str, Any]]:
    counts = {}
    for did, cards, notes in col.db.all('select case when odid then odid else did end as d, count(), count(distinct nid) from cards group by d'):
        counts[int(did)] = (int(cards), int(notes))
    decks = [(int(row.id), str(row.name)) for row in _deck_rows(col) if not _is_filtered(col, int(row.id))]
    ids = {name: did for did, name in decks}
    parents = {name.rsplit('::', 1)[0] for _did, name in decks if '::' in name}
    subtree: Dict[int, set] = {ids[name]: set() for name in parents if name in ids}
    if subtree:
        names = {did: name for did, name in decks}
        for did, nid in col.db.all('select distinct case when odid then odid else did end as d, nid from cards'):
            name = names.get(int(did))
            while name:
                if ids.get(name) in subtree:
                    subtree[ids[name]].add(int(nid))
                name = name.rsplit('::', 1)[0] if '::' in name else ''
    out = []
    for did, name in decks:
        cards, notes = counts.get(did, (0, 0))
        out.append({'id': did, 'name': name, 'cards': cards, 'notes': notes, 'subtree_notes': len(subtree[did]) if did in subtree else notes})
    out.sort(key=lambda d: d['name'].lower().split('::'))
    return out

def _chosen_ids(col, deck_ids: Iterable[Any], subdecks: bool):
    known = {int(row.id) for row in _deck_rows(col)}
    wanted, missing = ([], [])
    for raw in deck_ids:
        try:
            did = int(raw)
        except (TypeError, ValueError):
            raise RequestError(400, 'bad_deck')
        if did not in known:
            missing.append(did)
            continue
        ids = col.decks.deck_and_child_ids(did) if subdecks else [did]
        for one in ids:
            one = int(one)
            if one not in wanted and (not _is_filtered(col, one)):
                wanted.append(one)
    return (wanted, missing)

def _deck_query(ids: List[int]) -> str:
    return '(' + ' or '.join((f'did:{did}' for did in ids)) + ')'

def rule_query(rule: Dict[str, Any]) -> str:
    kind = str((rule or {}).get('kind') or 'mature')
    if kind not in RULES:
        raise RequestError(400, 'bad_rule')
    if kind == 'mature':
        try:
            ivl = int((rule or {}).get('ivl', DEFAULT_IVL))
        except (TypeError, ValueError):
            raise RequestError(400, 'bad_rule')
        if not 1 <= ivl <= 36500:
            raise RequestError(400, 'bad_rule')
        return f'prop:ivl>={ivl}'
    if kind == 'reviewed':
        return '-is:new'
    return ''

def _model_fields(col, mid: int) -> List[str]:
    model = col.models.get(mid) or {}
    fields = sorted(model.get('flds') or [], key=lambda f: f.get('ord', 0))
    return [str(f.get('name', '')) for f in fields]

def _chunks(items: List[int], size: int=500):
    for i in range(0, len(items), size):
        yield items[i:i + size]

def note_types(col, deck_ids: Iterable[Any], subdecks: bool=True) -> Dict[str, Any]:
    ids, missing = _chosen_ids(col, deck_ids, subdecks)
    if not ids:
        return {'note_types': [], 'missing': missing}
    per = {}
    for chunk in _chunks(ids):
        marks = ','.join('?' * len(chunk))
        for mid, notes in col.db.all(f'select n.mid, count(distinct n.id) from cards c join notes n on n.id = c.nid where (case when c.odid then c.odid else c.did end) in ({marks}) group by n.mid', *chunk):
            per[int(mid)] = per.get(int(mid), 0) + int(notes)
    out = []
    for mid, notes in per.items():
        model = col.models.get(mid) or {}
        out.append({'id': mid, 'name': str(model.get('name', '')), 'fields': _model_fields(col, mid), 'notes': notes})
    out.sort(key=lambda m: -m['notes'])
    return {'note_types': out, 'missing': missing}

def known_words(col, request: Dict[str, Any]) -> Dict[str, Any]:
    deck_ids = request.get('decks') or []
    if not isinstance(deck_ids, list) or len(deck_ids) > MAX_DECKS:
        raise RequestError(400, 'bad_deck')
    ids, missing = _chosen_ids(col, deck_ids, bool(request.get('subdecks', True)))
    rule = rule_query(request.get('rule') or {})
    mapping = {}
    for key, pair in (request.get('fields') or {}).items():
        if not isinstance(pair, dict):
            raise RequestError(400, 'bad_fields')
        try:
            mapping[int(key)] = (str(pair.get('expression') or ''), str(pair.get('reading') or ''))
        except (TypeError, ValueError):
            raise RequestError(400, 'bad_fields')
    if not ids:
        return {'query': '', 'notes': 0, 'skipped': 0, 'missing': missing, 'entries': []}
    query = _deck_query(ids) + (' ' + rule if rule else '')
    nids = sorted(set((int(n) for n in col.find_notes(query))))
    entries, skipped = ([], 0)
    places = {}
    for chunk in _chunks(nids):
        marks = ','.join('?' * len(chunk))
        for nid, mid, flds in col.db.all(f'select id, mid, flds from notes where id in ({marks})', *chunk):
            mid = int(mid)
            pair = mapping.get(mid)
            if pair is None or not pair[0]:
                skipped += 1
                continue
            if mid not in places:
                names = _model_fields(col, mid)
                places[mid] = (names.index(pair[0]) if pair[0] in names else None, names.index(pair[1]) if pair[1] in names else None)
            word_at, reading_at = places[mid]
            values = str(flds).split('\x1f')
            if word_at is None or word_at >= len(values):
                skipped += 1
                continue
            reading = values[reading_at] if reading_at is not None and reading_at < len(values) else ''
            entries.append([values[word_at], reading])
    return {'query': query, 'notes': len(nids), 'skipped': skipped, 'missing': missing, 'entries': entries}

def _manifest_version() -> str:
    try:
        with open(os.path.join(os.path.dirname(__file__), 'manifest.json'), encoding='utf-8') as fh:
            return str(json.load(fh).get('human_version') or '')
    except Exception:
        return ''

def _default_run_on_main(fn: Callable[[], Any]) -> Any:
    from aqt import mw
    done = threading.Event()
    box = {}

    def call() -> None:
        try:
            box['value'] = fn()
        except BaseException as error:
            box['error'] = error
        finally:
            done.set()
    mw.taskman.run_on_main(call)
    if not done.wait(MAIN_THREAD_TIMEOUT_S):
        raise RequestError(503, 'anki_busy')
    if 'error' in box:
        raise box['error']
    return box.get('value')

def _default_collection():
    from aqt import mw
    return (getattr(mw, 'col', None), getattr(getattr(mw, 'pm', None), 'name', None) or '')

def make_handler(port: int, get_collection: Callable[[], Any], run_on_main: Callable[[Callable[[], Any]], Any]):
    allowed_hosts = {f'127.0.0.1:{port}', f'localhost:{port}'}

    class Handler(BaseHTTPRequestHandler):
        server_version = 'AobanaReibun'
        sys_version = ''

        def log_message(self, fmt, *args) -> None:
            pass

        def _send(self, status: int, body: Dict[str, Any]) -> None:
            data = json.dumps(body, ensure_ascii=False).encode('utf-8')
            self.send_response(status)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Content-Length', str(len(data)))
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            self.wfile.write(data)

        def _guard(self) -> None:
            if self.headers.get('X-Aobana') != '1':
                raise RequestError(403, 'forbidden')
            if (self.headers.get('Host') or '').lower() not in allowed_hosts:
                raise RequestError(403, 'forbidden')
            if self.headers.get('Origin'):
                raise RequestError(403, 'forbidden')

        def _body(self) -> Dict[str, Any]:
            try:
                length = int(self.headers.get('Content-Length') or 0)
            except ValueError:
                raise RequestError(400, 'bad_request')
            if length > MAX_BODY:
                raise RequestError(413, 'too_large')
            try:
                data = json.loads(self.rfile.read(length).decode('utf-8') or '{}')
            except (UnicodeDecodeError, ValueError):
                raise RequestError(400, 'bad_request')
            if not isinstance(data, dict):
                raise RequestError(400, 'bad_request')
            return data

        def _answer(self, method: str) -> None:
            try:
                self._guard()
                path = self.path.split('?', 1)[0]
                routes = {('GET', '/hello'), ('GET', '/decks'), ('POST', '/fields'), ('POST', '/words')}
                if (method, path) not in routes:
                    raise RequestError(404, 'not_found')
                body = self._body() if method == 'POST' else {}

                def work():
                    col, profile = get_collection()
                    if path == '/hello':
                        return {'app': APP, 'version': _manifest_version(), 'protocol': PROTOCOL, 'profile': profile, 'ready': col is not None}
                    if col is None:
                        raise RequestError(503, 'no_collection')
                    if path == '/decks':
                        return {'profile': profile, 'decks': list_decks(col)}
                    if path == '/fields':
                        got = note_types(col, body.get('decks') or [], bool(body.get('subdecks', True)))
                        got['profile'] = profile
                        return got
                    got = known_words(col, body)
                    got['profile'] = profile
                    return got
                self._send(200, run_on_main(work))
            except RequestError as error:
                self._send(error.status, {'error': error.code})
            except Exception as error:
                try:
                    from .logger import get_logger
                    get_logger().exception('known words: %s', error)
                except Exception:
                    pass
                self._send(500, {'error': 'internal'})

        def do_GET(self) -> None:
            self._answer('GET')

        def do_POST(self) -> None:
            self._answer('POST')
    return Handler

class _Server(ThreadingHTTPServer):
    allow_reuse_address = os.name != 'nt'
    daemon_threads = True

    def server_bind(self) -> None:
        if os.name == 'nt' and hasattr(socket, 'SO_EXCLUSIVEADDRUSE'):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()
LAST_ERROR = ''

def start(port: int=DEFAULT_PORT, get_collection=None, run_on_main=None) -> bool:
    global _SERVER, LAST_ERROR
    with _LOCK:
        if _SERVER is not None:
            if _SERVER[2] == port:
                return True
            _stop_locked()
        handler = make_handler(port, get_collection or _default_collection, run_on_main or _default_run_on_main)
        try:
            server = _Server(('127.0.0.1', port), handler)
        except OSError as error:
            LAST_ERROR = port_taken_message(port)
            try:
                from .logger import get_logger
                get_logger().error('known words: %s (%s)', LAST_ERROR, error)
            except Exception:
                pass
            return False
        LAST_ERROR = ''
        server.daemon_threads = True
        thread = threading.Thread(target=server.serve_forever, name='aobana-reibun-known-words', daemon=True)
        thread.start()
        _SERVER = (server, thread, port)
        return True

def port_taken_message(port: int) -> str:
    return 'Port %d is taken by another program, so Aobana cannot read your decks for known words. Choose another port under Known words for Aobana, and set "reibun_port" to it in Aobana\'s config.json.' % port

def _stop_locked() -> None:
    global _SERVER
    if _SERVER is None:
        return
    server, thread, _ = _SERVER
    _SERVER = None
    server.shutdown()
    server.server_close()
    thread.join(5)

def stop() -> None:
    with _LOCK:
        _stop_locked()

def running_port() -> Optional[int]:
    return _SERVER[2] if _SERVER else None

def apply_config(cfg: Dict[str, Any]) -> bool:
    global LAST_ERROR
    if not bool(cfg.get('known_words_server', True)):
        stop()
        LAST_ERROR = ''
        return False
    try:
        port = int(cfg.get('known_words_port', DEFAULT_PORT))
    except (TypeError, ValueError):
        port = DEFAULT_PORT
    if not 1024 <= port <= 65535:
        port = DEFAULT_PORT
    return start(port)
