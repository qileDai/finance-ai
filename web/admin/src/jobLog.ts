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
    const phaseRaw = (item as JobLogLine).phase ?? (item as Record<string, unknown>).phase;
    const phase =
      phaseRaw != null && String(phaseRaw).trim() ? String(phaseRaw).trim() : undefined;
    const line: JobLogLine = { level, message };
    if (time) line.time = time;
    if (phase) line.phase = phase;
    out.push(line);
  }
  return out;
}

export const ACTIVATION_LOG_BANNER = "—— 账号激活开始 ——";
export const NNC1_LOG_BANNER = "—— NNC1 填表开始 ——";

export type JobLogPhase = "register" | "activation" | "nnc1";

export function splitLogPhases(
  lines: JobLogLine[],
  opts?: { formStatus?: string; activationStatus?: string }
): Record<JobLogPhase, JobLogLine[]> {
  const buckets: Record<JobLogPhase, JobLogLine[]> = {
    register: [],
    activation: [],
    nnc1: [],
  };
  let current: JobLogPhase = "register";
  const formSt = (opts?.formStatus || "").trim().toLowerCase();
  const actSt = (opts?.activationStatus || "").trim().toLowerCase();
  const failBucket: JobLogPhase | "" =
    formSt === "failed" ? "nnc1" : actSt === "failed" ? "activation" : "";
  const lastI = lines.length - 1;
  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];
    const msg = asLogText(line.message).trim();
    if (msg.includes(ACTIVATION_LOG_BANNER) || msg === ACTIVATION_LOG_BANNER) {
      current = "activation";
      continue;
    }
    if (msg.includes(NNC1_LOG_BANNER) || msg === NNC1_LOG_BANNER) {
      current = "nnc1";
      continue;
    }
    const phase = (line.phase || "").trim();
    if (phase === "register" || phase === "activation" || phase === "nnc1") {
      current = phase;
      buckets[phase].push(line);
      continue;
    }
    const lv = (line.level || "").toUpperCase();
    if (i === lastI && failBucket && (lv === "ERROR" || lv === "CRITICAL")) {
      buckets[failBucket].push(line);
      continue;
    }
    buckets[current].push(line);
  }
  return buckets;
}

export function logLineClass(level: string): string {
  const lv = (level || "INFO").toUpperCase();
  if (lv === "ERROR" || lv === "CRITICAL") return "job-log-line is-error";
  if (lv === "WARNING" || lv === "WARN") return "job-log-line is-warning";
  return "job-log-line";
}
