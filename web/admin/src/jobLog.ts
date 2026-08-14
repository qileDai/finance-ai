import type { JobLogLine } from "./api";

/** Coerce any log payload field to readable text (never bare [object Object]). */
export function asLogText(value: unknown): string {
  if (value == null) return "";
  if (typeof value === "string") return value;
  if (typeof value === "number" || typeof value === "boolean") return String(value);
  try {
    return JSON.stringify(value);
  } catch {
    return Object.prototype.toString.call(value);
  }
}

/**
 * Normalize API messages (string[] or {level,message}[]) for step-log UI.
 * Avoids Array.join / String(obj) producing literal "[object Object]".
 */
export function normalizeLogLines(
  raw: Array<JobLogLine | string | Record<string, unknown>> | undefined | null
): JobLogLine[] {
  if (!raw?.length) return [];
  const out: JobLogLine[] = [];
  for (const item of raw) {
    if (typeof item === "string") {
      const message = item.trim();
      if (message && message !== "[object Object]") {
        out.push({ level: "INFO", message });
      }
      continue;
    }
    if (!item || typeof item !== "object") continue;

    const level = asLogText(
      (item as JobLogLine).level ?? (item as Record<string, unknown>).lvl ?? "INFO"
    )
      .toUpperCase()
      .trim() || "INFO";

    let message = asLogText((item as JobLogLine).message);
    // Legacy / mistaken shapes: whole object stringified, or text under other keys
    if (!message || message === "[object Object]") {
      const alt =
        (item as Record<string, unknown>).msg ??
        (item as Record<string, unknown>).text ??
        (item as Record<string, unknown>).content;
      message = asLogText(alt);
    }
    if (!message || message === "[object Object]") continue;

    const timeRaw = (item as JobLogLine).time;
    const time = timeRaw != null && String(timeRaw).trim() ? String(timeRaw).trim() : undefined;
    out.push(time ? { level, message, time } : { level, message });
  }
  return out;
}

export function logLineClass(level: string): string {
  const lv = (level || "INFO").toUpperCase();
  if (lv === "ERROR" || lv === "CRITICAL") return "job-log-line is-error";
  if (lv === "WARNING" || lv === "WARN") return "job-log-line is-warning";
  return "job-log-line";
}
