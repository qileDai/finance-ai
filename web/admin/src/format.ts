/** 管理后台时间展示：本地时区 YYYY-MM-DD HH:mm:ss */

function pad2(n: number): string {
  return n < 10 ? `0${n}` : String(n);
}

export function formatDateTime(raw: string | null | undefined): string {
  if (raw == null) return "-";
  const s = String(raw).trim();
  if (!s) return "-";

  // 已是目标格式则原样返回
  if (/^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$/.test(s)) return s;

  const d = new Date(s);
  if (Number.isNaN(d.getTime())) {
    // 不可解析：去掉 T/Z 尽量可读
    const m = s.match(
      /^(\d{4}-\d{2}-\d{2})[T\s](\d{2}:\d{2}:\d{2})/
    );
    if (m) return `${m[1]} ${m[2]}`;
    return s.length > 19 ? s.slice(0, 19).replace("T", " ") : s;
  }

  return (
    `${d.getFullYear()}-${pad2(d.getMonth() + 1)}-${pad2(d.getDate())} ` +
    `${pad2(d.getHours())}:${pad2(d.getMinutes())}:${pad2(d.getSeconds())}`
  );
}
