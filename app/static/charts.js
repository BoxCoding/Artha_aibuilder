/* Charts drawn as SVG by hand: the page's strict CSP rules out chart libraries, and these three are
   all the app needs. Colours come from CSS classes, so both themes style them. */

const NS = "http://www.w3.org/2000/svg";
function svg(tag, attrs = {}, parent = null) {
  const node = document.createElementNS(NS, tag);
  Object.entries(attrs).forEach(([k, v]) => node.setAttribute(k, v));
  if (parent) parent.appendChild(node);
  return node;
}
function text(parent, x, y, value, className, anchor = "middle") {
  const node = svg("text", { x, y, class: className, "text-anchor": anchor }, parent);
  node.textContent = value;
  return node;
}

const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
const monthName = (iso) => MONTHS[Number(iso.slice(5, 7)) - 1] || iso;
// ₹1.5L, ₹85k: short enough to sit above a bar.
export function shortRupees(value) {
  const n = Math.abs(value);
  if (n >= 1e7) return `₹${(value / 1e7).toFixed(1)}Cr`;
  if (n >= 1e5) return `₹${(value / 1e5).toFixed(1)}L`;
  if (n >= 1e3) return `₹${Math.round(value / 1e3)}k`;
  return `₹${Math.round(value)}`;
}

/* Probability of default on a half dial (0–20%, beyond is pinned), coloured by risk band. */
export function riskGauge(pd, band) {
  const percent = pd * 100;
  const root = svg("svg", { viewBox: "0 0 220 128", class: "gauge", role: "img",
                            "aria-label": `Estimated default risk ${percent.toFixed(1)}%, band ${band}` });
  const r = 88;
  const arc = `M ${110 - r} 112 A ${r} ${r} 0 0 1 ${110 + r} 112`;
  const length = Math.PI * r;
  svg("path", { d: arc, class: "gauge-track" }, root);
  const value = svg("path", { d: arc, class: `gauge-value band-${band.toLowerCase()}`,
                              "stroke-dasharray": length.toFixed(1), "stroke-dashoffset": length.toFixed(1) }, root);
  const filled = Math.min(percent / 20, 1);
  // Two frames so the starting offset is painted first and the dial sweeps up to the value.
  requestAnimationFrame(() => requestAnimationFrame(() => value.setAttribute("stroke-dashoffset", (length * (1 - filled)).toFixed(1))));
  text(root, 110, 92, `${percent.toFixed(1)}%`, "gauge-num");
  text(root, 110, 110, "default risk", "gauge-cap");
  text(root, 110 - r, 126, "0%", "gauge-tick");
  text(root, 110 + r, 126, "20%", "gauge-tick");
  return root;
}

/* Money in and out per month as paired bars, with the average balance as a line on its own scale. */
export function cashflowChart(monthly) {
  const W = 640, H = 230, top = 22, bottom = 30, left = 8, right = 8;
  const root = svg("svg", { viewBox: `0 0 ${W} ${H}`, class: "chart", role: "img",
                            "aria-label": "Monthly money in, money out and average balance" });
  const peak = Math.max(...monthly.flatMap((m) => [m.inflow, m.outflow]), 1);
  const balancePeak = Math.max(...monthly.map((m) => m.avg_balance), 1);
  const plotH = H - top - bottom;
  const slot = (W - left - right) / monthly.length;
  const barW = Math.min(26, slot * 0.28);
  svg("line", { x1: left, x2: W - right, y1: top + plotH, y2: top + plotH, class: "axis" }, root);
  const points = [];
  monthly.forEach((m, i) => {
    const cx = left + slot * (i + 0.5);
    [[m.inflow, "bar-in", -barW - 2], [m.outflow, "bar-out", 2]].forEach(([amount, cls, dx]) => {
      const h = Math.max((amount / peak) * plotH, 2);
      const bar = svg("rect", { x: (cx + dx).toFixed(1), y: (top + plotH - h).toFixed(1), width: barW.toFixed(1),
                                height: h.toFixed(1), rx: 5, class: `${cls} grow` }, root);
      bar.style.animationDelay = `${i * 70}ms`;
    });
    text(root, cx - barW / 2 - 2, top + plotH - (m.inflow / peak) * plotH - 6, shortRupees(m.inflow), "value-label");
    text(root, cx, H - 10, monthName(m.month), "label");
    points.push([cx, top + plotH - (m.avg_balance / balancePeak) * plotH * 0.92]);
  });
  svg("path", { d: points.map(([x, y], i) => `${i ? "L" : "M"}${x.toFixed(1)} ${y.toFixed(1)}`).join(" "), class: "line" }, root);
  points.forEach(([x, y]) => svg("circle", { cx: x.toFixed(1), cy: y.toFixed(1), r: 3.5, class: "dot" }, root));
  return root;
}

export function chartLegend() {
  const legend = document.createElement("div");
  legend.className = "legend";
  [["in", "Money in"], ["out", "Money out"], ["bal", "Average balance"]].forEach(([cls, label]) => {
    const item = document.createElement("span");
    const swatch = document.createElement("i");
    swatch.className = cls;
    item.append(swatch, label);
    legend.appendChild(item);
  });
  return legend;
}
