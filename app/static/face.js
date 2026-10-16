/* Live presenter face: brings a still portrait to life in the browser, with nothing downloaded and
   nothing sent anywhere.

   A WebGL fragment shader warps the photo every frame. Each output pixel is traced back to the pixel
   of the photo it came from, through a chain of small, smooth deformations:
     head  - a gentle tilt, nod and sway around the neck, fading out at the shoulders
     blobs - local pushes for the brows, cheeks, mouth corners and a little parallax of the features
     mouth - the lower lip and chin drop below the line between the lips; the gap that opens is drawn as
             a mouth (teeth at the top, shade below), so the mouth really opens and closes
     eyes  - the upper lid slides down over the eye for a blink; the iris moves for a glance
   The rig drives those deformations from speech (mouth shapes, small nods and brow lifts on words),
   from the conversation (listening, thinking, empathy, good news) and from idle life (blinks at human
   intervals, glances, breathing, settling). Landmarks for each portrait are measured once, below. */

// Landmarks in the source photos' pixel coordinates (941 x 1672). "left" is the left of the image.
export const FACES = {
  female: {
    eyeL: { outer: [327, 448], inner: [411, 440], top: [370, 424], bottom: [370, 457], iris: [370, 441], r: 11 },
    eyeR: { inner: [492, 418], outer: [575, 402], top: [535, 392], bottom: [533, 423], iris: [537, 405], r: 11 },
    browL: [[317, 400], [370, 384], [412, 395]],
    browR: [[465, 368], [515, 350], [578, 358]],
    nose: [470, 503],
    mouth: { left: [407, 583], right: [558, 550], seam: [482, 574], upper: [490, 557], lower: [480, 609] },
    chin: [495, 690],
    head: { c: [480, 420], r: [240, 330] },
    pivot: [480, 780],
  },
  male: {
    eyeL: { outer: [327, 442], inner: [392, 435], top: [360, 424], bottom: [360, 447], iris: [360, 435], r: 10 },
    eyeR: { inner: [480, 412], outer: [545, 407], top: [510, 397], bottom: [510, 420], iris: [509, 409], r: 10 },
    browL: [[300, 415], [350, 395], [400, 388]],
    browR: [[450, 367], [495, 355], [550, 360]],
    nose: [440, 502],
    mouth: { left: [382, 570], right: [520, 550], seam: [455, 572], upper: [460, 542], lower: [460, 598] },
    chin: [470, 680],
    head: { c: [460, 400], r: [220, 300] },
    pivot: [460, 740],
  },
};

const NB = 12; // deformation blobs

const VERT = `
attribute vec2 aPos;
uniform vec2 uCanvas;
varying vec2 vPx;
void main() {
  vPx = vec2((aPos.x * 0.5 + 0.5) * uCanvas.x, (0.5 - aPos.y * 0.5) * uCanvas.y);
  gl_Position = vec4(aPos, 0.0, 1.0);
}`;

