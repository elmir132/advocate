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
        showSummary(msg.markdown, msg.languages, msg.overview);
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
  const activeAudioSources = new Set();

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
    activeAudioSources.add(source);
    source.onended = () => activeAudioSources.delete(source);

    const currentTime = outAudioCtx.currentTime;
    if (nextStartTime < currentTime) {
      nextStartTime = currentTime + 0.05; // 50ms buffer to prevent initial click
    }
    source.start(nextStartTime);
    nextStartTime += audioBuffer.duration;
  }

  function stopVoicePlayback() {
    activeAudioSources.forEach((source) => {
      try { source.stop(0); } catch (error) {}
    });
    activeAudioSources.clear();
    nextStartTime = outAudioCtx ? outAudioCtx.currentTime : 0;
  }

  muteToggle.addEventListener("change", () => {
    document.body.dataset.audioMuted = String(muteToggle.checked);
    if (muteToggle.checked) {
      stopVoicePlayback();
      showHint("Voice playback muted. Transcription and analysis will continue.", "live");
    } else {
      showHint("Voice playback is on.", "live");
    }
  });

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

  const SUMMARY_HEADINGS = {
    uk: ["Коротко про розмову", "Обіцянки", "Не є обіцянкою", "Фінансові умови, штрафи та правила"],
    ru: ["Кратко о разговоре", "Обещания", "Не является обязательством", "Финансовые условия, штрафы и правила"],
    es: ["Resumen de la conversación", "Promesas", "No es un compromiso", "Condiciones económicas, penalizaciones y reglas"],
    zh: ["对话概述", "承诺", "并非明确承诺", "费用、处罚和规则"],
    en: ["Conversation overview", "Promises", "Not a commitment", "Money terms, penalties and rules"],
  };

  function renderMarkdown(md, languages = [], overview = null) {
    const sections = [];
    let current = null;
    const lines = String(md || "").split(/\r?\n/);
    for (const rawLine of lines) {
      const line = rawLine.trim();
      if (!line) continue;
      const heading = line.match(/^#{1,3}\s+(.+)$/);
      if (heading) {
        current = { title: heading[1].replace(/\*\*/g, "").trim(), items: [] };
        sections.push(current);
        continue;
      }
      if (!current) {
        current = { title: "Summary", items: [] };
        sections.push(current);
      }
      current.items.push(line.replace(/^[-*]\s+/, "").replace(/\*\*/g, "").trim());
    }

    const generic = /^(summary|резюме|підсумок)$/i;
    const useful = sections.filter((section) => !(generic.test(section.title) && section.items.length === 0));
    const empty = /^(none|none\.|no items|немає|жодного|ningun[oa]|无)$/i;
    const tenantLanguage = String(languages[0] || "").split("-", 1)[0].toLowerCase();
    const headingsPerLanguage = SUMMARY_HEADINGS.en.length;
    if (useful.length >= headingsPerLanguage * 2) {
      const tenantHeadings = SUMMARY_HEADINGS[tenantLanguage];
      if (tenantHeadings) tenantHeadings.forEach((title, index) => { useful[index].title = title; });
      SUMMARY_HEADINGS.en.forEach((title, index) => { useful[useful.length - headingsPerLanguage + index].title = title; });
    } else if (useful.length === headingsPerLanguage && SUMMARY_HEADINGS[tenantLanguage]) {
      SUMMARY_HEADINGS[tenantLanguage].forEach((title, index) => { useful[index].title = title; });
    }
    if (overview && useful.length >= headingsPerLanguage * 2) {
      useful[0].items = [overview.tenant];
      useful[useful.length - headingsPerLanguage].items = [overview.english];
    }
    const sectionHtml = (section) => {
      const title = escapeHtml(section.title);
      const items = section.items.filter(Boolean);
      const body = !items.length || items.every((item) => empty.test(item))
        ? '<p class="summary-empty">No items recorded</p>'
        : `<ul>${items.filter((item) => !empty.test(item)).map((item) => `<li>${escapeHtml(item)}</li>`).join("")}</ul>`;
      return `<section class="summary-section"><h3>${title}</h3>${body}</section>`;
    };

    if (useful.length >= headingsPerLanguage * 2) {
      const split = Math.ceil(useful.length / 2);
      return `<div class="summary-grid"><div class="summary-language"><span class="summary-language-label">Tenant language</span>${useful.slice(0, split).map(sectionHtml).join("")}</div><div class="summary-language"><span class="summary-language-label">English</span>${useful.slice(split).map(sectionHtml).join("")}</div></div>`;
    }
    return `<div class="summary-grid summary-grid-single"><div class="summary-language">${useful.map(sectionHtml).join("")}</div></div>`;
  }

  function showSummary(markdown, languages, overview) {
    elements.summaryContent.innerHTML = renderMarkdown(markdown, languages, overview);
    
    // Add copy button if not exists
    if (!document.getElementById("copy-summary-btn")) {
      const copyBtn = document.createElement("button");
      copyBtn.id = "copy-summary-btn";
      copyBtn.textContent = "Copy summary";
      copyBtn.style.marginTop = "1rem";
      copyBtn.style.marginRight = "1rem";
      copyBtn.onclick = () => {
        navigator.clipboard.writeText(elements.summaryContent.innerText).then(() => {
          copyBtn.textContent = "Copied";
          setTimeout(() => copyBtn.textContent = "Copy summary", 2000);
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
  const MAX_UPLOAD_SECONDS = 60;
  const MAX_UPLOAD_BYTES = 25_000_000;
  const UPLOAD_CHUNK_SAMPLES = 16_000;
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

  const wait = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

  async function uploadAudioFile(file, selectedSpeaker) {
    if (!file) return;
    if (!ws || ws.readyState !== WebSocket.OPEN) throw new Error("Not connected yet. Wait a moment and try again.");
    if (wantRecording || recording) throw new Error("Stop the live microphone before uploading a recording.");
    if (file.size > MAX_UPLOAD_BYTES) throw new Error("That file is too large. Choose a voice message under 25 MB and 60 seconds.");
    if (file.type && !file.type.startsWith("audio/")) throw new Error("Choose an audio file, not a video. WAV, MP3, M4A, OGG and WebM audio are typical choices.");
    showHint(`Reading ${file.name}…`, "live");
    elements.statusIndicator.textContent = "Reading audio";

    const Ctx = window.AudioContext || window.webkitAudioContext;
    const decodeContext = new Ctx();
    let audioBuffer;
    try {
      audioBuffer = await decodeContext.decodeAudioData(await file.arrayBuffer());
    } catch (error) {
      throw new Error("This audio file could not be read by the browser. Try WAV, MP3 or M4A.");
    } finally {
      try { await decodeContext.close(); } catch (error) {}
    }
    if (!Number.isFinite(audioBuffer.duration) || audioBuffer.duration < 0.4) throw new Error("The voice message is too short to transcribe.");
    if (audioBuffer.duration > MAX_UPLOAD_SECONDS + 0.05) throw new Error("Choose a voice message that is 60 seconds or shorter.");

    const mono = new Float32Array(audioBuffer.length);
    for (let channel = 0; channel < audioBuffer.numberOfChannels; channel++) {
      const data = audioBuffer.getChannelData(channel);
      const weight = 1 / audioBuffer.numberOfChannels;
      for (let i = 0; i < data.length; i++) mono[i] += data[i] * weight;
    }
    const pcm16k = resampleTo16k(mono, audioBuffer.sampleRate);
    const chunks = Math.ceil(pcm16k.length / UPLOAD_CHUNK_SAMPLES);
    elements.statusIndicator.textContent = "Uploading";
    document.body.dataset.state = "thinking";
    for (let offset = 0, index = 0; offset < pcm16k.length; offset += UPLOAD_CHUNK_SAMPLES, index++) {
      if (!ws || ws.readyState !== WebSocket.OPEN) throw new Error("The connection closed during upload. Reconnect and try again.");
      sendChunk(pcm16k.subarray(offset, Math.min(offset + UPLOAD_CHUNK_SAMPLES, pcm16k.length)));
      const progress = Math.round(((index + 1) / chunks) * 100);
      showHint(`Uploading ${file.name}: ${progress}%`, "live");
      while (ws.bufferedAmount > 256_000) await wait(10);
      if (index % 8 === 0) await wait(0);
    }
    ws.send(JSON.stringify({ type: "audio_end", speaker: selectedSpeaker, source: "upload" }));
    elements.statusIndicator.textContent = "Working…";
    showHint("Audio received. Transcribing and checking the conversation…", "live");
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
  function updateTextComposer(value) {
    const input = document.getElementById("tenant-text-input");
    if (!input) return;
    const copy = value === "landlord"
      ? ["Type the landlord’s message", "Type the landlord’s message"]
      : value === "tenant"
        ? ["Type your message", "Type the renter’s message"]
        : ["Type a message — Auto detects the speaker", "Type a message; Auto routes English to the landlord and the selected language to the renter"];
    input.placeholder = copy[0];
    input.setAttribute("aria-label", copy[1]);
    input.dataset.speaker = value;
  }

  function setSpeaker(value) {
    speakerChoice = value;
    speakerButtons.forEach((b) => b.setAttribute("aria-checked", String(b.dataset.speaker === value)));
    updateTextComposer(value);
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
  updateTextComposer(speakerChoice);

  const audioFileInput = document.getElementById("audio-file-input");
  const audioUploadButton = document.getElementById("audio-upload-btn");
  audioUploadButton.addEventListener("click", () => audioFileInput.click());
  audioFileInput.addEventListener("change", async () => {
    const file = audioFileInput.files && audioFileInput.files[0];
    if (!file) return;
    const selectedSpeaker = speakerChoice;
    audioUploadButton.disabled = true;
    elements.micBtn.disabled = true;
    try {
      await uploadAudioFile(file, selectedSpeaker);
    } catch (error) {
      elements.statusIndicator.textContent = "Ready";
      document.body.dataset.state = "idle";
      showHint(error && error.message ? error.message : "The audio file could not be uploaded.", "error");
    } finally {
      audioUploadButton.disabled = false;
      elements.micBtn.disabled = false;
      audioFileInput.value = "";
    }
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
    const text = tenantTextInput.value.trim();
    if (e.key === "Enter" && text !== "") {
      if (ws && ws.readyState === WebSocket.OPEN) {
        if (speakerChoice === "landlord") {
          ws.send(JSON.stringify({ type: "landlord_text", id: `typed-${Date.now()}`, text }));
          showHint("Landlord message sent. Translating and checking details…", "live");
        } else if (speakerChoice === "tenant") {
          ws.send(JSON.stringify({ type: "tenant_text", text }));
          showHint("Your message was sent. Translating…", "live");
        } else {
          ws.send(JSON.stringify({ type: "auto_text", text }));
          showHint("Detecting the language and speaker…", "live");
        }
        tenantTextInput.value = "";
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
  const tabPairs = [
    [elements.tabAdvocate, elements.viewAdvocate],
    [elements.tabSimulator, elements.viewSimulator],
    [elements.tabScore, elements.viewScore],
  ];

  function activateTab(selectedTab) {
    tabPairs.forEach(([tab, view]) => {
      const active = tab === selectedTab;
      tab.classList.toggle("active", active);
      tab.setAttribute("aria-selected", String(active));
      tab.tabIndex = active ? 0 : -1;
      view.classList.toggle("active", active);
      view.classList.toggle("hidden", !active);
    });
  }

  tabPairs.forEach(([tab], index) => {
    tab.addEventListener("keydown", (event) => {
      let next = null;
      if (event.key === "ArrowRight") next = (index + 1) % tabPairs.length;
      if (event.key === "ArrowLeft") next = (index + tabPairs.length - 1) % tabPairs.length;
      if (event.key === "Home") next = 0;
      if (event.key === "End") next = tabPairs.length - 1;
      if (next === null) return;
      event.preventDefault();
      tabPairs[next][0].focus();
      tabPairs[next][0].click();
    });
  });

  elements.tabAdvocate.addEventListener("click", () => activateTab(elements.tabAdvocate));
  elements.tabSimulator.addEventListener("click", () => activateTab(elements.tabSimulator));
  const aboutDialog = document.getElementById("about-dialog");
  document.getElementById("about-help").addEventListener("click", () => aboutDialog.showModal());
  document.getElementById("close-about").addEventListener("click", () => aboutDialog.close());
  aboutDialog.addEventListener("click", (event) => { if (event.target === aboutDialog) aboutDialog.close(); });

  function scoreNode(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  function renderScorecard(score) {
    const percent = (value) => value == null ? "—" : `${(value * 100).toFixed(value === 1 ? 0 : 1)}%`;
    const output = elements.scoreOutput;
    output.replaceChildren();

    const metrics = scoreNode("div", "score-metrics");
    [
      ["Precision", percent(score.micro_precision)],
      ["Recall", percent(score.micro_recall)],
      ["Promise accuracy", percent(score.promise_accuracy)],
    ].forEach(([label, value]) => {
      const card = scoreNode("article", "score-metric");
      card.append(scoreNode("span", "score-label", label), scoreNode("strong", "score-value", value));
      metrics.appendChild(card);
    });
    output.appendChild(metrics);

    const context = scoreNode("div", "score-context");
    context.append(
      scoreNode("strong", "", "How to read this"),
      scoreNode("p", "", "Precision asks whether displayed cues were relevant. Recall asks whether the expected cues were found. Higher is better, but this set was written and reviewed during development.")
    );
    output.appendChild(context);

    const heading = scoreNode("div", "score-section-title");
    heading.append(scoreNode("h4", "", "Results by cue"), scoreNode("span", "", `${score.lines} lines`));
    output.appendChild(heading);

    const table = scoreNode("table", "score-table");
    const head = document.createElement("thead");
    const headRow = document.createElement("tr");
    ["Conversation cue", "Precision", "Recall"].forEach((text) => headRow.appendChild(scoreNode("th", "", text)));
    head.appendChild(headRow); table.appendChild(head);
    const body = document.createElement("tbody");
    const labels = {
      financial_term: "Financial terms",
      penalty: "Penalties",
      vague_commitment: "Vague commitments",
      promise: "Promises",
      restriction: "Restrictions",
    };
    Object.entries(labels).forEach(([key, label]) => {
      const row = document.createElement("tr");
      const values = score.per_flag[key] || {};
      row.append(scoreNode("th", "", label), scoreNode("td", "score-number", percent(values.precision)), scoreNode("td", "score-number", percent(values.recall)));
      body.appendChild(row);
    });
    table.appendChild(body); output.appendChild(table);
    const caveat = scoreNode("div", "score-caveat");
    caveat.append(scoreNode("strong", "", "Development sample only"), scoreNode("span", "", "Two of 20 scripted lines contained at least one error."));
    output.appendChild(caveat);
  }

  elements.tabScore.addEventListener("click", async () => {
    activateTab(elements.tabScore);
    elements.scoreOutput.textContent = "Loading evaluation…";
    const _fetch = window.OriginalFetch || window.fetch;
    try {
      const [resEval, resLines] = await Promise.all([
        _fetch(API_BASE + "/eval/results.json").catch(() => null),
        _fetch(API_BASE + "/data/landlord_lines.json").catch(() => null)
      ]);
      if (!resEval || !resEval.ok) {
        elements.scoreOutput.textContent = "Evaluation results are not available yet.";
        return;
      }
      const results = await resEval.json();
      const truth = (resLines && resLines.ok) ? await resLines.json() : window.landlordLines;
      if (!Array.isArray(truth)) throw new Error("Scripted lines could not be loaded.");

      const CATS = ["financial_term", "penalty", "vague_commitment", "promise", "restriction"];
      const got = {};
      results.forEach((result) => { got[result.id] = result; });
      const tp = {}, fp = {}, fn = {};
      CATS.forEach((category) => { tp[category] = 0; fp[category] = 0; fn[category] = 0; });
      let promiseOk = 0;
      truth.forEach((line) => {
        const result = got[line.id] || { flags: [], promise: false };
        const expected = new Set(line.expected_flags || []);
        const predicted = new Set((result.flags || []).filter((flag) => CATS.includes(flag)));
        CATS.forEach((category) => {
          if (expected.has(category) && predicted.has(category)) tp[category]++;
          else if (predicted.has(category)) fp[category]++;
          else if (expected.has(category)) fn[category]++;
        });
        if (Boolean(result.promise) === Boolean(line.promise)) promiseOk++;
      });
      const ratio = (correct, incorrect) => (correct + incorrect) ? Number((correct / (correct + incorrect)).toFixed(3)) : null;
      const sum = (values) => Object.values(values).reduce((total, value) => total + value, 0);
      const perFlag = {};
      CATS.forEach((category) => { perFlag[category] = { precision: ratio(tp[category], fp[category]), recall: ratio(tp[category], fn[category]) }; });
      renderScorecard({
        lines: truth.length,
        micro_precision: ratio(sum(tp), sum(fp)),
        micro_recall: ratio(sum(tp), sum(fn)),
        promise_accuracy: Number((promiseOk / truth.length).toFixed(3)),
        per_flag: perFlag,
      });
    } catch (error) {
      elements.scoreOutput.textContent = "Evaluation results could not be loaded: " + error.message;
    }
  });

  activateTab(elements.tabAdvocate);

  // Init
  loadSimulator();
  connect();
});
