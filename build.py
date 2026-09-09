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
build.py - Empaquetado de un solo comando para SovNode (Windows)

ES: Uso: python build.py. Qué hace, en orden: (1) busca el único archivo
.ico en la raíz del proyecto y lo normaliza a 'logo.ico' — el nombre que
SovNode.spec y get_resource_path() (sovnode_qt.py) esperan encontrar, así
que poner un ícono nuevo en la raíz alcanza para que el .exe salga con ese
ícono, sin editar el .spec a mano; (2) verifica que PyInstaller esté
instalado (lo instala si falta, con confirmación); (3) limpia build/SovNode
y dist/SovNode de una corrida anterior; (4) corre `pyinstaller --noconfirm
SovNode.spec` con la salida real visible en consola; (5) si el build salió
bien, comprime dist/SovNode/ en SovNode.zip en la raíz del proyecto — el
artefacto que el README linkea desde Releases.

El ícono se resuelve siempre desde la raíz del proyecto, en los dos modos
(empaquetado y no empaquetado): el ícono INCRUSTADO en el .exe (`icon=` en
el .spec) y el ícono en tiempo de EJECUCIÓN (ventana/bandeja, que necesita
el archivo dentro del bundle de PyInstaller vía `datas`) son dos mecanismos
distintos, y ambos apuntan a la misma fuente de verdad para evitar que
diverjan.

EN: Usage: python build.py. What it does, in order: (1) finds the single
.ico file at the project root and normalizes it to 'logo.ico' — the name
SovNode.spec and get_resource_path() (sovnode_qt.py) expect, so dropping a
new icon at the root is enough for the .exe to pick it up, no manual .spec
edit needed; (2) verifies PyInstaller is installed (installs it if
missing, with confirmation); (3) cleans build/SovNode and dist/SovNode
from a previous run; (4) runs `pyinstaller --noconfirm SovNode.spec` with
real output visible on the console; (5) if the build succeeded, zips
dist/SovNode/ into SovNode.zip at the project root — the artifact the
README links from Releases.

The icon is always resolved from the project root, in both modes (frozen
and unfrozen): the icon EMBEDDED in the .exe (`icon=` in the .spec) and
the icon at RUNTIME (window/tray, which needs the file inside the
PyInstaller bundle via `datas`) are two distinct mechanisms, and both point
to the same source of truth to keep them from drifting apart.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
SPEC_FILE = PROJECT_ROOT / "SovNode.spec"
CANONICAL_ICON_NAME = "logo.ico"
APP_NAME = "SovNode"


def _fail(message: str) -> None:
    print(f"\n[ERROR] {message}")
    sys.exit(1)


def resolve_icon() -> Path:
    """
    ES: Encuentra el único .ico en la raíz del proyecto y lo normaliza a
    'logo.ico'. Falla con un mensaje claro si no hay ninguno o si hay más
    de uno (ambigüedad real: mejor pedirle al usuario que elija que
    adivinar cuál empaquetar).

    EN: Finds the single .ico at the project root and normalizes it to
    'logo.ico'. Fails with a clear message if there's none or more than
    one (a real ambiguity: better to ask the user to pick than to guess
    which one to package).
    """
    candidates = sorted(PROJECT_ROOT.glob("*.ico"))
    if not candidates:
        _fail(
            f"No se encontró ningún archivo .ico en la raíz del proyecto "
            f"({PROJECT_ROOT}). Poné tu ícono ahí (cualquier nombre) y "
            f"volvé a correr este script."
        )
    if len(candidates) > 1:
        nombres = ", ".join(p.name for p in candidates)
        _fail(
            f"Hay más de un archivo .ico en la raíz del proyecto ({nombres}) "
            f"— dejá solo uno para evitar empaquetar el ícono equivocado."
        )

    icon_path = candidates[0]
    canonical_path = PROJECT_ROOT / CANONICAL_ICON_NAME
    if icon_path.name != CANONICAL_ICON_NAME:
        print(f"[build] Usando '{icon_path.name}' -> copiando a '{CANONICAL_ICON_NAME}' "
              f"(nombre que SovNode.spec espera)...")
        shutil.copyfile(icon_path, canonical_path)
        icon_path = canonical_path
    else:
        print(f"[build] Ícono encontrado: {icon_path.name}")
    return icon_path


