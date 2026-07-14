// Typed client for the operator backend (Phase 1, read-only).
// Base URL is configurable so the deployed frontend can point at the hosted API.

export const API_BASE =
  process.env.NEXT_PUBLIC_API_BASE ?? "http://localhost:8077";

export type SkuState =
  | "candidate"
  | "keyword-clustered"
  | "drafted"
  | "live"
  | "testing"
  | "winner"
  | "killed";

export interface StoreSummary {
  store: string;
  // Catalog path this store runs: general (default), fashion, or both.
  mode?: "general" | "fashion" | "both";
  updated: string | null;
  categories: number;
  skus_total: number;
  skus_by_state: Record<SkuState, number>;
}

export interface Overview {
  stores: StoreSummary[];
  totals: {
    stores: number;
    categories: number;
    skus: number;
    skus_by_state: Record<SkuState, number>;
    dossiers: number;
    niche_launches: number;
    found_keywords: number;
    keywords_gated_pass: number;
    keyword_segments: number;
    found_trends: number;
    trends_rising: number;
    trends_breakout: number;
  };
}

export interface Sku {
  id: string;
  title: string | null;
  cogs: number | null;
  price: number | null;
  state: SkuState;
  created: string | null;
}

export interface Category {
  slug: string;
  keyword: string | null;
  sv: number | null;
  capture_bucket: string | null;
  state: string | null;
  recon: string | null;
  skus: Sku[];
}

export interface StoreDetail {
  summary: StoreSummary;
  categories: Category[];
}

export interface Dossier {
  slug: string;
  is_pool: boolean;
  has_report: boolean;
  report: string | null;
}

// ---- Product Research: Google-competitor spy roster ----
export type SpyTier = "T1" | "T2" | "T3";

export interface SpyStore {
  domain: string;
  monthly_visits: number | null;
  tier: SpyTier | null;
  us_share: number | null;
  // Other markets the store also draws traffic from (TrendTrack geo split, US excluded).
  other_markets?: { country: string; share: number }[];
  products: number | null;
  category: string | null;
  active_meta_ads: number | null;
  google_ads_count: number | null;
  google_ads_shopping: number | null;
  google_ads_capped: boolean;
  google_ads_last_shown: string | null;
  created: string | null;
  history: number[];
  mom_pct: number | null;
  flag_remove: boolean;
  verdict: "GENERAL" | "NICHE" | null;
  dominant_dept: string | null;
  dominant_share: number | null;
  distinct_departments: number | null;
  n_collections: number | null;
  has_traffic: boolean;
}

export interface SpyRoster {
  updated: string | null;
  history_months: string[];
  totals: {
    tracked: number;
    by_tier: Record<SpyTier, number>;
    flagged_downtrend: number;
    general: number;
    niche: number;
    running_google_ads: number;
  };
  ads_checked: string | null;
  stores: SpyStore[];
}

// ---- Keyword Research: general-store discovery funnel ----
export interface KeywordLane {
  id: string;
  n: number;
  name: string;
  role: "GATE" | "validation";
  what: string;
}

// One expanded sub-keyword of a head keyword — a candidate catalog entry (the SKU plan).
export interface KeywordSegment {
  term: string;
  sv: number | null;
  source: string | null; // "shopping_scan" | "serp" | "dataforseo"
  price_band: string | number | null;
  in_catalog: boolean;
}

export interface SegmentSource {
  id: string;
  name: string;
  what: string;
}

export interface KeywordCandidate {
  store: string;
  keyword: string | null;
  sv: number | null;
  gate: string | null;
  capture_bucket: string | null;
  momentum: number | null;
  score: number | null;
  validation_lanes: string[];
  n_validation: number;
  segments: KeywordSegment[];
  n_segments: number;
}

export interface KeywordDiscovery {
  gate_sv: number;
  lanes: KeywordLane[];
  capture_buckets: string[];
  segment_sources: SegmentSource[];
  stores: string[];
  totals: { stores: number; candidates: number; gated_pass: number; segments: number };
  candidates: KeywordCandidate[];
}

// ---- SKU Plan engine (the DRIVER of research) ----
export type SkuRole = "anchor" | "standalone" | "combine" | "supporting";
export type SkuSelected = "build" | "built" | "hold";

export interface SourceQuota {
  google_shopping: number;
  competitor_catalog: number;
  marketplace: number;
  amazon: number;
  meta: number;
}

export interface SkuPlanRow {
  term: string | null;
  sv: number | null;
  source: string | null;
  price_band: string | number | null;
  in_catalog: boolean;
  role: SkuRole;
  selected: SkuSelected;
  budget: number;
  quota: SourceQuota | null;
}

export interface SkuPlanAnchor {
  term: string;
  sv: number | null;
  role: "anchor";
  selected: "build";
  budget: number;
  quota: SourceQuota;
  combine_children: string[];
}

export interface SkuPlanHead {
  store: string | null;
  keyword: string;
  sv: number | null;
  gate: string | null;
  capture_bucket: string | null;
  volume_tier: string;
  volume_tier_label: string;
  products_per_build: number;
  products_per_build_base: number;
  anchor: SkuPlanAnchor;
  segments: SkuPlanRow[];
  counts: {
    build: number;
    tier2_build: number;
    combine: number;
    supporting: number;
    built: number;
    hold: number;
  };
  demand: { tier2_total: number; tier2_captured: number; coverage_pct_actual: number };
  research_budget: number;
  anchor_share_pct: number;
  build_terms: string[];
}

// Users & Access — per-person, per-app RBAC (ported from NN Master Settings). The
// active user's access map drives the shell app list + FinanceGuard. `restricted` =
// app ids the active user can't open. `role` stays for backward-compatible gates.
export type AppRole = "owner" | "admin" | "rep";
export type AccessRole = "owner" | "rep";
export interface ActiveUser {
  id: string;
  name: string;
  position: string;
  photo: string | null;
}
export interface Access {
  role: AccessRole;
  restricted: string[];
  user: ActiveUser | null;
  apps: Record<string, AppRole>;
  roles: AppRole[];
  role_labels: Record<AppRole, string>;
  app_ids: string[];
}
export interface Person {
  id: string;
  name: string;
  position: string;
  photo: string | null;
  has_password: boolean;
  can_reveal?: boolean;
  access: Record<string, AppRole>;
}
export interface UsersPayload {
  people: Person[];
  active_user_id: string | null;
  roles: AppRole[];
  role_labels: Record<AppRole, string>;
  app_ids: string[];
}
export interface PersonInput {
  name: string;
  password?: string;
  position?: string;
  photo?: string | null;
  access?: Record<string, AppRole>;
}

// ---- Auth (real name/password sign-in on top of the RBAC above) ----
export interface AuthUser {
  id: string;
  name: string;
  position: string;
  photo: string | null;
  has_password: boolean;
  access: Record<string, AppRole>;
}
export interface AuthStatus {
  authenticated: boolean;
  user: AuthUser | null;
}
export interface AuthResult {
  token: string;
  user: AuthUser;
}

export interface SkuPlanSettings {
  anchor_pct: number;
  coverage_pct: number;
  products_per_build: number;
  dedup_cap: number;
  source_weights: SourceQuota;
  role: {
    standalone_sv_floor: number;
    standalone_head_frac: number;
    supporting_sv_ceiling: number;
  };
}

// Per-source supply state. A `dry` source has no NEW products to find right now; its find-budget
// is redistributed onto the live sources so the daily listing count is still met, and it's
// re-checked until products reappear (then flips back to live and its weight returns).
export type SupplyState = "live" | "dry";

export interface SourceSupplyEntry {
  state: SupplyState;
  checked: string | null;
  last_new: string | null;
  configured_weight?: number;
  effective_weight?: number;
}

export interface SourceSupplyMap {
  google_shopping: SourceSupplyEntry;
  competitor_catalog: SourceSupplyEntry;
  marketplace: SourceSupplyEntry;
  amazon: SourceSupplyEntry;
  meta: SourceSupplyEntry;
}

export interface SupplyBlock {
  sources: SourceSupplyMap;
  dry: string[];
  adapted: boolean;
  note: string;
}

export interface SkuPlan {
  settings: SkuPlanSettings;
  source_labels: Record<string, string>;
  google_paths: { id: string; label: string; what: string }[];
  supply: SupplyBlock;
  stores: string[];
  segment_sources: SegmentSource[];
  dedup: { cap: number; rule: string; vision_status: string; vision_hint?: string | null };
  totals: {
    heads: number;
    build_skus: number;
    tier2_builds: number;
    combine: number;
    supporting: number;
    built: number;
    research_budget: number;
  };
  heads: SkuPlanHead[];
}

export interface SkuPlanResearchResult {
  store: string;
  keyword: string;
  terms: string[];
  jobs: JobRecord[];
  count: number;
}

// The products the discovery lanes have found for ONE head keyword — the drill-in over a
// SKU-plan row. Aggregated across all lanes; empty until 'Fire research' collects products.
export interface SkuPlanFound {
  store: string;
  keyword: string;
  found: number;
  validated: number;
  products: LaneProduct[];
  // Summary of the last Gemini photo-duplicate run for this keyword (null = never ran).
  photo_dedup?: {
    checked_at: string | null;
    images_checked: number;
    images_skipped: number;
    duplicate_groups: number;
    duplicates: number;
    dedup_cap: number;
  } | null;
}

export interface PhotoDedupResult {
  store: string;
  keyword: string;
  checked_at: string;
  images_checked: number;
  images_skipped: number;
  duplicate_groups: number;
  duplicates: number;
}

// Gate #3 result — the store-check + 1688-check chain auto-fired once the found products are
// confirmed worth pursuing. Both surface in the Sourcing Match tab.
export interface FoundValidatedResult {
  store: string;
  keyword: string;
  chained: boolean;
  checks: string[];
  jobs: JobRecord[];
}

// The persisted SKU-plan weight split (Settings editor) — saved override + the hard-coded
// defaults so the UI can show a reset target.
export interface SkuPlanSettingsState {
  settings: SkuPlanSettings;
  defaults: SkuPlanSettings;
}

// ---- Niche & Keyword Research: dossier detail ----
export interface KeywordSvPoint {
  year: number;
  month: number;
  sv: number | null;
}

export interface DossierKeyword {
  keyword: string | null;
  sv: number | null;
  cpc: number | null;
  competition: string | null;
  competition_index: number | null;
  low_bid: number | null;
  high_bid: number | null;
  sv_series: KeywordSvPoint[];
}

export interface DossierTrend {
  keyword: string | null;
  geo: string | null;
  trend_verdict: string | null;
  evergreen_verdict: string | null;
  growth_ratio: number | null;
  peak_month: number | null;
  trough_month: number | null;
  mean_interest: number | null;
  raw_series: number[];
}

export interface SerpGeoSummary {
  n_listings: number;
  unique_merchants: number;
  price_low: number;
  price_med: number;
  price_high: number;
  top_sources: string[];
}

export interface DossierDoc {
  name: string;
  kind: "report" | "strategy" | "icp" | "framing" | "other";
}

export interface BuyerQuote {
  text: string;
  source: string | null;
  source_ref: string | null;
  tags: string[];
}

export interface BuyerVoice {
  file: string;
  n_quotes: number;
  quotes: BuyerQuote[];
  tag_counts: Record<string, number>;
  top_tags: { tag: string; count: number }[];
  source_counts: Record<string, number>;
  key_findings: { label: string | null; text: string }[];
}

export interface DomainRow {
  name: string;
  status: string | null;
  source: string | null;
  price_usd: number | null;
  valuation_usd: number | null;
  is_premium: boolean | null;
  bid_count: number | null;
  end_time_iso: string | null;
  brandability_score: number | null;
  listing_url: string | null;
  notes: string | null;
}

export interface DossierDomains {
  file: string | null;
  brandable: DomainRow[];
  auctions: DomainRow[];
}

export interface DossierDetail {
  slug: string;
  is_pool: boolean;
  location: string | null;
  keyword_file: string | null;
  summary: {
    n_keywords: number;
    total_sv: number;
    top_keyword: string | null;
    top_sv: number | null;
    n_trends: number;
    n_docs: number;
    n_images: number;
    n_quotes: number;
    n_domains: number;
  };
  keywords: DossierKeyword[];
  trends: DossierTrend[];
  serp_summary: Record<string, Record<string, SerpGeoSummary>> | null;
  buyer_voice: BuyerVoice | null;
  domains: DossierDomains | null;
  docs: DossierDoc[];
  images: string[];
}

// ---- Pain-first niche-discovery pipeline (01b) ----
export interface PainNiche {
  slug: string;
  verdict: "GO" | "HOLD" | "SKIP" | null;
  score: number | null;
  max_score: number | null;
  gates_passed: number;
  gates_total: number;
  has_verdict: boolean;
}

export interface PainGate {
  id: string;
  label: string;
  passed: boolean;
}

export interface PainTrend {
  keyword: string | null;
  geo: string | null;
  trend_verdict: string | null;
  evergreen_verdict: string | null;
  growth_ratio: number | null;
  mean_interest: number | null;
  slope: number | null;
  peak_month: number | null;
}

export interface PainSignal {
  signal: string;
  count: number | null;
  present: boolean | null;
  examples: string[];
}

export interface PainMetaAds {
  n_ads_90d: number | null;
  top_brands: string[] | null;
  longest_ad_days: number | null;
  hook_archetype: string | null;
  offer_archetype: string | null;
  visual_archetype: string | null;
  emotional_angle: string | null;
  scale_signal: string | null;
}

export interface PainReview {
  asin: string | null;
  rating: number | null;
  title: string | null;
  body: string | null;
}

export interface PainDetail {
  slug: string;
  verdict: "GO" | "HOLD" | "SKIP" | null;
  score: number | null;
  max_score: number | null;
  gates: PainGate[];
  growing_count: number | null;
  max_median: number | null;
  table_stakes: { feature: string; count: number; total: number }[];
  low_star_reviews: PainReview[];
  trends: PainTrend[];
  sample_messages: PainSignal[];
  repurchase: { count: number; verdict: string; examples: string[] } | null;
  meta_ads: PainMetaAds | null;
  docs: DossierDoc[];
}

// ---- Product Research: best-seller rank movers ----
export type Top30State = "entered" | "climbing" | null;

export interface Mover {
  handle: string | null;
  title: string | null;
  class: "gainer" | "faller" | null;
  rank: number | null;
  prior_rank: number | null;
  rank_delta: number | null;
  price: number | null;
  image: string | null;
  url: string | null;
  is_fresh: boolean | null;
  days_old: number | null;
  in_top30: boolean;
  top30: Top30State;
}

export interface MoverStore {
  store: string;
  prior_date: string | null;
  latest_date: string | null;
  latest_count: number | null;
  prior_count: number | null;
  comparable_depth: number | null;
  summary: string | null;
  movers: Mover[];
  n_gainers: number;
  n_fallers: number;
  n_entered_top30: number;
  n_climbing_top30: number;
}

export interface BestsellerMovers {
  generated_at: string | null;
  top30: number;
  totals: Record<string, number>;
  stores: MoverStore[];
}

// ---- Spy roster admission: discovered candidates + remove/admit writers ----
export interface SpyCandidate {
  domain: string;
  monthly_visits: number | null;
  tier: SpyTier | null;
  mom: number | string | null;
  us_share: number | null;
  products: number | null;
  note: string | null;
}
export interface SpyCandidates {
  source: string | null;
  count: number;
  candidates: SpyCandidate[];
  discover_cmd: string;
}

// ---- Product Research: validated-sales competitor keyword scan ----
// Starts from the competitor's OWN keyword set (its /search?q=<kw> — catches deep-catalog products),
// then validates each against the store's own merchandising: its Best-Sellers collection + the
// keyword's category collection (robots-allowed collection JSON — `?sort_by=best-selling` is robots-
// blocked by BD). tier: "bestseller" > "category" > "listed" (deep-catalog, no sales signal).
export interface CompetitorScanProduct {
  handle: string | null;
  title: string | null;
  price: string | number | null;
  url: string | null;
  relevance_rank?: number | null;
  tier?: "bestseller" | "category" | "listed";
  collection?: string | null; // the store collection that validates it (e.g. "Best Sellers")
  collection_rank?: number | null; // its position within that collection
  bestseller_rank?: number | null; // set only for the bestseller tier
  validated?: boolean; // tier !== "listed"
}
export interface CompetitorScan {
  domain: string;
  keyword: string;
  geo?: string;
  keyword_matches?: number; // total products the store lists for the keyword
  bestsellers_seen: number; // total products across the validating collections
  bestseller_collections?: string[];
  category_collections?: string[];
  n_validated: number;
  validated: CompetitorScanProduct[];
  results?: CompetitorScanProduct[]; // validated (ranked) + listed, capped
  note?: string;
  has_browser_cdp?: boolean;
}

// ---- Product Research: per-store current best-seller boards (live top-ranked) ----
export interface BestsellerProduct {
  rank: number | null;
  handle: string | null;
  title: string | null;
  vendor: string | null;
  price: number | null;
  compare_at: number | null;
  image: string | null;
  url: string | null;
  created_at: string | null;
  variant_count: number | null;
}

export interface BestsellerStore {
  store: string;
  date: string | null;
  count: number | null;
  depth: number | null;
  price_note: string | null;
  products: BestsellerProduct[];
}

export interface Bestsellers {
  generated_at: string | null;
  totals: { stores: number; products: number };
  stores: BestsellerStore[];
}

// ---- Product Research: store duplicate-products scanner (new-products feed) ----
export interface NewProduct extends Mover {
  competitor: string | null;
  in_store: boolean;
  matched_term: string | null;
  matched_store: string | null;
}

export interface NewProducts {
  generated_at: string | null;
  catalog_terms: number;
  totals: { scanned: number; duplicates: number; new: number; shown: number };
  products: NewProduct[];
}

// ---- Per-keyword product drill-in (shared across the discovery lanes) ----
// Each gated keyword's lane can carry many FOUND products (the funnel finds e.g. 50
// listings per keyword, validates a subset). Rendered as a "N found · M validated" count
// with a window-in-window drill-in modal listing the products themselves.
export interface LaneProduct {
  title: string | null;
  price: number | null;
  cogs: number | null;
  orders: number | null;
  sold_count: number | null;
  reviews: number | null;
  pct_gain: number | null;
  ad_longevity_days: number | null;
  dup_creatives: number | null;
  image: string | null;
  url: string | null;
  store_name: string | null;
  note: string | null;
  rank: number | null;
  validated: boolean;
  // Amazon lane (DFS merchant/amazon) — demand + quality signals
  bought_past_month?: number | null;
  rating?: number | null;
  price_regular?: number | null;
  source?: string | null;
  // Product-duplicate check — stamped after a /api/sku-plan/photo-dedup run. Matches by TITLE
  // + PHOTO (Gemini vision) and scans the store catalog.
  photo_dup?: boolean;
  dup_group?: number | null;
  dup_group_size?: number | null;
  photo_dup_drop?: boolean;
  in_store?: boolean; // already in this store's catalog (category or SKU title match)
  in_store_label?: string | null;
}

export interface LaneTotals {
  signals: number;
  keywords: number;
  found: number;
  validated: number;
}

