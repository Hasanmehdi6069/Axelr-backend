# AI Agents Module - Production Documentation

**Module ID**: ai_agents  
**Status**: Production Ready  
**Last Updated**: Master production finalization

---

## Overview

AI Agents is a multi-agent orchestration platform that provides wait-for-graph cycle detection, agent state management, and inter-agent communication. This module builds on top of the core Sovereign Engine and Anti-Hallucination Engine to enable complex multi-agent workflows.

## Core Capabilities

- **Graph-based Orchestration**: Directed acyclic graph agent workflow definition
- **Cycle Detection**: Automatic wait-for-graph cycle detection and deadlock prevention
- **State Persistence**: SQLite-backed agent state with WAL mode
- **Inter-agent Communication**: In-memory message bus with queue backpressure
- **Agent Lifecycle Management**: Spawn, pause, resume, terminate operations

## Implementation Details

### Architecture Flow
```
1. Agent workflow graph is submitted via API
2. Cycle detector validates graph acyclicity
3. Agent instances are spawned for each node
4. Message bus routes inter-agent communications
5. State changes are persisted to SQLite
6. Workflow completes when all terminal nodes finish
```

### Key Components
- `AgentOrchestrator`: Main workflow execution engine
- `CycleDetector`: Wait-for graph analysis and deadlock prevention
- `AgentStateStore`: SQLite-backed persistent state
- `MessageBus`: Inter-agent communication layer
- `BaseAgent`: Abstract base class for all agent implementations

## Run Instructions

### Prerequisites
- Core Sovereign Engine and AHE enabled
- Minimum 4GB RAM for agent execution
- SQLite 3.37+ with WAL mode support

### Configuration
```env
# AI Agents Settings
AGENTS_MAX_CONCURRENT=16
AGENTS_DEFAULT_TIMEOUT=300
AGENTS_STATE_DB=./data/agents_state.db
AGENTS_MESSAGE_BUS_SIZE=1000
AGENTS_CYCLE_DETECTION_ENABLED=true
```

### Start Module
```bash
# Run module health check
python -c "from modules.ai_agents import AgentOrchestrator; print('AI Agents OK:', AgentOrchestrator().health_check())"

# Module is automatically loaded with core API server
uvicorn api.main:app --host 0.0.0.0 --port 8000
```

### API Endpoints
- `POST /api/v1/agents/workflows` - Submit new agent workflow
- `GET /api/v1/agents/workflows/{id}` - Workflow status
- `POST /api/v1/agents/{id}/pause` - Pause running agent
- `POST /api/v1/agents/{id}/resume` - Resume paused agent
- `DELETE /api/v1/agents/{id}` - Terminate agent

## Performance Characteristics
- **Workflow Validation**: <10ms (graph cycle detection)
- **Agent Spawn Time**: <50ms per agent
- **Message Latency**: <1ms (inter-agent communication)
- **State Persistence**: <5ms per transaction (SQLite WAL)

---