"""
SovNode — Streaming AST Parser en Tiempo Real (ast_stream.py)
Parsea respuestas fragmentadas de Ollama y valida bloques de código Python en tiempo real.
"""

from __future__ import annotations

import ast
import json
import re
import threading
from typing import Generator, Tuple, Optional
import requests

_CODE_BLOCK_RE = re.compile(r"```(?:python|py)?\s*\n?(.*?)```", re.DOTALL | re.IGNORECASE)

# Transporte local persistente (Optimización #3): Ollama solo expone HTTP
# sobre loopback - no hay socket de dominio UNIX ni memoria compartida que
# activar. El equivalente real de bajo overhead es evitar que cada turno
# pague una negociación TCP nueva: `requests.post()` como función de
# módulo abre una Session desechable por llamada (sin reutilización de
# conexión); esta Session de módulo, con keep-alive y pool de conexiones,
# se comparte entre todas las instancias de ASTStreamProcessor que no
# reciban una sesión explícita.
_DEFAULT_SESSION_LOCK = threading.Lock()
_default_session: Optional[requests.Session] = None


def _get_default_session() -> requests.Session:
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
    def __init__(
        self,
        endpoint: str = "http://localhost:11434/api/generate",
        session: Optional[requests.Session] = None,
    ) -> None:
        self.endpoint = endpoint
        # Si el llamador ya tiene una Session persistente (p. ej.
        # Orchestrator._session), se reutiliza esa - mismo pool de
        # conexiones TCP para las llamadas streaming y no-streaming, en
        # vez de mantener dos pools separados hacia el mismo backend
        # local. Si no se provee ninguna, cae a la Session de módulo
        # persistente de arriba (nunca a `requests.post` desechable).
        self._session = session or _get_default_session()
        # Métricas reales de la última llamada streaming (prefill/decode
        # tok/s tal como las reporta Ollama en el chunk final con
        # "done": true) - antes se descartaban por completo. Se exponen
        # acá en vez de cambiar la forma de la tupla que yield-ea
        # stream_and_validate(), para no tocar sus 4 call sites
        # existentes en sovnode_qt.py. El llamador lee este atributo
        # después de agotar el generador (cuando ya se recibió el chunk
        # final con done=true) y antes de reutilizar el mismo processor
        # para otra llamada (p. ej. entre Pasada 1 y Pasada 2 del modo
        # TwoPass), porque cada nueva llamada lo resetea a None.
        self.last_stream_stats: Optional[dict] = None

    def stream_and_validate(
        self,
        payload: dict,
        timeout: float = 180.0
    ) -> Generator[Tuple[str, Optional[str]], None, None]:
        """
        Emite una tupla (chunk_de_texto, error_ast_detectado) mientras lee el flujo SSE/NDJSON de Ollama.
        """
        req_payload = payload.copy()
        req_payload["stream"] = True
        accumulated_text = ""
        last_error = None
        self.last_stream_stats = None

        try:
            # Uso de context manager para evitar fuga de sockets HTTP durante la transmisión
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

                        # Extrae el chunk tanto si la API es /api/generate como si es /api/chat
                        chunk = data.get("response")
                        if chunk is None:
                            chunk = data.get("message", {}).get("content", "")

                        accumulated_text += chunk

                        # Verificación parcial de código AST al detectar bloques Markdown
                        if "```" in accumulated_text:
                            last_error = self._check_partial_ast(accumulated_text)
                        else:
                            last_error = None

                        # El chunk final de Ollama (done=true) trae las
                        # métricas reales de prefill/decode - se capturan
                        # acá sin alterar lo que se yield-ea.
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
        """Extrae bloques completos e intenta compilarlos con AST sin emitir falsos positivos por EOF."""
        code_blocks = _CODE_BLOCK_RE.findall(text)
        
        # Si hay un bloque abierto activo al final del stream sin cerrar con ```
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
                # Omitir errores causados únicamente por transmisión incompleta del stream (EOF)
                if "unexpected EOF" in str(exc) or "EOF while scanning" in str(exc):
                    continue
                return f"Línea {exc.lineno}: {exc.msg}"
        return None