const FRAG = `
precision highp float;
uniform sampler2D uTex;
uniform vec2 uImg;
uniform float uScale;
uniform vec2 uOffset;
uniform vec2 uHeadC; uniform vec2 uHeadR; uniform vec2 uPivot; uniform float uRoll; uniform vec2 uHeadT;
uniform vec4 uBlob[${NB}];   // x, y, sigma
uniform vec2 uMO; uniform vec2 uMU; uniform float uMW; uniform float uSeam;
uniform float uJaw; uniform float uLift; uniform float uChinV; uniform float uLipL; uniform float uRound;
uniform vec2 uEO0; uniform vec2 uEU0; uniform vec3 uEP0; uniform vec3 uIris0;
uniform vec2 uEO1; uniform vec2 uEU1; uniform vec3 uEP1; uniform vec3 uIris1;
uniform float uBlink; uniform float uSquint; uniform vec2 uGaze;
varying vec2 vPx;

vec2 headDisp(vec2 x) {
  vec2 q = (x - uHeadC) / uHeadR;
  float m = 1.0 - smoothstep(0.8, 1.3, length(q));
  m *= 1.0 - smoothstep(uPivot.y - 80.0, uPivot.y + 40.0, x.y);
  vec2 d = x - uPivot;
  float c = cos(uRoll), s = sin(uRoll);
  vec2 r = vec2(c * d.x - s * d.y, s * d.x + c * d.y) + uPivot + uHeadT;
  return m * (r - x);
}

uniform vec2 uBlobD[${NB}];
vec2 blobs(vec2 x) {
  vec2 sum = vec2(0.0);
  for (int i = 0; i < ${NB}; i++) {
    vec4 b = uBlob[i];
    vec2 d = x - b.xy;
    sum += uBlobD[i] * exp(-dot(d, d) / (2.0 * b.z * b.z + 0.0001));
  }
  return sum;
}

// Vertical displacement of the lower lip and chin at local (u, v), v below the seam.
float lowerD(float t, float v, float seam) {
  float prof = pow(max(0.0, 1.0 - t * t), 0.8);
  float chin = exp(-t * t / 2.6);
  float tt = smoothstep(seam, seam + uLipL, v);
  float fade = 1.0 - smoothstep(uChinV, uChinV + 70.0, v);
  return uJaw * (prof * (1.0 - tt) + chin * tt * 0.85) * fade;
}
float upperD(float t, float v, float seam) {
  float prof = pow(max(0.0, 1.0 - t * t), 0.8);
  float k = (seam - v) / 12.0;
  return uLift * prof * exp(-k * k);
}

vec2 eye(vec2 s, vec2 O, vec2 U, vec3 P, vec3 iris) {
  vec2 V = vec2(-U.y, U.x);
  vec2 d = s - O;
  float u = dot(d, U), v = dot(d, V);
  float t = u / P.x;
  if (abs(t) >= 1.0) return s;
  float f = 1.0 - t * t;
  float top = P.y * f, low = P.z * f, h = low - top;
  float top2 = top + uBlink * h * 0.94;
  float low2 = max(low - uSquint * h * 0.3, top2 + 0.05 * f);
  float A = top - (abs(P.y) * 1.7 + 8.0);
  float B = low + (abs(P.z) * 1.2 + 6.0);
  float vs = v;
  if (v > A && v < top2) vs = A + (v - A) * (top - A) / (top2 - A);
  else if (v >= top2 && v <= low2) vs = top + (v - top2) * h / max(low2 - top2, 0.0001);
  else if (v > low2 && v < B) vs = low + (v - low2) * (B - low) / (B - low2);
  vec2 r = O + u * U + vs * V;
  float inside = smoothstep(0.0, 2.5, vs - top) * smoothstep(0.0, 2.5, low - vs);
  vec2 di = r - iris.xy;
  float sg = iris.z * 1.7;
  return r - uGaze * inside * exp(-dot(di, di) / (2.0 * sg * sg));
}

void main() {
  vec2 p = (vPx - uOffset) / uScale;
  float px = 1.0 / uScale;
  vec2 x = p;
  for (int i = 0; i < 3; i++) x = p - headDisp(x);
  vec2 y = x;
  for (int i = 0; i < 3; i++) y = x - blobs(y);

  // Mouth
  vec2 V = vec2(-uMU.y, uMU.x);
  vec2 dm = y - uMO;
  float u = dot(dm, uMU), v = dot(dm, V);
  float w = uMW * (1.0 - 0.22 * uRound);
  float t = u / w;
  float f = max(0.0, 1.0 - t * t);
  float seam = uSeam * max(0.0, 1.0 - (u / uMW) * (u / uMW));
  float prof = pow(f, 0.8);
  float top = seam - uLift * prof, bot = seam + uJaw * prof;
  float cav = 0.0;
  float k = 0.0;
  float vs = v;
  if (abs(t) < 1.0 && v > top && v < bot) {
    cav = smoothstep(0.0, px, v - top) * smoothstep(0.0, px, bot - v);
    k = (v - top) / max(bot - top, 0.001);
    vs = seam;
  } else if (v >= bot) {
    float tt = u / uMW;
    vs = v - uJaw * prof;
    for (int i = 0; i < 3; i++) vs = v - lowerD(tt, vs, seam);
  } else if (v <= top) {
    float tt = u / uMW;
    vs = v + uLift * prof;
    for (int i = 0; i < 3; i++) vs = v + upperD(tt, vs, seam);
  }
  vec2 s = uMO + u * uMU + vs * V;

  s = eye(s, uEO0, uEU0, uEP0, uIris0);
  s = eye(s, uEO1, uEU1, uEP1, uIris1);

  vec3 col = texture2D(uTex, clamp(s / uImg, vec2(0.0), vec2(1.0))).rgb;
  if (cav > 0.0) {
    // Inside the mouth: a soft row of upper teeth in the middle, shade below, a hint of tongue when wide.
    float gap = bot - top;
    float teethK = clamp(4.5 / max(gap, 0.001), 0.0, 0.38) * smoothstep(3.0, 8.0, gap);
    vec3 mouth = mix(vec3(0.12, 0.035, 0.045), vec3(0.21, 0.075, 0.085), smoothstep(0.2, 0.9, k));
    mouth = mix(mouth, vec3(0.44, 0.17, 0.19), smoothstep(0.74, 1.0, k) * smoothstep(9.0, 15.0, gap) * 0.6);
    vec3 teeth = vec3(0.84, 0.80, 0.76) * (0.62 + 0.38 * (1.0 - abs(t))) * (0.85 + 0.15 * (1.0 - k / max(teethK, 0.001)));
    float teethA = (1.0 - smoothstep(teethK - 0.08, teethK + 0.08, k)) * smoothstep(0.8, 0.45, abs(t));
    mouth *= (0.5 + 0.5 * f) * (0.72 + 0.28 * smoothstep(0.0, 0.22, min(k, 1.0 - k)));
    mouth = mix(mouth, teeth, teethA * 0.92);
    col = mix(col, mouth, cav * smoothstep(1.0, 0.86, abs(t)));
  }
  gl_FragColor = vec4(col, 1.0);
}`;

