// Relay · CLI Chat Agent frontend. Website-derived text is untrusted: always escaped before rendering.
const $ = (s, r = document) => r.querySelector(s);
const chat = $("#chat"), form = $("#form"), input = $("#input"), sendBtn = $("#send");
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const el = (tag, cls, html) => { const e = document.createElement(tag); if (cls) e.className = cls; if (html !== undefined) e.innerHTML = html; return e; };
const scrollDown = () => { chat.scrollTop = chat.scrollHeight; };
const fmtS = (ms) => (ms / 1000).toFixed(ms < 10000 ? 2 : 1) + " s";
const TOOL_LABEL = { crawl4ai: "crawl4ai", crawl_site: "sitecrawl", check_site: "curl" };
let busy = false, health = null, lastUrl = null;

// ---------------------------------------------------------------- health / sidebar
fetch("/api/health").then((r) => r.json()).then((h) => {
  health = h;
  const llmOn = !h.llm.startsWith("rule");
  $("#modelName").textContent = llmOn ? h.llm.replace(/^.*\((.*)\)$/, "$1").replace(/^gemini/i, "Gemini") : "Rule-based mode";
  $("#modelSub").textContent = llmOn ? "intent + summary · fallback ready" : "no LLM key · extractive summaries";
  $("#modelDot").className = "dot " + (llmOn ? "success" : "partial");
  const order = ["crawl4ai", "crawl_site", "check_site"];
  const desc = { crawl4ai: "Headless Chromium · JS sites", crawl_site: "Python crawler · JSON out", check_site: "HTTP HEAD status check" };
  const name = { crawl4ai: "crawl4ai (crwl)", crawl_site: "sitecrawl", check_site: "curl" };
  $("#tools").innerHTML = order.filter((k) => h.tools[k]).map((k) => `
    <div class="tool" title="${esc(h.tools[k].description)}"><div class="ic">$_</div>
      <div><b>${name[k]}</b><small>${h.tools[k].available ? desc[k] : "not installed · fallback active"}</small></div>
      <span class="dot ${h.tools[k].available ? "success" : "off"}"></span></div>`).join("");
  const ready = order.filter((k) => h.tools[k]?.available).length;
  $("#toolsReady").textContent = `${ready} TOOLS READY`;
  $("#rate").textContent = h.limits.rate_per_min;
  $("#gLimits").textContent = `≤ ${h.limits.max_pages} pages · depth ${h.limits.max_depth}`;
  $("#gTimeout").textContent = `${h.limits.process_timeout_s} s process timeout`;
}).catch(() => {});

// recent runs (per-browser convenience only)
const RUNS_KEY = "relay.runs.v1";
const loadRuns = () => { try { return JSON.parse(localStorage.getItem(RUNS_KEY) || "[]"); } catch { return []; } };
const saveRuns = (r) => { try { localStorage.setItem(RUNS_KEY, JSON.stringify(r.slice(0, 8))); } catch {} };
function renderRuns(activeIdx = -1) {
  const runs = loadRuns(), box = $("#runs");
  if (!runs.length) { box.innerHTML = `<p class="side-empty">Your runs will appear here.</p>`; return; }
  box.innerHTML = runs.map((r, i) => `<button class="run ${i === activeIdx ? "active" : ""}" data-i="${i}" title="${esc(r.msg)}"><span class="dot ${esc(r.status)}"></span><b>${esc(r.title)}</b><small>${esc(r.meta)}</small></button>`).join("");
  box.querySelectorAll(".run").forEach((b) => b.addEventListener("click", () => { input.value = runs[+b.dataset.i].msg; input.focus(); }));
}
function addRun(msg, status, meta) {
  const host = (msg.match(/https?:\/\/([^\s/]+)/) || [])[1];
  const title = /crawl|scrape|summar|extract/i.test(msg) && host ? `Crawl ${host}` : /up\?|status|headers/i.test(msg) && host ? `Is ${host} up?` : msg.slice(0, 40);
  const runs = loadRuns(); runs.unshift({ msg, status, meta, title }); saveRuns(runs); renderRuns(0);
}
renderRuns();

