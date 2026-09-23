from tavily import TavilyClient
import os
from dotenv import load_dotenv

load_dotenv()

client = TavilyClient(
    api_key= os.getenv("TAVILY_API_KEY")
)


def tavily_search(query):
    try:
        response = client.search(
            query=query,
            max_results=5,
        )
    except Exception as exc:
        # Search is an optional source. Network/firewall failures should not
        # take down the whole travel-planning request.
        print(
            f"Tavily search unavailable ({type(exc).__name__}); continuing without live search results.",
            flush=True,
        )
        return []

    results = []

    for i, r in enumerate(response["results"], 1):
        title   = r.get("title", "Unknown")
        url     = r.get("url", "")
        snippet = r.get("content", "").strip()
        # Keep only the first 300 characters to avoid wall-of-text
        if len(snippet) > 300:
            snippet = snippet[:300].rsplit(" ", 1)[0] + "..."

        results.append(f"{i}. **{title}**\n   {url}\n   {snippet}")

    return "\n\n".join(results)
