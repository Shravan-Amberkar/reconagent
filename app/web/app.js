// ReconAgent web app. All text from files (narrations, notes, names) is escaped before display.
const $ = (s, el = document) => el.querySelector(s);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const inr = (p) => "₹" + (p / 100).toLocaleString("en-IN", { maximumFractionDigits: 0 });
const inr2 = (p) => (p == null ? "—" : "₹" + (p / 100).toLocaleString("en-IN", { minimumFractionDigits: 2 }));
const PATTERN = {
  netted_refund: "A refund was netted out of the settlement",
  split_settlement: "One settlement arrived as two bank credits",
  fee_gst_deduction: "Fees and GST explain the difference",
  chargeback: "A chargeback was deducted",
  partial_payment: "The invoice was paid in parts",
  many_to_one: "One payment covered several invoices",
  lump_sum: "A lender paid several students' fees at once",
  holiday_shift: "Settled after a bank holiday",
  utr_truncated: "The bank cut the reference number short",
};
const RULE = { L1_utr: "bank reference (UTR)", L1_receipt: "receipt number", L2_amount: "exact amount", L3_subset_sum: "combination of payments" };
let R = null, tab = new URLSearchParams(location.search).get("tab") || "paid", token = "sample", showPaid = false, q = "";

async function load(mode) {
  document.querySelectorAll(".mode").forEach((b) => b.classList.toggle("on", b.dataset.mode === mode));
  $("#upload").hidden = mode !== "upload";
  if (mode === "sample") {
    token = "sample";
    R = await (await fetch("/api/sample")).json();
    render();
  } else {
    $("#upload").scrollIntoView({ behavior: "smooth" });
  }
}

function render() {
  const h = R.headline;
  $("#kpis").innerHTML = `
    <div class="card kpi fight"><small>Bank credits explained</small><div class="v">${h.credits_explained}/${h.credits}</div><div class="s">every rupee traced to a settlement</div></div>
    <div class="card kpi"><small>Fees received</small><div class="v">${h.paid}/${h.invoices}</div><div class="s">invoices paid, each with proof</div></div>
    <div class="card kpi due"><small>To chase</small><div class="v">${inr(h.to_chase_amount)}</div><div class="s">${h.to_chase} invoices unpaid</div></div>
    <div class="card kpi accept"><small>Charged above your rates</small><div class="v">${inr(h.fee_leak)}</div><div class="s">fee audit vs your contract</div></div>`;
  $("#banner").hidden = false;
  $("#banner").textContent = (R.replayed ? "AI agent results replayed from a recorded run (free, no API key needed). " : "") +
    `Rules matched the straightforward cases; the AI agent solved ${h.agent_solved} harder ones; ${h.needs_you} need your decision.` +
    (R.run.calls ? ` ${R.run.calls} AI calls, ${R.replayed ? "≈$" + R.run.cost_usd + " if run live" : "$" + R.run.cost_usd}.` : "");
  const tabs = [["paid", "Who paid"], ["bank", "Bank vs Razorpay"], ["ai", "What the AI solved"], ["you", "Needs you", h.needs_you], ["proof", "Does it work?"]];
  $("#tabs").innerHTML = tabs.filter(([k]) => k !== "proof" || R.eval).map(([k, label, n]) =>
    `<button class="tab ${k === tab ? "on" : ""}" data-t="${k}">${label}${n ? `<span class="n">${n}</span>` : ""}</button>`).join("");
  ({ paid: paneWhoPaid, bank: paneBank, ai: paneAI, you: paneYou, proof: paneProof }[tab] || paneWhoPaid)();
}