const sub = (a, b) => [a[0] - b[0], a[1] - b[1]];
const add = (a, b) => [a[0] + b[0], a[1] + b[1]];
const mul = (a, k) => [a[0] * k, a[1] * k];
const dot = (a, b) => a[0] * b[0] + a[1] * b[1];
const len = (a) => Math.hypot(a[0], a[1]);
const unit = (a) => mul(a, 1 / (len(a) || 1));
const mid = (a, b) => mul(add(a, b), 0.5);
const perp = (u) => [-u[1], u[0]]; // "down" for a left-to-right axis, in image coordinates

// Geometry derived once per portrait: local frames for the mouth and eyes, in the shader's terms.
function geometry(F) {
  const m = F.mouth;
  const mo = mid(m.left, m.right);
  const mu = unit(sub(m.right, m.left));
  const mv = perp(mu);
  const mw = len(sub(m.right, m.left)) / 2;
  const local = (p) => { const d = sub(p, mo); return [dot(d, mu), dot(d, mv)]; };
  const [su, sv] = local(m.seam);
  const seam = sv / Math.max(0.2, 1 - (su / mw) ** 2);
  const lipL = (local(m.lower)[1] - sv) * 1.3;
  const chinV = local(F.chin)[1];
  const eye = (e) => {
    const o = mid(e.outer, e.inner);
    const left = e.outer[0] < e.inner[0] ? e.outer : e.inner;
    const right = left === e.outer ? e.inner : e.outer;
    const u = unit(sub(right, left));
    const v = perp(u);
    const half = len(sub(right, left)) / 2;
    const vAt = (p) => { const d = sub(p, o); const t = dot(d, u) / half; return dot(d, v) / Math.max(0.3, 1 - t * t); };
    return { o, u, p: [half, vAt(e.top), vAt(e.bottom)], iris: [...e.iris, e.r] };
  };
  const faceU = unit(sub(F.eyeR.outer, F.eyeL.outer));
  return { mo, mu, mv, mw, seam, lipL, chinV, eyes: [eye(F.eyeL), eye(F.eyeR)], up: mul(perp(faceU), -1), faceU };
}

/* ------------------------------------------------------------------ the rig: expressions over time */

