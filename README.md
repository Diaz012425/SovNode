# SovNode (Sovereign AI Node)

**SovNode** is a privacy-first, 100% offline desktop client designed to run and orchestrate local AI workflows seamlessly. Built with PyQt6 and powered locally by Ollama, it gives you absolute control over your environment without relying on external cloud servers.

## 🚀 Key Features

* **100% Offline & Private:** Operates entirely on your local machine with zero telemetry or external API calls.
* **Single-Model MoE Architecture:** Powered by `gpt-oss:20b` for unified general reasoning and code generation, optimized with Harmony protocol budgeting (`think: "low"`).
* **Instant Intent Routing:** Offloads quick queries to a lightweight `qwen2.5:0.5b` standalone router.
* **Modern PyQt6 UI:** Clean dark desktop interface with real-time streaming, LaTeX math rendering, and persistent session memory (WAL).
* **Dynamic Tool Engine:** Extensible tool engine with local file inspection, execution guardrails, and sandboxed workflows.

## 📥 Installation & Usage

1. Ensure you have **Ollama** installed and running on your local system with your preferred models.
2. Go to the **[Releases](../../releases)** tab of this repository.
3. Download the latest **`SovNode.zip`** package.
4. Extract the contents and run **`SovNode.exe`**. No installation or Python setup required!

## ⚙️ System Requirements

* **OS:** Windows 10 / 11 (64-bit)
* **Backend:** Ollama running locally (`localhost:11434`)

## 📄 License

Distributed under the GNU Affero General Public License v3.0 (AGPLv3). See the LICENSE file for more details.

## System Requirements
SovNode runs entirely on your local machine using Ollama for inference. To ensure a smooth experience with local AI workloads, your system should meet the following specifications:

## Minimum Requirements
OS: Windows 10/11 (64-bit).

CPU: 4-core processor with AVX2 instruction support.

RAM: 8 GB (required to handle the AI model and the SovNode UI simultaneously).

Storage: 10 GB free space (for Ollama binaries, AI models, and application data).

## Recommended Requirements
OS: Windows 10/11 (64-bit).

CPU: Modern 6-core processor or better.

RAM: 16 GB or higher.

GPU: Dedicated NVIDIA or AMD GPU with at least 4 GB VRAM (highly recommended for significantly faster code generation and inference).

Storage: SSD (highly recommended to improve application responsiveness and reduce AI model loading times).

Note on Performance: SovNode performance depends heavily on your local hardware. While the application's interface is lightweight (built with PyQt6), the speed of AI responses is determined by your CPU/GPU capability. An NVIDIA GPU with CUDA support will provide the optimal inference speed for complex code generation tasks.
