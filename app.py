"""
El Monolito Personal — Interfaz Gráfica Renovada (app.py)
================================================================
Capa de presentación en Streamlit v2.0 con CSS personalizado,
healthcheck del backend y trazas visuales con badges.
"""

from __future__ import annotations

import uuid
from typing import Optional

import requests
import streamlit as st

from orchestrator import MonolithOrchestrator, ProactiveAlert, TurnTrace
from wal import WriteAheadLog

# 1. Configuración de página
st.set_page_config(
    page_title="El Monolito Personal — Nodo Soberano",
    page_icon="🗿",
    layout="wide",
    initial_sidebar_state="expanded",
)

# 2. Inyección de CSS para diseño UI personalizado
st.markdown(
    """
    <style>
    .stApp {
        background-color: #0e1117;
    }
    .badge-fast {
        background-color: #1e3a8a;
        color: #93c5fd;
        padding: 2px 8px;
        border-radius: 4px;
        font-size: 0.8em;
        font-weight: bold;
    }
    .badge-slow {
        background-color: #581c87;
        color: #e9d5ff;
        padding: 2px 8px;
        border-radius: 4px;
        font-size: 0.8em;
        font-weight: bold;
    }
    .badge-status-ok {
        color: #4ade80;
        font-weight: bold;
    }
    .badge-status-err {
        color: #f87171;
        font-weight: bold;
    }
    .stExpander {
        border: 1px solid #1f2937 !important;
        background-color: #111827 !important;
        border-radius: 6px !important;
    }
    </style>
    """,
    unsafe_allow_html=True,
)

# Diccionario I18N
TEXTS = {
    "Español": {
        "sidebar_header": "🗿 Nodo Soberano Local",
        "backend_status": "Estado Ollama:",
        "session_label": "Sesión activa:",
        "turns_label": "Turnos procesados:",
        "model_label": "Modelo Ollama:",
        "alerts_header": "🔔 Alertas proactivas",
        "no_alerts": "Sin alertas pendientes del daemon.",
        "update_btn": "🔄 Actualizar",
        "clear_btn": "🧹 Limpiar chat",
        "export_btn": "💾 Exportar Chat (MD)",
        "main_title": "🗿 El Monolito Personal",
        "main_caption": "Interfaz de operación del Nodo Soberano Local — Orquestador v2.0 (Routing dual, Motores simbólicos y Ollama).",
        "chat_hint": "Escribe tu consulta al nodo...",
        "spinner": "Enrutando consulta y ejecutando verificación...",
        "toast_alert": "nueva(s) alerta(s) recibida(s).",
    },
    "English": {
        "sidebar_header": "🗿 Local Sovereign Node",
        "backend_status": "Ollama Status:",
        "session_label": "Active session:",
        "turns_label": "Processed turns:",
        "model_label": "Ollama Model:",
        "alerts_header": "🔔 Proactive Alerts",
        "no_alerts": "No pending daemon alerts.",
        "update_btn": "🔄 Refresh",
        "clear_btn": "🧹 Clear chat",
        "export_btn": "💾 Export Chat (MD)",
        "main_title": "🗿 The Personal Monolith",
        "main_caption": "Local Sovereign Node Operating Interface — Orchestrator v2.0 (Dual routing, Symbolic engines & Ollama).",
        "chat_hint": "Type your query to the node...",
        "spinner": "Routing query and executing verification...",
        "toast_alert": "new proactive alert(s) received.",
    },
}


def _check_ollama_health(endpoint: str) -> bool:
    """Verifica si el servicio local de Ollama está respondiendo."""
    try:
        base_url = endpoint.rsplit("/api", 1)[0]
        res = requests.get(base_url, timeout=1.5)
        return res.status_code == 200
    except Exception:
        return False


def _get_orchestrator() -> MonolithOrchestrator:
    if "orchestrator" not in st.session_state:
        wal_instance = WriteAheadLog("monolith.wal")
        # Sin model_name: cae a Orchestrator.RESPONSE_MODEL (qwen2.5:7b),
        # la única fuente de verdad del modelo activo — igual que hace la
        # UI Qt (sovnode_qt.py: Orchestrator(wal=...)).
        st.session_state.orchestrator = MonolithOrchestrator(wal=wal_instance)
    return st.session_state.orchestrator


def _init_session_state() -> None:
    if "messages" not in st.session_state:
        st.session_state.messages = []
    if "proactive_alerts" not in st.session_state:
        st.session_state.proactive_alerts = []
    if "session_id" not in st.session_state:
        st.session_state.session_id = str(uuid.uuid4())[:8]


_init_session_state()
orchestrator: MonolithOrchestrator = _get_orchestrator()

# Selector de idioma
selected_lang = st.sidebar.selectbox("Idioma / Language", ["Español", "English"])
t = TEXTS[selected_lang]
orchestrator.set_language(selected_lang)


def _drain_proactive_alerts(node: MonolithOrchestrator) -> int:
    drained = 0
    alert_queue = getattr(node, "alert_queue", None)
    if alert_queue is not None:
        while not alert_queue.empty():
            alert: ProactiveAlert = alert_queue.get_nowait()
            st.session_state.proactive_alerts.append(alert)
            drained += 1
    return drained