// Mouth shapes for the speech visemes the guide already computes.
const VISEME = {
  rest: { jaw: 0, wide: 0, round: 0 }, closed: { jaw: 0, wide: 0.05, round: 0 }, small: { jaw: 0.3, wide: 0.1, round: 0 },
  open: { jaw: 0.85, wide: 0.1, round: 0 }, round: { jaw: 0.45, wide: 0, round: 0.85 },
  wide: { jaw: 0.32, wide: 0.75, round: 0 }, fv: { jaw: 0.12, wide: 0.15, round: 0 },
};
// Resting expression for each mood. smile < 0 turns the corners down a touch.
const MOODS = {
  warm: { smile: 0.3, browUp: 0, browInner: 0, furrow: 0, squint: 0.08, roll: 0 },
  happy: { smile: 0.75, browUp: 0.35, browInner: 0, furrow: 0, squint: 0.3, roll: 0.01 },
  empathetic: { smile: -0.1, browUp: 0, browInner: 0.85, furrow: 0, squint: 0.05, roll: 0.045 },
  polite: { smile: 0.2, browUp: 0.1, browInner: 0.15, furrow: 0, squint: 0.05, roll: 0 },
  listening: { smile: 0.25, browUp: 0.2, browInner: 0.1, furrow: 0, squint: 0.05, roll: 0.035 },
  thinking: { smile: 0.05, browUp: 0, browInner: 0, furrow: 0.6, squint: 0.15, roll: -0.02 },
};

// A critically damped spring: smooth, quick, never overshooting wildly.
function spring(x, v, target, omega, dt) {
  const f = 1 + 2 * dt * omega;
  const oo = omega * omega;
  const hoo = dt * oo;
  const hhoo = dt * hoo;
  const det = 1 / (f + hhoo);
  return [(f * x + dt * v + hhoo * target) * det, (v + hoo * (target - x)) * det];
}

