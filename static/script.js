const CLIENT_BUILD_ID = "trip-planner-demo-v11";
let currentThreadId = localStorage.getItem("travel_thread_id") || null;
if (currentThreadId && currentThreadId.startsWith("offline_")) {
    localStorage.removeItem("travel_thread_id");
    currentThreadId = null;
}
let latestAnswerMarkdown = "";

function setPrompt(text) {
    document.getElementById("userInput").value = text;
}

function setLoading(isLoading) {
    const sendBtn = document.getElementById("sendBtn");
    const btnText = document.getElementById("btnText");
    const btnLoader = document.getElementById("btnLoader");

    sendBtn.disabled = isLoading;

    if (isLoading) {
        btnText.classList.add("hidden");
        btnLoader.classList.remove("hidden");
    } else {
        btnText.classList.remove("hidden");
        btnLoader.classList.add("hidden");
    }
}

function showError(message) {
    const errorBox = document.getElementById("errorBox");

    errorBox.textContent = message;
    errorBox.classList.remove("hidden");
}

function hideError() {
    const errorBox = document.getElementById("errorBox");

    errorBox.classList.add("hidden");
    errorBox.textContent = "";
}

function compactResearch(kind, content) {
    const clean = content.replace(/\s+/g, " ").trim();

    if (kind === "flight") {
        if (/^Live flight (?:details offline|tracking is unavailable|details unavailable)\./i.test(clean)) {
            return "Live flight schedules were not returned. Use the route search link below to compare dates and fares.";
        }
        const records = [...content.matchAll(/Flight date:\s*([^\r\n]+)[\s\S]*?Airline:\s*([^\r\n]+)\s+Flight:\s*([^\r\n]+)\s+Status:\s*([^\r\n]+)[\s\S]*?Departure:[\s\S]*?IATA:\s*([^\r\n]+)[\s\S]*?Scheduled local time:\s*([^\r\n]+)[\s\S]*?Arrival:[\s\S]*?IATA:\s*([^\r\n]+)[\s\S]*?Scheduled local time:\s*([^\r\n]+)/gi)];
        if (!records.length) return `${clean} Confirm schedules and ticket prices with the airline before booking.`;
        return records.slice(0, 3).map((m) => `${m[2].trim()} ${m[3].trim()} ? ${m[5].trim()} ? ${m[7].trim()} ? ${m[1].trim()} ? ${m[4].trim()} ? dep ${m[6].trim()} / arr ${m[8].trim()}`).join(" | ");
    }

    if (kind === "hotel") {
        if (/no results found|no current property results|check availability directly/i.test(clean)) {
            return "No live property listings were returned. Use the neighborhood map searches in the Accommodation Recommendations section.";
        }
        const titles = [...content.matchAll(/\[([^\]]+)\]\((https?:\/\/[^)]+)\)/g)]
            .map((match) => match[1].trim())
            .filter(Boolean)
            .slice(0, 3);
        return titles.length
            ? `Property search results: ${titles.join(" · ")}. Open the itinerary links to review the source listings.`
            : "No readable property listings returned. Use the linked neighborhood map searches in the itinerary.";
    }

    if (kind === "weather") {
        return clean.replace("Near-term forecast:", "Forecast:");
    }

    return clean;
}

