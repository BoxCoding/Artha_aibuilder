/* The video guide: a video call with Sahayika or Saarthi, who walks a customer through their application.

   - Voice: the best installed speech-synthesis voice for the language and the presenter, read sentence
     by sentence, with rupee amounts and acronyms rewritten for speech.
   - Face: a photo animated by face.js (mouth, blinks, glances, nods, expressions); without WebGL the
     still photo with a speaking ring. The presenter is the other gender from the person's optional,
     self-declared profile (female if not stated), and can be switched in the call.
   - Conversation: hands-free with speech recognition where the browser has it. Camera and microphone
     stay on the device; only the recognised text of a question is sent.
   - Replies come from POST /api/guide. Buttons only take the customer where they need to go; the guide
     never submits or accepts anything for them. */

const version = new URL(import.meta.url).search;
const { el, button, MIC_BLOCKED, Recognition, SPEECH_LANG, questionPicker, recogniser } = await import(`./ui.js${version}`);
let createFace = null;
try { ({ createFace } = await import(`./face.js${version}`)); } catch { /* still photo */ }

const VOICE_FALLBACKS = { en: ["en-IN", "en-GB", "en-AU", "en-US"], hi: ["hi-IN"], id: ["id-ID"] };
// macOS ships these as joke or effect voices; they must never be picked automatically.
const HI_FEMININE = [["रहा हूँ", "रही हूँ"], ["सकता हूँ", "सकती हूँ"], ["देता हूँ", "देती हूँ"], ["पूछूँगा", "पूछूँगी"],
  ["करूँगा", "करूँगी"], ["बताऊँगा", "बताऊँगी"], ["पाया।", "पाई।"]];
const NOVELTY = /\b(albert|bad news|bahh|bells|boing|bubbles|cellos|good news|jester|organ|superstar|trinoids|whisper|wobble|zarvox|fred|junior|ralph|kathy|grandma|grandpa|rocko|shelley|eddy|flo|reed|sandy)\b/i;
// The two presenters: portrait photos served from /static/guide/.
const PRESENTERS = {
  female: { name: "Sahayika", hi: "सहायिका", src: "/static/guide/presenter-female.webp" },
  male: { name: "Saarthi", hi: "सारथी", src: "/static/guide/presenter-male.webp" },
};
// Installed voices whose names say which voice they are. Unknown voices score neutral.
const FEMALE_VOICES = /\b(female|woman|samantha|karen|moira|tessa|victoria|veena|lekha|kalpana|swara|heera|neerja|aditi|raveena|kajal|ananya|sangeeta|priya|isha|zira|susan|hazel|serena|fiona|damayanti|gadis|kiara|google हिन्दी|google bahasa)\b/i;
const MALE_VOICES = /\b(male|man|rishi|ravi|hemant|madhur|prabhat|aarav|kunal|daniel|alex|oliver|arthur|aaron|gordon|david|mark|george|guy|ardi|thomas|tom|lee)\b/i;

// The form interview: the presenter asks for each empty field in turn.
const FORM_ORDER = ["full_name", "mobile", "pan", "business_name", "industry", "business_start_year", "address_line",
  "city", "state", "pincode", "requested_amount", "tenure_months"];
