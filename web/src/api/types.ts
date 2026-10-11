/**
 * TypeScript types mirroring the backend Pydantic schemas.
 *
 * Source of truth: app/modules/<module>/schemas.py. When a schema changes on the
 * backend, update it here too. Types are deliberately exact (no unions the
 * backend does not produce) so mistakes surface at compile time.
 *
 * Notes on tricky fields:
 *  - checkin_time/checkout_time are Python `datetime.time` → serialized as
 *    "HH:MM:SS" strings. Parse, do not feed them to `new Date()`.
 *  - id fields are Postgres uuid, returned as text. Never Number()-cast.
 *  - address is free-form JSONB; may be `{}`.
 */

export type PropertyType = "hotel" | "apartment" | "house" | "room" | "hostel";

/**
 * Property-type labels — the mirror of the partner cabinet's select, so the
 * guest's eyebrow never shows the raw `hotel` key the DB stores. Same one
 * place as the amenity catalog above.
 */
export const PROPERTY_TYPE_LABELS: Record<PropertyType, string> = {
  hotel: "Отель",
  apartment: "Квартира",
  house: "Дом",
  room: "Комната",
  hostel: "Хостел",
};

/** The Russian label, or the key itself if the catalog lost the entry. */
export function propertyTypeLabel(type: string | null | undefined): string {
  if (!type) return "";
  return PROPERTY_TYPE_LABELS[type as PropertyType] ?? type;
}

export type PropertyStatus =
  "draft" | "pending_moderation" | "published" | "blocked";
export type BookingStatus =
  | "hold"
  | "paid"
  | "confirmed"
  | "cancelled"
  | "failed"
  | "conflict"
  | "no_show"
  | "refunded";

/** "HH:MM:SS" from the backend's datetime.time. */
export type TimeString = string;

export interface Address {
  street?: string;
  city?: string;
  settlement?: string;
  district?: string;
  region?: string;
  postal_code?: string;
  house_number?: string;
  building?: string;
  apartment?: string;
  [key: string]: string | undefined;
}

/** Public catalog item - GET /v1/properties. */
export interface PropertyPublic {
  id: string;
  name: string;
  slug: string | null;
  property_type: PropertyType;
  city: string;
  timezone: string;
  checkin_time: TimeString;
  checkout_time: TimeString;
  currency: string;
  lat: number | null;
  lng: number | null;
  address: Address;
  /** Property photos — the first is the catalog cover. Empty until the partner
   * uploads some (`POST /v1/partner/properties/{id}/photos`). */
  photos?: Photo[];
  /** Amenity keys from the backend catalog (app.config.amenities). Empty until
   * the partner picks some in the cabinet. */
  amenities?: string[];
  /** Cheapest night across the property's room types. Null when the property
   * has no rooms yet — published but not yet bookable. */
  min_price?: number | null;
}

/**
 * The amenity catalog, mirrored from `app/config/amenities.py`.
 *
 * The backend owns the keys and the labels; this copy is what the UI renders,
 * so a guest and a partner never see two names for the same key. A key the
 * catalog lost still renders — as its own key, not a crash.
 */
export const AMENITY_CATALOG: { key: string; label: string }[] = [
  { key: "wifi", label: "Wi-Fi" },
  { key: "parking", label: "Парковка" },
  { key: "pool", label: "Бассейн" },
  { key: "breakfast", label: "Завтрак" },
  { key: "kitchen", label: "Кухня" },
  { key: "washer", label: "Стиральная машина" },
  { key: "conditioner", label: "Кондиционер" },
  { key: "heating", label: "Отопление" },
  { key: "tv", label: "Телевизор" },
  { key: "balcony", label: "Балкон" },
  { key: "sea_view", label: "Вид на море" },
  { key: "pets", label: "Можно с животными" },
  { key: "smoking", label: "Можно курить" },
  { key: "family", label: "Семейный" },
  { key: "accessibility", label: "Доступность" },
  { key: "gym", label: "Спортзал" },
  { key: "spa", label: "Спа" },
  { key: "transfer", label: "Трансфер" },
];