function showResearchCards(research = {}) {
    const resultBox = document.getElementById("resultBox");
    let panel = document.getElementById("researchCards");

    if (!panel) {
        panel = document.createElement("section");
        panel.id = "researchCards";
        resultBox.insertAdjacentElement("afterend", panel);
    }

    const budgetSource = typeof research.budget_results === "string" ? research.budget_results : "";
    const currencyCodes = budgetSource.match(/\b(BDT|INR|USD|GBP|EUR|AED|JPY|CAD|AUD|SGD|THB|IDR|TRY|CHF|NZD|CNY|KRW|MYR|PHP|LKR|NPR|PKR|ZAR)\b/i);
    const budgetCurrency = (research.currency || (currencyCodes && currencyCodes[1]) || "USD").toUpperCase();
    const tripDays = Math.max(1, Number(research.target_days || research.itinerary_data?.target_days || 1));
    const dailyBudgetRanges = { BDT: [12000, 25000], INR: [8000, 20000], USD: [100, 250], GBP: [90, 220], EUR: [85, 210], AED: [350, 850], JPY: [12000, 30000], CAD: [130, 300], AUD: [140, 320], SGD: [130, 300], THB: [2500, 6500], IDR: [900000, 2300000], TRY: [2500, 6500] };
    const [dailyLow, dailyHigh] = dailyBudgetRanges[budgetCurrency] || [100, 250];
    const flightText = typeof research.flight_results === "string"
        ? research.flight_results.trim()
        : (research.flight_results && research.flight_results.message) || "";
    const weatherText = typeof research.weather_results === "string" ? research.weather_results.trim() : "";
    const budgetText = typeof research.budget_results === "string" ? research.budget_results.trim() : "";
    const displayResearch = {
        ...research,
        flight_results: flightText || "Live flight tracking is unavailable. No schedules or fares were returned for this route.",
        weather_results: weatherText || "Live weather currently unavailable. Pack for seasonal averages.",
        budget_results: budgetText || `Estimated trip budget: ${budgetCurrency} ${(dailyLow * tripDays).toLocaleString()}–${(dailyHigh * tripDays).toLocaleString()} for ${tripDays} days. Planning estimate, not a live quote.`,
    };
    const currencySymbols = { BDT: "\u09f3", INR: "\u20b9", USD: "$", GBP: "\u00a3", EUR: "\u20ac", AED: "AED", JPY: "\u00a5", CAD: "C$", AUD: "A$", SGD: "S$", THB: "\u0e3f", IDR: "Rp", TRY: "\u20ba", CHF: "CHF", NZD: "NZ$", CNY: "\u00a5", KRW: "\u20a9", MYR: "RM", PHP: "\u20b1", LKR: "Rs", NPR: "Rs", PKR: "Rs", ZAR: "R" };
    const cards = [
        ["flight", "✈️", "Flight research", "LIVE STATUS · VERIFY BEFORE BOOKING", displayResearch.flight_results],
        ["hotel", "🏨", "Hotel research", "DISCOVERY SOURCES · CHECK AVAILABILITY", research.hotel_results],
        ["weather", "☀️", "Weather research", "LIVE WEATHER ? CHECKED TIME SHOWN", displayResearch.weather_results],
        ["budget", currencySymbols[budgetCurrency] || budgetCurrency, "Budget research", "PLANNING ESTIMATE", displayResearch.budget_results],
    ].filter(([, , , , content]) => content && content.trim());

    panel.replaceChildren();
    panel.classList.toggle("hidden", cards.length === 0);

    for (const [kind, icon, title, label, content] of cards) {
        let statusLabel = label;
        const cleanContent = content.toLowerCase();
        if (kind === "flight" && (cleanContent.includes("live flight details unavailable") || cleanContent.includes("live flight details offline") || cleanContent.includes("live flight tracking is unavailable"))) statusLabel = "LIVE DATA UNAVAILABLE";
        if (kind === "weather" && cleanContent.includes("live weather currently unavailable")) statusLabel = "SEASONAL GUIDANCE";
        if (kind === "hotel" && (cleanContent.includes("no results found") || cleanContent.includes("no current property results"))) statusLabel = "NEIGHBORHOOD GUIDANCE";
        const card = document.createElement("article");
        card.className = "research-card";
        const heading = document.createElement("h3");
        heading.textContent = `${icon} ${title}`;
        const status = document.createElement("span");
        status.className = "research-status";
        status.textContent = statusLabel;
        const body = document.createElement("p");
        body.textContent = compactResearch(kind, content);
        card.append(heading, status, body);
        if (kind === "flight") {
            const airportCodes = `${research.origin || ""} ${research.destination || ""}`.match(/\b[A-Z]{3}\b/g) || [];
            if (airportCodes.length >= 2) {
                const flightSearch = document.createElement("a");
                flightSearch.href = `https://www.google.com/travel/flights?q=${encodeURIComponent(`Flights from ${airportCodes[0]} to ${airportCodes[1]}`)}`;
                flightSearch.target = "_blank";
                flightSearch.rel = "noopener noreferrer";
                flightSearch.textContent = `Search ${airportCodes[0]} to ${airportCodes[1]} flights`;
                card.append(flightSearch);
            }
        }
        panel.append(card);
    }
}

