<div align="center">

# ✈️ TripPilot AI
### A Multi-Agent Travel Planner with LangGraph & MCP

Turn a single sentence into a full trip plan — flights, hotels, weather, and a day-by-day itinerary.

[![Python](https://img.shields.io/badge/Python-3.10%2B-3776AB?style=flat-square&logo=python&logoColor=white)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-backend-009688?style=flat-square&logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com/)
[![LangGraph](https://img.shields.io/badge/LangGraph-multi--agent-1C3C3C?style=flat-square)](https://www.langchain.com/langgraph)
[![Groq](https://img.shields.io/badge/LLM-Groq-F55036?style=flat-square)](https://groq.com/)
[![PostgreSQL](https://img.shields.io/badge/PostgreSQL-state-4169E1?style=flat-square&logo=postgresql&logoColor=white)](https://www.postgresql.org/)
[![License](https://img.shields.io/badge/license-MIT-green?style=flat-square)](#license)

[Quick Start](#-quick-start) • [How It Works](#-how-the-workflow-works) • [API](#-api-endpoints) • [Contributing](#-contributing)

</div>

---

## 📖 Why This Project?

Planning a trip usually means juggling five browser tabs, a spreadsheet, and a group chat full of "wait what did we decide." **TripPilot** collapses that into one request, handled by a coordinated team of AI agents:

| Agent | Job |
|---|---|
| ✈️ **Flight Agent** | Researches routes, airlines, and typical fares via AviationStack |
| 🏨 **Hotel Agent** | Finds accommodation suggestions via Tavily search |
| 🌦️ **Weather Agent** | Pulls current + forecast conditions for the destination |
| 🗓️ **Itinerary Agent** | Builds a practical, budget-aware day-by-day plan |
| 📝 **Final Agent** | Formats everything into one polished response |

All five are orchestrated through a **LangGraph** state machine, so each agent's output feeds cleanly into the next.

---

## ✨ Features

- ✈️ Flight research using **AviationStack**
- 🏨 Hotel suggestions using **Tavily** search
- 🌦️ Live weather + forecast lookups
- 🧠 Multi-agent orchestration with **LangGraph**
- 📝 Structured, section-by-section itinerary generation
- 🌐 **FastAPI** backend with a simple web interface
- 💾 Conversation state persistence via **PostgreSQL** checkpointing
- ⚡ LLM-powered responses via **Groq** (fast inference)

---

## 🧭 How the Workflow Works

```mermaid
flowchart LR
    A([User Request]) --> B[✈️ Flight Agent]
    B --> C[🏨 Hotel Agent]
    C --> D[🌦️ Weather Agent]
    D --> E[🗓️ Itinerary Agent]
    E --> F[📝 Final Agent]
    F --> G([Polished Travel Plan])

    style A fill:#1C3C3C,color:#fff
    style G fill:#1C3C3C,color:#fff
```

1. The user submits a travel request in plain English.
2. The **flight agent** gathers route, airline, and fare information.
3. The **hotel agent** searches for accommodation suggestions.
4. The **weather agent** fetches current conditions and a forecast.
5. The **itinerary agent** stitches it all into a practical day-by-day plan.
6. The **final agent** formats everything into one polished response.

Every step's state is checkpointed to PostgreSQL, so conversations can resume by `thread_id`.

---

## 🛠️ Tech Stack

<table>
<tr>
<td valign="top" width="50%">

**Backend**
- Python 3.10+
- FastAPI
- LangGraph
- LangChain
- PostgreSQL (state checkpointing)

</td>
<td valign="top" width="50%">

**Frontend & APIs**
- Jinja2 + HTML/CSS/JavaScript
- Groq (LLM inference)
- Tavily API (search)
- AviationStack API (flights)

</td>
</tr>
</table>

---

## 📁 Project Structure

```
.
├── app.py                # FastAPI app entry point
├── backend.py             # LangGraph travel workflow
├── mcp_client.py          # Flight / hotel / weather data wrappers
├── requirements.txt       # Python dependencies
├── static/                # Static frontend assets
├── templates/              # HTML templates
└── tools/                  # Flight and web search integrations
```

---

## ✅ Prerequisites

- Python **3.10+**
- PostgreSQL running and accessible
- API keys for:
  - [Groq](https://console.groq.com/)
  - [Tavily](https://tavily.com/)
  - [AviationStack](https://aviationstack.com/)

---

## 🔑 Environment Variables

Create a `.env` file in the project root:

```env
DATABASE_URL=postgresql://user:password@localhost:5432/travel_db
GROQ_API_KEY=your_groq_api_key
AVIATIONSTACK_API_KEY=your_aviationstack_api_key
TAVILY_API_KEY=your_tavily_api_key
DEFAULT_ORIGIN_IATA=DAC
```

<details>
<summary><strong>🔒 Never commit your real .env — click for a quick safety checklist</strong></summary>

- Add `.env` to `.gitignore`
- Commit a `.env.example` with empty/placeholder values instead
- Rotate any key you've accidentally pushed to a public repo

</details>

---

## 🚀 Quick Start

<details open>
<summary><strong>1. Clone & set up a virtual environment</strong></summary>

```bash
git clone <your-repo-url>
cd TripPilot

python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
```
</details>

<details open>
<summary><strong>2. Install dependencies</strong></summary>

```bash
pip install -r requirements.txt
```

Using `uv` instead? 
```bash
uv sync
```
</details>

<details open>
<summary><strong>3. Configure your .env</strong></summary>

Fill in the variables shown in [Environment Variables](#-environment-variables) above.
</details>

<details open>
<summary><strong>4. Run the app</strong></summary>

```bash
python app.py
```

Then open:
```
http://127.0.0.1:8000/
```
</details>

---

## 🔌 API Endpoints

| Method | Endpoint | Description |
|---|---|---|
| `GET` | `/health` | Health check |
| `POST` | `/api/travel` | Submit a travel request |

**Example request:**

```bash
curl -X POST http://127.0.0.1:8000/api/travel \
  -H "Content-Type: application/json" \
  -d '{"message":"Plan a 3-day trip to Tokyo with a budget of $1200"}'
```

**Example response shape:**

```json
{
  "thread_id": "user_ab12cd34",
  "answer": "Trip Summary...",
  "flight_results": "...",
  "hotel_results": "...",
  "weather_results": "...",
  "itinerary": "...",
  "llm_calls": 4
}
```

---

## 🤝 Contributing

Contributions are welcome!

1. Fork the repository
2. Create a feature branch (`git checkout -b feature/my-idea`)
3. Make your changes
4. Open a pull request

---

## 📸 Screenshots

> Illustrative mockups shown below — swap these for real app screenshots once your frontend is running, using the same file paths.

| Trip request | Agent progress | Final itinerary |
|---|---|---|
| ![Trip request screen](docs/screenshots/request.png) | ![Agent progress screen](docs/screenshots/progress.png) | ![Itinerary result screen](docs/screenshots/result.png) |

---

## 📄 Resume Bullet (ATS-Optimized)

Use this line on your resume to describe this project — written with keyword density and quantifiable impact for Applicant Tracking Systems (ATS):

> Built **TripPilot**, a multi-agent travel planning system using **Python, LangGraph, LangChain, FastAPI, and PostgreSQL**, orchestrating 5 specialized AI agents (flight, hotel, weather, itinerary, response) with **Groq LLM inference**, **Tavily** and **AviationStack API** integrations, and persistent conversation state via PostgreSQL checkpointing.

**Shorter variant (1 line, resume bullet format):**

> Designed and deployed a multi-agent AI travel planner (Python, LangGraph, FastAPI, PostgreSQL) integrating Groq LLMs and third-party travel APIs to automate end-to-end trip itinerary generation.

**Tips to keep the ATS score high:**
- Keep exact tech-stack keywords from the job description (e.g. "LangGraph", "multi-agent", "LLM", "REST API", "PostgreSQL") — ATS parsers match literal strings.
- Lead with an action verb (Built / Designed / Engineered / Architected).
- Add a metric if you have one (e.g. "reduced manual trip-planning time from ~2 hours to under 2 minutes").
- Avoid tables/graphics in the actual resume file — ATS parsers often can't read them; plain bullet text only.

---

## 🙏 Acknowledgments

Built with modern LLM tooling and real-world travel APIs, as a practical example of combining **LangGraph** multi-agent orchestration with an actual usable application.

<div align="center">

Made with ✈️ and a bit of chaos-taming.

</div>