"""
SovNode — Gestor de licencias offline.

Formato de licencia:
    base64url(payload_json).base64url(hmac_sha256(payload_json))

El payload contiene:
    {
        "license_id": "SOV-...",
        "product": "SovNode Pro",
        "customer": "Nombre del cliente",
        "issued_at": "2026-08-08T00:00:00+00:00",
        "expires_at": null
    }

La validación no realiza solicitudes de red y normalmente tarda menos de 5 ms.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Tuple

from PyQt6.QtCore import QStandardPaths


PRODUCT_NAME = "SovNode Pro"

# =====================================================================
# SECRETO DE FIRMA
# =====================================================================
# NO existe un secreto por defecto utilizable. Antes había uno
# hardcodeado aquí, y ese es un fallo de seguridad real en cuanto el
# paquete se distribuye: el binario incluye este módulo, así que
# cualquiera con acceso a él podía leer la clave y usar
# `create_license_key()` -que vive en este mismo archivo- para emitirse
# licencias Pro válidas indefinidamente. Agravante concreto en este
# proyecto: el motor de herramientas dinámicas ejecutaba código
# generado por el LLM y era evadible hasta poder leer archivos
# arbitrarios del disco; leer `license_manager.py` y extraer la clave
# era una cadena de ataque completa (ver dynamic_tool_engine.py).
#
# Ahora el secreto debe inyectarse por entorno (SOVNODE_LICENSE_SECRET)
# o pasarse explícitamente. Sin él, la validación falla cerrada: se
# rechaza toda licencia en vez de aceptar firmas hechas con una clave
# que es pública de facto.
_LICENSE_SECRET_ENV_VAR = "SOVNODE_LICENSE_SECRET"

# Marca de compilación opcional: el proceso de empaquetado puede
# sustituir este valor por el secreto real de producción. Se mantiene
# vacío en el árbol de fuentes para que nunca se versione un secreto.
BUILD_TIME_LICENSE_SECRET = ""


class LicenseSecretUnavailable(RuntimeError):
    """El secreto de firma no está configurado: no se puede validar ni emitir."""


def _resolve_secret(explicit: Optional[str] = None) -> Optional[str]:
    """Resuelve el secreto de firma, o None si no hay ninguno configurado."""
    for candidate in (explicit, os.getenv(_LICENSE_SECRET_ENV_VAR), BUILD_TIME_LICENSE_SECRET):
        if candidate and candidate.strip():
            return candidate
    return None


@dataclass(frozen=True)
class LicenseInfo:
    license_id: str
    product: str
    customer: str
    issued_at: str
    expires_at: Optional[str] = None

    def is_expired_at(self, reference: datetime) -> bool:
        """
        Expiración evaluada contra un instante de referencia explícito,
        no contra el reloj del sistema directamente. Es lo que permite a
        `LicenseManager` pasar una marca temporal ANTI-RETROCESO en vez
        del `datetime.now()` crudo, que un usuario puede retrasar desde
        el sistema operativo para revivir una licencia caducada.
        """
        if not self.expires_at:
            return False

        try:
            expiration = datetime.fromisoformat(
                self.expires_at.replace("Z", "+00:00")
            )
            if expiration.tzinfo is None:
                expiration = expiration.replace(tzinfo=timezone.utc)
            return reference > expiration
        except ValueError:
            return True

    @property
    def is_expired(self) -> bool:
        """Compatibilidad: evalúa contra el reloj del sistema tal cual."""
        return self.is_expired_at(datetime.now(timezone.utc))


class LicenseManager:
    """Valida y persiste localmente el estado de una licencia SovNode Pro."""

    def __init__(self, secret: Optional[str] = None) -> None:
        configured_secret = _resolve_secret(secret)
        # Se guarda None si no hay secreto: `validate_key()` falla
        # CERRADO en ese caso (rechaza todo) en vez de validar contra una
        # clave por defecto conocida públicamente.
        self._secret = configured_secret.encode("utf-8") if configured_secret else None
        self._license_path = self._application_data_path() / "license.json"

    @staticmethod
    def _application_data_path() -> Path:
        location = QStandardPaths.writableLocation(
            QStandardPaths.StandardLocation.AppDataLocation
        )
        path = Path(location or ".") / "SovNode"
        path.mkdir(parents=True, exist_ok=True)
        return path

    @staticmethod
    def _b64encode(value: bytes) -> str:
        return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")

    @staticmethod
    def _b64decode(value: str) -> bytes:
        padding = "=" * (-len(value) % 4)
        return base64.urlsafe_b64decode((value + padding).encode("ascii"))

    def validate_key(self, license_key: str) -> Tuple[bool, str, Optional[LicenseInfo]]:
        """
        Comprueba integridad, formato, producto y expiración.

        No usa red, no requiere reloj externo y no abre conexiones.
        """
        raw_key = (license_key or "").strip()

        if not raw_key:
            return False, "Introduce una clave de licencia.", None

        if self._secret is None:
            # Falla CERRADO: sin secreto configurado no hay forma de
            # distinguir una licencia legítima de una falsificada, así
            # que se rechaza en vez de aceptar cualquier cosa.
            return (
                False,
                f"Validación de licencias no configurada en este build (falta {_LICENSE_SECRET_ENV_VAR}).",
                None,
            )

        try:
            encoded_payload, encoded_signature = raw_key.split(".", 1)
            payload_bytes = self._b64decode(encoded_payload)
            received_signature = self._b64decode(encoded_signature)
        except (ValueError, UnicodeError, base64.binascii.Error):
            return False, "El formato de la clave no es válido.", None

        expected_signature = hmac.new(
            self._secret,
            payload_bytes,
            hashlib.sha256,
        ).digest()

        if not hmac.compare_digest(received_signature, expected_signature):
            return False, "La firma criptográfica de la licencia no es válida.", None

        try:
            payload = json.loads(payload_bytes.decode("utf-8"))
            license_info = LicenseInfo(
                license_id=str(payload["license_id"]),
                product=str(payload["product"]),
                customer=str(payload["customer"]),
                issued_at=str(payload["issued_at"]),
                expires_at=payload.get("expires_at"),
            )
        except (KeyError, TypeError, ValueError, UnicodeDecodeError, json.JSONDecodeError):
            return False, "La información interna de la licencia está dañada.", None

        if license_info.product != PRODUCT_NAME:
            return False, "La clave pertenece a otro producto.", None

        if license_info.is_expired_at(self._trusted_now()):
            return False, "Esta licencia Pro ha expirado.", None

        return True, "Licencia Pro activada correctamente.", license_info

    # =================================================================
    # RELOJ ANTI-RETROCESO (trinquete monótono)
    # =================================================================
    #: Tolerancia para desfases legítimos (ajuste de zona horaria, NTP
    #: corrigiendo una deriva, arranque antes de sincronizar la hora).
    #: Por debajo de este margen, un reloj "hacia atrás" no se considera
    #: manipulación.
    CLOCK_ROLLBACK_TOLERANCE_SECONDS: int = 24 * 3600

    def _watermark_path(self) -> Path:
        return self._license_path.with_name("clock_watermark.json")

    def _trusted_now(self) -> datetime:
        """
        Instante "de confianza" para evaluar expiración.

        `datetime.now()` por sí solo es evadible: basta retrasar la fecha
        del sistema para revivir una licencia caducada indefinidamente.
        Aquí se mantiene una MARCA DE AGUA monótona en disco con el
        instante más avanzado jamás observado; si el reloj actual está
        por detrás de esa marca más allá de la tolerancia, se asume
        retroceso y se usa la marca (el valor más avanzado), de modo que
        el tiempo nunca "corre hacia atrás" a efectos de licencia.

        No es inviolable —el usuario controla su propia máquina y puede
        borrar el archivo— pero eleva el ataque de "cambiar la fecha en
        Ajustes" a "encontrar y manipular el estado persistido", que es
        el objetivo realista de una protección local y offline.
        """
        now = datetime.now(timezone.utc)
        path = self._watermark_path()

        previous: Optional[datetime] = None
        try:
            if path.exists():
                stored = json.loads(path.read_text(encoding="utf-8"))
                previous = datetime.fromisoformat(str(stored["last_seen"]))
                if previous.tzinfo is None:
                    previous = previous.replace(tzinfo=timezone.utc)
        except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
            previous = None

        effective = now
        if previous is not None:
            drift = (previous - now).total_seconds()
            if drift > self.CLOCK_ROLLBACK_TOLERANCE_SECONDS:
                # Reloj retrasado más allá de lo tolerable: prevalece la
                # marca más avanzada ya observada.
                effective = previous

        # La marca solo AVANZA, nunca retrocede.
        if previous is None or effective > previous:
            try:
                path.write_text(
                    json.dumps({"last_seen": effective.isoformat()}),
                    encoding="utf-8",
                )
            except OSError:
                pass  # no poder escribir la marca nunca debe bloquear la app

        return effective

    def activate(self, license_key: str) -> Tuple[bool, str, Optional[LicenseInfo]]:
        """Valida y guarda de forma local la licencia activa."""
        valid, message, info = self.validate_key(license_key)

        if not valid or info is None:
            return valid, message, info

        record = {
            "license_key": license_key.strip(),
            "activated_at": datetime.now(timezone.utc).isoformat(),
        }

        try:
            temporary_path = self._license_path.with_suffix(".tmp")
            temporary_path.write_text(
                json.dumps(record, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            temporary_path.replace(self._license_path)
        except OSError as exc:
            return False, f"La licencia era válida, pero no pudo guardarse: {exc}", None

        return True, message, info

    def active_license(self) -> Optional[LicenseInfo]:
        """Carga y revalida la licencia persistida localmente."""
        if not self._license_path.exists():
            return None

        try:
            record = json.loads(self._license_path.read_text(encoding="utf-8"))
            valid, _, info = self.validate_key(str(record.get("license_key", "")))
            return info if valid else None
        except (OSError, ValueError, json.JSONDecodeError):
            return None

    def deactivate(self) -> None:
        """Elimina la activación local actual."""
        try:
            self._license_path.unlink(missing_ok=True)
        except OSError:
            pass


def create_license_key(
    customer: str,
    license_id: str,
    expires_at: Optional[str] = None,
    secret: Optional[str] = None,
) -> str:
    """
    Herramienta de emisión para uso del proveedor.

    Ejemplo:
        print(create_license_key("Acme Corp", "SOV-2026-0001"))

    En producción, esta función debe ejecutarse en un entorno privado,
    usando SOVNODE_LICENSE_SECRET y nunca entregando el secreto al cliente.
    """
    resolved = _resolve_secret(secret)
    if not resolved:
        # Sin secreto no se emite nada. Antes caía a la clave hardcodeada,
        # así que este mismo módulo -incluido en el binario distribuido-
        # era una fábrica de licencias Pro válidas para cualquiera.
        raise LicenseSecretUnavailable(
            f"No hay secreto de firma configurado. Define {_LICENSE_SECRET_ENV_VAR} "
            f"en el entorno privado de emisión antes de generar licencias."
        )
    signing_secret = resolved.encode("utf-8")

    payload = {
        "license_id": license_id.strip(),
        "product": PRODUCT_NAME,
        "customer": customer.strip(),
        "issued_at": datetime.now(timezone.utc).isoformat(),
        "expires_at": expires_at,
    }

    payload_bytes = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")

    signature = hmac.new(
        signing_secret,
        payload_bytes,
        hashlib.sha256,
    ).digest()

    return (
        f"{LicenseManager._b64encode(payload_bytes)}."
        f"{LicenseManager._b64encode(signature)}"
    )


if __name__ == "__main__":
    print(
        create_license_key(
            customer="Cliente de prueba",
            license_id="SOV-DEMO-2026-0001",
        )
    )