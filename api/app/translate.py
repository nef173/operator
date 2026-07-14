"""Chinese → English translation for 1688 sourcing text (variants / specs / titles).

The 1688 / TMAPI feed comes back in Chinese (颜色: 红色, 风速档位: 3档, 噪音: 36dB(A)以下).
The operator reads English, so every label shown in the Sourcing Match UI is translated here.

Two layers, cheapest-first:
  1. A deterministic GLOSSARY (free, instant, offline) covering the common SKU + spec
     vocabulary — property names, colors, sizes, materials, electronics spec terms. This
     alone makes the vast majority of labels readable with zero cost or latency.
  2. A cached LLM fallback for anything the glossary leaves in Chinese — so "translate ALL
     to English" actually holds for arbitrary spec text. It batches every residual CJK
     string into ONE chat-completions call (same OpenAI-compatible gateway the Assistant
     uses, over stdlib urllib), then caches the result to disk FOREVER (translation never
     changes), so a given string is only ever paid for once. If the gateway isn't configured
     (or the call fails) it degrades silently to the glossary result — never raises.

`prime(strings)` warms the cache for a whole page-load in one batch; `to_english(s)` then
resolves each label from cache → glossary with no per-label network cost.
"""
from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request

from . import config, connections

# ---------------------------------------------------------------- glossary
# Keys are matched longest-first so multi-char terms win over their substrings. Single CJK
# characters are deliberately AVOIDED — they mangle compound words (带→"With" turns 腰带 into
# "腰With"). The LLM layer mops up whatever the glossary can't.
_GLOSSARY: dict[str, str] = {
    # ── property names ──
    "颜色分类": "Color", "颜色名称": "Color", "颜色": "Color",
    "尺码大小": "Size", "尺寸": "Size", "尺码": "Size", "规格": "Spec",
    "型号": "Model", "适用型号": "Compatible model", "材质": "Material", "材料": "Material",
    "款式": "Style", "风格": "Style", "图案": "Pattern", "套餐": "Bundle", "套装": "Set",
    "容量": "Capacity", "功率": "Power", "额定功率": "Rated power", "额定电压": "Rated voltage",
    "额定电流": "Rated current", "重量": "Weight", "长度": "Length", "宽度": "Width",
    "高度": "Height", "直径": "Diameter", "厚度": "Thickness", "品牌": "Brand", "产地": "Origin",
    "包装": "Packaging", "包装方式": "Packaging", "数量": "Quantity", "版本": "Version",
    "功能": "Function", "附加功能": "Extra functions", "类型": "Type", "分类": "Category",
    "净含量": "Net content", "毛重": "Gross weight", "净重": "Net weight",
    # ── electronics / appliance specs (e.g. neck fans, lamps, gadgets) ──
    "电源方式": "Power source", "供电方式": "Power source", "充电方式": "Charging",
    "续航时长": "Battery life", "续航时间": "Battery life", "电池容量": "Battery capacity",
    "风速档位": "Wind speed levels", "档位": "Levels", "风扇分类": "Fan type",
    "挂脖风扇": "Neck fan", "挂脖": "Neck-hanging", "手持": "Handheld", "落地": "Floor-standing",
    "噪音": "Noise", "数显": "Digital display", "静音": "Quiet", "无线": "Wireless",
    "有线": "Wired", "接口类型": "Port type", "工作电压": "Working voltage",
    "工作时间": "Working time", "适用场景": "Use scene", "适用人群": "For",
    "是否充电": "Rechargeable", "充电时间": "Charging time", "材质工艺": "Material",
    "线长": "Cable length", "灯光颜色": "Light color", "光源功率": "Light power",
    "防水等级": "Waterproof rating", "使用方式": "Usage", "安装方式": "Mounting",
    # ── 1688 cross-border export boilerplate spec fields ──
    "操作方式": "Operation", "普通按钮": "Push button", "普通": "Standard",
    "主要下游平台": "Main platforms", "主要销售地区": "Main markets",
    "有可授权的自有": "Own-brand authorization", "是否跨境出口专供货源": "Cross-border export supply",
    "是否专利货源": "Patented source", "是否变频": "Variable frequency", "不支持变频": "No variable frequency",
    "是否内置": "Built-in", "是否支持": "Supported", "是否充电": "Rechargeable",
    "内置蓄": "Built-in battery", "内置": "Built-in", "蓄电池": "Battery",
    "电机": "Motor", "无刷电机": "Brushless motor", "扇叶": "Fan blade", "无叶": "Bladeless",
    "货号": "Item No.", "生产企业": "Manufacturer", "能效等级": "Energy rating",
    "无能效等级": "No energy rating", "装箱数": "Pcs per carton", "免安装": "No install",
    "中国大陆": "Mainland China", "毫安": "mAh", "外观": "Appearance", "产品": "Product",
    "体积": "Volume", "支持": "Supported", "不支持": "Not supported",
    # export platform + sales-region values (1688 boilerplate lists)
    "亚马逊": "Amazon", "速卖通": "AliExpress", "独立站": "Independent site", "其他": "Other",
    "日韩": "Japan & Korea", "非洲": "Africa", "韩国": "Korea", "日本": "Japan",
    "欧洲": "Europe", "南美": "South America", "东南亚": "Southeast Asia", "北美": "North America",
    "东北亚": "Northeast Asia", "东亚": "East Asia", "中东": "Middle East",
    "拉丁美洲": "Latin America", "欧美": "Europe & America", "数据线": "Data cable",
    # last-resort single chars (applied last, after every multi-char key) — common spec values
    "是": "Yes", "否": "No", "含": "incl.", "档": " levels", "线": " cable",
    # ── colors ──
    "红色": "Red", "大红色": "Red", "橙色": "Orange", "黄色": "Yellow", "绿色": "Green",
    "青色": "Cyan", "蓝色": "Blue", "天蓝色": "Sky Blue", "深蓝色": "Navy", "浅蓝色": "Light Blue",
    "紫色": "Purple", "粉红色": "Pink", "粉色": "Pink", "黑色": "Black", "白色": "White",
    "灰色": "Gray", "棕色": "Brown", "咖啡色": "Coffee", "米色": "Beige", "金色": "Gold",
    "银色": "Silver", "玫瑰金": "Rose Gold", "透明": "Transparent", "彩色": "Multicolor",
    "军绿色": "Army Green", "卡其色": "Khaki", "玫红色": "Rose Red", "酒红色": "Wine Red",
    "藏青色": "Navy", "深灰色": "Dark Gray", "浅灰色": "Light Gray", "墨绿色": "Dark Green",
    # ── sizes / quantities ──
    "加大码": "XL", "特大码": "XXL", "均码": "One Size", "标准": "Standard", "通用": "Universal",
    "默认": "Default", "加厚": "Thickened", "加长": "Extended", "便携式": "Portable",
    "便携": "Portable", "大号": "Large", "中号": "Medium", "小号": "Small", "加大": "XL",
    "特大": "XXL", "一套": "1 Set", "单个": "Single", "一个": "1 pc", "两个": "2 pcs",
    "三个": "3 pcs", "以下": "below", "以上": "above", "左右": "approx.",
    # ── materials ──
    "不锈钢": "Stainless Steel", "铝合金": "Aluminum Alloy", "塑料": "Plastic", "金属": "Metal",
    "硅胶": "Silicone", "橡胶": "Rubber", "玻璃": "Glass", "木质": "Wood", "实木": "Solid Wood",
    "皮革": "Leather", "纯棉": "Cotton", "尼龙": "Nylon", "陶瓷": "Ceramic", "合金": "Alloy",
    "碳纤维": "Carbon Fiber", "亚克力": "Acrylic", "聚酯纤维": "Polyester",
    # ── modifiers (multi-char only) ──
    "新款": "New", "升级": "Upgraded", "电动": "Electric", "手动": "Manual",
    "充电": "Rechargeable", "电池": "Battery", "家用": "Home", "不含": "Excl.", "附加": "Extra",
    "左侧": "Left", "右侧": "Right", "成人": "Adult", "儿童": "Kids", "男款": "Men",
    "女款": "Women", "双人": "Double", "单人": "Single", "大容量": "Large capacity",
}
_GLOSSARY_KEYS = sorted(_GLOSSARY, key=len, reverse=True)