const FORM_Q = {
  en: { full_name: "What is your full name, as on your PAN card?", mobile: "What is your mobile number?",
        pan: "Please say your PAN, letter by letter.", business_name: "What is the name of your business?",
        industry: "What kind of business is it? For example a shop, a restaurant, transport or manufacturing.",
        business_start_year: "Which year did the business start?",
        address_line: "What is the business address? Just the street and area.", city: "Which city?",
        state: "Which state?", pincode: "And the PIN code?",
        requested_amount: "How much would you like to borrow? You can say, for example, three lakh.",
        tenure_months: "Over how many months would you like to repay: 12, 18, 24 or 36?" },
  hi: { full_name: "आपका पूरा नाम क्या है, जैसा PAN कार्ड पर है?", mobile: "आपका मोबाइल नंबर क्या है?",
        pan: "कृपया अपना PAN एक-एक अक्षर बोलें।", business_name: "आपके व्यवसाय का नाम क्या है?",
        industry: "आपका व्यवसाय किस तरह का है? जैसे दुकान, रेस्टोरेंट, ट्रांसपोर्ट या मैन्युफैक्चरिंग।",
        business_start_year: "व्यवसाय किस साल शुरू हुआ?", address_line: "व्यवसाय का पता क्या है? सिर्फ़ गली और इलाका।",
        city: "कौन सा शहर?", state: "कौन सा राज्य?", pincode: "और पिन कोड?",
        requested_amount: "आपको कितना लोन चाहिए? जैसे, तीन लाख।",
        tenure_months: "कितने महीनों में चुकाना चाहेंगे: 12, 18, 24 या 36?" },
  id: { full_name: "Siapa nama lengkap Anda, sesuai kartu PAN?", mobile: "Berapa nomor ponsel Anda?",
        pan: "Sebutkan PAN Anda, huruf demi huruf.", business_name: "Apa nama usaha Anda?",
        industry: "Usaha Anda bergerak di bidang apa? Misalnya toko, restoran, transportasi, atau manufaktur.",
        business_start_year: "Tahun berapa usaha dimulai?", address_line: "Apa alamat usaha Anda? Cukup jalan dan daerahnya.",
        city: "Kota apa?", state: "Negara bagian apa?", pincode: "Dan kode PIN-nya?",
        requested_amount: "Berapa pinjaman yang Anda butuhkan? Misalnya, tiga lakh.",
        tenure_months: "Dalam berapa bulan ingin melunasi: 12, 18, 24, atau 36?" },
};
const FORM_TEXT = {
  en: { fill: "🎙 Fill the form with me", stop: "■ Stop filling",
        start: "Let's fill the form together. I'll ask one thing at a time. Say skip to leave one, back to change the last answer, or stop to finish. You'll check everything before it's saved.",
        allFilled: "Your details are already filled in.", gotIt: "Got it: {value}.", notCaught: "Sorry, I didn't catch that.",
        leaveIt: "I'll leave that one for you to type.",
        done: "That's everything I need. Please read the consent text and tick the box yourself, then press Save and continue. I'll show you a summary to check before anything is saved.",
        stopped: "Okay, I've stopped. You can carry on typing, or ask me to fill the form with you again.",
        notHere: "Open New application first, then I can help you fill the form.",
        typeInstead: "The microphone isn't available, so please type your answers in the box below.",
        months: "months" },
  hi: { fill: "🎙 मेरे साथ फ़ॉर्म भरें", stop: "■ भरना बंद करें",
        start: "चलिए साथ में फ़ॉर्म भरते हैं। मैं एक-एक करके पूछूँगा। किसी को छोड़ने के लिए छोड़ो, पिछला जवाब बदलने के लिए पीछे, और खत्म करने के लिए बस बोलें। सेव से पहले आप सब कुछ जाँचेंगे।",
        allFilled: "आपकी जानकारी पहले से भरी हुई है।", gotIt: "ठीक है: {value}।", notCaught: "माफ़ कीजिए, मैं समझ नहीं पाया।",
        leaveIt: "इसे मैं आपके लिखने के लिए छोड़ देता हूँ।",
        done: "मुझे बस इतना ही चाहिए था। कृपया सहमति वाला हिस्सा पढ़कर बॉक्स पर खुद टिक करें, फिर 'सेव करें और आगे बढ़ें' दबाएँ। सेव से पहले मैं आपको जाँचने के लिए पूरा सारांश दिखाऊँगा।",
        stopped: "ठीक है, मैंने रोक दिया। आप लिखना जारी रख सकते हैं, या फिर से मेरे साथ फ़ॉर्म भरने को कह सकते हैं।",
        notHere: "पहले 'नया आवेदन' खोलें, फिर मैं फ़ॉर्म भरने में मदद कर सकता हूँ।",
        typeInstead: "माइक्रोफ़ोन उपलब्ध नहीं है, कृपया नीचे बॉक्स में जवाब लिखें।", months: "महीने" },
  id: { fill: "🎙 Isi formulir bersama saya", stop: "■ Berhenti mengisi",
        start: "Mari isi formulir bersama. Saya akan bertanya satu per satu. Ucapkan lewati untuk melewatkan, kembali untuk mengubah jawaban terakhir, atau berhenti untuk selesai. Anda akan memeriksa semuanya sebelum disimpan.",
        allFilled: "Data Anda sudah terisi.", gotIt: "Baik: {value}.", notCaught: "Maaf, saya kurang menangkapnya.",
        leaveIt: "Yang ini saya biarkan untuk Anda ketik.",
        done: "Itu saja yang saya perlukan. Silakan baca teks persetujuan dan centang kotaknya sendiri, lalu tekan Simpan dan lanjutkan. Saya akan menampilkan ringkasan untuk diperiksa sebelum disimpan.",
        stopped: "Baik, saya berhenti. Anda bisa lanjut mengetik, atau minta saya mengisi formulir lagi.",
        notHere: "Buka Pengajuan baru dulu, lalu saya bisa membantu mengisi formulir.",
        typeInstead: "Mikrofon tidak tersedia, silakan ketik jawaban Anda di kotak di bawah.", months: "bulan" },
};
const FORM_WORDS = {
  start: ["fill the form", "fill my form", "help me fill", "fill it with me", "fill with me", "form bhar", "फ़ॉर्म भर",
          "फॉर्म भर", "isi formulir", "bantu isi"],
  stop: ["stop", "cancel", "finish", "enough", "रुको", "बंद", "बस", "berhenti", "selesai", "batal"],
  skip: ["skip", "next", "later", "don't know", "dont know", "छोड़", "छोड़ो", "आगे", "बाद में", "lewati", "nanti"],
  back: ["back", "previous", "change", "wrong", "पीछे", "गलत", "बदल", "kembali", "salah", "ubah"],
  repeat: ["repeat", "again", "say that again", "दोबारा", "फिर से", "ulangi", "lagi"],
};