function destinationHero(query) {
    const destinations = [
        ["dubai", "https://images.unsplash.com/photo-1512453979798-5ea266f8880c?auto=format&fit=crop&w=1600&q=85"],
        ["tokyo", "https://images.unsplash.com/photo-1540959733332-eab4deabeeaf?auto=format&fit=crop&w=1600&q=85"],
        ["japan", "https://images.unsplash.com/photo-1493976040374-85c8e12f0c0e?auto=format&fit=crop&w=1600&q=85"],
        ["paris", "https://images.unsplash.com/photo-1502602898657-3e91760cbb34?auto=format&fit=crop&w=1600&q=85"],
        ["london", "https://images.unsplash.com/photo-1513635269975-59663e0ac1ad?auto=format&fit=crop&w=1600&q=85"],
        ["bali", "https://images.unsplash.com/photo-1537996194471-e657df975ab4?auto=format&fit=crop&w=1600&q=85"],
        ["thailand", "https://images.unsplash.com/photo-1508009603885-50cf7c579365?auto=format&fit=crop&w=1600&q=85"],
        ["bangkok", "https://images.unsplash.com/photo-1508009603885-50cf7c579365?auto=format&fit=crop&w=1600&q=85"],
        ["goa", "https://images.unsplash.com/photo-1507525428034-b723cf961d3e?auto=format&fit=crop&w=1600&q=85"],
        ["jaipur", "https://images.unsplash.com/photo-1477587458883-47145ed94245?auto=format&fit=crop&w=1600&q=85"],
    ];
    const normalized = query.toLowerCase();
    return destinations.find(([name]) => normalized.includes(name))?.[1]
        || "https://images.unsplash.com/photo-1488646953014-85cb44e25828?auto=format&fit=crop&w=1600&q=85";
}

function answerFromStructuredPlan(plan) {
    if (!plan || !Array.isArray(plan.daily_itinerary) || !plan.daily_itinerary.length) return "";
    const days = Number(plan.target_days);
    if (!Number.isInteger(days) || days < 1 || plan.daily_itinerary.length !== days) return "";
    const lines = ["# TripPilot AI: Complete Travel Plan", "", "## Executive Summary", `- **Origin:** ${plan.origin || "Not specified"}`, `- **Destination:** ${plan.destination || "Not specified"}`, `- **Duration:** ${days} days`, "", "## Day-by-Day Detailed Itinerary", ""];
    plan.daily_itinerary.forEach((day, index) => {
        if (Number(day.day) !== index + 1) return;
        lines.push(`### Day ${day.day}: ${day.title || "Itinerary"}`, `- **Morning:** ${day.morning || "Details unavailable"}`, `- **Afternoon:** ${day.afternoon || "Details unavailable"}`, `- **Evening:** ${day.evening || "Details unavailable"}`, `- **Pro-Tip:** ${day.pro_tip || "Verify local details before travel."}`, "");
    });
    return lines.join("\n");
}

