/* Video guide: Sahayak, a video-call assistant that walks a customer through their application.

   - Voice: the browser's speech synthesis, with the most natural installed voice chosen per language
     (Premium / Enhanced / Natural / Google voices first, novelty voices never), spoken sentence by
     sentence so long replies are not cut off, with rupee amounts and acronyms rewritten for speech.
   - Face: an illustrated presenter drawn in SVG. Lips follow the words (a viseme timeline built from
     the text and re-synchronised on every word boundary the voice reports); eyes blink and glance;
     the head sways while idle and tilts while listening.
   - Conversation: hands-free turn-taking with speech recognition where the browser has it. Tap the
     presenter to interrupt. Camera and microphone are previews only: no frame or sound leaves the
     device, only the recognised text of a question is sent.
   - Every word it says comes from POST /api/guide. Its buttons only take the customer where they need
     to go; it never presses submit or accept on their behalf. */

const SPEECH_LANG = { en: "en-IN", hi: "hi-IN", id: "id-ID" };
const VOICE_FALLBACKS = { en: ["en-IN", "en-GB", "en-AU", "en-US"], hi: ["hi-IN"], id: ["id-ID"] };
// macOS ships these as joke or effect voices; they must never be picked automatically.
const NOVELTY = /\b(albert|bad news|bahh|bells|boing|bubbles|cellos|good news|jester|organ|superstar|trinoids|whisper|wobble|zarvox|fred|junior|ralph|kathy|grandma|grandpa|rocko|shelley|eddy|flo|reed|sandy)\b/i;
const NAME = "Sahayak";

const UI = {
  en: { role: "Your application guide", start: "Video call with Sahayak", onCall: "On call · tap to open",
        placeholder: "Type a question", send: "Ask", step: "Step", of: "of", listening: "Listening…", thinking: "Thinking…",
        you: "You", mic: "Talk hands-free", micOff: "Stop listening", cam: "Camera", cc: "Captions", mute: "Mute voice",
        unmute: "Unmute voice", end: "End call", expand: "Larger", shrink: "Smaller", minimise: "Minimise",
        voice: "Voice", auto: "Best available", camNote: "Your camera is shown only to you.", interrupt: "Tap me to interrupt",
        aiBadge: "AI presenter", connecting: "Connecting the video presenter…", sound: "🔊 Tap to turn on sound",
        avatarEnded: "The video presenter has ended. I'll carry on from here.",
        noMic: "The microphone is blocked here. You can type your questions instead.",
        noCam: "The camera could not be opened here. Talking and typing still work.",
        list: "Here are your applications. Open one and I'll tell you where it stands, or start a new application.",
        chips: ["What do I do next?", "Which documents do you need?", "My statement has a password", "How long will it take?", "I want to talk to a person"] },
  hi: { role: "आपका आवेदन गाइड", start: "सहायक से वीडियो कॉल", onCall: "कॉल पर · खोलने के लिए दबाएँ",
        placeholder: "अपना सवाल लिखें", send: "पूछें", step: "चरण", of: "में से", listening: "सुन रहा हूँ…", thinking: "सोच रहा हूँ…",
        you: "आप", mic: "बिना हाथ लगाए बात करें", micOff: "सुनना बंद करें", cam: "कैमरा", cc: "कैप्शन", mute: "आवाज़ बंद",
        unmute: "आवाज़ चालू", end: "कॉल समाप्त", expand: "बड़ा करें", shrink: "छोटा करें", minimise: "छोटा करके रखें",
        voice: "आवाज़", auto: "सबसे अच्छी उपलब्ध", aiBadge: "AI प्रस्तुतकर्ता", sound: "🔊 आवाज़ चालू करने के लिए दबाएँ",
        connecting: "वीडियो प्रस्तुतकर्ता जुड़ रहा है…", avatarEnded: "वीडियो प्रस्तुतकर्ता का सत्र खत्म हो गया। मैं यहीं से आगे मदद करूँगा।", camNote: "आपका कैमरा सिर्फ़ आपको दिखता है।", interrupt: "रोकने के लिए मुझे दबाएँ",
        noMic: "यहाँ माइक्रोफ़ोन बंद है। आप अपना सवाल लिख सकते हैं।",
        noCam: "यहाँ कैमरा नहीं खुल सका। बात करना और लिखना अब भी काम करता है।",
        list: "ये आपके आवेदन हैं। किसी एक को खोलें, मैं बताऊँगा कि वह कहाँ तक पहुँचा है, या नया आवेदन शुरू करें।",
        chips: ["अब मुझे क्या करना है?", "कौन से दस्तावेज़ चाहिए?", "मेरे स्टेटमेंट पर पासवर्ड है", "कितना समय लगेगा?", "मुझे किसी से बात करनी है"] },
  id: { role: "Pemandu pengajuan Anda", start: "Panggilan video dengan Sahayak", onCall: "Sedang panggilan · ketuk untuk membuka",
        placeholder: "Tulis pertanyaan", send: "Tanya", step: "Langkah", of: "dari", listening: "Mendengarkan…", thinking: "Berpikir…",
        you: "Anda", mic: "Bicara tanpa tangan", micOff: "Berhenti mendengarkan", cam: "Kamera", cc: "Teks", mute: "Bisukan suara",
        unmute: "Suarakan", end: "Akhiri panggilan", expand: "Perbesar", shrink: "Perkecil", minimise: "Kecilkan",
        voice: "Suara", auto: "Terbaik yang tersedia", aiBadge: "Presenter AI", sound: "🔊 Ketuk untuk menyalakan suara",
        connecting: "Menghubungkan presenter video…", avatarEnded: "Sesi presenter video telah berakhir. Saya lanjutkan dari sini.", camNote: "Kamera Anda hanya terlihat oleh Anda.", interrupt: "Ketuk saya untuk menyela",
        noMic: "Mikrofon diblokir di sini. Anda bisa mengetik pertanyaan.",
        noCam: "Kamera tidak bisa dibuka di sini. Bicara dan mengetik tetap berfungsi.",
        list: "Ini daftar pengajuan Anda. Buka salah satu dan saya akan memberi tahu posisinya, atau mulai pengajuan baru.",
        chips: ["Apa yang harus saya lakukan?", "Dokumen apa yang dibutuhkan?", "Rekening koran saya berpassword", "Berapa lama prosesnya?", "Saya ingin bicara dengan petugas"] },
};

