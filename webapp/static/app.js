const state = { profile: "developing", task: "retry", run: null, catalog: null };
const $ = (sel, root = document) => root.querySelector(sel);
const slot = (name) => $(`[data-slot="${name}"]`, $("#result"));
const LEVELS = ["exposed", "explain", "predict", "modify", "critique"];

function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") node.className = v;
    else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
    else node.setAttribute(k, v);
  }
  for (const child of children.flat()) {
    if (child != null) node.append(child instanceof Node ? child : document.createTextNode(child));
  }
  return node;
}

async function api(path, body) {
  const res = await fetch(path, body === undefined ? {} : {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
  });
  if (!res.ok) {
    const detail = await res.json().catch(() => ({}));
    throw new Error(detail.detail || `Request failed (${res.status})`);
  }
  return res.json();
}

function live() { return $("#live").checked; }
function profileName(id) { return state.catalog.profiles.find((p) => p.id === id).name; }

// -- pickers ---------------------------------------------------------------

function renderPickers() {
  const { profiles, tasks } = state.catalog;
  $("#profiles").replaceChildren(...profiles.map((p) => el("button", {
    class: "pick", "aria-pressed": String(p.id === state.profile),
    onclick: () => { state.profile = p.id; renderPickers(); loadModel(); },
  }, el("strong", {}, p.name), el("span", {}, p.summary))));
  $("#tasks").replaceChildren(...tasks.map((t) => el("button", {
    class: "pick", "aria-pressed": String(t.id === state.task),
    onclick: () => { state.task = t.id; renderPickers(); },
  }, el("strong", {}, t.title), el("span", {}, `${t.kind === "code" ? "Code" : "Writing"}: ${t.request}`))));
}

// -- learner model -----------------------------------------------------------

function renderModel(model, events = [], changed = new Set()) {
  $("#model-who").textContent = `· ${profileName(state.profile)}`;
  $("#events").replaceChildren(...events.map((e) => {
    const moved = e.to !== e.from;
    const text = moved
      ? `${e.concept.replaceAll("-", " ")}: level ${e.from} → ${e.to} (${LEVELS[e.to]})`
      : `${e.concept.replaceAll("-", " ")}: evidence recorded, level stays ${e.to}`;
    return el("div", { class: `event${e.to < e.from ? " down" : ""}` },
      el("div", {}, el("b", {}, e.label)), el("div", {}, text),
      moved && e.rules.length ? el("div", {}, "Rule: ", ...e.rules.map((r) => el("code", {}, r + " "))) : null);
  }));
  $("#concepts").replaceChildren(...model.concepts.map((c) => el("div", {
    class: `concept${changed.has(c.name) ? " flash" : ""}`,
  },
    el("div", { class: "name" }, c.name),
    el("div", { class: "levels", title: `Level ${c.level} of 4` },
      [1, 2, 3, 4].map((n) => el("i", { class: n <= c.level ? "on" : "" }))),
    el("div", { class: "meta" },
      el("span", {}, `Level ${c.level}: ${c.level_name}`),
      el("span", {}, `${c.track === "skill" ? "skill track" : "one-off"} · ${c.evidence} observation${c.evidence === 1 ? "" : "s"}`)),
    c.changes.length ? el("ul", { class: "changes" }, c.changes.slice(-2).map((ch) =>
      el("li", {}, `${ch.from_level} → ${ch.to_level} by ${ch.rule_id}, ${ch.at.slice(0, 10)}`))) : null,
  )));
  const k = model.criteria;
  const rows = [
    ["A · unassisted uses", k.A_unassisted.unassisted_use_evidence],
    ["B · repeat asks (prior → last 30d)", `${k.B_fewer_repeat_asks.reasks_prior_30d} → ${k.B_fewer_repeat_asks.reasks_last_30d}`],
    ["C · question depth, SOLO (prior → last 30d)", `${k.C_harder_questions.solo_avg_prior_30d ?? "–"} → ${k.C_harder_questions.solo_avg_last_30d ?? "–"}`],
    ["D · critiques + fixes", k.D_critical_engagement.critiques + k.D_critical_engagement.substantive_fixes],
  ];
  $("#criteria").replaceChildren(el("div", { class: "criteria" }, rows.flatMap(([a, b]) => [el("span", {}, a), el("span", {}, String(b))])));
  $("#flags").replaceChildren(...model.flags.map((f) => el("div", { class: "flag" },
    el("b", {}, "Worth a look: "), `${f.concept_id.replaceAll("-", " ")}, ${f.behavior.replaceAll("_", " ")} (${f.suggests}).`)));
}

