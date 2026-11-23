/**
 * Persian presentation helpers.
 *
 * Everything the user reads is formatted here, so a number can never be shown
 * in Latin digits in one screen and Persian digits in another, and a date is
 * always the Jalali date the product speaks in. No value is *derived* here:
 * balances, prices and counts all arrive from the API.
 */

const FA_DIGITS = ["۰", "۱", "۲", "۳", "۴", "۵", "۶", "۷", "۸", "۹"];

/** Latin digits and thousands separators → Persian. */
export function toFa(value: string | number): string {
  return String(value)
    .replace(/[0-9]/g, (d) => FA_DIGITS[Number(d)])
    .replace(/,/g, "٬")
    .replace(/\./g, "٫");
}

export function formatNumber(value: number): string {
  if (!Number.isFinite(value)) return toFa(0);
  return toFa(Math.round(value).toLocaleString("en-US"));
}

export function formatMoney(toman: number): string {
  return `${formatNumber(toman)} تومان`;
}

/** One credit figure, spoken the way a student would: hours, then minutes. */
export function formatCredit(seconds: number): string {
  const totalMinutes = Math.max(0, Math.floor((seconds || 0) / 60));
  const hours = Math.floor(totalMinutes / 60);
  const minutes = totalMinutes % 60;
  if (hours > 0 && minutes > 0) return `${toFa(hours)} ساعت و ${toFa(minutes)} دقیقه`;
  if (hours > 0) return `${toFa(hours)} ساعت`;
  return `${toFa(minutes)} دقیقه`;
}

/** Split form for the balance card: a big unit and its remainder. */
export function splitCredit(seconds: number): { value: string; unit: string; rest: string } {
  const totalMinutes = Math.max(0, Math.floor((seconds || 0) / 60));
  const hours = Math.floor(totalMinutes / 60);
  const minutes = totalMinutes % 60;
  if (hours > 0) {
    return {
      value: toFa(hours),
      unit: "ساعت",
      rest: minutes > 0 ? `${toFa(minutes)} دقیقه` : "",
    };
  }
  return { value: toFa(minutes), unit: "دقیقه", rest: "" };
}

export function formatSize(bytes: number): string {
  const value = Math.max(0, Number(bytes) || 0);
  if (value < 1024) return `${toFa(value)} بایت`;
  if (value < 1024 * 1024) return `${toFa(Math.round(value / 1024))} کیلوبایت`;
  return `${toFa((value / (1024 * 1024)).toFixed(1))} مگابایت`;
}

const JALALI_MONTHS = [
  "فروردین",
  "اردیبهشت",
  "خرداد",
  "تیر",
  "مرداد",
  "شهریور",
  "مهر",
  "آبان",
  "آذر",
  "دی",
  "بهمن",
  "اسفند",
];

const div = (a: number, b: number) => Math.trunc(a / b);
const mod = (a: number, b: number) => a - Math.trunc(a / b) * b;

function jalCal(jy: number): { leap: number; gy: number; march: number } {
  const breaks = [
    -61, 9, 38, 199, 426, 686, 756, 818, 1111, 1181, 1210, 1635, 2060, 2097, 2192,
    2262, 2324, 2394, 2456, 3178,
  ];
  const gy = jy + 621;
  let leapJ = -14;
  let jp = breaks[0];
  let jump = 0;
  for (let i = 1; i < breaks.length; i += 1) {
    const jm = breaks[i];
    jump = jm - jp;
    if (jy < jm) break;
    leapJ = leapJ + div(jump, 33) * 8 + div(mod(jump, 33), 4);
    jp = jm;
  }
  let n = jy - jp;
  leapJ = leapJ + div(n, 33) * 8 + div(mod(n, 33) + 3, 4);
  if (mod(jump, 33) === 4 && jump - n === 4) leapJ += 1;
  const leapG = div(gy, 4) - div((div(gy, 100) + 1) * 3, 4) - 150;
  const march = 20 + leapJ - leapG;
  if (jump - n < 6) n = n - jump + div(jump + 4, 33) * 33;
  let leap = mod(mod(n + 1, 33) - 1, 4);
  if (leap === -1) leap = 4;
  return { leap, gy, march };
}

function g2d(gy: number, gm: number, gd: number): number {
  let d =
    div((gy + div(gm - 8, 6) + 100100) * 1461, 4) +
    div(153 * mod(gm + 9, 12) + 2, 5) +
    gd -
    34840408;
  d = d - div(div(gy + 100100 + div(gm - 8, 6), 100) * 3, 4) + 752;
  return d;
}

function d2g(jdn: number): { gy: number; gm: number; gd: number } {
  let j = 4 * jdn + 139361631;
  j = j + div(div(4 * jdn + 183187720, 146097) * 3, 4) * 4 - 3908;
  const i = div(mod(j, 1461), 4) * 5 + 308;
  const gd = div(mod(i, 153), 5) + 1;
  const gm = mod(div(i, 153), 12) + 1;
  const gy = div(j, 1461) - 100100 + div(8 - gm, 6);
  return { gy, gm, gd };
}

function d2j(jdn: number): { jy: number; jm: number; jd: number } {
  const gy = d2g(jdn).gy;
  let jy = gy - 621;
  const r = jalCal(jy);
  const jdn1f = g2d(r.gy, 3, r.march);
  let k = jdn - jdn1f;
  if (k >= 0) {
    if (k <= 185) {
      return { jy, jm: 1 + div(k, 31), jd: mod(k, 31) + 1 };
    }
    k -= 186;
  } else {
    jy -= 1;
    k += 179;
    if (r.leap === 1) k += 1;
  }
  return { jy, jm: 7 + div(k, 30), jd: mod(k, 30) + 1 };
}

/** Gregorian calendar date → Jalali date (Persian calendar, no libraries). */
export function toJalali(date: Date): { jy: number; jm: number; jd: number } {
  return d2j(g2d(date.getFullYear(), date.getMonth() + 1, date.getDate()));
}

/** Parse the API's ISO timestamps; an unparseable value yields null. */
export function parseDate(iso: string | null | undefined): Date | null {
  if (!iso) return null;
  const value = String(iso);
  // SQLite-style "2026-01-02 03:04:05" has no zone; the API writes UTC.
  const normalized = value.includes("T") ? value : value.replace(" ", "T");
  const withZone = /[zZ]|[+-]\d{2}:?\d{2}$/.test(normalized) ? normalized : `${normalized}Z`;
  const date = new Date(withZone);
  return Number.isNaN(date.getTime()) ? null : date;
}

export function formatDate(iso: string | null | undefined): string {
  const date = parseDate(iso);
  if (!date) return "—";
  const { jy, jm, jd } = toJalali(date);
  return `${toFa(jd)} ${JALALI_MONTHS[jm - 1]} ${toFa(jy)}`;
}

/** A short relative stamp for lists, falling back to the date after a week. */
export function formatRelative(iso: string | null | undefined): string {
  const date = parseDate(iso);
  if (!date) return "—";
  const diff = Date.now() - date.getTime();
  const minute = 60_000;
  const hour = 60 * minute;
  const day = 24 * hour;
  if (diff < 0) return formatDate(iso);
  if (diff < minute) return "همین حالا";
  if (diff < hour) return `${toFa(Math.floor(diff / minute))} دقیقه پیش`;
  if (diff < day) return `${toFa(Math.floor(diff / hour))} ساعت پیش`;
  if (diff < 2 * day) return "دیروز";
  if (diff < 7 * day) return `${toFa(Math.floor(diff / day))} روز پیش`;
  return formatDate(iso);
}
