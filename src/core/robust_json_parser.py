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
SovNode — Robust JSON Parser
============================
ES: Extractor y reparador de invocaciones JSON para Function Calling en LLMs compactos.
EN: Extractor and repair tool for JSON function-call payloads from small LLMs.
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, Optional

class RobustJSONParser:
    @staticmethod
    def extract_and_repair(raw_response: str) -> Optional[Dict[str, Any]]:
        """ES: Extrae la llamada a función JSON aunque el modelo agregue texto extra o sintaxis imperfecta.
        EN: Extracts the JSON function call even if the model adds extra text or malformed syntax."""
        if not raw_response or not raw_response.strip():
            return None

        code_block_match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", raw_response, re.DOTALL)
        if code_block_match:
            candidate = code_block_match.group(1)
            parsed = RobustJSONParser._try_parse(candidate)
            if parsed:
                return parsed

        json_matches = re.findall(r"\{[^{}]*\"tool\"[^{}]*\}", raw_response, re.DOTALL)
        for match in json_matches:
            parsed = RobustJSONParser._try_parse(match)
            if parsed:
                return parsed

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
        """ES: Intenta parsear JSON tal cual; si falla, aplica reparaciones rápidas comunes en modelos pequeños.
        EN: Tries to parse JSON as-is; if that fails, applies quick repairs common with small models."""
        try:
            data = json.loads(json_str)
            if isinstance(data, dict) and "tool" in data:
                return data
        except json.JSONDecodeError:
            pass

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