async function loadModel() {
  renderModel(await api(`/api/model/${state.profile}`));
}

function applyUpdate(data) {
  const changed = new Set((data.events || []).map((e) => e.concept.replaceAll("-", " ")));
  renderModel(data.model, data.events || [], changed);
}

// -- a run -------------------------------------------------------------------

function setLoading(on, text) {
  $("#loading").classList.toggle("hidden", !on);
  if (text) $("#loading-text").textContent = text;
  $("#start").disabled = on;
  document.querySelectorAll("#result button").forEach((b) => (b.disabled = on));
}

function withTimer(text) {
  const start = Date.now();
  setLoading(true, text);
  const timer = setInterval(() => {
    $("#loading-text").textContent = `${text} (${Math.round((Date.now() - start) / 1000)} s)`;
  }, 1000);
  return () => { clearInterval(timer); setLoading(false); };
}

async function start() {
  $("#empty").classList.add("hidden");
  $("#result").classList.add("hidden");
  const done = withTimer(live()
    ? "Claude is doing the task and deciding what to leave for the learner…"
    : "Loading the recorded response…");
  try {
    const run = await api("/api/run", { task_id: state.task, profile: state.profile, live: live() });
    state.run = run;
    renderRun(run);
    applyUpdate(run);
  } catch (err) {
    $("#empty").classList.remove("hidden");
    $("#empty").replaceChildren(el("p", {}, `Something went wrong: ${err.message}`));
  } finally {
    done();
  }
}

// Minimal Markdown for Claude's prose: escaped first, then bold, inline code, bullet lists.
function inline(text) {
  return text.replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c])
    .replace(/`([^`]+)`/g, "<code>$1</code>")
    .replace(/\*\*([^*]+)\*\*/g, "<b>$1</b>");
}

function md(text) {
  const box = document.createElement("div");
  let list = null;
  for (const raw of text.split("\n")) {
    const line = raw.trim();
    const bullet = line.match(/^[-*] (.*)/);
    if (bullet) {
      if (!list) box.append((list = document.createElement("ul")));
      const li = document.createElement("li");
      li.innerHTML = inline(bullet[1]);
      list.append(li);
    } else {
      list = null;
      if (line) {
        const p = document.createElement("p");
        p.innerHTML = inline(line);
        box.append(p);
      }
    }
  }
  return [...box.childNodes];
}

function mdInline(text) {
  const span = document.createElement("span");
  span.innerHTML = inline(text);
  return span;
}

function renderRun(run) {
  const result = $("#result");
  result.replaceChildren($("#tpl-result").content.cloneNode(true));
  result.classList.remove("hidden");
  const name = profileName(run.profile);

  slot("source").textContent = run.source === "live"
    ? "Live response from Claude Opus 5.5."
    : `Recorded live response (instant replay${run.note && run.note !== "recorded mode selected" ? `: ${run.note}` : ""}).`;
  slot("plan").replaceChildren(el("span", { class: "muted" }, `Plan for ${name}:`), ...run.plan.map((c) =>
    el("span", { class: "chip" }, el("b", {}, c.name), ` · level ${c.level} · `,
      c.interventions.length ? c.interventions.join(", ").replaceAll("_", " ") : "no interventions")));

  if (run.orientation) {
    slot("orientation-card").classList.remove("hidden");
    slot("orientation").replaceChildren(...md(run.orientation));
  }
  if (run.prediction_prompt) {
    slot("prediction-card").classList.remove("hidden");
    slot("prediction").replaceChildren(mdInline(run.prediction_prompt));
  }

  slot("draft-title").textContent = run.gaps.length
    ? `Claude's draft, with ${run.gaps.length === 1 ? "a part" : "parts"} left for ${name}`
    : "Claude's draft";
  slot("draft").value = run.scaffold;
  const writing = state.catalog.tasks.find((t) => t.id === run.task_id).kind === "writing";
  slot("draft").classList.toggle("wrap", writing);
  slot("ownership").classList.toggle("wrap", writing);
  slot("gaps").replaceChildren(...run.gaps.map((g) => el("div", { class: "gap", "data-gap": g.gap_id },
    el("div", {}, el("code", {}, `TODO(learner) ${g.gap_id}`), " · ", mdInline(g.description)),
    el("div", { class: "muted" }, "Hint: ", mdInline(g.hint)),
    el("button", { class: "link", onclick: () => reveal(g.gap_id) }, "Show me Claude's version"))));
  if (!run.gaps.length) {
    slot("submit-note").textContent = "No gaps this time. Edit the draft anyway to see how ownership is tracked.";
  }

  slot("decisions").replaceChildren(...run.decision_notes.map((d) => el("li", {}, mdInline(d))));
  if (run.critique_invitation) {
    slot("critique").classList.remove("hidden");
    slot("critique").replaceChildren(el("b", {}, "Push back: "), mdInline(run.critique_invitation));
  }
  slot("takeaway").replaceChildren("Takeaway: ", mdInline(run.takeaway));

  $('[data-action="answer"]', result).addEventListener("click", answer);
  $('[data-action="skip"]', result).addEventListener("click", skip);
  $('[data-action="submit"]', result).addEventListener("click", submit);
}