function paneWhoPaid() {
  const rows = R.lines.filter((x) => (showPaid || x.status !== "paid") &&
    (!q || (x.customer + " " + x.invoice).toLowerCase().includes(q.toLowerCase())));
  const cls = (s) => (s === "paid" ? "st-paid" : s === "unpaid" ? "st-unpaid" : "st-partly");
  $("#pane").innerHTML = `<div class="card">
    <h2>${showPaid ? "All invoices" : "Who to chase"}</h2>
    <p class="muted">${showPaid ? "Every paid invoice shows its proof: payment → Razorpay settlement → bank line, and the rule that matched them." : "Invoices with no proven payment yet. Download the list and send reminders."}</p>
    <div class="toolbar">
      <input type="search" id="q" placeholder="Search customer or invoice" value="${esc(q)}">
      <label><input type="checkbox" id="showpaid" ${showPaid ? "checked" : ""}> show paid invoices too</label>
      <button class="ghost" id="dl">Download chase list (CSV)</button>
    </div>
    <div class="scroll"><table><thead><tr><th>Customer</th><th>Invoice</th><th>Amount</th><th>Due</th><th>Status</th>${showPaid ? "<th>Proof</th>" : ""}</tr></thead><tbody>
      ${rows.map((x) => `<tr><td>${esc(x.customer)}</td><td>${esc(x.invoice)}</td><td>${inr2(x.amount)}</td><td>${esc(x.due)}</td>
        <td><span class="st ${cls(x.status)}">${esc(x.status)}</span></td>${showPaid ? `<td class="proofline">${esc(x.proof)}</td>` : ""}</tr>`).join("") ||
        `<tr><td colspan="6" class="muted">Nothing to show.</td></tr>`}
    </tbody></table></div>
    ${R.unplaced.length ? `<h3>Money that arrived but couldn't be tied to an invoice (${R.unplaced.length})</h3>
      <p class="muted">These payments are in Razorpay but nothing proves which invoice they pay. They are listed, never guessed; see Needs you for the agent's candidate explanations.</p>
      <div class="scroll" style="max-height:260px"><table><thead><tr><th>Payment</th><th>Amount</th><th>Date</th><th>What the payer wrote</th></tr></thead><tbody>
      ${R.unplaced.map((p) => `<tr><td class="proofline">${esc(p.payment_id)}</td><td>${inr2(p.amount)}</td><td>${esc(p.date)}</td><td>${esc(p.notes || "(nothing)")}</td></tr>`).join("")}
      </tbody></table></div>` : ""}
  </div>`;
  $("#q").addEventListener("input", (e) => { q = e.target.value; paneWhoPaid(); $("#q").focus(); $("#q").setSelectionRange(q.length, q.length); });
  $("#showpaid").addEventListener("change", (e) => { showPaid = e.target.checked; paneWhoPaid(); });
  $("#dl").addEventListener("click", () => {
    const chase = R.lines.filter((x) => x.status !== "paid");
    const csv = "customer,invoice,amount,paid,due,status\n" + chase.map((x) => [x.customer, x.invoice, (x.amount / 100).toFixed(2), (x.paid / 100).toFixed(2), x.due, x.status].map((v) => `"${String(v).replace(/"/g, '""')}"`).join(",")).join("\n");
    const a = document.createElement("a");
    a.href = URL.createObjectURL(new Blob([csv], { type: "text/csv" }));
    a.download = "chase_list.csv";
    a.click();
  });
}