def has_cjk(s: str) -> bool:
    return any("\u4e00" <= ch <= "\u9fff" for ch in s)


def _glossary(s: str) -> str:
    """Deterministic substring translation (longest-match-first). Leaves unknown CJK + all
    Latin/digits untouched, then tidies separators left by removed tokens."""
    out = s
    for k in _GLOSSARY_KEYS:
        if k in out:
            out = out.replace(k, _GLOSSARY[k])
    out = " ".join(out.split())
    return out.strip(" :·,-") or s


# ---------------------------------------------------------------- disk cache
_CACHE_PATH = config.data_root() / "operator-app" / "api" / "data" / "translation-cache.json"
_lock = threading.Lock()
_cache: dict[str, str] | None = None


def _load() -> dict[str, str]:
    global _cache
    if _cache is None:
        try:
            _cache = json.loads(_CACHE_PATH.read_text("utf-8"))
            if not isinstance(_cache, dict):
                _cache = {}
        except (OSError, ValueError):
            _cache = {}
    return _cache


def _save() -> None:
    if _cache is None:
        return
    try:
        _CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        _CACHE_PATH.write_text(json.dumps(_cache, ensure_ascii=False, indent=0), "utf-8")
    except OSError:
        pass


# ---------------------------------------------------------------- LLM batch (stdlib)
def _llm_configured() -> bool:
    return bool(
        connections.runtime_get("ASSISTANT_LLM_BASE_URL")
        and connections.runtime_get("ASSISTANT_LLM_API_KEY")
    )


