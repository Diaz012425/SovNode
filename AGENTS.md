# AGENTS.md - System Onboarding & Architecture Guide

## 1. Project Overview
SovNode is a privacy-first, 100% offline desktop AI engine built with Python and PyQt6. It orchestrates local LLMs via Ollama, integrates FAISS vector search, and provides dynamic web scraping tools without blocking the main UI thread.

## 2. Architecture & Core Modules
- `src/core/router.py`: Micro-model (0.5B) intent classifier for routing queries to proper pipelines.
- `src/core/orchestrator.py`: Async pipeline dispatcher managing model context and tool executions.
- `src/core/ollama_manager.py`: VRAM allocation controller handling `keep_alive` dynamic model loading/unloading.
- `src/ui/`: PyQt6 UI thread isolation, Markdown rendering, and real-time LaTeX engine (`math_render.py`).

## 3. Development Rules & Conventions
- **UI Thread Guardrail:** Never run blocking synchronous I/O or LLM inference directly on the main PyQt UI thread. Use worker threads and async dispatchers.
- **VRAM Control:** Always handle model unloading explicitly when switching routing paths if VRAM limits are reached.
- **Parsing Resiliency:** Ensure JSON responses from micro-models pass through `robust_json_parser.py` before execution.

## 4. Running Tests
Execute regression and system verification suites before pushing:
```bash
python -m unittest discover -s tests