/** The Russian label for a key, or the key itself if the catalog lost it. */
export function amenityLabel(key: string): string {
  return AMENITY_CATALOG.find((a) => a.key === key)?.label ?? key;
}


/** One property photo: `full` is the sized-down original, `thumb` the card view. */
export interface Photo {
  id: string;
  full: string;
  thumb: string;
}

/** One page of the catalog: the items plus what the UI needs to page. */
export interface PropertyPage {
  items: PropertyPublic[];
  total: number;
  limit: number;
  offset: number;
}

export interface PropertyOut extends PropertyPublic {
  id: string;
  partner_id: string;
  status: PropertyStatus;
  created_at: string;
}

export interface UnitTypeOut {
  id: string;
  property_id: string;
  name: string;
  capacity: number;
  total_units: number;
  base_price?: number | null;
  cancellation_policy?: string | null;
}

/**
 * A rate plan: the price schedule of one unit type.
 *
 * `commission_rate` is the platform's share of the nights this plan prices;
 * `null` means the platform default applies.
 */
export interface RatePlan {
  id: string;
  unit_type_id: string;
  name: string;
  cancellation_policy: string;
  active: boolean;
  commission_rate?: number | null;
}

export interface GuestInfo {
  name: string;
  email: string;
  phone: string;
}

export interface HoldRequest {
  unit_type_id: string;
  checkin: string; // ISO date "YYYY-MM-DD"
  checkout: string; // ISO date "YYYY-MM-DD"
  guest: GuestInfo;
}

export interface BookingLineOut {
  date: string; // ISO date
  price: number;
}

export interface BookingOut {
  id: string;
  /** Pays for, cancels or refunds this booking. Only the hold and the lookup
   * answer with it; the client keeps it for the tab (see api/client.ts). */
  access_token?: string;
  code: string;
  status: BookingStatus;
  total_amount: number;
  hold_expires_at: string | null; // ISO datetime, null unless status === "hold"
  checkin_date: string;
  checkout_date: string;
  lines: BookingLineOut[];
}

/**
 * A booking as the partner sees it: the guest's contacts and the booking's
 * origin, which the guest's own BookingOut deliberately omits.
 */
export interface PartnerBookingOut {
  id: string;
  code: string;
  status: BookingStatus;
  total_amount: number;
  commission_rate: number;
  commission_amount: number;
  origin: "web" | "channel";
  source_channel: string | null;
  guest_name: string;
  guest_email: string;
  guest_phone: string;
  property_id: string;
  property_name: string;
  unit_type_id: string;
  unit_type_name: string;
  checkin_date: string; // ISO date
  checkout_date: string; // ISO date
  created_at: string; // ISO datetime
  lines: BookingLineOut[];
}

export interface AvailabilityDay {
  date: string; // ISO date
  available: number;
  hold: number;
  sold: number;
  free: number;
  closed: boolean;
  price: number;
}

/** One cell of the partner chessboard. */
export interface CalendarDay {
  date: string;
  available: number;
  hold: number;
  sold: number;
  free: number;
  closed: boolean;
  price: number;
  min_stay: number;
}

export interface CalendarUnit {
  unit_type_id: string;
  unit_type_name: string;
  property_id: string;
  property_name: string;
  total_units: number;
  rate_plan_id: string | null;
  days: CalendarDay[];
}

export interface CalendarResponse {
  date_from: string;
  date_to: string;
  units: CalendarUnit[];
}

export interface IcalFeed {
  id: string;
  token: string;
  enabled: boolean;
  created_at: string;
  url: string;
}

export interface IcalSubscription {
  id: string;
  url: string;
  enabled: boolean;
  last_synced_at: string | null;
  last_status: "pending" | "ok" | "error";
  last_error: string | null;
  last_blocked: number;
}

