/* YatraDemo frontend — talks only to this same origin's /api/v1/. */
const $ = (id) => document.getElementById(id);
const state = { cities: [], from: "HYD", to: "BLR", date: "2026-12-05",
  options: [], option: null, seats: [], selected: new Set(), booking: null, user: null };
const inr = (n) => "₹" + Number(n).toLocaleString("en-IN");
const show = (id) => { ["step-search","step-results","step-seats","step-passenger","step-summary"].forEach(s => $(s).hidden = s !== id); };
const showErr = (id, msg) => { const el = $(id); if (!msg) { el.hidden = true; return; } el.textContent = msg; el.hidden = false; };

function cityCode(val) { const m = /\(([A-Z]{3})\)/.exec(val || ""); return m ? m[1] : (val || "").trim().toUpperCase(); }
function cityLabel(code) { const c = state.cities.find(c => c.code === code); return c ? `${c.name} (${c.code})` : code; }

async function loadCities() {
  try {
    const r = await fetch("/api/v1/cities"); const j = await r.json();
    state.cities = j.cities || [];
    $("cityList").innerHTML = state.cities.map(c => `<option value="${c.name} (${c.code})">`).join("");
  } catch (e) { /* datalist is progressive enhancement; typed codes still work */ }
}

function buildCalendar() {
  const cal = $("calendar"); const dows = ["Sun","Mon","Tue","Wed","Thu","Fri","Sat"];
  let html = dows.map(d => `<div class="dow">${d}</div>`).join("");
  const first = new Date(2026, 11, 1).getDay();
  for (let i = 0; i < first; i++) html += "<span></span>";
  for (let d = 1; d <= 31; d++) {
    const iso = `2026-12-${String(d).padStart(2, "0")}`;
    html += `<button type="button" data-date="${iso}" data-testid="cal-day-${iso}" class="${iso === state.date ? "sel" : ""}">${d}</button>`;
  }
  cal.innerHTML = html;
  cal.querySelectorAll("button[data-date]").forEach(b => b.addEventListener("click", () => {
    state.date = b.dataset.date; $("dateInput").value = state.date; buildCalendar();
  }));
}
$("calToggle").addEventListener("click", () => {
  const cal = $("calendar"); cal.hidden = !cal.hidden;
  $("calToggle").setAttribute("aria-expanded", String(!cal.hidden));
  if (!cal.hidden) buildCalendar();
});
$("dateInput").addEventListener("change", (e) => { state.date = e.target.value; buildCalendar(); });

$("loginBtn").addEventListener("click", async () => {
  try {
    const r = await fetch("/api/v1/auth/login", { method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({ username: "demo", password: "demo" }) });
    const j = await r.json();
    if (!r.ok || !j.ok) throw new Error(j.detail || "login failed");
    state.user = j.user; $("loginBtn").hidden = true;
    const chip = $("userChip"); chip.hidden = false; chip.textContent = "Signed in as " + j.user.name + " (demo)";
  } catch (e) { alert("Demo sign-in failed: " + e.message); }
});

$("searchForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  state.from = cityCode($("fromInput").value); state.to = cityCode($("toInput").value); state.date = $("dateInput").value;
  if (state.from === state.to) { showErr("searchError", "From and To must be different cities."); return; }
  showErr("searchError", null); show("step-results");
  $("resultsTitle").textContent = `${cityLabel(state.from)} → ${cityLabel(state.to)} · ${state.date}`;
  await runSearch();
});

async function runSearch() {
  $("resultsLoading").hidden = false; $("resultsList").innerHTML = ""; showErr("resultsError", null);
  const q = new URLSearchParams({ from_code: state.from, to_code: state.to, date: state.date,
    mode: $("filterMode").value, sort: $("sortBy").value });
  if ($("filterPrice").value) q.set("max_price", $("filterPrice").value);
  if ($("filterTime").value) q.set("dep_after", $("filterTime").value);
  try {
    const r = await fetch("/api/v1/search?" + q); const j = await r.json();
    if (!r.ok) throw new Error(j.detail || "search failed");
    state.options = j.options || [];
    renderResults();
    if (!state.options.length) showErr("resultsError", "No options found for this route and date. Try another date or remove filters.");
  } catch (e) { showErr("resultsError", "Search failed: " + e.message + " — please retry."); }
  finally { $("resultsLoading").hidden = true; }
}
$("applyFilters").addEventListener("click", runSearch);
$("backToSearch").addEventListener("click", () => show("step-search"));

function renderResults() {
  $("resultsList").innerHTML = state.options.map(o => `
    <li class="result" data-testid="result-${o.id}">
      <div><div class="svc">${o.service}</div>
      <div class="muted">${o.operator} · ${o.mode === "train" ? "Train" : "Bus"} · ★ ${o.rating}</div>
      <div class="times">${o.dep_time} → ${o.arr_time}</div>
      <div class="muted small">${o.amenities}</div></div>
      <div style="text-align:right"><div class="fare" data-testid="fare-${o.id}">${inr(o.fare)}</div>
      <div class="muted small">${o.seats_available} of ${o.total_seats} seats left</div>
      <button class="btn small" data-testid="select-${o.id}" data-id="${o.id}" ${o.seats_available === 0 ? "disabled" : ""}>Select seats</button></div>
    </li>`).join("");
  $("resultsList").querySelectorAll("button[data-id]").forEach(b => b.addEventListener("click", () => openSeats(Number(b.dataset.id))));
}