function showResult(answer, threadId, requiresApproval = false, approvalRequest = "", tripRequest = "", research = {}) {
    answer = (typeof answer === "string" && answer.trim()) ? answer : answerFromStructuredPlan(research.itinerary_data);
    if (!answer) answer = "The trip plan is temporarily unavailable. Please try again.";
    latestAnswerMarkdown = answer;

    const resultSection = document.getElementById("resultSection");
    const resultBox = document.getElementById("resultBox");
    const threadInfo = document.getElementById("threadInfo");
    const approvalSection = document.getElementById("approvalSection");
    const finalFeedbackSection = document.getElementById("finalFeedbackSection");
    const stageLabel = document.getElementById("planStageLabel");
    const planLegend = document.getElementById("planLegend");
    const resultTitle = document.getElementById("resultTitle");
    const isFlightGuide = /^#{1,2}\s*Flight guide\b/im.test(answer);

    const hasPlanImage = /images\.unsplash\.com/i.test(answer);
    const heroImage = destinationHero(research.destination || research.itinerary_data?.destination || tripRequest || answer);
    const heroMarkup = hasPlanImage
        ? ""
        : `<p><img src="${heroImage}" alt="Travel destination" loading="lazy" style="display:block;width:100%;height:240px;object-fit:cover;border-radius:18px"></p>`;

    if (typeof marked !== "undefined") {
        resultBox.innerHTML = heroMarkup + marked.parse(answer);
    } else {
        resultBox.innerHTML = heroMarkup;
        resultBox.append(document.createTextNode(answer));
    }

    threadInfo.textContent = `Thread ID: ${threadId}`;
    if (stageLabel) {
        stageLabel.textContent = requiresApproval
            ? "TRIP PLAN · REVIEW BEFORE BOOKING"
            : "TRIP PLAN · ESTIMATES — VERIFY BEFORE BOOKING";
    }
    if (planLegend) planLegend.innerHTML = `
        <span class="legend-intro">Plan data labels</span>
        <span class="info-label input-label">Your details</span>
        <span class="legend-detail text-xs text-slate-500">Information you gave TripPilot</span>
        <span class="info-label estimate-label">Planning estimate</span>
        <span class="legend-detail text-xs text-slate-500">Expected cost, time, or route</span>
        <span class="info-label live-label">Verify before booking</span>
        <span class="legend-detail text-xs text-slate-500">Price, availability, visa, and timing</span>
    `;
    if (isFlightGuide) {
        if (stageLabel) stageLabel.textContent = "FLIGHT GUIDE · CHECK BEFORE BOOKING";
        resultTitle.textContent = "Your Flight Guide";
    } else {
        resultTitle.textContent = "Your AI Travel Plan";
    }

    resultSection.classList.remove("hidden");
    showResearchCards(research);
    approvalSection.classList.toggle("hidden", !requiresApproval);
    if (finalFeedbackSection) {
        finalFeedbackSection.classList.toggle("hidden", requiresApproval);
    }

    if (requiresApproval) {
        document.getElementById("approvalRequest").textContent = approvalRequest ||
            "Review the draft itinerary. Approve it to generate the final plan, or request changes.";
    }

    resultBox.querySelectorAll("h2, h3").forEach((heading) => {
        heading.style.borderLeft = "4px solid #0284c7";
        heading.style.paddingLeft = "0.7rem";
        heading.style.letterSpacing = "0.01em";
    });

    resultSection.scrollIntoView({
        behavior: "smooth",
        block: "start"
    });
}

async function submitFinalFeedback() {
    const feedback = document.getElementById("finalFeedback").value.trim();
    const rating = Number(document.getElementById("finalRating").value);
    const status = document.getElementById("finalFeedbackStatus");
    const button = document.getElementById("finalFeedbackBtn");

    if (!feedback) {
        status.textContent = "Please enter your feedback before sending.";
        return;
    }

    button.disabled = true;
    status.textContent = "Sending feedback...";

    try {
        const response = await fetch("/api/feedback", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                thread_id: currentThreadId || "local-plan",
                rating,
                feedback
            })
        });
        const data = await response.json();
        if (!response.ok || !data.success) {
            throw new Error(data.error || "Could not send feedback.");
        }
        status.textContent = data.message;
        document.getElementById("finalFeedback").value = "";
    } catch (error) {
        status.textContent = error.message;
    } finally {
        button.disabled = false;
    }
}

