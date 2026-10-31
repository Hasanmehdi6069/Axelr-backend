# AXELR AI Demo

12 production-grade Python modules with a professional demo dashboard, real WebSocket logging, and honest health reporting. No fake logs, no forced green states.

## 📦 Modules Overview

| Module | Description | Category |
|--------|-------------|----------|
| `axelr_nebius_nvidia` | Nebius NVIDIA GPU inference bridge with streaming completions, rate limiting, and failover | Cloud GPU · Inference |
| `axelr_ugreen_ai_nas` | UGREEN AI NAS edge file indexing with watchdog change detection | Edge · File Storage |
| `axelr_ai_agents` | AI Agents platform with wait-for-graph cycle detection | Orchestration |
| `axelr_opencv_ai` | OpenCV AI pipeline with RAII frame disposal and pooled buffers | Computer Vision |
| `axelr_paypal` | PayPal FinTech integration with Orders v2 and webhook verification | Fintech · Payments |
| `axelr_vultr` | Vultr Cloud provisioning with cost-guarded launch and orphan detection | Cloud · Infrastructure |
| `axelr_insights_engine` | Data platform with MongoDB audit logging and TF-IDF search | Data · Analytics |
| `axelr_meta_ai` | Meta AI Gateway with Llama 3 multi-step function calling | AI · Gateway |
| `axelr_buildx_india` | Zero-trust security platform with multi-tenant auth | Security · Identity |
| `axelr_ai_genesis` | AI model registry with memory-pressure eviction | AI · Model Management |
| `axelr_buildhybridai` | MLOps platform for edge-cloud workload placement | MLOps · Deployment |
| `axelr_bharat_agentic` | Resilience framework with SQLite WAL and reconciliation | Resilience · Persistence |

## 🚀 Quick Start

### Prerequisites
- Node.js 18+ (for WebSocket relay)
- Python 3.10+ (for module execution)
- ffmpeg (optional, for post-processing recordings)

### One-Click Recording (Windows)
Run the polished Windows batch file to start everything automatically:
```cmd
ONE-CLICK-RECORD.bat
```

This script:
1. Validates all system prerequisites
2. Installs Node dependencies if missing
3. Displays clear recording instructions
4. Counts down before starting the demo
5. Shows post-demo ffmpeg merge commands

### Manual Setup
1. Install dependencies:
```bash
npm install ws ts-node typescript @types/node dotenv --no-save
```

2. Configure environment:
```bash
cp .env.demo.example .env.demo
# Edit .env.demo to set your MongoDB URI if needed
```

3. Start the WebSocket relay:
```bash
npx ts-node demo-master.ts
```

4. Open `Axelr-Frontend/hackathon-demo.html` in Chrome/Edge
5. Click "START RECORDING" to begin the demo

## 🧪 Testing All Modules

Verify all 12 modules import and pass health checks with these commands:

```bash
# PayPal
python -c "from Axelr-backend.hackathons.axelr_paypal import health_probe; print(health_probe())"

# Vultr
python -c "from Axelr-backend.hackathons.axelr_vultr import health_probe; print(health_probe())"

# Nebius NVIDIA
python -c "from Axelr-backend.hackathons.axelr_nebius_nvidia import health; print(health())"

# Insights Engine
python -c "from Axelr-backend.hackathons.axelr_insights_engine import health; print(health())"

# UGREEN AI NAS
python -c "from Axelr-backend.hackathons.axelr_ugreen_ai_nas import health; print(health())"

# OpenCV AI
python -c "from Axelr-backend.hackathons.axelr_opencv_ai_competition import health; print(health())"

# Meta AI Gateway
python -c "from Axelr-backend.hackathons.axelr_meta_global_ai import health; print(health())"

# AI Genesis
python -c "from Axelr-backend.hackathons.axelr_ai_genesis_2026 import health; print(health())"

# Build Hybrid AI
python -c "from Axelr-backend.hackathons.axelr_buildhybridai_2026 import health; print(health())"

# Bharat Agentic
python -c "from Axelr-backend.hackathons.axelr_bharat_agentic import health; print(health())"
```

### Run Project Tests
```bash
pytest Axelr-backend/core/hackathon/
```

## 🎬 Recording Workflow

1. When the countdown begins, press `Win+Alt+R` to start Windows Game Bar recording
2. The demo dashboard will automatically run all module health checks
3. When complete, your recording is saved to `%USERPROFILE%\Videos\Captures`
4. Merge with voiceover using the provided ffmpeg commands:

```bash
# First merge video and audio
ffmpeg -i video-raw.mp4 -i voice.mp3 -c:v copy -c:a aac -b:a 192k -shortest merged.mp4

# Then normalize audio levels for production
ffmpeg -i merged.mp4 -af "loudnorm=I=-16:TP=-1.5:LRA=11" -c:v copy axelr-demo-final.mp4
```

## 🏗️ Architecture

### demo-master.ts
- Auto-discovers all axelr_* modules (no hardcoded paths)
- Starts WebSocket relay on ws://127.0.0.1:8765
- Waits for `{"event":"start_demo"}` from dashboard before running
- Executes real Python health checks for each module
- Broadcasts all events over WebSocket to the dashboard
- Writes real audit logs to MongoDB if configured
- Handles SIGINT/SIGTERM for clean shutdown

### hackathon-demo.html
- Professional dark theme dashboard (Linear/Vercel/Stripe inspired)
- Connects exclusively to WebSocket for all events (no fake logs)
- Requires user gesture to start recording (browser compliance)
- Saves and displays recordings inline after completion
- Real-time module status, progress bar, and event stream
- URL query support: `?ws=ws://127.0.0.1:9000` for custom WS endpoints

## 📄 License
All rights reserved. AXELR AI - Production-Grade AI Infrastructure