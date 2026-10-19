import { readFileSync } from "node:fs";

const html = readFileSync(new URL("../index.html", import.meta.url), "utf-8");
let consoleTs = "";
let importMain = "";
let css = "";
try { consoleTs = readFileSync(new URL("../src/taskconsole.ts", import.meta.url), "utf-8"); } catch {}
try { importMain = readFileSync(new URL("../src/main.ts", import.meta.url), "utf-8"); } catch {}
try { css = readFileSync(new URL("../src/styles/taskconsole.css", import.meta.url), "utf-8"); } catch {}

const tests = [];
function test(name, fn) {
  try { fn(); tests.push(`  PASS ${name}`); }
  catch (e) { tests.push(`  FAIL ${name} — ${e.message}`); process.exitCode = 1; }
}

test("console section exists with id", () => {
  if (!html.includes('id="console"')) throw new Error("missing #console");
});
test("console has aria-live region", () => {
  if (!html.includes('aria-live="polite"')) throw new Error("missing aria-live");
});
test("console has role=dialog modal", () => {
  if (!html.includes('role="dialog"')) throw new Error("missing dialog");
});
test("start button disabled when textarea empty (logic)", () => {
  if (!consoleTs.includes("startBtn.disabled")) throw new Error("no disable logic");
});
test("task console wired into main.ts", () => {
  if (!importMain.includes("taskconsole")) throw new Error("main.ts does not import taskconsole");
});
test("console points at backend endpoints", () => {
  if (!consoleTs.includes("/api/task")) throw new Error("no /api/task");
  if (!consoleTs.includes("/ws/client")) throw new Error("no /ws/client");
});
test("state banner derives from backend taskState only", () => {
  if (!consoleTs.includes("taskState")) throw new Error("no taskState handling");
});
test("reduced-motion honored for console animation", () => {
  if (!css.includes("prefers-reduced-motion")) throw new Error("no reduced-motion fallback");
});

console.log("console structure tests\n" + tests.join("\n"));
