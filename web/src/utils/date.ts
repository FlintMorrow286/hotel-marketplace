/**
 * Calendar day math on `YYYY-MM-DD` strings.
 *
 * Dates are kept as plain strings end-to-end: the API sends ISO dates, and
 * `new Date('2026-11-10')` is UTC midnight, so rounding through a Date object
 * can shift a day for a guest west of London. String arithmetic cannot.
 */

/** Whole days in a stay `[from, to)`: checkout is not a night. */
export function daysBetween(from: string, to: string): number {
  if (!from || !to) return 0;
  const a = Date.UTC(Number(from.slice(0, 4)), Number(from.slice(5, 7)) - 1, Number(from.slice(8, 10)));
  const b = Date.UTC(Number(to.slice(0, 4)), Number(to.slice(5, 7)) - 1, Number(to.slice(8, 10)));
  const days = Math.round((b - a) / 86_400_000);
  return days > 0 ? days : 0;
}

export function addDays(date: string, days: number): string {
  const timestamp = Date.UTC(
    Number(date.slice(0, 4)),
    Number(date.slice(5, 7)) - 1,
    Number(date.slice(8, 10)),
  );
  return new Date(timestamp + days * 86_400_000).toISOString().slice(0, 10);
}