const UI = {
  en: { role: "Your application guide", start: "Video call with {name}", onCall: "On call · tap to open",
        placeholder: "Type a question", send: "Ask", step: "Step", of: "of", listening: "Listening…", thinking: "Thinking…",
        you: "You", mic: "Talk hands-free", micOff: "Stop listening", cam: "Camera", cc: "Captions", mute: "Mute voice",
        unmute: "Unmute voice", end: "End call", expand: "Larger", shrink: "Smaller", minimise: "Minimise",
        voice: "Voice", auto: "Best available", switchTo: "Switch presenter", camNote: "Your camera is shown only to you.", interrupt: "Tap me to interrupt",
        aiBadge: "AI presenter", connecting: "Connecting the video presenter…", sound: "🔊 Tap to turn on sound",
        avatarSilent: "The video presenter didn't respond, so I answered with this device's voice.",
        topics: "Ask about", more: "More questions", moreHint: "Everything I can answer for your application right now",
        avatarEnded: "The video presenter has ended. I'll carry on from here.",
        noMic: "The microphone is blocked here. You can type your questions instead.",
        noCam: "The camera could not be opened here. Talking and typing still work.",
        chips: ["What do I do next?", "Which documents do you need?", "My statement has a password", "How long will it take?", "I want to talk to a person"] },
  hi: { role: "आपका आवेदन गाइड", start: "{name} से वीडियो कॉल", onCall: "कॉल पर · खोलने के लिए दबाएँ",
        placeholder: "अपना सवाल लिखें", send: "पूछें", step: "चरण", of: "में से", listening: "सुन रहा हूँ…", thinking: "सोच रहा हूँ…",
        you: "आप", mic: "बिना हाथ लगाए बात करें", micOff: "सुनना बंद करें", cam: "कैमरा", cc: "कैप्शन", mute: "आवाज़ बंद",
        unmute: "आवाज़ चालू", end: "कॉल समाप्त", expand: "बड़ा करें", shrink: "छोटा करें", minimise: "छोटा करके रखें",
        voice: "आवाज़", auto: "सबसे अच्छी उपलब्ध", switchTo: "प्रस्तुतकर्ता बदलें", aiBadge: "AI प्रस्तुतकर्ता", sound: "🔊 आवाज़ चालू करने के लिए दबाएँ",
        avatarSilent: "वीडियो प्रस्तुतकर्ता ने जवाब नहीं दिया, इसलिए मैंने इस डिवाइस की आवाज़ में जवाब दिया।",
        topics: "किस बारे में पूछें", more: "और सवाल", moreHint: "आपके आवेदन के लिए अभी मैं इन सबका जवाब दे सकता हूँ",
        connecting: "वीडियो प्रस्तुतकर्ता जुड़ रहा है…", avatarEnded: "वीडियो प्रस्तुतकर्ता का सत्र खत्म हो गया। मैं यहीं से आगे मदद करूँगा।", camNote: "आपका कैमरा सिर्फ़ आपको दिखता है।", interrupt: "रोकने के लिए मुझे दबाएँ",
        noMic: "यहाँ माइक्रोफ़ोन बंद है। आप अपना सवाल लिख सकते हैं।",
        noCam: "यहाँ कैमरा नहीं खुल सका। बात करना और लिखना अब भी काम करता है।",
        chips: ["अब मुझे क्या करना है?", "कौन से दस्तावेज़ चाहिए?", "मेरे स्टेटमेंट पर पासवर्ड है", "कितना समय लगेगा?", "मुझे किसी से बात करनी है"] },
  id: { role: "Pemandu pengajuan Anda", start: "Panggilan video dengan {name}", onCall: "Sedang panggilan · ketuk untuk membuka",
        placeholder: "Tulis pertanyaan", send: "Tanya", step: "Langkah", of: "dari", listening: "Mendengarkan…", thinking: "Berpikir…",
        you: "Anda", mic: "Bicara tanpa tangan", micOff: "Berhenti mendengarkan", cam: "Kamera", cc: "Teks", mute: "Bisukan suara",
        unmute: "Suarakan", end: "Akhiri panggilan", expand: "Perbesar", shrink: "Perkecil", minimise: "Kecilkan",
        voice: "Suara", switchTo: "Ganti presenter", auto: "Terbaik yang tersedia", aiBadge: "Presenter AI", sound: "🔊 Ketuk untuk menyalakan suara",
        avatarSilent: "Presenter video tidak merespons, jadi saya menjawab dengan suara perangkat ini.",
        topics: "Tanya tentang", more: "Pertanyaan lain", moreHint: "Semua yang bisa saya jawab untuk pengajuan Anda saat ini",
        connecting: "Menghubungkan presenter video…", avatarEnded: "Sesi presenter video telah berakhir. Saya lanjutkan dari sini.", camNote: "Kamera Anda hanya terlihat oleh Anda.", interrupt: "Ketuk saya untuk menyela",
        noMic: "Mikrofon diblokir di sini. Anda bisa mengetik pertanyaan.",
        noCam: "Kamera tidak bisa dibuka di sini. Bicara dan mengetik tetap berfungsi.",
        chips: ["Apa yang harus saya lakukan?", "Dokumen apa yang dibutuhkan?", "Rekening koran saya berpassword", "Berapa lama prosesnya?", "Saya ingin bicara dengan petugas"] },
};

