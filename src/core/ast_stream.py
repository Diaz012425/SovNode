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
SovNode — Streaming AST Parser en Tiempo Real (ast_stream.py)
ES: Parsea respuestas fragmentadas de Ollama y valida bloques de código Python en tiempo real.
EN: Parses Ollama's streamed responses and validates Python code blocks in real time.
"""

from __future__ import annotations

import ast
import json
import re
import threading
from typing import Generator, Tuple, Optional
import requests

_CODE_BLOCK_RE = re.compile(r"```(?:python|py)?\s*\n?(.*?)```", re.DOTALL | re.IGNORECASE)

_DEFAULT_SESSION_LOCK = threading.Lock()
_default_session: Optional[requests.Session] = None


def _get_default_session() -> requests.Session:
    """ES: Devuelve (creando si hace falta) la sesión HTTP de módulo persistente.
    EN: Returns (creating if needed) the persistent module-level HTTP session."""
    global _default_session
    if _default_session is None:
        with _DEFAULT_SESSION_LOCK:
            if _default_session is None:
                session = requests.Session()
                adapter = requests.adapters.HTTPAdapter(pool_connections=4, pool_maxsize=4)
                session.mount("http://", adapter)
                session.mount("https://", adapter)
                _default_session = session
    return _default_session


class ASTStreamProcessor:
    """ES: Consume el stream SSE/NDJSON de Ollama y valida el código Python que va llegando.
    EN: Consumes Ollama's SSE/NDJSON stream and validates the Python code as it arrives."""
    def __init__(
        self,
        endpoint: str = "http://localhost:11434/api/generate",
        session: Optional[requests.Session] = None,
    ) -> None:
        self.endpoint = endpoint
        self._session = session or _get_default_session()
        self.last_stream_stats: Optional[dict] = None

    def stream_and_validate(
        self,
        payload: dict,
        timeout: float = 180.0
    ) -> Generator[Tuple[str, Optional[str]], None, None]:
        """
        ES: Emite una tupla (chunk_de_texto, error_ast_detectado) mientras lee el flujo SSE/NDJSON de Ollama.
        EN: Yields a (text_chunk, detected_ast_error) tuple while reading Ollama's SSE/NDJSON stream.
        """
        req_payload = payload.copy()
        req_payload["stream"] = True
        accumulated_text = ""
        last_error = None
        self.last_stream_stats = None

        try:
            with self._session.post(
                self.endpoint,
                json=req_payload,
                stream=True,
                timeout=timeout
            ) as response:
                response.raise_for_status()

                for line in response.iter_lines(decode_unicode=True):
                    if not line:
                        continue

                    try:
                        data = json.loads(line)

                        chunk = data.get("response")
                        if chunk is None:
                            chunk = data.get("message", {}).get("content", "")

                        accumulated_text += chunk

                        if "```" in accumulated_text:
                            last_error = self._check_partial_ast(accumulated_text)
                        else:
                            last_error = None

                        if data.get("done"):
                            try:
                                self.last_stream_stats = {
                                    "prompt_eval_count": int(data.get("prompt_eval_count", 0) or 0),
                                    "prompt_eval_duration": int(data.get("prompt_eval_duration", 0) or 0),
                                    "eval_count": int(data.get("eval_count", 0) or 0),
                                    "eval_duration": int(data.get("eval_duration", 0) or 0),
                                    "load_duration": int(data.get("load_duration", 0) or 0),
                                    "done_reason": data.get("done_reason"),
                                }
                            except (TypeError, ValueError):
                                self.last_stream_stats = None

                        yield chunk, last_error

                    except json.JSONDecodeError:
                        continue

        except Exception as exc:
            yield f"[ERROR STREAMING]: {exc}", str(exc)

    def _check_partial_ast(self, text: str) -> Optional[str]:
        """ES: Extrae bloques de código completos e intenta compilarlos con AST sin
        emitir falsos positivos por corte de stream (EOF).
        EN: Extracts complete code blocks and tries to compile them with AST without
        raising false positives from a truncated stream (EOF)."""
        code_blocks = _CODE_BLOCK_RE.findall(text)

        if not code_blocks and "```" in text:
            raw_block = text.split("```")[-1]
            if raw_block.startswith(("python", "py")):
                lines = raw_block.split("\n", 1)
                if len(lines) > 1:
                    code_blocks = [lines[1]]

        for code in code_blocks:
            code_clean = code.strip()
            if not code_clean:
                continue
            try:
                ast.parse(code_clean)
            except SyntaxError as exc:
                if "unexpected EOF" in str(exc) or "EOF while scanning" in str(exc):
                    continue
                return f"Línea {exc.lineno}: {exc.msg}"
        return None