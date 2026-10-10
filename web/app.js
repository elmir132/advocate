document.addEventListener("DOMContentLoaded", () => {
  // Same origin whenever the page comes from the server (local port 8000 or a public https link).
  // Only a file:// page or a separate dev server falls back to the local backend address.
  const devPorts = ["3000", "5173", "5500", "8080"];
  const isServedByBackend = window.location.protocol.startsWith("http") && !devPorts.includes(window.location.port);
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
    viewSimulator: document.getElementById("simulator-view"),
    scoreOutput: document.getElementById("score-output"),
    summaryDialog: document.getElementById("summary-dialog"),
    summaryContent: document.getElementById("summary-content"),
    closeSummary: document.getElementById("close-summary"),
    tabSimulator: document.getElementById("tab-simulator"),
  };

  let lastTenantBubble = null;
  let lastLandlordBubble = null;
  let activePlayBtn = null;

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
      case "status": {
        // "listening" from the server only means the session is open, so show "Ready"
        // unless the tenant is actually holding the button.
        const labels = { idle: "Ready", listening: "Ready", thinking: "Working…", speaking: "Speaking" };
        const held = elements.micBtn.classList.contains("active");
        elements.statusIndicator.textContent = held ? "Recording" : (labels[msg.state] || "Ready");
        document.body.dataset.state = held ? "recording" : msg.state;
        elements.waveform.classList.toggle("active", held || msg.state === "thinking" || msg.state === "speaking");
        break;
      }

      case "transcript":
        appendTranscript(msg.speaker, msg.text, msg.lang);
        if (window.advocateShowHint) window.advocateShowHint("", "");
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
        
      case "speaker":
        if (window.advocateShowHint) {
          if (msg.note) window.advocateShowHint(msg.note, "error");
          else window.advocateShowHint(msg.speaker === "landlord" ? "Heard the landlord. Checking what was said…" : "Heard you. Translating…", "live");
        }
        break;

      case "error":
        console.error("Server Error:", msg.message);
        if (window.advocateShowHint) window.advocateShowHint(msg.message, "error");
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

    if (speaker === "landlord" && activePlayBtn) {
      activePlayBtn.textContent = "▶ Play";
      activePlayBtn.disabled = false;
      activePlayBtn = null;
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
        // Appending instead of replacing for streamed translations
        if (transEl.textContent) {
          transEl.textContent += " " + text.trim();
        } else {
          transEl.textContent = text.trim();
        }
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
      
      // Global flag list: group repeated cues so the panel stays scannable.
      let chip = elements.flagsContainer.querySelector(`[data-flag="${flag}"]`);
      if (chip) {
        chip.dataset.count = String(Number(chip.dataset.count || "1") + 1);
      } else {
        chip = document.createElement("span");
        chip.className = "flag-chip";
        chip.dataset.flag = flag;
        chip.dataset.count = "1";
        chip.textContent = plainText;
        elements.flagsContainer.prepend(chip);
      }

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

  function escapeHtml(value) {
    return String(value ?? "").replace(/[&<>"']/g, (char) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
    })[char]);
  }

  function renderMarkdown(md) {
    const safe = escapeHtml(md);
    if (safe.includes("|")) {
      const rows = safe.trim().split('\n').filter(r => r.includes('|'));
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
        if (rows.length === safe.trim().split('\n').filter(r => r.trim()).length) return html;
      }
    }

    const parts = safe.split(/(?=## )/);
    if (parts.length === 3) {
      let html = '<div style="display: flex; gap: 2rem;">';
      parts.forEach(p => {
        if (!p.trim()) return;
        html += `<div style="flex: 1;">${p.replace(/\n/g, '<br>').replace(/\*\*(.*?)\*\*/g, '<strong>$1</strong>')}</div>`;
      });
      html += '</div>';
      return html;
    }

    return safe.replace(/\n/g, '<br>').replace(/\*\*(.*?)\*\*/g, '<strong>$1</strong>');
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

  // Keep the newest line in view: translations and flags are added after the bubble appears.
  let scrollQueued = false;
  new MutationObserver(() => {
    if (scrollQueued) return;
    scrollQueued = true;
    requestAnimationFrame(() => {
      scrollQueued = false;
      elements.conversation.scrollTo({ top: elements.conversation.scrollHeight, behavior: window.matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth" });
    });
  }).observe(elements.conversation, { childList: true, subtree: true, characterData: true });

  // ---- Microphone: hold to speak -------------------------------------------------
  // The stream and audio graph are opened once and kept, so there is no start-up gap
  // on every press. A short pre-roll keeps the first syllable. Releasing the button
  // sends "audio_end" so the server transcribes at once.
  const TARGET_RATE = 16000;
  const MIN_HOLD_MS = 400;
  const TAP_TO_LOCK_MS = 320;
  let micCtx = null, micStream = null, micNode = null, micSource = null;
  let micReady = null;          // promise while the mic is being opened
  let wantRecording = false;    // the button or key is held
  let recording = false;        // chunks are being sent
  let holdStartedAt = 0;
  let latchedRecording = false;
  let peakLevel = 0;
  let preroll = [];             // last few idle chunks (Float32Array at 16 kHz)
  const micHint = (() => {
    let el = document.getElementById("mic-hint");
    if (!el) {
      el = document.createElement("p");
      el.id = "mic-hint";
      el.className = "mic-hint";
      el.setAttribute("role", "status");
      el.setAttribute("aria-live", "polite");
      const host = document.querySelector(".controls-left") || document.body;
      host.appendChild(el);
    }
    return el;
  })();
  let hintTimer = null;
  function showHint(text, kind) {
    micHint.textContent = text || "";
    micHint.dataset.kind = kind || "";
    clearTimeout(hintTimer);
    if (text && kind !== "live") hintTimer = setTimeout(() => { micHint.textContent = ""; micHint.dataset.kind = ""; }, 6000);
  }
  window.advocateShowHint = showHint;

  const micLabel = elements.micBtn.querySelector(".mic-label");
  function setLatchedRecording(active) {
    latchedRecording = active;
    elements.micBtn.dataset.latched = String(active);
    if (micLabel) micLabel.textContent = active ? "Tap to stop" : "Hold or tap";
    elements.micBtn.setAttribute("aria-label", active ? "Recording hands-free. Tap to stop" : "Hold or tap to speak");
    elements.micBtn.title = active ? "Tap to stop recording" : "Hold Space, or tap for hands-free recording";
  }

  const bars = [...elements.waveform.querySelectorAll("i")];
  function drawLevel(level) {
    const v = Math.min(1, level * 6);
    if (bars.length) {
      bars.forEach((bar, i) => {
        const shape = [0.55, 0.85, 1, 0.8, 0.5][i % 5];
        bar.style.transform = `scaleY(${(0.18 + v * shape).toFixed(2)})`;
      });
    }
    elements.micBtn.style.setProperty("--level", v.toFixed(2));
  }

  function resampleTo16k(input, fromRate) {
    if (fromRate === TARGET_RATE) return new Float32Array(input);
    const ratio = fromRate / TARGET_RATE;
    const out = new Float32Array(Math.floor(input.length / ratio));
    for (let i = 0; i < out.length; i++) {
      const pos = i * ratio, i0 = Math.floor(pos), i1 = Math.min(i0 + 1, input.length - 1);
      out[i] = input[i0] + (input[i1] - input[i0]) * (pos - i0);
    }
    return out;
  }

  function sendChunk(float16k) {
    if (ws && ws.readyState === WebSocket.OPEN && float16k.length) {
      ws.send(JSON.stringify({ type: "audio", data: base64EncodeAudio(float16k) }));
    }
  }

  function base64EncodeAudio(float32Array) {
    const bytes = new Uint8Array(float32Array.length * 2);
    const view = new DataView(bytes.buffer);
    for (let i = 0; i < float32Array.length; i++) {
      const s = Math.max(-1, Math.min(1, float32Array[i]));
      view.setInt16(i * 2, s < 0 ? s * 0x8000 : s * 0x7FFF, true);
    }
    let binary = "";
    for (let i = 0; i < bytes.length; i += 0x8000) {
      binary += String.fromCharCode.apply(null, bytes.subarray(i, i + 0x8000));
    }
    return btoa(binary);
  }

  function openMic() {
    if (micReady) return micReady;
    micReady = (async () => {
      if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
        throw new Error("This page cannot use the microphone here. Open it at http://127.0.0.1:8000 in Chrome.");
      }
      micStream = await navigator.mediaDevices.getUserMedia({
        audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true, autoGainControl: true }
      });
      const Ctx = window.AudioContext || window.webkitAudioContext;
      try { micCtx = new Ctx({ sampleRate: TARGET_RATE }); } catch (e) { micCtx = new Ctx(); }
      micSource = micCtx.createMediaStreamSource(micStream);
      micNode = micCtx.createScriptProcessor(2048, 1, 1);
      const silent = micCtx.createGain();
      silent.gain.value = 0;                      // never play the microphone back
      micNode.onaudioprocess = (e) => {
        const chunk = resampleTo16k(e.inputBuffer.getChannelData(0), micCtx.sampleRate);
        let sum = 0;
        for (let i = 0; i < chunk.length; i++) sum += chunk[i] * chunk[i];
        const level = Math.sqrt(sum / Math.max(1, chunk.length));
        if (recording) {
          peakLevel = Math.max(peakLevel, level);
          drawLevel(level);
          sendChunk(chunk);
        } else {
          preroll.push(chunk);
          if (preroll.length > 3) preroll.shift();   // about 0.35 s
        }
      };
      micSource.connect(micNode);
      micNode.connect(silent);
      silent.connect(micCtx.destination);
      micStream.getAudioTracks().forEach((t) => { t.onended = () => { closeMic(); showHint("The microphone was disconnected.", "error"); }; });
    })().catch((err) => {
      micReady = null;
      throw err;
    });
    return micReady;
  }

  function closeMic() {
    recording = false; wantRecording = false;
    setLatchedRecording(false);
    try { if (micNode) micNode.disconnect(); if (micSource) micSource.disconnect(); } catch (e) {}
    try { if (micStream) micStream.getTracks().forEach((t) => t.stop()); } catch (e) {}
    try { if (micCtx) micCtx.close(); } catch (e) {}
    micCtx = micStream = micNode = micSource = null; micReady = null; preroll = [];
    elements.micBtn.classList.remove("active");
  }

  async function startMic() {
    if (wantRecording) return;
    if (!ws || ws.readyState !== WebSocket.OPEN) { showHint("Not connected yet. Wait a moment and try again.", "error"); return; }
    wantRecording = true;
    holdStartedAt = performance.now();
    elements.micBtn.classList.add("active");
    elements.micBtn.setAttribute("aria-pressed", "true");
    showHint("Listening… hold to talk, or release quickly for hands-free recording.", "live");
    elements.statusIndicator.textContent = "Recording";
    document.body.dataset.state = "recording";
    elements.waveform.classList.add("active");
    // Unlock audio playback while we still have the user's gesture.
    try {
      if (!outAudioCtx) outAudioCtx = new (window.AudioContext || window.webkitAudioContext)({ sampleRate: 24000 });
      if (outAudioCtx.state === "suspended") outAudioCtx.resume();
    } catch (e) {}
    try {
      await openMic();
      if (micCtx.state === "suspended") await micCtx.resume();
    } catch (err) {
      wantRecording = false;
      setLatchedRecording(false);
      elements.micBtn.classList.remove("active");
      elements.micBtn.setAttribute("aria-pressed", "false");
      const denied = err && (err.name === "NotAllowedError" || err.name === "SecurityError");
      showHint(denied ? "Microphone is blocked. Allow it in the address bar, then try again, or type the line."
                      : (err && err.message) || "The microphone could not start. You can type the line instead.", "error");
      return;
    }
    if (!wantRecording) return;                    // released before the mic was ready: stopMic reports it
    peakLevel = 0;
    preroll.forEach(sendChunk);                    // keep the first syllable
    preroll = [];
    recording = true;
  }

  function stopMic() {
    if (!wantRecording) return;
    wantRecording = false;
    setLatchedRecording(false);
    const wasRecording = recording;
    recording = false;
    elements.micBtn.classList.remove("active");
    elements.micBtn.setAttribute("aria-pressed", "false");
    drawLevel(0);
    elements.statusIndicator.textContent = wasRecording ? "Working…" : "Ready";
    document.body.dataset.state = wasRecording ? "thinking" : "idle";
    const held = performance.now() - holdStartedAt;
    if (!wasRecording) {
      showHint("Hold the button a little longer: the microphone was still starting.", "error");
      return;
    }
    if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify({ type: "audio_end", speaker: speakerChoice }));
    if (held < MIN_HOLD_MS) showHint("That was very short. Hold the button while you speak, then let go.", "error");
    else if (peakLevel < 0.004) showHint("No sound reached the microphone. Check the input device in your system settings.", "error");
    else showHint("Got it. Transcribing…", "live");
  }

  // Who is speaking: "auto" goes by language (English = landlord, your language = you).
  let speakerChoice = "auto";
  const speakerButtons = [...document.querySelectorAll("#speaker-choice [data-speaker]")];
  function setSpeaker(value) {
    speakerChoice = value;
    speakerButtons.forEach((b) => b.setAttribute("aria-checked", String(b.dataset.speaker === value)));
  }
  speakerButtons.forEach((b, i) => {
    b.addEventListener("click", () => setSpeaker(b.dataset.speaker));
    b.addEventListener("keydown", (e) => {
      if (e.key !== "ArrowRight" && e.key !== "ArrowLeft") return;
      e.preventDefault();
      const next = speakerButtons[(i + (e.key === "ArrowRight" ? 1 : speakerButtons.length - 1)) % speakerButtons.length];
      next.focus(); setSpeaker(next.dataset.speaker);
    });
  });

  // Pointer events support both familiar patterns on one control:
  // hold and release for push-to-talk, or tap once to latch and tap again to stop.
  let pointerActive = false;
  let pointerStartedAt = 0;
  elements.micBtn.addEventListener("pointerdown", (e) => {
    if (e.button !== undefined && e.button !== 0) return;
    e.preventDefault();
    if (latchedRecording) {
      pointerActive = false;
      stopMic();
      return;
    }
    pointerActive = true;
    pointerStartedAt = performance.now();
    try { elements.micBtn.setPointerCapture(e.pointerId); } catch (err) {}
    startMic();
  });
  elements.micBtn.addEventListener("pointerup", (e) => {
    if (!pointerActive) return;
    e.preventDefault();
    pointerActive = false;
    if (performance.now() - pointerStartedAt <= TAP_TO_LOCK_MS && wantRecording) {
      setLatchedRecording(true);
      showHint("Recording hands-free. Tap again to stop.", "live");
      return;
    }
    stopMic();
  });
  ["pointercancel", "lostpointercapture"].forEach((name) => elements.micBtn.addEventListener(name, () => {
    if (!pointerActive) return;
    pointerActive = false;
    stopMic();
  }));
  elements.micBtn.addEventListener("click", (e) => e.preventDefault());
  elements.micBtn.addEventListener("contextmenu", (e) => e.preventDefault());

  const typingTarget = (t) => t && (t.tagName === "INPUT" || t.tagName === "TEXTAREA" || t.tagName === "SELECT" || t.isContentEditable);
  let spaceHeld = false;
  document.addEventListener("keydown", (e) => {
    if (e.code !== "Space" || typingTarget(e.target)) return;
    e.preventDefault();                            // no page scroll, no button "click"
    if (e.repeat || spaceHeld) return;
    spaceHeld = true;
    startMic();
  });
  document.addEventListener("keyup", (e) => {
    if (e.code !== "Space") return;
    if (!spaceHeld) return;
    spaceHeld = false;
    e.preventDefault();
    stopMic();
  });
  window.addEventListener("blur", () => { spaceHeld = false; stopMic(); });
  document.addEventListener("visibilitychange", () => { if (document.hidden) { spaceHeld = false; stopMic(); } });
  window.addEventListener("pagehide", closeMic);

  const tenantTextInput = document.getElementById("tenant-text-input");
  tenantTextInput.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && tenantTextInput.value.trim() !== "") {
      if (ws && ws.readyState === WebSocket.OPEN) {
        ws.send(JSON.stringify({ type: "tenant_text", text: tenantTextInput.value.trim() }));
        tenantTextInput.value = "";
        showHint("Sent. Translating…", "live");
      } else {
        showHint("Not connected yet. Wait a moment and try again.", "error");
      }
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
        if (activePlayBtn) {
          activePlayBtn.textContent = "▶ Play";
          activePlayBtn.disabled = false;
        }
        btn.textContent = "Speaking...";
        btn.disabled = true;
        activePlayBtn = btn;
        
        if (ws && ws.readyState === WebSocket.OPEN) {
          ws.send(JSON.stringify({ type: "landlord_text", id: line.id, text: line.text }));
        } else {
          btn.textContent = "\u25B6 Play"; btn.disabled = false; activePlayBtn = null;
          if (window.advocateShowHint) window.advocateShowHint("Not connected yet. Wait a moment and try again.", "error");
        }
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
    elements.tabSimulator.classList.remove("active");
    elements.viewAdvocate.classList.add("active");
    elements.viewAdvocate.classList.remove("hidden");
    elements.viewScore.classList.remove("active");
    elements.viewSimulator.classList.remove("active");
    elements.tabAdvocate.setAttribute("aria-selected", "true");
    elements.tabScore.setAttribute("aria-selected", "false");
    elements.tabSimulator.setAttribute("aria-selected", "false");
  });

  elements.tabSimulator.addEventListener("click", () => {
    elements.tabSimulator.classList.add("active");
    elements.tabAdvocate.classList.remove("active");
    elements.tabScore.classList.remove("active");
    elements.viewSimulator.classList.add("active");
    elements.viewSimulator.classList.remove("hidden");
    elements.viewAdvocate.classList.remove("active");
    elements.viewScore.classList.remove("active");
    elements.tabSimulator.setAttribute("aria-selected", "true");
    elements.tabAdvocate.setAttribute("aria-selected", "false");
    elements.tabScore.setAttribute("aria-selected", "false");
  });

  elements.tabScore.addEventListener("click", async () => {
    elements.tabScore.classList.add("active");
    elements.tabAdvocate.classList.remove("active");
    elements.tabSimulator.classList.remove("active");
    elements.viewScore.classList.add("active");
    elements.viewScore.classList.remove("hidden");
    elements.viewAdvocate.classList.remove("active");
    elements.viewSimulator.classList.remove("active");
    elements.tabScore.setAttribute("aria-selected", "true");
    elements.tabAdvocate.setAttribute("aria-selected", "false");
    elements.tabSimulator.setAttribute("aria-selected", "false");

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