async function openSeats(id) {
  state.option = state.options.find(o => o.id === id); state.selected = new Set();
  show("step-seats"); $("seatLoading").hidden = false; $("seatMap").innerHTML = ""; showErr("seatError", null);
  $("seatTitle").textContent = `${state.option.service} · ${state.date} · ${inr(state.option.fare)} / seat`;
  try {
    const r = await fetch(`/api/v1/options/${id}?date=${state.date}`); const j = await r.json();
    if (!r.ok) throw new Error(j.detail || "failed to load seats");
    state.seats = j.option.seats; renderSeats();
  } catch (e) { showErr("seatError", "Could not load the seat map: " + e.message); }
  finally { $("seatLoading").hidden = true; }
}
function renderSeats() {
  $("seatMap").innerHTML = state.seats.map(s => `<button type="button" class="seat ${s.status} ${state.selected.has(s.seat_no) ? "selected" : ""}" data-testid="seat-${s.seat_no}" data-no="${s.seat_no}" ${s.status !== "available" ? "disabled" : ""} aria-label="Seat ${s.label}, ${s.status}">${s.label}</button>`).join("");
  $("seatMap").querySelectorAll("button[data-no]").forEach(b => b.addEventListener("click", () => {
    const n = Number(b.dataset.no);
    state.selected.has(n) ? state.selected.delete(n) : state.selected.add(n);
    renderSeats(); $("holdBtn").disabled = state.selected.size === 0;
  }));
  $("holdBtn").disabled = state.selected.size === 0;
}
$("backToResults").addEventListener("click", () => show("step-results"));

$("holdBtn").addEventListener("click", async () => {
  $("holdBtn").disabled = true; $("holdBtn").textContent = "Holding seats…";
  try {
    const r = await fetch("/api/v1/holds", { method: "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify({ option_id: state.option.id, date: state.date, seats: [...state.selected] }) });
    const j = await r.json();
    if (!r.ok) throw new Error(j.detail || "hold failed");
    state.booking = j; show("step-passenger");
  } catch (e) { alert("Could not hold seats: " + e.message); await openSeats(state.option.id); }
  finally { $("holdBtn").disabled = false; $("holdBtn").textContent = "Hold selected seats"; }
});

$("passengerForm").addEventListener("submit", async (e) => {
  e.preventDefault(); showErr("passengerError", null);
  const p = { name: $("pName").value.trim(), age: Number($("pAge").value), gender: $("pGender").value, email: $("pEmail").value.trim(), phone: $("pPhone").value.trim() };
  try {
    const r = await fetch(`/api/v1/bookings/${state.booking.booking_id}/passenger`, { method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(p) });
    const j = await r.json();
    if (!r.ok) throw new Error(j.detail || "could not save passenger");
    state.booking.passenger = p; renderSummary(); show("step-summary");
  } catch (err) { showErr("passengerError", "Could not save passenger details: " + err.message); }
});
$("backToPassenger").addEventListener("click", () => show("step-passenger"));

function renderSummary() {
  const o = state.option, b = state.booking, p = b.passenger;
  $("summaryBox").innerHTML = `
    <dt>Service</dt><dd>${o.service} (${o.operator})</dd>
    <dt>Route</dt><dd>${cityLabel(state.from)} → ${cityLabel(state.to)} · ${state.date}</dd>
    <dt>Departure</dt><dd>${o.dep_time} · arrives ${o.arr_time}</dd>
    <dt>Seats</dt><dd>${b.seats.map(s => "S" + s).join(", ")}</dd>
    <dt>Passenger</dt><dd>${p.name}, ${p.age} · ${p.email || "—"}</dd>
    <dt>Booking ref</dt><dd>${b.booking_id} · status ${b.status}</dd>`;
  const fare = b.fare_per_seat, n = b.seats.length, gst = Math.round(b.fare_total * 0.05);
  $("fareBox").innerHTML = `Fare ${inr(fare)} × ${n} seats = ${inr(b.fare_total)}<br>GST (5%) ${inr(gst)}<br><strong>Total payable ${inr(b.fare_total + gst)}</strong> (payment disabled in demo)`;
}
$("checkoutBtn").addEventListener("click", async () => {
  const btn = $("checkoutBtn"); btn.disabled = true; btn.textContent = "Preparing booking…";
  try {
    const r = await fetch(`/api/v1/bookings/${state.booking.booking_id}/checkout`, { method: "POST" });
    const j = await r.json();
    const msg = $("checkoutMsg"); msg.hidden = false;
    if (r.status === 402 && j.error === "payment_disabled") {
      state.booking.status = "PREPARED";
      msg.textContent = `Booking ${j.booking_id} prepared (status: PREPARED). Payment is disabled in this demo sandbox — no charge was made and no ticket was issued.`;
      renderSummary(); btn.textContent = "Payment disabled — booking prepared";
    } else { msg.textContent = "Unexpected checkout response: " + JSON.stringify(j); btn.disabled = false; btn.textContent = "Proceed to payment"; }
  } catch (e) { const m = $("checkoutMsg"); m.hidden = false; m.textContent = "Checkout failed: " + e.message; btn.disabled = false; btn.textContent = "Proceed to payment"; }
});
loadCities(); buildCalendar();
