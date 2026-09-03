"""
SovNode — Robust JSON Parser
============================
Extractor y reparador de invocaciones JSON para Function Calling en LLMs compactos.
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, Optional

class RobustJSONParser:
    @staticmethod
    def extract_and_repair(raw_response: str) -> Optional[Dict[str, Any]]:
        """Extrae la llamada a función JSON incluso si el modelo incluye texto adicional o sintaxis imperfecta."""
        if not raw_response or not raw_response.strip():
            return None

        # 1. Intentar extracción directa si está en bloque ```json ... ```
        code_block_match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", raw_response, re.DOTALL)
        if code_block_match:
            candidate = code_block_match.group(1)
            parsed = RobustJSONParser._try_parse(candidate)
            if parsed:
                return parsed

        # 2. Búsqueda por Regex de cualquier objeto JSON `{...}`
        json_matches = re.findall(r"\{[^{}]*\"tool\"[^{}]*\}", raw_response, re.DOTALL)
        for match in json_matches:
            parsed = RobustJSONParser._try_parse(match)
            if parsed:
                return parsed

        # 3. Intentar extracción del primer `{` al último `}`
        start = raw_response.find("{")
        end = raw_response.rfind("}")
        if start != -1 and end != -1 and end > start:
            candidate = raw_response[start : end + 1]
            parsed = RobustJSONParser._try_parse(candidate)
            if parsed:
                return parsed

        return None

    @staticmethod
    def _try_parse(json_str: str) -> Optional[Dict[str, Any]]:
        # Intento estándar
        try:
            data = json.loads(json_str)
            if isinstance(data, dict) and "tool" in data:
                return data
        except json.JSONDecodeError:
            pass

        # Reparaciones rápidas para modelos 3B:
        # - Reemplazar comillas simples por dobles en llaves
        # - Remover comas flotantes antes de cierres de llave
        repaired = json_str.replace("'", '"')
        repaired = re.sub(r",\s*\}", "}", repaired)
        repaired = re.sub(r",\s*\]", "]", repaired)

        try:
            data = json.loads(repaired)
            if isinstance(data, dict) and "tool" in data:
                return data
        except json.JSONDecodeError:
            return None

        return None