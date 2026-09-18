# Copyright (c) 2026 Stephen Díaz
# SovNode - Local Desktop AI Application
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program. If not, see <https://www.gnu.org/licenses/>.
"""
SovNode — Workspace File Watcher
=================================
ES: Vigilancia en segundo plano de carpetas "Workspace" (agregadas desde la UI)
para mantener el RAG vectorial de workspace sincronizado con el disco: reindexa
archivos cuando cambian y los retira cuando se borran, sin intervención manual.

Deliberadamente NO usa `watchdog` u otra librería con inotify/ReadDirectoryChangesW
(esta sesión no puede instalar paquetes en la máquina del usuario); en su lugar,
polling plano (`os.walk` + `mtime`) con debounce de dos pasadas.

Módulo Python puro, sin import de Qt — así `WorkspaceScanner` se puede testear
con una carpeta temporal real, sin necesitar QApplication.

EN: Background watcher for "Workspace" folders (added from the UI) that keeps
the workspace vector RAG in sync with disk: reindexes files when they change
and removes them when deleted, with no manual action needed.

Deliberately does NOT use `watchdog` or another inotify/ReadDirectoryChangesW
library (this session cannot install packages on the user's machine); instead,
flat polling (`os.walk` + `mtime`) with two-pass debouncing.

Pure Python module, no Qt import — so `WorkspaceScanner` can be unit-tested
against a real temp folder without needing QApplication.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from typing import Dict, List, Set, Tuple

DEFAULT_WATCHED_EXTENSIONS = {".py", ".txt", ".md", ".json", ".csv"}

DEFAULT_EXCLUDED_DIR_NAMES = {
    ".git", "__pycache__", "node_modules", "venv", ".venv", "env",
    "dist", "build", ".idea", ".vscode", "site-packages", ".mypy_cache",
    ".pytest_cache",
}

DEFAULT_EXCLUDED_SUFFIXES = {".db", ".wal", ".pyc"}

DEFAULT_MAX_FILE_SIZE_BYTES = 2 * 1024 * 1024


@dataclass
class _KnownFile:
    mtime: float
    confirmed: bool = False


@dataclass
class WorkspaceScanner:
    """
    ES: Escaneo por polling de un conjunto de carpetas raíz ("workspaces"), con
    debounce de dos pasadas: un archivo nuevo o modificado se reporta listo
    para (re)indexar solo cuando su `mtime` se observa igual en dos
    `scan_once()` consecutivos (evita reindexar a mitad de una escritura larga).

    Uso (ver WorkspaceWatcherWorker en sovnode_qt.py):
        scanner = WorkspaceScanner()
        scanner.add_root("C:/Users/.../mi_proyecto")
        while True:
            to_index, to_remove = scanner.scan_once()
            ...
            time.sleep(intervalo)

    EN: Polling scan over a set of root folders ("workspaces"), with two-pass
    debouncing: a new or modified file is only reported ready to (re)index once
    its `mtime` is observed unchanged across two consecutive `scan_once()`
    calls (avoids reindexing mid-write on a long save).
    """

    watched_extensions: Set[str] = field(
        default_factory=lambda: set(DEFAULT_WATCHED_EXTENSIONS)
    )
    excluded_dir_names: Set[str] = field(
        default_factory=lambda: set(DEFAULT_EXCLUDED_DIR_NAMES)
    )
    excluded_suffixes: Set[str] = field(
        default_factory=lambda: set(DEFAULT_EXCLUDED_SUFFIXES)
    )
    max_file_size_bytes: int = DEFAULT_MAX_FILE_SIZE_BYTES

    _roots: Set[str] = field(default_factory=set, init=False, repr=False)
    _known: Dict[str, _KnownFile] = field(default_factory=dict, init=False, repr=False)

    def add_root(self, path: str) -> bool:
        """ES: Agrega una carpeta a vigilar. No valida existencia en disco a propósito;
        scan_once() simplemente no encuentra nada bajo una ruta inválida y sigue con las demás.
        EN: Adds a folder to watch. Deliberately doesn't validate it exists on disk;
        scan_once() just finds nothing under an invalid path and continues with the rest."""
        norm = os.path.normpath(path)
        if norm in self._roots:
            return False
        self._roots.add(norm)
        return True

    def remove_root(self, path: str) -> List[str]:
        """ES: Quita una carpeta vigilada y devuelve las rutas que estaban indexadas bajo
        ella; el llamador es responsable de retirarlas del índice vectorial.
        EN: Removes a watched folder and returns the paths that were indexed under
        it; the caller is responsible for removing them from the vector index."""
        norm = os.path.normpath(path)
        self._roots.discard(norm)
        prefix = norm + os.sep
        orphaned = [
            fp for fp in self._known
            if fp == norm or fp.startswith(prefix)
        ]
        for fp in orphaned:
            del self._known[fp]
        return orphaned

    @property
    def roots(self) -> List[str]:
        return sorted(self._roots)

    def _is_excluded_dir(self, dirname: str) -> bool:
        return dirname in self.excluded_dir_names or dirname.startswith(".")

    def _is_watched_extension(self, full_path: str) -> bool:
        lower = full_path.lower()
        if any(lower.endswith(suf) for suf in self.excluded_suffixes):
            return False
        _, ext = os.path.splitext(lower)
        return ext in self.watched_extensions

    def _walk_current_files(self) -> Dict[str, float]:
        """ES: Devuelve {ruta_absoluta: mtime} de todo archivo elegible bajo las raíces vigiladas, ahora mismo.
        EN: Returns {absolute_path: mtime} for every eligible file under the watched roots, right now."""
        current: Dict[str, float] = {}
        for root in list(self._roots):
            if not os.path.isdir(root):
                continue
            self._scan_dir_into(root, current)
        return current

    def _scan_dir_into(self, dirpath: str, current: Dict[str, float]) -> None:
        """
        ES: Recorrido recursivo con os.scandir en vez de os.walk (2026-09-09).
        os.walk YA usa scandir por dentro para distinguir archivo/carpeta,
        pero solo le entrega al llamador los nombres en texto plano — el
        `os.DirEntry` con el resultado de stat() ya resuelto (FindNextFileW
        en Windows; lstat en POSIX) se descarta antes de devolver el
        control acá. Este código, con os.walk, volvía a pagar esa
        información con dos syscalls MÁS por archivo elegible
        (os.path.getsize + os.path.getmtime) — en Windows, donde cada
        syscall pesa más que en Linux/Mac, eso es plata tirada en cada
        ciclo de polling (cada 15s, para SIEMPRE, mientras la app esté
        abierta). Iterando con `os.scandir` directo y llamando a
        `entry.stat()` una sola vez por archivo, se reutiliza el resultado
        que el propio listado del directorio ya trajo — mismo conjunto de
        archivos, mismos mtimes, sin ninguna llamada extra al sistema de
        archivos. El impacto crece con el tamaño del Workspace: en una
        carpeta de demo con un puñado de archivos no se nota, en un
        proyecto real con miles de archivos sí.

        Semántica de symlinks preservada IDÉNTICA a la versión con
        os.walk(root) (followlinks=False por defecto): un symlink que
        apunta a una carpeta se detecta como carpeta pero NUNCA se
        recorre (antes tampoco se recorría); un symlink que apunta a un
        archivo se trata como archivo normal y su mtime se seguía a
        través del link (igual que os.path.getmtime, que sigue symlinks
        por defecto).

        EN: Recursive walk with os.scandir instead of os.walk
        (2026-09-09). os.walk already uses scandir internally to tell
        files from directories, but only hands the caller plain-text
        names — the `os.DirEntry` with its already-resolved stat()
        result (FindNextFileW on Windows; lstat on POSIX) is discarded
        before control returns here. This code, via os.walk, used to pay
        for that information again with two MORE syscalls per eligible
        file (os.path.getsize + os.path.getmtime) — on Windows, where
        each syscall costs more than on Linux/Mac, that's money burned on
        every polling cycle (every 15s, forever, while the app is open).
        Iterating with `os.scandir` directly and calling `entry.stat()`
        once per file reuses the result the directory listing itself
        already fetched — same set of files, same mtimes, zero extra
        filesystem calls. The impact scales with Workspace size: a demo
        folder with a handful of files won't show it, a real project with
        thousands of files will.

        Symlink semantics preserved IDENTICAL to the os.walk(root)
        version (followlinks=False by default): a symlink pointing to a
        directory is detected as a directory but is NEVER recursed into
        (it wasn't before either); a symlink pointing to a file is
        treated as a normal file and its mtime is followed through the
        link (same as os.path.getmtime, which follows symlinks by
        default).
        """
        try:
            entries = os.scandir(dirpath)
        except OSError:
            return
        with entries:
            for entry in entries:
                try:
                    is_symlink = entry.is_symlink()
                    is_dir = entry.is_dir()
                except OSError:
                    continue
                if is_dir:
                    if is_symlink or self._is_excluded_dir(entry.name):
                        continue
                    self._scan_dir_into(entry.path, current)
                    continue

                full_path = os.path.normpath(entry.path)
                if not self._is_watched_extension(full_path):
                    continue
                try:
                    st = entry.stat()
                except OSError:
                    continue
                if st.st_size > self.max_file_size_bytes:
                    continue
                current[full_path] = st.st_mtime

    def scan_once(self) -> Tuple[List[str], List[str]]:
        """
        ES: Ejecuta una pasada de escaneo. Devuelve (to_index, to_remove): rutas
        listas para (re)indexar (mtime ya estable en dos pasadas) y rutas que
        desaparecieron del filesystem. No lee contenido de archivo ni llama al
        orquestador — el llamador decide qué hacer con las rutas devueltas.
        EN: Runs one scan pass. Returns (to_index, to_remove): paths ready to
        (re)index (mtime stable across two passes) and paths that vanished from
        disk. Doesn't read file content or call the orchestrator — the caller
        decides what to do with the returned paths.
        """
        current = self._walk_current_files()

        to_remove = [fp for fp in self._known if fp not in current]
        for fp in to_remove:
            del self._known[fp]

        to_index: List[str] = []
        for fp, mtime in current.items():
            known = self._known.get(fp)
            if known is None:
                self._known[fp] = _KnownFile(mtime=mtime, confirmed=False)
                continue
            if known.mtime != mtime:
                self._known[fp] = _KnownFile(mtime=mtime, confirmed=False)
                continue
            if not known.confirmed:
                known.confirmed = True
                to_index.append(fp)

        return to_index, to_remove