async function sendMessage() {
    hideError();

    const input = document.getElementById("userInput");
    const message = input.value.trim();

    if (!message) {
        showError("Please enter your travel request first.");
        return;
    }

    setLoading(true);

    try {
        const response = await fetch("/api/travel", {
            method: "POST",
            headers: {
                "Content-Type": "application/json"
            },
            body: JSON.stringify({
                message: message,
                thread_id: currentThreadId
            })
        });

        const data = await response.json();

        if (!response.ok || !data.success) {
            throw new Error(data.error || "Something went wrong.");
        }

        if (data.build_id !== CLIENT_BUILD_ID) {
            throw new Error(
                `TripPilot page/backend versions differ (page: ${CLIENT_BUILD_ID}, server: ${data.build_id || "unknown"}). Restart the project server on this same address and refresh.`
            );
        }

        currentThreadId = data.thread_id;
        localStorage.setItem("travel_thread_id", currentThreadId);

        showResult(
            data.answer,
            data.thread_id,
            Boolean(data.requires_approval),
            data.approval_request || "",
            message,
            data
        );

    } catch (error) {
        showError(error.message);
    } finally {
        setLoading(false);
    }
}

async function submitApproval(approved) {
    if (!currentThreadId) {
        showError("Generate a travel plan before submitting a review.");
        return;
    }

    const feedback = document.getElementById("approvalFeedback").value.trim();
    if (!approved && !feedback) {
        showError("Please describe the changes you want before requesting a revision.");
        return;
    }

    const approveBtn = document.getElementById("approveBtn");
    const reviseBtn = document.getElementById("reviseBtn");
    approveBtn.disabled = true;
    reviseBtn.disabled = true;
    hideError();

    try {
        const response = await fetch("/api/travel/approve", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                thread_id: currentThreadId,
                approved,
                feedback
            })
        });
        const data = await response.json();
        if (!response.ok || !data.success) {
            throw new Error(data.error || "Could not update the travel plan.");
        }

        document.getElementById("approvalFeedback").value = "";
        showResult(data.answer, data.thread_id, false, "");
    } catch (error) {
        showError(error.message);
    } finally {
        approveBtn.disabled = false;
        reviseBtn.disabled = false;
    }
}

function copyResult() {
    const resultBox = document.getElementById("resultBox");
    const text = resultBox.innerText;

    if (!text) {
        return;
    }

    navigator.clipboard.writeText(text)
        .then(() => {
            const copyBtn = document.querySelector(".copy-btn");
            const oldText = copyBtn.textContent;

            copyBtn.textContent = "Copied!";

            setTimeout(() => {
                copyBtn.textContent = oldText;
            }, 1400);
        })
        .catch(() => {
            showError("Could not copy result.");
        });
}

function downloadPDF() {
    const pdfContent = document.getElementById("pdfContent");

    if (!latestAnswerMarkdown || !pdfContent) {
        showError("No travel plan available to download.");
        return;
    }

    const downloadBtn = document.querySelector(".download-btn");
    const oldText = downloadBtn.textContent;

    downloadBtn.textContent = "Preparing PDF...";
    downloadBtn.disabled = true;

    const options = {
        margin: 0.5,
        filename: "ai-travel-plan.pdf",
        image: {
            type: "jpeg",
            quality: 0.98
        },
        html2canvas: {
            scale: 2,
            useCORS: true,
            backgroundColor: "#ffffff"
        },
        jsPDF: {
            unit: "in",
            format: "a4",
            orientation: "portrait"
        },
        pagebreak: {
            mode: ["avoid-all", "css", "legacy"]
        }
    };

    html2pdf()
        .set(options)
        .from(pdfContent)
        .save()
        .then(() => {
            downloadBtn.textContent = oldText;
            downloadBtn.disabled = false;
        })
        .catch(() => {
            downloadBtn.textContent = oldText;
            downloadBtn.disabled = false;
            showError("Could not download PDF.");
        });
}

document.addEventListener("keydown", function(event) {
    if (event.ctrlKey && event.key === "Enter") {
        sendMessage();
    }
});

document.addEventListener("DOMContentLoaded", function() {
    const flightChip = Array.from(document.querySelectorAll(".prompt-chip"))
        .find((chip) => chip.textContent.trim() === "Flight explorer");

    if (flightChip) {
        flightChip.textContent = "Flight route guide";
        flightChip.onclick = () => setPrompt(
            "Give me a flight guide from Delhi to Dubai: airports, route options, " +
            "typical journey time, fare estimate, and booking checks."
        );
    }
});