function paneBank() {
  const g = R.gaps, t = g.totals, max = t.gross || 1;
  const bar = (label, v, kind) => `<div class="wrow"><span>${label}</span><div class="wbar ${kind}" style="width:${Math.max(0.6, (100 * v) / max)}%"></div><span class="amt">${kind === "minus" ? "−" : ""}${inr2(v)}</span></div>`;
  const reached = g.groups.filter((x) => x.status === "reached bank").length;
  $("#pane").innerHTML = `<div class="card">
    <h2>Where the money went</h2>
    <p class="muted">From what customers paid to what reached your bank, settlement by settlement.</p>
    <div class="water">${bar("Customers paid", t.gross, "")}${bar("Gateway fees", t.fees, "minus")}${bar("GST on fees (18%)", t.gst, "minus")}${bar("Refunds", t.refunds, "minus")}${bar("Chargebacks", t.chargebacks, "minus")}${t.other ? bar("Other adjustments", t.other, "minus") : ""}${bar("Settled to your bank", t.net, "net")}</div>
    <p><b>${reached} of ${g.groups.length}</b> settlements were found in the bank statement${g.unreceived ? `, and <b>${inr2(g.unreceived)}</b> settled but not found` : ", every rupee accounted for"}.</p>

    <h3>Fee audit: were you charged what you signed up for?</h3>
    <p class="muted">Enter your contracted rates. Every payment's fee is recomputed and anything above contract is listed with the exact payment ids.${R.mode === "sample" ? " The sample uses a made-up contract (UPI 0%, net banking 1.75%, cards 2%)." : ""}</p>
    <div class="rates">${Object.entries(g.card).filter(([k]) => ["upi", "netbanking", "card", "wallet"].includes(k)).map(([k, v]) => `<label>${esc(k)} %<input type="number" step="0.05" min="0" max="10" data-m="${esc(k)}" value="${v / 100}"></label>`).join("")}
      <button class="ghost" id="recalc">Recalculate</button></div>
    <p style="margin-top:12px"><b>${inr2(g.leak)}</b> charged above your rates across <b>${g.findings.filter((f) => f.excess > 0).length}</b> payments.</p>
    ${g.findings.length ? `<div class="scroll" style="max-height:280px"><table><thead><tr><th>Payment</th><th>Method</th><th>Amount</th><th>Fee charged</th><th>Per contract</th><th>Excess (with GST)</th></tr></thead><tbody>
      ${g.findings.slice(0, 200).map((f) => `<tr><td class="proofline">${esc(f.payment)}</td><td>${esc(f.method)}</td><td>${inr2(f.amount)}</td><td>${inr2(f.fee_charged)}</td><td>${inr2(f.fee_expected)}</td><td>${inr2(f.excess)}</td></tr>`).join("")}
    </tbody></table></div>` : ""}
    <h3>Ticket to send to Razorpay</h3>
    <textarea class="ticket" id="ticket" readonly>${esc(g.ticket)}</textarea>
    <div class="actions"><button class="ghost" id="copyt">Copy ticket</button></div>

    <h3>Every settlement</h3>
    <div class="scroll" style="max-height:340px"><table><thead><tr><th>Settled</th><th>Customers paid</th><th>Fees + GST</th><th>Refunds</th><th>Chargebacks</th><th>Net</th><th>In bank</th><th>Status</th></tr></thead><tbody>
      ${g.groups.map((x) => `<tr><td>${esc(x.settled_on)}</td><td>${inr2(x.gross)}</td><td>${inr2(x.fees + x.gst)}</td><td>${inr2(x.refunds)}</td><td>${inr2(x.chargebacks)}</td><td>${inr2(x.net)}</td><td>${inr2(x.bank)}</td><td><span class="st ${x.status === "reached bank" ? "st-reached" : "st-missing"}">${esc(x.status)}</span></td></tr>`).join("")}
    </tbody></table></div>
  </div>`;
  $("#recalc").addEventListener("click", async () => {
    const card = { ...Object.fromEntries(Object.entries(R.gaps.card).map(([k, v]) => [k, v / 100])) };
    document.querySelectorAll(".rates input").forEach((i) => (card[i.dataset.m] = parseFloat(i.value || "0")));
    const url = token === "sample" ? "/api/sample/rates" : `/api/run/${token}`;
    R = await (await fetch(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ card }) })).json();
    render();
  });
  $("#copyt").addEventListener("click", async () => { await navigator.clipboard.writeText(g.ticket); $("#copyt").textContent = "Copied"; });
}

const INJECTION = /ignore (all )?(previous|prior) instructions|admin mode|system note/i;

