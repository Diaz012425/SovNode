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
SovNode — Módulo de Lectura y Diagnóstico del Sistema (sys_optimizer.py)
"""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
import time
from typing import Dict, Any, Optional

# TTL de la caché de telemetría: get_system_telemetry() lanza hasta 3
# subprocess.run() (dos PowerShell + nvidia-smi) por llamada - sin
# caché, cada invocación repetida en poco tiempo (p. ej. desde
# build_optimization_prompt() llamado varias veces seguidas) repetía
# ese costo de spawnear procesos nuevos sin que los datos hubieran
# cambiado de forma significativa.
TELEMETRY_CACHE_TTL_SECONDS = 5.0
_telemetry_cache: Optional[Dict[str, Any]] = None
_telemetry_cache_ts = 0.0


def get_system_telemetry() -> Dict[str, Any]:
    """Recopila información técnica y consumo actual del sistema (con caché TTL, ver TELEMETRY_CACHE_TTL_SECONDS)."""
    global _telemetry_cache, _telemetry_cache_ts
    now = time.monotonic()
    if _telemetry_cache is not None and (now - _telemetry_cache_ts) < TELEMETRY_CACHE_TTL_SECONDS:
        return _telemetry_cache

    telemetry = _compute_system_telemetry()
    _telemetry_cache = telemetry
    _telemetry_cache_ts = now
    return telemetry


def _compute_system_telemetry() -> Dict[str, Any]:
    telemetry: Dict[str, Any] = {}

    # Información básica del sistema operativo y CPU
    telemetry["os"] = f"{platform.system()} {platform.release()} ({platform.architecture()[0]})"
    telemetry["cpu_count_logical"] = os.cpu_count() or 0

    # Memoria RAM mediante PowerShell WMI (sin psutil)
    try:
        ps_script = (
            "Get-CimInstance Win32_OperatingSystem | "
            "Select-Object TotalVisibleMemorySize, FreePhysicalMemory | "
            "ConvertTo-Json"
        )
        res = subprocess.run(
            ["powershell", "-Command", ps_script],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if res.returncode == 0 and res.stdout.strip():
            import json
            data = json.loads(res.stdout)
            total_kb = float(data.get("TotalVisibleMemorySize", 0))
            free_kb = float(data.get("FreePhysicalMemory", 0))
            used_kb = total_kb - free_kb
            telemetry["ram_total_gb"] = round(total_kb / (1024 * 1024), 2)
            telemetry["ram_free_gb"] = round(free_kb / (1024 * 1024), 2)
            telemetry["ram_used_gb"] = round(used_kb / (1024 * 1024), 2)
            telemetry["ram_usage_pct"] = round((used_kb / total_kb) * 100, 1) if total_kb > 0 else 0
    except Exception as exc:
        telemetry["ram_error"] = str(exc)

    # Espacio en Disco Principal
    try:
        total, used, free = shutil.disk_usage("C:\\")
        telemetry["disk_total_gb"] = round(total / (1024**3), 2)
        telemetry["disk_free_gb"] = round(free / (1024**3), 2)
        telemetry["disk_usage_pct"] = round((used / total) * 100, 1)
    except Exception as exc:
        telemetry["disk_error"] = str(exc)

    # Detección de procesos que más RAM consumen
    try:
        ps_top = (
            "Get-Process | Sort-Object WorkingSet64 -Descending | "
            "Select-Object -First 5 ProcessName, @{Name='RAM_MB';Expression={[math]::Round($_.WorkingSet64/1MB,1)}} | "
            "ConvertTo-Json"
        )
        res_proc = subprocess.run(
            ["powershell", "-Command", ps_top],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if res_proc.returncode == 0 and res_proc.stdout.strip():
            import json
            parsed_proc = json.loads(res_proc.stdout)
            telemetry["top_processes"] = [parsed_proc] if isinstance(parsed_proc, dict) else parsed_proc
    except Exception as exc:
        telemetry["processes_error"] = str(exc)

    # Verificación de GPU (nvidia-smi si existe)
    try:
        gpu_res = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,memory.total,memory.used,memory.free,utilization.gpu", "--format=csv,noheader,nounits"],
            capture_output=True,
            text=True,
            timeout=3,
        )
        if gpu_res.returncode == 0 and gpu_res.stdout.strip():
            gpu_info = gpu_res.stdout.strip().split(",")
            telemetry["gpu_name"] = gpu_info[0].strip()
            telemetry["gpu_vram_total_mb"] = gpu_info[1].strip()
            telemetry["gpu_vram_used_mb"] = gpu_info[2].strip()
            telemetry["gpu_utilization_pct"] = gpu_info[4].strip()
    except Exception:
        telemetry["gpu_status"] = "No detectada o mediante driver CPU/iGPU."

    return telemetry


def build_optimization_prompt() -> str:
    """Genera el reporte estructurado de hardware para que el orquestador proponga optimizaciones."""
    data = get_system_telemetry()
    
    prompt = (
        "Analiza el estado actual de este sistema informático y entrega recomendaciones "
        "concretas, técnicas y aplicables para optimizar su rendimiento, reducir el consumo de RAM/CPU "
        "y mejorar la velocidad de ejecución de SovNode y del sistema en general:\n\n"
        "=== TELEMETRÍA DEL SISTEMA ===\n"
        f"- Sistema Operativo: {data.get('os', 'N/A')}\n"
        f"- Núcleos de CPU: {data.get('cpu_count_logical', 'N/A')}\n"
        f"- Memoria RAM Total: {data.get('ram_total_gb', 'N/A')} GB\n"
        f"- Memoria RAM Usada: {data.get('ram_used_gb', 'N/A')} GB ({data.get('ram_usage_pct', 'N/A')}%)\n"
        f"- Memoria RAM Libre: {data.get('ram_free_gb', 'N/A')} GB\n"
        f"- Disco C: {data.get('disk_free_gb', 'N/A')} GB libres de {data.get('disk_total_gb', 'N/A')} GB ({data.get('disk_usage_pct', 'N/A')}% usado)\n"
    )

    if "gpu_name" in data:
        prompt += (
            f"- GPU: {data['gpu_name']} | VRAM Usada: {data['gpu_vram_used_mb']} MB / {data['gpu_vram_total_mb']} MB "
            f"| Carga GPU: {data['gpu_utilization_pct']}%\n"
        )

    if "top_processes" in data and isinstance(data["top_processes"], list):
        prompt += "\n- Top 5 procesos con mayor consumo de memoria:\n"
        for proc in data["top_processes"]:
            prompt += f"  • {proc.get('ProcessName')}: {proc.get('RAM_MB')} MB\n"

    prompt += "\nPor favor, estructura tus recomendaciones en:\n"
    prompt += "1. Acciones inmediatas para liberar recursos de memoria.\n"
    prompt += "2. Ajustes recomendados en la configuración de SovNode/Ollama.\n"
    prompt += "3. Mantenimiento general del sistema a mediano plazo.\n"

    return prompt