function createRig(reduced) {
  const keys = ["jaw", "wide", "round", "smile", "browUp", "browInner", "furrow", "squint", "gx", "gy", "yaw", "pitch", "roll", "nod", "brow"];
  const cur = Object.fromEntries(keys.map((k) => [k, 0]));
  const vel = Object.fromEntries(keys.map((k) => [k, 0]));
  const tgt = Object.fromEntries(keys.map((k) => [k, 0]));
  const r = { mood: "warm", state: "idle", viseme: "rest", blink: 0, blinkAt: 0, blinkT: -1, glanceAt: 0, nodAt: 0,
    impulses: [], t: 0, energy: 0 };
  const omega = { jaw: 34, wide: 26, round: 26, gx: 40, gy: 40, nod: 14, brow: 16 };

  function scheduleBlink(now, soon = false) { r.blinkAt = now + (soon ? 120 : 2200 + Math.random() * 3600); }
  scheduleBlink(0);

  return {
    cur,
    setMood(m) { if (MOODS[m]) r.mood = m; },
    setState(s) { r.state = s; },
    setViseme(v) { r.viseme = VISEME[v] ? v : "rest"; },
    // A word has started: sometimes a small nod or a lift of the brows, as people do when they speak.
    word(text = "") {
      const strong = text.length > 6 || /[!?]/.test(text);
      if (Math.random() < (strong ? 0.5 : 0.22)) r.impulses.push({ key: "nod", amt: 0.6 + Math.random() * 0.6 });
      if (Math.random() < (strong ? 0.3 : 0.08)) r.impulses.push({ key: "brow", amt: 0.5 + Math.random() * 0.5 });
    },
    sentenceEnd() { if (Math.random() < 0.6) scheduleBlink(performance.now(), true); },
    nod() { r.impulses.push({ key: "nod", amt: 1.2 }); },
    step(now, dt) {
      r.t += dt;
      const mood = MOODS[r.state === "listening" ? "listening" : r.state === "thinking" ? "thinking" : r.mood];
      const talking = r.state === "talking";
      const vis = VISEME[talking ? r.viseme : "rest"];
      Object.assign(tgt, { jaw: vis.jaw, wide: vis.wide, round: vis.round, smile: mood.smile * (talking ? 0.75 : 1),
        browUp: mood.browUp, browInner: mood.browInner, furrow: mood.furrow, squint: mood.squint });

      // Glances: quick jumps of the eyes at irregular intervals; while thinking, up and to the side.
      if (now > r.glanceAt) {
        if (r.state === "thinking") { tgt.gx = -3 - Math.random(); tgt.gy = -2.2 - Math.random() * 0.6; }
        else if (r.state === "listening" || talking) { tgt.gx = (Math.random() - 0.5) * 1.6; tgt.gy = (Math.random() - 0.5) * 0.8; }
        else { tgt.gx = (Math.random() - 0.5) * 3.2; tgt.gy = (Math.random() - 0.5) * 1.4; }
        if (Math.random() < 0.35) { tgt.gx = 0; tgt.gy = 0; }
        r.glanceAt = now + (talking ? 900 : 1100) + Math.random() * 2200;
      }
      // While listening, an occasional slow nod says "I'm following".
      if (r.state === "listening" && now > r.nodAt) { r.impulses.push({ key: "nod", amt: 0.9 }); r.nodAt = now + 2600 + Math.random() * 2400; }

      // Settling: slow, never-repeating sway made of incommensurate sines.
      const sway = reduced ? 0.15 : 1;
      const T = r.t;
      tgt.yaw = sway * (0.45 * Math.sin(T * 0.31) + 0.3 * Math.sin(T * 0.77 + 1.3));
      tgt.pitch = sway * (0.35 * Math.sin(T * 0.43 + 2.1) + 0.2 * Math.sin(T * 1.1)) + (talking ? cur.jaw * 0.5 : 0);
      tgt.roll = mood.roll + sway * 0.012 * Math.sin(T * 0.23 + 0.4);
      tgt.nod = 0; tgt.brow = 0;

      for (const k of keys) {
        const w = omega[k] || 7;
        [cur[k], vel[k]] = spring(cur[k], vel[k], tgt[k], w, dt);
      }
      r.impulses.forEach((imp) => { vel[imp.key] += imp.amt * 9; });
      r.impulses = [];

      // Blink: a quick close, a short hold, a slower opening. Sometimes twice.
      if (r.blinkT < 0 && now > r.blinkAt) r.blinkT = 0;
      if (r.blinkT >= 0) {
        r.blinkT += dt * 1000;
        const b = r.blinkT;
        r.blink = b < 75 ? b / 75 : b < 115 ? 1 : b < 245 ? 1 - (b - 115) / 130 : 0;
        r.blink = r.blink * r.blink * (3 - 2 * r.blink);
        if (b >= 245) { r.blinkT = -1; r.blink = 0; Math.random() < 0.14 ? scheduleBlink(now, true) : scheduleBlink(now); }
      }
      return { ...cur, blink: r.blink, breath: Math.sin(T * (2 * Math.PI / 4.6)) };
    },
  };
}

/* ------------------------------------------------------------------ the renderer */

function compile(gl, type, source) {
  const shader = gl.createShader(type);
  gl.shaderSource(shader, source);
  gl.compileShader(shader);
  if (!gl.getShaderParameter(shader, gl.COMPILE_STATUS)) throw new Error(gl.getShaderInfoLog(shader) || "shader");
  return shader;
}

