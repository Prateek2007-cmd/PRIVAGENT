// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import "../lib/content";

/**
 * Stream 4: readable page text must include result rows/cards.
 *
 * Search/booking results are commonly plain <li> rows (service + fare
 * + times) or <article> cards (product name + price) with no heading
 * or <p> inside. Before the fix, extractText() returned the results
 * heading and the action buttons but NONE of the result content, so
 * the planner re-filled the search form forever believing the search
 * had produced nothing. Fixture is fictional (Harbour City rail +
 * shop cards) — never the operator's prompts or sandbox data.
 */

const extractText = () =>
  (
    window.__navigatorAgent as unknown as {
      extractText: (maxLen?: number) => string;
    }
  ).extractText();

function stubOffsetParent(): void {
  // jsdom has no layout: offsetParent is null for everything. Mirror
  // a real browser — visible elements have an offsetParent, elements
  // inside a `hidden` section do not.
  Object.defineProperty(window.HTMLElement.prototype, "offsetParent", {
    configurable: true,
    get(this: HTMLElement) {
      let el: HTMLElement | null = this;
      while (el) {
        if (el.hasAttribute && el.hasAttribute("hidden")) return null;
        el = el.parentElement;
      }
      return document.body;
    },
  });
}

beforeEach(() => {
  stubOffsetParent();
});

afterEach(() => {
  document.body.innerHTML = "";
});

describe("readable text includes result rows and cards", () => {
  it("includes <li> result rows with fares", () => {
    document.body.innerHTML = `
      <main>
        <section id="step-search" hidden>
          <h1>Book trains &amp; buses</h1>
        </section>
        <section id="step-results">
          <h2>Results</h2>
          <ul>
            <li class="result">
              <div><div class="svc">Lake Express 118</div>
              <div class="muted">Harbour Rail · Train</div>
              <div class="times">09:25 → 15:05</div></div>
              <div><div class="fare">₹655</div>
              <button>Select seats</button></div>
            </li>
            <li class="result">
              <div><div class="svc">Coastal Line 204</div>
              <div class="muted">Harbour Rail · Train</div>
              <div class="times">07:10 → 12:40</div></div>
              <div><div class="fare">₹812</div>
              <button>Select seats</button></div>
            </li>
          </ul>
        </section>
      </main>`;
    const text = extractText();
    expect(text).toContain("Lake Express 118");
    expect(text).toContain("₹655");
    expect(text).toContain("Coastal Line 204");
    expect(text).toContain("₹812");
    // The hidden search section's content must not leak in.
    expect(text).not.toContain("Book trains");
  });

  it("includes <article> product cards with prices", () => {
    document.body.innerHTML = `
      <main>
        <div class="grid">
          <article class="card">
            <h3>Harbour Buds Pro</h3>
            <div class="price">₹2,499</div>
            <div>In stock</div>
          </article>
          <article class="card">
            <h3>Lake Watch S</h3>
            <div class="price">₹1,899</div>
            <div>Out of stock</div>
          </article>
        </div>
      </main>`;
    const text = extractText();
    expect(text).toContain("Harbour Buds Pro");
    expect(text).toContain("₹2,499");
    expect(text).toContain("Lake Watch S");
    expect(text).toContain("₹1,899");
  });
});