def ensure_pyinstaller() -> None:
    """ES: Verifica que PyInstaller esté instalado y lo instala (con
    confirmación del usuario) si falta.
    EN: Verifies PyInstaller is installed and installs it (with user
    confirmation) if missing."""
    try:
        import PyInstaller  # noqa: F401
        print(f"[build] PyInstaller ya instalado (versión {PyInstaller.__version__}).")
        return
    except ImportError:
        pass

    respuesta = input(
        "[build] PyInstaller no está instalado. ¿Instalarlo ahora con pip? [S/n] "
    ).strip().lower()
    if respuesta not in ("", "s", "si", "sí", "y", "yes"):
        _fail("PyInstaller es necesario para empaquetar. Instalalo con "
              "'pip install pyinstaller' y volvé a correr este script.")

    subprocess.run(
        [sys.executable, "-m", "pip", "install", "pyinstaller"], check=True
    )


def clean_previous_build() -> None:
    """ES: Borra build/SovNode y dist/SovNode de una corrida anterior.
    EN: Removes build/SovNode and dist/SovNode from a previous run."""
    for stale_dir in (PROJECT_ROOT / "build" / APP_NAME, PROJECT_ROOT / "dist" / APP_NAME):
        if stale_dir.exists():
            print(f"[build] Limpiando {stale_dir.relative_to(PROJECT_ROOT)}/ de una corrida anterior...")
            shutil.rmtree(stale_dir, ignore_errors=True)


def run_pyinstaller() -> None:
    """ES: Corre PyInstaller sobre SovNode.spec con la salida visible en
    consola.
    EN: Runs PyInstaller against SovNode.spec with output visible on the
    console."""
    if not SPEC_FILE.exists():
        _fail(f"No se encontró {SPEC_FILE.name} en {PROJECT_ROOT}.")

    print(f"\n[build] Corriendo PyInstaller sobre {SPEC_FILE.name}...\n")
    result = subprocess.run(
        [sys.executable, "-m", "PyInstaller", "--noconfirm", str(SPEC_FILE)],
        cwd=PROJECT_ROOT,
    )
    if result.returncode != 0:
        _fail(
            "PyInstaller terminó con errores (ver la salida de arriba) — "
            "el .zip no se generó."
        )


def zip_output() -> Path:
    """ES: Comprime dist/SovNode/ en SovNode.zip en la raíz del proyecto.
    EN: Zips dist/SovNode/ into SovNode.zip at the project root."""
    dist_dir = PROJECT_ROOT / "dist" / APP_NAME
    if not dist_dir.exists():
        _fail(f"PyInstaller no dejó nada en {dist_dir} — no hay nada que comprimir.")

    zip_base = PROJECT_ROOT / APP_NAME  # shutil.make_archive agrega '.zip'
    print(f"\n[build] Comprimiendo {dist_dir.relative_to(PROJECT_ROOT)}/ -> {APP_NAME}.zip ...")
    archive_path = shutil.make_archive(str(zip_base), "zip", root_dir=dist_dir)
    return Path(archive_path)


def main() -> None:
    """ES: Orquesta el empaquetado completo: ícono, PyInstaller, limpieza
    y compresión final.
    EN: Orchestrates the full packaging run: icon, PyInstaller, cleanup,
    and final zip."""
    print(f"=== Empaquetando {APP_NAME} ===\n")
    icon_path = resolve_icon()
    ensure_pyinstaller()
    clean_previous_build()
    run_pyinstaller()
    zip_path = zip_output()

    print("\n=== Listo ===")
    print(f"Ícono usado:    {icon_path.relative_to(PROJECT_ROOT)}")
    print(f"Carpeta app:    dist/{APP_NAME}/")
    print(f"Ejecutable:     dist/{APP_NAME}/{APP_NAME}.exe")
    print(f"Paquete .zip:   {zip_path.name}")


if __name__ == "__main__":
    main()
