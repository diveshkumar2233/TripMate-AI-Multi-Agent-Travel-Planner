# ✈️ TripPilot AI — Multi-Agent System using LangGraph + MCP — Supervisor, Guardrails & HITL
### Multi-Agent System using LangGraph + MCP — Supervisor, Guardrails & HITL

A demo multi-agent travel-planning assistant built with **LangGraph** and **MCP**, featuring a Supervisor agent, input Guardrails, and Human-In-The-Loop (HITL) approval flows.

🔗 **Live Demo:** https://tripmate-ai-multi-agent-travel-planner-3.onrender.com/

[![Python](https://img.shields.io/badge/Python-3.10%2B-3776AB?style=flat-square&logo=python&logoColor=white)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-backend-009688?style=flat-square&logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com/)
[![LangGraph](https://img.shields.io/badge/LangGraph-multi--agent-1C3C3C?style=flat-square)](https://www.langchain.com/langgraph)
[![MCP](https://img.shields.io/badge/MCP-model--context--protocol-6E56CF?style=flat-square)](https://modelcontextprotocol.io/)
[![Groq](https://img.shields.io/badge/LLM-Groq-F55036?style=flat-square)](https://groq.com/)
[![PostgreSQL](https://img.shields.io/badge/PostgreSQL-state-4169E1?style=flat-square&logo=postgresql&logoColor=white)](https://www.postgresql.org/)
[![License](https://img.shields.io/badge/license-see%20LICENSE-green?style=flat-square)](#-license)
[![Live Demo](https://img.shields.io/badge/demo-live-brightgreen?style=flat-square)](https://tripmate-ai-multi-agent-travel-planner-2.onrender.com)

[Key Ideas](#-key-ideas) • [What You Built](#-what-you-built) • [Quick Start](#-quick-start-windows) • [Results](#-results--performance) • [Decisions](#-design-decisions) • [API](#-api-endpoints) • [Contributing](#-contributing)

---

## 💡 Key Ideas

- 🧠 **Multi-agent coordination** using LangGraph and MCP
- 🧭 **Supervisor agent** to manage complex workflows and route to specialists
- 🛡️ **Input guardrails** to validate user requests before they reach the agents
- ✅ **Human-in-the-loop approval** for generated plans before finalizing

---

## 🏗️ What You Built

TripPilot AI is a full travel-planning pipeline, not a single prompt-and-response bot:

- A **guardrail layer** rejects off-topic or unsafe requests before any agent runs, so downstream agents never waste a call on garbage input.
- A **supervisor agent** reads the request and dynamically decides which specialists actually apply — a weekend city trip might only need the hotel and weather agents, while a longer international trip pulls in flights and budget too.
- Four **specialist agents** (flight, hotel, weather, budget) each own one concern and write their findings into shared graph state, so the itinerary agent doesn't need to know how any of them work internally.
- An **itinerary agent** merges everything into a draft plan and stops — it does not auto-finalize. The plan sits in a pending state until a human approves it or sends feedback, which routes back into another itinerary pass.
- A **final agent** only runs after approval, polishing the accepted draft into the response the user sees.
- Conversation and approval state is checkpointed in **PostgreSQL** via LangGraph, so a thread can be resumed later instead of restarting the whole conversation.
- A working **MCP server example** (`custom_weather_mcp_server.py`) shows how a domain adapter plugs into the agent graph over the Model Context Protocol, rather than being hardcoded as a Python function call.

---

## 🧭 How the Workflow Works

```mermaid
flowchart LR
    A([User Request]) --> G{🛡️ Guardrail}
    G -- blocked --> X([Blocked Response])
    G -- allowed --> S[🧭 Supervisor]
    S --> F[✈️ Flight Agent]
    S --> H[🏨 Hotel Agent]
    S --> W[🌦️ Weather Agent]
    S --> B[💰 Budget Agent]
    F --> I[🗓️ Itinerary Agent]
    H --> I
    W --> I
    B --> I
    I --> HITL{✅ Human Review}
    HITL -- approved --> Fin[📝 Final Agent]
    HITL -- feedback --> I
    Fin --> Done([Polished Travel Plan])

    style A fill:#1C3C3C,color:#fff
    style Done fill:#1C3C3C,color:#fff
    style G fill:#6E56CF,color:#fff
    style HITL fill:#6E56CF,color:#fff
```

1. The user submits a travel request.
2. The **guardrail** checks the request is valid travel-planning content — unrelated or unsafe requests are blocked immediately.
3. The **supervisor** decides which specialist agents are actually needed (flight, hotel, weather, budget) and always includes the itinerary agent.
4. Selected specialists run, each contributing to shared state.
5. The **itinerary agent** drafts a plan and pauses for **human review (HITL)**.
6. On approval, the **final agent** polishes and returns the plan. On feedback, it loops back for revision.

---

## 📁 Contents

| File | Purpose |
|---|---|
| `app.py` | FastAPI web frontend and API endpoints |
| `backend.py` | Core agent orchestration / travel-planner logic (Supervisor, Guardrails, HITL) |
| `mcp_client.py` | Client helpers to interact with the MCP server |
| `custom_weather_mcp_server.py` | Example MCP server for weather checks |
| `templates/`, `static/` | Frontend UI assets (HTML, JS, CSS) |

---

## ✨ Features

- 🌐 Interactive web UI for sending travel-planning prompts
- 📝 Endpoint for drafting travel plans, with a **separate approval endpoint**
- 🔌 Example MCP server demonstrating domain adapters (weather, checkpoints)
- 🛡️ Guardrails that validate requests before any agent runs
- 🧭 Supervisor that dynamically selects only the agents a request needs
- 💾 Conversation state persistence via PostgreSQL checkpointing

---

## ✅ Prerequisites

- Python **3.10+** (recommended)
- Git (to clone the repo)
- A virtual environment tool (`venv` or similar)

---

## 🚀 Quick Start (Windows)

<details open>
<summary><strong>1. Create and activate a virtual environment</strong></summary>

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1    # PowerShell
```

Using cmd.exe instead?
```cmd
.venv\Scripts\activate
```
</details>

<details open>
<summary><strong>2. Install dependencies</strong></summary>

```bash
pip install -r requirements.txt
```
</details>

<details open>
<summary><strong>3. Run the FastAPI app (development)</strong></summary>

```bash
# option A (run module)
python app.py

# option B (uvicorn)
uvicorn app:app --reload --host 127.0.0.1 --port 8001
```
</details>

<details open>
<summary><strong>4. Open the web UI</strong></summary>

Visit **http://127.0.0.1:8001** in your browser to use the TripPilot frontend.
</details>

---

## 🛰️ Running the MCP Server (Example)

The repository includes `custom_weather_mcp_server.py` as an example MCP server. Run it in a **separate terminal** to experiment with custom adapters used by the demo:

```bash
python custom_weather_mcp_server.py
```

---

## 📸 Screenshots

> Illustrative mockups shown below — swap these for real app screenshots once your frontend is running, using the same file paths.

| Trip request | Agent progress | Final itinerary |
|---|---|---|
| ![Trip request screen](docs/screenshots/request.png) | ![Agent progress screen](docs/screenshots/progress.png) | ![Itinerary result screen](docs/screenshots/result.png) |

---

## 📊 Results & Performance

> ⚠️ **Fill this in with real, measured numbers before publishing.** Don't reuse generic stats like "94% accuracy" or "10K+ users" unless you actually measured them on this project — an interviewer asking "how did you get that number?" is the fastest way to lose credibility. Suggested things to actually measure and report:

| Metric | How to measure it |
|---|---|
| Guardrail block/allow accuracy | Hand-label ~30–50 sample prompts (valid travel requests vs. off-topic/unsafe) and check how many the guardrail classifies correctly |
| Supervisor agent-selection accuracy | For a sample of requests, note which agents *should* fire (e.g. no flight agent for a local staycation) vs. which the supervisor actually picked |
| End-to-end plan generation time | Time from `/api/travel` request to itinerary draft ready for review, averaged over several runs |
| HITL revision rate | Of test plans generated, what fraction needed at least one feedback loop before approval |

Once you have real numbers, replace this table with 2–4 short stats, each with a one-line note on how it was measured.

---

## 🧠 Design Decisions

Full reasoning also lives in [`DECISIONS.md`](./DECISIONS.md). Key choices below.

### 1. Supervisor + specialist agents instead of one agent with many tools

**Choice:** A dedicated Supervisor agent decides which of the flight/hotel/weather/budget agents to invoke, rather than giving one agent all four tools and letting it decide per-call.

**Why:** As the tool count on a single agent grows, tool-selection accuracy tends to degrade and the agent's reasoning trace gets harder to debug. A supervisor that only does routing is a smaller, more testable decision, and each specialist agent can be developed, tested, and swapped independently.

**Trade-offs considered:**
- Extra hop (supervisor call before any specialist runs) adds latency.
- If the supervisor misroutes, the error happens before any useful work starts, so its accuracy matters more than any single specialist's.
- Chosen because the project's whole point is demonstrating multi-agent orchestration patterns, not minimizing latency.

### 2. Guardrail as a separate step before the supervisor, not inside it

**Choice:** Input validation runs as its own graph node before the supervisor is even invoked, instead of being folded into the supervisor's prompt.

**Why:** Keeping "is this request valid" separate from "which agents does this need" means a blocked request costs one LLM call instead of a full supervisor + specialist chain, and the guardrail logic can be tested and tuned without touching routing logic at all.

**Trade-offs considered:**
- Adds a hard gate that could reject a legitimate but oddly-phrased request; needs decent guardrail prompt tuning to avoid false blocks.
- Worth it for cost control — rejecting bad input for one LLM call rather than five is a meaningful saving once this runs at any volume.

### 3. PostgreSQL for LangGraph checkpointing instead of in-memory or SQLite

**Choice:** Conversation and approval state is persisted via LangGraph's PostgreSQL checkpointer.

**Why:** The HITL flow depends on a thread being resumable after the process restarts or the user comes back later — a plan can sit "pending approval" for an arbitrary amount of time. In-memory state would lose that on any restart, and SQLite doesn't handle concurrent access from a multi-worker FastAPI deployment as cleanly as Postgres does.

**Trade-offs considered:**
- Requires a running Postgres instance (`DATABASE_URL`) instead of zero-setup local storage — more moving parts for a demo project.
- Chosen because HITL is a core feature here, not an afterthought, and losing a pending approval on restart would break the main demo flow.

---

## 🔌 API Endpoints

| Method | Endpoint | Description |
|---|---|---|
| `POST` | `/api/travel` | Create or resume a travel-planning thread. JSON: `{ "message": "<user prompt>", "thread_id": "optional-thread-id" }` |
| `POST` | `/api/travel/approve` | Approve or request revisions for a draft. JSON: `{ "thread_id": "<id>", "approved": true\|false, "feedback": "optional" }` |
| `GET` | `/health` | Basic health check and features list |

**Example — create a plan:**
```bash
curl -X POST http://127.0.0.1:8001/api/travel \
  -H "Content-Type: application/json" \
  -d '{"message":"Plan a 3-day trip to Tokyo with a budget of $1200"}'
```

**Example — approve a draft:**
```bash
curl -X POST http://127.0.0.1:8001/api/travel/approve \
  -H "Content-Type: application/json" \
  -d '{"thread_id":"user_ab12cd34","approved":true}'
```

---

## 🔑 Configuration & Environment

Secrets and API keys are **not included in the repo**. Use environment variables or a `.env` file for any required keys consumed by `langgraph`, `langchain`, or other adapters:

```env
DATABASE_URL=postgresql://user:password@localhost:5432/travel_db
GROQ_API_KEY=your_groq_api_key
AVIATIONSTACK_API_KEY=your_aviationstack_api_key
TAVILY_API_KEY=your_tavily_api_key
DEFAULT_ORIGIN_IATA=DEL
```

<details>
<summary><strong>🔒 Never commit your real .env — click for a quick safety checklist</strong></summary>

- Add `.env` to `.gitignore`
- Commit a `.env.example` with empty/placeholder values instead
- Rotate any key you've accidentally pushed to a public repo

</details>

---

## 🛠️ Development Notes

- The project keeps **synchronous convenience wrappers** in `backend.py` while running an **async FastAPI server** — `nest_asyncio` is applied in `app.py` to allow the sync helpers to call async MCP helpers.
- Tests are not included; to experiment, interact with the web UI or call the API endpoints directly.

---

## 📄 Resume Bullet (ATS-Optimized)

Use this line on your resume to describe this project — written with keyword density for Applicant Tracking Systems (ATS):

> Built **TripPilot AI**, a multi-agent travel planning system using **Python, LangGraph, MCP, LangChain, FastAPI, and PostgreSQL**, with a Supervisor agent, input Guardrails, and Human-in-the-Loop approval flow, orchestrating specialist agents (flight, hotel, weather, budget, itinerary) via **Groq LLM inference** and **Tavily / AviationStack** API integrations.

**Shorter variant (1 line, resume bullet format):**

> Designed and deployed a multi-agent AI travel planner (Python, LangGraph, MCP, FastAPI, PostgreSQL) with Supervisor routing, input guardrails, and human-in-the-loop review to automate end-to-end trip itinerary generation.

**Tips to keep the ATS score high:**
- Keep exact tech-stack keywords from the job description (e.g. "LangGraph", "MCP", "multi-agent", "LLM", "REST API", "PostgreSQL") — ATS parsers match literal strings.
- Lead with an action verb (Built / Designed / Engineered / Architected).
- Add a metric if you have one — from the Results section above, once measured.
- Avoid tables/graphics in the actual resume file — ATS parsers often can't read them; plain bullet text only.

---

## 🤝 Contributing

Contributions are welcome. Please open issues or pull requests for bug fixes, documentation improvements, or new adapter examples.

1. Fork the repository
2. Create a feature branch (`git checkout -b feature/my-idea`)
3. Make your changes
4. Open a pull request

---

## 📜 License

This repository follows the license in the `LICENSE` file.

---

## 🙏 Acknowledgements

Built as a demonstration of **LangGraph + MCP** patterns with Supervisor and Guardrail concepts.

---

## 📬 Contact

For questions or suggestions, open an issue or contact the repository owner.


Made with ✈️ and a bit of chaos-taming.