export function createFace() {
  const reduced = window.matchMedia?.("(prefers-reduced-motion: reduce)").matches || false;
  const canvas = document.createElement("canvas");
  canvas.className = "presenter-canvas";
  canvas.setAttribute("aria-hidden", "true");
  const gl = canvas.getContext("webgl", { antialias: false, alpha: false, premultipliedAlpha: false, preserveDrawingBuffer: false });
  if (!gl) return null;

  let program;
  try {
    program = gl.createProgram();
    gl.attachShader(program, compile(gl, gl.VERTEX_SHADER, VERT));
    gl.attachShader(program, compile(gl, gl.FRAGMENT_SHADER, FRAG));
    gl.linkProgram(program);
    if (!gl.getProgramParameter(program, gl.LINK_STATUS)) throw new Error(gl.getProgramInfoLog(program) || "link");
  } catch (error) {
    console.warn("Live presenter unavailable:", error.message);
    return null;
  }
  gl.useProgram(program);
  const buffer = gl.createBuffer();
  gl.bindBuffer(gl.ARRAY_BUFFER, buffer);
  gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([-1, -1, 3, -1, -1, 3]), gl.STATIC_DRAW);
  const aPos = gl.getAttribLocation(program, "aPos");
  gl.enableVertexAttribArray(aPos);
  gl.vertexAttribPointer(aPos, 2, gl.FLOAT, false, 0, 0);
  const U = {};
  ["uTex", "uImg", "uScale", "uOffset", "uCanvas", "uHeadC", "uHeadR", "uPivot", "uRoll", "uHeadT", "uBlob", "uBlobD",
   "uMO", "uMU", "uMW", "uSeam", "uJaw", "uLift", "uChinV", "uLipL", "uRound", "uEO0", "uEU0", "uEP0", "uIris0",
   "uEO1", "uEU1", "uEP1", "uIris1", "uBlink", "uSquint", "uGaze"].forEach((n) => { U[n] = gl.getUniformLocation(program, n); });
  const texture = gl.createTexture();
  gl.bindTexture(gl.TEXTURE_2D, texture);
  [[gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE], [gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE], [gl.TEXTURE_MIN_FILTER, gl.LINEAR],
   [gl.TEXTURE_MAG_FILTER, gl.LINEAR]].forEach(([k, v]) => gl.texParameteri(gl.TEXTURE_2D, k, v));

  const rig = createRig(reduced);
  const f = { F: null, G: null, img: null, ready: false, raf: 0, last: 0, running: false, anchor: 0.5, zoom: 1.05,
              w: 0, h: 0, onReady: null };

  function setPortrait(kind, src) {
    const F = FACES[kind];
    if (!F) return;
    f.ready = false;
    const img = new Image();
    img.decoding = "async";
    img.onload = () => {
      if (f.img !== img) return;
      gl.bindTexture(gl.TEXTURE_2D, texture);
      gl.pixelStorei(gl.UNPACK_FLIP_Y_WEBGL, false);
      gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGB, gl.RGB, gl.UNSIGNED_BYTE, img);
      f.F = F;
      f.G = geometry(F);
      f.ready = true;
      f.onReady?.();
    };
    f.img = img;
    img.src = src;
  }

  function resize() {
    const rect = canvas.getBoundingClientRect();
    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    const w = Math.max(1, Math.round(rect.width * dpr));
    const h = Math.max(1, Math.round(rect.height * dpr));
    if (w !== f.w || h !== f.h) { canvas.width = w; canvas.height = h; f.w = w; f.h = h; }
  }

  function draw(now) {
    const dt = Math.min(0.05, f.last ? (now - f.last) / 1000 : 0.016);
    f.last = now;
    const p = rig.step(now, dt);
    if (!f.ready) return;
    resize();
    const { F, G } = f;
    const iw = f.img.naturalWidth, ih = f.img.naturalHeight;
    const scale = Math.max(f.w / iw, f.h / ih) * f.zoom;
    // Frame the face, not the photo: the middle of brows-to-chin sits at the anchor height, so the eyes
    // and mouth stay in view in a short call window as well as a tall one.
    const faceMid = (Math.min(F.browL[1][1], F.browR[1][1]) + F.chin[1]) / 2;
    const offset = [(f.w - iw * scale) * 0.5,
      Math.min(0, Math.max(f.h - ih * scale, f.h * f.anchor - faceMid * scale))];
    gl.viewport(0, 0, f.w, f.h);
    gl.uniform1i(U.uTex, 0);
    gl.uniform2f(U.uImg, iw, ih);
    gl.uniform1f(U.uScale, scale);
    gl.uniform2f(U.uOffset, offset[0], offset[1]);
    gl.uniform2f(U.uCanvas, f.w, f.h);

    // Head: sway, nod and breath move the head; the roll turns it about the neck.
    const up = G.up;
    const headT = add(mul(G.faceU, p.yaw * 2.2), mul(up, -(p.pitch * 1.6 + p.nod * 2.2) + p.breath * 0.7));
    gl.uniform2f(U.uHeadC, ...F.head.c);
    gl.uniform2f(U.uHeadR, ...F.head.r);
    gl.uniform2f(U.uPivot, ...F.pivot);
    gl.uniform1f(U.uRoll, p.roll);
    gl.uniform2f(U.uHeadT, ...headT);

    // Blobs: [x, y, sigma] and a displacement each.
    const blobs = [];
    const browLift = (pts, innerIdx) => pts.forEach((pt, i) => {
      const inner = i === innerIdx ? 1 : i === 1 ? 0.4 : 0;
      const lift = p.browUp * 7 + p.brow * 4 + p.browInner * 8 * inner - p.furrow * 4 * inner;
      const toward = sub(F.nose, pt);
      blobs.push([pt, 20, add(mul(up, lift), mul(unit(toward), p.furrow * 3 * inner))]);
    });
    browLift(F.browL, 2);
    browLift(F.browR, 0);
    const cheek = (e) => add(e.bottom, add(mul(up, -30), mul(sub(e.outer, e.inner), 0.15)));
    blobs.push([cheek(F.eyeL), 30, mul(up, Math.max(0, p.smile) * 4.5)]);
    blobs.push([cheek(F.eyeR), 30, mul(up, Math.max(0, p.smile) * 4.5)]);
    const cornerShift = (sign) => add(mul(G.mu, sign * (p.wide * 5 + Math.max(0, p.smile) * 4 - p.round * 8)),
                                      mul(up, p.smile * 7));
    blobs.push([F.mouth.left, G.mw * 0.38, cornerShift(-1)]);
    blobs.push([F.mouth.right, G.mw * 0.38, cornerShift(1)]);
    // Parallax: the features shift a little more than the outline when the head turns or nods.
    blobs.push([F.nose, 110, add(mul(G.faceU, p.yaw * 3.2), mul(up, -(p.pitch * 1.4 + p.nod * 1.8)))]);
    while (blobs.length < NB) blobs.push([[0, 0], 1, [0, 0]]);
    gl.uniform4fv(U.uBlob, blobs.flatMap(([c, s]) => [c[0], c[1], s, 0]));
    gl.uniform2fv(U.uBlobD, blobs.flatMap(([, , d]) => d));

    // Mouth: the jaw opens up to ~16% of the mouth's width.
    const amp = G.mw * 2 * 0.16;
    gl.uniform2f(U.uMO, ...G.mo);
    gl.uniform2f(U.uMU, ...G.mu);
    gl.uniform1f(U.uMW, G.mw);
    gl.uniform1f(U.uSeam, G.seam);
    gl.uniform1f(U.uJaw, Math.max(0, p.jaw) * amp);
    gl.uniform1f(U.uLift, Math.max(0, p.jaw) * amp * 0.18);
    gl.uniform1f(U.uChinV, G.chinV);
    gl.uniform1f(U.uLipL, G.lipL);
    gl.uniform1f(U.uRound, Math.max(0, Math.min(1, p.round)));

    G.eyes.forEach((e, i) => {
      gl.uniform2f(U[`uEO${i}`], ...e.o);
      gl.uniform2f(U[`uEU${i}`], ...e.u);
      gl.uniform3f(U[`uEP${i}`], ...e.p);
      gl.uniform3f(U[`uIris${i}`], ...e.iris);
    });
    gl.uniform1f(U.uBlink, Math.min(1, Math.max(p.blink, 0)));
    gl.uniform1f(U.uSquint, Math.max(0, p.squint));
    gl.uniform2f(U.uGaze, p.gx, p.gy);
    gl.drawArrays(gl.TRIANGLES, 0, 3);
  }

  function loop(now) {
    if (!f.running) return;
    draw(now);
    f.raf = requestAnimationFrame(loop);
  }

  return {
    canvas,
    rig,
    setPortrait,
    setFraming(anchor) { f.anchor = anchor; },
    onReady(fn) { f.onReady = fn; },
    get ready() { return f.ready; },
    start() { if (f.running) return; f.running = true; f.last = 0; f.raf = requestAnimationFrame(loop); },
    stop() { f.running = false; cancelAnimationFrame(f.raf); },
  };
}