def _render_trace_expander(trace: TurnTrace) -> None:
    """Renderiza traza con badges gráficos de rendimiento y ruta."""
    is_fast = "fast_path" in trace.routing_decision.path.value.lower()
    badge_class = "badge-fast" if is_fast else "badge-slow"
    badge_text = "⚡ FAST PATH" if is_fast else "🧠 SLOW PATH"

    label = f"🔍 Traza — Turno #{trace.turn_id[:8]} | {trace.total_elapsed_ms:.1f} ms"

    with st.expander(label, expanded=False):
        st.markdown(
            f"<span class='{badge_class}'>{badge_text}</span> &nbsp; "
            f"<b>Resultado:</b> <code>{trace.outcome.value}</code>",
            unsafe_allow_html=True,
        )
        st.markdown("---")

        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Tiempo Total", f"{trace.total_elapsed_ms:.1f} ms")
        c2.metric("Score Routing", f"{trace.routing_decision.score:+.1f}")
        c3.metric(
            "Nodo Persistido", "Sí" if trace.knowledge_node_persisted else "No"
        )
        c4.metric(
            "Confianza",
            f"{getattr(trace, 'confidence_label', 'N/D')} ({getattr(trace, 'confidence_score', 0):.2f})",
        )

        if trace.engine_results:
            st.markdown("**Resultados de Motores Simbólicos:**")
            for res in trace.engine_results:
                st.code(res, language="text")

        genius_badges = []
        if getattr(trace, "thought_code_verified", False):
            genius_badges.append("🧪 Verificado en sandbox")
        if getattr(trace, "tot_used", False):
            genius_badges.append(f"🌳 Tree-of-Thoughts ({getattr(trace, 'tot_agreement', 0):.0%} acuerdo)")
        if getattr(trace, "epistemic_drift_detected", False):
            genius_badges.append("⚠️ Deriva epistémica corregida")
        if genius_badges:
            st.caption(" · ".join(genius_badges))

        st.caption(f"Status Lógico: `{trace.logical_status}` | Web Context: `{trace.web_context_used}`")


def _generate_markdown_export() -> str:
    """Genera un archivo Markdown del historial activo para descargar."""
    output = [f"# Historial de Sesión — {st.session_state.session_id}\n"]
    for msg in st.session_state.messages:
        role = "Usuario" if msg["role"] == "user" else "Nodo Monolito"
        output.append(f"### {role}\n{msg['content']}\n")
    return "\n".join(output)


# --- BARRA LATERAL ---
turn_count = len([m for m in st.session_state.messages if m["role"] == "user"])
ollama_online = _check_ollama_health(orchestrator.ollama_endpoint)

with st.sidebar:
    st.markdown(f"### {t['sidebar_header']}")

    status_html = (
        "<span class='badge-status-ok'>🟢 Online</span>"
        if ollama_online
        else "<span class='badge-status-err'>🔴 Offline</span>"
    )
    st.markdown(f"**{t['backend_status']}** {status_html}", unsafe_allow_html=True)
    st.markdown(f"**{t['session_label']}** `{st.session_state.session_id}`")
    st.markdown(f"**{t['turns_label']}** {turn_count}")
    st.markdown(f"**{t['model_label']}** `{orchestrator.ollama_model}`")

    st.markdown("---")
    st.markdown(f"### {t['alerts_header']}")
    nuevas = _drain_proactive_alerts(orchestrator)
    if nuevas:
        st.toast(f"{nuevas} {t['toast_alert']}", icon="🔔")

    if st.session_state.proactive_alerts:
        for alert in reversed(st.session_state.proactive_alerts[-15:]):
            st.warning(alert.report())
    else:
        st.caption(t["no_alerts"])

    st.markdown("---")
    col_a, col_b = st.columns(2)
    with col_a:
        if st.button(t["update_btn"], use_container_width=True):
            st.rerun()
    with col_b:
        if st.button(t["clear_btn"], use_container_width=True):
            # Nota: antes solo se vaciaba st.session_state.messages,
            # dejando intacta la memoria conversacional persistente
            # (MemoryGraph.conversation_turns/web_knowledge/reasoning_
            # lessons + el índice vectorial en memoria de proceso) - el
            # siguiente turno seguía arrastrando historial de la sesión
            # "limpiada". session_id también se renueva para que quede
            # claro, en cualquier log/export, que se trata de una sesión
            # nueva de verdad.
            st.session_state.messages = []
            st.session_state.proactive_alerts = []
            st.session_state.session_id = str(uuid.uuid4())[:8]
            orchestrator.clear_conversation_memory()
            st.rerun()

    # Descargar historial
    if st.session_state.messages:
        st.download_button(
            label=t["export_btn"],
            data=_generate_markdown_export(),
            file_name=f"monolith_session_{st.session_state.session_id}.md",
            mime="text/markdown",
            use_container_width=True,
        )

# --- ÁREA PRINCIPAL ---
st.title(t["main_title"])
st.caption(t["main_caption"])

# Renderizado de mensajes
for entry in st.session_state.messages:
    with st.chat_message(entry["role"]):
        st.markdown(entry["content"])
        trace: Optional[TurnTrace] = entry.get("trace")
        if entry["role"] == "assistant" and trace is not None:
            _render_trace_expander(trace)

# Input del usuario
user_input = st.chat_input(t["chat_hint"])

if user_input:
    st.session_state.messages.append({"role": "user", "content": user_input, "trace": None})
    with st.chat_message("user"):
        st.markdown(user_input)

    with st.chat_message("assistant"):
        with st.spinner(t["spinner"]):
            trace: TurnTrace = orchestrator.process_turn(user_input)
        st.markdown(trace.final_response)
        _render_trace_expander(trace)

    st.session_state.messages.append(
        {
            "role": "assistant",
            "content": trace.final_response,
            "trace": trace,
        }
    )

    if _drain_proactive_alerts(orchestrator):
        st.rerun()