async function answer() {
  const text = slot("prediction-answer").value.trim();
  if (!text) return;
  const done = withTimer(live() ? "Claude is checking your answer…" : "Recording your answer…");
  try {
    const data = await api(`/api/runs/${state.run.run_id}/predict`, { profile: state.profile, answer: text, live: live() });
    const fb = slot("prediction-feedback");
    fb.classList.remove("hidden");
    fb.replaceChildren(el("span", { class: `badge ${data.outcome === "correct" ? "as_expected" : data.outcome === "partial" ? "different" : "incorrect"}` }, data.outcome), " ", mdInline(data.feedback));
    slot("prediction-card").querySelectorAll("button").forEach((b) => b.remove());
    applyUpdate(data);
  } catch (err) {
    alert(err.message);
  } finally {
    done();
  }
}

async function skip() {
  const data = await api(`/api/runs/${state.run.run_id}/skip`, { profile: state.profile });
  slot("prediction-card").classList.add("hidden");
  applyUpdate(data);
}

async function reveal(gapId) {
  const data = await api(`/api/runs/${state.run.run_id}/reveal`, { profile: state.profile, gap_id: gapId });
  const box = $(`[data-gap="${gapId}"]`, $("#result"));
  box.querySelector("button")?.remove();
  box.append(el("pre", { class: "answer" }, data.answer));
  applyUpdate(data);
}

async function submit() {
  const done = withTimer(live() ? "Claude is comparing your version with the one it would have written…" : "Reviewing…");
  try {
    const data = await api(`/api/runs/${state.run.run_id}/submit`, {
      profile: state.profile, text: slot("draft").value, live: live(),
    });
    slot("review-card").classList.remove("hidden");
    const byId = Object.fromEntries(state.run.gaps.map((g) => [g.gap_id, g]));
    const verdicts = data.gaps.filter((v) => byId[v.gap_id]).map((v) => el("div", { class: "verdict" },
      el("span", { class: `badge ${v.verdict}` }, v.verdict.replaceAll("_", " ")),
      el("span", {}, el("b", {}, `${v.gap_id}: `), mdInline(v.feedback))));
    if (data.other_changes) verdicts.push(el("p", {}, el("b", {}, "Other changes: "), mdInline(data.other_changes)));
    if (data.source !== "live") verdicts.push(el("p", { class: "muted" }, "Offline review (heuristic)."));
    slot("review").replaceChildren(...(verdicts.length ? verdicts : [el("p", { class: "muted" }, "No gaps to review.")]));
    slot("share").textContent = `${Math.round(data.ownership.learner_share * 100)}% of lines are yours or edited by you`;
    slot("ownership").replaceChildren(...data.ownership.lines.map((l) => el("span", { class: l.cls }, l.text || " ")));
    slot("review-card").scrollIntoView({ behavior: "smooth", block: "start" });
    applyUpdate(data);
  } catch (err) {
    alert(err.message);
  } finally {
    done();
  }
}

async function reset() {
  if (!confirm(`Restore ${profileName(state.profile)}'s seeded history?`)) return;
  renderModel(await api("/api/reset", { profile: state.profile }));
}

// -- boot --------------------------------------------------------------------

(async function boot() {
  state.catalog = await api("/api/catalog");
  if (!state.catalog.live_available) {
    $("#live").checked = false;
    $("#live").disabled = true;
    $("#live").parentElement.title = "Live Claude isn't configured on this server; showing recorded responses.";
  }
  renderPickers();
  await loadModel();
  $("#start").addEventListener("click", start);
  $("#reset").addEventListener("click", reset);
})();