// ---------------------------------------------------------------- composer
const autosize = () => { input.style.height = "auto"; input.style.height = Math.min(input.scrollHeight, 160) + "px"; };
input.addEventListener("input", autosize);
input.addEventListener("keydown", (e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); form.requestSubmit(); } });
document.querySelectorAll(".ex").forEach((b) => b.addEventListener("click", () => { input.value = b.dataset.msg; form.requestSubmit(); }));
document.querySelectorAll(".chip").forEach((c) => c.addEventListener("click", () => {
  const t = c.dataset.insert; input.value = t.startsWith(" ") ? (input.value.trimEnd() + t) : t; input.focus();
  const i = input.value.indexOf("https://"); if (i >= 0 && !t.startsWith(" ")) input.setSelectionRange(i + 8, i + 8); autosize();
}));
$("#newTask").addEventListener("click", () => location.reload());
document.addEventListener("keydown", (e) => { if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "k") { e.preventDefault(); input.focus(); } });
form.addEventListener("submit", (e) => {
  e.preventDefault();
  const msg = input.value.trim();
  if (!msg || busy) return;
  input.value = ""; autosize(); send(msg);
});
function setBusy(b) { busy = b; sendBtn.disabled = b; form.classList.toggle("busy", b); }

// ---------------------------------------------------------------- inspector
const Ins = {
  reset(plan) {
    $("#insRun").textContent = "starting…"; this.badge("running", "live");
    $("#ptCmd").className = "dimc"; $("#ptCmd").textContent = "preparing"; $("#ptMeta").innerHTML = "&nbsp;";
    this.max = { t: health?.limits.process_timeout_s || 90, p: plan?.max_pages || 1, d: plan?.max_depth || 0 };
    this.pages = 0; this.depth = 0; this.meter("Time", 0, this.max.t, "s"); this.meter("Pages", 0, this.max.p); this.meter("Depth", 0, this.max.d);
    document.querySelectorAll(".bar").forEach((b) => b.classList.remove("done"));
    document.querySelectorAll(".check").forEach((c) => { c.className = "check"; c.querySelector("code").textContent = "—"; });
    document.querySelectorAll("#files a").forEach((a) => { a.classList.remove("ready"); a.removeAttribute("href"); });
  },
  badge(kind, label) { const b = $("#insBadge"); b.className = "badge " + kind; b.innerHTML = `<i></i>${esc(label)}`; },
  meter(k, v, max, unit = "") {
    $("#m" + k).textContent = `${unit === "s" ? v.toFixed(1) : v} / ${max}${unit ? " " + unit : ""}`;
    $("#b" + k).style.width = Math.min(100, max ? (v / max) * 100 : 0) + "%";
  },
  check(k, ok, val) { const c = document.querySelector(`.check[data-k="${k}"]`); if (!c) return; c.className = "check " + (ok ? "pass" : "fail"); c.querySelector("b").textContent = ok ? "✓" : "✗"; c.querySelector("code").textContent = val; },
  files(runId) { document.querySelectorAll("#files a").forEach((a) => { a.href = `/api/runs/${runId}/${a.dataset.f}`; a.target = "_blank"; a.classList.add("ready"); }); },
};

// ---------------------------------------------------------------- streaming
async function send(message) {
  setBusy(true);
  $("#welcome")?.remove();
  const host = (message.match(/https?:\/\/([^\s/]+)/) || [])[1];
  $("#crumb").textContent = host ? (/up\?|status|header/i.test(message) ? `Is ${host} up?` : `Crawl ${host}`) : message.slice(0, 48);
  const u = el("div", "msg-user"); const b = el("div", "bubble"); b.textContent = message; u.appendChild(b); chat.appendChild(u);
  const turn = new Turn(message);
  try {
    const res = await fetch("/api/chat", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ message, last_url: lastUrl }) });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      turn.final({ status: "failed", text: typeof err.detail === "string" ? err.detail : `Request rejected (HTTP ${res.status})` });
    } else {
      const reader = res.body.getReader(), dec = new TextDecoder();
      let buf = "";
      while (true) {
        const { value, done } = await reader.read();
        if (done) break;
        buf += dec.decode(value, { stream: true });
        let i;
        while ((i = buf.indexOf("\n\n")) >= 0) {
          const chunk = buf.slice(0, i); buf = buf.slice(i + 2);
          if (chunk.startsWith("data: ")) { try { turn.handle(JSON.parse(chunk.slice(6))); } catch (e) { console.error(e); } }
        }
      }
    }
  } catch (e) {
    turn.final({ status: "failed", text: "Connection to the server failed: " + e.message });
  } finally {
    turn.stopTimer(); setBusy(false); input.focus();
  }
}

const STEPS = ["Understand", "Validate URL", "Run CLI", "Verify output", "Respond"];

class Turn {
  constructor(message) {
    this.message = message;
    this.root = el("div", "turn"); this.root.innerHTML = `<div class="avatar">&gt;_</div>`;
    this.stack = el("div", "stack"); this.root.appendChild(this.stack); chat.appendChild(this.root);
    this.exec = null; this.runId = null; this.timer = null; this.done = false; this.notes = [];
    scrollDown();
  }
  handle(ev) {
    const h = { plan: "plan", status: "status", job: "job", exec_start: "execStart", log: "log", exec_end: "execEnd",
      summary_start: "summaryStart", summary_delta: "summaryDelta", final: "final" }[ev.type];
    if (h) this[h](ev);
  }

