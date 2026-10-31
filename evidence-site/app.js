/* AutoHeal Evaluation & Evidence. Read-only: renders data/evidence.js and makes no network requests.
   Recorded text is always inserted as text, never as HTML. */
(function () {
  "use strict";

  var DATA = window.AUTOHEAL_EVIDENCE;
  if (!DATA) {
    var failure = document.createElement("p");
    failure.className = "error";
    failure.textContent = "The evidence data could not be loaded. Expected data/evidence.js next to this page.";
    document.getElementById("overview").appendChild(failure);
    return;
  }

  var GROUPS = DATA.groups;
  var ORDER = ["recovered", "correct_escalation", "correct_noop", "recovered_and_escalated", "guardrail_stop", "wrong_diagnosis", "detection_failure"];
  var NOT_CORRECT = { guardrail_stop: 1, wrong_diagnosis: 1, detection_failure: 1 };
  var STAGE_NAMES = { Outcome: "Resolve / Escalate" };
  var GLYPH = { completed: "✓", active: "•", failed: "×", blocked: "!", escalated: "!", skipped: "–", pending: "" };
  var EVENT_TONES = { success: "success", info: "info", warning: "safety", danger: "failure" };
  // Hidden from "Key steps" only: read-only investigation, model routing, notes and passing checks. Every event stays under "All activity".
  var SUPPORTING_EVENTS = { observe: 1, route: 1, thought: 1, nudge: 1 };

  var state = { group: GROUPS[0].key, view: "explore", allGroup: GROUPS[0].key, allActivity: false, x: { group: GROUPS[0].key, caseId: null, number: 1, engine: "agent" } };

  // -------------------------------------------------------------------- helpers
  function h(tag, attrs) {
    var node = document.createElement(tag);
    Object.keys(attrs || {}).forEach(function (k) {
      var v = attrs[k];
      if (v === null || v === undefined || v === false) return;
      if (k === "class") node.className = v;
      else if (k === "text") node.textContent = v;
      else if (k.slice(0, 2) === "on") node.addEventListener(k.slice(2), v);
      else node.setAttribute(k, v === true ? "" : v);
    });
    for (var i = 2; i < arguments.length; i++) add(node, arguments[i]);
    return node;
  }
  function add(node, child) {
    if (child === null || child === undefined || child === false) return;
    if (Array.isArray(child)) child.forEach(function (c) { add(node, c); });
    else node.appendChild(child.nodeType ? child : document.createTextNode(String(child)));
  }
  function fill(id) {
    var node = typeof id === "string" ? document.getElementById(id) : id;
    node.textContent = "";
    for (var i = 1; i < arguments.length; i++) add(node, arguments[i]);
    return node;
  }
  function group(key) { return GROUPS.filter(function (g) { return g.key === key; })[0]; }
  function caseOf(g, id) { return g.cases.filter(function (c) { return c.id === id; })[0]; }
  function runsOf(g, caseId) { return g.runs.filter(function (r) { return r.case === caseId; }); }
  function plural(n, noun) { return n + " " + noun + (n === 1 ? "" : "s"); }
  function usd(v) { return "$" + (v === 0 ? "0" : v.toFixed(4)); }
  function seconds(v) { return v < 0.1 ? "< 0.1 s" : (v < 10 ? v.toFixed(1) : Math.round(v)) + " s"; }
  function ratio(p) { return p ? p[0] + "/" + p[1] : null; }

  // Shared tooltip: hover or keyboard focus, on anything with data-tip.
  var tip = document.getElementById("tip");
  function showTip(e) {
    var host = e.target.closest ? e.target.closest("[data-tip]") : null;
    if (!host) { tip.hidden = true; return; }
    tip.textContent = host.getAttribute("data-tip");
    tip.hidden = false;
    var box = host.getBoundingClientRect();
    var left = Math.min(Math.max(8, box.left), window.innerWidth - tip.offsetWidth - 8);
    var top = box.top - tip.offsetHeight - 6;
    tip.style.left = left + "px";
    tip.style.top = (top < 8 ? box.bottom + 6 : top) + "px";
  }
  document.addEventListener("mouseover", showTip);
  document.addEventListener("focusin", showTip);
  document.addEventListener("mouseout", function () { tip.hidden = true; });
  document.addEventListener("focusout", function () { tip.hidden = true; });

  function segmented(id, options, current, onPick) {
    fill(id, options.map(function (o) {
      return h("button", { type: "button", "aria-pressed": String(o[0] === current), text: o[1], onclick: function () { onPick(o[0]); } });
    }));
  }

  // -------------------------------------------------------------------- overview
  function outcomeBar(summary) {
    var total = summary.total;
    var parts = ORDER.filter(function (k) { return summary.outcomes[k]; }).map(function (k) {
      var n = summary.outcomes[k], o = DATA.outcomes[k];
      return h("div", { class: "seg-bar tone-" + o.tone, style: "flex:" + n + " " + n + " 0", tabindex: "0",
                        "data-tip": o.label + ": " + plural(n, "run") + (NOT_CORRECT[k] ? " (not correct)" : " (correct)"),
                        "aria-label": o.label + ": " + plural(n, "run"), text: n });
    });
    return h("div", { class: "bar", role: "group", "aria-label": "Outcomes across " + total + " runs" }, parts);
  }
  function scoreBlock(summary) {
    return h("div", { class: "score" },
      h("div", { class: "score-head" },
        h("span", { class: "score-name", text: summary.name }),
        h("span", { class: "score-value", text: summary.correct + "/" + summary.total }),
        h("span", { class: "score-note", text: summary.pct + "% correct outcomes. 95% interval " + summary.ci[0] + "–" + summary.ci[1] + "%." })),
      outcomeBar(summary));
  }
  function renderOverview() {
    var p = GROUPS[0], a = p.agent, b = p.playbook;
    var tied = p.cases.filter(function (c) { return c.category === "tied"; });
    fill("answer", "In the prospective evaluation, the AI Agent reached the correct outcome in " + a.correct + " of " + a.total +
      " recorded runs and the rule-based Playbook in " + b.correct + " of " + b.total + ". All " + p.paired.agent_only +
      " runs that only the AI Agent got right come from three scenario types disclosed in advance as favourable to it. On the " + plural(tied.length, "fault scenario") + " where both were correct, the Playbook was far faster and free of API costs. " +
      "This shows where AI adds value. It does not show general superiority.");

    fill("group-cards", GROUPS.map(function (g) {
      return h("article", { class: "card" },
        h("span", { class: "card-tag", text: g.role }),
        h("h3", { text: g.name }),
        h("p", { class: "muted small", text: g.summary }),
        scoreBlock(g.agent), scoreBlock(g.playbook));
    }));

    function legendGroup(title, keys) {
      return h("div", { class: "legend-group" }, h("span", { class: "legend-title", text: title }), keys.map(function (k) {
        var o = DATA.outcomes[k];
        return h("span", { class: "legend-item tone-" + o.tone, tabindex: "0", "data-tip": o.meaning },
          h("span", { class: "swatch" }), o.label);
      }));
    }
    fill("outcome-legend",
      legendGroup("Correct:", ORDER.filter(function (k) { return !NOT_CORRECT[k]; })),
      legendGroup("Not correct:", ORDER.filter(function (k) { return NOT_CORRECT[k]; })));

    fill("facts-title", "Across the " + p.name);
    function tile(label, value, caption) {
      return h("div", { class: "tile" }, h("div", { class: "l", text: label }), h("div", { class: "v", text: value }), h("div", { class: "c", text: caption }));
    }
    fill("key-facts",
      tile("Faults detected", ratio(a.detection), "The same for both: they share one detector"),
      tile("False positives", String(a.false_positives[0] + b.false_positives[0]), "On " + a.false_positives[1] + " healthy control runs each"),
      tile("Unsafe actions executed", String(a.unsafe_executed + b.unsafe_executed),
           "Playbook attempted " + b.unsafe_attempted + ", both refused"),
      tile("Time to finish", seconds(a.latency.mean_s) + " vs " + seconds(b.latency.max_s), "AI Agent mean, across runs that called a model, vs Playbook"),
      tile("API cost", usd(a.cost_usd) + " vs " + usd(b.cost_usd), "Across all recorded runs in this evaluation"));
  }

  // ------------------------------------------------------------------- comparison
  function resultCell(engine) {
    var mark = engine.correct === engine.total ? "✓" : (engine.correct ? "•" : "×");
    return h("div", { class: "result tone-" + engine.tone },
      h("span", { class: "result-label", text: engine.outcome_label + (engine.mixed ? " (mixed)" : "") }),
      h("span", { class: "result-score" }, mark + " ", h("b", { text: engine.correct + " of " + engine.total }), " correct"));
  }
  function caseTags(c) {
    var tags = [h("span", { class: "id-tag", "data-tip": "Original scenario identifier", text: c.id })];
    if (c.leaning === "favours agent") tags.push(h("span", { class: "tag", text: "Disclosed as favourable to the AI Agent" }));
    if (c.leaning === "favours playbook") tags.push(h("span", { class: "tag", text: "Disclosed as favourable to the Playbook" }));
    if (c.category === "shared_failure") tags.push(h("span", { class: "tag", text: "Shared failure" }));
    return tags;
  }
  function openRun(groupKey, caseId, number, engine) {
    state.x = { group: groupKey, caseId: caseId, number: number, engine: engine };
    state.view = "explore";
    renderRuns();
    history.replaceState(null, "", "#run/" + caseId + "-" + number + "-" + engine);
    document.getElementById("runs").scrollIntoView();
  }
  function runLabel(n) { return "Recorded Run " + n; }

  function renderMatrix(g) {
    fill("matrix-body", g.cases.map(function (c) {
      return h("tr", {},
        h("td", {}, h("button", { type: "button", class: "row-link", onclick: function () { openRun(g.key, c.id, 1, "agent"); } },
          h("span", { class: "case-title", text: c.title }), h("span", { class: "meta" }, caseTags(c)))),
        h("td", { "data-label": "AI Agent" }, resultCell(c.agent)),
        h("td", { "data-label": "Rule-based Playbook" }, resultCell(c.playbook)));
    }));
  }
  function renderInsights(g) {
    function list(cases, line) { return h("ul", {}, cases.map(function (c) { return h("li", {}, h("strong", { text: c.title }), line(c)); })); }
    var diff = g.cases.filter(function (c) { return c.category === "difference"; });
    var tied = g.cases.filter(function (c) { return c.category === "tied"; });
    var shared = g.cases.filter(function (c) { return c.category === "shared_failure"; });
    var isProspective = g.key === "prospective";
    fill("insights",
      h("article", { class: "card" }, h("h3", { text: "Where AI made a meaningful difference" }),
        list(diff, function (c) { return ": " + c.agent.outcome_label.toLowerCase() + ", against " + c.playbook.outcome_label.toLowerCase() + " for the Playbook."; }),
        h("p", { class: "muted small", text: isProspective
          ? "All of these were disclosed before running as favourable to the AI Agent, so the lead does not show general superiority."
          : "The system was improved after seeing these scenarios, so this shows regression safety rather than general ability." })),
      h("article", { class: "card" }, h("h3", { text: "Where rules were enough" }),
        h("ul", {}, tied.map(function (c) { return h("li", { text: c.title }); })),
        h("p", { class: "muted small", text: "Both systems were fully correct here. The Playbook finished in " + seconds(g.playbook.latency.max_s) + " with no API cost." })),
      h("article", { class: "card" }, h("h3", { text: "Shared failures" }),
        shared.length
          ? list(shared, function () { return ": not detected by either system. The shared detector needs strictly rising queue depth, so neither engine was invoked."; })
          : h("p", { class: "muted small", text: "Neither system failed on a scenario the other also failed." })));
  }
  function renderMetrics(g) {
    var a = g.agent, b = g.playbook, NA = "Not recorded";
    function row(label, x, y, tipText) {
      return h("tr", {}, h("th", { scope: "row" }, tipText ? h("span", { tabindex: "0", "data-tip": tipText, text: label, style: "border-bottom:1px dotted #5A6A84" }) : label),
        h("td", { class: "num", text: x === null || x === undefined ? NA : x }), h("td", { class: "num", text: y === null || y === undefined ? NA : y }));
    }
    fill("metrics-body",
      row("Correct outcomes", a.correct + "/" + a.total, b.correct + "/" + b.total, "A recovery, a correct escalation, a partial recovery with escalation, or correctly doing nothing."),
      row("Faults detected", ratio(a.detection), ratio(b.detection), "Fault runs where a health check raised a symptom. Both systems share one detector."),
      row("False positives on healthy controls", a.false_positives ? ratio(a.false_positives) : null, b.false_positives ? ratio(b.false_positives) : null),
      row("Accepted diagnosis", ratio(a.diagnosis), ratio(b.diagnosis), "Runs where an engine was invoked and named an accepted cause."),
      row("Correct escalation where required", ratio(a.escalation), ratio(b.escalation)),
      row("Unsafe actions executed", a.unsafe_executed, b.unsafe_executed),
      row("Unsafe actions attempted and refused", a.unsafe_attempted, b.unsafe_attempted),
      row("Unrelated changes executed", a.irrelevant_executed, b.irrelevant_executed),
      row("Invalid tool calls", a.invalid_tool_calls, b.invalid_tool_calls),
      row("Mean time to finish", seconds(a.latency.mean_s) + " (" + Math.round(a.latency.min_s) + "–" + Math.round(a.latency.max_s) + " s)", seconds(b.latency.max_s)),
      row("Mean steps", a.steps.mean + " (up to " + a.steps.max + ")", b.steps.min + "–" + b.steps.max + " per run"),
      row("API cost, whole evaluation", usd(a.cost_usd), usd(b.cost_usd)),
      row("API cost per correct outcome", usd(a.cost_per_correct), usd(b.cost_per_correct)));
    fill("metrics-note", g.key === "prospective"
      ? "AI Agent time and steps cover the runs that called a model. Playbook API cost is zero because it makes no model calls."
      : "“Not recorded” means this group's result file has no such field. Its healthy control is shown in the table above.");
  }
  function renderInvalid(g) {
    var invalid = [];
    g.runs.forEach(function (r) {
      (r.raw.invalid_tool_calls || []).forEach(function (call) { invalid.push({ run: r, call: call }); });
    });
    document.getElementById("invalid-panel").hidden = !invalid.length;
    fill("invalid-list", invalid.map(function (item) {
      var message = item.call.error;
      try { message = JSON.parse(message).error || message; } catch (e) { /* stored text stays as it is */ }
      return h("div", {},
        h("button", { type: "button", class: "link", text: caseOf(g, item.run.case).title + ", " + runLabel(item.run.number), onclick: function () { openRun(g.key, item.run.case, item.run.number, item.run.engine); } }),
        h("pre", { text: message }));
    }));
  }
  function renderLatency(g) {
    var W = 880, rowsY = { agent: 34, playbook: 84 }, left = 152, right = 18, bottom = 118;
    var runs = g.runs.filter(function (r) { return r.invoked; });
    var max = Math.ceil(Math.max.apply(null, runs.map(function (r) { return r.latency_s; })) / 50) * 50;
    var x = function (v) { return left + (W - left - right) * v / max; };
    var ns = "http://www.w3.org/2000/svg";
    function s(tag, attrs, text) {
      var n = document.createElementNS(ns, tag);
      Object.keys(attrs).forEach(function (k) { n.setAttribute(k, attrs[k]); });
      if (text !== undefined) n.textContent = text;
      return n;
    }
    var svg = s("svg", { viewBox: "0 0 " + W + " 146", role: "img", "aria-label": "Seconds to finish for each recorded run where an engine was invoked" });
    for (var t = 0; t <= max; t += 50) {
      svg.appendChild(s("line", { x1: x(t), x2: x(t), y1: 12, y2: bottom, stroke: "#25324A", "stroke-width": 1 }));
      svg.appendChild(s("text", { x: x(t), y: bottom + 16, fill: "#A9B5C6", "font-size": 12, "text-anchor": "middle" }, t + " s"));
    }
    ["agent", "playbook"].forEach(function (engine) {
      svg.appendChild(s("text", { x: 0, y: rowsY[engine] + 4, fill: "#E6EBF2", "font-size": 13, "font-weight": 600 }, g[engine].name));
      runs.filter(function (r) { return r.engine === engine; }).forEach(function (r) {
        var c = caseOf(g, r.case);
        var pt = s("circle", { class: "pt", cx: x(r.latency_s), cy: rowsY[engine], r: 7, fill: engine === "agent" ? "#5EC4C0" : "#A9B5C6",
                               stroke: "#131C2E", "stroke-width": 2, tabindex: 0, role: "button",
                               "data-tip": c.title + ", " + runLabel(r.number) + ": " + seconds(r.latency_s),
                               "aria-label": c.title + ", " + runLabel(r.number) + ", " + seconds(r.latency_s) });
        var go = function () { openRun(g.key, r.case, r.number, r.engine); };
        pt.addEventListener("click", go);
        pt.addEventListener("keydown", function (e) { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); go(); } });
        svg.appendChild(pt);
      });
    });
    fill("latency-chart", svg);
  }
  function renderRunsTable(g) {
    fill("runs-body", g.runs.slice().sort(function (x, y) {
      return x.case < y.case ? -1 : x.case > y.case ? 1 : x.number - y.number || (x.engine < y.engine ? -1 : 1);
    }).map(function (r) {
      return h("tr", {},
        h("td", {}, h("button", { type: "button", class: "link", text: caseOf(g, r.case).title, onclick: function () { openRun(g.key, r.case, r.number, r.engine); } })),
        h("td", { text: r.number }), h("td", { text: r.engine_name }), h("td", { text: r.outcome_label }), h("td", { text: r.correct ? "Yes" : "No" }),
        h("td", { class: "num", text: r.steps }), h("td", { class: "num", text: r.tool_calls }), h("td", { class: "num", text: r.executed }),
        h("td", { class: "num", text: r.invoked ? seconds(r.latency_s) : "–" }), h("td", { class: "num", text: usd(r.cost_usd) }));
    }));
  }
  function renderComparison() {
    var g = group(state.group);
    segmented("compare-group", GROUPS.map(function (x) { return [x.key, x.name]; }), state.group, function (k) { state.group = k; renderComparison(); });
    // The main group's description sits directly above in the overview; the note marks the switch to the other group.
    var note = fill("compare-note", g.key === GROUPS[0].key ? "" : g.summary);
    note.hidden = g.key === GROUPS[0].key;
    renderMatrix(g); renderInsights(g); renderMetrics(g);
  }

  // ------------------------------------------------------------------- explorer
  function pretty(value) { return typeof value === "string" ? value : JSON.stringify(value, null, 2); }
  function isKeyEvent(e) { return !SUPPORTING_EVENTS[e.kind] && !(e.kind === "action" && e.stage === "Decide"); }
  function eventNode(e) {
    var parts = [STAGE_NAMES[e.stage] || e.stage];
    if (e.step) parts.push("Step " + e.step);
    if (e.tool) parts.push("Tool: " + e.tool);
    if (e.model) parts.push("Model: " + e.model);
    var body = [h("div", { class: "ev-facts", text: parts.join(" · ") })];
    if (e.args !== undefined) body.push(h("div", { class: "pre-label", text: "Arguments" }), h("pre", { text: pretty(e.args) }));
    if (e.result !== undefined && e.kind !== "thought") {
      body.push(h("div", { class: "pre-label", text: "Recorded result" }), h("pre", { text: pretty(e.result) }));
      if (e.truncated) body.push(h("p", { class: "trunc-note", text: "This result was stored cut to " + DATA.trace_result_limit.toLocaleString("en-US") + " characters. The rest was not recorded." }));
    }
    var tone = "ev tone-" + (EVENT_TONES[e.tone] || "info");
    var row = [h("span", { class: "ev-dot" }),
      h("span", {}, h("span", { class: "ev-t", text: e.title }), e.summary ? h("span", { class: "ev-s", text: e.summary }) : null),
      h("span", { class: "ev-stage", text: STAGE_NAMES[e.stage] || e.stage })];
    if (parts.length === 1 && body.length === 1) return h("div", { class: tone }, h("div", { class: "ev-row" }, row));  // nothing more was recorded
    return h("details", { class: tone }, h("summary", { class: "ev-row" }, row), h("div", { class: "ev-body" }, body));
  }
  function verdict(g, r) {
    var meaning = DATA.outcomes[r.outcome].meaning;
    var explain = r.outcome === "wrong_diagnosis" && r.status === "resolved"
      ? meaning + " The run closed itself as resolved and passed verification; the scoring is stricter than that."
      : meaning;
    return h("div", { class: "verdict tone-" + r.tone, role: "status" },
      h("div", { class: "t", text: r.outcome_label }),
      h("div", { class: "e", text: explain }),
      h("div", { class: "s" }, r.correct ? h("span", { class: "mark-ok", text: "✓ Scored correct" }) : h("span", { class: "mark-no", text: "× Scored not correct" }),
        " for this scenario."));
  }
  function stepper(stages) {
    return h("div", { class: "stepper", role: "list", "aria-label": "Recorded lifecycle" }, stages.map(function (s) {
      return h("div", { class: "step " + s.state, role: "listitem" },
        h("div", { class: "dot", "aria-hidden": "true", text: GLYPH[s.state] }),
        h("div", { class: "n", text: s.name }), h("div", { class: "s", text: s.state_label }), s.detail ? h("div", { class: "d", text: s.detail }) : null);
    }));
  }
  function scenarioBrief(c) {
    return h("div", { class: "brief" },
      h("div", {}, h("h4", { text: "What happens" }), h("p", { text: c.what })),
      h("div", {}, h("h4", { text: "Correct result" }), h("p", { text: c.correct })),
      c.why ? h("div", {}, h("h4", { text: "Why the results differ" }), h("p", { text: c.why })) : null,
      c.bias ? h("div", {}, h("h4", { text: "Bias disclosed before running" }), h("p", { text: c.bias })) : null);
  }
  function runStats(r) {
    return h("dl", { class: "stats" }, [["Steps", r.steps], ["Tool calls", r.tool_calls], ["Actions executed", r.executed],
      ["Time", r.invoked ? seconds(r.latency_s) : "–"], ["API cost", usd(r.cost_usd)], ["Model calls", r.llm_requests]].map(function (s) {
      return h("div", {}, h("dt", { text: s[0] }), h("dd", { text: s[1] }));
    }));
  }
  function kvTable(pairs) {
    return h("table", { class: "kv" }, h("tbody", {}, pairs.map(function (p) { return h("tr", {}, h("th", { scope: "row", text: p[0] }), h("td", { text: p[1] })); })));
  }
  function technical(g, r) {
    var rawBox = h("div", {});
    var file = g.source.file.split("/").pop();
    var show = h("button", { type: "button", class: "btn", text: "Show original record", onclick: function () {
      var showing = !!rawBox.firstChild;
      fill(rawBox, showing ? null : h("pre", { text: JSON.stringify(r.raw, null, 2) }));
      show.textContent = showing ? "Show original record" : "Hide original record";
    } });
    return h("details", { class: "panel" },
      h("summary", {}, "Technical details and original record"),
      kvTable([["Scenario identifier", r.case], ["Original seed", r.seed], ["Engine identifier", r.raw.agent], ["Recorded outcome", r.outcome], ["Recorded status", r.status],
               ["Original scenario title", r.raw.title], ["Source file", file], ["Position in its results list", r.source_index]]),
      h("div", { class: "actions" }, show,
        h("button", { type: "button", class: "btn", text: "Download this record", onclick: function () {
          var blob = new Blob([JSON.stringify(r.raw, null, 2)], { type: "application/json" });
          var link = h("a", { href: URL.createObjectURL(blob), download: "autoheal-" + r.case + "-" + r.seed + "-" + r.raw.agent + ".json" });
          document.body.appendChild(link); link.click(); link.remove(); URL.revokeObjectURL(link.href);
        } })),
      rawBox);
  }
  function runBody(g, r) {
    var key = r.events.filter(isKeyEvent);
    var trimmed = key.length < r.events.length;
    var shown = trimmed && !state.allActivity ? key : r.events;
    var truncated = shown.filter(function (e) { return e.truncated; }).length;
    var notes = [];
    if (r.engine === "playbook") notes.push("Rule-based recovery makes no model calls and does not investigate. Any model name in its original record is routing bookkeeping.");
    if (!r.invoked && r.fault_present) notes.push("The fault was present, but no health check raised a symptom, so nothing was invoked. There are no tool calls to show.");
    if (!r.invoked && !r.fault_present) notes.push("The system was healthy and no symptom was raised, so nothing was invoked. There are no tool calls to show.");
    if (shown.some(function (e) { return e.title.indexOf("Guarded action requested") === 0; })) {
      notes.push("Each “Guarded action requested” entry and its result come from the same recorded step: the guard's decision and the result were stored together.");
    }
    if (shown.some(function (e) { return e.kind === "thought"; })) notes.push("Agent notes are message text the model returned with its tool calls. Private model reasoning was never recorded.");
    if (truncated) notes.push(plural(truncated, "result") + " in this run " + (truncated === 1 ? "was" : "were") + " stored cut to " + DATA.trace_result_limit.toLocaleString("en-US") + " characters, and " + (truncated === 1 ? "is" : "are") + " shown as stored.");

    var feed = h("div", {});
    shown.forEach(function (e) { add(feed, eventNode(e)); });
    var modes = h("div", { class: "seg", role: "group", "aria-label": "Activity shown" });
    if (trimmed) segmented(modes, [["key", "Key steps"], ["all", "All activity"]], state.allActivity ? "all" : "key", function (k) { state.allActivity = k === "all"; renderExplorer(); });
    return [
      scenarioBrief(caseOf(g, r.case)),
      verdict(g, r),
      runStats(r),
      stepper(r.lifecycle),
      h("div", { class: "run-grid" },
        h("div", {},
          h("div", { class: "feed-head" }, h("h3", { text: "Recorded activity, in order" }), trimmed ? modes : null),
          trimmed ? h("p", { class: "muted small", text: "Showing " + shown.length + " of " + plural(r.events.length, "recorded event") + "." }) : null,
          feed,
          notes.map(function (n) { return h("p", { class: "muted small", style: "margin-top:.5rem", text: n }); })),
        h("aside", { class: "side" },
          h("h3", { text: "Symptoms at the start" }),
          r.symptoms.length ? h("ul", { class: "side-text" }, r.symptoms.map(function (s) { return h("li", { text: s }); }))
            : h("p", { class: "side-text", text: r.fault_present ? "None raised, although a fault was present." : "None. The system was healthy." }),
          r.model_path ? h("p", { class: "muted small", text: r.model_path + "." }) : null,
          h("h3", { text: "How it was scored" }),
          kvTable(r.scoring.map(function (s) { return [s.label, s.value]; })),
          r.escalation && r.escalation.summary ? h("div", {}, h("h3", { text: "Escalation summary" }), h("p", { class: "side-text", text: r.escalation.summary })) : null)),
      technical(g, r)];
  }
  function renderExplorer() {
    var x = state.x, g = group(x.group);
    if (!x.caseId || !caseOf(g, x.caseId)) x.caseId = g.cases[0].id;
    var numbers = runsOf(g, x.caseId).map(function (r) { return r.number; }).filter(function (n, i, all) { return all.indexOf(n) === i; }).sort();
    if (numbers.indexOf(x.number) < 0) x.number = numbers[0];

    // The two evaluation groups stay separate inside one scenario list.
    var select = fill("x-case", GROUPS.map(function (o) {
      return h("optgroup", { label: o.name }, o.cases.map(function (c) { return h("option", { value: c.id, text: c.title }); }));
    }));
    select.value = x.caseId;
    segmented("x-run", numbers.map(function (n) { return [n, "Run " + n]; }), x.number, function (n) { x.number = n; renderExplorer(); });
    segmented("x-engine", [["agent", "AI Agent"], ["playbook", "Rule-based Playbook"]], x.engine, function (k) { x.engine = k; renderExplorer(); });

    var run = g.runs.filter(function (r) { return r.case === x.caseId && r.number === Number(x.number) && r.engine === x.engine; })[0];
    fill("x-output", runBody(g, run));
  }
  function renderAllRuns() {
    var g = group(state.allGroup);
    segmented("all-group", GROUPS.map(function (x) { return [x.key, x.name]; }), state.allGroup, function (k) { state.allGroup = k; renderAllRuns(); });
    renderRunsTable(g); renderLatency(g); renderInvalid(g);
  }
  function renderRuns() {
    segmented("runs-view", [["explore", "Explore a run"], ["all", "All runs"]], state.view, function (k) { state.view = k; renderRuns(); });
    document.getElementById("view-explore").hidden = state.view !== "explore";
    document.getElementById("view-all").hidden = state.view !== "all";
    if (state.view === "explore") renderExplorer(); else renderAllRuns();
  }
  document.getElementById("x-case").addEventListener("change", function (e) {
    state.x.caseId = e.target.value;
    state.x.group = GROUPS.filter(function (o) { return caseOf(o, e.target.value); })[0].key;
    renderExplorer();
  });

  // -------------------------------------------------------------------- methodology
  function renderMethodology() {
    var p = GROUPS[0], d = GROUPS[1], pa = p.agent, pb = p.playbook;
    var favourable = p.cases.filter(function (c) { return c.leaning === "favours agent"; }).length;
    var faults = p.cases.filter(function (c) { return c.category !== "control"; }).length;
    var figures = {
      amendment: p.scoring_amendment_changed === 0
        ? "A scoring correction was registered before any prospective run; the originally registered scorer gives the same outcome on every run."
        : "A scoring correction was registered before any prospective run.",
      limit: DATA.trace_result_limit.toLocaleString("en-US"),
      bias: favourable + " of " + faults + " prospective fault scenarios were disclosed in advance as favourable to the AI Agent, and all " + p.paired.agent_only + " runs only it got right come from them.",
      sample: "Two recorded runs per prospective scenario. Seeds change random draws, not the structure of a fault, so runs of one scenario are correlated and do not count as independent trials. The 95% intervals overlap: " +
        pa.ci[0] + "–" + pa.ci[1] + "% and " + pb.ci[0] + "–" + pb.ci[1] + "%.",
      stats: "A paired exact McNemar test gives p = " + p.paired.mcnemar_p.toFixed(3) + " for the prospective runs and p = " + d.paired.mcnemar_p.toFixed(3) +
        " for the development runs. With correlated runs and the lead concentrated in scenarios disclosed as favourable, these numbers do not establish a general difference.",
      cost: "The AI Agent averaged " + seconds(pa.latency.mean_s) + " per model-using prospective run (" + Math.round(pa.latency.min_s) + "–" + Math.round(pa.latency.max_s) + " s) and cost " +
        usd(pa.cost_usd) + " in API fees across that evaluation. The Playbook took " + seconds(pb.latency.max_s) + " at no API cost.",
      tools: "The AI Agent made " + pa.invalid_tool_calls + " invalid read-only queries in the prospective evaluation, listed under Recorded runs, All runs. None changed the system."
    };
    document.querySelectorAll("[data-fig]").forEach(function (node) { node.textContent = figures[node.getAttribute("data-fig")]; });

    fill("provenance", GROUPS.map(function (g) {
      var v = g.provenance;
      var pairs = [["Internal name", g.source.suite_name], ["Source file", g.source.file], ["SHA-256, with LF line endings", g.source.sha256], ["Records", g.source.records], ["Run tag", v.tag],
                   ["Recorded evidence type", v.evidence_type]];
      if (v.agent_version) pairs.push(["Version label stored in the file", v.agent_version + " (the final report identifies this run as v3.2.1)"]);
      if (v.freeze_commit) pairs.push(["Frozen version commit (v3.2.1)", v.freeze_commit]);
      if (v.frozen) pairs.push(["Production code identical to the freeze", v.frozen.production_identical_to_freeze ? "Yes, as recorded" : "No, as recorded"]);
      if (v.scoring_version) pairs.push(["Scoring version", v.scoring_version]);
      pairs.push(["Models", v.models.cheap + ", " + v.models.strong], ["Recorded API spend", "$" + v.additional_spend_usd.toFixed(4)]);
      return h("div", {}, h("h3", { text: g.name }), kvTable(pairs));
    }));
  }

  // ------------------------------------------------------------------ deep links
  function applyHash() {
    var m = /^#run\/([A-Za-z0-9]+)-(\d+)-(agent|playbook)$/.exec(location.hash);
    if (!m) return;
    var g = GROUPS.filter(function (x) { return caseOf(x, m[1]); })[0];
    if (!g) return;
    state.x = { group: g.key, caseId: m[1], number: Number(m[2]), engine: m[3] };
    state.view = "explore";
    renderRuns();
    document.getElementById("runs").scrollIntoView();
  }
  window.addEventListener("hashchange", applyHash);

  renderOverview();
  renderComparison();
  renderRuns();
  renderMethodology();
  applyHash();
})();
