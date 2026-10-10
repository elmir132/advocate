document.addEventListener("DOMContentLoaded", () => {
  const isServedByBackend = window.location.port === "8000";
  const API_BASE = isServedByBackend ? "" : "http://127.0.0.1:8000";
  const WS_BASE = isServedByBackend 
    ? (window.location.protocol === "https:" ? "wss://" : "ws://") + window.location.host + "/ws"
    : "ws://127.0.0.1:8000/ws";

  let ws = null;
  let hasFlagsOrPromises = false;

  const elements = {
    overlay: document.getElementById("connection-overlay"),
    retryBtn: document.getElementById("retry-btn"),
    statusIndicator: document.getElementById("status-indicator"),
    waveform: document.getElementById("waveform"),
    conversation: document.getElementById("conversation"),
    promisesUl: document.getElementById("promises-ul"),
    flagsContainer: document.getElementById("flags-container"),
    micBtn: document.getElementById("mic-btn"),
    tenantLang: document.getElementById("tenant-lang"),
    approveBtn: document.getElementById("approve-btn"),
    simulatorLines: document.getElementById("simulator-lines"),
    tabAdvocate: document.getElementById("tab-advocate"),
    tabScore: document.getElementById("tab-score"),
    viewAdvocate: document.getElementById("advocate-view"),
    viewScore: document.getElementById("score-view"),
    scoreOutput: document.getElementById("score-output"),
    summaryDialog: document.getElementById("summary-dialog"),
    summaryContent: document.getElementById("summary-content"),
    closeSummary: document.getElementById("close-summary")
  };

  let lastTenantBubble = null;
  let lastLandlordBubble = null;

  function connect() {
    ws = new WebSocket(WS_BASE);
    // Expose for mock.js
    document.getElementById("app")._ws = ws;

    ws.onopen = () => {
      elements.overlay.classList.add("hidden");
      ws.send(JSON.stringify({ type: "start", tenant_lang: elements.tenantLang.value }));
    };

    ws.onclose = () => {
      elements.overlay.classList.remove("hidden");
    };

    ws.onerror = () => {
      // Handled by onclose
    };

    ws.onmessage = (event) => {
      const msg = JSON.parse(event.data);
      handleMessage(msg);
    };
  }

  elements.retryBtn.addEventListener("click", connect);

  function handleMessage(msg) {
    switch (msg.type) {
      case "status":
        elements.statusIndicator.textContent = msg.state.charAt(0).toUpperCase() + msg.state.slice(1);
        if (msg.state === "listening" || msg.state === "thinking" || msg.state === "speaking") {
          elements.waveform.classList.add("active");
        } else {
          elements.waveform.classList.remove("active");
        }
        break;

      case "transcript":
        appendTranscript(msg.speaker, msg.text, msg.lang);
        break;

      case "translation":
        appendTranslation(msg.to, msg.text);
        break;

      case "analysis":
        handleAnalysis(msg);
        break;

      case "audio":
        playAudio(msg.data);
        break;

      case "summary":
        showSummary(msg.markdown);
        break;
        
      case "error":
        console.error("Server Error:", msg.message);
        break;
    }
  }

  // Audio Playback
  const muteToggle = document.getElementById("mute-toggle");
  let outAudioCtx = null;
  let nextStartTime = 0;

  function base64ToFloat32(base64) {
    const binary = atob(base64);
    const len = binary.length;
    const bytes = new Uint8Array(len);
    for (let i = 0; i < len; i++) {
      bytes[i] = binary.charCodeAt(i);
    }
    const int16Array = new Int16Array(bytes.buffer);
    const float32Array = new Float32Array(int16Array.length);
    for (let i = 0; i < int16Array.length; i++) {
      float32Array[i] = int16Array[i] / (int16Array[i] < 0 ? 0x8000 : 0x7FFF);
    }
    return float32Array;
  }

  function playAudio(base64Data) {
    if (muteToggle.checked) return;

    if (!outAudioCtx) {
      outAudioCtx = new (window.AudioContext || window.webkitAudioContext)({ sampleRate: 24000 });
    }
    if (outAudioCtx.state === 'suspended') {
      outAudioCtx.resume();
    }

    const float32Data = base64ToFloat32(base64Data);
    const audioBuffer = outAudioCtx.createBuffer(1, float32Data.length, 24000);
    audioBuffer.getChannelData(0).set(float32Data);

    const source = outAudioCtx.createBufferSource();
    source.buffer = audioBuffer;
    source.connect(outAudioCtx.destination);

    const currentTime = outAudioCtx.currentTime;
    if (nextStartTime < currentTime) {
      nextStartTime = currentTime + 0.05; // 50ms buffer to prevent initial click
    }
    source.start(nextStartTime);
    nextStartTime += audioBuffer.duration;
  }

  function appendTranscript(speaker, text, lang) {
    const emptyState = document.getElementById("empty-state");
    if (emptyState) {
      emptyState.style.display = "none";
    }

    const bubble = document.createElement("div");
    bubble.className = `message ${speaker}`;
    
    const speakerLabel = document.createElement("div");
    speakerLabel.className = "speaker";
    speakerLabel.textContent = speaker;
    
    const original = document.createElement("div");
    original.className = "original";
    original.textContent = text;
    original.lang = lang;

    const translation = document.createElement("div");
    translation.className = "translation";
    // Translation will be populated later

    bubble.appendChild(speakerLabel);
    bubble.appendChild(original);
    bubble.appendChild(translation);

    elements.conversation.appendChild(bubble);
    elements.conversation.scrollTop = elements.conversation.scrollHeight;

    if (speaker === "tenant") {
      lastTenantBubble = bubble;
    } else {
      lastLandlordBubble = bubble;
      // Prepare a container for flags within the landlord bubble
      const inlineFlags = document.createElement("div");
      inlineFlags.className = "inline-flags";
      inlineFlags.style.marginTop = "0.5rem";
      bubble.appendChild(inlineFlags);
    }
  }

  function appendTranslation(to, text) {
    // If 'to' is landlord, it translates the tenant's speech
    const targetBubble = to === "landlord" ? lastTenantBubble : lastLandlordBubble;
    if (targetBubble) {
      const transEl = targetBubble.querySelector(".translation");
      if (transEl) {
        transEl.textContent = text;
      }
    }
  }

  function handleAnalysis(msg) {
    if (msg.flags.length > 0 || msg.promise) {
      hasFlagsOrPromises = true;
      elements.approveBtn.disabled = false;
    }

    const flagNames = {
      "financial_term": "money term",
      "penalty": "penalty",
      "vague_commitment": "soft no",
      "promise": "promise",
      "restriction": "rule"
    };

    // Add to latest landlord bubble if exists
    let bubbleFlags = null;
    if (lastLandlordBubble) {
      bubbleFlags = lastLandlordBubble.querySelector(".inline-flags");
    }

    if (msg.promise) {
      const li = document.createElement("li");
      // Find the text from the last bubble or use ID?
      const text = lastLandlordBubble ? lastLandlordBubble.querySelector(".original").textContent : msg.id;
      li.textContent = text;
      elements.promisesUl.appendChild(li);
    }

    msg.flags.forEach(flag => {
      const plainText = flagNames[flag] || flag;
      
      // Global flag list
      const chip = document.createElement("span");
      chip.className = "flag-chip";
      chip.textContent = plainText;
      elements.flagsContainer.prepend(chip);

      // Inline bubble chip
      if (bubbleFlags) {
        const bChip = document.createElement("span");
        bChip.className = "flag-chip";
        bChip.textContent = plainText;
        bubbleFlags.appendChild(bChip);
      }

      if (flag === "vague_commitment") {
        const note = document.createElement("div");
        note.className = "soft-no-note";
        note.textContent = "↳ " + (msg.note || "ask for it in writing");
        if (bubbleFlags) {
          bubbleFlags.appendChild(note);
        }
      }
    });
  }

  function renderMarkdown(md) {
    if (md.includes("|")) {
      const rows = md.trim().split('\n').filter(r => r.includes('|'));
      if (rows.length > 2) {
        let html = '<table style="width:100%; border-collapse: collapse; text-align: left;">';
        rows.forEach((row, i) => {
          if (row.includes('---')) return;
          const cells = row.split('|').slice(1, -1);
          html += '<tr>';
          cells.forEach(c => {
            const tag = i === 0 ? 'th' : 'td';
            html += `<${tag} style="border: 1px solid var(--border-color); padding: 8px;">${c.trim()}</${tag}>`;
          });
          html += '</tr>';
        });
        html += '</table>';
        // if the entire thing was a table, return. Otherwise, we might have mixed content. 
        // For simplicity, just return table if it looks like one.
        if (rows.length === md.trim().split('\n').filter(r=>r.trim()).length) return html;
      }
    }
    
    // Split by languages if they use ## headings
    const parts = md.split(/(?=## )/);
    if (parts.length === 3) {
      // Possibly a header and two language sections. Put them in two columns.
      let html = '<div style="display: flex; gap: 2rem;">';
      parts.forEach(p => {
        if (!p.trim()) return;
        html += `<div style="flex: 1;">${p.replace(/\n/g, '<br>').replace(/\*\*(.*?)\*\*/g, '<strong>$1</strong>')}</div>`;
      });
      html += '</div>';
      return html;
    }

    return md.replace(/\n/g, '<br>').replace(/\*\*(.*?)\*\*/g, '<strong>$1</strong>');
  }

  function showSummary(markdown) {
    elements.summaryContent.innerHTML = renderMarkdown(markdown);
    
    // Add copy button if not exists
    if (!document.getElementById("copy-summary-btn")) {
      const copyBtn = document.createElement("button");
      copyBtn.id = "copy-summary-btn";
      copyBtn.textContent = "📋 Copy Summary";
      copyBtn.style.marginTop = "1rem";
      copyBtn.style.marginRight = "1rem";
      copyBtn.onclick = () => {
        navigator.clipboard.writeText(markdown).then(() => {
          copyBtn.textContent = "✅ Copied!";
          setTimeout(() => copyBtn.textContent = "📋 Copy Summary", 2000);
        });
      };
      elements.summaryDialog.querySelector(".dialog-content").insertBefore(copyBtn, elements.closeSummary);
    }
    
    elements.summaryDialog.showModal();
  }

  elements.closeSummary.addEventListener("click", () => {
    elements.summaryDialog.close();
  });

  elements.approveBtn.addEventListener("click", () => {
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify({ type: "approve_summary" }));
    }
  });

  elements.tenantLang.addEventListener("change", () => {
    if (ws && ws.readyState === WebSocket.OPEN) {
       ws.send(JSON.stringify({ type: "start", tenant_lang: elements.tenantLang.value }));
    }
  });

  let audioContext;
  let mediaStream;
  let scriptProcessor;
  let sourceNode;

  function base64EncodeAudio(float32Array) {
    const pcm16 = new Int16Array(float32Array.length);
    for (let i = 0; i < float32Array.length; i++) {
      let s = Math.max(-1, Math.min(1, float32Array[i]));
      pcm16[i] = s < 0 ? s * 0x8000 : s * 0x7FFF;
    }
    const buffer = new ArrayBuffer(pcm16.length * 2);
    const view = new DataView(buffer);
    for (let i = 0; i < pcm16.length; i++) {
      view.setInt16(i * 2, pcm16[i], true); // little-endian
    }
    let binary = '';
    const bytes = new Uint8Array(buffer);
    for (let i = 0; i < bytes.byteLength; i++) {
      binary += String.fromCharCode(bytes[i]);
    }
    return btoa(binary);
  }

  async function startMic() {
    if (!ws || ws.readyState !== WebSocket.OPEN) return;
    if (elements.micBtn.classList.contains("active")) return;
    
    try {
      mediaStream = await navigator.mediaDevices.getUserMedia({ audio: true });
      elements.micBtn.classList.add("active");
      
      // Need 16kHz audio context
      audioContext = new (window.AudioContext || window.webkitAudioContext)({ sampleRate: 16000 });
      sourceNode = audioContext.createMediaStreamSource(mediaStream);
      
      // using script processor for simplicity (100ms chunks ~ 2048 at 16khz = 128ms)
      scriptProcessor = audioContext.createScriptProcessor(2048, 1, 1);
      
      scriptProcessor.onaudioprocess = (e) => {
        const inputData = e.inputBuffer.getChannelData(0);
        const b64 = base64EncodeAudio(inputData);
        if (ws && ws.readyState === WebSocket.OPEN) {
          ws.send(JSON.stringify({ type: "audio", data: b64 }));
        }
      };

      sourceNode.connect(scriptProcessor);
      scriptProcessor.connect(audioContext.destination);

    } catch (err) {
      console.error("Microphone access denied or error:", err);
      alert("Microphone access denied. Please allow microphone access to use voice.");
    }
  }

  function stopMic() {
    if (!elements.micBtn.classList.contains("active")) return;
    elements.micBtn.classList.remove("active");
    
    if (scriptProcessor) {
      scriptProcessor.disconnect();
      scriptProcessor = null;
    }
    if (sourceNode) {
      sourceNode.disconnect();
      sourceNode = null;
    }
    if (audioContext) {
      audioContext.close();
      audioContext = null;
    }
    if (mediaStream) {
      mediaStream.getTracks().forEach(track => track.stop());
      mediaStream = null;
    }
  }

  elements.micBtn.addEventListener("mousedown", startMic);
  elements.micBtn.addEventListener("mouseup", stopMic);
  elements.micBtn.addEventListener("mouseleave", stopMic);

  document.addEventListener("keydown", (e) => {
    // Only capture Space for mic if not in an input
    if (e.code === "Space" && !e.repeat && e.target.tagName !== "INPUT" && e.target.tagName !== "SELECT" && e.target.tagName !== "TEXTAREA") {
      startMic();
    }
  });

  const tenantTextInput = document.getElementById("tenant-text-input");
  tenantTextInput.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && tenantTextInput.value.trim() !== "") {
      if (ws && ws.readyState === WebSocket.OPEN) {
        ws.send(JSON.stringify({ type: "tenant_text", text: tenantTextInput.value.trim() }));
        tenantTextInput.value = "";
      }
    }
  });

  document.addEventListener("keyup", (e) => {
    if (e.code === "Space") {
      stopMic();
    }
  });

  // Load simulator lines
  async function loadSimulator() {
    let lines = [];
    const _fetch = window.OriginalFetch || window.fetch;
    try {
      const res = await _fetch(API_BASE + "/data/landlord_lines.json");
      if (res.ok) lines = await res.json();
      else throw new Error("Fetch failed");
    } catch (e) {
      // Fallback to mock
      lines = window.landlordLines || [];
    }

    lines.forEach(line => {
      const div = document.createElement("div");
      div.className = "simulator-item";
      
      const p = document.createElement("p");
      p.textContent = line.text;
      
      const btn = document.createElement("button");
      btn.textContent = "▶ Play";
      btn.onclick = () => {
        const _fetch2 = window.OriginalFetch || window.fetch;
        _fetch2(API_BASE + "/landlord/say", {
          method: "POST",
          body: JSON.stringify({ id: line.id, text: line.text })
        });
      };

      div.appendChild(p);
      div.appendChild(btn);
      elements.simulatorLines.appendChild(div);
    });
  }

  // Tabs
  elements.tabAdvocate.addEventListener("click", () => {
    elements.tabAdvocate.classList.add("active");
    elements.tabScore.classList.remove("active");
    elements.viewAdvocate.classList.remove("hidden");
    elements.viewScore.classList.add("hidden");
    elements.tabAdvocate.setAttribute("aria-selected", "true");
    elements.tabScore.setAttribute("aria-selected", "false");
  });

  elements.tabScore.addEventListener("click", async () => {
    elements.tabScore.classList.add("active");
    elements.tabAdvocate.classList.remove("active");
    elements.viewScore.classList.remove("hidden");
    elements.viewAdvocate.classList.add("hidden");
    elements.tabScore.setAttribute("aria-selected", "true");
    elements.tabAdvocate.setAttribute("aria-selected", "false");

    const _fetch = window.OriginalFetch || window.fetch;
    try {
      const [resEval, resLines] = await Promise.all([
        _fetch(API_BASE + "/eval/results.json").catch(() => null),
        _fetch(API_BASE + "/data/landlord_lines.json").catch(() => null)
      ]);
      
      if (resEval && resEval.ok) {
        const results = await resEval.json();
        const truth = (resLines && resLines.ok) ? await resLines.json() : window.landlordLines;

        const CATS = ["financial_term", "penalty", "vague_commitment", "promise", "restriction"];
        let got = {};
        results.forEach(r => got[r.id] = r);

        let tp = {}, fp = {}, fn = {};
        CATS.forEach(c => { tp[c] = 0; fp[c] = 0; fn[c] = 0; });
        let promise_ok = 0;

        truth.forEach(t => {
          let r = got[t.id] || { flags: [], promise: false };
          let exp = new Set(t.expected_flags || []);
          let pred = new Set((r.flags || []).filter(f => CATS.includes(f)));

          CATS.forEach(c => {
            if (exp.has(c) && pred.has(c)) tp[c]++;
            else if (pred.has(c)) fp[c]++;
            else if (exp.has(c)) fn[c]++;
          });
          if (Boolean(r.promise) === Boolean(t.promise)) promise_ok++;
        });

        const pr = (a, b) => (a + b) ? Number((a / (a + b)).toFixed(3)) : null;
        
        let sum_tp = Object.values(tp).reduce((a,b)=>a+b,0);
        let sum_fp = Object.values(fp).reduce((a,b)=>a+b,0);
        let sum_fn = Object.values(fn).reduce((a,b)=>a+b,0);

        let per = {};
        CATS.forEach(c => {
          per[c] = { precision: pr(tp[c], fp[c]), recall: pr(tp[c], fn[c]) };
        });

        const scoreObj = {
          lines: truth.length,
          micro_precision: pr(sum_tp, sum_fp),
          micro_recall: pr(sum_tp, sum_fn),
          promise_accuracy: Number((promise_ok / truth.length).toFixed(3)),
          per_flag: per
        };

        elements.scoreOutput.textContent = JSON.stringify(scoreObj, null, 2);
      } else {
        elements.scoreOutput.textContent = "No eval results found (eval/results.json).";
      }
    } catch (e) {
      elements.scoreOutput.textContent = "Failed to load eval results: " + e.message;
    }
  });

  // Init
  loadSimulator();
  connect();
});