// ---- Product Research: Amazon Movers & Shakers feed (spy lane 4) ----
export interface AmazonMover {
  store: string;
  keyword: string | null;
  sv: number | null;
  gate: string | null;
  score: number | null;
  pct_gain: number | null;
  reviews: number | null;
  seen: string | null;
  found: number;
  validated: number;
  products: LaneProduct[];
}

export interface AmazonMovers {
  lane: { id: string; n: number; name: string; what: string; signal: string };
  ingest_cmd: string;
  totals: LaneTotals;
  movers: AmazonMover[];
}

// ---- Product Research: Marketplace movers feed (spy lane 5: AliExpress / Temu / 1688) ----
export interface MarketplaceMover {
  store: string;
  keyword: string | null;
  sv: number | null;
  gate: string | null;
  score: number | null;
  orders: number | null;
  sold_count: number | null;
  cogs: number | null;
  price: number | null;
  image: string | null;
  url: string | null;
  store_name: string | null;
  note: string | null;
  seen: string | null;
  found: number;
  validated: number;
  products: LaneProduct[];
}

export interface MarketplaceMovers {
  lane: { id: string; n: number; name: string; what: string; signal: string };
  ingest_cmd: string;
  sources: string[];
  totals: LaneTotals;
  movers: MarketplaceMover[];
}

// ---- Marketplace native best-seller browse launchers (Temu / AliExpress / 1688) ----
export interface MarketplaceBrowseCategory {
  label: string;
  term: string;
}
export interface MarketplaceBrowseTimeframe {
  label: string;
  value: string;
}
export interface MarketplaceBrowsePlatform {
  id: string;
  name: string;
  color: string;
  supports_all: boolean;
  timeframes: MarketplaceBrowseTimeframe[];
  default_timeframe?: string;
  timeframe_hint?: string;
  all_url: string | null;
  search_tpl: string;
  sort_note: string;
}
export interface MarketplaceBrowse {
  platforms: MarketplaceBrowsePlatform[];
  categories: MarketplaceBrowseCategory[];
  note: string;
}

// ---- Amazon native demand boards (Best Sellers + New Releases) launcher ----
export interface AmazonBrowseView {
  id: string;
  name: string;
  color: string;
  base_url: string;
  what: string;
}
export interface AmazonBrowseCategory {
  label: string;
  slug: string;
}
export interface AmazonBrowse {
  views: AmazonBrowseView[];
  categories: AmazonBrowseCategory[];
  search_tpl: string;
  note: string;
}

// ---- Cross-feed: a validated keyword/trend → find products for it ----
export interface FindProductsPlatform {
  id: string;
  name: string;
  color: string;
  url: string | null;
  sort_note: string | null;
  timeframes: MarketplaceBrowseTimeframe[];
}
export interface FindProducts {
  keyword: string;
  sv: number | null;
  source: string; // "keyword" | "trend" | "event" — where the seed came from
  products?: LaneProduct[]; // REAL finds: AliExpress + Temu + 1688 + Amazon for the keyword/trend
  found?: number;
  warming?: boolean; // cold keyword fetching in the background — client re-polls until it lands
  weighting: { google_competitor: string; marketplace: string };
  keyword_research: {
    what: string;
    seed: string;
    seed_sv: number | null;
    seed_tier: { name: string; label: string };
    floor_vs_focus: string;
    expand_cmd: string;
    then: string;
  };
  competitor_scan: {
    weight: string;
    what: string;
    depth: string;
    n_stores: number;
    stores: { domain: string; products_url: string }[];
    scan_cmd: string;
    google_shopping: { label: string; what: string; url: string };
  };
  marketplace: { weight: string; what: string; platforms: FindProductsPlatform[] };
  dedup: { what: string; scan_cmd: string };
  vision_check: { status: string; what: string; note: string };
  pricing: PricingRules;
}

// ---- Listing price rules + suggestion ----
export interface PricingRule {
  id: string;
  name: string;
  rule: string;
  basis: string;
  priority: number;
  status: string; // "locked" | "draft" | "setting"
  multiple?: number;
  endings?: number[]; // charm-ending rule: [2.99, 4.99, 7.99, 9.99]
  note?: string;
}
export interface PricingSettings {
  undercut: { enabled: boolean; amount: number; basis: string; note: string };
  compare_at: {
    enabled: boolean;
    tiers: number[]; // [30, 40, 50] percent off
    weights: number[];
    scope: string;
    note: string;
  };
  per_variant: boolean;
}
// The editable knobs the operator saves in Settings → Listing (source of truth).
export interface PricingEditable {
  undercut_enabled: boolean;
  undercut_amount: number;
  marketplace_markup: number;
  compare_at_enabled: boolean;
  compare_at_tiers: number[];   // [30, 40, 50] percent off
  compare_at_weights: number[];
}
export interface PricingRules {
  rules: PricingRule[];
  editable: PricingEditable;
  defaults: PricingEditable;
  marketplace_markup: number;
  charm: string;
  charm_endings: number[]; // [2.99, 4.99, 7.99, 9.99]
  settings: PricingSettings;
  per_variant: boolean;
  note: string;
}
export interface PricingSettingsState {
  settings: PricingEditable;
  defaults: PricingEditable;
}
export interface PriceSuggest {
  inputs: { cogs: number | null; competitor_low: number | null };
  undercut_enabled: boolean;
  undercut: number | null;
  markup_floor: number | null;
  marketplace_markup: number;
  recommended: number | null;
  compare_at_pct: number | null;
  compare_at: number | null;
  basis: string | null;
  conflict: boolean;
  margin_pct: number | null;
  note: string;
}
export interface CompareAtTier {
  discount_pct: number;
  tiers: number[];
}

// ---- Product Research: Meta dropship-winners feed (spy lane 6) ----
export interface MetaWinner {
  store: string;
  keyword: string | null;
  sv: number | null;
  gate: string | null;
  score: number | null;
  ad_longevity_days: number | null;
  dup_creatives: number | null;
  price: number | null;
  image: string | null;
  url: string | null;
  store_name: string | null;
  note: string | null;
  seen: string | null;
  found: number;
  validated: number;
  products: LaneProduct[];
}

export interface MetaDropship {
  lane: { id: string; n: number; name: string; what: string; signal: string };
  ingest_cmd: string;
  totals: LaneTotals;
  winners: MetaWinner[];
}

// ---- Listings: per-category drill-down (recon + specs + SKU galleries) ----
export interface SkuImage {
  file: string;
  path: string;
  role: string | null;
  order: number;
}

export interface SupplierRefs {
  count: number;
  manifest: boolean;
  files: { file: string; path: string }[];
}

export interface CategorySku {
  id: string;
  slug: string;
  title: string | null;
  state: SkuState | null;
  cogs: number | null;
  price: number | null;
  created: string | null;
  tags: string[];
  variants: { size: string | null; price: string | null }[];
  seo_title: string | null;
  seo_description: string | null;
  body_html: string | null;
  images: SkuImage[];
  n_images: number;
  supplier_refs: SupplierRefs;
  has_spec: boolean;
}

export interface DedupCluster {
  skus: string[];
  size: number | null;
  violation: boolean;
}

export interface DedupReport {
  skus_scanned: number | null;
  images_hashed: number | null;
  max_per_product: number | null;
  n_clusters: number;
  n_violations: number;
  clusters: DedupCluster[];
}

export interface CategoryDetail {
  store: string;
  slug: string;
  keyword: string | null;
  sv: number | null;
  capture_bucket: string | null;
  state: string | null;
  product_type: string | null;
  vendor: string | null;
  category_fullname: string | null;
  subject: string | null;
  spec_status: string | null;
  n_spec_skus: number;
  docs: string[];
  dedup: DedupReport | null;
  skus: CategorySku[];
}

// ---- Image-QA (VisionScan) gate — operator-review-first per-image verdicts ----
export type ImageQaVerdict = "PASS" | "FIX" | "REJECT";
export type ImageQaFixKind = "auto-clean" | "upscale" | "language-rewrite" | "regen" | "resource";

export interface ImageQaRow {
  sku: string | null;
  file: string | null;
  path: string | null;
  role: string | null;
  order: number | null;
  tier: "hero" | "gallery";
  expected_product?: string;
  needs_vision?: boolean;
  verdict: ImageQaVerdict;
  fix_kind: ImageQaFixKind | null;
  blocks_go_live: boolean;
  reasons: string[];
  notes?: string;
  observations?: Record<string, unknown> | null;
  // operator's per-image change before applying:
  operator_override?: { verdict: ImageQaVerdict; fix_kind?: ImageQaFixKind | null } | null;
}

export interface ImageQaSku {
  id: string | null;
  images: ImageQaRow[];
}

export interface ImageQaPolicy {
  verdicts: string[];
  fix_kinds: string[];
  tiers: { hero: string; gallery: string };
  gate: string;
  vision_model: string;
  store_language: string;
  model_available: boolean;
  review_first: boolean;
}

export interface ImageQaGoLive {
  blocks: boolean;
  verdict: "BLOCKED" | "CLEAR";
  blocking_images: { sku: string | null; file: string | null; fix_kind?: ImageQaFixKind | null }[];
  auto_clean_queue?: { sku: string | null; file: string | null; path: string | null }[];
  upscale_queue?: { sku: string | null; file: string | null; path: string | null }[];
}

export interface ImageQaReport {
  store: string;
  slug: string;
  scanned_at?: string;
  applied_at?: string;
  model_available: boolean;
  policy?: ImageQaPolicy;
  skus?: ImageQaSku[];
  summary: { total: number; PASS: number; FIX: number; REJECT: number; by_fix_kind?: Record<string, number>; scanned?: number };
  go_live: ImageQaGoLive;
  command?: string | null;
  decision_id?: number | null;
  images?: ImageQaRow[];
  handoffs?: Record<string, unknown>;
  persisted?: boolean;
}

export interface ImageQaState {
  policy: ImageQaPolicy;
  report: ImageQaReport | null;
}

export interface ListingMethod {
  id: string;
  name: string;
  tagline: string;
  engine: string;
  best_for: string;
  inputs: string[];
  steps: string[];
  job_spec: string | null;
  // When set, this method runs in a STANDALONE listing app (opens in a new tab) instead of
  // the in-app job runner — the "normal listing" path, which owns the "fix bad images" step.
  external_app?: string;
  external_note?: string;
}

// A way to START a research run (mirrors ListingMethod, grouped by surface).
export type ResearchSurface = "keyword" | "niche" | "product" | "trend" | "marketplace";

// ---- Trend Research: cross-pipeline momentum aggregator ----
export type TrendBucket =
  | "rising"
  | "seasonal"
  | "evergreen"
  | "declining"
  | "other";

export interface TrendRow {
  keyword: string | null;
  geo: string | null;
  trend_verdict: string | null;
  evergreen_verdict: string | null;
  growth_ratio: number | null;
  peak_month: number | null;
  trough_month: number | null;
  mean_interest: number | null;
  raw_series: number[];
  monthly: TrendMonth[];
  sv: number | null;
  monthly_sv?: TrendMonth[];
  related_queries?: TrendRelatedQueries;
  first_date: string | null;
  last_date: string | null;
  growth_week: number | null;
  growth_month: number | null;
  growth_quarter: number | null;
  horizon: TrendHorizon | null;
  breakout: boolean;
  days_old: number | null; // age of the latest data point vs today
  stale: boolean; // data_age > 10d — momentum is "as of" days_old ago, not now
  slug: string;
  pipeline: "keyword-first" | "pain-first";
  bucket: TrendBucket;
}

export type TrendHorizon = "week" | "month" | "quarter";

// Related queries under a head keyword (Google Trends, via DFS). `top` = the
// established sub-searches; `rising` = the ones surging now (the "portable"
// signal) — a rising `change` is a %-increase number or "Breakout" (>5000%).
export interface TrendRelatedQueries {
  top: { query: string; search_interest: number | null }[];
  rising: { query: string; change: number | string | null }[];
}

export interface TrendMonth {
  m: string; // "YYYY-MM"
  v: number; // mean interest 0-100
}

export interface TrendsOverview {
  totals: {
    trends: number;
    rising: number;
    seasonal: number;
    evergreen: number;
    declining: number;
    horizon_week: number;
    horizon_month: number;
    horizon_quarter: number;
    breakout: number;
    stale: number;
    data_age_days: number | null; // age of the freshest signal vs today
  };
  trends: TrendRow[];
}

// ---- News Radar (GDELT news-velocity leading signal) -----------------------
// The EARLIEST layer in the breakout chain: real-world event → news spike →
// Google searches → Shopping demand. News leads the search breakout by hours-to-
// days on acute events (the air-conditioner heatwave play). Each signal watches one
// news theme's article-volume velocity and flags BREAKOUT while search may still be
// flat — the A-grade pre-list lead window. `product_keywords` = what the theme drives.
export interface NewsTimelinePoint {
  date: string; // "YYYYMMDD"
  value: number; // articles that day
}

export interface NewsSignal {
  theme: string;
  geo: string | null;
  product_keywords: string[];
  state: "BREAKOUT" | "RISING" | "FLAT" | "NO_DATA";
  baseline_per_day?: number;
  recent_peak?: number;
  recent_peak_date?: string;
  surge_ratio?: number;
  peak_value?: number;
  peak_date?: string;
  alert_date?: string | null; // first day the curve crossed the breakout threshold
  distinct_outlets?: number | null; // corroboration: a 6-outlet surge ≠ a 1-outlet mention
  top_headlines?: NewsHeadline[]; // the ACTUAL recent stories — the causal "why", not just the theme label
  today_partial?: NewsTimelinePoint | null;
  timeline?: NewsTimelinePoint[];
  discovered?: boolean; // true = open-ended discovery (a NEW story), not the seeded watchlist
  candidate_topics?: string[]; // concrete story words pulled from the headlines (e.g. "egg","saline")
}

// One representative recent story behind a signal — so the operator reads the real cause
// ("UK orders air-con removal") instead of the abstract theme ("air conditioning").
export interface NewsHeadline {
  title: string;
  outlet: string; // source domain
  url?: string;
  seendate?: string; // GDELT "YYYYMMDDTHHMMSSZ"
}

export interface NewsOverview {
  synced_at: string | null;
  synced_ago_seconds: number | null;
  geo: string | null;
  timespan: string | null;
  params: Record<string, unknown>;
  signals: NewsSignal[];
  totals: {
    signals: number;
    breakout: number;
    rising: number;
    flat: number;
    no_data: number;
  };
  has_snapshot: boolean;
}

export interface NewsSyncResult {
  ok: boolean;
  synced_at?: string;
  error?: string;
  snapshot: NewsOverview;
}

// ---- World-events calendar (the PREDICTABLE, dated demand signal) ----------
// Distinct from News Radar (unpredictable breaking events): a curated per-country calendar of
// holidays / seasonal moments / world events. Computed live (no fetch) — each event resolves
// its next occurrence + days-until + a horizon that speaks the listing-plan's timing language.
export type EventHorizon = "now" | "build_ahead" | "later";

export interface CalendarEvent {
  name: string;
  country: string; // ISO-2 or "GLOBAL"
  category: string | null;
  next_date: string; // ISO date of the next occurrence
  days_until: number;
  weeks_until: number;
  lead_weeks: number; // weeks before the date you should already be listed
  horizon: EventHorizon;
  horizon_label: string; // "List now" | "Build ahead" | "Upcoming"
  recurring: boolean; // annual (fixed / easter / season) vs one-off (scheduled dates)
  is_season: boolean; // one of the four seasons (spans a start..end window)
  in_season: boolean; // currently inside the season window (list now)
  season_start: string | null; // ISO date the season window opens
  season_end: string | null; // ISO date the season window closes
  keywords: string[]; // the product keywords this event's demand drives
}

export interface EventsOverview {
  as_of: string;
  country: string;
  within_days: number;
  countries: string[]; // every market present in the calendar (for the country filter)
  events: CalendarEvent[];
  totals: { events: number; now: number; build_ahead: number; later: number };
}

// ---- Product optimization (Shopify revenue × ad-spend ROAS cockpit) --------
// Per-product snapshot across rolling 7/14/30-day windows. v1 is Shopify-only
// (units / revenue / refunds / net) — the Google Ads spend + ROAS columns layer
// in once the ads read path is wired (ads_connected flags whether that store has
// a Google Ads customer id set in Connections). Money is in the STORE's own
// currency; we never sum across currencies.
export interface OptimizationWindow {
  qty: number;
  cv: number; // conversion value (revenue) in store currency
  refunds: number; // refunded amount in window
  orders: number;
  net: number; // cv − refunds (derived server-side)
  cog?: number; // PM invoice-derived landed COGS for this product/window
  profit?: number; // net − cog − ad cost (derived server-side)
  margin_pct?: number | null; // profit / net (derived server-side)
  // Google Ads layer (present once the store's Ads Script pushes per-product rows):
  cost?: number; // ad spend attributed to this product in the window
  clicks?: number;
  impressions?: number;
  conversions?: number; // Google-attributed conversions
  conv_value?: number; // Google-attributed conversion value
}

export interface OptimizationProduct {
  product_id: string | null;
  title: string;
  status: string | null; // ACTIVE / DRAFT / ARCHIVED
  image: string | null;
  variants_count: number | null;
  published_at: string | null; // Shopify publishedAt (the PUBLISHED column)
  tags: string[]; // Shopify tags ∪ app-side tags (powers the Tags filter)
  app_tags?: string[]; // the editable app-side subset (Shopify tags are read-only here)
  windows: Record<string, OptimizationWindow>; // keyed "7" | "14" | "30" | "all" (lifetime)
  // Per-country (market) breakdown — {countryCode: {window: OptimizationWindow}}. Powers the
  // row's globe-expand + the automation's "check all markets" rules. Ad cost is the product's
  // reconciled spend split across markets by revenue share (no per-country ad data exists).
  markets?: Record<string, Record<string, OptimizationWindow>>;
  hidden?: boolean; // server-persisted Exclude flag (pm_optimization_flags)
  note?: string | null; // server-persisted per-product note
}

export interface OptimizationSnapshot {
  store: string;
  synced_at: string | null;
  synced_ago_seconds: number | null;
  currency: string | null;
  data_start_date: string | null; // pull floor (blank = all history)
  windows: number[]; // rolling day-windows [7, 14, 30]; "all" lifetime bucket lives in each window map
  orders_scanned: number;
  truncated: boolean; // hit the page cap — totals are a floor, not exact
  ads_connected: boolean;
  ads_source?: "script" | "allocated" | "reconciled" | null; // "reconciled" = total ad spend tied to the canonical P&L (fin_ad_spend), per-product = script attribution + revenue-share of the remainder
  totals: Record<string, OptimizationWindow>;
  products: OptimizationProduct[];
  has_snapshot: boolean;
  error: string | null;
}

export interface AlertItem {
  store: string;
  product_id: string | null;
  title: string;
  status: string | null;
  kind: string; // bleeding | wasting_ads | refund_spike | thin_margin | winner
  severity: "high" | "medium" | "positive";
  headline: string;
  impact: number;
  roas: number | null;
  revenue: number;
  ad_spend: number;
  profit: number | null;
  margin_pct: number | null;
  refunds: number;
}
export interface AlertsDigest {
  ok: boolean;
  store?: string; // present on the single-store digest
  counts: { high: number; medium: number; positive: number };
  total: number;
  needs_attention: number;
  by_store?: { store: string; high: number; medium: number; positive: number }[]; // cross-store digest only
  items: AlertItem[];
}