  // ---- plan
  plan({ plan: p }) {
    this.planData = p;
    if (p.url && !p.error) lastUrl = p.url;
    if (p.intent === "chat" || p.intent === "unsupported") return;
    Ins.reset(p);
    const card = el("div", "card");
    const params = p.intent === "crawl_site"
      ? [["URL", p.url], ["PAGES", "max " + p.max_pages], ["DEPTH", p.max_depth], ["OUTPUT", [p.want_summary && "summary", p.want_json && "JSON"].filter(Boolean).join(" + ")], ["ROBOTS", "respected"]]
      : [["URL", p.url], ["REQUEST", "HEAD"], ["OUTPUT", "status + JSON"]];
    if (p.focus) params.push(["FOCUS", p.focus]);
    card.innerHTML = `<div class="card-head"><span class="label">UNDERSTOOD REQUEST</span><span class="pill green">${esc(p.intent)}</span><span class="right">interpreted by ${esc(p.parser_note || p.parser)}</span></div>
      <div class="params">${params.map(([k, v]) => `<span class="param" title="${esc(v)}"><b>${k}</b>${esc(v ?? "-")}</span>`).join("")}</div>
      <div class="stepper">${STEPS.map((s, i) => `<div class="step"><div class="ring">${i + 1}</div>${s}</div>`).join("")}</div>`;
    this.steps = card.querySelectorAll(".step");
    this.stack.appendChild(card);
    this.step(1);
    if (p.error) { this.step(1, true); Ins.check("url", false, "rejected"); }
    else if (p.url) { this.step(2); Ins.check("url", true, "pass"); Ins.check("ip", true, "public"); }
    scrollDown();
  }
  step(active, fail = false) {
    if (!this.steps) return;
    this.steps.forEach((s, i) => {
      s.classList.toggle("done", i < active || (active >= STEPS.length));
      s.classList.toggle("active", i === active && !fail && active < STEPS.length);
      s.classList.toggle("fail", i === active && fail);
      s.querySelector(".ring").textContent = i < active || active >= STEPS.length ? "✓" : i === active && fail ? "✗" : i + 1;
    });
  }

  // ---- notes (crawler choice, fallback)
  status({ message }) {
    if (/robots\.txt pre-check/i.test(message)) { Ins.check("robots", true, "pre-checked"); return; }
    if (this.exec) this.line("sys", "[app] " + message);
    if (/fall|not installed/i.test(message)) { const n = el("div", "note"); n.textContent = message; (this.exec?.card || this.stack).appendChild(n); }
    if (/verified/i.test(message)) this.step(4);
  }
  job({ run_id }) { this.runId = run_id; $("#insRun").textContent = run_id; }

