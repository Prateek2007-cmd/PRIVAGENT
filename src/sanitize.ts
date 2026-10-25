/** Small DOM-free helpers for rendering untrusted page/model text.
 *
 * Everything that leaves the backend is untrusted: page titles, evidence
 * strings and final answers all echo remote content. These helpers escape
 * first and only turn validated http(s) URLs into anchors.
 */

const HTML_ESCAPES: Record<string, string> = {
  "&": "&amp;",
  "<": "&lt;",
  ">": "&gt;",
  '"': "&quot;",
  "'": "&#39;",
};

export function escapeHtml(text: string): string {
  return text.replace(/[&<>"']/g, ch => HTML_ESCAPES[ch] ?? ch);
}

/** Returns a normalized http(s) URL, or null for javascript:/data:/inline
 * credentials and malformed strings. */
export function safeHttpUrl(raw: string): string | null {
  try {
    const u = new URL(raw);
    if (u.protocol !== "http:" && u.protocol !== "https:") return null;
    if (!u.hostname) return null;
    if (u.username || u.password) return null;
    return u.href;
  } catch {
    return null;
  }
}

/** Only raster data URLs or bare base64 are accepted for the inspector. */
export function safeImageSrc(screenshot: string | null | undefined): string | null {
  if (!screenshot) return null;
  const s = screenshot.trim();
  if (/^data:image\/(?:jpeg|jpg|png|webp|gif);base64,[A-Za-z0-9+/=\s]+$/i.test(s)) return s;
  if (/^[A-Za-z0-9+/=\s]+$/.test(s)) return `data:image/jpeg;base64,${s.replace(/\s+/g, "")}`;
  return null;
}

export function linkLabel(url: string): string {
  try {
    return new URL(url).hostname.replace(/^www\./, "") || "Open page";
  } catch {
    return "Open page";
  }
}

/** Escape the whole string, then wrap markdown/bare http(s) links. */
export function linkify(text: string): string {
  const re = /\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)|(https?:\/\/[^\s<"')]+)/g;
  const parts: string[] = [];
  let last = 0;
  let m: RegExpExecArray | null;
  while ((m = re.exec(text)) !== null) {
    parts.push(escapeHtml(text.slice(last, m.index)));
    const label = m[1] ?? m[3];
    const href = safeHttpUrl(m[2] ?? m[3]);
    if (href) {
      parts.push(
        `<a href="${escapeHtml(href)}" target="_blank" rel="noopener noreferrer">${escapeHtml(label)} ↗</a>`
      );
    } else {
      parts.push(escapeHtml(m[0]));
    }
    last = re.lastIndex;
  }
  parts.push(escapeHtml(text.slice(last)));
  return parts.join("");
}
