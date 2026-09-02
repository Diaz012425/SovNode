"""
SovNode — Workspace File Watcher
=================================
Vigilancia en segundo plano de carpetas de "Workspaces" (agregadas por el
usuario desde la UI) para mantener `Orchestrator.workspace_vector_rag`
(ver rag_faiss.py / orchestrator.py) sincronizado con el disco sin
intervención manual — soltar un archivo en el chat ya lo indexaba, pero
solo UNA vez; esto lo re-indexa cuando cambia y lo retira cuando se borra,
sin que el usuario tenga que volver a arrastrarlo.

Deliberadamente NO usa `watchdog` (o cualquier otra librería de sistema de
archivos con inotify/ReadDirectoryChangesW): esta sesión no puede instalar
paquetes en la máquina del usuario (sin `device_bash` disponible), así que
un `import watchdog` roto silenciosamente en el primer arranque sin esa
dependencia sería peor que no tener la función. En su lugar: polling plano
(`os.walk` + comparación de `mtime`), con doble confirmación (debounce) y
exclusiones — ver `WorkspaceScanner.scan_once()`.

Este módulo es Python puro, SIN import de Qt — `WorkspaceWatcherWorker`
(sovnode_qt.py) es el único punto donde esto se envuelve en un QThread.
Eso permite probar `WorkspaceScanner` con un test funcional simple contra
una carpeta temporal real, sin necesitar QApplication.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from typing import Dict, List, Set, Tuple

# Mismas extensiones que SUPPORTED_DROP_EXTENSIONS en sovnode_qt.py — un
# archivo soltado a mano y uno descubierto por el watcher deben pasar el
# mismo filtro, para que "indexado por drag-and-drop" y "indexado por
# workspace" sean intercambiables desde el punto de vista de RAG.
DEFAULT_WATCHED_EXTENSIONS = {".py", ".txt", ".md", ".json", ".csv"}

# Directorios que nunca aportan valor semántico y sí generan ruido/costo
# real de escaneo — entorno virtuales y control de versiones son los
# casos más comunes en un proyecto de código como el que el propio
# SovNode vive.
DEFAULT_EXCLUDED_DIR_NAMES = {
    ".git", "__pycache__", "node_modules", "venv", ".venv", "env",
    "dist", "build", ".idea", ".vscode", "site-packages", ".mypy_cache",
    ".pytest_cache",
}

# Archivos propios del proyecto (bases de datos/WAL/bytecode) que NUNCA
# deben terminar indexados como "conocimiento" — evita, entre otras
# cosas, que SovNode se indexe a sí mismo re-leyendo su propia memoria.
DEFAULT_EXCLUDED_SUFFIXES = {".db", ".wal", ".pyc"}

# Un archivo más grande que esto se salta directo — evita que un log o un
# dataset gigante soltado en una carpeta de workspace bloquee un ciclo de
# escaneo entero chunkeando/embebiendo megabytes de texto.
DEFAULT_MAX_FILE_SIZE_BYTES = 2 * 1024 * 1024  # 2 MB


@dataclass
class _KnownFile:
    mtime: float
    confirmed: bool = False


@dataclass
class WorkspaceScanner:
    """
    Escaneo por polling de un conjunto de carpetas raíz ("workspaces"),
    con debounce de dos pasadas: un archivo nuevo o modificado solo se
    reporta como listo para (re)indexar una vez que su `mtime` se observó
    IGUAL en dos `scan_once()` consecutivos — evita reindexar a mitad de
    una escritura larga (p. ej. un editor guardando un archivo grande) y
    evita ráfagas de reindexado si algo toca el mismo archivo varias
    veces seguidas en segundos.

    Uso (ver WorkspaceWatcherWorker en sovnode_qt.py):
        scanner = WorkspaceScanner()
        scanner.add_root("C:/Users/.../mi_proyecto")
        while True:
            to_index, to_remove = scanner.scan_once()
            ...
            time.sleep(intervalo)
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
        """
        Agrega una carpeta a vigilar. No valida existencia en disco aquí
        a propósito — `scan_once()` simplemente no encuentra nada bajo una
        ruta inválida/temporalmente inaccesible (unidad de red caída,
        etc.) y sigue con las demás raíces sin lanzar.
        """
        norm = os.path.normpath(path)
        if norm in self._roots:
            return False
        self._roots.add(norm)
        return True

    def remove_root(self, path: str) -> List[str]:
        """
        Quita una carpeta de vigilancia y devuelve las rutas de archivo
        que estaban indexadas bajo ella — el llamador (WorkspaceWatcherWorker)
        es responsable de pedirle a Orchestrator.remove_document_from_rag()
        que las retire del índice vectorial, ya que este módulo no conoce
        el orquestador.
        """
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
        """Devuelve {ruta_absoluta: mtime} de todo archivo elegible bajo
        las raíces vigiladas, ahora mismo."""
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
        Ejecuta una pasada de escaneo. Devuelve `(to_index, to_remove)`:
        - `to_index`: rutas listas para (re)indexar — nuevas o
          modificadas, con `mtime` ya estable en dos pasadas.
        - `to_remove`: rutas que existían en la pasada anterior y ya no
          aparecen en el filesystem (archivo borrado, o carpeta que salió
          de la vigilancia).

        No lee contenido de archivo ni llama a Orchestrator — eso es
        deliberado, para mantener este módulo sin dependencias de disco
        pesadas ni acoplado al pipeline de RAG; el llamador decide qué
        hacer con las rutas devueltas.
        """
        current = self._walk_current_files()

        to_remove = [fp for fp in self._known if fp not in current]
        for fp in to_remove:
            del self._known[fp]

        to_index: List[str] = []
        for fp, mtime in current.items():
            known = self._known.get(fp)
            if known is None:
                # Primera vez que se ve: se registra sin confirmar, se
                # reporta recién en la PRÓXIMA pasada si el mtime no
                # cambió — así una escritura en curso no dispara un
                # indexado a medio guardar.
                self._known[fp] = _KnownFile(mtime=mtime, confirmed=False)
                continue
            if known.mtime != mtime:
                # Cambió respecto a la última pasada: reinicia el
                # debounce en vez de confirmar de una.
                self._known[fp] = _KnownFile(mtime=mtime, confirmed=False)
                continue
            if not known.confirmed:
                # Mismo mtime que la pasada anterior por primera vez:
                # confirmado. Se reporta una única vez (al pasar de
                # confirmed=False a True), no en cada pasada subsiguiente.
                known.confirmed = True
                to_index.append(fp)

        return to_index, to_remove