/* ------------------------------------------------------------------ small DOM helpers */

const SVG_NS = "http://www.w3.org/2000/svg";
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
  const node = button(undefined, `guide-ctl ${extra}`.trim());
  node.setAttribute("aria-label", label);
  node.title = label;
  const icon = svg("svg", { viewBox: "0 0 24 24", "aria-hidden": "true", class: "guide-icon" });
  svg("path", { d: ICONS[name] }, icon);
  node.appendChild(icon);
  return node;
}
function store(key, value) {
  try {
    if (value === undefined) return localStorage.getItem(key);
    localStorage.setItem(key, value);
  } catch { /* private window or blocked storage: preferences simply do not persist */ }
  return null;
}

/* ------------------------------------------------------------------ the presenter */

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

function buildPresenter(kind = "female", { live = false } = {}) {
  const root = el("div", "guide-avatar photo-presenter");
  const img = document.createElement("img");
  img.className = "presenter-img";
  img.decoding = "async";
  img.draggable = false;
  const ring = el("i", "presenter-ring");
  root.append(img, ring);
  // The live face draws over the photo once its portrait has loaded; until then the photo shows.
  const face = live && createFace ? createFace() : null;
  if (face) {
    root.insertBefore(face.canvas, ring);
    face.onReady(() => root.classList.add("live"));
  }
  const set = (k) => {
    const p = PRESENTERS[k] || PRESENTERS.female;
    if (root.dataset.presenter === k) return;
    img.src = p.src;
    img.alt = p.name;
    root.dataset.presenter = k;
    if (face) { root.classList.remove("live"); face.setPortrait(k, p.src); }
  };
  set(kind);
  return { root, set, face };
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

export function createGuide({ api, post, context, actions }) {
  const synth = window.speechSynthesis || null;
  const g = {
    user: null, open: false, minimised: false, expanded: false, lang: "en", lastKey: null, busy: false,
    muted: store("guide-muted") === "1", captions: store("guide-captions") !== "0",
    convo: false, rec: null, camStream: null, micStream: null, audioCtx: null, startedAt: 0, speakToken: 0,
    presenter: "female",
  };
  // Hindi first person is gendered; the lines here are written in the masculine.
  const inVoice = (text) => (g.lang === "hi" && g.presenter === "female"
    ? HI_FEMININE.reduce((out, [male, female]) => out.replaceAll(male, female), text) : text);
  const presenterName = (lang = "en") => PRESENTERS[g.presenter][lang] || PRESENTERS[g.presenter].name;

  // ---- layout: a floating video-call window
  const launcher = button(undefined, "guide-launcher hidden");
  const launcherPresenter = buildPresenter();
  const launcherFace = el("span", "guide-mini");
  launcherFace.appendChild(launcherPresenter.root);
  const launcherText = el("span", "guide-launcher-text");
  launcher.append(launcherFace, launcherText);

  const call = el("section", "guide-call hidden");
  call.setAttribute("aria-label", "Video call");

  const top = el("div", "guide-top");
  const who = el("div", "guide-who");
  const whoName = el("b");
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
  const switchBtn = button("⇄", "guide-ctl small guide-switch");
  top.append(who, live, langPick, switchBtn, expandBtn, minBtn);

  const stage = el("div", "guide-stage");
  const presenter = buildPresenter("female", { live: true });
  const face = presenter.face; // null without WebGL: the still photo and its speaking ring remain
  const presenterWrap = button(undefined, "guide-presenter");
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
  const soundBtn = button(undefined, "guide-sound hidden");
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
  // When new topics appear (the statement was read, a decision arrived), lead with them.
  const questions = questionPicker({ onAsk: (q) => ask(q), prefer: ["decision", "income", "next"],
                                     chipsClass: "chips guide-chips" });
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
  body.append(actionRow, transcript, questions.root, askRow, voiceRow, note);

  call.append(top, stage, progress, controls, body);
  document.body.append(launcher, call);

  /* ---------------------------------------------------------------- presenter life */

  // The speech rhythm drives the live face's mouth, and the speaking ring when the photo is still.
  const ENERGY = { rest: 0, small: 0.35, round: 0.6, wide: 0.75, open: 1 };
  function setViseme(name) {
    face?.rig.setViseme(name);
    stage.style.setProperty("--talk", String(ENERGY[name] ?? 0.3));
  }
  function setMood(mood) { // idle | talking | listening | thinking
    stage.dataset.mood = mood;
    launcherFace.dataset.mood = mood;
    face?.rig.setState(mood);
  }
  setMood("idle");

  // The live face moves by itself. A still photo only drifts a little, like a person settling in a chair.
  let driftTimer = 0;
  function startLife() {
    stopLife();
    if (face) { face.start(); return; }
    const drift = () => {
      const x = stage.dataset.mood === "listening" ? 0 : (Math.random() - 0.5) * 2;
      const y = stage.dataset.mood === "thinking" ? -0.9 : (Math.random() - 0.5) * 0.75;
      presenter.root.style.setProperty("--drift-x", `${x.toFixed(2)}%`);
      presenter.root.style.setProperty("--drift-y", `${y.toFixed(2)}%`);
      driftTimer = setTimeout(drift, 1200 + Math.random() * 2600);
    };
    drift();
  }
  function stopLife() { clearTimeout(driftTimer); face?.stop(); }

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
        const wantFemale = g.presenter === "female";
        if ((wantFemale ? FEMALE_VOICES : MALE_VOICES).test(v.name)) score += 45;
        else if ((wantFemale ? MALE_VOICES : FEMALE_VOICES).test(v.name)) score -= 60;
        return { v, score };
      })
      .filter(Boolean)
      .sort((a, b) => b.score - a.score)
      .map((x) => x.v);
  }
  function chosenVoice() {
    const ranked = rankVoices(g.lang);
    const saved = store(`guide-voice-${g.presenter}-${g.lang}`);
    return ranked.find((v) => v.voiceURI === saved) || ranked[0] || null;
  }
  function fillVoicePicker() {
    const ranked = rankVoices(g.lang).slice(0, 12);
    const saved = store(`guide-voice-${g.presenter}-${g.lang}`) || "";
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
  // speaking: the presenter is mid-sentence (from its own events); onStarted: waits for the next sentence to begin.
  const avatar = { room: null, session: null, pending: null, starting: null, speaking: false, onStarted: null };
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
          post("/api/guide/avatar", { language: g.lang, presenter: g.presenter })]);
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
          if (event.event_type === "avatar.speak_started") { avatar.speaking = true; setMood("talking"); avatar.onStarted?.(); }
          if (event.event_type === "avatar.speak_ended") {
            avatar.speaking = false;
            setMood("idle");
            avatar.pending?.();
            avatar.pending = null;
          }
          if (event.event_type === "session.stopped") endAvatar(true);
        });
        room.on(LK.RoomEvent.Disconnected, () => endAvatar(true));
        await room.connect(session.livekit_url, session.livekit_token);
        avatar.room = room;
        avatar.session = session;
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
    aiBadge.textContent = UI[g.lang].aiBadge;
    try { room?.disconnect(); } catch { /* already closed */ }
    stage.querySelectorAll(".guide-room-audio").forEach((player) => player.remove());
    if (session) {
      post("/api/guide/avatar/stop", { session_id: session.session_id, session_token: session.session_token }).catch(() => {});
    }
    if (byProvider && g.open) note.textContent = inVoice(UI[g.lang].avatarEnded);
  }

  function stopSpeaking() {
    g.speakToken += 1;
    if (avatar.room) {
      if (avatar.speaking) sendAvatar("avatar.interrupt");
      avatar.speaking = false;
      avatar.onStarted = null;
      avatar.pending?.();
      avatar.pending = null;
    }
    if (synth) synth.cancel();
    lipsStop();
    if (stage.dataset.mood === "talking") setMood("idle");
  }

  // Speak a reply sentence by sentence. Resolves when it has finished (or was interrupted).
  function say(text) {
    text = inVoice(text);
    stopSpeaking();
    const token = g.speakToken;
    addTranscript("bot", text);
    if (avatar.room && !g.muted) {
      captionNow.textContent = text;
      sendAvatar("avatar.speak_text", { text: forSpeech(text, g.lang) });
      return new Promise((resolve) => {
        let ceiling = 0;
        const done = () => {
          clearTimeout(ceiling);
          clearTimeout(startWait);
          avatar.pending = null;
          avatar.onStarted = null;
          resolve();
        };
        // The answer must never be lost: if the presenter has not started speaking within 4 seconds,
        // say it with the device voice instead.
        const startWait = setTimeout(() => {
          avatar.pending = null;
          avatar.onStarted = null;
          if (token !== g.speakToken) { resolve(); return; }
          note.textContent = UI[g.lang].avatarSilent;
          speakWithBrowser(text, token).then(resolve);
        }, 4000);
        // Once it starts, resolve on its own "finished" event, with a ceiling in case that never comes.
        avatar.onStarted = () => { clearTimeout(startWait); ceiling = setTimeout(done, 4000 + text.length * 90); };
        avatar.pending = done;
      });
    }
    return speakWithBrowser(text, token);
  }

  function speakWithBrowser(text, token) {
    const parts = sentences(text);
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
        u.onboundary = (e) => {
          if (token !== g.speakToken || e.name === "sentence") return;
          lipsSync(e.charIndex);
          face?.rig.word(spoken.slice(e.charIndex).split(/\s/, 1)[0]);
        };
        const finish = () => {
          if (token !== g.speakToken) { resolve(); return; }
          lipsStop();
          face?.rig.sentenceEnd();
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

  // One utterance from the microphone: the words, "" if nobody spoke, or null if there is no usable mic.
  function hear() {
    return new Promise((resolve) => {
      let heard = "";
      let blocked = false;
      const rec = recogniser(g.lang, { onText: (text) => { heard = text; captionNow.textContent = `${UI[g.lang].you}: ${text}`; } });
      rec.onerror = (event) => { blocked = MIC_BLOCKED.includes(event.error); };
      rec.onend = () => { g.rec = null; setListening(false); resolve(blocked ? null : heard); };
      try { rec.start(); g.rec = rec; setListening(true); } catch { resolve(""); }
    });
  }
  function stopHearing() {
    if (g.rec) { try { g.rec.abort(); } catch { /* already stopped */ } }
    g.rec = null;
  }

  // Hands-free conversation: listen, ask, listen again. Silence keeps the line open a few times.
  let quietRetries = 0;
  async function listen() {
    if (!Recognition || g.rec || !g.convo || g.busy) return;
    const heard = await hear();
    if (heard === null) { setConvo(false); note.textContent = UI[g.lang].noMic; return; }
    if (heard) { quietRetries = 0; ask(heard); return; }
    if (g.convo && quietRetries < 3) { quietRetries += 1; setTimeout(listen, 250); }
    else if (g.convo) setConvo(false);
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
      stopHearing();
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
    line.append(el("b", null, who === "bot" ? presenterName() : UI[g.lang].you), document.createTextNode(` ${text}`));
    transcript.appendChild(line);
    while (transcript.children.length > 12) transcript.removeChild(transcript.firstChild);
    transcript.scrollTop = transcript.scrollHeight;
  }

  function renderJourney(result) {
    const j = result.journey;
    bar.style.width = `${j.progress}%`;
    stepChip.textContent = `${UI[g.lang].step} ${j.step_number} ${UI[g.lang].of} ${j.total_steps}`;
    stepChip.classList.toggle("hidden", j.step === "list");
    actionRow.replaceChildren(...result.actions.map((a) => {
      const go = button(a.label, a.id === "callback" ? "ghost" : "stamp", async () => {
        go.disabled = true;
        try { await actions[a.id]?.(); } finally { go.disabled = false; }
      });
      return go;
    }));
    renderFormButtons();
  }

  /* ---------------------------------------------------------------- form interview */

  const form = actions.form;
  const flow = { active: false, queue: [], idx: 0, fails: 0, micBlocked: false };
  // Commands are short ("skip", "go back"); a long answer that happens to contain "next" is still an answer.
  const said = (text, words) => text.split(/\s+/).length <= 4 && words.some((w) => text.includes(w));

  function readBack(key, value) {
    if (key === "pan") return String(value).split("").join(" ");
    if (key === "mobile") return String(value).replace(/^(\d{5})(\d{5})$/, "$1 $2");
    if (key === "requested_amount") return `₹${Number(value).toLocaleString("en-IN")}`;
    if (key === "tenure_months") return `${value} ${FORM_TEXT[g.lang].months}`;
    return String(value);
  }

  function formButtons() {
    if (!form?.open()) return [];
    return [button(flow.active ? FORM_TEXT[g.lang].stop : FORM_TEXT[g.lang].fill, flow.active ? "ghost" : "stamp",
                   () => (flow.active ? endForm(true) : startForm()))];
  }
  function renderFormButtons() {
    actionRow.querySelectorAll(".form-flow-btn").forEach((b) => b.remove());
    formButtons().forEach((b) => { b.classList.add("form-flow-btn"); actionRow.appendChild(b); });
  }

  async function startForm() {
    if (!form?.open()) { await say(FORM_TEXT[g.lang].notHere); return; }
    setConvo(false); // the interview does its own listening
    Object.assign(flow, { active: true, idx: 0, fails: 0, queue: FORM_ORDER.filter((k) => form.isEmpty(k)) });
    renderFormButtons();
    if (!flow.queue.length) { await say(FORM_TEXT[g.lang].allFilled); return endForm(); }
    askField(FORM_TEXT[g.lang].start);
  }

  async function askField(prefix = "") {
    if (!flow.active) return;
    if (flow.idx >= flow.queue.length) { await say(prefix); return endForm(); }
    const key = flow.queue[flow.idx];
    form.focus(key);
    await say(`${prefix} ${FORM_Q[g.lang][key]}`.trim());
    if (!flow.active) return;
    const heard = await listenOnce();
    if (heard && flow.active) handleFormAnswer(heard);
  }

  // One answer for the form interview, or null (no mic, or silence): then a typed answer is awaited.
  async function listenOnce() {
    for (let tries = 0; tries < 3 && Recognition && !flow.micBlocked && flow.active; tries += 1) {
      const heard = await hear();
      if (heard === null) { flow.micBlocked = true; note.textContent = FORM_TEXT[g.lang].typeInstead; }
      if (heard) return heard;
    }
    return null;
  }

  async function handleFormAnswer(text) {
    if (!flow.active) return;
    stopHearing();
    addTranscript("me", text);
    input.value = "";
    const t = text.toLowerCase().trim();
    const key = flow.queue[flow.idx];
    const W = FORM_WORDS;
    if (said(t, W.stop)) return endForm(true);
    if (said(t, W.skip)) { flow.idx += 1; flow.fails = 0; return askField(); }
    if (said(t, W.back)) {
      if (flow.idx > 0) { flow.idx -= 1; form.clear(flow.queue[flow.idx]); }
      flow.fails = 0;
      return askField();
    }
    if (said(t, W.repeat)) return askField();
    let value = null;
    try {
      const body = await post("/api/form/voice", { transcript: text, field: key });
      value = body.fields?.[key] ?? null;
    } catch { value = null; }
    if (value !== null && form.set(key, value)) {
      flow.idx += 1;
      flow.fails = 0;
      return askField(FORM_TEXT[g.lang].gotIt.replace("{value}", readBack(key, value)));
    }
    flow.fails += 1;
    if (flow.fails >= 2) { flow.idx += 1; flow.fails = 0; return askField(FORM_TEXT[g.lang].leaveIt); }
    return askField(FORM_TEXT[g.lang].notCaught);
  }

  async function endForm(stopped = false) {
    stopHearing();
    const wasActive = flow.active;
    flow.active = false;
    renderFormButtons();
    if (!wasActive && stopped) return;
    await say(stopped ? FORM_TEXT[g.lang].stopped : FORM_TEXT[g.lang].done);
    if (!stopped) form.finish();
  }

  async function ask(message = "", { greet = false } = {}) {
    // During the form interview every message is an answer (or a command) for the current field.
    if (flow.active && message) { handleFormAnswer(message); return; }
    if (!flow.active && message && form?.open() && FORM_WORDS.start.some((w) => message.toLowerCase().includes(w))) {
      addTranscript("me", message);
      input.value = "";
      startForm();
      return;
    }
    if (g.busy) return;
    g.busy = true;
    stopSpeaking();
    input.value = "";
    if (message) { addTranscript("me", message); setMood("thinking"); captionNow.textContent = inVoice(UI[g.lang].thinking); }
    let reply = null;
    try {
      const { caseId, screen, field } = context();
      reply = await post("/api/guide", { message, case_id: caseId, language: g.lang, screen,
                                         field: !caseId && screen !== "list" ? field : null, greet, presenter: g.presenter });
      renderJourney(reply);
      face?.rig.setMood(reply.mood || "warm");
      if (reply.field && !message) actions.highlight?.(reply.field);
    } catch (error) {
      reply = { reply: error.message };
    } finally { g.busy = false; }
    if (stage.dataset.mood === "thinking") setMood("idle");
    await say(reply.reply);
    if (g.convo && g.open) listen();
  }

  /* ---------------------------------------------------------------- suggested questions, by topic */

  const loaded = { key: null, groups: null };
  async function loadQuestions() {
    const { caseId } = context();
    const key = `${g.lang}:${caseId || ""}:${g.lastKey || ""}`;
    if (loaded.key === key) return;
    loaded.key = key;
    try {
      const params = new URLSearchParams({ language: g.lang, ...(caseId ? { case_id: caseId } : {}) });
      loaded.groups = (await api(`/api/guide/questions?${params}`)).groups;
    } catch {
      loaded.groups = null;
    }
    renderQuestions();
  }
  function renderQuestions() {
    const t = UI[g.lang];
    questions.render({ groups: loaded.groups, fallback: t.chips, labels: { topics: t.topics, more: t.more, hint: inVoice(t.moreHint) } });
  }

  // Speak again only when the customer's step (or, on a new application, the field to fill next)
  // actually changes, not on every repaint.
  let refreshTimer = 0;
  async function refresh() {
    if (!g.open || !g.user) return;
    if (g.busy) { clearTimeout(refreshTimer); refreshTimer = setTimeout(refresh, 400); return; }
    const { caseId, screen, field } = context();
    let key = screen === "list" ? "list" : `details:${field || ""}`;
    if (screen !== "list" && caseId) {
      try { key = `${caseId}:${(await api(`/api/cases/${caseId}/journey`)).step}`; } catch { return; }
    }
    // During the voice form interview, the interview asks the questions; field hints would talk over it.
    if (key !== g.lastKey && !(flow.active && key.startsWith("details:"))) {
      const greet = g.lastKey === null;
      g.lastKey = key;
      ask("", { greet });
    }
    loadQuestions();
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
    loadQuestions(); // the questions do not need to wait for the presenter
    // Connect the photoreal presenter first (when configured), so the greeting comes from one voice.
    Promise.resolve(startAvatar()).finally(refresh);
  }
  function minimise() {
    g.minimised = true;
    face?.stop();
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
    launcherText.textContent = g.minimised ? `${presenterName(g.lang)} · ${t.onCall}` : t.start.replace("{name}", presenterName(g.lang));
    whoName.textContent = presenterName();
    call.setAttribute("aria-label", `Video call with ${presenterName()}`);
    presenter.set(g.presenter);
    launcherPresenter.set(g.presenter);
    switchBtn.title = `${t.switchTo}: ${PRESENTERS[g.presenter === "female" ? "male" : "female"].name}`;
    switchBtn.setAttribute("aria-label", switchBtn.title);
    launcher.classList.toggle("on-call", g.minimised);
    whoRole.textContent = t.role;
    if (!avatar.session) aiBadge.textContent = t.aiBadge;
    input.placeholder = t.placeholder;
    send.textContent = t.send;
    listenText.textContent = inVoice(t.listening);
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
    face?.setFraming(g.expanded ? 0.48 : 0.5);
    langPick.value = g.lang;
    renderQuestions();
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
    stopHearing();
    relabel();
    g.lastKey = null;
    // The avatar's voice language is fixed per session, so a new language needs a new session.
    const hadAvatar = Boolean(avatar.room);
    endAvatar();
    Promise.resolve(hadAvatar ? startAvatar() : null).finally(refresh);
  });
  function applyPresenter(kind, { remember = false } = {}) {
    if (!PRESENTERS[kind] || kind === g.presenter) return;
    g.presenter = kind;
    if (remember && g.user) store(`guide-presenter-${g.user.uid}`, kind);
    stopSpeaking();
    relabel();
    // A photoreal session shows one avatar; switching starts a new one with the other presenter.
    if (avatar.room) { endAvatar(); startAvatar(); }
  }
  switchBtn.addEventListener("click", () => applyPresenter(g.presenter === "female" ? "male" : "female", { remember: true }));
  voicePick.addEventListener("change", () => {
    store(`guide-voice-${g.presenter}-${g.lang}`, voicePick.value);
    const sample = { en: "Hello, this is how I sound.", hi: "नमस्ते, मेरी आवाज़ ऐसी है।", id: "Halo, beginilah suara saya." }[g.lang];
    say(sample);
  });
  document.addEventListener("keydown", (e) => { if (e.key === "Escape" && g.open && !g.minimised) minimise(); });
  relabel();

  return {
    setUser(user, lang) {
      g.user = user && (user.role === "borrower" || user.role === "loan_officer") ? user : null;
      if (lang && UI[lang]) g.lang = lang;
      // The presenter of the other gender from the person's own profile (female if not stated); a
      // presenter the person picked themselves on this device wins.
      const saved = g.user ? store(`guide-presenter-${g.user.uid}`) : null;
      g.presenter = PRESENTERS[saved] ? saved : (PRESENTERS[g.user?.guide_presenter] ? g.user.guide_presenter : "female");
      if (!g.user) endCall();
      relabel();
      launcher.classList.toggle("hidden", !g.user || (g.open && !g.minimised));
    },
    onView() {
      // Leaving the form (or saving it) ends the interview quietly.
      if (flow.active && !form?.open()) {
        flow.active = false;
        stopHearing();
      }
      refresh();
      renderFormButtons();
    },
    setPresenter(kind) { applyPresenter(kind, { remember: true }); }, // a profile change replaces a device pick
    onUploadError(message) {
      if (!g.open) return;
      // Known problems get the guide's fuller help; anything else is read out as the server wrote it.
      const topic = /password/i.test(message) ? "My statement has a password" : /scan|photograph/i.test(message)
        ? "Can I upload a photo of my statement?" : "";
      if (topic) ask(topic); else say(message);
    },
  };
}
