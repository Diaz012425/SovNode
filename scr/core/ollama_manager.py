"""
Ollama Manager & Dialogs — SovNode
===================================
Gestiona el ciclo de vida del servidor Ollama y los diálogos de instalación/descarga en-app.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from typing import Optional
import requests
from PyQt6.QtCore import QThread, Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QDialog,
    QLabel,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
)

CREATE_NO_WINDOW = 0x08000000


class ModelPullWorker(QThread):
    """Ejecuta 'ollama pull <modelo>' y emite el progreso en tiempo real a la UI."""

    progress_updated = pyqtSignal(str, int)
    completed = pyqtSignal(bool, str)

    def __init__(self, model_name: str) -> None:
        super().__init__()
        self.model_name = model_name

    def run(self) -> None:
        try:
            process = subprocess.Popen(
                ["ollama", "pull", self.model_name],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                creationflags=CREATE_NO_WINDOW,
                bufsize=1,
            )

            percent_pattern = re.compile(r"(\d+)%")

            if process.stdout:
                for line in iter(process.stdout.readline, ""):
                    if not line:
                        break
                    match = percent_pattern.search(line)
                    percent = int(match.group(1)) if match else -1
                    clean_line = line.strip()
                    if clean_line:
                        self.progress_updated.emit(clean_line, percent)

            process.wait()
            if process.returncode == 0:
                self.completed.emit(
                    True, f"Modelo {self.model_name} instalado con éxito."
                )
            else:
                self.completed.emit(
                    False, f"Error al descargar {self.model_name}."
                )

        except Exception as exc:
            self.completed.emit(False, str(exc))


class OllamaInstallerWorker(QThread):
    """Descarga el instalador oficial de Ollama para Windows y lo ejecuta."""

    progress_updated = pyqtSignal(str, int)
    completed = pyqtSignal(bool, str)

    def run(self) -> None:
        try:
            url = "https://ollama.com/download/OllamaSetup.exe"
            temp_dir = os.environ.get("TEMP", "C:\\Temp")
            installer_path = os.path.join(temp_dir, "OllamaSetup.exe")

            self.progress_updated.emit("Conectando con el servidor de Ollama...", 0)
            response = requests.get(url, stream=True, timeout=30)
            total_size = int(response.headers.get("content-length", 0))

            downloaded = 0
            with open(installer_path, "wb") as f:
                for chunk in response.iter_content(chunk_size=8192):
                    if chunk:
                        f.write(chunk)
                        downloaded += len(chunk)
                        if total_size > 0:
                            percent = int((downloaded / total_size) * 100)
                            self.progress_updated.emit(
                                f"Descargando Ollama: {percent}%", percent
                            )

            self.progress_updated.emit("Iniciando instalador oficial...", 100)
            subprocess.Popen([installer_path])
            self.completed.emit(
                True, "Instalador ejecutado. Siga los pasos en pantalla."
            )

        except Exception as exc:
            self.completed.emit(False, f"Error en la descarga: {exc}")


class OllamaProcessManager:
    """Gestiona la detección, arranque y cierre silencioso del servidor Ollama."""

    MOCK_MISSING_OLLAMA: bool = False  # <--- Debe estar en False
    MOCK_MISSING_MODEL: bool = False

    # Nota (propuesto y verificado por el usuario contra el código
    # real): `_llm_lock` en orchestrator.py protege cada llamada HTTP a
    # Ollama de punta a punta a propósito, para no competir por la
    # misma GPU/VRAM - pero eso solo tiene sentido si el SERVIDOR de
    # Ollama también está limitado a 1 request en vuelo. Con la config
    # por defecto (`OLLAMA_NUM_PARALLEL` sin fijar, Ollama asume 1),
    # arrancar el propio motor de concurrencia de esta app (ramas A/B
    # del Tree-of-Thought vía ThreadPoolExecutor, ver
    # `_tree_of_thought_reasoning`) no reducía la latencia real: ambos
    # hilos igual quedaban serializados esperando el mismo cerrojo. Con
    # el servidor aceptando 2 requests concurrentes de verdad, el
    # cerrojo pasa de `RLock` a `BoundedSemaphore(2)` (ver
    # `Orchestrator.__init__`) para dejar pasar hasta 2 llamadas en
    # simultáneo - recién ahí las ramas del ToT, el CognitiveGovernor y
    # el turno del usuario pueden solaparse de verdad.
    # `OLLAMA_MAX_LOADED_MODELS=2` porque `general_model`/`coder_model`
    # son variantes DISTINTAS que se alternan seguido (ver
    # CODER_MODEL_VARIANTS/GENERAL_MODEL_VARIANTS) - sin esto, Ollama
    # descargaría uno de la VRAM cada vez que se carga el otro.
    # Empieza en 2 (no más) porque el modelo activo por defecto es de
    # 3B - subir esto sin medir VRAM real disponible puede degradar el
    # throughput por contención en vez de mejorarlo.
    _SERVER_ENV_OVERRIDES: dict = {
        "OLLAMA_NUM_PARALLEL": "2",
        "OLLAMA_MAX_LOADED_MODELS": "2",
    }

    @classmethod
    def _server_env(cls) -> dict:
        return {**os.environ, **cls._SERVER_ENV_OVERRIDES}

    def __init__(self, endpoint: str = "http://localhost:11434") -> None:
        self.endpoint = endpoint
        self._process: Optional[subprocess.Popen] = None

    def is_installed(self) -> bool:
        if self.MOCK_MISSING_OLLAMA:
            return False

        if shutil.which("ollama") is not None:
            return True

        user_appdata = os.path.expanduser(
            "~\\AppData\\Local\\Programs\\Ollama\\ollama.exe"
        )
        return os.path.exists(user_appdata)

    def is_running(self) -> bool:
        try:
            res = requests.get(self.endpoint, timeout=1.0)
            return res.status_code < 500
        except Exception:
            return False

    def start_server_silent(self) -> bool:
        if self.is_running():
            return True

        ollama_bin = shutil.which("ollama") or os.path.expanduser(
            "~\\AppData\\Local\\Programs\\Ollama\\ollama.exe"
        )

        if not os.path.exists(ollama_bin) and shutil.which("ollama") is None:
            return False

        try:
            self._process = subprocess.Popen(
                [ollama_bin, "serve"],
                creationflags=CREATE_NO_WINDOW,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env=self._server_env(),
            )
            return True
        except Exception as exc:
            print(f"Error al iniciar Ollama: {exc}")
            return False

    def stop_server(self) -> None:
        if self._process is not None:
            try:
                self._process.terminate()
                self._process.wait(timeout=2)
            except Exception:
                self._process.kill()
            self._process = None

        try:
            # 1. Mata el proceso principal de Ollama
            subprocess.run(
                ["taskkill", "/F", "/IM", "ollama.exe"],
                creationflags=CREATE_NO_WINDOW,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            # 2. Mata cualquier proceso backend de llama-server huérfano
            subprocess.run(
                ["taskkill", "/F", "/IM", "llama-server.exe"],
                creationflags=CREATE_NO_WINDOW,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except Exception:
            pass

    def ensure_server_running(self) -> bool:
        """Comprueba el puerto 11434 e inicia el proceso 'ollama serve' en segundo plano si está offline."""
        import socket
        import subprocess
        import os

        # 1. Verificar si Ollama ya responde en el puerto 11434
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(1.0)
            if s.connect_ex(("127.0.0.1", 11434)) == 0:
                return True

        # 2. Si está caído, arrancarlo en segundo plano
        try:
            creation_flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
            self._process = subprocess.Popen(
                ["ollama", "serve"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=creation_flags,
                env=self._server_env(),
            )
            return True
        except Exception as exc:
            print(f"Error al intentar auto-arrancar Ollama: {exc}")
            return False   


class SetupDialog(QDialog):
    """Ventana modal de bienvenida, descarga y configuración inicial."""

    def __init__(
        self, missing_binary: bool, missing_models: list[str], parent=None
    ) -> None:
        # Poner 'None' aquí desvincula la ventana de la ventana principal no mostrada
        super().__init__(None)

        self.setWindowTitle("Configuración de Nodo — SovNode v2.0")
        self.setFixedSize(480, 260)

        # Forzar a Windows a crear una ventana e ícono independiente en la barra de tareas
        self.setWindowFlags(Qt.WindowType.Window)

        self.setModal(True)
        self.setStyleSheet(
            "QDialog { background-color: #14171F; color: #FFFFFF; }"
        )

        self.missing_binary = missing_binary
        self.missing_models = missing_models
        self.current_model_index = 0
        self.worker: Optional[QThread] = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(12)

        self.lbl_title = QLabel("🚀 Configuración Inicial del Nodo Local")
        self.lbl_title.setStyleSheet(
            "font-size: 16px; font-weight: bold; color: #4C8BF5;"
        )
        layout.addWidget(self.lbl_title)

        self.lbl_status = QLabel("Preparando componentes...")
        self.lbl_status.setWordWrap(True)
        self.lbl_status.setStyleSheet("font-size: 12px; color: #E6E8EC;")
        layout.addWidget(self.lbl_status)

        self.progress_bar = QProgressBar()
        self.progress_bar.setStyleSheet(
            "QProgressBar { background-color: #1B1F2A; border: 1px solid #262B36; border-radius: 6px; text-align: center; color: #FFFFFF; }"
            "QProgressBar::chunk { background-color: #3DDC97; border-radius: 5px; }"
        )
        layout.addWidget(self.progress_bar)

        self.btn_action = QPushButton("Descargar e instalar automáticamente")
        self.btn_action.setStyleSheet(
            "QPushButton { background-color: #1E3A8A; color: white; font-weight: bold; border-radius: 6px; padding: 10px; }"
            "QPushButton:hover { background-color: #2563EB; }"
        )
        layout.addWidget(self.btn_action)

        if missing_binary:
            self.lbl_status.setText(
                "No se detectó el motor Ollama en el sistema."
            )
            self.progress_bar.setVisible(False)
            
            # Botón 1: Descarga automática
            self.btn_action.setText("Descargar e instalar automáticamente")
            self.btn_action.clicked.connect(self._start_ollama_download)
            
            # Botón 2: Re-comprobar instalación en el sistema
            self.btn_recheck = QPushButton("🔍 Ya lo instalé (Volver a verificar)")
            self.btn_recheck.setStyleSheet(
                "QPushButton { background-color: #171B24; color: #3DDC97; border: 1px solid #3DDC97; border-radius: 6px; padding: 8px; }"
                "QPushButton:hover { background-color: #3DDC97; color: #000000; }"
            )
            self.btn_recheck.clicked.connect(self._recheck_system)
            layout.addWidget(self.btn_recheck)

    def _recheck_system(self) -> None:
        """Verifica en tiempo real si el usuario instaló Ollama externamente."""
        from shutil import which
        import os

        installed_in_path = which("ollama") is not None
        user_appdata = os.path.expanduser("~\\AppData\\Local\\Programs\\Ollama\\ollama.exe")
        installed_in_appdata = os.path.exists(user_appdata)

        if installed_in_path or installed_in_appdata:
            # Apaga la bandera de simulación para permitir el inicio real
            OllamaProcessManager.MOCK_MISSING_OLLAMA = False
            
            QMessageBox.information(
                self,
                "Nodo Detectado",
                "¡Se detectó la instalación de Ollama en el sistema! Iniciando SovNode..."
            )
            self.accept()
        else:
            QMessageBox.warning(
                self,
                "No Detectado",
                "Todavía no se detecta el ejecutable de Ollama en las rutas del sistema."
            )

    def _start_ollama_download(self) -> None:
        self.btn_action.setEnabled(False)
        self.progress_bar.setVisible(True)
        self.progress_bar.setValue(0)

        self.worker = OllamaInstallerWorker()
        self.worker.progress_updated.connect(self._on_progress)
        self.worker.completed.connect(self._on_ollama_installed)
        self.worker.start()

    def _on_ollama_installed(self, success: bool, message: str) -> None:
        if success:
            self.lbl_status.setText(
                "¡Instalador completado! Cierre esta ventana una vez finalice la instalación del sistema."
            )
            self.btn_action.setText("Continuar")
            self.btn_action.setEnabled(True)
            self.btn_action.clicked.disconnect()
            self.btn_action.clicked.connect(self.accept)
        else:
            QMessageBox.critical(self, "Error de instalación", message)
            self.btn_action.setEnabled(True)

    def _start_next_model_download(self) -> None:
        if self.current_model_index < len(self.missing_models):
            model = self.missing_models[self.current_model_index]
            self.lbl_status.setText(
                f"Descargando modelo de Inteligencia Artificial: {model}..."
            )
            self.progress_bar.setValue(0)

            self.worker = ModelPullWorker(model)
            self.worker.progress_updated.connect(self._on_progress)
            self.worker.completed.connect(self._on_model_completed)
            self.worker.start()
        else:
            self.lbl_status.setText("¡Todos los componentes están listos!")
            self.accept()

    def _on_progress(self, message: str, percent: int) -> None:
        self.lbl_status.setText(message)
        if percent >= 0:
            self.progress_bar.setValue(percent)

    def _on_model_completed(self, success: bool, message: str) -> None:
        if success:
            self.current_model_index += 1
            self._start_next_model_download()
        else:
            QMessageBox.critical(self, "Error de instalación", message)
            self.reject()