def _llm_translate(strings: list[str]) -> dict[str, str]:
    """One chat-completions call translating a batch of short CN labels → EN. Returns a
    {source: english} map; raises on any transport/parse problem so the caller can fall back."""
    base = (connections.runtime_get("ASSISTANT_LLM_BASE_URL") or "").rstrip("/")
    key = connections.runtime_get("ASSISTANT_LLM_API_KEY") or ""
    model = connections.runtime_get("ASSISTANT_LLM_MODEL") or "gpt-4o-mini"
    system = (
        "You translate short Chinese e-commerce product attribute labels (variant options, "
        "spec names and values from 1688/Alibaba) into concise natural English. Keep it short "
        "and literal — these are dropdown/spec labels, not sentences. Preserve any Latin text, "
        "numbers, units and punctuation as-is. Reply with ONLY a JSON object mapping each input "
        "string to its English translation, no commentary."
    )
    user = json.dumps(strings, ensure_ascii=False)
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "max_tokens": 1500,
        "temperature": 0,
    }
    req = urllib.request.Request(
        f"{base}/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=45) as resp:
        body = json.loads(resp.read().decode("utf-8"))
    content = body["choices"][0]["message"]["content"] or "{}"
    # tolerate a ```json fence
    c = content.strip()
    if c.startswith("```"):
        c = c.split("```")[1] if "```" in c[3:] else c[3:]
        if c.startswith("json"):
            c = c[4:]
    start, end = c.find("{"), c.rfind("}")
    if start == -1 or end == -1:
        raise ValueError("no JSON object in translation reply")
    out = json.loads(c[start : end + 1])
    return {k: str(v) for k, v in out.items() if isinstance(k, str)}


def prime(strings: list[str]) -> None:
    """Warm the cache for a set of raw labels in ONE batch. Glossary first (free); whatever
    still contains Chinese is sent to the LLM (if configured) and cached. Best-effort: any
    failure leaves the glossary result in place. Call once per page-load before to_english()."""
    cache = _load()
    residual: list[str] = []
    dirty = False
    for s in {x for x in strings if x and has_cjk(x)}:
        if s in cache:
            continue
        g = _glossary(s)
        cache[s] = g  # always at least the glossary result
        dirty = True
        if has_cjk(g):  # glossary didn't fully translate → needs the LLM
            residual.append(s)
    if residual and _llm_configured():
        try:
            with _lock:
                mapped = _llm_translate(residual)
            for src, eng in mapped.items():
                if eng and not has_cjk(eng):
                    cache[src] = eng
                    dirty = True
        except (urllib.error.URLError, ValueError, KeyError, TimeoutError, OSError):
            pass  # keep glossary fallback
    if dirty:
        _save()


def to_english(s: str | None) -> str:
    """Resolve one label: cache → glossary. No network here (prime() does the LLM batch)."""
    if not s or not has_cjk(s):
        return s or ""
    cache = _load()
    hit = cache.get(s)
    if hit and not has_cjk(hit):
        return hit
    return _glossary(s)
