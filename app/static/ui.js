/* Helpers shared by the app and the video guide. */

export function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

export function button(text, className, onClick) {
  const node = el("button", className, text);
  node.type = "button";
  if (onClick) node.addEventListener("click", onClick);
  return node;
}

/* ------------------------------------------------------------------ speech recognition */

export const Recognition = window.SpeechRecognition || window.webkitSpeechRecognition || null;
export const SPEECH_LANG = { en: "en-IN", hi: "hi-IN", id: "id-ID" };
// Errors that mean there is no usable microphone here, as opposed to nobody speaking.
export const MIC_BLOCKED = ["not-allowed", "service-not-allowed", "audio-capture"];

// A recogniser for one language; onText receives the whole transcript so far, interim words included.
export function recogniser(lang, { continuous = false, onText } = {}) {
  const rec = new Recognition();
  rec.lang = SPEECH_LANG[lang] || SPEECH_LANG.en;
  rec.continuous = continuous;
  rec.interimResults = true;
  rec.onresult = (event) => onText?.(Array.from(event.results).map((r) => r[0].transcript).join(" ").trim());
  return rec;
}

/* ------------------------------------------------------------------ suggested questions, by topic */

/* Topic pills, the featured questions of the chosen topic, and every question under "more".
   `memory` keeps the chosen topic across re-renders; when the set of topics changes, the first of
   `prefer` that is present is chosen (new topics are usually what the person can now ask about). */
export function questionPicker({ onAsk, prefer = [], chipsClass = "chips", memory = {} }) {
  const root = el("div", "question-picker");
  const topics = el("div", "guide-topics");
  topics.setAttribute("role", "tablist");
  const chips = el("div", chipsClass);
  const more = el("details", "guide-more");
  const summary = el("summary", "small");
  const hint = el("p", "small muted");
  const list = el("div", "guide-more-list");
  more.append(summary, hint, list);
  root.append(topics, chips, more);

  const ask = (text, className) => button(text, className, () => { more.open = false; onAsk(text); });
  let shown = { groups: null, fallback: [], labels: {} };

  function draw() {
    const { groups, fallback, labels } = shown;
    if (!groups?.length) {
      topics.replaceChildren();
      more.classList.add("hidden");
      chips.replaceChildren(...fallback.map((q) => ask(q)));
      return;
    }
    const tags = groups.map((g) => g.tag);
    if (tags.join() !== memory.seen) {
      memory.seen = tags.join();
      memory.tag = prefer.find((t) => tags.includes(t)) ?? (tags.includes(memory.tag) ? memory.tag : tags[0]);
    }
    topics.replaceChildren(el("span", "small muted", labels.topics), ...groups.map((g) => {
      const pill = button(g.label, "guide-topic", () => { memory.tag = g.tag; draw(); });
      pill.setAttribute("role", "tab");
      pill.setAttribute("aria-selected", String(g.tag === memory.tag));
      return pill;
    }));
    const current = groups.find((g) => g.tag === memory.tag) || groups[0];
    chips.replaceChildren(...current.questions.filter((q) => q.featured).map((q) => ask(q.text)));
    summary.textContent = labels.more;
    hint.textContent = labels.hint || "";
    hint.hidden = !labels.hint;
    more.classList.remove("hidden");
    list.replaceChildren(...groups.map((g) => {
      const section = el("div", "guide-more-group");
      section.appendChild(el("b", "small", g.label));
      g.questions.forEach((q) => section.appendChild(ask(q.text, "guide-more-q")));
      return section;
    }));
  }

  return {
    root,
    render(next) { shown = { ...shown, ...next }; draw(); },
  };
}