/* ------------------------------------------------------------------ small DOM helpers */

const SVG_NS = "http://www.w3.org/2000/svg";
function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}
function svg(tag, attrs = {}, parent = null) {
  const node = document.createElementNS(SVG_NS, tag);
  Object.entries(attrs).forEach(([k, v]) => node.setAttribute(k, v));
  if (parent) parent.appendChild(node);
  return node;
}
const ICONS = {
  mic: "M12 3a3 3 0 0 0-3 3v6a3 3 0 0 0 6 0V6a3 3 0 0 0-3-3z M5 11a7 7 0 0 0 14 0 M12 18v3",
  cam: "M3 7h12v10H3z M15 10.5l6-3.5v10l-6-3.5",
  cc: "M3 5h18v14H3z M10.5 10a2.5 2.5 0 1 0 0 4 M17.5 10a2.5 2.5 0 1 0 0 4",
  speaker: "M4 9h4l5-4v14l-5-4H4z M16.5 9a4 4 0 0 1 0 6 M19 6.5a8 8 0 0 1 0 11",
  end: "M3 14.5c5-4.7 13-4.7 18 0l-2.4 2.6-3.6-1.4v-2.4a10 10 0 0 0-6 0v2.4l-3.6 1.4z",
  expand: "M4 10V4h6 M20 14v6h-6 M4 4l6 6 M20 20l-6-6",
  minimise: "M5 12h14",
};
function iconButton(name, label, extra = "") {
  const button = el("button", `guide-ctl ${extra}`.trim());
  button.type = "button";
  button.setAttribute("aria-label", label);
  button.title = label;
  const icon = svg("svg", { viewBox: "0 0 24 24", "aria-hidden": "true", class: "guide-icon" });
  svg("path", { d: ICONS[name] }, icon);
  button.appendChild(icon);
  return button;
}
function store(key, value) {
  try {
    if (value === undefined) return localStorage.getItem(key);
    localStorage.setItem(key, value);
  } catch { /* private window or blocked storage: preferences simply do not persist */ }
  return null;
}

/* ------------------------------------------------------------------ the presenter */

// Mouth shapes (visemes). The lips are one closed path around (120,151).
const MOUTH = {
  rest:   "M105 150 Q120 158 135 150 Q120 154 105 150 Z",
  closed: "M106 151 Q120 153 134 151 Q120 154 106 151 Z",
  small:  "M107 149 Q120 159 133 149 Q120 151 107 149 Z",
  open:   "M108 146 Q120 169 132 146 Q120 149 108 146 Z",
  round:  "M113 145 Q120 165 127 145 Q120 141 113 145 Z",
  wide:   "M103 148 Q120 161 137 148 Q120 151 103 148 Z",
  fv:     "M106 150 Q120 156 134 150 Q120 151 106 150 Z",
};