export interface SyncResult {
  status: "ok" | "error" | "skipped";
  blocked?: number;
  cleared?: number;
  error?: string;
}

export interface ApiKeyOut {
  id: string;
  label: string;
  key_prefix: string;
  enabled: boolean;
  last_used_at: string | null;
  created_at: string;
}

/** Only ever returned by the create endpoint; never stored anywhere. */
export interface ApiKeyWithSecret extends ApiKeyOut {
  key: string;
}

export interface WebhookOut {
  id: string;
  url: string;
  event_types: string[];
  enabled: boolean;
  last_delivery_at: string | null;
  last_status: "success" | "failed" | null;
  last_error: string | null;
  created_at: string;
}

/** The partner's Telegram binding: a one-time link code plus the bound chat. */
export interface TelegramStatus {
  link_code: string;
  chat_id: string | null;
  linked_at: string | null;
}

export interface OutboxEvent {
  id: string;
  aggregate: string;
  aggregate_id: string;
  event_type: string;
  payload: Record<string, unknown>;
  status: "pending" | "delivering" | "published" | "failed";
  attempts: number;
  max_attempts: number;
  last_error: string | null;
  happened_at: string;
  published_at: string | null;
  delivered_ok: number;
  delivered_fail: number;
}

export interface OutboxMetrics {
  pending: number;
  delivering: number;
  failed: number;
  scheduled_for_retry: number;
  median_latency_sec: number;
  oldest_pending_sec: number;
  /** How deep the queue may grow before bulk events are shed. */
  depth_limit: number;
  /** How old a pending event may be before the strip flags it. */
  lag_alert_sec: number;
  /** Events dropped by the backlog limit since the counter began. */
  shed_total: number;
  /** How many logical shards the queue is sliced into. */
  shard_count: number;
  /** How many delivery workers drain those shards in parallel. */
  workers: number;
}

export interface ReconciliationEvent {
  id: string;
  event_type: string;
  status: "pending" | "delivering" | "published" | "failed";
  happened_at: string;
  last_error: string | null;
  expected_subscribers: number;
  delivered_ok: number;
}

export interface ReconciliationRow {
  booking_id: string;
  code: string;
  status: string;
  checkin_date: string;
  checkout_date: string;
  total_amount: number;
  created_at: string;
  delivery_status: ReconciliationDeliveryStatus;
  event: ReconciliationEvent | null;
}

export type ReconciliationDeliveryStatus =
  "delivered" | "queued" | "failed" | "undelivered" | "partial" | "no_listener";

export interface ReconciliationReport {
  date_from: string | null;
  date_to: string | null;
  summary: Record<ReconciliationDeliveryStatus, number> & { total: number };
  bookings: ReconciliationRow[];
}

export interface AvailabilityResponse {
  unit_type_id: string;
  date_from: string;
  date_to: string;
  days: AvailabilityDay[];
}

export interface TokenResponse {
  access_token: string;
  token_type: string;
}

export interface AuthMe {
  partner_id: string;
  scope: string;
}

export interface HoldConflict {
  detail: string;
  reason: "not_available" | "conflict";
}

export interface ApiError {
  detail: string;
  reason?: string;
}

// ---- admin ----------------------------------------------------------------

export type AdminPropertyStatus =
  "draft" | "pending_moderation" | "published" | "blocked";

export interface AdminProperty {
  id: string;
  name: string;
  property_type: PropertyType;
  city: string;
  status: AdminPropertyStatus;
  created_at: string;
  partner_email: string;
}

export interface CommissionPartner {
  partner_id: string;
  partner_email: string;
  bookings: number;
  gross: number;
  commission: number;
}

export interface CommissionReport {
  from: string | null;
  to: string | null;
  total: {
    bookings: number;
    gross: number;
    commission: number;
  };
  partners: CommissionPartner[];
}
