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
   
To run from source:
```
git clone [https://github.com/drastico01/SovNode.git](https://github.com/drastico01/SovNode.git)
cd SovNode
python sovnode_qt.py
```

## ⚙️ Model Setup (Ollama)

* **OS:** Windows 10 / 11 (64-bit)
* **Backend:** Ollama running locally (`localhost:11434`)
SovNode requires **Ollama** installed and running locally (`localhost:11434`). Pull the required models before launching:

```
ollama pull gpt-oss:20b
ollama pull qwen2.5:0.5b
```

## 📄 License

Distributed under the GNU Affero General Public License v3.0 (AGPLv3). See the LICENSE file for more details.

## 📊 Benchmarks & System Evals

- **Testbed Hardware:** AMD Radeon RX 5500 XT (8GB VRAM) / 16GB System RAM
- **Inference Throughput:** 15.5 - 19.0 tokens/sec continuous generation (`gpt-oss:20b` / `qwen2.5:7b` Q4_K_M)
- **Router Latency:** < 20 ms intent classification using `qwen2.5:0.5b`
- **VRAM Memory Control:** Dynamic `keep_alive` offloading (0 MB leak across model swaps)
- **UI Thread Stability:** 100% Async PyQt6 execution (0% frame drops during web scraping/RAG ingestion)
  
## 💻 System Requirements
SovNode runs entirely on your local machine using Ollama for inference. To ensure a smooth experience with local AI workloads, your system should meet the following specifications:

### Minimum Requirements
OS: Windows 10/11 (64-bit)

CPU: 4-core processor with AVX2

RAM: 16 GB

GPU: 4 GB VRAM

Storage: 15 GB free space (for Ollama binaries, AI models, and application data)

### Recommended Requirements
OS: Windows 10/11 (64-bit)

CPU: Modern 6-core or 8-core CPU

RAM: 16 GB+

GPU: Dedicated GPU with 8 GB+ VRAM

Storage: 25 GB free SSD space

Note on Performance: SovNode performance depends heavily on local hardware capability. While the application's interface is lightweight (built with PyQt6), the speed of AI responses is determined by your CPU/GPU capability. A dedicated GPU with VRAM/CUDA support will provide optimal inference speed.
