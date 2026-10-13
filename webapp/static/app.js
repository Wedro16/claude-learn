const state = { profile: "developing", task: "retry", run: null, catalog: null, runSeq: 0 };
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
    onclick: () => { if (p.id === state.profile) return; state.profile = p.id; clearRun(); renderPickers(); loadModel(); },
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
  const profile = state.profile;
  const model = await api(`/api/model/${profile}`);
  if (profile === state.profile) renderModel(model);  // a later selection wins
}

// Runs belong to one learner's sandbox; switching or resetting the learner ends the run.
function clearRun() {
  state.run = null;
  state.final = null;
  state.runSeq += 1;
  $("#result").classList.add("hidden");
  $("#result").replaceChildren();
  $("#events").replaceChildren();
  $("#empty").classList.remove("hidden");
  setLoading(false);
}

// True when a response still belongs to the run on screen.
function current(runId) {
  return state.run && state.run.run_id === runId;
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
  // done(false) stops the timer without touching a newer run's loading state.
  return (finish = true) => { clearInterval(timer); if (finish) setLoading(false); };
}

async function start() {
  $("#empty").classList.add("hidden");
  $("#result").classList.add("hidden");
  const seq = ++state.runSeq;
  const profile = state.profile;
  const done = withTimer(live()
    ? "Claude is doing the task and deciding what to leave for the learner…"
    : "Loading the recorded response…");
  try {
    const run = await api("/api/run", { task_id: state.task, profile, live: live() });
    if (seq !== state.runSeq || profile !== state.profile) return;  // superseded while in flight
    state.run = run;
    renderRun(run);
    applyUpdate(run);
  } catch (err) {
    if (seq !== state.runSeq) return;
    $("#empty").classList.remove("hidden");
    $("#empty").replaceChildren(el("p", {}, `Something went wrong: ${err.message}`));
  } finally {
    done(seq === state.runSeq);
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
    : `Recorded live response (instant replay).${run.note && run.note !== "recorded mode selected" ? ` ${run.note}` : ""}`;
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

  slot("chat-input").placeholder = writing
    ? "e.g. I led with the Redis trade-off because a reviewer will ask about it first…"
    : "e.g. I capped the delay because without a cap, attempt 10 would wait over 4 minutes…";
  slot("chat-input").addEventListener("keydown", (e) => {
    if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) send();
  });
  $('[data-action="send"]', result).addEventListener("click", send);
  $('[data-action="answer"]', result).addEventListener("click", answer);
  $('[data-action="skip"]', result).addEventListener("click", skip);
  $('[data-action="submit"]', result).addEventListener("click", submit);
  $('[data-action="finish"]', result).addEventListener("click", finishCheck);
  $('[data-action="keep-editing"]', result).addEventListener("click", () => slot("finish-card").classList.add("hidden"));
  $('[data-action="continue"]', result).addEventListener("click", () => {
    $('[data-step="explain"]', result).classList.remove("hidden");
    $('[data-action="continue"]', result).parentElement.classList.add("hidden");
    slot("teach-a").focus();
  });
  $('[data-action="explain"]', result).addEventListener("click", explainBack);
  $('[data-action="skip-explain"]', result).addEventListener("click", () => complete());
  $('[data-action="copy"]', result).addEventListener("click", copyWork);
  $('[data-action="download"]', result).addEventListener("click", downloadWork);
  $('[data-action="other-learner"]', result).addEventListener("click", () => { clearRun(); window.scrollTo({ top: 0, behavior: "smooth" }); });
  $('[data-action="new-task"]', result).addEventListener("click", () => { clearRun(); window.scrollTo({ top: 0, behavior: "smooth" }); });
}