function visemeOf(ch) {
  const c = ch.toLowerCase();
  if (/[\s.,!?;:\-–—।'"()]/.test(c)) return "rest";
  if (/[mbp]/.test(c) || /[पफबभम]/.test(c)) return "closed";
  if (/[fv]/.test(c)) return "fv";
  if (/[ouw]/.test(c) || /[उऊओऔोौुू]/.test(c)) return "round";
  if (/[a]/.test(c) || /[अआा]/.test(c)) return "open";
  if (/[eiy]/.test(c) || /[इईएऐिीेै]/.test(c)) return "wide";
  return "small";
}

function buildPresenter() {
  const root = svg("svg", { viewBox: "0 0 240 240", class: "guide-avatar", role: "img", "aria-label": NAME });
  const body = svg("g", { class: "av-body" }, root);
  svg("path", { class: "av-shirt", d: "M38 240 C42 200 70 184 120 182 C170 184 198 200 202 240 Z" }, body);
  svg("path", { class: "av-collar", d: "M100 184 L120 206 L140 184 Z" }, body);
  svg("path", { class: "av-lanyard", d: "M104 186 L118 222 M136 186 L122 222" }, body);
  svg("rect", { class: "av-badge", x: "111", y: "218", width: "18", height: "14", rx: "2" }, body);

  const head = svg("g", { class: "av-head" }, root);
  svg("rect", { class: "av-skin", x: "108", y: "160", width: "24", height: "28", rx: "8" }, head); // neck
  svg("ellipse", { class: "av-skin", cx: "68", cy: "112", rx: "8", ry: "12" }, head);              // ears
  svg("ellipse", { class: "av-skin", cx: "172", cy: "112", rx: "8", ry: "12" }, head);
  svg("ellipse", { class: "av-skin", cx: "120", cy: "112", rx: "52", ry: "60" }, head);
  svg("path", { class: "av-hair", d: "M66 108 C62 62 92 44 122 46 C156 48 180 66 174 108 C170 92 160 78 146 74 C128 84 98 84 80 76 C72 86 68 96 66 108 Z" }, head);
  svg("ellipse", { class: "av-cheek", cx: "90", cy: "136", rx: "9", ry: "5" }, head);
  svg("ellipse", { class: "av-cheek", cx: "150", cy: "136", rx: "9", ry: "5" }, head);
  const brows = svg("g", { class: "av-brows" }, head);
  svg("path", { d: "M86 92 Q97 86 108 91" }, brows);
  svg("path", { d: "M132 91 Q143 86 154 92" }, brows);
  const eyes = svg("g", { class: "av-eyes" }, head);
  [97, 143].forEach((cx) => {
    svg("ellipse", { class: "av-eye-white", cx, cy: "108", rx: "9", ry: "7" }, eyes);
  });
  const irises = svg("g", { class: "av-irises" }, eyes);
  [97, 143].forEach((cx) => {
    svg("circle", { class: "av-iris", cx, cy: "108", r: "5" }, irises);
    svg("circle", { class: "av-glint", cx: cx + 1.8, cy: "106", r: "1.4" }, irises);
  });
  const lids = svg("g", { class: "av-lids" }, eyes);
  [97, 143].forEach((cx) => svg("rect", { class: "av-skin", x: cx - 10, y: "100", width: "20", height: "16" }, lids));
  svg("path", { class: "av-nose", d: "M120 112 Q116 126 120 130 Q123 131 125 129" }, head);
  const mouth = svg("path", { class: "av-mouth", d: MOUTH.rest }, head);
  return { root, mouth, irises, eyes };
}

/* ------------------------------------------------------------------ text made for speaking */

const RUPEES = { en: "rupees", hi: "रुपये", id: "rupee" };
function indianWords(n, lang) {
  // 230000 -> "2 lakh 30 thousand" (en), "2 लाख 30 हज़ार" (hi). Indonesian reads plain digits well.
  if (lang === "id") return String(n);
  const units = lang === "hi" ? [[1e7, "करोड़"], [1e5, "लाख"], [1e3, "हज़ार"]] : [[1e7, "crore"], [1e5, "lakh"], [1e3, "thousand"]];
  const parts = [];
  let rest = n;
  units.forEach(([size, word]) => {
    if (rest >= size) { parts.push(`${Math.floor(rest / size)} ${word}`); rest %= size; }
  });
  if (rest || !parts.length) parts.push(String(rest));
  return parts.join(" ");
}
function forSpeech(text, lang) {
  return text
    .replace(/₹\s?([\d,]+)(?:\.\d+)?/g, (_, num) => `${indianWords(Number(num.replace(/,/g, "")), lang)} ${RUPEES[lang]}`)
    .replace(/\b(EMI|KFS|UPI|PDF|APR|PAN|GST|NACH|IFSC|ID)\b/g, (m) => (m === "PAN" ? "pan" : m.split("").join(" ")))
    .replace(/(\d+)%/g, lang === "hi" ? "$1 प्रतिशत" : lang === "id" ? "$1 persen" : "$1 percent")
    .replace(/\s*·\s*/g, ", ");
}
function sentences(text) {
  return (text.match(/[^.!?।]+[.!?।]+["')\]]*\s*|[^.!?।]+$/g) || [text]).map((s) => s.trim()).filter(Boolean);
}

/* ------------------------------------------------------------------ the guide */

export function createGuide({ api, context, actions }) {
  const Recognition = window.SpeechRecognition || window.webkitSpeechRecognition;
  const synth = window.speechSynthesis || null;
  const g = {
    user: null, open: false, minimised: false, expanded: false, lang: "en", lastKey: null, busy: false,
    muted: store("guide-muted") === "1", captions: store("guide-captions") !== "0",
    convo: false, rec: null, camStream: null, micStream: null, audioCtx: null, startedAt: 0, speakToken: 0,
  };

  // ---- layout: a floating video-call window
  const launcher = el("button", "guide-launcher hidden");
  launcher.type = "button";
  const launcherFace = el("span", "guide-mini");
  launcherFace.appendChild(buildPresenter().root);
  const launcherText = el("span", "guide-launcher-text");
  launcher.append(launcherFace, launcherText);

  const call = el("section", "guide-call hidden");
  call.setAttribute("aria-label", `Video call with ${NAME}`);

  const top = el("div", "guide-top");
  const who = el("div", "guide-who");
  const whoName = el("b", null, NAME);
  const whoRole = el("span", "small muted");
  who.append(whoName, whoRole);
  const live = el("span", "guide-live");
  const liveDot = el("i");
  const clock = el("span", null, "0:00");
  live.append(liveDot, clock);
  const langPick = el("select", "guide-select");
  [["en", "English"], ["hi", "हिन्दी"], ["id", "Indonesia"]].forEach(([v, label]) => {
    const o = el("option", null, label); o.value = v; langPick.appendChild(o);
  });
  langPick.setAttribute("aria-label", "Language");
  const expandBtn = iconButton("expand", "Larger", "small");
  const minBtn = iconButton("minimise", "Minimise", "small");
  top.append(who, live, langPick, expandBtn, minBtn);

  const stage = el("div", "guide-stage");
  const presenter = buildPresenter();
  const presenterWrap = el("button", "guide-presenter");
  presenterWrap.type = "button";
  presenterWrap.appendChild(presenter.root);
  const stepChip = el("span", "guide-chip-step");
  const listenPill = el("span", "guide-listen hidden");
  const bars = [0, 1, 2, 3].map(() => el("i"));
  const listenText = el("span");
  listenPill.append(...bars, listenText);
  const selfTile = el("div", "guide-self hidden");
  const selfVideo = el("video");
  selfVideo.muted = true; selfVideo.autoplay = true; selfVideo.playsInline = true;
  const selfLabel = el("span");
  selfTile.append(selfVideo, selfLabel);
  const captionBar = el("div", "guide-captions");
  const captionNow = el("p");
  captionNow.setAttribute("aria-live", "polite");
  captionBar.appendChild(captionNow);
  const photo = el("video", "guide-photo hidden");
  photo.autoplay = true; photo.playsInline = true; photo.muted = true; // sound comes from its own audio element
  const aiBadge = el("span", "guide-ai-badge");
  const soundBtn = el("button", "guide-sound hidden");
  soundBtn.type = "button";
  stage.append(presenterWrap, photo, stepChip, listenPill, aiBadge, soundBtn, selfTile, captionBar);

  const progress = el("div", "guide-progress");
  const bar = el("i");
  progress.appendChild(bar);

  const controls = el("div", "guide-controls");
  const micBtn = iconButton("mic", "Talk hands-free");
  const camBtn = iconButton("cam", "Camera");
  const ccBtn = iconButton("cc", "Captions");
  const muteBtn = iconButton("speaker", "Mute voice");
  const endBtn = iconButton("end", "End call", "end");
  controls.append(micBtn, camBtn, ccBtn, muteBtn, endBtn);
  if (!Recognition) micBtn.classList.add("hidden");
  if (!navigator.mediaDevices?.getUserMedia) camBtn.classList.add("hidden");

  const body = el("div", "guide-body");
  const actionRow = el("div", "guide-actions");
  const transcript = el("div", "guide-transcript");
  const chips = el("div", "chips guide-chips");
  const askRow = el("div", "guide-ask");
  const input = el("input");
  input.maxLength = 500;
  const send = el("button", null);
  askRow.append(input, send);
  const voiceRow = el("label", "guide-voice small muted");
  const voiceLabel = el("span");
  const voicePick = el("select", "guide-select");
  voiceRow.append(voiceLabel, voicePick);
  const note = el("p", "small muted guide-note");
  body.append(actionRow, transcript, chips, askRow, voiceRow, note);

  call.append(top, stage, progress, controls, body);
  document.body.append(launcher, call);

  /* ---------------------------------------------------------------- presenter life */

  function setViseme(name) { presenter.mouth.setAttribute("d", MOUTH[name] || MOUTH.rest); }
  function setMood(mood) { // idle | talking | listening | thinking
    stage.dataset.mood = mood;
    launcherFace.dataset.mood = mood;
  }
  setMood("idle");

  // Blinks and glances at human-ish, irregular intervals.
  let lifeTimers = [];
  function startLife() {
    stopLife();
    const blink = () => {
      stage.classList.add("blink");
      setTimeout(() => stage.classList.remove("blink"), 140);
      lifeTimers[0] = setTimeout(blink, 2200 + Math.random() * 3800);
    };
    const glance = () => {
      const listening = stage.dataset.mood === "listening";
      const x = listening ? 0 : (Math.random() - 0.5) * 5;
      const y = stage.dataset.mood === "thinking" ? -3 : (Math.random() - 0.5) * 2.5;
      presenter.irises.setAttribute("transform", `translate(${x.toFixed(1)} ${y.toFixed(1)})`);
      lifeTimers[1] = setTimeout(glance, 1200 + Math.random() * 2600);
    };
    lifeTimers = [setTimeout(blink, 1500), setTimeout(glance, 900)];
  }
  function stopLife() { lifeTimers.forEach(clearTimeout); lifeTimers = []; }

  // Lip sync: a timeline of mouth shapes estimated from the text, re-anchored to every word boundary
  // the voice reports, so it stays in step whether or not the voice sends boundary events.
  const lips = { frame: 0, timeline: [], t0: 0, idx: 0 };
  function lipsStart(text, rate) {
    const msPerChar = 1000 / (14.5 * rate);
    let t = 0;
    lips.timeline = Array.from(text).map((ch) => {
      const item = { at: t, v: visemeOf(ch) };
      t += /\s/.test(ch) ? msPerChar * 0.7 : msPerChar;
      return item;
    });
    lips.t0 = performance.now();
    lips.idx = 0;
    cancelAnimationFrame(lips.frame);
    const tick = (now) => {
      const elapsed = now - lips.t0;
      const tl = lips.timeline;
      while (lips.idx < tl.length - 1 && tl[lips.idx + 1].at <= elapsed) lips.idx += 1;
      // Past the estimate: hold a soft half-open mouth until the voice says it has finished.
      setViseme(elapsed > (tl[tl.length - 1]?.at || 0) + 150 ? "small" : tl[lips.idx]?.v || "rest");
      lips.frame = requestAnimationFrame(tick);
    };
    lips.frame = requestAnimationFrame(tick);
  }
  function lipsSync(charIndex) {
    const item = lips.timeline[charIndex];
    if (item) { lips.t0 = performance.now() - item.at; lips.idx = Math.min(charIndex, lips.idx); }
  }
  function lipsStop() { cancelAnimationFrame(lips.frame); setViseme("rest"); }

  /* ---------------------------------------------------------------- voice */

  // Chrome keeps reading queued speech across a reload, which would talk over the new page:
  // start silent, and fall silent when the page goes away.
  if (synth) {
    synth.cancel();
    window.addEventListener("pagehide", () => synth.cancel());
  }

  let voices = [];
  function loadVoices() { voices = synth ? synth.getVoices() : []; fillVoicePicker(); }
  if (synth) { loadVoices(); synth.addEventListener?.("voiceschanged", loadVoices); }

  function rankVoices(lang) {
    const wanted = VOICE_FALLBACKS[lang];
    return voices
      .map((v) => {
        const tag = v.lang.replace("_", "-");
        let score = -1;
        const exact = wanted.indexOf(tag);
        if (exact >= 0) score = 100 - exact * 12;
        else if (tag.toLowerCase().startsWith(lang)) score = 40;
        if (score < 0 || NOVELTY.test(v.name)) return null;
        if (/premium|enhanced|natural|neural/i.test(v.name)) score += 30;
        if (/google|microsoft.*online|siri/i.test(v.name)) score += 18;
        if (v.default) score += 2;
        return { v, score };
      })
      .filter(Boolean)
      .sort((a, b) => b.score - a.score)
      .map((x) => x.v);
  }
  function chosenVoice() {
    const ranked = rankVoices(g.lang);
    const saved = store(`guide-voice-${g.lang}`);
    return ranked.find((v) => v.voiceURI === saved) || ranked[0] || null;
  }
  function fillVoicePicker() {
    const ranked = rankVoices(g.lang).slice(0, 12);
    const saved = store(`guide-voice-${g.lang}`) || "";
    const auto = el("option", null, `${UI[g.lang].auto}${ranked[0] ? ` (${ranked[0].name})` : ""}`);
    auto.value = "";
    voicePick.replaceChildren(auto, ...ranked.map((v) => {
      const o = el("option", null, `${v.name} · ${v.lang}`);
      o.value = v.voiceURI;
      return o;
    }));
    voicePick.value = ranked.some((v) => v.voiceURI === saved) ? saved : "";
    voiceRow.classList.toggle("hidden", !synth || !ranked.length);
  }

  /* ---------------------------------------------------------------- photoreal presenter (LiveAvatar) */

  // When the server has a LiveAvatar key, the face and voice come from HeyGen's stream and the browser
  // voice stays silent. The avatar is in restricted mode: it says only the text we send it.
  const avatar = { room: null, session: null, pending: null, starting: null };
  let liveAvatarOn = null;
  async function liveAvatarAvailable() {
    if (liveAvatarOn === null) {
      try { liveAvatarOn = Boolean((await api("/api/config")).live_avatar); } catch { liveAvatarOn = false; }
    }
    return liveAvatarOn;
  }
  function loadLiveKit() {
    if (window.LivekitClient) return Promise.resolve(window.LivekitClient);
    return new Promise((resolve, reject) => {
      const script = document.createElement("script");
      script.src = `/static/vendor/livekit-client-2.22.3.umd.js`;
      script.onload = () => resolve(window.LivekitClient);
      script.onerror = reject;
      document.head.appendChild(script);
    });
  }
  // Browsers only let a page start sound inside a click or key press. The room connects after a network
  // round trip, by which time the opening click no longer counts, so every later gesture retries.
  function unlockAvatarAudio() {
    if (!avatar.room) return;
    avatar.room.startAudio().catch(() => {});
    stage.querySelectorAll(".guide-room-audio").forEach((player) => player.play().catch(() => {}));
  }
  function showSoundPrompt() {
    soundBtn.textContent = UI[g.lang].sound;
    soundBtn.classList.toggle("hidden", !avatar.room || avatar.room.canPlaybackAudio);
  }
  call.addEventListener("pointerdown", unlockAvatarAudio, true);
  call.addEventListener("keydown", unlockAvatarAudio, true);
  soundBtn.addEventListener("click", () => { unlockAvatarAudio(); setTimeout(showSoundPrompt, 300); });

  function sendAvatar(eventType, extra = {}) {
    if (!avatar.room || !avatar.session) return;
    const event = { event_id: crypto.randomUUID(), event_type: eventType, session_id: avatar.session.session_id, ...extra };
    avatar.room.localParticipant.publishData(new TextEncoder().encode(JSON.stringify(event)),
      { reliable: true, topic: "agent-control" });
  }
  async function startAvatar() {
    if (avatar.room || avatar.starting || !(await liveAvatarAvailable())) return avatar.starting;
    avatar.starting = (async () => {
      captionNow.textContent = UI[g.lang].connecting;
      try {
        const [LK, session] = await Promise.all([loadLiveKit(),
          api("/api/guide/avatar", { method: "POST", body: JSON.stringify({ language: g.lang }) })]);
        const room = new LK.Room({ adaptiveStream: true });
        room.on(LK.RoomEvent.AudioPlaybackStatusChanged, showSoundPrompt);
        room.on(LK.RoomEvent.TrackSubscribed, (track) => {
          if (track.kind === "video") { track.attach(photo); photo.classList.remove("hidden"); stage.classList.add("photoreal"); }
          // The room carries two audio tracks (the avatar's voice and a silent control agent). Each gets its
          // own player: sharing one element let the silent track replace the voice.
          if (track.kind === "audio") { const player = track.attach(); player.className = "guide-room-audio"; stage.appendChild(player); }
        });
        room.on(LK.RoomEvent.TrackUnsubscribed, (track) => { track.detach().forEach((node) => node.remove()); });
        room.on(LK.RoomEvent.DataReceived, (payload, _participant, _kind, topic) => {
          if (topic !== "agent-response") return;
          let event = {};
          try { event = JSON.parse(new TextDecoder().decode(payload)); } catch { return; }
          if (event.event_type === "avatar.speak_started") setMood("talking");
          if (event.event_type === "avatar.speak_ended") { setMood("idle"); avatar.pending?.(); avatar.pending = null; }
          if (event.event_type === "session.stopped") endAvatar(true);
        });
        room.on(LK.RoomEvent.Disconnected, () => endAvatar(true));
        await room.connect(session.livekit_url, session.livekit_token);
        avatar.room = room;
        avatar.session = session;
        window.__ledgerlensAvatarRoom = room; // inspection hook for debugging audio; holds no secrets
        unlockAvatarAudio();
        showSoundPrompt();
        aiBadge.textContent = `${UI[g.lang].aiBadge}${session.sandbox ? " · sandbox" : ""}`;
      } catch {
        avatar.room = null;
        avatar.session = null;
      } finally {
        avatar.starting = null;
        if (captionNow.textContent === UI[g.lang].connecting) captionNow.textContent = "";
      }
    })();
    return avatar.starting;
  }
  function endAvatar(byProvider = false) {
    const { room, session } = avatar;
    if (!room && !session) return;
    avatar.room = null;
    avatar.session = null;
    avatar.pending?.();
    avatar.pending = null;
    photo.classList.add("hidden");
    stage.classList.remove("photoreal");
    soundBtn.classList.add("hidden");
    window.__ledgerlensAvatarRoom = null;
    aiBadge.textContent = UI[g.lang].aiBadge;
    try { room?.disconnect(); } catch { /* already closed */ }
    stage.querySelectorAll(".guide-room-audio").forEach((player) => player.remove());
    if (session) {
      api("/api/guide/avatar/stop", { method: "POST",
        body: JSON.stringify({ session_id: session.session_id, session_token: session.session_token }) }).catch(() => {});
    }
    if (byProvider && g.open) note.textContent = UI[g.lang].avatarEnded;
  }

  function stopSpeaking() {
    g.speakToken += 1;
    if (avatar.room) { sendAvatar("avatar.interrupt"); avatar.pending?.(); avatar.pending = null; }
    if (synth) synth.cancel();
    lipsStop();
    if (stage.dataset.mood === "talking") setMood("idle");
  }

  // Speak a reply sentence by sentence. Resolves when it has finished (or was interrupted).
  function say(text) {
    stopSpeaking();
    const token = g.speakToken;
    const parts = sentences(text);
    addTranscript("bot", text);
    if (avatar.room && !g.muted) {
      captionNow.textContent = text;
      sendAvatar("avatar.speak_text", { text });
      // Resolve on the avatar's own "finished" event, with a ceiling in case that event never comes.
      return new Promise((resolve) => {
        const ceiling = setTimeout(resolve, 4000 + text.length * 90);
        avatar.pending = () => { clearTimeout(ceiling); resolve(); };
      });
    }
    if (g.muted || !synth || !parts.length) {
      captionNow.textContent = text;
      // Give a reader time before listening again in hands-free mode.
      return new Promise((resolve) => setTimeout(resolve, Math.min(400 + text.length * 35, 5000)));
    }
    const voice = chosenVoice();
    const rate = g.lang === "en" ? 0.97 : 0.93;
    return new Promise((resolve) => {
      parts.forEach((part, i) => {
        const spoken = forSpeech(part, g.lang);
        const u = new SpeechSynthesisUtterance(spoken);
        u.lang = voice?.lang || SPEECH_LANG[g.lang];
        if (voice) u.voice = voice;
        u.rate = rate;
        u.pitch = 1;
        u.onstart = () => {
          if (token !== g.speakToken) return;
          captionNow.textContent = part;
          setMood("talking");
          lipsStart(spoken, rate);
        };
        u.onboundary = (e) => { if (token === g.speakToken && e.name !== "sentence") lipsSync(e.charIndex); };
        const finish = () => {
          if (token !== g.speakToken) { resolve(); return; }
          lipsStop();
          if (i === parts.length - 1) { setMood("idle"); resolve(); }
        };
        u.onend = finish;
        u.onerror = finish;
        synth.speak(u);
      });
    });
  }

  /* ---------------------------------------------------------------- listening */

  function setListening(on) {
    listenPill.classList.toggle("hidden", !on);
    if (on) setMood("listening"); else if (stage.dataset.mood === "listening") setMood("idle");
  }

  let quietRetries = 0;
  function listen() {
    if (!Recognition || g.rec || !g.convo || g.busy) return;
    const rec = new Recognition();
    g.rec = rec;
    rec.lang = SPEECH_LANG[g.lang];
    rec.interimResults = true;
    rec.continuous = false;
    let heard = "";
    let final = false;
    rec.onresult = (event) => {
      heard = Array.from(event.results).map((r) => r[0].transcript).join(" ").trim();
      captionNow.textContent = `${UI[g.lang].you}: ${heard}`;
      final = event.results[event.results.length - 1].isFinal;
    };
    rec.onerror = (event) => {
      if (event.error === "not-allowed" || event.error === "service-not-allowed" || event.error === "audio-capture") {
        setConvo(false);
        note.textContent = UI[g.lang].noMic;
      }
    };
    rec.onend = () => {
      g.rec = null;
      setListening(false);
      if (final && heard) { quietRetries = 0; ask(heard); return; }
      // Nobody spoke: keep the line open a few times, then wait for the customer to tap the mic.
      if (g.convo && quietRetries < 3) { quietRetries += 1; setTimeout(listen, 250); }
      else if (g.convo) setConvo(false);
    };
    try { rec.start(); setListening(true); } catch { g.rec = null; }
  }

  async function setConvo(on) {
    g.convo = on;
    micBtn.setAttribute("aria-pressed", String(on));
    micBtn.classList.toggle("on", on);
    micBtn.title = on ? UI[g.lang].micOff : UI[g.lang].mic;
    micBtn.setAttribute("aria-label", micBtn.title);
    if (on) {
      quietRetries = 0;
      stopSpeaking();
      startMeter();
      listen();
    } else {
      if (g.rec) { try { g.rec.abort(); } catch { /* already stopped */ } g.rec = null; }
      setListening(false);
      stopMeter();
    }
  }

  // A live level meter for the listening pill, from a separate audio stream that never leaves the page.
  async function startMeter() {
    if (g.micStream || !navigator.mediaDevices?.getUserMedia) return;
    try {
      g.micStream = await navigator.mediaDevices.getUserMedia({ audio: true });
      const Ctx = window.AudioContext || window.webkitAudioContext;
      g.audioCtx = new Ctx();
      const analyser = g.audioCtx.createAnalyser();
      analyser.fftSize = 256;
      g.audioCtx.createMediaStreamSource(g.micStream).connect(analyser);
      const data = new Uint8Array(analyser.frequencyBinCount);
      const draw = () => {
        if (!g.micStream) return;
        analyser.getByteFrequencyData(data);
        bars.forEach((b, i) => {
          const slice = data.slice(4 + i * 10, 14 + i * 10);
          const level = slice.reduce((s, v) => s + v, 0) / slice.length / 255;
          b.style.transform = `scaleY(${(0.25 + level * 1.6).toFixed(2)})`;
        });
        requestAnimationFrame(draw);
      };
      listenPill.classList.add("metered");
      draw();
    } catch { listenPill.classList.remove("metered"); }
  }
  function stopMeter() {
    if (g.micStream) g.micStream.getTracks().forEach((t) => t.stop());
    g.micStream = null;
    if (g.audioCtx) g.audioCtx.close().catch(() => {});
    g.audioCtx = null;
    listenPill.classList.remove("metered");
  }

  /* ---------------------------------------------------------------- camera (preview only) */

  async function toggleCamera() {
    if (g.camStream) { stopCamera(); return; }
    try {
      g.camStream = await navigator.mediaDevices.getUserMedia({ video: { facingMode: "user" }, audio: false });
      selfVideo.srcObject = g.camStream;
      selfTile.classList.remove("hidden");
      camBtn.classList.add("on");
      camBtn.setAttribute("aria-pressed", "true");
      note.textContent = UI[g.lang].camNote;
    } catch { note.textContent = UI[g.lang].noCam; }
  }
  function stopCamera() {
    if (g.camStream) g.camStream.getTracks().forEach((t) => t.stop());
    g.camStream = null;
    selfVideo.srcObject = null;
    selfTile.classList.add("hidden");
    camBtn.classList.remove("on");
    camBtn.setAttribute("aria-pressed", "false");
  }

  /* ---------------------------------------------------------------- conversation */

  function addTranscript(who, text) {
    const line = el("p", `guide-line ${who}`);
    line.append(el("b", null, who === "bot" ? NAME : UI[g.lang].you), document.createTextNode(` ${text}`));
    transcript.appendChild(line);
    while (transcript.children.length > 12) transcript.removeChild(transcript.firstChild);
    transcript.scrollTop = transcript.scrollHeight;
  }

  function renderJourney(result) {
    const j = result.journey;
    bar.style.width = `${j.progress}%`;
    stepChip.textContent = `${UI[g.lang].step} ${j.step_number} ${UI[g.lang].of} ${j.total_steps}`;
    stepChip.classList.remove("hidden");
    actionRow.replaceChildren(...result.actions.map((a) => {
      const button = el("button", a.id === "callback" ? "ghost" : "stamp", a.label);
      button.addEventListener("click", async () => {
        button.disabled = true;
        try { await actions[a.id]?.(); } finally { button.disabled = false; }
      });
      return button;
    }));
  }

  async function ask(message = "") {
    if (g.busy) return;
    g.busy = true;
    stopSpeaking();
    input.value = "";
    if (message) { addTranscript("me", message); setMood("thinking"); captionNow.textContent = UI[g.lang].thinking; }
    let reply = null;
    try {
      const { caseId } = context();
      reply = await api("/api/guide", { method: "POST", body: JSON.stringify({ message, case_id: caseId, language: g.lang }) });
      renderJourney(reply);
    } catch (error) {
      reply = { reply: error.message };
    } finally { g.busy = false; }
    if (stage.dataset.mood === "thinking") setMood("idle");
    await say(reply.reply);
    if (g.convo && g.open) listen();
  }

  // Greet again only when the customer's step actually changes, not on every repaint.
  async function refresh() {
    if (!g.open || !g.user) return;
    const { caseId, screen } = context();
    if (screen === "list") {
      if (g.lastKey !== "list") {
        g.lastKey = "list";
        bar.style.width = "0%";
        stepChip.classList.add("hidden");
        actionRow.replaceChildren();
        await say(UI[g.lang].list);
        if (g.convo) listen();
      }
      return;
    }
    let key = "details";
    if (caseId) {
      try { key = `${caseId}:${(await api(`/api/cases/${caseId}/journey`)).step}`; } catch { return; }
    }
    if (key !== g.lastKey) { g.lastKey = key; ask(""); }
  }

  /* ---------------------------------------------------------------- window states */

  let clockTimer = 0;
  function tickClock() {
    const s = Math.floor((Date.now() - g.startedAt) / 1000);
    clock.textContent = `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
  }
  function openCall() {
    g.open = true;
    g.minimised = false;
    g.startedAt = g.startedAt || Date.now();
    clearInterval(clockTimer);
    clockTimer = setInterval(tickClock, 1000);
    tickClock();
    call.classList.remove("hidden");
    launcher.classList.add("hidden");
    startLife();
    g.lastKey = null;
    // Connect the photoreal presenter first (when configured), so the greeting comes from one voice.
    Promise.resolve(startAvatar()).finally(refresh);
  }
  function minimise() {
    g.minimised = true;
    call.classList.add("hidden");
    relabel();
    launcher.classList.remove("hidden");
  }
  function endCall() {
    g.open = false;
    g.minimised = false;
    g.startedAt = 0;
    clearInterval(clockTimer);
    stopSpeaking();
    endAvatar();
    setConvo(false);
    stopCamera();
    stopLife();
    call.classList.add("hidden");
    transcript.replaceChildren();
    relabel();
    launcher.classList.toggle("hidden", !g.user);
  }

  function relabel() {
    const t = UI[g.lang];
    launcherText.textContent = g.minimised ? `${NAME} · ${t.onCall}` : t.start;
    launcher.classList.toggle("on-call", g.minimised);
    whoRole.textContent = t.role;
    if (!avatar.session) aiBadge.textContent = t.aiBadge;
    input.placeholder = t.placeholder;
    send.textContent = t.send;
    listenText.textContent = t.listening;
    voiceLabel.textContent = t.voice;
    selfLabel.textContent = t.you;
    presenterWrap.setAttribute("aria-label", t.interrupt);
    presenterWrap.title = t.interrupt;
    [[micBtn, g.convo ? t.micOff : t.mic], [camBtn, t.cam], [ccBtn, t.cc], [muteBtn, g.muted ? t.unmute : t.mute],
     [endBtn, t.end], [expandBtn, g.expanded ? t.shrink : t.expand], [minBtn, t.minimise]].forEach(([b, label]) => {
      b.title = label; b.setAttribute("aria-label", label);
    });
    muteBtn.classList.toggle("on", g.muted);
    muteBtn.setAttribute("aria-pressed", String(g.muted));
    ccBtn.classList.toggle("on", g.captions);
    ccBtn.setAttribute("aria-pressed", String(g.captions));
    captionBar.classList.toggle("hidden", !g.captions);
    call.classList.toggle("expanded", g.expanded);
    langPick.value = g.lang;
    chips.replaceChildren(...t.chips.map((q) => {
      const chip = el("button", null, q);
      chip.addEventListener("click", () => ask(q));
      return chip;
    }));
    fillVoicePicker();
  }

  /* ---------------------------------------------------------------- wiring */

  launcher.addEventListener("click", openCall);
  minBtn.addEventListener("click", minimise);
  endBtn.addEventListener("click", endCall);
  expandBtn.addEventListener("click", () => { g.expanded = !g.expanded; relabel(); });
  presenterWrap.addEventListener("click", () => { stopSpeaking(); if (g.convo) listen(); });
  photo.addEventListener("click", () => { stopSpeaking(); if (g.convo) listen(); });
  micBtn.addEventListener("click", () => setConvo(!g.convo));
  camBtn.addEventListener("click", toggleCamera);
  ccBtn.addEventListener("click", () => { g.captions = !g.captions; store("guide-captions", g.captions ? "1" : "0"); relabel(); });
  muteBtn.addEventListener("click", () => {
    g.muted = !g.muted;
    store("guide-muted", g.muted ? "1" : "0");
    if (g.muted) stopSpeaking();
    relabel();
  });
  send.addEventListener("click", () => input.value.trim() && ask(input.value.trim()));
  input.addEventListener("keydown", (e) => { if (e.key === "Enter" && input.value.trim()) ask(input.value.trim()); });
  langPick.addEventListener("change", () => {
    g.lang = langPick.value;
    if (g.rec) { try { g.rec.abort(); } catch { /* stopped */ } }
    relabel();
    g.lastKey = null;
    // The avatar's voice language is fixed per session, so a new language needs a new session.
    const hadAvatar = Boolean(avatar.room);
    endAvatar();
    Promise.resolve(hadAvatar ? startAvatar() : null).finally(refresh);
  });
  voicePick.addEventListener("change", () => {
    store(`guide-voice-${g.lang}`, voicePick.value);
    const sample = { en: "Hello, this is how I sound.", hi: "नमस्ते, मेरी आवाज़ ऐसी है।", id: "Halo, beginilah suara saya." }[g.lang];
    say(sample);
  });
  document.addEventListener("keydown", (e) => { if (e.key === "Escape" && g.open && !g.minimised) minimise(); });
  relabel();

  return {
    setUser(user, lang) {
      g.user = user && (user.role === "borrower" || user.role === "loan_officer") ? user : null;
      if (lang && UI[lang]) g.lang = lang;
      if (!g.user) endCall();
      relabel();
      launcher.classList.toggle("hidden", !g.user || (g.open && !g.minimised));
    },
    onView() { refresh(); },
    onUploadError(message) {
      if (!g.open) return;
      // Known problems get the guide's fuller help; anything else is read out as the server wrote it.
      const topic = /password/i.test(message) ? "My statement has a password" : /scan|photograph/i.test(message)
        ? "Can I upload a photo of my statement?" : "";
      if (topic) ask(topic); else say(message);
    },
  };
}