function side(c) {
  const L = c.level === "A"
    ? c.left.map((b) => `<div><b>Bank credit ${inr2(b.amount)}</b><br>${esc(b.date)} · ${esc(b.narration.slice(0, 60))}</div>`).join("")
    : c.left.map((p) => `<div><b>Payment ${inr2(p.amount)}</b><br>${esc(p.date)} · ${esc(p.notes || "no reference")}</div>`).join("");
  const Rr = c.level === "A"
    ? c.right.map((s) => `<div><b>Settlement ${inr2(s.amount)}</b><br>${esc(s.date)} · UTR ${esc(s.utr)}</div>`).join("")
    : c.right.map((i) => `<div><b>Invoice ${esc(i.id)} · ${inr2(i.amount)}</b><br>${esc(i.customer)}</div>`).join("");
  const inj = c.left.some((p) => INJECTION.test(p.notes || p.narration || ""))
    ? `<div class="chip due">⚠ The payer's note tried to instruct the AI. It was treated as data and ignored.</div>` : "";
  return `<div class="side"><div class="box">${L}</div><div class="arrow">→</div><div class="box">${Rr}</div></div>${inj}`;
}

function paneAI() {
  const rules = Object.entries(R.rules).map(([k, v]) => `${v} by ${esc(RULE[k] || k)}`).join(", ");
  $("#pane").innerHTML = `<div class="card">
    <h2>The hard cases, and how the agent proved each one</h2>
    <p class="muted">Rules cleared the straightforward matches (${rules}). These are the ${R.agent_cases.length} the rules could not decide. For each, the agent proposed a match and the verifier checked it with exact arithmetic before it counted.</p>
    <div class="cases">${R.agent_cases.map((c) => `<div class="case">
      <div class="pat">${esc(PATTERN[c.pattern] || c.pattern || "Matched")}</div>
      ${side(c)}
      <div class="eq">${esc(c.equation)}</div>
      <div class="muted" style="font-size:12.5px">${c.source === "human" ? "Approved by a person" : `Proved by the verifier after ${c.steps.filter((s) => s === "tool").length} lookups${c.steps.includes("challenge") ? ", and a second pass found no equally valid alternative" : ""}.`}</div>
      ${c.rationale ? `<details><summary>Agent's reasoning</summary><div class="why">${esc(c.rationale)}</div><div class="muted" style="font-size:12px;margin-top:6px">Steps: ${esc(c.steps.join(" → "))}</div></details>` : ""}
    </div>`).join("") || `<p class="muted">Rules handled everything this month.</p>`}</div>
  </div>`;
}

function paneYou() {
  $("#pane").innerHTML = `<div class="card">
    <h2>Needs your decision</h2>
    <p class="muted">The agent found more than one explanation that passes every check, or none it could prove. It never guesses. Approving re-runs the same verifier before anything is committed.</p>
    ${R.pending ? `<p class="error">${R.pending} items were not sent to the AI (no API key on this server, or the per-upload limit). They stay here for review.</p>` : ""}
    ${R.escalations.map((e) => `<div class="case" style="margin-top:12px">
      <div class="pat">${e.level === "A" ? "Bank credit" : "Payment"} ${esc(e.id)}</div>
      <div class="why">${esc(e.reason)}</div>
      <div class="opts">${e.options.map((o, i) => `<div class="opt"><b>Option ${i + 1}: ${esc(PATTERN[o.pattern] || o.pattern)}</b>
        <span class="muted">→ ${esc(o.right.join(", "))}</span><span class="why">${esc(o.rationale)}</span>
        <button class="ghost" data-ex="${esc(e.id)}" data-i="${i}">Approve this</button></div>`).join("") || `<span class="muted">No explanation passed the checks. Investigate manually.</span>`}</div>
    </div>`).join("") || `<p>Nothing waiting. Every item was matched with proof.</p>`}
  </div>`;
  document.querySelectorAll("[data-ex]").forEach((b) => b.addEventListener("click", async () => {
    const res = await fetch(`/api/approve/${token}/${encodeURIComponent(b.dataset.ex)}`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ choice: +b.dataset.i }) });
    if (!res.ok) { alert((await res.json()).detail); return; }
    R = await res.json();
    render();
  }));
}