async function answer() {
  const text = slot("prediction-answer").value.trim();
  if (!text) return;
  const runId = state.run.run_id;
  const done = withTimer(live() ? "Claude is checking your answer…" : "Recording your answer…");
  try {
    const data = await api(`/api/runs/${runId}/predict`, { profile: state.profile, answer: text, live: live() });
    if (!current(runId)) return;
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
  const runId = state.run.run_id;
  const data = await api(`/api/runs/${runId}/skip`, { profile: state.profile });
  if (!current(runId)) return;
  slot("prediction-card").classList.add("hidden");
  applyUpdate(data);
}

const RECORDED_AS = {
  explanation: (d) => `your explanation (${d.understanding})`,
  question: (d) => `a question (SOLO level ${d.solo})`,
  pushback: (d) => (d.pushback_valid ? "pushback Claude agreed with" : "pushback (Claude explained the trade-off)"),
};

async function send() {
  const input = slot("chat-input");
  const message = input.value.trim();
  if (!message) return;
  const runId = state.run.run_id;
  const chat = slot("chat");
  const button = $('[data-action="send"]', $("#result"));
  input.value = "";
  button.disabled = true;
  chat.append(el("div", { class: "msg you" }, message));
  const pending = el("div", { class: "msg claude pending" }, live() ? "Claude is reading your draft…" : "…");
  chat.append(pending);
  try {
    const data = await api(`/api/runs/${runId}/discuss`, {
      profile: state.profile, message, text: slot("draft").value, live: live(),
    });
    if (!current(runId)) return;
    const recorded = RECORDED_AS[data.kind]?.(data);
    pending.className = "msg claude";
    pending.replaceChildren(...md(data.reply),
      recorded ? el("div", { class: "tag" }, `Recorded ${recorded} · ${data.concept}`) : null);
    applyUpdate(data);
  } catch (err) {
    if (!current(runId)) return;
    pending.className = "msg claude";
    pending.replaceChildren(el("p", {}, err.message));
    input.value = message;
  } finally {
    button.disabled = false;
  }
}

async function reveal(gapId) {
  const runId = state.run.run_id;
  const data = await api(`/api/runs/${runId}/reveal`, { profile: state.profile, gap_id: gapId });
  if (!current(runId)) return;
  const box = $(`[data-gap="${gapId}"]`, $("#result"));
  box.querySelector("button")?.remove();
  box.append(el("pre", { class: "answer" }, data.answer));
  applyUpdate(data);
}

async function submit() {
  const runId = state.run.run_id;
  const done = withTimer(live() ? "Claude is comparing your version with the one it would have written…" : "Reviewing…");
  try {
    const data = await api(`/api/runs/${runId}/submit`, {
      profile: state.profile, text: slot("draft").value, live: live(),
    });
    if (!current(runId)) return;
    slot("review-card").classList.remove("hidden");
    const byId = Object.fromEntries(state.run.gaps.map((g) => [g.gap_id, g]));
    const verdicts = data.gaps.filter((v) => byId[v.gap_id]).map((v) => el("div", { class: "verdict" },
      el("span", { class: `badge ${v.verdict}` }, v.verdict.replaceAll("_", " ")),
      el("span", {}, el("b", {}, `${v.gap_id}: `), mdInline(v.feedback))));
    if (data.other_changes) verdicts.push(el("p", {}, el("b", {}, "Other changes: "), mdInline(data.other_changes)));
    if (data.source !== "live") verdicts.push(el("p", { class: "muted" }, "Offline review: only exact matches earn credit; anything else waits for a live review."));
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

// -- finishing -----------------------------------------------------------------

async function finishCheck() {
  const runId = state.run.run_id;
  const text = slot("draft").value;
  const done = withTimer(live() ? "Claude is giving your version a final read…" : "Checking…");
  try {
    const data = await api(`/api/runs/${runId}/finish/check`, { profile: state.profile, text, live: live() });
    if (!current(runId)) return;
    state.final = { text, check: data };
    const card = slot("finish-card");
    card.classList.remove("hidden");
    $('[data-step="explain"]', card).classList.add("hidden");
    $('[data-action="continue"]', card).parentElement.classList.remove("hidden");
    const blocking = data.issues.filter((i) => i.severity === "blocking");
    slot("check").replaceChildren(
      el("p", {}, el("span", { class: `badge ${data.ready ? "as_expected" : "incorrect"}` }, data.ready ? "ready to use" : "has a problem"), " ",
        data.ready ? "Nothing blocking." : "You can fix it first, or finish anyway: the work is yours to ship."),
      ...(data.issues.length ? [el("ul", {}, data.issues.map((i) => el("li", {}, el("b", {}, `${i.severity}: `), mdInline(i.description))))] : []),
    );
    $('[data-action="continue"]', card).textContent = blocking.length ? "Finish anyway" : "Continue";
    slot("teach-q").replaceChildren(mdInline(data.teach_back_question));
    card.scrollIntoView({ behavior: "smooth", block: "start" });
  } catch (err) {
    alert(err.message);
  } finally {
    done();
  }
}

async function explainBack() {
  const answer = slot("teach-a").value.trim();
  if (!answer) return;
  const runId = state.run.run_id;
  const done = withTimer(live() ? "Claude is reading your explanation…" : "Recording…");
  try {
    const data = await api(`/api/runs/${runId}/finish/explain`, { profile: state.profile, answer, live: live() });
    if (!current(runId)) return;
    const fb = slot("teach-fb");
    fb.classList.remove("hidden");
    fb.replaceChildren(el("span", { class: `badge ${data.outcome === "correct" ? "as_expected" : data.outcome === "partial" ? "different" : "incorrect"}` }, data.outcome), " ", mdInline(data.feedback));
    applyUpdate(data);
  } catch (err) {
    alert(err.message);
    return;
  } finally {
    done();
  }
  await complete();
}

function summarize(data) {
  const parts = [];
  const share = Math.round(data.ownership.learner_share * 100);
  parts.push(el("h5", {}, "What you built"),
    el("p", {}, `${share}% of the lines in your final version are yours or edited by you.`),
    el("p", { class: "legend" }, el("span", { class: "claude" }, "Claude"), el("span", { class: "claude_modified" }, "Claude, edited by you"), el("span", { class: "human" }, "You")),
    el("pre", { class: `ownership${state.catalog.tasks.find((t) => t.id === state.run.task_id).kind === "writing" ? " wrap" : ""}` },
      data.ownership.lines.map((l) => el("span", { class: l.cls }, l.text || " "))));
  parts.push(el("h5", {}, "What changed in your learner model"));
  parts.push(data.changes.length
    ? el("ul", {}, data.changes.map((c) => el("li", {}, `${c.concept}: level ${c.from} → ${c.to} (${LEVELS[c.to]})`)))
    : el("p", {}, `No level changes this time, which is normal for a single session. ${data.observations} piece${data.observations === 1 ? "" : "s"} of evidence recorded.`));
  const TEACH_BACK = {
    correct: "Your explanation at the end was right, and it counts as evidence of understanding.",
    partial: "Your explanation at the end was partly right. It's logged, but only a fully correct explanation moves a level, so Claude will come back to the missing piece.",
    incorrect: "Your explanation at the end missed the key idea. Claude will revisit it next time.",
  };
  parts.push(el("p", {}, TEACH_BACK[data.teach_back] || "You skipped explaining it back. That's fine; it's logged, not counted against you."));
  if (data.rules.length) {
    parts.push(el("p", { class: "muted" }, "Rules that fired: ", ...data.rules.map((r) => el("code", {}, `${r.rule_id} `))));
  }
  parts.push(el("h5", {}, "What's next"), el("ul", {}, data.upcoming.map((u) => el("li", {},
    el("b", {}, u.concept), ` · level ${u.level} (${u.level_name}). `,
    u.track === "skill"
      ? (u.review_on ? `Review due ${u.review_on}. ` : "Review scheduled once you've shown some understanding. ")
      : "One-off topic: no review scheduled unless it comes up again. ",
    `Next time Claude will lead with: ${u.next_plan.join(", ").replaceAll("_", " ") || "no interventions"}.`))));
  return parts;
}

async function complete() {
  const runId = state.run.run_id;
  const done = withTimer("Wrapping up…");
  try {
    const data = await api(`/api/runs/${runId}/finish/complete`, { profile: state.profile, text: state.final.text });
    if (!current(runId)) return;
    state.final.result = data;
    const card = slot("finish-card");
    for (const step of ["summary", "take"]) $(`[data-step="${step}"]`, card).classList.remove("hidden");
    $('[data-action="explain"]', card).parentElement.classList.add("hidden");
    slot("summary").replaceChildren(...summarize(data));
    slot("download-label").textContent = `Download ${data.file.name}`;
    const note = JSON.stringify(data.commit.note, null, 2).replace(/\[\s+(\d+),\s+(\d+)\s+\]/g, "[$1, $2]");
    slot("commit").textContent = `${data.commit.message}\n\n# git notes --ref=claude-attribution\n${note}`;
    lockRun();
    renderModel(data.model);
    $('[data-step="summary"]', card).scrollIntoView({ behavior: "smooth", block: "start" });
  } catch (err) {
    alert(err.message);
  } finally {
    done();
  }
}

// A finished run is read-only: the summary and the file are what remain.
function lockRun() {
  const result = $("#result");
  slot("draft").readOnly = true;
  for (const action of ["submit", "finish", "send", "answer", "skip", "continue", "keep-editing"]) {
    $(`[data-action="${action}"]`, result)?.classList.add("hidden");
  }
  slot("chat-input").classList.add("hidden");
  slot("discuss-card").classList.toggle("hidden", !slot("chat").children.length);
  result.querySelectorAll(".gap button").forEach((b) => b.remove());
  slot("submit-note").textContent = "Finished. This run is read-only.";
}

async function copyWork() {
  try {
    await navigator.clipboard.writeText(state.final.result.file.content);
    slot("copy-note").textContent = "Copied.";
  } catch {
    slot("copy-note").textContent = "Couldn't copy; use Download instead.";
  }
}

function downloadWork() {
  const { name, content } = state.final.result.file;
  const url = URL.createObjectURL(new Blob([content], { type: "text/plain" }));
  const link = el("a", { href: url, download: name });
  document.body.append(link);
  link.click();
  link.remove();
  URL.revokeObjectURL(url);
}

async function reset() {
  if (!confirm(`Restore ${profileName(state.profile)}'s seeded history?`)) return;
  const profile = state.profile;
  clearRun();
  const model = await api("/api/reset", { profile });
  if (profile === state.profile) renderModel(model);
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
