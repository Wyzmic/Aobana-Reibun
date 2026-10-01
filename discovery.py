from __future__ import annotations
"Finding the Aobana program to start: its folder and the Python that runs it.\n\nAobana 1.7 and later is the `aobana` package, started as `python -m aobana.server.app` from\nthe folder that holds it. Nothing here imports Qt or Anki, so the tests run it directly.\n\nWhere an installed copy lives, per platform, from Aobana's own installers:\n\n* Windows: the Inno Setup installer registers AppId {dedba003-ceb5-4880-a40f-660dc88c9345}\n  under ...\\CurrentVersion\\Uninstall\\<AppId>_is1, with the folder in InstallLocation.\n  A per-user install writes HKEY_CURRENT_USER, an all-users one HKEY_LOCAL_MACHINE, and the\n  folder is whatever the user picked, so it is read from there, never guessed. The runtime is\n  the bundled python\\python.exe (the launcher runs pythonw.exe from the same folder).\n* Linux (tar.gz): install.sh copies the program to $XDG_DATA_HOME/aobana-app (default\n  ~/.local/share/aobana-app); the package and python/bin/python3 are in its app/ folder.\n  An AppImage mounts only while it runs, so it has no fixed folder to find.\n* macOS: Aobana.app/Contents/Resources/app, in /Applications or ~/Applications.\n\nThe installed program folder is not the user's data folder: settings, databases and the\nlibrary stay in the per-user data directory Aobana itself chooses (aobana/paths.py). This\nmodule only ever needs the program.\n\nAnki's own interpreter is never used: it lacks Aobana's dependencies (Flask, SudachiPy).\n"
import os
import shutil
import sys
from dataclasses import dataclass
from typing import Callable, Iterable, List, Optional, Tuple
INNO_UNINSTALL_KEY = 'Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\{dedba003-ceb5-4880-a40f-660dc88c9345}_is1'
DEFAULT_PYTHON = 'python'

class DiscoveryError(Exception):
    pass

@dataclass(frozen=True)
class Install:
    program_dir: str
    python: str
    where: str

def is_program_dir(folder: str) -> bool:
    return bool(folder) and os.path.isfile(os.path.join(folder, 'aobana', '__main__.py'))

def is_old_aobana(folder: str) -> bool:
    return bool(folder) and os.path.isfile(os.path.join(folder, 'app.py')) and (not is_program_dir(folder))

def bundled_python(folder: str) -> Optional[str]:
    for parts in (('python', 'python.exe'), ('python', 'bin', 'python3')):
        path = os.path.join(folder, *parts)
        if os.path.isfile(path):
            return path
    return None

def resolve_command(python: str, which: Callable[[str], Optional[str]]=shutil.which) -> str:
    if os.path.dirname(python):
        return python
    found = which(python)
    if not found:
        raise DiscoveryError('The Python command "%s" was not found.\n\nSet Python command in Settings to the Python that runs your Aobana copy.' % python)
    return found

def _norm(path: str) -> str:
    return os.path.normcase(os.path.normpath(os.path.abspath(path)))

def windows_registry_dirs(winreg_module=None) -> List[Tuple[str, str]]:
    if winreg_module is None:
        if os.name != 'nt':
            return []
        import winreg as winreg_module
    wr = winreg_module
    out: List[Tuple[str, str]] = []
    hives = ((wr.HKEY_CURRENT_USER, 'per-user install'), (wr.HKEY_LOCAL_MACHINE, 'all-users install'))
    views = (getattr(wr, 'KEY_WOW64_64KEY', 0), getattr(wr, 'KEY_WOW64_32KEY', 0))
    for hive, where in hives:
        for view in views:
            try:
                with wr.OpenKey(hive, INNO_UNINSTALL_KEY, 0, wr.KEY_READ | view) as key:
                    value, _kind = wr.QueryValueEx(key, 'InstallLocation')
            except OSError:
                continue
            value = str(value or '').strip()
            if value:
                out.append((value.rstrip('\\/') or value, where))
    return out

def unix_dirs(environ=None, home: Optional[str]=None, platform: Optional[str]=None) -> List[Tuple[str, str]]:
    environ = os.environ if environ is None else environ
    home = home if home is not None else os.path.expanduser('~')
    platform = platform or sys.platform
    if platform == 'darwin':
        return [('/Applications/Aobana.app/Contents/Resources/app', 'Applications folder'), (os.path.join(home, 'Applications', 'Aobana.app', 'Contents', 'Resources', 'app'), 'your Applications folder')]
    if platform.startswith('linux') or platform.startswith('freebsd'):
        data = environ.get('XDG_DATA_HOME') or os.path.join(home, '.local', 'share')
        return [(os.path.join(data, 'aobana-app', 'app'), 'Linux install')]
    return []

def find_installs(candidates: Optional[Iterable[Tuple[str, str]]]=None) -> List[Install]:
    if candidates is None:
        candidates = list(windows_registry_dirs()) + unix_dirs()
    seen = set()
    out: List[Install] = []
    for folder, where in candidates:
        if not folder:
            continue
        key = _norm(folder)
        if key in seen:
            continue
        seen.add(key)
        python = bundled_python(folder)
        if is_program_dir(folder) and python:
            out.append(Install(os.path.normpath(folder), python, where))
    return out

def resolve(project_dir: str, python_setting: str=DEFAULT_PYTHON, finder: Callable[[], List[Install]]=find_installs, old_candidates: Optional[Callable[[], List[str]]]=None) -> Install:
    python_setting = str(python_setting or '').strip() or DEFAULT_PYTHON
    folder = str(project_dir or '').strip()
    if folder:
        if not os.path.isdir(folder):
            raise DiscoveryError('The Aobana folder set in Settings does not exist:\n%s\n\nClear the setting to use the installed Aobana, or choose its new folder.' % folder)
        if is_old_aobana(folder):
            raise DiscoveryError('The Aobana in the folder set in Settings is older than 1.7:\n%s\n\nUpdate Aobana; this add-on starts version 1.7 and later.' % folder)
        if not is_program_dir(folder):
            raise DiscoveryError('No Aobana program in the folder set in Settings:\n%s\n\nChoose the folder that holds the aobana folder, or clear the setting to use the installed Aobana.' % folder)
        python = python_setting
        if python_setting == DEFAULT_PYTHON:
            python = bundled_python(folder) or DEFAULT_PYTHON
        return Install(os.path.normpath(folder), resolve_command(python), 'the folder set in Settings')
    installs = finder()
    if len(installs) == 1:
        found = installs[0]
        if python_setting != DEFAULT_PYTHON:
            return Install(found.program_dir, resolve_command(python_setting), found.where)
        return found
    if len(installs) > 1:
        listing = '\n'.join(('  %s (%s)' % (i.program_dir, i.where) for i in installs))
        raise DiscoveryError('More than one Aobana is installed:\n%s\n\nChoose which one to use in Settings (Aobana folder).' % listing)
    old = [f for f in (old_candidates() if old_candidates else _registered_old()) if is_old_aobana(f)]
    if old:
        raise DiscoveryError('The installed Aobana is older than 1.7:\n%s\n\nUpdate Aobana; this add-on starts version 1.7 and later.' % old[0])
    raise DiscoveryError('No installed Aobana was found.\n\nInstall Aobana 1.7 or later, or set the Aobana folder in Settings (for a portable or source copy).')

def _registered_old() -> List[str]:
    try:
        return [folder for folder, _where in windows_registry_dirs() + unix_dirs()]
    except Exception:
        return []