export interface OptHistoryEntry {
  id: number;
  at: string;
  field: string; // "status" | "hidden" | "note" | "tags"
  old_val: string | null;
  new_val: string | null;
  label: string | null;
  reverted: boolean;
}

export interface OptSavedFilter {
  name: string;
  state: unknown; // the FilterState blob (opaque to api.ts)
}

export interface VariantBreakdownRow {
  variant_id: string;
  name: string;
  sku: string | null;
  status: string | null;
  price: number | null;
  cost: number | null;
  margin_pct: number | null;
  units: number;
  revenue: number;      // gross conversion value in the window
  refunds: number;      // revenue-share of the product's refunds
  net: number;          // revenue − refunds
  cogs: number | null;
  profit: number | null; // net − COGS
  // Ad layer — null until Google Ads is connected (rendered "—", never a fake 0)
  ad_cost: number | null;
  impressions: number | null;
  clicks: number | null;
  ctr: number | null;
  cpc: number | null;
  roas: number | null;
}
export interface VariantBreakdown {
  ok: boolean;
  error?: string;
  store: string;
  product_id: string;
  product_title: string | null;
  currency: string | null;
  days: number;
  variants: VariantBreakdownRow[];
  kpi: {
    variants: number;
    units: number;
    revenue: number;
    refunds: number;
    net: number;
    cogs: number | null;
    profit: number | null;
    margin_pct: number | null;
    ad_cost?: number | null;
    roas?: number | null;
  };
}

export interface OptimizationSyncResult {
  ok: boolean;
  synced_at?: string;
  error?: string | null;
  snapshot: OptimizationSnapshot;
}

export interface AutomationCondition {
  metric: string; // ad_spend | roas | sales | qty | margin | profit | refunds | net | orders
  op: string; // gt | gte | lt | lte | eq
  value: number;
}
export interface AutomationRule {
  id?: number;
  name: string;
  enabled: boolean;
  window: string; // "7" | "14" | "30" | "all"
  scope: string; // "total" | "any_market" | "all_markets"
  conditions: AutomationCondition[];
  action: string; // draft | exclude | lower_price | optimize_title | optimize_product | flag
}
export interface AutomationMatch {
  rule_id: number;
  rule_name: string;
  action: string;
  action_label: string;
  window: string;
  scope: string;
  market: string | null;
  product_id: string | null;
  title: string;
  image: string | null;
  status: string | null;
  values: {
    ad_spend: number; sales: number; qty: number; refunds: number;
    profit: number | null; margin: number | null; roas: number | null;
  };
}
export interface AutomationLogEntry {
  at: string;
  rule_name: string | null;
  product_id: string | null;
  product_title: string | null;
  market: string | null;
  action: string;
  action_label: string;
  detail: string | null;
  result: string; // applied | flagged | failed | logged
  vals: Record<string, number | null> | null;
}
export interface AutomationEval {
  ok: boolean;
  evaluated_at: string;
  paused?: boolean; // true when the global master switch is OFF
  currency: string | null;
  rule_count: number;
  product_count: number;
  match_count: number;
  matches: AutomationMatch[];
}

// ---- Listing game plan (daily calendar) -----------------------------------
export type PlanHorizon = "now" | "month";
export type PlanWindow = "week" | "month";
export type PlanSource = "keyword" | "trend" | "winning" | "marketplace" | "amazon" | "meta";
// The keyword lanes are EXCLUSIVE: rising-momentum keywords live only in `trending_now`
// (its own weighted lane — replaces the old trend_bias reorder dial); steady keywords split
// list-now / build-ahead. Exclusivity is what makes a separate trending weight safe.
export type PlanCategoryId =
  | "trending_now"
  | "keyword_now"
  | "keyword_ahead"
  | "winning"
  | "marketplace"
  | "amazon"
  | "meta";

export interface PlanItem {
  source: PlanSource;
  category: PlanCategoryId;
  keyword: string | null;
  store: string | null;
  sv: number | null;
  capture_bucket?: string | null;
  gate?: string | null;
  score: number | null;
  n_validation?: number | null;
  momentum: number | string | null;
  trend_bucket?: string | null;
  // Rising-momentum keyword — lives in the exclusive `trending_now` lane.
  is_rising?: boolean;
  horizon: PlanHorizon;
  method: string;
  // Products already FOUND for this keyword in the SKU-plan research (candidate-queue
  // lanes) — what listing actually starts from.
  found_products?: number | null;
  day?: number;
  date?: string;
  price?: number | string | null;
  image?: string | null;
  url?: string | null;
}

export interface PlanDay {
  day: number;
  date: string;
  weekday: string;
  items: PlanItem[];
}

export interface PlanCategory {
  id: PlanCategoryId;
  name: string;
  source: PlanSource;
  weight: number;
  available: number;
  scheduled: number;
}

// A saved per-store Daily-Listings settings bundle (source mix, window, cadence, method).
export interface GameplanConfig {
  window: PlanWindow;
  per_day: number;
  weights: Partial<Record<PlanCategoryId, number>>;
  // DEPRECATED momentum dial — replaced by the `trending_now` lane weight. Still present on
  // older saved plans; read-tolerated, never written by the current UI.
  trend_bias?: number;
  method: string;
  // Whether this batch sets a struck-through compare-at ("was" price). One random
  // 30/40/50%-off tier is drawn PER PRODUCT at build time and applied uniformly to all
  // its variants. Toggleable here so a gameplan can list at the flat price instead.
  // Defaults to true (compare-at on) when absent on older saved plans.
  compare_at?: boolean;
}

export interface Gameplan {
  id: number;
  ts: string;
  updated: string;
  store: string;
  name: string;
  config: GameplanConfig;
  is_default: boolean;
}

export interface ListingPlan {
  params: { window: PlanWindow; per_day: number; days: number; capacity: number; store: string | null };
  start_date: string;
  windows: { id: PlanWindow; label: string; days: number }[];
  stores: string[];
  days: PlanDay[];
  schedule: PlanItem[];
  categories: PlanCategory[];
  weights: Record<PlanCategoryId, number>;
  trend_bias: number;
  sources: { id: string; name: string; count: number; rising?: number }[];
  methods: { id: string; name: string }[];
  totals: {
    pool: number;
    scheduled: number;
    unscheduled: number;
    capacity: number;
    days: number;
  };
}

// ---- Sourcing match (1688 / Alibaba validation gate) ----------------------
export type MatchVerdict = "IDENTICAL" | "UNCERTAIN" | "DIFFERENT";
// Which source grounds the listing build. 1688-first: IDENTICAL -> the 1688 listing is the
// source of truth; DIFFERENT -> fall back to the researched source; UNCERTAIN -> verify first.
export type SourceOfTruth = "1688" | "researched" | "verify";

// The researched product the 1688 candidate was matched AGAINST — the LEFT side of the
// side-by-side comparison (Amazon/competitor/Google listing Product Research found).
export interface SourcingResearchProduct {
  title: string | null;
  price: number | string | null;
  currency: string | null;
  url: string | null;
  image: string | null;
  platform: string | null;
  rating: number | null;
  reviews: number | null;
  variants: string[];
  specs: [string, string][];
}

// How the verdict was produced. "vision" = automated Gemini VLM judge; "agent" = Claude Code
// packet verification; "unverified" = no judgement on file (a stub) — never honoured as truth.
export type MatchVerification = "vision" | "agent" | "unverified" | "operator";

// One of the "N judged" candidates the VLM evaluated for a match — surfaced as image + url
// so the operator can open them and decide whether the AI picked the right one. `picked` =
// the candidate the judge chose as the match.
export interface SourcingCandidate {
  offer_id: string | null;
  url: string | null;
  image: string | null;
  title: string | null;
  verdict: MatchVerdict;
  confidence: number | null;
  sold: number | null;
  supplier: string | null;
  price: number | null;
  currency: string | null;
  matching_variant: string | null;
  differences: string[] | null;
  picked: boolean;
}

// Operator's learning-loop feedback on a single match: was the AI's find good or bad,
// and (if bad) which candidate offer is actually the right one.
export interface MatchFeedback {
  verdict: "good" | "bad";
  correct_offer_id: string | null;
  note: string | null;
  ai_verdict: string | null;
  ai_offer_id: string | null;
  subject: string | null;
  ts: string;
}

// Running tally of the learning loop — the AI's match hit-rate as judged by the operator.
export interface MatchLearning {
  reviewed: number;
  good: number;
  bad: number;
  accuracy: number | null;
}

export interface SourcingMatchRow {
  key: string;
  subject: string | null;
  slug: string | null;
  verdict: MatchVerdict;
  verified: boolean;
  verification: MatchVerification;
  source_of_truth: SourceOfTruth;
  confidence: number | null;
  offer_id: string | null;
  price: number | string | null;
  currency: string | null;
  sold: number | null;
  url: string | null;
  image: string | null;
  n_candidates: number | null;
  note: string | null;
  variants: string[];
  specs: [string, string][];
  candidates: SourcingCandidate[];
  feedback: MatchFeedback | null;
  operator_reviewed: "confirmed" | "corrected" | "rejected" | null;
  corrected_from: string | null;
  research: SourcingResearchProduct | null;
  store: string | null;
  source: string;
}

export interface SourcingMatch {
  available: boolean;
  files: string[];
  results: SourcingMatchRow[];
  totals: { matched: number; unverified: number; IDENTICAL: number; UNCERTAIN: number; DIFFERENT: number };
  learning: MatchLearning;
  commands: { find: string; enrich: string; judge: string };
  dir: string;
  note: string;
}

// ---- Catalog dedup (Step 0, BEFORE sourcing — "already on the store?") ----
export type CatalogVerdict = "ALREADY_LISTED" | "NEW" | "UNCERTAIN";
// What to DO about the verdict. ALREADY_LISTED forks on how many copies already exist:
//   under the cap -> you may add ONE differentiated A/B variant, or optimize the live one;
//   at/over the cap -> don't clone, optimize the existing listing to outcompete.
export type CatalogAction =
  | "SOURCE"
  | "REVIEW"
  | "ADD_VARIANT_OR_OPTIMIZE"
  | "OPTIMIZE_EXISTING";

export interface CatalogScanRow {
  subject: string | null;
  verdict: CatalogVerdict;
  confidence: number | null;
  matched_handle: string | null;
  matched_title: string | null;
  store_price: number | string | null;
  store_currency: string | null;
  store_url: string | null;
  n_checked: number | null;
  n_already_listed: number | null;
  cap: number | null;
  recommended_action: CatalogAction | null;
  store: string | null;
  source: string;
}

export interface CatalogScan {
  available: boolean;
  files: string[];
  indexes: { store: string | null; count: number; generated_at: string | null; file: string }[];
  results: CatalogScanRow[];
  totals: { checked: number; already_listed: number; new: number; uncertain: number };
  commands: { index: string; check: string };
  dir: string;
  note: string;
}

export interface ResearchMethod {
  id: string;
  name: string;
  tagline: string;
  engine: string;
  best_for: string;
  inputs: string[];
  steps: string[];
  job_spec: string | null;
  role?: "discovery" | "support";
}

// ---- Operation shell (the OS-like launcher above the apps) ----
export interface ShellOverview {
  system: string;
  stores: StoreSummary[];
  totals: { stores: number; categories: number; skus: number };
}

// ---- Product Feed & Optimization (GMC feed-readiness) ----
export type FeedSeverity = "error" | "warn";
export type FeedSkuStatus = "pass" | "needs-work";

export interface FeedIssue {
  check: string;
  rule: string;
  detail: string;
  severity: FeedSeverity;
}

export interface FeedCheck {
  check: string;
  rule: string;
  what: string;
}

export interface FeedSku {
  id: string | null;
  title: string | null;
  state: string | null;
  price: number | null;
  n_images: number;
  status: FeedSkuStatus;
  n_issues: number;
  issues: FeedIssue[];
  suggested_title: string | null;
  category: string | null;
  keyword: string | null;
}

export interface FeedCategory {
  slug: string | null;
  keyword: string | null;
  sv: number | null;
  category_fullname: string | null;
  has_category_gid: boolean;
  n_skus: number;
  n_pass: number;
  n_needs_work: number;
  skus: FeedSku[];
}

export interface FeedTopIssue {
  check: string;
  rule: string;
  count: number;
  severity: FeedSeverity;
}

export interface FeedGmc {
  connected: boolean;
  what: string;
  blocker: string;
  next_step: string;
}

export interface FeedReport {
  store: string;
  checks: FeedCheck[];
  limits: {
    title_max: number;
    seo_title_cap: number;
    keyword_head_window: number;
    body_min_chars: number;
  };
  totals: {
    skus: number;
    pass: number;
    needs_work: number;
    categories: number;
    ready_pct: number;
  };
  top_issues: FeedTopIssue[];
  categories: FeedCategory[];
  gmc: FeedGmc;
  note: string;
}

// ---- Phase 2/3: control-layer run-log + execution jobs ----
export interface RunRecord {
  id: number;
  ts: string;
  store: string | null;
  action: string;
  target: string | null;
  status: string;
  detail: string | null;
  output: string | null;
}

export type JobMode = "auto" | "auto-local" | "manual";
export type JobStatus =
  | "queued"
  | "running"
  | "done"
  | "failed"
  | "needs-operator";

export interface JobSpec {
  id: string;
  title: string;
  mode: JobMode;
  surface: string;
  summary: string;
}

export interface JobRecord {
  id: number;
  ts: string;
  updated: string;
  store: string | null;
  spec: string;
  mode: JobMode;
  title: string | null;
  status: JobStatus;
  command: string | null;
  detail: string | null;
  output: string | null;
}

// ---- Phase B: worker / hybrid control ----
export type AutonomyMode = "manual" | "suggest" | "auto";
export type Cadence = "on-demand" | "daily" | "every-3-days" | "weekly";

export interface AutonomyStep {
  step: string;
  title: string;
  description?: string;
  surface: string;
  kind: "job" | "plan";
  spec_mode: JobMode;
  mode: AutonomyMode;
  cadence: Cadence;
  overridden: boolean;
}

export interface WorkerState {
  id: number;
  enabled: boolean;
  status: string;
  last_tick: string | null;
  detail: string | null;
  ticks: number;
  updated: string | null;
}

export interface SchedulerInfo {
  running: boolean;
  poll_seconds: number;
  last_loop: {
    at: string | null;
    ran: number;
    suggested: number;
    stores: number;
    error: string | null;
  };
}

export interface WorkerStatus {
  worker: WorkerState;
  scheduler?: SchedulerInfo;
  running: JobRecord[];
  queued: JobRecord[];
  needs_operator_jobs: JobRecord[];
  pending_decisions: number;
  scheduled: AutonomyStep[];
  counts: {
    running: number;
    queued: number;
    needs_operator: number;
    pending_decisions: number;
  };
}

export interface Learning {
  id: number;
  ts: string;
  kind: string;
  store: string | null;
  signal: string | null;
  reason: string;
  action: string | null;
  decision_id: number | null;
}

export interface Decision {
  id: number;
  ts: string;
  updated: string;
  store: string | null;
  kind: string;
  title: string;
  summary: string | null;
  payload: Record<string, unknown>;
  status: "pending" | "approved" | "rejected";
  source: string | null;
  result_job_id: number | null;
  related_learnings?: Learning[];
}

// A per-business deployment wiring check (Settings → System → Deployment). These two values are
// bootstrap infra (env vars, not app-entered keys): the DB connection the app itself opens, and
// the mounted data volume. The app's role is to CONFIRM + guide, not collect them.
export interface DeployCheck {
  id: string;
  label: string;
  ok: boolean;
  status_ok: string;
  status_warn: string;
  env: string;
  fix: string;
}

export interface Settings {
  api_version: string;
  tenant: string;
  repo_root: string;
  data_root: string;
  data_root_isolated: boolean;
  general_stores_dir: string;
  db_path: string;
  db_backend: string;
  deploy_checks: DeployCheck[];
  cors_origins: string[];
  stores: string[];
  /** Per-store operational health: Shopify auth capability + catalog mode + last data-sync. */
  stores_health?: {
    store: string;
    mode: "general" | "fashion" | "both";
    shopify_auth: boolean;
    last_sync: { at: string; status: "done" | "failed" | "skipped"; detail: string | null } | null;
  }[];
  /** slug → operator-set display name (falls back to the slug). The label shown everywhere. */
  store_labels?: Record<string, string>;
  counts: { runs: number; jobs: number };
  storage?: StorageStats;
  job_specs: JobSpec[];
  sku_states: SkuState[];
  capture_buckets: string[];
}

// Storage footprint — the two numbers that drive the Railway bill at scale (DB size + volume
// used %), surfaced so growth is visible before it costs money. Both reads are O(1) server-side.
export interface StorageStats {
  db_backend: string;
  db_bytes: number | null;
  volume_total_bytes: number | null;
  volume_used_bytes: number | null;
  volume_free_bytes?: number | null;
  volume_used_pct?: number | null;
}

// ---- Operation costs (estimates from editable unit-cost assumptions) ----
export interface CostUnit {
  value: number;
  unit: string;
  label: string;
  source: string;
  edited?: boolean;
}
export interface CostBreakdownLine {
  driver: string;
  label: string;
  qty: number;
  unit: string;
  unit_cost: number;
  cost: number;
}
export interface CostSpec {
  spec: string;
  title: string;
  mode: string;
  surface: string;
  category: string;
  est_cost: number;
  breakdown: CostBreakdownLine[];
}
export interface CostGroup {
  category: string;
  label: string;
  specs: CostSpec[];
  subtotal: number;
}
export interface CostListingType {
  id: string;
  label: string;
  images: number;
  est_cost: number;
  breakdown: CostBreakdownLine[];
  note: string;
}
export interface CostAgentModel {
  id: string;
  label: string;
  input: number;
  output: number;
  source: string;
}
export interface CostInfraOption {
  id: string;
  label: string;
  value: number;
  role?: "worker_api" | "frontend";
  selected: boolean;
  resource?: string;
  note: string;
}
export interface CostOverview {
  store: string | null;
  unit_costs: Record<string, CostUnit>;
  agent: {
    selected: string;
    models: CostAgentModel[];
    note: string;
  };
  fixed_monthly: {
    options: CostInfraOption[];
    worker_options: CostInfraOption[];
    frontend_options: CostInfraOption[];
    services: { id: string; label: string; value: number; note: string }[];
    selected: string;
    selected_worker: string;
    selected_frontend: string;
    total: number;
    note: string;
  };
  per_spec: CostSpec[];
  cost_groups: CostGroup[];
  per_listing: {
    specs: string[];
    est_cost: number;
    types: CostListingType[];
    plain: CostListingType | null;
    branded: CostListingType | null;
    note: string;
  };
  spend_to_date: {
    by_spec: { spec: string; runs: number; unit_cost: number; cost: number }[];
    total: number;
    total_jobs: number;
  };
  projection: {
    listings_per_month: number;
    stores: number;
    total_listings: number;
    per_listing: number;
    variable_monthly: number;
    storage_monthly: number;
    fixed_monthly: number;
    projected_monthly: number;
    variable_share: number;
  };
  storage: {
    months_retained: number;
    cumulative_listings: number;
    db_gb: number;
    db_cost: number;
    image_gb: number;
    image_cost: number;
    total: number;
    note: string;
  };
  scenarios: {
    label: string;
    stores: number;
    per_store: number;
    total_listings: number;
    variable: number;
    storage: number;
    fixed: number;
    monthly: number;
    variable_share: number;
  }[];
  note: string;
}

