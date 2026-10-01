from __future__ import annotations
import json
import random
import re
import time
import threading
from email.utils import parsedate_to_datetime
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse
import requests
_DOWNLOAD_USER_AGENT = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0 Safari/537.36'
_API_USER_AGENT = 'AobanaReibun/1.0 (+https://nadeshiko.co/docs/api)'
_SORT_MODES = frozenset({'RELEVANCE', 'ASC', 'DESC', 'TIME_ASC', 'TIME_DESC', 'RANDOM'})
_MAX_TAKE = 50
_MEDIA_PUBLIC_ID_RE = re.compile('^[A-Za-z0-9_-]{12}$')
_RATE_LIMIT_RETRIES = 2
_RATE_LIMIT_WAIT_S = 20.0
_SERVICE_RETRIES = 2
_SEARCH_SPACING_S = 0.41

class SearchLimiter:

    def __init__(self, clock=time.monotonic, sleep=time.sleep) -> None:
        self._clock = clock
        self._sleep = sleep
        self._lock = threading.Lock()
        self._last = None

    def wait(self) -> None:
        with self._lock:
            if self._last is not None:
                delay = self._last + _SEARCH_SPACING_S - self._clock()
                if delay > 0:
                    self._sleep(delay)
            self._last = self._clock()
_SEARCH_LIMITER = SearchLimiter()

def _retry_after(resp, default: float, wall_clock) -> float:
    value = str(getattr(resp, 'headers', {}).get('Retry-After', '') or '').strip()
    try:
        return max(0.0, float(value))
    except ValueError:
        try:
            return max(0.0, parsedate_to_datetime(value).timestamp() - wall_clock())
        except (ValueError, TypeError, OverflowError):
            return default
_FATAL_STATUSES = frozenset({401, 403})
_FATAL_CODES = frozenset({'QUOTA_EXCEEDED'})

class NadeshikoApiError(Exception):

    def __init__(self, message: str, status: int=0, code: str='') -> None:
        super().__init__(message)
        self.status = status
        self.code = code

    @property
    def fatal(self) -> bool:
        return self.status in _FATAL_STATUSES or self.code in _FATAL_CODES or self.rate_limited

    @property
    def rate_limited(self) -> bool:
        return self.status == 429 and self.code != 'QUOTA_EXCEEDED'

class NadeshikoCancelled(NadeshikoApiError):
    pass

def _error_code(body: str) -> str:
    try:
        data = json.loads(body)
        if isinstance(data, dict):
            return str(data.get('code', '') or '').strip()
    except Exception:
        pass
    return ''

def _format_api_error(status: int, body: str) -> str:
    try:
        data = json.loads(body)
        if isinstance(data, dict):
            code = str(data.get('code', '') or '').strip()
            detail = str(data.get('detail', '') or data.get('title', '') or '').strip()
            if code and detail:
                return f'HTTP {status} {code}: {detail}'
            if code or detail:
                return f'HTTP {status}: {code or detail}'
    except Exception:
        pass
    return f'HTTP {status}: {body}'

