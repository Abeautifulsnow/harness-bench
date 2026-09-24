/** 数字/时间格式化。
 *
 * 关键约定：``null`` 与 ``0`` 必须显示得不一样。
 * PRD §59 规定无定价时 cost 为 null（而不是 0.0）；UI 把 null 显示成 0 会让
 * "成本降到零"这种假象重新出现在图表里——所以这里 null 一律是 "—"。 */

export function fmtNumber(value: number | null | undefined, digits = 2): string {
  if (value === null || value === undefined || Number.isNaN(value)) return "—";
  if (Number.isInteger(value) && digits <= 2) return String(value);
  return value.toFixed(digits);
}

export function fmtRatio(value: number | null | undefined, digits = 1): string {
  if (value === null || value === undefined || Number.isNaN(value)) return "—";
  return `${(value * 100).toFixed(digits)}%`;
}

export function fmtCost(value: number | null | undefined): string {
  if (value === null || value === undefined) return "—";
  if (value === 0) return "$0";
  if (value < 0.01) return `$${value.toFixed(6)}`;
  return `$${value.toFixed(4)}`;
}

export function fmtDelta(value: number | null | undefined, digits = 2): string {
  if (value === null || value === undefined) return "—";
  if (value === 0) return "0";
  return `${value > 0 ? "+" : ""}${value.toFixed(digits)}`;
}

export function fmtPercentDelta(value: number | null | undefined, digits = 2): string {
  if (value === null || value === undefined) return "—";
  if (value === 0) return "0%";
  return `${value > 0 ? "+" : ""}${value.toFixed(digits)}%`;
}

export function fmtMs(value: number | null | undefined): string {
  if (value === null || value === undefined) return "—";
  if (value < 1000) return `${Math.round(value)} ms`;
  return `${(value / 1000).toFixed(2)} s`;
}

export function fmtBytes(value: number): string {
  if (value < 1024) return `${value} B`;
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KB`;
  return `${(value / 1024 / 1024).toFixed(2)} MB`;
}

export function fmtTokens(value: number | null | undefined): string {
  if (value === null || value === undefined) return "—";
  if (value < 1000) return String(value);
  if (value < 1_000_000) return `${(value / 1000).toFixed(1)}k`;
  return `${(value / 1_000_000).toFixed(2)}M`;
}

export function fmtTime(value: string | null | undefined): string {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString("zh-CN", { hour12: false });
}

export function fmtRelative(value: string | null | undefined): string {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  const diff = Date.now() - date.getTime();
  const minutes = Math.round(diff / 60000);
  if (minutes < 1) return "刚刚";
  if (minutes < 60) return `${minutes} 分钟前`;
  const hours = Math.round(minutes / 60);
  if (hours < 24) return `${hours} 小时前`;
  const days = Math.round(hours / 24);
  if (days < 30) return `${days} 天前`;
  return fmtTime(value);
}

export function shortId(value: string | null | undefined, length = 12): string {
  if (!value) return "—";
  return value.length <= length ? value : value.slice(0, length);
}

export function shortCommit(value: string | null | undefined): string {
  if (!value) return "—";
  return value.slice(0, 8);
}
