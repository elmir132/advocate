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

  window.landlordLines = landlordLines; // export for UI

  class MockWebSocket {
    constructor(url) {
      this.url = url;
      this.readyState = 1;
      this.onmessage = null;
      this.onopen = null;
      this.onclose = null;
      this.onerror = null;

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
          this._sendToClient({ type: "status", state: "listening" });
        } else if (msg.type === "audio") {
          // Mock tenant speaking
          this._sendToClient({ type: "status", state: "thinking" });
          setTimeout(() => {
            this._sendToClient({ type: "transcript", speaker: "tenant", lang: "uk", text: "Що щодо депозиту?" });
            this._sendToClient({ type: "translation", to: "landlord", text: "What about the deposit?" });
            this._sendToClient({ type: "status", state: "listening" });
          }, 1000);
        } else if (msg.type === "landlord_text") {
          this._sendToClient({ type: "status", state: "thinking" });
          setTimeout(() => {
            const line = landlordLines.find(l => l.id === msg.id) || { text: msg.text, expected_flags: [], promise: false };
            this._sendToClient({ type: "transcript", speaker: "landlord", lang: "en", text: line.text });
            const mockTranslations = {
              L01: "Застава становить орендну плату за два місяці й не повертається, якщо ви виїдете до закінчення терміну оренди.",
              L03: "Ми, ймовірно, зможемо полагодити обігрівач десь наступного тижня. Я подивлюся, що можна зробити.",
              L04: "Я обіцяю перефарбувати всю квартиру до того, як ви в’їдете першого числа.",
              L07: "Суборенда повністю заборонена, а порушення цього правила означає втрату застави."
            };
            this._sendToClient({
              type: "translation",
              to: "tenant",
              text: mockTranslations[line.id] || "[Mock mode: translation is not available for this sample.]"
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
              languages: ["uk", "en"],
              markdown: "### Summary\\n\\n**Promises**:\\n- Repaint the apartment\\n- Email the signed lease\\n- Cover the window cost\\n\\n**Warnings**:\\n- Deposit is non-refundable\\n- Rent goes up 5%\\n"
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