// ---- Assistant (read-only copilot) ----------------------------------------
// A cheap-model copilot that reads pipeline state and PROPOSES actions. It never
// executes anything — each proposed action is rendered as a confirm-button that
// routes through an EXISTING control endpoint (createJob / setAutonomy / decision
// approve-reject / workerTick / setWorkerEnabled) or a pure client navigation.
// The union is kept permissive (type: string + optional fields) because the
// backend allowlist-validates before returning; the UI switches on `type`.
export interface AssistantAction {
  type: string; // "run_job" | "set_autonomy" | "approve_decision" | "reject_decision"
  //               | "worker_tick" | "set_worker_enabled" | "navigate"
  label: string;
  spec?: string;
  store?: string | null;
  args?: Record<string, unknown>;
  step?: string;
  mode?: AutonomyMode | null;
  cadence?: Cadence | null;
  id?: number;
  enabled?: boolean;
  href?: string;
}

export interface AssistantReply {
  reply: string;
  proposed_actions: AssistantAction[];
}

export interface AssistantMessage {
  role: string; // "user" | "assistant"
  content: string;
}

export interface AssistantFeedbackResult {
  ok: boolean;
  learning_id: number;
  verdict: "approve" | "reject" | "refine";
  about: string | null;
  note: string;
  store: string | null;
}

// Reachability of a local dependency (e.g. shopping_scan / dataforseo) an auto-local job drives.
export interface Preflight {
  dep: string;
  reachable: boolean;
  base?: string;
  detail: string;
}

// A 4xx with the backend's `detail` surfaced (so confirm dialogs can show the gate
// message — e.g. a promote rejected because the candidate hasn't cleared the gate).
export class ApiError extends Error {
  status: number;
  constructor(status: number, detail: string) {
    super(detail);
    this.status = status;
    this.name = "ApiError";
  }
}

// ---- Bearer token ----
// Web and api are separate origins, so the session token rides in the Authorization
// header (not a cross-site cookie). Kept in localStorage + mirrored in memory so it
// survives reloads and every request carries it automatically.
const TOKEN_KEY = "operator_auth_token";
let _token: string | null = null;

export function getToken(): string | null {
  if (_token) return _token;
  if (typeof window !== "undefined") _token = window.localStorage.getItem(TOKEN_KEY);
  return _token;
}
export function setToken(token: string | null) {
  _token = token;
  if (typeof window === "undefined") return;
  if (token) window.localStorage.setItem(TOKEN_KEY, token);
  else window.localStorage.removeItem(TOKEN_KEY);
}
function authHeaders(base: Record<string, string> = {}): Record<string, string> {
  const t = getToken();
  return t ? { ...base, Authorization: `Bearer ${t}` } : base;
}

// GETs are idempotent, so transient failures retry automatically. This is what keeps the UI
// alive through an api deploy/restart: Railway swaps containers in ~10-45s, during which a
// fetch either throws "Failed to fetch" (connection refused) or the edge answers 502/503/504.
// Backoff spans ~11s of retries — enough to ride out most restart windows without the user
// ever seeing an error card. Writes (POST/PUT/DELETE) are NOT retried (no double-writes).
const _RETRY_DELAYS_MS = [600, 1500, 3000, 6000];
const _TRANSIENT_STATUS = new Set([502, 503, 504]);

async function get<T>(path: string): Promise<T> {
  let lastErr: unknown;
  for (let attempt = 0; attempt <= _RETRY_DELAYS_MS.length; attempt++) {
    if (attempt > 0) await new Promise((r) => setTimeout(r, _RETRY_DELAYS_MS[attempt - 1]));
    try {
      const res = await fetch(`${API_BASE}${path}`, {
        cache: "no-store",
        headers: authHeaders(),
      });
      if (_TRANSIENT_STATUS.has(res.status)) {
        lastErr = new Error(`${path} → ${res.status}`);
        continue; // backend restarting behind the edge — retry
      }
      if (!res.ok) throw new Error(`${path} → ${res.status}`);
      return (await res.json()) as T;
    } catch (e) {
      if (e instanceof TypeError) {
        lastErr = e; // network-level failure (restart/offline) — retry
        continue;
      }
      throw e; // real HTTP error — surface immediately
    }
  }
  throw lastErr instanceof Error ? lastErr : new Error(`${path} → unreachable`);
}

