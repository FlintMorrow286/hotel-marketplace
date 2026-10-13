import type { PayoutLine } from "@/api/types";
import { formatDate } from "@/utils/format";
import { formatMoney, LINE_KIND_LABEL } from "@/utils/payout";

/**
 * What a payout is made of, one row per booking. A correction line is a booking that was paid out
 * earlier and has changed since (a refund after the payout): its amounts are the difference.
 */
export function PayoutLines({ lines, currency }: { lines: PayoutLine[]; currency: string }) {
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-left text-sm sm:min-w-[34rem]">
        <caption className="sr-only">Состав выплаты по броням</caption>
        <thead className="text-xs uppercase tracking-eyebrow text-text-secondary">
          <tr>
            <th scope="col" className="py-2 pr-4 font-medium">Бронь</th>
            <th scope="col" className="hidden py-2 pr-4 font-medium sm:table-cell">Даты</th>
            <th scope="col" className="py-2 pr-4 text-right font-medium">Сумма</th>
            <th scope="col" className="hidden py-2 pr-4 text-right font-medium sm:table-cell">Комиссия</th>
            <th scope="col" className="py-2 text-right font-medium">К выплате</th>
          </tr>
        </thead>
        <tbody>
          {lines.map((line) => (
            <tr key={line.booking_id} className="border-t border-border-default align-top">
              <td className="py-2 pr-4">
                <span className="font-mono text-xs">{line.code}</span>
                <span className="block text-text-secondary">{line.property_name}</span>
                <span className="block text-xs text-text-tertiary sm:hidden">
                  {formatDate(line.checkin_date)} — {formatDate(line.checkout_date)}
                </span>
                {line.kind === "adjustment" ? (
                  <span className="block text-xs text-feedback-warning-text">
                    {LINE_KIND_LABEL.adjustment}: бронь изменилась после выплаты
                  </span>
                ) : null}
              </td>
              <td className="hidden py-2 pr-4 whitespace-nowrap text-text-secondary sm:table-cell">
                {formatDate(line.checkin_date)} — {formatDate(line.checkout_date)}
              </td>
              <td className="py-2 pr-4 text-right whitespace-nowrap">
                {formatMoney(line.gross_amount, currency)}
              </td>
              <td className="hidden py-2 pr-4 text-right whitespace-nowrap sm:table-cell">
                {formatMoney(line.commission_amount, currency)}
              </td>
              <td className="py-2 text-right font-medium whitespace-nowrap">
                {formatMoney(line.net_amount, currency)}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
