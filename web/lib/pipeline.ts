// The Google Stores project as TWO operator paths behind a pre-selector:
//   - "general" — broad multi-category Google-Shopping catalog. ONE lean
//     "Research" stage holds both discovery natures (keyword+trend AND market
//     signals: spy best-sellers minus our catalog, Meta dropship, Amazon movers),
//     both feeding one candidate queue → LITE listing → ads → scale.
//   - "niche"   — deep single-niche launches: dossier/ICP research → full-PDP
//     listing → ads → scale.
// Each path is its own ordered "waterfall". Built stages carry a `href`;
// not-yet-built stages (ads / scale) are shown as upcoming with `href: null`.
// `carriesKeyword` marks stages where a `?keyword=` handoff is meaningful.
//
// keyword/trend/product are no longer top-level stages — they are *modes inside*
// the Research stage. They survive as keys only so the existing deep-dive pages
// (/keyword-discovery, /trends, /product-research) can resolve back to "research"
// for the rail. `railStageKey()` does that mapping.

export type PipelinePath = "general" | "niche";

export type PipelineStageKey =
  | "niche"
  | "research"
  | "keyword"
  | "trend"
  | "product"
  | "listing"
  | "ads"
  | "scale";

// Sub-modes of Research → which rail stage they light up.
const STAGE_PARENT: Partial<Record<PipelineStageKey, PipelineStageKey>> = {
  keyword: "research",
  trend: "research",
  product: "research",
};

// The rail/overview stage a page belongs to (folds research modes into Research).
export function railStageKey(current: PipelineStageKey): PipelineStageKey {
  return STAGE_PARENT[current] ?? current;
}

export type PipelineStage = {
  key: PipelineStageKey;
  n: number;
  label: string;
  href: string | null; // null = surface not built yet (upcoming)
  tagline: string;
  produces: string; // what flows OUT of this stage into the next
  carriesKeyword: boolean;
};

export type PathDef = {
  key: PipelinePath;
  label: string;
  tagline: string;
  stages: PipelineStage[];
};

const SHARED_TAIL: PipelineStage[] = [
  {
    key: "ads",
    n: 0,
    label: "Ads",
    href: null,
    tagline: "Bridge live listings to Google Shopping campaigns.",
    produces: "Spending campaigns",
    carriesKeyword: false,
  },
  {
    key: "scale",
    n: 0,
    label: "Scale",
    href: null,
    tagline: "The weekly compounding loop — cull losers, graduate winners.",
    produces: "Winners looped back into Listing",
    carriesKeyword: false,
  },
];

const GENERAL_HEAD: PipelineStage[] = [
  {
    key: "research",
    n: 0,
    label: "Research",
    href: "/keyword-discovery",
    tagline:
      "Two discovery modes into one candidate queue: Keyword & Trend, and Market Signals (spy best-sellers minus our catalog, Meta dropship, Amazon movers).",
    produces: "A scored candidate queue → locked product set",
    carriesKeyword: true,
  },
  {
    key: "listing",
    n: 0,
    label: "Listing",
    href: "/stores",
    tagline: "Build the SKUs — supplier-true variants, gallery, LITE PDP, go-live.",
    produces: "Live Google-Shopping listings",
    carriesKeyword: false,
  },
];

const NICHE_HEAD: PipelineStage[] = [
  {
    key: "niche",
    n: 0,
    label: "Niche Research",
    href: "/dossiers",
    tagline: "Deep per-niche dossier — demand, competition, trends, SERP, buyer voice, domains.",
    produces: "A validated niche dossier + ICP",
    carriesKeyword: false,
  },
  {
    key: "listing",
    n: 0,
    label: "Listing",
    href: "/stores",
    tagline: "Build the niche store — full-PDP SKUs, brand voice, go-live.",
    produces: "Live niche-store listings",
    carriesKeyword: false,
  },
];

// Number the stages 1..n within each path.
function numbered(stages: PipelineStage[]): PipelineStage[] {
  return stages.map((s, i) => ({ ...s, n: i + 1 }));
}

export const PATHS: Record<PipelinePath, PathDef> = {
  general: {
    key: "general",
    label: "General Store",
    tagline: "Broad multi-category catalog — keyword-driven, breadth finds the winners.",
    stages: numbered([...GENERAL_HEAD, ...SHARED_TAIL]),
  },
  niche: {
    key: "niche",
    label: "Niche Store",
    tagline: "Deep single-niche launch — dossier-driven, full-PDP craft.",
    stages: numbered([...NICHE_HEAD, ...SHARED_TAIL]),
  },
};

export const PATH_ORDER: PipelinePath[] = ["general", "niche"];

export const DEFAULT_PATH: PipelinePath = "general";

// Where a path selection should land the operator (its first built stage).
export function pathEntryHref(path: PipelinePath): string {
  const first = PATHS[path].stages.find((s) => s.href != null);
  return first?.href ?? "/";
}

// Which path owns a stage page. Research sub-modes fold to "research" first.
// Shared stages (listing/ads/scale) defer to the currently-active path;
// path-unique stages resolve to their owning path.
export function resolvePath(current: PipelineStageKey, activePath: PipelinePath): PipelinePath {
  const stage = railStageKey(current);
  if (PATHS[activePath].stages.some((s) => s.key === stage)) return activePath;
  const owner = PATH_ORDER.find((p) => PATHS[p].stages.some((s) => s.key === stage));
  return owner ?? activePath;
}

// Resolve a stage's destination, forwarding the keyword context when the stage
// can act on it (so rail / handoff links keep the keyword).
export function stageHref(stage: PipelineStage, keyword?: string | null): string | null {
  if (!stage.href) return null;
  if (keyword && stage.carriesKeyword) {
    return `${stage.href}?keyword=${encodeURIComponent(keyword)}`;
  }
  return stage.href;
}