  // ---- execution card (one per attempt)
  execStart(ev) {
    this.step(2);
    const card = el("div", "card");
    const [bin, ...rest] = ev.argv;
    const shown = rest.map((a) => (/[\s%{}=']/.test(a) ? `'${a}'` : a)).join(" ");
    const crawl = ev.tool !== "check_site";
    card.innerHTML = `<div class="accent" style="background:var(--cobalt)"></div>
      <div class="card-head"><span class="label">EXECUTING CLI</span><span class="badge running state"><i></i>running</span><span class="pill">${esc(TOOL_LABEL[ev.tool] || ev.tool)}</span><button class="stop" type="button">■ Stop</button></div>
      <div class="cmd"><span class="p">$</span><span class="bin">${esc(bin.split(/[\\/]/).pop())}</span> ${esc(shown)}<button class="copy" type="button">copy</button></div>
      <p class="why">${esc(ev.why)}</p>
      <div class="meta"><div><span>PID</span><code>${ev.pid}</code></div><div><span>ELAPSED</span><code class="elapsed">0.0 s</code></div>
        <div><span>TIMEOUT</span><code>${ev.timeout_s} s</code></div><div><span>SHELL</span><code>none (argv)</code></div>
        ${crawl ? `<div class="prog"><span class="label pl">PAGES 0 / ${this.planData?.max_pages ?? "?"}</span><div class="track ${ev.tool === "crawl4ai" ? "indeterminate" : ""}"><i></i></div></div>` : ""}</div>
      <div class="terminal"><div class="term-tabs"><span class="on">live output</span><span>stderr + stdout</span></div><div class="term-body"></div></div>`;
    card.querySelector(".copy").addEventListener("click", (e) => { navigator.clipboard?.writeText(ev.argv.join(" ")); e.target.textContent = "copied"; });
    card.querySelector(".stop").addEventListener("click", () => this.cancel());
    this.stack.appendChild(card);
    this.exec = { card, term: card.querySelector(".term-body"), tool: ev.tool, pages: 0, started: Date.now(), timeout: ev.timeout_s };
    $("#ptCmd").className = "live"; $("#ptCmd").textContent = `${bin.split(/[\\/]/).pop()} ${ev.tool === "crawl_site" ? "cli/sitecrawl.py" : ""}`.trim();
    $("#ptMeta").textContent = `pid ${ev.pid} · running`;
    Ins.check("env", true, "stripped");
    this.stopTimer();
    const elapsed = card.querySelector(".elapsed");
    this.timer = setInterval(() => { const s = (Date.now() - this.exec.started) / 1000; elapsed.textContent = s.toFixed(1) + " s"; Ins.meter("Time", s, this.exec.timeout, "s"); }, 100);
    scrollDown();
  }
  async cancel() { if (!this.runId) return; this.line("sys", "[app] cancellation requested…"); await fetch(`/api/cancel/${this.runId}`, { method: "POST" }).catch(() => {}); }
  line(cls, text) {
    if (!this.exec) return;
    const t = new Date(); const ts = t.toTimeString().slice(0, 8) + "." + String(t.getMilliseconds()).padStart(3, "0");
    const ln = el("div", "ln " + cls); ln.innerHTML = `<span class="ts">${ts}</span><span></span>`; ln.lastChild.textContent = text;
    this.exec.term.appendChild(ln); this.exec.term.scrollTop = this.exec.term.scrollHeight;
  }
  log(ev) {
    if (!this.exec) return;
    const p = ev.parsed;
    if (ev.stream === "system") return this.line("sys", "[system] " + ev.line);
    if (!p) {
      if (this.exec.tool === "crawl4ai") {
        const last = this.exec.term.lastElementChild;
        if (/^\s*\|/.test(ev.line) && last) { last.lastChild.textContent += " " + ev.line.trim(); return; }
        const tag = (ev.line.match(/^\[(\w+)\]/) || [])[1];
        if (tag === "COMPLETE") this.bumpPages();
        const cls = tag === "COMPLETE" ? "ok" : tag === "FETCH" ? "fetch" : tag === "ERROR" ? "err" : "out";
        return this.line(cls, ev.line);
      }
      return this.line(ev.stream === "stdout" ? "out" : "", "[stderr] " + ev.line);
    }
    const pr = this.exec.card.querySelector(".pl"), bar = this.exec.card.querySelector(".track i");
    switch (p.event) {
      case "start": return this.line("", `[start] ${p.tool} v${p.version} → ${p.url} (max ${p.max_pages} pages, depth ${p.max_depth})`);
      case "robots": Ins.check("robots", true, ({ loaded: "allowed", not_found_allow_all: "no robots.txt", unavailable_allow_all: "unreachable", ignored: "ignored", access_denied_treated_as_disallow: "denied" })[p.status] || p.status); return this.line("", `[robots] ${p.status}${p.url ? " · " + p.url : ""}`);
      case "fetch": Ins.depth = Math.max(Ins.depth, p.depth); Ins.meter("Depth", Ins.depth, Ins.max.d); return this.line("fetch", `→ GET ${p.url}   page ${p.n}/${p.max} · depth ${p.depth}`);
      case "redirect": return this.line("", `  ↪ redirect → ${p.to}`);
      case "page_ok":
        this.bumpPages();
        return this.line("ok", `  ✓ ${p.title || "(no title)"} · ${p.words} words · ${p.ms} ms`);
      case "page_error": return this.line("err", `  ✗ ${p.url}: ${p.error}`);
      case "skip": return this.line("", `  – skipped ${p.url} (${p.reason})`);
      case "done": return this.line("ok", `[done] ${p.pages} page(s) crawled, ${p.errors} error(s) → ${p.status}`);
      case "output": return this.line("", `[output] result JSON written (${p.bytes} bytes)`);
      case "error": Ins.check("robots", !/robots/.test(p.message), /robots/.test(p.message) ? "blocked" : "ok"); return this.line("err", `[error] ${p.message}`);
      default: return this.line("", ev.line);
    }
  }
  bumpPages(setTo) {
    if (!this.exec) return;
    this.exec.pages = setTo ?? this.exec.pages + 1;
    const max = this.planData?.max_pages || 1;
    Ins.pages = this.exec.pages; Ins.meter("Pages", Ins.pages, Ins.max.p);
    const pr = this.exec.card.querySelector(".pl"), bar = this.exec.card.querySelector(".track i");
    if (pr) pr.textContent = `PAGES ${this.exec.pages} / ${max}`;
    if (bar) bar.style.width = Math.min(100, (this.exec.pages / max) * 100) + "%";
  }
  execEnd(ev) {
    this.stopTimer();
    if (!this.exec) { const c = el("div", "card"); c.innerHTML = `<div class="card-head"><span class="label">EXECUTING CLI</span><span class="badge failed"><i></i>not started</span></div><div class="errbox">${esc(ev.error || "Process could not be started")}</div>`; this.stack.appendChild(c); return; }
    const card = this.exec.card;
    card.querySelector(".stop")?.remove();
    const st = ev.timed_out ? "timeout" : ev.cancelled ? "cancelled" : ev.exit_code === 0 ? "success" : "failed";
    const badge = card.querySelector(".state"); badge.className = "badge state " + st; badge.innerHTML = `<i></i>${st === "success" ? "exited" : st}`;
    card.querySelector(".accent").style.background = st === "success" ? "var(--green)" : st === "failed" ? "var(--red)" : "var(--amber)";
    card.querySelector(".elapsed").textContent = fmtS(ev.duration_ms);
    const tr = card.querySelector(".track"); if (tr) { tr.classList.remove("indeterminate"); tr.querySelector("i").style.width = "100%"; tr.querySelector("i").style.background = st === "success" ? "var(--green)" : "var(--red)"; }
    const end = el("div", "endbar"); end.innerHTML = `<span>exit code <b>${ev.exit_code ?? "-"}</b> (${esc(ev.exit_meaning || "")})</span><span>·</span><span>${fmtS(ev.duration_ms)}</span>`;
    card.appendChild(end);
    $("#ptCmd").className = st === "success" ? "done" : "bad"; $("#ptMeta").textContent = `exit ${ev.exit_code ?? "-"} · ${fmtS(ev.duration_ms)}`;
    document.querySelectorAll(".bar").forEach((b) => b.classList.add("done"));
    this.step(3);
  }
  stopTimer() { if (this.timer) clearInterval(this.timer); this.timer = null; }

  // ---- streaming summary
  summaryStart({ llm }) {
    this.result = this.makeResult();
    const pane = this.result.querySelector('[data-pane="summary"]');
    pane.innerHTML = llm ? `<p class="summary-text streaming"></p>` : `<div class="skeleton" style="width:92%"></div><div class="skeleton" style="width:78%"></div><div class="skeleton" style="width:85%"></div>`;
    this.streamEl = pane.querySelector(".summary-text"); this.streamBuf = "";
    scrollDown();
  }
  summaryDelta({ text }) {
    if (!this.streamEl) return;
    this.streamBuf += text;
    const buf = this.streamBuf.replace(/\b(SITE TYPE|FOCUS|SUSPICIOUS)\s*:[^\n]*/gi, "");
    const cut = buf.search(/KEY POINTS|^\s*[-•*]\s+/im);
    this.streamEl.textContent = (cut >= 0 ? buf.slice(0, cut) : buf).trim();
    if (cut >= 0) {
      const pts = buf.slice(cut).split("\n").filter((l) => /^\s*[-•*]\s+\S/.test(l)).map((l) => l.replace(/^\s*[-•*]\s*/, ""));
      let ul = this.streamEl.parentElement.querySelector(".points"); if (!ul) { ul = el("ul", "points"); this.streamEl.after(ul); }
      ul.innerHTML = pts.map((p) => `<li>${esc(p)}</li>`).join("");
    }
    scrollDown();
  }
  makeResult() {
    const card = el("div", "card result");
    card.innerHTML = `<div class="accent" style="background:var(--cobalt)"></div>
      <div class="card-head"><span class="badge running rbadge"><i></i>summarising</span><span class="right rby"></span></div>
      <div class="headline">Writing summary from verified CLI output…</div><div class="stats"></div>
      <div class="tabs"><button class="on" data-t="summary">Summary</button><button data-t="json">JSON</button><button data-t="pages">Pages</button><button data-t="raw">Raw CLI output</button></div>
      <div class="pane on" data-pane="summary"></div><div class="pane" data-pane="json"></div><div class="pane" data-pane="pages"></div><div class="pane" data-pane="raw"></div>`;
    card.querySelectorAll(".tabs button").forEach((b) => b.addEventListener("click", () => {
      card.querySelectorAll(".tabs button").forEach((x) => x.classList.toggle("on", x === b));
      card.querySelectorAll(".pane").forEach((p) => p.classList.toggle("on", p.dataset.pane === b.dataset.t));
    }));
    this.stack.appendChild(card);
    return card;
  }

  // ---- final
  final(ev) {
    if (this.done) return; this.done = true; this.stopTimer();
    const ok = ev.status === "success" || ev.status === "partial";
    const env = ev.json, exec = env?.execution;
    if (!this.steps && (ev.status === "info" || ev.status === "refused")) {
      const b = el("div", "bot-bubble"); b.textContent = ev.text;
      if (ev.status === "refused") { b.prepend(el("span", "badge refused", "<i></i>refused"), document.createTextNode("  ")); Ins.badge("refused", "refused"); }
      this.stack.appendChild(b);
      addRun(this.message, ev.status === "refused" ? "refused" : "success", ev.status === "refused" ? "refused · not an approved tool" : "answered · no tool needed");
      scrollDown(); return;
    }
    this.step(ok ? STEPS.length : (exec ? 3 : 1), !ok);
    if (!exec) { $("#insRun").textContent = "no process started"; $("#ptCmd").className = "bad"; $("#ptCmd").textContent = "no process started"; $("#ptMeta").textContent = "rejected by validation / policy"; }
    const card = this.result || this.makeResult();
    const accent = { success: "var(--green)", partial: "var(--amber)", refused: "var(--plum)", cancelled: "var(--dim)" }[ev.status] || "var(--red)";
    card.querySelector(".accent").style.background = accent;
    const badge = card.querySelector(".rbadge"); badge.className = "badge rbadge " + ev.status; badge.innerHTML = `<i></i>${esc(ev.status.replace("_", " "))}`;
    Ins.badge(ev.status, ev.status.replace("_", " "));
    const tabs = card.querySelector(".tabs");
    if (!ok) {
      card.querySelector(".headline").textContent = exec ? "Task did not complete" : "Rejected before any process started";
      card.querySelector(".stats").remove();
      const sum = card.querySelector('[data-pane="summary"]');
      sum.innerHTML = `<div class="errbox">${esc(ev.text || "The task failed.")}</div><p class="byline">${exec ? "No data is shown because the CLI did not produce a verified successful result." : "Input validation failed, so no CLI was started."}</p>`;
      tabs.querySelector('[data-t="summary"]').textContent = "Error";
      tabs.querySelector('[data-t="pages"]').remove();
      if (!exec) tabs.querySelector('[data-t="raw"]').remove();
      addRun(this.message, ev.status, exec ? `${ev.status} · exit ${exec.exit_code ?? "-"} · ${TOOL_LABEL[exec.tool] || exec.tool}` : "rejected · " + (ev.text || "").replace(/^I couldn't start the task: /, "").slice(0, 40));
    } else {
      const sm = ev.summary || {}, data = env?.data || {};
      if (data.stats && this.exec) this.bumpPages(data.stats.pages_crawled);
      if (data.pages?.length) { const d = Math.max(...data.pages.map((p) => p.depth || 0)); Ins.meter("Depth", d, Ins.max.d); }
      card.querySelector(".headline").textContent = ev.headline || "Completed";
      card.querySelector(".rby").textContent = sm.generated_by === "llm" ? `Summary by ${health?.llm?.replace(/^.*\((.*)\)$/, "$1") || "LLM"}` : "Extractive summary (no LLM)";
      const stats = card.querySelector(".stats");
      if (data.stats) {
        const failed = data.stats.pages_failed;
        stats.innerHTML = [["PAGES CRAWLED", data.stats.pages_crawled, failed ? `${failed} failed` : "all succeeded"], ["WORDS EXTRACTED", Number(data.stats.total_words).toLocaleString(), `via ${TOOL_LABEL[exec?.tool] || "-"}`],
          ["DURATION", fmtS(exec?.duration_ms || 0), `limit ${health?.limits.process_timeout_s || 90} s`], ["EXIT CODE", exec?.exit_code ?? "-", exec?.exit_meaning || ""]]
          .map(([k, v, s], i) => `<div class="stat"><span>${k}</span><b class="${i === 3 ? (exec?.exit_code === 0 ? "good" : "bad") : ""}">${esc(v)}</b><small>${esc(s)}</small></div>`).join("");
      } else if (data.http_status !== undefined) {
        stats.innerHTML = [["HTTP STATUS", data.http_status ?? "-", data.reason || ""], ["RESPONSE TIME", data.time_total_s != null ? data.time_total_s + " s" : "-", "curl time_total"], ["SERVER", data.server || "-", data.content_type || ""], ["EXIT CODE", exec?.exit_code ?? "-", exec?.exit_meaning || ""]]
          .map(([k, v, s], i) => `<div class="stat"><span>${k}</span><b class="${i === 3 ? (exec?.exit_code === 0 ? "good" : "bad") : ""}" style="${String(v).length > 10 ? "font-size:16px" : ""}">${esc(v)}</b><small>${esc(s)}</small></div>`).join("");
      } else stats.remove();
      const sumPane = card.querySelector('[data-pane="summary"]');
      sumPane.innerHTML = `<p class="summary-text">${esc(sm.text)}</p>${sm.key_points?.length ? `<ul class="points">${sm.key_points.map((k) => `<li>${esc(k)}</li>`).join("")}</ul>` : ""}
          ${sm.focus_findings ? `<div class="focus"><b>Focus:</b> ${esc(sm.focus_findings)}</div>` : ""}
          ${sm.suspicious_content_detected ? `<div class="warnbox">⚠ <b>Possible prompt injection ignored.</b> A crawled page contained instruction-like text. Website content is treated as untrusted data and cannot trigger commands.</div>` : ""}
          ${sm.note ? `<p class="byline">${esc(sm.note)}</p>` : ""}${sm.site_type ? `<p class="byline">Site type: ${esc(sm.site_type)}</p>` : ""}`;
      sumPane.appendChild(renderStructured(data));
      if (data.pages?.length) {
        card.querySelector('[data-pane="pages"]').innerHTML = `<table class="pages"><thead><tr><th>#</th><th>STATUS</th><th>URL</th><th>TITLE</th><th>WORDS</th></tr></thead><tbody>
          ${data.pages.map((p, i) => `<tr><td>${i + 1}</td><td><span class="code ${p.status_code >= 300 && p.status_code < 400 ? "redir" : ""}">${p.status_code ?? 200}</span></td><td class="url"><a href="${esc(p.url)}" target="_blank" rel="noopener noreferrer">${esc(p.url)}</a></td><td>${esc(p.title || "-")}</td><td>${p.word_count ?? ""}</td></tr>`).join("")}
          ${(data.errors || []).map((e) => `<tr><td>✗</td><td><span class="code bad">ERR</span></td><td class="url">${esc(e.url)}</td><td colspan="2" style="color:var(--red-text)">${esc(e.error)}</td></tr>`).join("")}</tbody></table>`;
        tabs.querySelector('[data-t="pages"]').textContent = `Pages (${data.pages.length})`;
      } else tabs.querySelector('[data-t="pages"]')?.remove();
      Ins.check("untrusted", true, sm.suspicious_content_detected ? "injection flagged" : "on");
      const meta = data.stats ? `${TOOL_LABEL[exec?.tool]} · ${data.stats.pages_crawled}/${data.stats.pages_crawled + (data.errors?.length || 0)} pages · ${fmtS(exec?.duration_ms || 0)}` : `${TOOL_LABEL[exec?.tool]} · HTTP ${data.http_status ?? "-"} · ${fmtS(exec?.duration_ms || 0)}`;
      addRun(this.message, ev.status, meta);
    }
    if (env) {
      const pretty = JSON.stringify(env, null, 2);
      const jp = card.querySelector('[data-pane="json"]');
      jp.innerHTML = `<div class="json-bar"><span class="pill green">✓ valid JSON</span><span style="flex:1"></span><button class="btn cp" type="button">Copy</button><button class="btn dl" type="button">Download .json</button></div><pre class="json">${highlight(pretty)}</pre>`;
      jp.querySelector(".cp").addEventListener("click", (e) => { navigator.clipboard?.writeText(pretty); e.target.textContent = "Copied ✓"; });
      jp.querySelector(".dl").addEventListener("click", () => { const a = document.createElement("a"); a.href = URL.createObjectURL(new Blob([pretty], { type: "application/json" })); a.download = `${exec?.run_id || "relay-result"}.json`; a.click(); });
      if (ok && ev.show_summary === false) card.querySelector('[data-t="json"]').click();
    } else tabs.querySelector('[data-t="json"]')?.remove();
    if (exec?.run_id) {
      const rp = card.querySelector('[data-pane="raw"]');
      if (rp) rp.innerHTML = `<p class="byline" style="margin-top:0">Evidence saved by the backend for run <code>${esc(exec.run_id)}</code>${exec.attempts?.length > 1 ? ` · attempts: ${exec.attempts.map((a) => `${TOOL_LABEL[a.tool]} (${a.outcome})`).join(" → ")}` : ""}</p>
        <div class="raw-links">${["command", "stdout", "stderr", "exit", "raw", "response"].map((f) => `<a href="/api/runs/${exec.run_id}/${f}" target="_blank">${{ command: "command.json", stdout: "stdout.log", stderr: "stderr.log", exit: "exit.json", raw: "raw CLI output", response: "response.json" }[f]} ↗</a>`).join("")}</div>`;
      Ins.files(exec.run_id); $("#insRun").textContent = exec.run_id;
    }
    scrollDown();
  }
}

function renderStructured(data) {
  const box = el("div", "structured");
  const items = data.extracted_items || [], ps = data.price_stats;
  if (ps) {
    const c = ps.currency || "";
    box.insertAdjacentHTML("beforeend", `<div class="chips-row">
      <span class="kchip"><small>ITEMS WITH PRICE</small><b>${ps.items_with_price}</b></span>
      <span class="kchip"><small>LOWEST</small><b>${esc(ps.cheapest.price)}</b><em>${esc(ps.cheapest.name)}</em></span>
      <span class="kchip"><small>HIGHEST</small><b>${esc(ps.most_expensive.price)}</b><em>${esc(ps.most_expensive.name)}</em></span>
      <span class="kchip"><small>AVERAGE</small><b>${esc(c)}${ps.average.toFixed(2)}</b></span></div>`);
  }
  if (items.length) {
    const hasPrice = items.some((i) => i.price), hasDet = items.some((i) => i.details);
    const sec = el("div", "items-sec");
    sec.innerHTML = `<div class="sec-head"><span class="label">EXTRACTED ITEMS (${items.length})</span><span class="src">from CLI output · not generated by the LLM</span></div>
      <table class="items"><thead><tr><th>#</th><th class="sort" data-k="name">ITEM \u2195</th>${hasPrice ? `<th class="sort num" data-k="price_value">PRICE \u2195</th>` : ""}${hasDet ? "<th>DETAILS</th>" : ""}<th>PAGE</th></tr></thead><tbody></tbody></table>
      ${items.length > 10 ? `<button class="btn more" type="button">Show all ${items.length}</button>` : ""}`;
    let rows = items.slice(), showAll = false, dir = {};
    const tb = sec.querySelector("tbody");
    const draw = () => {
      tb.innerHTML = (showAll ? rows : rows.slice(0, 10)).map((i, n) => `<tr><td>${n + 1}</td><td class="iname">${esc(i.name)}</td>
        ${hasPrice ? `<td class="num price">${esc(i.price || "\u2014")}</td>` : ""}${hasDet ? `<td class="det">${esc(i.details || "")}</td>` : ""}
        <td class="pg"><a href="${esc(i.page)}" target="_blank" rel="noopener noreferrer">${esc((i.page || "").replace(/^https?:\/\/[^/]+/, "") || "/")}</a></td></tr>`).join("");
    };
    sec.querySelectorAll("th.sort").forEach((th) => th.addEventListener("click", () => {
      const k = th.dataset.k; dir[k] = -(dir[k] || -1);
      rows.sort((a, b) => ((a[k] ?? Infinity) > (b[k] ?? Infinity) ? 1 : (a[k] ?? Infinity) < (b[k] ?? Infinity) ? -1 : 0) * dir[k]);
      draw();
    }));
    sec.querySelector(".more")?.addEventListener("click", (e) => { showAll = !showAll; e.target.textContent = showAll ? "Show fewer" : `Show all ${items.length}`; draw(); });
    draw(); box.appendChild(sec);
  } else {
    const hl = [];
    (data.pages || []).forEach((p) => (p.key_paragraphs || []).forEach((t) => { if (hl.length < 8 && !hl.some((h) => h.t === t)) hl.push({ t, u: p.url }); }));
    if (hl.length) {
      const sec = el("div", "items-sec");
      sec.innerHTML = `<div class="sec-head"><span class="label">HIGHLIGHTS FROM THE PAGES</span><span class="src">verbatim from CLI output</span></div>
        <div class="hl">${hl.map((h) => `<blockquote>${esc(h.t)}<a href="${esc(h.u)}" target="_blank" rel="noopener noreferrer">${esc((h.u || "").replace(/^https?:\/\/[^/]+/, "") || "/")}</a></blockquote>`).join("")}</div>`;
      box.appendChild(sec);
    }
  }
  return box;
}

function highlight(json) {
  return esc(json).replace(/(&quot;(?:\\u[a-fA-F0-9]{4}|\\[^u]|[^\\&]|&(?!quot;))*?&quot;)(\s*:)?|\b(true|false)\b|\bnull\b|-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?/g, (m, str, colon, bool) => {
    if (str) return colon ? `<span class="j-key">${str}</span>${colon}` : `<span class="j-str">${str}</span>`;
    if (bool) return `<span class="j-bool">${m}</span>`;
    if (m === "null") return `<span class="j-null">${m}</span>`;
    return `<span class="j-num">${m}</span>`;
  });
}
