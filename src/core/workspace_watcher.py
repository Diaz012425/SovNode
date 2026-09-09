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

# ES: Mismas extensiones que SUPPORTED_DROP_EXTENSIONS en sovnode_qt.py, para que
# "indexado por drag-and-drop" e "indexado por workspace" sean intercambiables.
# EN: Same extensions as SUPPORTED_DROP_EXTENSIONS in sovnode_qt.py, so
# "indexed by drag-and-drop" and "indexed by workspace" are interchangeable.
DEFAULT_WATCHED_EXTENSIONS = {".py", ".txt", ".md", ".json", ".csv"}

# ES: Directorios sin valor semántico que solo generan ruido/costo de escaneo.
# EN: Directories with no semantic value that only add scan noise/cost.
DEFAULT_EXCLUDED_DIR_NAMES = {
    ".git", "__pycache__", "node_modules", "venv", ".venv", "env",
    "dist", "build", ".idea", ".vscode", "site-packages", ".mypy_cache",
    ".pytest_cache",
}

# ES: Archivos propios del proyecto que nunca deben indexarse como "conocimiento"
# (evita que SovNode se indexe a sí mismo releyendo su propia memoria).
# EN: The project's own files that should never be indexed as "knowledge"
# (avoids SovNode indexing itself by re-reading its own memory).
DEFAULT_EXCLUDED_SUFFIXES = {".db", ".wal", ".pyc"}

# ES: Un archivo más grande que esto se salta, para no bloquear un ciclo de
# escaneo entero embebiendo megabytes de texto.
# EN: A file larger than this is skipped, to avoid blocking an entire scan
# cycle embedding megabytes of text.
DEFAULT_MAX_FILE_SIZE_BYTES = 2 * 1024 * 1024  # 2 MB


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

    def _should_watch_file(self, full_path: str) -> bool:
        lower = full_path.lower()
        if any(lower.endswith(suf) for suf in self.excluded_suffixes):
            return False
        _, ext = os.path.splitext(lower)
        if ext not in self.watched_extensions:
            return False
        try:
            if os.path.getsize(full_path) > self.max_file_size_bytes:
                return False
        except OSError:
            return False
        return True

    def _walk_current_files(self) -> Dict[str, float]:
        """ES: Devuelve {ruta_absoluta: mtime} de todo archivo elegible bajo las raíces vigiladas, ahora mismo.
        EN: Returns {absolute_path: mtime} for every eligible file under the watched roots, right now."""
        current: Dict[str, float] = {}
        for root in list(self._roots):
            if not os.path.isdir(root):
                continue
            for dirpath, dirnames, filenames in os.walk(root):
                dirnames[:] = [
                    d for d in dirnames if not self._is_excluded_dir(d)
                ]
                for fname in filenames:
                    full_path = os.path.normpath(os.path.join(dirpath, fname))
                    if not self._should_watch_file(full_path):
                        continue
                    try:
                        mtime = os.path.getmtime(full_path)
                    except OSError:
                        continue
                    current[full_path] = mtime
        return current

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
                # ES: Primera vez que se ve: se registra sin confirmar y se
                # reporta recién en la próxima pasada si el mtime no cambió.
                # EN: First time seen: registered unconfirmed, only reported
                # on the next pass if the mtime hasn't changed.
                self._known[fp] = _KnownFile(mtime=mtime, confirmed=False)
                continue
            if known.mtime != mtime:
                # ES: Cambió respecto a la última pasada: reinicia el debounce.
                # EN: Changed since the last pass: restarts the debounce.
                self._known[fp] = _KnownFile(mtime=mtime, confirmed=False)
                continue
            if not known.confirmed:
                # ES: Mismo mtime que la pasada anterior: confirmado y se
                # reporta una única vez.
                # EN: Same mtime as the previous pass: confirmed and
                # reported exactly once.
                known.confirmed = True
                to_index.append(fp)

        return to_index, to_remove
