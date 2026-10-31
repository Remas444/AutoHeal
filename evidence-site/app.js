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
  var SECTIONS = ["overview", "comparison", "runs"];
  var ORDER = ["recovered", "correct_escalation", "correct_noop", "recovered_and_escalated", "guardrail_stop", "wrong_diagnosis", "detection_failure"];
  var NOT_CORRECT = { guardrail_stop: 1, wrong_diagnosis: 1, detection_failure: 1 };
  var STAGE_NAMES = { Outcome: "Resolve / Escalate" };
  var EVENT_TONES = { success: "success", info: "info", warning: "safety", danger: "failure" };
  // Left out of the key steps: read-only investigation, model routing, notes, passing checks and prompts to the agent.
  // Every event remains in the original results file.
  var SUPPORTING_EVENTS = { observe: 1, route: 1, thought: 1, nudge: 1, decision: 1, focus: 1 };

  var state = { group: GROUPS[0].key, x: { group: GROUPS[0].key, caseId: null, number: 1, engine: "agent" } };

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
  function segmented(id, options, current, onPick) {
    fill(id, options.map(function (o) {
      return h("button", { type: "button", "aria-pressed": String(o[0] === current), text: o[1], onclick: function () { onPick(o[0]); } });
    }));
  }

  // -------------------------------------------------------------------- overview
  function scoreCard(summary) {
    var parts = ORDER.filter(function (k) { return summary.outcomes[k]; });
    return h("article", { class: "score" },
      h("div", { class: "score-name", text: summary.name }),
      h("div", { class: "score-value", text: summary.correct + "/" + summary.total }),
      h("div", { class: "score-note", text: summary.pct + "% correct outcomes. 95% interval " + summary.ci[0] + "–" + summary.ci[1] + "%." }),
      h("div", { class: "bar", "aria-hidden": "true" }, parts.map(function (k) {
        return h("div", { class: "seg-bar tone-" + DATA.outcomes[k].tone, style: "flex:" + summary.outcomes[k] + " " + summary.outcomes[k] + " 0" });
      })),
      h("ul", { class: "legend" }, parts.map(function (k) {
        var o = DATA.outcomes[k];
        return h("li", { class: "legend-item tone-" + o.tone }, h("span", { class: "swatch" }), summary.outcomes[k] + " " + o.label + (NOT_CORRECT[k] ? " (not correct)" : ""));
      })));
  }
  function renderOverview() {
    var p = GROUPS[0], d = GROUPS[1], a = p.agent, b = p.playbook;
    var tied = p.cases.filter(function (c) { return c.category === "tied"; });
    fill("answer", "In the prospective evaluation, the AI Agent reached the correct outcome in " + a.correct + " of " + a.total +
      " recorded runs and the rule-based Playbook in " + b.correct + " of " + b.total + ". All " + p.paired.agent_only +
      " runs that only the AI Agent got right come from three scenario types disclosed in advance as favourable to it. On the " +
      plural(tied.length, "fault scenario") + " where both were correct, the Playbook was far faster and free of API costs. " +
      "This shows where AI adds value. It does not show general superiority.");
    fill("scores", scoreCard(a), scoreCard(b));
    fill("regression-note", d.name + ", reported separately and never combined: AI Agent " + d.agent.correct + "/" + d.agent.total +
      ", Rule-based Playbook " + d.playbook.correct + "/" + d.playbook.total + ". " + d.summary);

    var favourable = p.cases.filter(function (c) { return c.leaning === "favours agent"; }).length;
    var faults = p.cases.filter(function (c) { return c.category !== "control"; }).length;
    var figures = {
      limit: DATA.trace_result_limit.toLocaleString("en-US"),
      bias: favourable + " of " + faults + " fault scenarios were disclosed in advance as favourable to the AI Agent, and some thresholds were tuned with earlier scenarios in view.",
      sample: "Two recorded runs per prospective scenario. Seeds change random draws, not the structure of a fault, so runs of one scenario are correlated. The 95% intervals overlap: " +
        a.ci[0] + "–" + a.ci[1] + "% and " + b.ci[0] + "–" + b.ci[1] + "%.",
      stats: "A paired exact McNemar test gives p = " + p.paired.mcnemar_p.toFixed(3) + " for the prospective runs, which does not establish a general difference."
    };
    document.querySelectorAll("[data-fig]").forEach(function (node) { node.textContent = figures[node.getAttribute("data-fig")]; });
  }

  // ------------------------------------------------------------------- comparison
  function resultCell(engine) {
    var mark = engine.correct === engine.total ? "✓" : (engine.correct ? "•" : "×");
    return h("div", { class: "result tone-" + engine.tone },
      h("span", { class: "result-label", text: engine.outcome_label + (engine.mixed ? " (mixed)" : "") }),
      h("span", { class: "result-score", text: mark + " " + engine.correct + "/" + engine.total }));
  }
  function caseTag(c) {
    if (c.leaning === "favours agent") return h("span", { class: "tag", text: "Disclosed as favourable to the AI Agent" });
    if (c.leaning === "favours playbook") return h("span", { class: "tag", text: "Disclosed as favourable to the Playbook" });
    return null;
  }
  function renderMatrix(g) {
    fill("matrix-body", g.cases.map(function (c) {
      return h("tr", {},
        h("td", {}, h("a", { class: "row-link", href: "#run/" + c.id + "-1-agent" },
          h("span", { class: "case-title" }, h("span", { class: "id-tag", text: c.id }), c.title), caseTag(c))),
        h("td", { "data-label": "AI Agent" }, resultCell(c.agent)),
        h("td", { "data-label": "Rule-based Playbook" }, resultCell(c.playbook)));
    }));
  }
  function renderMetrics(g) {
    var a = g.agent, b = g.playbook, NA = "Not recorded";
    function row(label, x, y) {
      return h("tr", {}, h("th", { scope: "row", text: label }),
        h("td", { class: "num", text: x === null || x === undefined ? NA : x }), h("td", { class: "num", text: y === null || y === undefined ? NA : y }));
    }
    fill("metrics-body",
      row("Correct outcomes", a.correct + "/" + a.total, b.correct + "/" + b.total),
      row("Faults detected", ratio(a.detection), ratio(b.detection)),
      row("False positives on healthy controls", a.false_positives ? ratio(a.false_positives) : null, b.false_positives ? ratio(b.false_positives) : null),
      row("Accepted diagnosis", ratio(a.diagnosis), ratio(b.diagnosis)),
      row("Correct escalation where required", ratio(a.escalation), ratio(b.escalation)),
      row("Unsafe actions executed", a.unsafe_executed, b.unsafe_executed),
      row("Unsafe actions attempted and refused", a.unsafe_attempted, b.unsafe_attempted),
      row("Invalid tool calls", a.invalid_tool_calls, b.invalid_tool_calls),
      row("Mean time to finish", seconds(a.latency.mean_s) + " (" + Math.round(a.latency.min_s) + "–" + Math.round(a.latency.max_s) + " s)", seconds(b.latency.max_s)),
      row("API cost, whole evaluation", usd(a.cost_usd), usd(b.cost_usd)));
    fill("metrics-note", g.key === GROUPS[0].key
      ? "Faults detected are the same because both systems share one detector. Invalid tool calls were read-only queries, rejected with no effect on the system. AI Agent time covers the runs that called a model."
      : "“Not recorded” means this group's result file has no such field.");
  }
  function renderComparison() {
    var g = group(state.group);
    segmented("compare-group", GROUPS.map(function (x) { return [x.key, x.name]; }), state.group, function (k) { state.group = k; renderComparison(); });
    fill("compare-note", g.summary);
    renderMatrix(g); renderMetrics(g);
  }

  // ------------------------------------------------------------------ recorded runs
  function keySteps(r) {
    var escalated = r.events.some(function (e) { return e.kind === "escalate"; });
    return r.events.filter(function (e) {
      if (SUPPORTING_EVENTS[e.kind]) return false;
      if (e.kind === "action" && e.stage === "Decide") return false;  // the next event is its execution
      if (e.kind === "error" && e.stage === "Investigate") return false;  // a rejected read-only query, counted in the comparison
      if (e.kind === "escalate_decision" && escalated) return false;  // the escalation that follows says the same
      return true;
    });
  }
  function stepNode(e) {
    return h("li", { class: "ev tone-" + (EVENT_TONES[e.tone] || "info") }, h("span", { class: "ev-dot" }),
      h("span", {}, h("span", { class: "ev-t", text: e.title }), e.summary ? h("span", { class: "ev-s", text: e.summary }) : null),
      h("span", { class: "ev-stage", text: STAGE_NAMES[e.stage] || e.stage }));
  }
  function runBody(g, r) {
    var c = caseOf(g, r.case), steps = keySteps(r);
    return [
      h("p", { class: "scenario-text", text: c.what }),
      h("div", { class: "verdict tone-" + r.tone, role: "status" },
        h("div", {},
          h("div", { class: "t", text: r.outcome_label }),
          h("div", { class: "e", text: DATA.outcomes[r.outcome].meaning }),
          r.outcome === "wrong_diagnosis" && r.status === "resolved"
            ? h("div", { class: "e", text: "The run closed itself as resolved and passed verification; the scoring is stricter than that." }) : null,
          h("div", { class: "s" }, r.correct ? h("span", { class: "mark-ok", text: "✓ Scored correct" }) : h("span", { class: "mark-no", text: "× Scored not correct" }),
            ". Correct result: " + c.correct)),
        h("dl", { class: "stats" }, [["Time", r.invoked ? seconds(r.latency_s) : "–"], ["API cost", usd(r.cost_usd)], ["Tool calls", r.tool_calls], ["Actions executed", r.executed]].map(function (s) {
          return h("div", {}, h("dt", { text: s[0] }), h("dd", { text: s[1] }));
        }))),
      h("h3", { text: "Key steps" }),
      h("ol", { class: "steps" }, steps.map(stepNode)),
      h("p", { class: "muted small" }, "Showing " + steps.length + " of " + plural(r.events.length, "recorded event") + ". The complete trace is in the ",
        h("a", { href: DATA.site.repository + "/blob/main/" + g.source.file, text: "original results file" }),
        ": scenario " + r.case + ", seed " + r.seed + ".")];
  }
  function renderRuns() {
    var x = state.x, g = group(x.group);
    if (!x.caseId || !caseOf(g, x.caseId)) x.caseId = g.cases[0].id;
    var numbers = runsOf(g, x.caseId).map(function (r) { return r.number; }).filter(function (n, i, all) { return all.indexOf(n) === i; }).sort();
    if (numbers.indexOf(x.number) < 0) x.number = numbers[0];

    // The two evaluation groups stay separate inside one scenario list.
    var select = fill("x-case", GROUPS.map(function (o) {
      return h("optgroup", { label: o.name }, o.cases.map(function (c) { return h("option", { value: c.id, text: c.title }); }));
    }));
    select.value = x.caseId;
    segmented("x-run", numbers.map(function (n) { return [n, "Run " + n]; }), x.number, function (n) { x.number = n; renderRuns(); });
    segmented("x-engine", [["agent", "AI Agent"], ["playbook", "Rule-based Playbook"]], x.engine, function (k) { x.engine = k; renderRuns(); });

    var run = g.runs.filter(function (r) { return r.case === x.caseId && r.number === Number(x.number) && r.engine === x.engine; })[0];
    fill("x-output", runBody(g, run));
  }
  document.getElementById("x-case").addEventListener("change", function (e) {
    state.x.caseId = e.target.value;
    state.x.group = GROUPS.filter(function (o) { return caseOf(o, e.target.value); })[0].key;
    renderRuns();
  });

  // ---------------------------------------------------------------------- views
  function show(name) {
    SECTIONS.forEach(function (s) { document.getElementById(s).hidden = s !== name; });
    document.querySelectorAll(".top nav a").forEach(function (a) {
      if (a.getAttribute("href") === "#" + name) a.setAttribute("aria-current", "page"); else a.removeAttribute("aria-current");
    });
    window.scrollTo(0, 0);
  }
  function route() {
    var m = /^#run\/([A-Za-z0-9]+)-(\d+)-(agent|playbook)$/.exec(location.hash);
    var g = m && GROUPS.filter(function (x) { return caseOf(x, m[1]); })[0];
    if (g) {
      state.x = { group: g.key, caseId: m[1], number: Number(m[2]), engine: m[3] };
      renderRuns();
      show("runs");
      return;
    }
    var name = location.hash.slice(1);
    show(SECTIONS.indexOf(name) >= 0 ? name : "overview");
  }
  window.addEventListener("hashchange", route);
  window.addEventListener("load", function () { window.scrollTo(0, 0); });  // a view always opens at its top, header included

  renderOverview();
  renderComparison();
  renderRuns();
  route();
})();