function paneProof() {
  const e = R.eval, pct = (x) => Math.round(100 * x) + "%";
  const lv = (k, label) => { const v = e.levels[k]; return `<tr><td>${label}</td><td>${v.items}</td><td class="ok">${pct(v.auto_match_rate)}</td><td class="${v.false ? "no" : "ok"}">${v.false}</td><td>${v.escalated}</td></tr>`; };
  $("#pane").innerHTML = `<div class="card">
    <h2>Does it work? Scored against the known answer</h2>
    <p class="muted">The sample month is synthetic: every payment's true invoice is known, including deliberate traps (look-alike names, same-amount decoys, a lender paying several students at once, prompt-injection text in payment notes).</p>
    <table><thead><tr><th>Matching</th><th>Items</th><th>Matched automatically</th><th>Wrong matches</th><th>Sent to a person</th></tr></thead><tbody>
      ${lv("A", "Bank credit ↔ Razorpay settlement")}${lv("B", "Payment ↔ invoice")}</tbody></table>
    <p>Decoys wrongly matched: <b class="${e.leakage ? "no" : "ok"}">${e.leakage}</b> · Escalations that really needed a person: <b>${e.escalation_precision ?? "n/a"}</b></p>
    <p class="muted">The rule the design is built around: a wrong match is worse than no match. Zero wrong matches is the bar; everything uncertain goes to a person.</p>
  </div>`;
}

$("#tabs").addEventListener("click", (e) => { const b = e.target.closest("[data-t]"); if (b) { tab = b.dataset.t; render(); } });
document.querySelectorAll(".mode").forEach((b) => b.addEventListener("click", () => load(b.dataset.mode)));
$("#upform").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  $("#uperr").hidden = true;
  $("#upstate").textContent = "Reading your files…";
  try {
    const res = await fetch("/api/preview", { method: "POST", body: new FormData(ev.target) });
    const p = await res.json();
    if (!res.ok) throw new Error(p.detail);
    token = p.token;
    $("#upstate").textContent = "";
    $("#preview").innerHTML = `<h3>Here is what we read. Does it look right?</h3>
      <p>${p.razorpay_rows} Razorpay rows in ${p.settlements} settlements · ${p.bank_lines} bank lines (format detected: ${esc(p.bank_format)}, running balance verified on every line) · ${p.invoices} invoices</p>
      <div class="scroll" style="max-height:220px"><table><thead><tr><th>Date</th><th>Narration</th><th>Credit</th><th>Debit</th><th>Balance</th></tr></thead><tbody>
      ${p.bank_sample.map((t) => `<tr><td>${esc(t.date)}</td><td>${esc(t.narration)}</td><td>${esc(t.credit)}</td><td>${esc(t.debit)}</td><td>${esc(t.balance)}</td></tr>`).join("")}
      </tbody></table></div><div class="actions"><button class="primary" id="go">Looks right: reconcile</button></div>`;
    $("#go").addEventListener("click", async () => {
      $("#go").disabled = true;
      $("#go").textContent = "Reconciling…";
      const r2 = await fetch(`/api/run/${token}`, { method: "POST" });
      const body = await r2.json();
      if (!r2.ok) { $("#uperr").hidden = false; $("#uperr").textContent = body.detail; return; }
      R = body; tab = "paid"; render();
      $("#kpis").scrollIntoView({ behavior: "smooth" });
    });
  } catch (e) {
    $("#uperr").hidden = false;
    $("#uperr").textContent = e.message;
    $("#upstate").textContent = "";
  }
});
load("sample");