class NadeshikoApiClient:

    def __init__(self, api_key: str, base_url: str='https://api.nadeshiko.co/v1', *, limiter=None, sleep=time.sleep, wall_clock=time.time, session=None, stop: Optional[threading.Event]=None) -> None:
        if not api_key:
            raise NadeshikoApiError('Missing Nadeshiko API key')
        self._base_url = base_url.rstrip('/')
        self._base_host = (urlparse(self._base_url).hostname or '').lower()
        self._auth_header = f'Bearer {api_key}'
        self._limiter = limiter if limiter is not None else _SEARCH_LIMITER
        self._sleep = sleep
        self._stop = stop
        self._wall_clock = wall_clock
        self._session = session if session is not None else requests.Session()
        self._session.headers.update({'Authorization': self._auth_header, 'Content-Type': 'application/json', 'Accept': 'application/json', 'User-Agent': _API_USER_AGENT})

    def search(self, query: str, take: int=1, sort_mode: Optional[str]=None, seed: Optional[int]=None, exact_match: bool=False, min_length: Optional[int]=None, max_length: Optional[int]=None, category: Optional[List[str]]=None, media_include: Optional[List[str]]=None, media_exclude: Optional[List[str]]=None, cursor: Optional[str]=None, include: Optional[List[str]]=None, timeout: float=30.0) -> Dict[str, Any]:
        search_query: Dict[str, Any] = {'search': query}
        if exact_match:
            search_query['exactMatch'] = True
        payload: Dict[str, Any] = {'query': search_query, 'take': max(1, min(int(take or 1), _MAX_TAKE))}
        if sort_mode in _SORT_MODES:
            sort: Dict[str, Any] = {'mode': sort_mode}
            if sort_mode == 'RANDOM':
                sort['seed'] = int(seed) if isinstance(seed, int) and seed >= 0 else random.randrange(2 ** 31)
            payload['sort'] = sort
        if cursor:
            payload['cursor'] = cursor
        if include:
            payload['include'] = list(include)
        filters: Dict[str, Any] = {}
        if isinstance(min_length, int) and min_length > 0 or (isinstance(max_length, int) and max_length > 0):
            length_filter: Dict[str, int] = {}
            if isinstance(min_length, int) and min_length > 0:
                length_filter['min'] = min_length
            if isinstance(max_length, int) and max_length > 0:
                length_filter['max'] = max_length
            filters['segmentLengthChars'] = length_filter
        if category:
            filters['category'] = category
        media_filter: Dict[str, Any] = {}
        include_ids = _valid_media_ids(media_include)
        exclude_ids = _valid_media_ids(media_exclude)
        if include_ids:
            media_filter['include'] = [{'mediaPublicId': mid} for mid in include_ids]
        if exclude_ids:
            media_filter['exclude'] = [{'mediaPublicId': mid} for mid in exclude_ids]
        if media_filter:
            filters['media'] = media_filter
        if filters:
            payload['filters'] = filters
        url = f'{self._base_url}/search'
        body = json.dumps(payload)
        rate_retries = service_retries = 0
        while True:
            self._check_stop()
            self._limiter.wait()
            self._check_stop()
            resp = self._session.post(url, data=body, timeout=timeout)
            if resp.status_code == 200:
                return resp.json() or {}
            code = _error_code(resp.text)
            if resp.status_code == 429 and code != 'QUOTA_EXCEEDED' and (rate_retries < _RATE_LIMIT_RETRIES):
                rate_retries += 1
                self._pause(_retry_after(resp, _RATE_LIMIT_WAIT_S * rate_retries, self._wall_clock))
                continue
            if resp.status_code == 503 and service_retries < _SERVICE_RETRIES:
                service_retries += 1
                self._pause(_retry_after(resp, 2.0, self._wall_clock))
                continue
            raise NadeshikoApiError(_format_api_error(resp.status_code, resp.text), status=resp.status_code, code=code)

    def _check_stop(self) -> None:
        if self._stop is not None and self._stop.is_set():
            raise NadeshikoCancelled('Cancelled.')

    def _pause(self, seconds: float) -> None:
        if self._stop is not None:
            self._stop.wait(seconds)
        else:
            self._sleep(seconds)
        self._check_stop()

    def search_pages(self, max_pages: int=2, **kwargs: Any) -> Dict[str, Any]:
        first: Dict[str, Any] = {}
        segments: List[Dict[str, Any]] = []
        cursor: Optional[str] = kwargs.pop('cursor', None)
        for _ in range(max(1, int(max_pages))):
            data = self.search(cursor=cursor, **kwargs)
            if not first:
                first = data
            segments.extend(data.get('segments') or [])
            pagination = data.get('pagination') or {}
            cursor = pagination.get('cursor')
            if not pagination.get('hasMore') or not cursor:
                break
        if first:
            first = dict(first)
            first['segments'] = segments
        return first

    def download(self, url: str, timeout: float=60.0) -> bytes:
        self._check_stop()
        headers = {'User-Agent': _DOWNLOAD_USER_AGENT, 'Accept': '*/*'}
        host = (urlparse(url).hostname or '').lower()
        if host == self._base_host:
            headers['Authorization'] = self._auth_header
        resp = requests.get(url, headers=headers, timeout=timeout)
        resp.raise_for_status()
        return resp.content

def _valid_media_ids(values: Optional[List[str]]) -> List[str]:
    out: List[str] = []
    for value in values or []:
        text = str(value or '').strip()
        if _MEDIA_PUBLIC_ID_RE.match(text):
            out.append(text)
    return out