async function send<T>(method: string, path: string, body?: unknown): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`, {
    method,
    headers: authHeaders({ "Content-Type": "application/json" }),
    body: body === undefined ? undefined : JSON.stringify(body),
    cache: "no-store",
  });
  if (!res.ok) {
    let detail = `${path} → ${res.status}`;
    try {
      const j = await res.json();
      if (j?.detail) detail = typeof j.detail === "string" ? j.detail : JSON.stringify(j.detail);
    } catch {
      /* non-JSON error body */
    }
    throw new ApiError(res.status, detail);
  }
  return res.json() as Promise<T>;
}

const post = <T>(path: string, body?: unknown) => send<T>("POST", path, body);
const put = <T>(path: string, body?: unknown) => send<T>("PUT", path, body);
const del = <T>(path: string) => send<T>("DELETE", path);

// ---- Connections (per-store Shopify creds + global API keys) ----
// Secrets are never returned raw: `masked` is a ••••last4 preview, `configured` says
// whether a value is set. Writes send only the fields being changed.
export interface ConnApiField {
  env: string;
  label: string;
  group: string;
  secret: boolean;
  configured: boolean;
  masked: string;
  placeholder?: string;
  multiline?: boolean;
}
export interface ConnShopifyField {
  key: string;
  label: string;
  group: string;
  secret: boolean;
  placeholder: string;
  configured: boolean;
  masked: string;
}
export interface StoreProfileLanguage {
  locale: string | null;
  name: string | null;
  primary: boolean;
  published: boolean;
}
export interface StoreProfileMarket {
  name: string | null;
  enabled: boolean;
  primary: boolean;
  currency: string | null;
}
// Store-level facts pulled from Shopify (Settings → Connections → Pull from Shopify).
// Single-source: flows downstream (VisionScan language, pricing currency, scheduling timezone).
export interface StoreProfile {
  shop_name?: string | null;
  myshopify_domain?: string | null;
  primary_domain?: string | null;
  currency?: string | null;
  timezone?: string | null;
  country?: string | null;
  language?: string | null;
  primary_locale?: string | null;
  languages?: StoreProfileLanguage[];
  markets?: StoreProfileMarket[];
  catalog_count?: number | null;
}
export interface ConnShopifyStore {
  store: string;
  connected: boolean;
  ads_ready: boolean;
  // Catalog path this store runs: general (default), fashion (apparel end-to-end), or both.
  mode?: "general" | "fashion" | "both";
  // Last automatic data-sync verdict (worker daily / on-connect / manual refresh).
  last_sync?: { at: string; status: "done" | "failed" | "skipped"; detail: string | null } | null;
  fields: ConnShopifyField[];
  profile?: StoreProfile | null;
}
export interface StoreSyncResult {
  ok: boolean;
  skipped?: boolean;
  reason?: string;
  parts: Record<string, { ok: boolean; error?: string }>;
  failures: string[];
}
export interface StoreProfilePullResult {
  ok: boolean;
  profile?: StoreProfile;
  error?: string;
  warning?: string;
}
export interface StoreVerifyResult {
  ok: boolean; // authenticated AND every required scope granted
  connected: boolean; // authenticated (token minted / resolved)
  shop_domain: string;
  granted: string[];
  missing: string[]; // required scopes the token is missing
  extra: string[]; // granted scopes the app doesn't need
  auth_error: string | null;
}
export interface BrightDataStatus {
  has_token: boolean;
  serp_zone: string | null;
  serp_password_set: boolean;
  customer_id_set: boolean;
  cn_proxy_set: boolean;
  zones: string[];
}
export interface BrightDataProvision {
  ok: boolean;
  error?: string;
  created?: string[];
  zones?: Record<string, string>;
  resolved?: Record<string, string | null>;
  needs?: string[];
}
export interface MarkifactStatus {
  connected: boolean;
  client_registered: boolean;
  scope: string | null;
  expires_at: number | null;
  expires_in: number | null;
  redirect_uri: string | null;
  endpoint: string;
  last_error: string | null;
}
export interface GoogleStatus {
  connected: boolean;
  client_ready: boolean;
  developer_token_set: boolean;
  scopes: string[] | null;
  expires_at: number | null;
  expires_in: number | null;
  redirect_uri: string | null;
  connected_at: number | null;
  last_error: string | null;
}
export interface GoogleAccount {
  id: string;
  label: string;
  aggregator?: boolean;
}
export interface GoogleAccounts {
  ads_accounts: GoogleAccount[];
  ads_error: string | null;
  merchant_accounts: GoogleAccount[];
  merchant_error: string | null;
}
// A connected Merchant Center account in the multi-account registry (render-safe: no secrets).
export interface GmcAccount {
  id: string;
  name: string;
  merchantId: string;
  authType: "oauth" | "service_account";
}
export interface GmcAccountTest {
  ok: boolean;
  error?: string;
  account?: GmcAccount;
  services?: number;
}
// Trustpilot reputation (OpenWeb Ninja) — per-store score out of 5 + review count.
export interface TrustpilotStore {
  store: string;
  name: string;
  domain: string | null;
  score: number | null;
  reviews: number | null;
  url: string | null;
  updated_ts: number | null;
  stale: boolean;
}
export interface TrustpilotOverview {
  ok: boolean;
  key_configured: boolean;
  updated_at: number | null;
  stores: TrustpilotStore[];
}
export interface TrustpilotScanResult {
  ok: boolean;
  error?: string;
  cached?: boolean;
  domain?: string;
  entry?: { domain: string; name: string; score: number | null; reviews: number; url: string; ts: number };
}
export interface TrustpilotRefreshRow {
  store: string;
  domain?: string;
  ok: boolean;
  error?: string | null;
  score?: number | null;
}
export interface ConnIntegration {
  id: string;
  name: string;
  kind: string;
  endpoint: string;
  auth: string;
  purpose: string;
  status: string;
  connected?: boolean;
  detail?: MarkifactStatus | GoogleStatus;
}
export interface McpTool {
  name: string;
  description?: string;
  inputSchema?: unknown;
}
export interface ConnectionsView {
  api: ConnApiField[];
  shopify: ConnShopifyStore[];
  integrations: ConnIntegration[];
}
export interface ConnectionsUpdate {
  api?: Record<string, string>;
  shopify?: Record<string, Record<string, string>>;
}
export interface ConnectorCheck {
  key: string;
  name: string;
  ok: boolean;
  status: "ok" | "out_of_credit" | "auth_failed" | "no_creds" | "error";
  message: string;
  fix: string;
  balance?: number;
}
export interface ConnectorHealth {
  ok: boolean;
  checks: ConnectorCheck[];
  problems: ConnectorCheck[];
}


// ---- Market price-push types ----
export interface MpMarket {
  id: string; name: string; primary: boolean; enabled: boolean;
  currency: string | null; countries: string[]; catalog_id: string | null;
}
export interface MpMarketRow {
  is_primary: boolean; catalog_id: string | null; market_id: string | null;
  market_name: string | null; country_code: string | null; price_list_id: string | null;
  published: boolean; price_source: string; price_local: number | null; currency: string;
  price_eur: number | null; fx_rate: number | null; cost: number | null; cost_source: string;
  margin_eur: number | null; margin_pct: number | null;
  pushed: { at: string; local: number | null; eur: number | null; fx_rate: number | null;
            fx_date: string | null; compare_local: number | null; cost_eur: number | null } | null;
}
export interface MpVariantMarkets {
  ok: boolean; store: string; variant_id: string; sku: string | null; title: string | null;
  markets: MpMarketRow[];
}
export interface MpPushResult {
  ok: boolean; scope: string; variant_id: string; price_list_id: string | null; currency: string;
  sale_local: number; sale_eur: number; compare_local: number | null; fx_rate: number; fx_date: string;
}
export interface MpBulkVariant {
  variantId: string; sku?: string | null; title?: string | null; cost?: number | null; price?: number | null;
}
export interface MpJob {
  job_id: string; store_key: string; status: string; total: number; current_index: number;
  current_label: string | null; error: string | null; cancel_requested: boolean;
  started_at: string | null; finished_at: string | null; created_at: string;
  counts: { ok?: number; fail?: number; skipped?: number; auto_bumped?: number; missing?: number;
            errors?: { label: string; msg: string }[]; pushed_variant_ids?: string[] };
}
export interface MpSummaryRow {
  store: string; per_market_pushes: number; distinct_variants: number;
  latest_push_at: string | null; daily: { day: string; pushes: number }[];
}
export interface MpOverMarginRow {
  variant_id: string; market_name: string | null; price_list_id: string | null;
  currency: string; price_local: number; price_eur: number; cost_eur: number;
  margin_pct: number; last_pushed_at: string | null;
  pushed_cost_eur?: number; cost_drift_pct?: number;
}
export interface MpOverMargin {
  ok: boolean; store: string; low: number; high: number; drift: number;
  scanned: number; priced: number;
  counts: { too_low: number; too_high: number; drifted: number };
  too_low: MpOverMarginRow[]; too_high: MpOverMarginRow[]; drifted: MpOverMarginRow[];
}

export const api = {
  overview: () => get<Overview>("/api/overview"),
  shell: () => get<ShellOverview>("/api/shell"),
  feedReport: (store: string) => get<FeedReport>(`/api/feed/${store}`),
  store: (store: string) => get<StoreDetail>(`/api/stores/${store}`),
  storeCategory: (store: string, slug: string) =>
    get<CategoryDetail>(`/api/stores/${store}/categories/${slug}`),
  imageQa: (store: string, slug: string) =>
    get<ImageQaState>(`/api/stores/${store}/categories/${slug}/image-qa`),
  imageQaScan: (store: string, slug: string) =>
    post<ImageQaReport>(`/api/stores/${store}/categories/${slug}/image-qa/scan`),
  imageQaApply: (store: string, slug: string, verdicts: ImageQaRow[], decisionId?: number | null) =>
    post<ImageQaReport>(`/api/stores/${store}/categories/${slug}/image-qa/apply`, {
      verdicts,
      ...(decisionId != null ? { decision_id: decisionId } : {}),
    }),
  storeFileUrl: (store: string, path: string) =>
    `${API_BASE}/api/stores/${store}/file?path=${encodeURIComponent(path)}`,
  storeImageUrl: (store: string, path: string) =>
    `${API_BASE}/api/stores/${store}/image?path=${encodeURIComponent(path)}`,
  dossiers: () => get<{ dossiers: Dossier[] }>("/api/dossiers"),
  dossier: (slug: string) => get<DossierDetail>(`/api/dossiers/${slug}`),
  dossierFileUrl: (slug: string, path: string) =>
    `${API_BASE}/api/dossiers/${slug}/file?path=${encodeURIComponent(path)}`,
  dossierImageUrl: (slug: string, path: string) =>
    `${API_BASE}/api/dossiers/${slug}/image?path=${encodeURIComponent(path)}`,
  keywordDiscovery: () => get<KeywordDiscovery>("/api/keyword-discovery"),
  skuPlan: (overrides?: {
    anchorPct?: number;
    coveragePct?: number;
    productsPerBuild?: number;
    dedupCap?: number;
    google?: number;
    marketplace?: number;
    amazon?: number;
    meta?: number;
  }) => {
    const q = new URLSearchParams();
    if (overrides?.anchorPct != null) q.set("anchor_pct", String(overrides.anchorPct));
    if (overrides?.coveragePct != null) q.set("coverage_pct", String(overrides.coveragePct));
    if (overrides?.productsPerBuild != null) q.set("products_per_build", String(overrides.productsPerBuild));
    if (overrides?.dedupCap != null) q.set("dedup_cap", String(overrides.dedupCap));
    if (overrides?.google != null) q.set("google", String(overrides.google));
    if (overrides?.marketplace != null) q.set("marketplace", String(overrides.marketplace));
    if (overrides?.amazon != null) q.set("amazon", String(overrides.amazon));
    if (overrides?.meta != null) q.set("meta", String(overrides.meta));
    const qs = q.toString();
    return get<SkuPlan>(`/api/sku-plan${qs ? `?${qs}` : ""}`);
  },
  skuPlanPhotoDedup: (store: string, keyword: string) =>
    post<PhotoDedupResult>("/api/sku-plan/photo-dedup", { store, keyword }),
  skuPlanResearch: (store: string, keyword: string) =>
    post<SkuPlanResearchResult>("/api/sku-plan/research", { store, keyword }),
  // Remove ONE sub-keyword from a head keyword's plan (the X on a sub-keyword row). Persisted
  // server-side so it drops from the SKU plan AND the keyword page and stays gone across re-scans.
  dismissSkuSegment: (store: string, keyword: string, term: string) =>
    post<{ ok: boolean; dismissed?: boolean; total?: number }>(
      "/api/sku-plan/segment/dismiss",
      { store, keyword, term },
    ),
  // Remove ONE found product (the X on a product card) — e.g. a big-box/brand result that slipped
  // through. `ident` = its title (or url). Persisted so it stays hidden across re-scans.
  dismissFoundProduct: (store: string, keyword: string, ident: string) =>
    post<{ ok: boolean; dismissed?: boolean; total?: number }>(
      "/api/sku-plan/found/dismiss",
      { store, keyword, ident },
    ),
  skuPlanFound: (store: string, keyword: string) =>
    get<SkuPlanFound>(
      `/api/sku-plan/found?store=${encodeURIComponent(store)}&keyword=${encodeURIComponent(keyword)}`,
    ),
  skuPlanFoundValidated: (store: string, keyword: string) =>
    post<FoundValidatedResult>("/api/sku-plan/found-validated", { store, keyword }),
  authStatus: () => get<AuthStatus>("/api/auth/status"),
  authLogin: (name: string, password: string) =>
    post<AuthResult>("/api/auth/login", { name, password }),
  // One-login pass-through: exchange a signed token from the NN shell for a bearer.
  authSso: (token: string) => post<AuthResult>("/api/auth/sso", { token }),
  authLogout: () => post<{ ok: boolean }>("/api/auth/logout"),
  authMe: () => get<{ user: AuthUser }>("/api/auth/me"),
  access: () => get<Access>("/api/access"),
  users: () => get<UsersPayload>("/api/users"),
  userCreate: (input: PersonInput) => post<Person>("/api/users", input),
  userUpdate: (id: string, input: Partial<PersonInput>) =>
    put<Person>(`/api/users/${id}`, input),
  userDelete: (id: string) => del<{ ok: boolean }>(`/api/users/${id}`),
  userRevealPassword: (id: string) =>
    get<{ password: string | null }>(`/api/users/${id}/password`),
  userSetActive: (id: string) => post<Access>("/api/users/active", { id }),
  skuPlanSettings: () => get<SkuPlanSettingsState>("/api/settings/sku-plan"),
  skuPlanSettingsSave: (settings: Partial<SkuPlanSettings>) =>
    put<SkuPlanSettingsState>("/api/settings/sku-plan", settings),
  skuPlanSettingsReset: () =>
    put<SkuPlanSettingsState>("/api/settings/sku-plan", { reset: true }),
  skuPlanSourceSupply: () =>
    get<{ supply: SourceSupplyMap }>("/api/sku-plan/source-supply"),
  skuPlanSetSourceSupply: (source: string, state: SupplyState) =>
    post<{ supply: SourceSupplyMap }>("/api/sku-plan/source-supply", { source, state }),
  spyRoster: () => get<SpyRoster>("/api/product-research/spy"),
  spyRemoveStore: (domain: string) =>
    post<{ ok: boolean; removed: boolean; domain?: string; remaining: number }>(
      "/api/product-research/spy/remove",
      { domain },
    ),
  spyCandidates: () => get<SpyCandidates>("/api/product-research/spy/candidates"),
  spyAdmitStores: (domains: string[]) =>
    post<{
      ok: boolean;
      admitted: string[];
      n_admitted: number;
      skipped: { domain: string; reason: string; google_ads_count: number | null }[];
      n_skipped: number;
      tracked: number;
    }>("/api/product-research/spy/admit", { domains }),
  // Manual add-by-URL: gate the pasted domain(s) on live Google Shopping ads + append to the
  // roster. Same endpoint/shape as spyAdmitStores — the intent-named helper for the URL box.
  spyAdmit: (domains: string[]) =>
    post<{
      ok: boolean;
      admitted: string[];
      n_admitted: number;
      skipped: { domain: string; reason: string; google_ads_count: number | null }[];
      n_skipped: number;
      tracked: number;
    }>("/api/product-research/spy/admit", { domains }),
  competitorScan: (domain: string, keyword: string, geo = "US", pages = 8) => {
    const q = new URLSearchParams({ domain, keyword, geo, pages: String(pages) });
    return get<CompetitorScan>(`/api/product-research/competitor-scan?${q.toString()}`);
  },
  bestsellerMovers: () => get<BestsellerMovers>("/api/product-research/movers"),
  bestsellers: (store?: string) =>
    get<Bestsellers>(
      `/api/product-research/bestsellers${store ? `?store=${encodeURIComponent(store)}` : ""}`,
    ),
  newProducts: (onlyNew = true) =>
    get<NewProducts>(`/api/product-research/new-products?only_new=${onlyNew}`),
  amazonMovers: () => get<AmazonMovers>("/api/product-research/amazon-movers"),
  marketplaceMovers: () => get<MarketplaceMovers>("/api/product-research/marketplace-movers"),
  marketplaceBrowse: () => get<MarketplaceBrowse>("/api/marketplace-browse"),
  amazonBrowse: () => get<AmazonBrowse>("/api/amazon-browse"),
  findProducts: (keyword: string, opts?: { sv?: number | null; source?: string }) => {
    const q = new URLSearchParams({ keyword });
    if (opts?.sv != null) q.set("sv", String(opts.sv));
    if (opts?.source) q.set("source", opts.source);
    return get<FindProducts>(`/api/find-products?${q.toString()}`);
  },
  pricingRules: () => get<PricingRules>("/api/pricing-rules"),
  pricingSettings: () => get<PricingSettingsState>("/api/settings/pricing"),
  pricingSettingsSave: (settings: Partial<PricingEditable>) =>
    put<PricingSettingsState>("/api/settings/pricing", settings),
  pricingSettingsReset: () =>
    put<PricingSettingsState>("/api/settings/pricing", { reset: true }),
  priceSuggest: (opts?: {
    cogs?: number | null;
    competitor?: number | null;
    undercut?: boolean;
    compareAtPct?: number | null;
  }) => {
    const q = new URLSearchParams();
    if (opts?.cogs != null) q.set("cogs", String(opts.cogs));
    if (opts?.competitor != null) q.set("competitor", String(opts.competitor));
    if (opts?.undercut === false) q.set("undercut", "false");
    if (opts?.compareAtPct != null) q.set("compare_at_pct", String(opts.compareAtPct));
    return get<PriceSuggest>(`/api/price-suggest?${q.toString()}`);
  },
  compareAtTier: () => get<CompareAtTier>("/api/compare-at-tier"),
  metaDropship: () => get<MetaDropship>("/api/product-research/meta-dropship"),
  painFirst: () => get<{ niches: PainNiche[] }>("/api/pain-first"),
  painDetail: (slug: string) => get<PainDetail>(`/api/pain-first/${slug}`),
  painFileUrl: (slug: string, path: string) =>
    `${API_BASE}/api/pain-first/${slug}/file?path=${encodeURIComponent(path)}`,
  listingMethods: () =>
    get<{ listing_methods: ListingMethod[] }>("/api/listing-methods"),
  researchMethods: (surface: ResearchSurface) =>
    get<{ surface: ResearchSurface; research_methods: ResearchMethod[] }>(
      `/api/research-methods/${surface}`,
    ),
  trends: () => get<TrendsOverview>("/api/trends"),
  dismissTrend: (slug: string, keyword: string, geo: string | null | undefined) =>
    post<{ dismissed: boolean; total: number; overview: TrendsOverview }>(
      "/api/trends/dismiss",
      { slug, keyword, geo: geo ?? null },
    ),
  restoreTrends: () =>
    post<{ restored: boolean; total: number; overview: TrendsOverview }>("/api/trends/restore"),
  news: () => get<NewsOverview>("/api/news"),
  newsSync: (geo = "ALL", timespan = "28d") =>
    post<NewsSyncResult>(
      `/api/news/sync?geo=${encodeURIComponent(geo)}&timespan=${encodeURIComponent(timespan)}`,
    ),
  events: (country = "ALL", withinDays = 180) =>
    get<EventsOverview>(
      `/api/events?country=${encodeURIComponent(country)}&within_days=${withinDays}`,
    ),
  optimization: (store: string) =>
    get<OptimizationSnapshot>(`/api/optimization/${store}`),
  optimizationSync: (store: string) =>
    post<OptimizationSyncResult>(`/api/optimization/${store}/sync`),
  // Bulk product-status change from the Product Performance table (real Shopify write).
  optimizationSetStatus: (
    store: string,
    productIds: string[],
    status: "ACTIVE" | "DRAFT" | "ARCHIVED",
    meta?: { prev?: Record<string, string>; titles?: Record<string, string> },
  ) =>
    post<{ ok: boolean; updated: number; failed: { id: string; error: string }[] }>(
      `/api/optimization/${store}/products/status`,
      { product_ids: productIds, status, prev: meta?.prev, titles: meta?.titles },
    ),
  // Server-persist a product's Exclude / Note flag (only the fields sent change).
  optimizationFlag: (store: string, productId: string, flag: { hidden?: boolean; note?: string; product_title?: string }) =>
    post<{ ok: boolean; product_id: string; hidden: boolean; note: string | null }>(
      `/api/optimization/${store}/flag`,
      { product_id: productId, ...flag },
    ),
  // Set a product's app-side tag list (Pythago per-row Add/Remove tag).
  optimizationSetTags: (store: string, productId: string, tags: string[], productTitle?: string) =>
    post<{ ok: boolean; product_id: string; tags: string[] }>(
      `/api/optimization/${store}/tags`,
      { product_id: productId, tags, product_title: productTitle },
    ),
  // Per-product mutation history (status / exclude / note / tag changes), newest first.
  optimizationHistory: (store: string, productId: string) =>
    get<{ ok: boolean; entries: OptHistoryEntry[] }>(
      `/api/optimization/${store}/history?product_id=${encodeURIComponent(productId)}`,
    ),
  optimizationRevert: (store: string, id: number) =>
    post<{ ok: boolean; reverted_field?: string; error?: string }>(
      `/api/optimization/${store}/history/revert`,
      { id },
    ),
  // Server-side saved filter presets.
  optimizationSavedFilters: (store: string) =>
    get<{ ok: boolean; filters: OptSavedFilter[] }>(`/api/optimization/${store}/saved-filters`),
  optimizationSaveFilter: (store: string, name: string, state: unknown) =>
    post<{ ok: boolean; name?: string; error?: string }>(`/api/optimization/${store}/saved-filters`, { name, state }),
  optimizationDeleteFilter: (store: string, name: string) =>
    del<{ ok: boolean }>(`/api/optimization/${store}/saved-filters/${encodeURIComponent(name)}`),
  // ── Automation rules (analysis-first: reports what WOULD trigger) ──
  automationRules: (store: string) =>
    get<{ ok: boolean; automation_enabled: boolean; rules: AutomationRule[] }>(`/api/optimization/${store}/automation/rules`),
  automationSaveRule: (store: string, rule: AutomationRule) =>
    post<{ ok: boolean; id?: number; error?: string }>(`/api/optimization/${store}/automation/rules`, rule),
  automationDeleteRule: (store: string, ruleId: number) =>
    del<{ ok: boolean }>(`/api/optimization/${store}/automation/rules/${ruleId}`),
  automationSetEnabled: (store: string, enabled: boolean) =>
    post<{ ok: boolean; enabled: boolean }>(`/api/optimization/${store}/automation/enabled`, { enabled }),
  automationEvaluate: (store: string) =>
    get<AutomationEval>(`/api/optimization/${store}/automation/evaluate`),
  automationApply: (store: string, m: { product_id: string; action: string; rule_name?: string; product_title?: string; market?: string | null; vals?: unknown }) =>
    post<{ ok: boolean; result: string; detail: string }>(`/api/optimization/${store}/automation/apply`, m),
  automationLog: (store: string) =>
    get<{ ok: boolean; entries: AutomationLogEntry[] }>(`/api/optimization/${store}/automation/log`),
  listingPlan: (p?: {
    window?: PlanWindow;
    per_day?: number;
    store?: string | null;
    weights?: Partial<Record<PlanCategoryId, number>> | null;
    trend_bias?: number;
  }) => {
    const q = new URLSearchParams();
    if (p?.window != null) q.set("window", p.window);
    if (p?.per_day != null) q.set("per_day", String(p.per_day));
    if (p?.store) q.set("store", p.store);
    if (p?.weights) q.set("weights", JSON.stringify(p.weights));
    if (p?.trend_bias != null) q.set("trend_bias", String(p.trend_bias));
    const qs = q.toString();
    return get<ListingPlan>(`/api/listing-plan${qs ? `?${qs}` : ""}`);
  },
  sourcingMatch: (store?: string) =>
    get<SourcingMatch>(
      `/api/sourcing-match${store ? `?store=${encodeURIComponent(store)}` : ""}`,
    ),
  sourcingFeedback: (body: {
    key: string;
    verdict: "good" | "bad";
    correct_offer_id?: string | null;
    note?: string | null;
    ai_verdict?: string | null;
    ai_offer_id?: string | null;
    subject?: string | null;
  }) => post<{ ok: boolean; learning: MatchLearning }>("/api/sourcing-match/feedback", body),
  sourcing1688Enabled: () => get<{ enabled: boolean }>("/api/sourcing-match/enabled"),
  setSourcing1688Enabled: (enabled: boolean) =>
    post<{ enabled: boolean }>("/api/sourcing-match/enabled", { enabled }),
  catalogScan: (store?: string) =>
    get<CatalogScan>(
      `/api/catalog-scan${store ? `?store=${encodeURIComponent(store)}` : ""}`,
    ),

  // Materialize a day of the Daily-Listings calendar into real jobs (hybrid model):
  // each scheduled row becomes a queued/needs-operator job, keyed to the store. Turns the
  // plan from a visualization into a scheduler. Defaults to day 1 (today).
  executeListingPlan: (payload: {
    store: string;
    window?: PlanWindow;
    per_day?: number;
    weights?: Partial<Record<PlanCategoryId, number>> | null;
    trend_bias?: number;
    day?: number;
  }) =>
    post<{
      ok: boolean;
      day: number;
      date: string | null;
      store: string;
      created: JobRecord[];
      skipped: { keyword: string | null; reason: string }[];
      counts: { created: number; skipped: number };
    }>("/api/listing-plan/execute", payload),

  // ---- Gameplans (per-store Daily-Listings settings bundles) ----
  gameplans: (store?: string) =>
    get<{ gameplans: Gameplan[] }>(
      `/api/gameplans${store ? `?store=${encodeURIComponent(store)}` : ""}`,
    ),
  createGameplan: (payload: {
    store: string;
    name: string;
    config: GameplanConfig;
    is_default?: boolean;
  }) => post<{ ok: boolean; gameplan: Gameplan }>("/api/gameplans", payload),
  updateGameplan: (
    id: number,
    payload: { name?: string; config?: GameplanConfig; is_default?: boolean },
  ) => put<{ ok: boolean; gameplan: Gameplan }>(`/api/gameplans/${id}`, payload),
  deleteGameplan: (id: number) => del<{ ok: boolean }>(`/api/gameplans/${id}`),

  // ---- Phase 2: write/control layer ----
  promoteCandidate: (store: string, keyword: string) =>
    post<{ ok: boolean; keyword: string; output: string; store: StoreSummary }>(
      `/api/stores/${store}/candidates/${encodeURIComponent(keyword)}/promote`,
    ),
  // Promote a Trend card's keyword into the pipeline (same meaning as the Keyword table's Promote:
  // it goes down the pipeline from here — SKU plan → product-find). Ingests it as a gated candidate
  // first if it isn't one yet.
  promoteTrend: (store: string, keyword: string) =>
    post<{ ok: boolean; keyword: string; path?: string; store: StoreSummary }>(
      `/api/trends/promote`,
      { store, keyword },
    ),
  removeCandidate: (store: string, keyword: string) =>
    del<{ ok: boolean; removed: number }>(
      `/api/stores/${store}/candidates/${encodeURIComponent(keyword)}`,
    ),
  setSkuState: (
    store: string,
    slug: string,
    sku: string,
    state: SkuState,
    note?: string,
  ) =>
    post<{ ok: boolean; slug: string; sku: string; state: SkuState; category: CategoryDetail }>(
      `/api/stores/${store}/categories/${slug}/skus/${sku}/state`,
      { state, note },
    ),
  addCategory: (
    store: string,
    payload: { slug: string; keyword?: string; sv?: number; capture?: string },
  ) =>
    post<{ ok: boolean; slug: string; store: StoreSummary }>(
      `/api/stores/${store}/categories`,
      payload,
    ),
  // Handoff: picked found-products (AliExpress / Temu / 1688 / Amazon) → the store's listing queue.
  // Adds the keyword as a category + each product as a `candidate` SKU carrying its research ref.
  addFoundProducts: (
    store: string,
    payload: { keyword: string; products: LaneProduct[] },
  ) =>
    post<{
      ok: boolean;
      slug: string;
      count: number;
      added: string[];
      errors: string[];
      store: StoreSummary;
    }>(`/api/stores/${store}/found-products`, payload),
  runs: (limit = 50) =>
    get<{ runs: RunRecord[]; counts: { runs: number; jobs: number } }>(
      `/api/runs?limit=${limit}`,
    ),
  storeRuns: (store: string, limit = 50) =>
    get<{ runs: RunRecord[] }>(`/api/stores/${store}/runs?limit=${limit}`),

  // ---- Phase 3: execution jobs ----
  jobSpecs: () => get<{ job_specs: JobSpec[] }>("/api/job-specs"),
  createJob: (spec: string, store: string, args?: Record<string, unknown>) =>
    post<JobRecord>("/api/jobs", { spec, store, args }),
  createBulkJobs: (spec: string, store: string, links: string[], argKey = "url") =>
    post<{ count: number; jobs: JobRecord[] }>("/api/jobs/bulk", {
      spec,
      store,
      links,
      arg_key: argKey,
    }),
  jobs: (limit = 50, store?: string) =>
    get<{ jobs: JobRecord[] }>(
      `/api/jobs?limit=${limit}${store ? `&store=${store}` : ""}`,
    ),
  job: (id: number) => get<JobRecord>(`/api/jobs/${id}`),

  // ---- Phase B: worker / hybrid control ----
  worker: () => get<WorkerStatus>("/api/worker"),
  setWorkerEnabled: (enabled: boolean) =>
    post<{ ok: boolean; worker: WorkerState }>("/api/worker/enable", { enabled }),
  workerTick: (store: string, steps?: string[], force?: boolean) =>
    post<{
      store: string;
      ran: unknown[];
      suggested: unknown[];
      skipped: unknown[];
      counts: { ran: number; suggested: number; skipped: number };
    }>("/api/worker/tick", { store, steps, force }),
  autonomy: () => get<{ steps: AutonomyStep[] }>("/api/autonomy"),
  setAutonomy: (step: string, mode?: AutonomyMode, cadence?: Cadence) =>
    put<{ ok: boolean; step: AutonomyStep }>(`/api/autonomy/${step}`, { mode, cadence }),
  decisions: (status: "pending" | "approved" | "rejected" | "all" = "pending", store?: string) =>
    get<{ decisions: Decision[] }>(
      `/api/decisions?status=${status}${store ? `&store=${store}` : ""}`,
    ),
  approveDecision: (id: number) =>
    post<{ ok: boolean; decision: Decision; job: JobRecord | null }>(
      `/api/decisions/${id}/approve`,
    ),
  rejectDecision: (id: number, reason?: string, action?: string) =>
    post<{ ok: boolean; decision: Decision; job: JobRecord | null; learning: Learning | null }>(
      `/api/decisions/${id}/reject`,
      { reason, action },
    ),
  learnings: (kind?: string, store?: string, limit = 50) =>
    get<{ learnings: Learning[]; count: number }>(
      `/api/learnings?limit=${limit}${kind ? `&kind=${kind}` : ""}${store ? `&store=${store}` : ""}`,
    ),

  // ---- Settings (backend system view) ----
  settings: () => get<Settings>("/api/settings"),
  setTenant: (tenant: string) =>
    put<{ tenant: string }>("/api/settings/tenant", { tenant }),
  // ---- Global data sync (all stores at once) ----
  storesSyncAll: () =>
    post<{ ok: boolean; started: boolean; running: boolean; stores: string[] }>("/api/stores/sync-all", {}),
  storesSyncAllStatus: () =>
    get<{ ok: boolean; running: boolean; stores: { store: string; status: string; at: string | null; detail: string | null }[] }>(
      "/api/stores/sync-all/status",
    ),
  // ---- Proactive performance alerts (cross-store digest) ----
  alerts: () => get<AlertsDigest>("/api/alerts"),
  // Single-store "fix these first" digest — powers the Alerts tab in Product Optimization.
  optimizationAlerts: (store: string) => get<AlertsDigest>(`/api/optimization/${store}/alerts`),
  // ---- Shopify webhooks (real-time order ingestion) ----
  storesWebhooksRegisterAll: () =>
    post<{ ok: boolean; stores: { store: string; ok: boolean; created?: string[]; existing?: string[]; error?: string }[] }>(
      "/api/stores/webhooks/register-all",
      {},
    ),
  storeWebhooks: (store: string) =>
    get<{ ok: boolean; callback?: string; error?: string; subscriptions: { id: string; topic: string; callback: string | null; ours: boolean }[] }>(
      `/api/stores/${store}/webhooks`,
    ),
  storeWebhooksRegister: (store: string) =>
    post<{ ok: boolean; created?: string[]; existing?: string[]; error?: string }>(`/api/stores/${store}/webhooks/register`, {}),
  storeWebhooksUnregister: (store: string) =>
    del<{ ok: boolean; removed?: number }>(`/api/stores/${store}/webhooks`),

  // ---- Connections (Shopify creds + API keys; masked on read) ----
  connections: () => get<ConnectionsView>("/api/connections"),
  connectionsHealth: () => get<ConnectorHealth>("/api/connections/health"),
  connectionsSave: (payload: ConnectionsUpdate) =>
    put<ConnectionsView>("/api/connections", payload),
  shopifyProfilePull: (store: string) =>
    post<StoreProfilePullResult>(`/api/stores/${store}/shopify/profile/pull`),
  // The Admin API scopes every part of the app needs (backend single source of truth).
  shopifyScopes: () => get<{ scopes: string[] }>("/api/shopify/scopes"),
  // Verify a store's Shopify connection end-to-end (auth + all required scopes). Read-only.
  verifyStore: (store: string) =>
    post<StoreVerifyResult>(`/api/stores/${store}/verify`),

  // ---- Bright Data auto-setup (one token provisions the zones we need) ----
  brightdataStatus: () => get<BrightDataStatus>("/api/integrations/brightdata"),
  brightdataProvision: () =>
    post<BrightDataProvision>("/api/integrations/brightdata/provision"),

  // ---- Markifact (Google/Meta Ads write layer over OAuth-MCP) ----
  markifactStatus: () => get<MarkifactStatus>("/api/integrations/markifact"),
  markifactConnect: () =>
    post<{ authorization_url: string; state: string }>("/api/integrations/markifact/connect"),
  markifactDisconnect: () =>
    post<MarkifactStatus>("/api/integrations/markifact/disconnect"),
  markifactTools: () =>
    get<{ count: number; tools: McpTool[] }>("/api/integrations/markifact/tools"),
  markifactCall: (name: string, args?: Record<string, unknown>) =>
    post<{ ok: boolean; result: unknown }>("/api/integrations/markifact/call", {
      name,
      arguments: args ?? {},
    }),

  // ---- Google (Ads + Merchant Center) over OAuth 2.0 ----
  // The real connection: authorize once, the refresh token authorizes downstream jobs.
  googleStatus: () => get<GoogleStatus>("/api/integrations/google"),
  googleConnect: () =>
    post<{ authorization_url: string; state: string }>("/api/integrations/google/connect"),
  googleDisconnect: () => post<GoogleStatus>("/api/integrations/google/disconnect"),
  googleAccounts: () => get<GoogleAccounts>("/api/integrations/google/accounts"),

  // ---- Merchant Center accounts (multi-account GMC connection registry) ----
  // Each account is connected via its OWN Google Cloud OAuth client + refresh token, keyed by
  // its numeric Merchant ID (multi-brand operators own several GMCs under different logins).
  gmcAccounts: () => get<{ accounts: GmcAccount[] }>("/api/gmc/accounts"),
  gmcAccountTest: (id: string) => get<GmcAccountTest>(`/api/gmc/accounts/${encodeURIComponent(id)}/test`),
  gmcAccountRegister: (id: string, developerEmail: string) =>
    post<{ ok: boolean; result?: unknown }>(`/api/gmc/accounts/${encodeURIComponent(id)}/register`, { developerEmail }),
  gmcAccountRemove: (id: string) => del<{ ok: boolean; removed: GmcAccount }>(`/api/gmc/accounts/${encodeURIComponent(id)}`),
  // The connect flow is a browser redirect to Google's consent screen, so it opens as a popup
  // pointed straight at the API (not a fetch). Build the URL with the entered account details.
  gmcOauthStartUrl: (f: { name: string; merchantId: string; clientId: string; clientSecret: string }) =>
    `${API_BASE}/api/gmc/oauth/start?` +
    new URLSearchParams({ name: f.name, merchantId: f.merchantId, clientId: f.clientId, clientSecret: f.clientSecret }).toString(),

  // ---- Trustpilot reputation (OpenWeb Ninja) — store-management overview ----
  trustpilotOverview: () => get<TrustpilotOverview>("/api/trustpilot/overview"),
  trustpilotScan: (domain: string, force = false) =>
    post<TrustpilotScanResult>("/api/trustpilot/scan", { domain, force }),
  trustpilotRefresh: () => post<{ ok: boolean; results: TrustpilotRefreshRow[] }>("/api/trustpilot/refresh"),

  // ---- Register a new Shopify store (scaffolds its listing queue) ----
  addStore: (key: string, mode: "general" | "fashion" | "both" = "general") =>
    post<{ ok: boolean; store: string; stores: string[] }>("/api/stores", { key, mode }),
  // Flip a store between the general and fashion catalog paths (cascades to research,
  // competitor finding and listing via STORE_MODE).
  setStoreMode: (store: string, mode: "general" | "fashion" | "both") =>
    put<{ ok: boolean; store: string; mode: string }>(`/api/stores/${store}/mode`, { mode }),
  // Full store data sync NOW (profile + finance + product orders + issues) — same engine
  // the worker runs daily.
  syncStore: (store: string) => post<StoreSyncResult>(`/api/stores/${store}/sync`),
  // ---- Unregister a store (removes its dir + clears its per-store credentials) ----
  deleteStore: (key: string) =>
    del<{ ok: boolean; store: string; stores: string[] }>(`/api/stores/${key}`),

  // ---- Operation costs (estimates) ----
  costs: (store?: string, listingsPerMonth = 30, stores = 1) =>
    get<CostOverview>(
      `/api/costs?listings_per_month=${listingsPerMonth}&stores=${stores}${store ? `&store=${store}` : ""}`,
    ),
  saveCostAssumptions: (payload: { unit_costs?: Record<string, number>; agent_model?: string }) =>
    put<{ ok: boolean; unit_costs: Record<string, CostUnit>; agent_model: string }>(
      "/api/costs/assumptions",
      payload,
    ),

  // ---- Assistant (read-only copilot) ----
  // POST the full conversation each turn → {reply, proposed_actions}. The backend
  // never runs an action; the page renders each proposed action as a confirm-button.
  assistantChat: (messages: AssistantMessage[], store?: string | null) =>
    post<AssistantReply>("/api/assistant/chat", { messages, store: store ?? undefined }),

  // Record the operator's verdict on an output the assistant produced (after it ran a job):
  // approve / reject / refine + a free-text note. Persisted as a learning the assistant reads
  // back every turn — the context+learning loop.
  assistantFeedback: (fb: {
    verdict: "approve" | "reject" | "refine";
    note?: string;
    spec?: string | null;
    store?: string | null;
    job_id?: number | null;
  }) =>
    post<AssistantFeedbackResult>("/api/assistant/feedback", {
      verdict: fb.verdict,
      note: fb.note,
      spec: fb.spec ?? undefined,
      store: fb.store ?? undefined,
      job_id: fb.job_id ?? undefined,
    }),

  // ---- Preflight (local-dependency reachability) ----
  // Whether a local dep an auto-local job needs (e.g. AdsPower for the Google Shopping
  // scan) is reachable right now. Reachable → the scan runs automatically; not → manual handoff.
  preflight: (dep: string) => get<Preflight>(`/api/preflight/${dep}`),

  // ---- Multimarket (setup + localization) ----
  mmCountryDefaults: () => get<MmCountryDefaults>("/api/multimarket/country-defaults"),
  mmScan: (store: string) => get<MmScan>(`/api/multimarket/${store}/scan`),
  mmAudit: (store: string, countries?: string[]) =>
    get<MmAudit>(
      `/api/multimarket/${store}/audit${countries?.length ? `?countries=${countries.join(",")}` : ""}`,
    ),
  mmLanguages: (store: string) => get<MmLanguages>(`/api/multimarket/${store}/languages`),
  mmLanguageOp: (store: string, op: MmLangOp, locale: string) =>
    post<MmResult>(`/api/multimarket/${store}/languages`, { op, locale }),
  mmSetupMarket: (store: string, body: MmMarketInput) =>
    post<MmSetupResult>(`/api/multimarket/${store}/markets/setup`, body),
  mmPolicies: (store: string) => get<MmPolicies>(`/api/multimarket/${store}/policies`),
  mmPoliciesPreview: (store: string, facts: Record<string, string>, types?: string[]) =>
    post<MmPolicyPreview>(`/api/multimarket/${store}/policies/preview`, { facts, types }),
  mmPolicyApply: (store: string, type: string, body_html: string) =>
    post<MmResult>(`/api/multimarket/${store}/policies/apply`, { type, body_html }),
  mmSyncShipping: (store: string, facts?: Record<string, string>) =>
    post<MmSyncShipping>(`/api/multimarket/${store}/policies/sync-shipping`, facts ? { facts } : {}),
  mmLocalize: (store: string, body: MmLocalizeInput) =>
    post<MmLocalizeResult>(`/api/multimarket/${store}/localize`, body),
  mmLocalizeAll: (store: string, body: MmLocalizeAllInput) =>
    post<MmLocalizeAllResult>(`/api/multimarket/${store}/localize-all`, body),
  mmLocalizeEverything: (store: string, body: MmLocalizeEverythingInput) =>
    post<MmLocalizeEverythingResult>(`/api/multimarket/${store}/localize-everything`, body),
  mmLocalizationCoverage: () =>
    get<MmCoverage>(`/api/multimarket/localization/coverage`),
  mmLocalizeRead: (store: string, handle: string, locale: string, market?: string) =>
    get<MmLocalizeRead>(
      `/api/multimarket/${store}/localize/read?handle=${encodeURIComponent(handle)}&locale=${encodeURIComponent(locale)}${market ? `&market=${encodeURIComponent(market)}` : ""}`,
    ),
  mmGmcOverview: (store: string) => get<MmGmcOverview>(`/api/multimarket/${store}/gmc/overview`),
  mmGmcShipping: (store: string, body: MmGmcShippingInput) =>
    post<MmResult>(`/api/multimarket/${store}/gmc/shipping`, body),
  mmGmcReturns: (store: string, body: MmGmcReturnsInput) =>
    post<MmResult>(`/api/multimarket/${store}/gmc/returns`, body),

  // ---- Finance / P&L ----
  finStoreHealth: (days = 30, tz?: string) => {
    const q = new URLSearchParams({ days: String(days) });
    if (tz) q.set("tz", tz); // reporting timezone — anchors Today/Yesterday (BKK/CET/NY chips)
    return get<FinStoreHealth>(`/api/finance/store-health?${q.toString()}`);
  },
  finPl: (store: string, days = 30) =>
    get<FinPl>(`/api/finance/${store}/pl?days=${days}`),
  // The P&L dashboard view — named range (today / yesterday / last7 / last30 / last90 /
  // thisMonth / lastMonth / custom) with previous-period deltas. `store` may be "TOTAL".
  finPlView: (
    store: string,
    range = "today",
    from?: string,
    to?: string,
    tz?: string,
    rebucket = false,
  ) => {
    const q = new URLSearchParams({ range });
    if (from) q.set("from", from);
    if (to) q.set("to", to);
    if (tz) q.set("tz", tz); // reporting timezone — anchors Today/Yesterday (BKK/CET/NY chips)
    if (rebucket) q.set("rebucket", "true"); // re-group per-order revenue+orders into that tz
    return get<FinPlView>(`/api/finance/${store}/pl-view?${q.toString()}`);
  },
  finSetCogOverride: (
    store: string,
    body: { date: string; cog?: number | null; fees?: number | null; note?: string | null },
  ) => post<FinResult>(`/api/finance/${store}/cog-override`, body),
  finClearCogOverride: (store: string, date: string) =>
    del<FinResult>(`/api/finance/${store}/cog-override/${date}`),
  finSync: (store: string, days = 60) =>
    post<FinResult>(`/api/finance/${store}/sync?days=${days}`),
  // Ad-spend ingest key — surfaced in Settings · Connections (Google Ads), NOT a Finance tab.
  finAdScript: (store: string) =>
    get<FinAdScript>(`/api/finance/${store}/ad-script`),
  finAdScriptRotate: (store: string) =>
    post<{ ok: boolean; script_key: string }>(`/api/finance/${store}/ad-script/rotate`),

  // ---- Company P&L (owner master-sheet) ----
  companyPlMeta: () => get<CompanyPlMeta>(`/api/company-pl/meta`),
  companyPlMatrix: (year: number) =>
    get<CompanyPlMatrix>(`/api/company-pl/matrix?year=${year}`),
  companyPlManual: (year: number) =>
    get<CompanyPlManual>(`/api/company-pl/manual?year=${year}`),
  companyPlManualSet: (year: number, month: number, slug: string, amountEur: number, name?: string) =>
    post<FinResult>(`/api/company-pl/manual`, { year, month, slug, amount_eur: amountEur, name }),
  companyPlManualDelete: (year: number, month: number, slug: string) =>
    del<FinResult>(`/api/company-pl/manual?year=${year}&month=${month}&slug=${encodeURIComponent(slug)}`),

  // ---- Market price-push ----
  mpMarkets: (store: string) =>
    get<{ ok: boolean; markets: MpMarket[] }>(`/api/market-push/${store}/markets`),
  mpSyncMarkets: (store: string) =>
    post<{ ok: boolean; running?: boolean; started?: boolean; catalogs: number | null; prices: number | null; publications: number | null; primary?: string }>(
      `/api/market-push/${store}/sync`, {}),
  mpVariantMarkets: (store: string, variantId: string) =>
    get<MpVariantMarkets>(`/api/market-push/${store}/variant/${variantId}`),
  mpPushPrice: (
    store: string,
    body: { variantId: string; priceEur?: number; priceLocal?: number; priceListId?: string | null;
            catalogId?: string | null; pushCompare?: boolean },
  ) => post<MpPushResult>(`/api/market-push/${store}/price`, body),
  mpBulkPush: (store: string, variants: MpBulkVariant[]) =>
    post<{ ok: boolean; job_id: string; status: string; total: number }>(
      `/api/market-push/${store}/bulk`, { variants }),
  mpJobs: (store: string) => get<{ ok: boolean; jobs: MpJob[] }>(`/api/market-push/${store}/jobs`),
  mpJob: (jobId: string) => get<{ ok: boolean; job: MpJob }>(`/api/market-push/jobs/${jobId}`),
  mpJobCancel: (jobId: string) => post<{ ok: boolean }>(`/api/market-push/jobs/${jobId}/cancel`, {}),
  mpPushLog: (store: string, limit = 100) =>
    get<{ ok: boolean; entries: { ts: string; level: string; msg: string }[] }>(
      `/api/market-push/${store}/log?limit=${limit}`),
  mpPushSummary: (store?: string) =>
    get<{ ok: boolean; since: string; per_store: MpSummaryRow[] }>(
      `/api/market-push/summary${store ? `?store=${store}` : ""}`),
  mpOverMargin: (store: string, low = 67, high = 85, drift = 15) =>
    get<MpOverMargin>(
      `/api/market-push/${store}/over-margin?low=${low}&high=${high}&drift=${drift}`),

  // ---- Product Management ----
  pmOverview: (store: string) =>
    get<PmOverview>(`/api/product-mgmt/${store}/overview`),
  pmCatalog: (store: string) =>
    get<PmCatalog>(`/api/product-mgmt/${store}/catalog`),
  pmInvoiceUpload: (store: string, filename: string, fileBase64: string) =>
    post<PmUploadResult>(`/api/product-mgmt/${store}/invoices/upload`, { filename, fileBase64 }),
  // Which store a supplier-invoice filename auto-routes to (drag-drop of many files → each to its
  // store). Returns { store: null } when nothing matches → caller uses the selected store.
  invoicesResolveStore: (filename: string) =>
    get<{ ok: boolean; store: string | null }>(`/api/invoices/resolve-store?filename=${encodeURIComponent(filename)}`),
  pmInvoices: (store: string) =>
    get<{ ok: boolean; invoices: PmInvoice[] }>(`/api/product-mgmt/${store}/invoices`),
  pmInvoiceDetail: (invoiceId: number) =>
    get<PmInvoiceDetail>(`/api/product-mgmt/invoices/${invoiceId}`),
  pmInvoiceDelete: (invoiceId: number) =>
    del<FinResult>(`/api/product-mgmt/invoices/${invoiceId}`),
  pmBackfill: (store: string) =>
    post<PmBackfillResult>(`/api/product-mgmt/${store}/invoices/backfill-order-dates`, {}),
  // Rebuild the per-variant/SKU + per-market cost tables from THIS store's own invoice lines
  // (native — no standalone dependency). Repopulates pm_product_costs + pm_product_costs_market.
  pmBackfillCosts: (store: string) =>
    post<PmBackfillCostsResult>(`/api/product-mgmt/${store}/backfill-costs`, {}),
  pmCogsSummary: (store: string, from: string, to: string) =>
    get<PmCogsSummary>(
      `/api/product-mgmt/${store}/cogs-summary?date_from=${from}&date_to=${to}`,
    ),
  pmCostOverride: (store: string, variantId: string, country: string, costEur: number | null) =>
    post<FinResult>(`/api/product-mgmt/${store}/cost-override`, { variantId, country, costEur }),
  pmOrdersSync: (store: string, days = 60) =>
    post<FinResult>(`/api/product-mgmt/${store}/orders/sync?days=${days}`),
  pmSyncVariants: (store: string) =>
    post<{ ok: boolean; running?: boolean; started?: boolean; products?: number; variants?: number; ghosted?: number; error?: string }>(
      `/api/product-mgmt/${store}/sync-variants`,
      {},
    ),
  pmMarkMissing: (store: string, variantIds: string[], source = "manual") =>
    post<{ ok: boolean; marked: number; missing_source: string; error?: string }>(
      `/api/product-mgmt/${store}/variants/mark-missing`,
      { variantIds, source },
    ),
  pmUnmarkMissing: (store: string, variantIds: string[]) =>
    post<{ ok: boolean; cleared: number; error?: string }>(
      `/api/product-mgmt/${store}/variants/unmark-missing`,
      { variantIds },
    ),
  pmMarkDuplicates: (store: string) =>
    post<PmDupeResult>(`/api/product-mgmt/${store}/variants/mark-duplicates`, {}),
  pmOrderMargins: (store: string, days = 30, band = "all", q = "", page = 0, pageSize = 100) => {
    const p = new URLSearchParams({ days: String(days), band, page: String(page), page_size: String(pageSize) });
    if (q) p.set("q", q);
    return get<PmOrderMargins>(`/api/product-mgmt/${store}/order-margins?${p.toString()}`);
  },
  pmMarginCheckerF: (store: string, filter = "flagged", q = "", threshold = 67) => {
    const p = new URLSearchParams({ filter, threshold: String(threshold) });
    if (q) p.set("q", q);
    return get<PmMarginChecker>(`/api/product-mgmt/${store}/margin-checker?${p.toString()}`);
  },
  pmMissingCostSource: (store: string, variantId: string) =>
    get<PmMissingCostSource>(
      `/api/product-mgmt/${store}/missing-cost-source?variant_id=${encodeURIComponent(variantId)}`,
    ),
  pmVariantBreakdown: (store: string, productId: string, days = 30) =>
    // The snapshot's product_id is a full gid (gid://shopify/Product/123) — send just the
    // numeric tail so it fits the path param (no slashes) and matches pm_variants_cache.
    get<VariantBreakdown>(
      `/api/product-mgmt/${store}/variant-breakdown/${encodeURIComponent(productId.split("/").pop() || productId)}?days=${days}`,
    ),
  pmOrderAudit: (store: string, days = 60) =>
    get<PmOrderAudit>(`/api/product-mgmt/${store}/order-audit?days=${days}`),
  pmNegotiation: (store: string, days = 90) =>
    get<PmNegotiation>(`/api/product-mgmt/${store}/negotiation?days=${days}`),
  pmMarginReview: (store: string) =>
    get<PmMarginReview>(`/api/product-mgmt/${store}/margin-review`),
  pmProductStatus: (store: string) =>
    get<{ ok: boolean; rows: PmProductStatus[] }>(`/api/product-mgmt/${store}/product-status`),
  pmSetProductStatus: (store: string, productId: string, status: string) =>
    post<FinResult>(`/api/product-mgmt/${store}/product-status`, { productId, status }),

  // ---- Orders & Issues (dispute management + Slack/Pumble escalation) ----
  issuesMeta: () => get<IssuesMeta>("/api/issues/meta"),
  issuesOverview: (store?: string | null) =>
    get<IssuesOverview>(`/api/issues/overview${store ? `?store=${encodeURIComponent(store)}` : ""}`),
  issuesOrdersSync: (store: string, days = 30) =>
    post<{ ok: boolean; orders?: number; items?: number; error?: string }>(
      `/api/issues/${store}/orders/sync?days=${days}`,
    ),
  issuesOrders: (store?: string | null, search?: string, limit = 300) => {
    const q = new URLSearchParams();
    if (store) q.set("store", store);
    if (search) q.set("search", search);
    q.set("limit", String(limit));
    return get<{ ok: boolean; orders: IssueOrder[] }>(`/api/issues/orders?${q.toString()}`);
  },
  issuesOrder: (orderId: string) =>
    get<IssueOrderDetail>(`/api/issues/orders/${encodeURIComponent(orderId)}`),
  issuesDisputes: (opts?: {
    status?: string;
    store?: string | null;
    search?: string;
    assignee?: string;
    source?: string;
  }) => {
    const q = new URLSearchParams();
    q.set("status", opts?.status ?? "open");
    if (opts?.store) q.set("store", opts.store);
    if (opts?.search) q.set("search", opts.search);
    if (opts?.assignee) q.set("assignee", opts.assignee);
    if (opts?.source) q.set("source", opts.source);
    return get<DisputeList>(`/api/issues/disputes?${q.toString()}`);
  },
  issuesAssignees: () => get<{ ok: boolean; assignees: string[] }>("/api/issues/disputes/assignees"),
  issuesFollowups: () => get<IssuesFollowups>("/api/issues/disputes/followups"),
  issuesCreateDispute: (body: DisputeCreateInput) =>
    post<{ ok: boolean; id: number }>("/api/issues/disputes", body),
  issuesDispute: (id: number) => get<DisputeDetail>(`/api/issues/disputes/${id}`),
  issuesPatchDispute: (id: number, body: Record<string, unknown>) =>
    send<DisputeDetail>("PATCH", `/api/issues/disputes/${id}`, body),
  issuesDeleteDispute: (id: number) =>
    del<{ ok: boolean; removed: boolean }>(`/api/issues/disputes/${id}`),
  issuesAddEvent: (id: number, text: string, author?: string) =>
    post<{ ok: boolean }>(`/api/issues/disputes/${id}/events`, { text, author }),
  issuesSetItems: (id: number, items: DisputeItemInput[]) =>
    post<DisputeDetail>(`/api/issues/disputes/${id}/items`, { items }),
  issuesSetContact: (id: number, seq: number, body: DisputeContactInput) =>
    post<DisputeDetail>(`/api/issues/disputes/${id}/contacts/${seq}`, body),
  issuesEscalate: (id: number, note?: string, channel?: string) =>
    post<{ ok: boolean; escalated: boolean; chat: ChatSendResult }>(
      `/api/issues/disputes/${id}/escalate`,
      { note, channel },
    ),
  issuesRefunds: (store?: string | null, disputeId?: number) => {
    const q = new URLSearchParams();
    if (store) q.set("store", store);
    if (disputeId != null) q.set("dispute_id", String(disputeId));
    const qs = q.toString();
    return get<{ ok: boolean; refunds: Refund[]; total_refund: number }>(
      `/api/issues/refunds${qs ? `?${qs}` : ""}`,
    );
  },
  issuesCreateRefund: (body: Record<string, unknown>) =>
    post<{ ok: boolean; id: number }>("/api/issues/refunds", body),
  issuesPatchRefund: (id: number, body: Record<string, unknown>) =>
    send<{ ok: boolean }>("PATCH", `/api/issues/refunds/${id}`, body),
  issuesDeleteRefund: (id: number) =>
    del<{ ok: boolean; removed: boolean }>(`/api/issues/refunds/${id}`),
  issuesTemplates: () => get<{ ok: boolean; templates: IssueTemplate[] }>("/api/issues/templates"),
  issuesCreateTemplate: (body: { title?: string; body?: string; created_by?: string }) =>
    post<{ ok: boolean; id: number }>("/api/issues/templates", body),
  issuesPatchTemplate: (id: number, body: { title?: string; body?: string }) =>
    send<{ ok: boolean }>("PATCH", `/api/issues/templates/${id}`, body),
  issuesDeleteTemplate: (id: number) =>
    del<{ ok: boolean; removed: boolean }>(`/api/issues/templates/${id}`),
  issuesNotes: (owner?: string) =>
    get<{ ok: boolean; notes: IssueNote[] }>(
      `/api/issues/notes${owner ? `?owner=${encodeURIComponent(owner)}` : ""}`,
    ),
  issuesCreateNote: (body: { owner?: string; body?: string; color?: string; pinned?: boolean }) =>
    post<{ ok: boolean; id: number }>("/api/issues/notes", body),
  issuesPatchNote: (id: number, body: { body?: string; color?: string; pinned?: boolean }) =>
    send<{ ok: boolean }>("PATCH", `/api/issues/notes/${id}`, body),
  issuesDeleteNote: (id: number) =>
    del<{ ok: boolean; removed: boolean }>(`/api/issues/notes/${id}`),
  issuesParcels: (store?: string | null, handled?: boolean, limit = 300) => {
    const q = new URLSearchParams();
    if (store) q.set("store", store);
    if (handled != null) q.set("handled", String(handled));
    q.set("limit", String(limit));
    return get<{ ok: boolean; parcels: IssueParcel[] }>(`/api/issues/parcels?${q.toString()}`);
  },
  issuesParcelHandled: (id: number, handled = true) =>
    post<{ ok: boolean }>(`/api/issues/parcels/${id}/handled`, { handled }),
  issuesParcelCreateIssue: (id: number, body?: Record<string, unknown>) =>
    post<{ ok: boolean; id?: number; error?: string }>(
      `/api/issues/parcels/${id}/create-issue`,
      body ?? {},
    ),

  // ---- Tasks & Gameplans ----
  tasksOverview: (store?: string | null) =>
    get<TasksOverview>(`/api/tasks/overview${store ? `?store=${encodeURIComponent(store)}` : ""}`),
  tasksLists: () => get<{ ok: boolean; lists: TaskListRow[] }>("/api/tasks/lists"),
  tasksCreateList: (body: { name: string; color?: string; sort?: number }) =>
    post<{ ok: boolean; id: number; error?: string }>("/api/tasks/lists", body),
  tasksDeleteList: (id: number) =>
    del<{ ok: boolean; removed: boolean }>(`/api/tasks/lists/${id}`),
  tasks: (opts?: { store?: string | null; status?: string; listId?: number; assignee?: string }) => {
    const q = new URLSearchParams();
    if (opts?.store) q.set("store", opts.store);
    if (opts?.status) q.set("status", opts.status);
    if (opts?.listId != null) q.set("list_id", String(opts.listId));
    if (opts?.assignee) q.set("assignee", opts.assignee);
    const qs = q.toString();
    return get<TaskListResult>(`/api/tasks${qs ? `?${qs}` : ""}`);
  },
  tasksCreate: (body: TaskCreateInput) =>
    post<{ ok: boolean; id: number; error?: string }>("/api/tasks", body),
  tasksPatch: (id: number, body: Record<string, unknown>) =>
    send<{ ok: boolean; error?: string }>("PATCH", `/api/tasks/${id}`, body),
  tasksDelete: (id: number) => del<{ ok: boolean; removed: boolean }>(`/api/tasks/${id}`),
  tasksStorePlan: (store: string) =>
    get<{ ok: boolean; store: string; plan: StorePlan | null }>(`/api/tasks/store-plan/${store}`),
  tasksSetStorePlan: (store: string, body: { body?: string; updated_by?: string }) =>
    post<{ ok: boolean; store: string }>(`/api/tasks/store-plan/${store}`, body),

  // ---- Store Management ----
  storeMgmtRoster: () => get<StoreRoster>("/api/store-mgmt/roster"),
  storeMgmtOverview: (store: string) =>
    get<StoreMgmtOverview>(`/api/store-mgmt/${store}/overview`),
  storeMgmtTrust: (store: string) => get<StoreTrustResult>(`/api/store-mgmt/${store}/trust`),
  storeMgmtSetTrust: (store: string, body: Record<string, unknown>) =>
    post<StoreTrustResult>(`/api/store-mgmt/${store}/trust`, body),
};

// ---- Store Management types ----
export type TrustGateState = "pass" | "fail" | "unknown";
export type TrustOverall = "pass" | "review" | "fail";

export interface TrustGate {
  gate: TrustGateState;
  value?: number | null;
  floor?: number;
  reviews?: number | null;
  rating?: number | null;
  min_reviews?: number;
  min_rating?: number;
}
export interface TrustEvaluation {
  overall: TrustOverall;
  ready: boolean;
  label: string;
  gates: {
    scamadviser: TrustGate;
    trustpilot: TrustGate;
    domain_age: TrustGate;
  };
}
export interface StoreTrustData {
  scamadviser_score: number | null;
  trustpilot_reviews: number | null;
  trustpilot_rating: number | null;
  domain_age_days: number | null;
  trustpilot_url: string | null;
  notes: string | null;
}
export interface StoreTrustResult {
  ok: boolean;
  store: string;
  trust: StoreTrustData;
  evaluation: TrustEvaluation;
  thresholds: {
    scamadviser_min: number;
    trustpilot_min_reviews: number;
    trustpilot_min_rating: number;
    domain_age_min_days: number;
  };
}
export interface StoreMgmtOverview {
  ok: boolean;
  store: string;
  catalog: {
    categories: number;
    skus_total: number;
    skus_by_state: Record<string, number>;
    updated: string | null;
  };
  gmc: { connected: boolean; detail: string };
  issues: { open_disputes: number; unhandled_parcels: number; refund_total: number };
  trust: StoreTrustData;
  trust_evaluation: TrustEvaluation;
}
export interface StoreRosterRow {
  store: string;
  categories: number;
  skus_total: number;
  trust: TrustOverall;
  trust_label: string;
}
export interface StoreRoster {
  ok: boolean;
  stores: StoreRosterRow[];
}

// ---- Finance / P&L types ----
export interface FinResult {
  ok: boolean;
  error?: string;
  [k: string]: unknown;
}
export interface FinPlRow {
  date: string;
  grossSales: number;
  discounts: number;
  revenue: number;
  returns: number;
  netRevenue: number;
  orders: number;
  adSpend: number;
  cog: number;
  fees: number;
  profit: number;
  margin: number;
  roas: number | null;
  cogSource: string;
  cogManual?: boolean;
  feesManual?: boolean;
  note?: string | null;
  currency: string;
}
export interface FinAdScript {
  ok: boolean;
  store: string;
  script_key: string;
  endpoint: string;
  usage: string;
}
export interface FinPlTotals {
  grossSales: number;
  discounts: number;
  revenue: number;
  returns: number;
  netRevenue: number;
  adSpend: number;
  cog: number;
  fees: number;
  profit: number;
  orders: number;
  margin: number;
  roas: number | null;
  aov: number;
  adSpendPct: number;
  cogPct: number;
  feePct: number;
  refundPct: number;
  currency: string;
  cogSource: string;
}
export interface FinPl {
  ok: boolean;
  error?: string;
  store?: string;
  days?: number;
  currency?: string;
  cogs_version?: number;
  params?: Record<string, number | string>;
  rows?: FinPlRow[];
  totals?: FinPlTotals;
}
// Per-KPI period-over-period delta. `lowerIsBetter` flips the good/bad coloring (ad spend, fees…).
export interface FinDelta {
  pct: number | null;
  lowerIsBetter: boolean;
}
// The dashboard view — a named range + its rows/totals + previous-period totals + per-KPI deltas.
export interface FinPlView {
  ok: boolean;
  error?: string;
  store?: string;
  range?: string;
  from?: string;
  to?: string;
  prevFrom?: string;
  prevTo?: string;
  currency?: string;
  params?: Record<string, number | string>;
  rows?: FinPlRow[];
  totals?: FinPlTotals;
  prevTotals?: FinPlTotals;
  deltas?: Record<string, FinDelta>;
  // Present only when rebucket=true was requested: which tz was applied + per-order coverage.
  rebucket?: {
    tz: string;
    meta?: {
      totalOrders?: number;
      ordersMissingUtc?: number;
      coveragePct?: number;
      note?: string;
    };
  };
}
// One store-health window (today / yesterday / 7d / 14d / 30d) — the standalone sumRows shape.
export interface FinHealthWindow {
  revenue: number;
  returns: number;
  netRevenue: number;
  adSpend: number;
  cog: number;
  fees: number;
  profit: number;
  orders: number;
  margin: number;
  roas: number | null;
}
export interface FinStoreHealthStore {
  store: string;
  currency: string | null;
  // Legacy flat fields = the 30-day rollup (back-compat).
  revenue: number;
  profit: number;
  margin: number;
  roas: number | null;
  adSpend: number;
  orders: number;
  // Per-window objects (added — TZ-aware). Optional so older cached responses still typecheck.
  today?: FinHealthWindow;
  yesterday?: FinHealthWindow;
  w7?: FinHealthWindow;
  w14?: FinHealthWindow;
  w30?: FinHealthWindow;
}
// Operation-wide rollup for one window.
export interface FinHealthWindowTotals {
  revenue: number;
  profit: number;
  adSpend: number;
  netRevenue: number;
  orders: number;
  margin: number;
  roas: number | null;
}
export interface FinStoreHealth {
  ok: boolean;
  days: number;
  tz?: string | null;
  stores: FinStoreHealthStore[];
  totals: {
    revenue: number;
    profit: number;
    adSpend: number;
    orders: number;
    margin: number;
    roas: number | null;
  };
  windowTotals?: {
    today: FinHealthWindowTotals;
    yesterday: FinHealthWindowTotals;
    w7: FinHealthWindowTotals;
    w14: FinHealthWindowTotals;
    w30: FinHealthWindowTotals;
  };
}

// ---- Company P&L types (owner master-sheet mirror) ----
export interface CompanyPlMeta {
  ok: boolean;
  company: string | null;
  base_currency?: string;
  years: number[];
  default_year: number | null;
  reason?: string;
}
export interface CompanyPlCategory {
  slug: string;
  name: string;
  kind: string;
  sort: number;
  by_month: Record<string, number>;
  total: number;
}
export interface CompanyPlCoverage {
  days_with_data: number;
  days_in_month: number;
  elapsed_days: number;
  is_current: boolean;
  is_partial: boolean;
  is_lagging: boolean;
}
export interface CompanyPlGroup {
  total: number;
  by_month: Record<string, number>;
  slugs: string[];
}
export interface CompanyPlOpexCategory {
  slug: string;
  name: string;
  sort: number;
}
export interface CompanyPlManualEntry {
  year: number;
  month: number;
  slug: string;
  name: string;
  amount_eur: number;
  updated_at: string;
}
export interface CompanyPlManual {
  ok: boolean;
  year: number;
  categories: CompanyPlOpexCategory[];
  entries: CompanyPlManualEntry[];
}
export interface CompanyPlMatrix {
  ok: boolean;
  year: number;
  reason?: string;
  computing?: boolean; // cold cache — numbers building in the background; page auto-refreshes
  stale?: boolean; // serving a cached matrix while a fresh recompute runs
  months: number[];
  coverage_start_month?: number;
  coverage_by_month?: Record<string, CompanyPlCoverage>;
  revenue_by_month?: Record<string, number>;
  revenue_total?: number;
  categories: CompanyPlCategory[];
  cost_groups?: Record<string, CompanyPlGroup>;
  totals_by_month?: Record<string, number>;
  total_cost?: number;
  profit_by_month?: Record<string, number>;
  profit_total?: number;
}

// ---- Product Management types ----
export interface PmOverview {
  ok: boolean;
  store: string;
  invoices: number;
  total_cogs_eur: number;
  lines: number;
  resolved: number;
  unresolved: number;
}
export interface PmCatalogProduct {
  id: string;
  title: string;
  status: string;
  inventory: number | null;
  url: string | null;
  image: string | null;
  variants: number;
  min_price: number | null;
  max_price: number | null;
  currency: string | null;
  issues: string[];
}
export interface PmCatalog {
  ok: boolean;
  error?: string;
  store?: string;
  currency?: string;
  counts?: { total: number; shown: number; active: number; draft: number; with_issues: number };
  truncated?: boolean;
  products?: PmCatalogProduct[];
}
export interface PmInvoice {
  id: number;
  store_key: string;
  filename: string | null;
  supplier: string | null;
  invoice_no: string | null;
  total_eur: number | null;
  currency: string | null;
  uploaded_at: string;
  refund_eur: number;
  line_count: number;
}
export interface PmInvoiceLine {
  id: number;
  order_no: string | null;
  order_date: string | null;
  sku: string | null;
  title: string | null;
  qty: number;
  line_type: string;
  bill_cost_eur: number;
  refund_amount_eur: number;
  country: string | null;
  resolve_status: string | null;
}
export interface PmInvoiceDetail {
  ok: boolean;
  error?: string;
  invoice?: PmInvoice;
  lines?: PmInvoiceLine[];
}
export interface PmBackfillResult {
  ok: boolean;
  running?: boolean; // backgrounded — the resolve runs in a thread; poll the reads
  started?: boolean;
  error?: string;
  store?: string;
  scanned?: number;
  resolved?: number;
  unresolved?: number;
  cross_store_hints?: Record<string, number>;
}
export interface PmBackfillCostsResult {
  ok: boolean;
  error?: string;
  store?: string;
  cost_keys?: number;
  per_market?: number;
  lines_scanned?: number;
}
export interface PmUploadResult {
  ok: boolean;
  error?: string;
  invoice_id?: number;
  supplier?: string | null; // auto-detected from the filename (CJdropshipping / Winwin / HST)
  line_count?: number;
  total_eur?: number;
  currency?: string;
  exchange_rate?: number;
  resolved?: number;
  unresolved?: number;
  // True while the Shopify order-date resolve runs in the BACKGROUND (deferred so a big invoice
  // can't 502 the upload). `unresolved` is then the count of orders queued for date-resolve, and
  // the page polls the invoice list as dates + COGS fill in. cross_store_hints arrive later via
  // "Re-resolve dates".
  resolving?: boolean;
  // Misassignment detector: {other_store_key: hit_count} when most order numbers resolved
  // on a DIFFERENT store — the answer to "I uploaded but nothing changed".
  cross_store_hints?: Record<string, number>;
  // price×qty vs billed-total deviations (>5%) worth an eyeball, per order.
  sanity_warnings?: string[];
  // True when this EXACT workbook (same file bytes / content hash) was already uploaded — the
  // insert was skipped to avoid double-counting COGS; invoice_id points at the existing one.
  duplicate?: boolean;
}
export interface PmCogsSummary {
  ok: boolean;
  order_count: number;
  total_eur: number;
  per_order: {
    order_no: string;
    cogs_eur: number;
    line_count: number;
    order_date: string | null;
    resolve_status: string | null;
  }[];
}
export interface PmOrderMargin {
  order_id: string;
  order_no: string | null;
  order_date: string | null;
  total: number | null;
  currency: string | null;
  financial_status: string | null;
  revenue_eur: number | null;
  cogs_eur: number | null;
  margin: number | null;
  margin_pct: number | null;
  band: string;
}
export interface PmOrderMargins {
  ok: boolean;
  store: string;
  days: number;
  band: string;
  orders: PmOrderMargin[];
  total: number;
  page: number;
  pages: number;
  page_size: number;
  band_counts: Record<string, number>;
  bands: Record<string, number>;
  kpi: { orders: number; with_cogs: number; losing_count: number; losing_amount: number; avg_margin_pct: number | null };
}
export interface PmMarginRow {
  variant_id: string;
  product_id?: string | null;
  sku: string | null;
  title: string | null;
  status?: string | null;
  price: number | null;
  cost: number | null;
  margin_eur: number | null;
  margin_pct: number | null;
  target: number;
  below: boolean;
  no_cost?: boolean;
  missing_cost?: boolean;
  order_count?: number;
  has_orders?: boolean;
  missing_in_shopify?: boolean;
  missing_source?: string | null;
}
export interface PmDupeResult {
  ok: boolean;
  error?: string;
  store?: string;
  duplicate_groups?: number;
  marked?: number;
  kept?: number;
  marked_variant_ids?: string[];
}
export interface PmMarginCounts {
  total: number;
  flagged: number;
  no_cost: number;
  missing_cost: number;
  missing_in_shopify: number;
  in_invoice: number;
}
export interface PmMarginChecker {
  ok: boolean;
  error?: string;
  store: string;
  threshold: number;
  currency?: string;
  filter?: string;
  counts?: PmMarginCounts;
  checked: number;
  below: number;
  ok_count: number;
  avg_margin_pct: number | null;
  rows: PmMarginRow[];
}
export interface PmMissingCostLine {
  line_id: number;
  invoice_id: number;
  order_no: string | null;
  order_date: string | null;
  sku: string | null;
  title: string | null;
  qty: number;
  line_type: string | null;
  bill_cost_eur: number;
  refund_amount_eur: number;
  country: string | null;
  resolve_status: string | null;
  matched_by: "variant_id" | "sku";
  invoice_filename: string | null;
  invoice_supplier: string | null;
  invoice_no: string | null;
  invoice_uploaded_at: string | null;
}
export interface PmMissingCostSource {
  ok: boolean;
  error?: string;
  store: string;
  variant_id: string;
  sku: string | null;
  title: string | null;
  line_count: number;
  lines: PmMissingCostLine[];
}
export interface PmAuditLine {
  order_no: string | null;
  sku: string | null;
  title: string | null;
  bill_cost_eur: number;
  country: string | null;
  resolve_status: string | null;
}
export interface PmAuditOrder {
  order_id: string;
  order_no: string | null;
  order_date: string | null;
  total: number | null;
  currency: string | null;
}
export interface PmAuditDay {
  date: string;
  order_count: number;
  cogs_eur: number;
  refund_eur: number;
  resolved_lines: number;
  unresolved_lines: number;
}
export interface PmOrderAudit {
  ok: boolean;
  error?: string;
  store: string;
  days: number;
  order_count: number;
  daily: PmAuditDay[];
  unresolved_count: number;
  unresolved: PmAuditLine[];
  uncosted_count: number;
  uncosted: PmAuditOrder[];
}
export interface PmNegotiationRow {
  variant_id: string;
  sku: string | null;
  title: string | null;
  units: number;
  revenue: number;
  unit_cost: number | null;
  spend: number | null;
  tier: string;
  discount_pct: number;
  potential_saving: number | null;
}
export interface PmNegotiation {
  ok: boolean;
  error?: string;
  store: string;
  days: number;
  tiers: { tier: string; min: number; max: number | null; discount_pct: number }[];
  count: number;
  total_potential_saving: number;
  rows: PmNegotiationRow[];
}
export interface PmReviewRow {
  sku: string;
  title: string | null;
  latest_cost: number;
  prev_cost: number | null;
  drift_pct: number | null;
  drifted: boolean;
  invoices: number;
  price: number | null;
  margin_pct: number | null;
}
export interface PmMarginReview {
  ok: boolean;
  error?: string;
  store: string;
  drift_pct: number;
  tracked: number;
  drifted: number;
  rows: PmReviewRow[];
}
export interface PmProductStatus {
  store_key: string;
  product_id: string;
  status: string | null;
  updated_at: string;
}

// ---- Multimarket types ----
export interface MmResult {
  ok: boolean;
  error?: string;
  [k: string]: unknown;
}
export interface MmSyncShipping {
  ok: boolean;
  error?: string;
  changed?: boolean;
  created?: boolean;
  countries?: string[];
}
export interface MmCountryDefaults {
  ok: boolean;
  defaults: Record<string, { currency: string; locale: string }>;
  currencies: Record<string, string>;
  countries: { code: string; currency: string | null; locale: string | null }[];
}
export interface MmMarket {
  id: string;
  name: string;
  handle: string;
  primary: boolean;
  status: string;
  currency: string | null;
  countries: string[];
  locale: string | null;
  subfolder: string | null;
  has_catalog: boolean;
}
export interface MmLocale {
  locale: string;
  name?: string;
  primary?: boolean;
  published?: boolean;
}
export interface MmScan {
  ok: boolean;
  error?: string;
  markets?: MmMarket[];
  locales?: MmLocale[];
}
export interface MmAuditRow {
  code: string;
  status: "OK" | "PARTIAL" | "MISSING";
  expected: { currency?: string; locale?: string };
  issues: string[];
  marketName?: string;
}
export interface MmAudit {
  ok: boolean;
  error?: string;
  results?: MmAuditRow[];
  summary?: { ok: number; partial: number; missing: number; total: number };
}
export interface MmLanguages {
  ok: boolean;
  error?: string;
  languages?: MmLocale[];
}
export type MmLangOp = "add" | "publish" | "unpublish" | "remove";
export interface MmMarketInput {
  name?: string;
  countryCode?: string;
  countryName?: string;
  currency?: string;
  locale?: string;
  subfolderSuffix?: string;
  handle?: string;
}
export interface MmSetupResult {
  ok: boolean;
  error?: string;
  steps?: { step: string; ok: boolean; skipped?: boolean; error?: string }[];
  market?: { id: string; name: string; handle: string } | null;
}
export interface MmPolicy {
  type: string;
  body?: string;
  url?: string;
}
export interface MmPolicies {
  ok: boolean;
  error?: string;
  policies?: MmPolicy[];
}
export interface MmPolicyPreview {
  ok: boolean;
  error?: string;
  previews?: { type: string; body: string }[];
}
export interface MmLocalizeInput {
  handle: string;
  locales: string[];
  markets?: string[];
  glossary?: Record<string, string>;
  category?: string;
  head_keyword?: string;
  max_fix?: number;
  dry_run?: boolean;
}
export interface MmLocalizeQa {
  verdict: "PASS" | "FIX" | "REJECT";
  fluency: number;
  adequacy: number;
  terminology: number;
  drift: string;
  language_match: boolean;
  notes?: string;
}
export interface MmLocalizeRow {
  locale: string;
  verdict?: "PASS" | "FIX" | "REJECT";
  qa?: MmLocalizeQa;
  proposed?: Record<string, string>;
  written?: boolean;
  writes?: { market: string | null; ok: boolean; count?: number; error?: string }[];
  error?: string;
}
export interface MmLocalizeResult {
  ok: boolean;
  error?: string;
  handle?: string;
  resource_id?: string;
  source?: Record<string, string>;
  results?: MmLocalizeRow[];
}
export interface MmLocalizeAllInput {
  locales: string[];
  markets?: string[];
  dry_run?: boolean;
  limit?: number;
}
export interface MmLocalizeAllSummary {
  products: number;
  locales: number;
  written: number;
  pass: number;
  fix: number;
  reject: number;
  errors: number;
}
export interface MmLocalizeAllProduct {
  handle: string;
  ok: boolean;
  error?: string;
  results?: MmLocalizeRow[];
}
export interface MmLocalizeAllResult {
  ok: boolean;
  error?: string;
  store?: string;
  dry_run?: boolean;
  locales?: string[];
  summary?: MmLocalizeAllSummary;
  products?: MmLocalizeAllProduct[];
}
export interface MmLocalizeRead {
  ok: boolean;
  error?: string;
  handle?: string;
  locale?: string;
  translations?: { key: string; locale: string; value: string; market?: { id: string; name: string } }[];
}
export interface MmCoverageItem {
  type: string;
  name: string;
  note: string;
  auto: boolean;
}
export interface MmCoverage {
  ok: boolean;
  error?: string;
  groups?: { group: string; items: MmCoverageItem[] }[];
  resource_types?: string[];
}
export interface MmLocalizeEverythingInput {
  locales: string[];
  markets?: string[];
  scope?: string[];
  dry_run?: boolean;
  limit?: number;
  per_type_limit?: number;
}
export interface MmLocalizeSectionSummary {
  products?: number;
  resources?: number;
  locales?: number;
  written?: number;
  pass?: number;
  fix?: number;
  reject?: number;
  errors?: number;
}
export interface MmLocalizeSection {
  type: string;
  name: string;
  group: string;
  ok: boolean;
  error?: string;
  summary?: MmLocalizeSectionSummary;
  products?: MmLocalizeAllProduct[];
  items?: { resource_id: string; results?: MmLocalizeRow[] }[];
}
export interface MmLocalizeEverythingResult {
  ok: boolean;
  error?: string;
  store?: string;
  dry_run?: boolean;
  locales?: string[];
  scope?: string[];
  totals?: { written: number; pass: number; fix: number; reject: number; errors: number; resources: number };
  sections?: MmLocalizeSection[];
}
export interface MmGmcShippingService {
  serviceName?: string;
  active?: boolean;
  deliveryCountries?: string[];
  currencyCode?: string;
  [k: string]: unknown;
}
export interface MmGmcReturnPolicy {
  label?: string;
  countries?: string[];
  returnPolicyUri?: string;
  policy?: { type?: string; days?: number };
  [k: string]: unknown;
}
export interface MmGmcOverview {
  ok: boolean;
  error?: string;
  merchant_id?: string;
  shipping_services?: MmGmcShippingService[];
  shipping_error?: string | null;
  return_policies?: MmGmcReturnPolicy[];
  returns_error?: string | null;
  feed_countries?: string[] | null;
  template_countries?: string[];
}
export interface MmGmcShippingInput {
  countries?: string[];
  handlingMin?: number;
  handlingMax?: number;
  transitMin?: number;
  transitMax?: number;
  cutoffHour?: number;
  cutoffMinute?: number;
  timeZone?: string;
}
export interface MmGmcReturnsInput {
  countries?: string[];
  days?: number;
  returnPolicyUri?: string;
  feeType?: "FREE" | "FIXED" | "CUSTOMER";
  feeAmount?: number;
  processRefundDays?: number;
  condition?: "NEW" | "NEW_USED";
}

// ---- Orders & Issues types (native NN dispute-management port) ----
export interface IssuesMeta {
  ok: boolean;
  sources: string[];
  statuses_by_source: Record<string, string[]>;
  status_labels: Record<string, Record<string, string>>;
  outcomes: string[];
  listing_states: string[];
  customer_categories: string[];
  supplier_categories: string[];
  note_colors: string[];
  stores: string[];
}
export interface IssuesOverview {
  ok: boolean;
  store: string | null;
  open: number;
  total: number;
  by_status: Record<string, number>;
  unhandled_parcels: number;
  refund_count: number;
  refund_total: number;
}
export interface IssueOrder {
  order_id: string;
  order_number: string | null;
  order_date: string | null;
  customer_name: string | null;
  customer_email: string | null;
  currency: string | null;
  total: number | null;
  financial_status: string | null;
  fulfillment_status: string | null;
  item_count: number | null;
  store_key: string | null;
  shipping_address: string | null;
  tracking_number: string | null;
  tracking_url: string | null;
  tracking_company: string | null;
  cancelled_at: string | null;
  updated_at: string | null;
  open_dispute_id?: number | null;
}
export interface IssueOrderItem {
  id: number;
  order_id: string;
  product_id: string | null;
  title: string | null;
  sku: string | null;
  qty: number | null;
  price: number | null;
  variant_id: string | null;
  variant_title: string | null;
  image_url: string | null;
}
export interface IssueOrderDetail {
  ok: boolean;
  error?: string;
  order?: IssueOrder;
  items?: IssueOrderItem[];
  disputes?: Dispute[];
}
export interface Dispute {
  id: number;
  order_id: string;
  scope: string | null;
  status: string;
  issue_category: string | null;
  issue_location: string | null;
  description: string | null;
  next_steps: string | null;
  supplier_response: string | null;
  created_at: string | null;
  updated_at: string | null;
  status_since: string | null;
  resolved_at: string | null;
  assignee: string | null;
  supplier_name: string | null;
  resolution_type: string | null;
  resolution_amount: number | null;
  resolution_currency: string | null;
  source: string;
  cs_link: string | null;
  reminder_at: string | null;
  reminder_note: string | null;
  priority: string | null;
  supplier_action: string | null;
  over8_status: string | null;
  from_parcel: number | null;
  product_related: number | null;
  // joined columns (from dispute_list)
  order_number?: string | null;
  customer_name?: string | null;
  customer_email?: string | null;
  store_key?: string | null;
  total?: number | null;
  currency?: string | null;
  seen_at?: string | null;
}
export interface DisputeItem {
  dispute_id: number;
  order_item_id: number;
  solution: string | null;
  listing_updated: string | null;
  product_related: number | null;
  title?: string | null;
  sku?: string | null;
  qty?: number | null;
  image_url?: string | null;
  variant_title?: string | null;
}
export interface DisputeEvent {
  id: number;
  dispute_id: number;
  ts: string;
  author: string | null;
  kind: string | null;
  text: string | null;
}
export interface DisputeContact {
  dispute_id: number;
  seq: number;
  planned_at: string | null;
  sent: number | null;
  sent_at: string | null;
  channel: string | null;
  note: string | null;
}
export interface Refund {
  id: number;
  dispute_id: number | null;
  order_id: string | null;
  store_key: string | null;
  order_number: string | null;
  refund_date: string | null;
  product: string | null;
  refund_pct: number | null;
  currency: string | null;
  order_amount: number | null;
  refund_amount: number | null;
  amount_usd: number | null;
  cogs_recovered: number | null;
  reason: string | null;
  processed_by: string | null;
  notes: string | null;
  ticket_link: string | null;
  supplier_refunded: number | null;
  created_at: string | null;
}
export interface DisputeList {
  ok: boolean;
  disputes: Dispute[];
  counts: Record<string, number>;
}
export interface DisputeDetail {
  ok: boolean;
  error?: string;
  dispute?: Dispute;
  order?: IssueOrder | null;
  items?: DisputeItem[];
  events?: DisputeEvent[];
  contacts?: DisputeContact[];
  refunds?: Refund[];
}
export interface DisputeItemInput {
  order_item_id: number;
  solution?: string | null;
  listing_updated?: string | null;
  product_related?: boolean;
}
export interface DisputeCreateInput {
  order_id: string;
  source?: string;
  status?: string;
  scope?: string;
  issue_category?: string | null;
  issue_location?: string | null;
  description?: string | null;
  next_steps?: string | null;
  supplier_response?: string | null;
  assignee?: string | null;
  supplier_name?: string | null;
  cs_link?: string | null;
  priority?: string | null;
  from_parcel?: boolean;
  product_related?: boolean;
  items?: DisputeItemInput[];
}
export interface DisputeContactInput {
  planned_at?: string | null;
  sent?: boolean;
  sent_at?: string | null;
  channel?: string | null;
  note?: string | null;
}
export interface IssuesFollowups {
  ok: boolean;
  reminders: {
    id: number;
    order_id: string;
    status: string;
    reminder_at: string | null;
    reminder_note: string | null;
    assignee: string | null;
    order_number: string | null;
    customer_name: string | null;
  }[];
  contacts: {
    dispute_id: number;
    seq: number;
    planned_at: string | null;
    channel: string | null;
    status: string;
    order_number: string | null;
    customer_name: string | null;
  }[];
}
export interface ChatSendResult {
  ok: boolean;
  provider?: string;
  channel?: string;
  error?: string;
}
export interface IssueTemplate {
  id: number;
  title: string | null;
  body: string | null;
  created_by: string | null;
  created_at: string | null;
}
export interface IssueNote {
  id: number;
  owner: string | null;
  body: string | null;
  color: string | null;
  pinned: number | null;
  created_at: string | null;
  updated_at: string | null;
}
export interface IssueParcel {
  id: number;
  store: string | null;
  order_id: string | null;
  order_number: string | null;
  tracking_number: string | null;
  carrier: string | null;
  status: string | null;
  status_label: string | null;
  last_event: string | null;
  days_in_transit: number | null;
  handled: number | null;
  updated_at: string | null;
}

// ---- Tasks & Gameplans types (native NN tasks-app port) ----
export type TaskStatus = "todo" | "in_progress" | "blocked" | "done";
export interface TaskListRow {
  id: number;
  name: string;
  color: string | null;
  sort: number | null;
  created_at: string | null;
}
export interface Task {
  id: number;
  title: string;
  detail: string | null;
  store_key: string | null;
  list_id: number | null;
  assignee: string | null;
  status: TaskStatus;
  priority: string | null;
  due_at: string | null;
  done_at: string | null;
  created_by: string | null;
  created_at: string | null;
  updated_at: string | null;
}
export interface TaskListResult {
  ok: boolean;
  tasks: Task[];
  counts: { all: number; open: number; done: number };
}
export interface TaskCreateInput {
  title: string;
  detail?: string | null;
  store_key?: string | null;
  list_id?: number | null;
  assignee?: string | null;
  status?: TaskStatus;
  priority?: string | null;
  due_at?: string | null;
  created_by?: string | null;
}
export interface TasksOverview {
  ok: boolean;
  store: string | null;
  open: number;
  done: number;
  total: number;
  store_plans: number;
}
export interface StorePlan {
  store_key: string;
  body: string | null;
  updated_by: string | null;
  updated_at: string | null;
}
