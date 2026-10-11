// mock.js
(function() {
  const landlordLines = [
    { "id": "L01", "text": "The deposit is two months' rent, and it is non-refundable if you leave before the lease ends.", "expected_flags": ["financial_term", "penalty"], "promise": false },
    { "id": "L02", "text": "If rent is more than three days late, there is a hundred dollar fee for every day after that.", "expected_flags": ["financial_term", "penalty"], "promise": false },
    { "id": "L03", "text": "We can probably fix the heater sometime next week, I'll see what I can do.", "expected_flags": ["vague_commitment"], "promise": false },
    { "id": "L04", "text": "I promise to repaint the whole apartment before you move in on the first.", "expected_flags": ["promise"], "promise": true },
    { "id": "L05", "text": "Utilities are included, mostly. Electricity is a little extra in summer.", "expected_flags": ["vague_commitment", "financial_term"], "promise": false },
    { "id": "L06", "text": "The apartment is on the third floor and faces the courtyard.", "expected_flags": [], "promise": false },
    { "id": "L07", "text": "Subletting is not allowed at all, and breaking that rule means you lose the deposit.", "expected_flags": ["restriction", "penalty"], "promise": false },
    { "id": "L08", "text": "We'll see about the broker fee, maybe we can work something out.", "expected_flags": ["vague_commitment", "financial_term"], "promise": false },
    { "id": "L09", "text": "I will send you the signed lease by email by Friday evening.", "expected_flags": ["promise"], "promise": true },
    { "id": "L10", "text": "The building has a laundry room in the basement.", "expected_flags": [], "promise": false },
    { "id": "L11", "text": "You must give ninety days written notice if you do not want to renew, or it renews automatically for a year.", "expected_flags": ["restriction", "financial_term"], "promise": false },
    { "id": "L12", "text": "I'll try to get back to you about the parking spot, no guarantees.", "expected_flags": ["vague_commitment"], "promise": false },
    { "id": "L13", "text": "Rent goes up five percent every year, starting from the second year.", "expected_flags": ["financial_term"], "promise": false },
    { "id": "L14", "text": "I will keep the security deposit in a separate account and give you the bank's name.", "expected_flags": ["promise", "financial_term"], "promise": true },
    { "id": "L15", "text": "The super lives in the building and is usually around in the mornings.", "expected_flags": [], "promise": false },
    { "id": "L16", "text": "No pets, and no overnight guests for more than a week.", "expected_flags": ["restriction"], "promise": false },
    { "id": "L17", "text": "Let me think about the late fee, maybe it can be lower, we can talk later.", "expected_flags": ["vague_commitment", "financial_term"], "promise": false },
    { "id": "L18", "text": "The lease starts on the first of next month.", "expected_flags": [], "promise": false },
    { "id": "L19", "text": "I'll cover the cost of fixing the window myself, you do not need to pay for it.", "expected_flags": ["promise", "financial_term"], "promise": true },
    { "id": "L20", "text": "If you break the lease early you owe the rest of the rent for the full term.", "expected_flags": ["financial_term", "penalty"], "promise": false }
  ];

  const landlordTranslations = {
    uk: {
      L01: "Застава становить орендну плату за два місяці й не повертається, якщо ви виїдете до закінчення терміну оренди.",
      L03: "Ми, ймовірно, зможемо полагодити обігрівач десь наступного тижня. Я подивлюся, що можна зробити.",
      L04: "Я обіцяю перефарбувати всю квартиру до того, як ви в’їдете першого числа.",
      L07: "Суборенда повністю заборонена, а порушення цього правила означає втрату застави."
    },
    ru: {
      L01: "Залог составляет арендную плату за два месяца и не возвращается, если вы съедете до окончания срока аренды.",
      L03: "Мы, вероятно, сможем починить обогреватель на следующей неделе. Я посмотрю, что можно сделать.",
      L04: "Я обещаю перекрасить всю квартиру до того, как вы въедете первого числа.",
      L07: "Субаренда полностью запрещена, и нарушение этого правила означает потерю залога."
    },
    es: {
      L01: "El depósito equivale a dos meses de alquiler y no es reembolsable si se marcha antes de que termine el contrato.",
      L03: "Probablemente podamos arreglar la calefacción la próxima semana. Veré qué puedo hacer.",
      L04: "Prometo volver a pintar todo el apartamento antes de que se mude el día primero.",
      L07: "El subarriendo no está permitido, y si incumple esa regla perderá el depósito."
    },
    zh: {
      L01: "押金相当于两个月的租金；如果您在租约到期前搬走，押金将不予退还。",
      L03: "我们也许能在下周某个时候修好暖气。我会看看能做些什么。",
      L04: "我保证在您一号搬进来之前重新粉刷整个公寓。",
      L07: "完全不允许转租；违反这项规定将意味着失去押金。"
    }
  };
  const tenantExamples = {
    uk: ["Що щодо депозиту?", "What about the deposit?"],
    ru: ["Что насчёт залога?", "What about the deposit?"],
    es: ["¿Qué pasa con el depósito?", "What about the deposit?"],
    zh: ["押金怎么办？", "What about the deposit?"],
    en: ["What about the deposit?", "What about the deposit?"]
  };
  function mockAutoSpeaker(text, language) {
    if (language === "en") return null;
    if ((language === "uk" || language === "ru") && /[\u0400-\u04ff]/.test(text)) return "tenant";
    if (language === "zh" && /[\u3400-\u9fff]/.test(text)) return "tenant";
    if (language === "es" && (/[¿¡áéíóúüñ]/i.test(text) || /\b(el|la|qué|depósito|alquiler|calefacción)\b/i.test(text))) return "tenant";
    const english = text.toLowerCase().match(/\b(the|this|that|what|when|we|you|your|can|could|will|probably|maybe|rent|deposit|heater|lease|fix|repair)\b/g) || [];
    return english.length >= 2 ? "landlord" : null;
  }

  const summaryHeadings = {
    uk: ["Коротко про розмову", "Обіцянки", "Не є обіцянкою", "Фінансові умови, штрафи та правила"],
    ru: ["Кратко о разговоре", "Обещания", "Не является обязательством", "Финансовые условия, штрафы и правила"],
    es: ["Resumen de la conversación", "Promesas", "No es un compromiso", "Condiciones económicas, penalizaciones y reglas"],
    zh: ["对话概述", "承诺", "并非明确承诺", "费用、处罚和规则"],
    en: ["Conversation overview", "Promises", "Not a commitment", "Money terms, penalties and rules"]
  };
  const summaryOverviews = {
    uk: "Розмова стосується ремонту обігрівача, обіцянки перефарбувати квартиру та умов застави.",
    ru: "Разговор касается ремонта обогревателя, обещания перекрасить квартиру и условий залога.",
    es: "La conversación trata sobre la reparación de la calefacción, la promesa de volver a pintar el apartamento y las condiciones del depósito.",
    zh: "对话涉及暖气维修、重新粉刷公寓的承诺以及押金条件。",
    en: "The conversation covers a heater repair, a promise to repaint the apartment, and deposit terms."
  };
  const englishLines = Object.fromEntries(["L01", "L03", "L04"].map((id) => [id, landlordLines.find((line) => line.id === id).text]));
  function translatedLine(language, id) {
    return language === "en" ? englishLines[id] : (landlordTranslations[language] || landlordTranslations.uk)[id];
  }
  function mockSummary(language) {
    const headings = summaryHeadings[language] || summaryHeadings.uk;
    const section = `# ${headings[0]}
- ${summaryOverviews[language] || summaryOverviews.uk}

# ${headings[1]}
- ${translatedLine(language, "L04")}

# ${headings[2]}
- ${translatedLine(language, "L03")}

# ${headings[3]}
- ${translatedLine(language, "L01")}`;
    if (language === "en") return section;
    const english = summaryHeadings.en;
    return `${section}

# ${english[0]}
- ${summaryOverviews.en}

# ${english[1]}
- ${englishLines.L04}

# ${english[2]}
- ${englishLines.L03}

# ${english[3]}
- ${englishLines.L01}`;
  }

  window.landlordLines = landlordLines; // export for UI

  class MockWebSocket {
    constructor(url) {
      this.url = url;
      this.readyState = 1;
      this.onmessage = null;
      this.onopen = null;
      this.onclose = null;
      this.onerror = null;
      this._hasBufferedAudio = false;
      this.tenantLang = "uk";

      setTimeout(() => {
        if (this.onopen) this.onopen(new Event("open"));
        // Simulate immediate interaction for screenshot purposes
        setTimeout(() => {
          this.send(JSON.stringify({type: "audio"}));
        }, 500);
        setTimeout(() => {
          this.send(JSON.stringify({type: "landlord_text", id: "L03", text: "We can probably fix the heater sometime next week, I'll see what I can do."}));
        }, 2000);
        setTimeout(() => {
          this.send(JSON.stringify({type: "landlord_text", id: "L04", text: "I promise to repaint the whole apartment before you move in on the first."}));
        }, 3500);
      }, 100);
    }

    send(data) {
      try {
        const msg = JSON.parse(data);
        if (msg.type === "start") {
          this.tenantLang = msg.tenant_lang || "uk";
          this._sendToClient({ type: "status", state: "listening" });
        } else if (msg.type === "auto_text") {
          const speaker = mockAutoSpeaker(msg.text, this.tenantLang);
          if (!speaker) {
            this._sendToClient({ type: "error", message: "I could not tell who typed that. Choose Me or Landlord and send it again." });
          } else {
            this._sendToClient({ type: "speaker", speaker, detected: true, lang: speaker === "landlord" ? "en" : this.tenantLang });
            if (speaker === "landlord") {
              this.send(JSON.stringify({ type: "landlord_text", id: "AUTO", text: msg.text }));
            } else {
              const example = tenantExamples[this.tenantLang] || tenantExamples.uk;
              this._sendToClient({ type: "transcript", speaker: "tenant", lang: this.tenantLang, text: msg.text });
              this._sendToClient({ type: "translation", to: "landlord", text: msg.text === example[0] ? example[1] : "[Mock English translation]" });
            }
          }
        } else if (msg.type === "audio") {
          if (!msg.data) {
            // Built-in screenshot example.
            this._sendToClient({ type: "status", state: "thinking" });
            setTimeout(() => {
              const example = tenantExamples[this.tenantLang] || tenantExamples.uk;
              this._sendToClient({ type: "transcript", speaker: "tenant", lang: this.tenantLang, text: example[0] });
              this._sendToClient({ type: "translation", to: "landlord", text: example[1] });
              this._sendToClient({ type: "status", state: "listening" });
            }, 1000);
          } else {
            this._hasBufferedAudio = true;
            this._sendToClient({ type: "status", state: "listening" });
          }
        } else if (msg.type === "audio_end" && this._hasBufferedAudio) {
          this._hasBufferedAudio = false;
          const landlord = msg.speaker === "landlord" || (msg.speaker === "auto" && msg.source === "upload");
          if (landlord) {
            this.send(JSON.stringify({ type: "landlord_text", id: "UPLOAD", text: "We can probably fix the heater sometime next week, I'll see what I can do." }));
          } else {
            this._sendToClient({ type: "status", state: "thinking" });
            setTimeout(() => {
              const example = tenantExamples[this.tenantLang] || tenantExamples.uk;
              this._sendToClient({ type: "transcript", speaker: "tenant", lang: this.tenantLang, text: example[0] });
              this._sendToClient({ type: "translation", to: "landlord", text: example[1] });
              this._sendToClient({ type: "status", state: "listening" });
            }, 500);
          }
        } else if (msg.type === "landlord_text") {
          this._sendToClient({ type: "status", state: "thinking" });
          setTimeout(() => {
            const line = landlordLines.find(l => l.id === msg.id || l.text === msg.text) || { id: msg.id, text: msg.text, expected_flags: [], promise: false };
            this._sendToClient({ type: "transcript", speaker: "landlord", lang: "en", text: line.text });
            this._sendToClient({
              type: "translation",
              to: "tenant",
              text: translatedLine(this.tenantLang, line.id) || "[Mock mode: translation is not available for this sample.]"
            });
            
            const noteMap = {
              "vague_commitment": "ask for it in writing",
              "financial_term": "money term",
              "penalty": "penalty",
              "promise": "promise",
              "restriction": "rule"
            };
            
            // Mock analysis
            this._sendToClient({
              type: "analysis",
              id: msg.id || "manual",
              flags: line.expected_flags || [],
              promise: line.promise || false,
              note: line.expected_flags?.includes("vague_commitment") ? "ask for it in writing" : "Зауважте це."
            });
            this._sendToClient({ type: "status", state: "listening" });
          }, 800);
        } else if (msg.type === "approve_summary") {
          setTimeout(() => {
            this._sendToClient({
              type: "summary",
              languages: [this.tenantLang, "en"],
              markdown: mockSummary(this.tenantLang)
            });
          }, 1000);
        }
      } catch (e) {
        console.error(e);
      }
    }

    close() {
      this.readyState = 3;
      if (this.onclose) this.onclose(new Event("close"));
    }

    _sendToClient(obj) {
      if (this.onmessage) {
        this.onmessage({ data: JSON.stringify(obj) });
      }
    }
  }

  MockWebSocket.CONNECTING = 0;
  MockWebSocket.OPEN = 1;
  MockWebSocket.CLOSING = 2;
  MockWebSocket.CLOSED = 3;

  window.OriginalWebSocket = window.WebSocket;
  window.WebSocket = MockWebSocket;

  window.OriginalFetch = window.fetch;
  window.fetch = async function(url, options) {
    if (url.includes("/health")) {
      return { json: async () => ({ ok: true, live: true }) };
    }
    if (url.includes("/landlord/say")) {
      const body = JSON.parse(options.body);
      // simulate websocket message injection
      const wss = document.querySelector("#app")._ws;
      if (wss) {
        wss.send(JSON.stringify({ type: "landlord_text", id: body.id, text: body.text }));
      }
      return { json: async () => ({ ok: true }) };
    }
    return window.OriginalFetch(url, options);
  };
})();
