"""Group gamification catalog — XP curve + event rules.

Source of truth: docs/group-gamification-catalog.md (ADR-009).

The catalog is checked-in code (not DB-driven) at this stage to keep the v1
deployment trivial. Future iteration may move to YAML / DB-backed config and a
hot-reload admin UI; the API surface here intentionally matches that shape so
the swap is mechanical.
"""

from __future__ import annotations

from dataclasses import dataclass

# ── XP / Level curve (catalog §1) ─────────────────────────────────────────
# Anchor points; intermediate levels are linearly interpolated.

LEVEL_ANCHORS: list[tuple[int, int]] = [
    (1, 0),
    (2, 50),
    (3, 120),
    (4, 200),
    (5, 300),
    (6, 400),
    (7, 500),
    (8, 600),
    (9, 780),
    (10, 1_000),
    (15, 2_000),
    (20, 3_500),
    (30, 6_000),
    (50, 12_000),
    (100, 30_000),
]

LEVEL_NAMES: dict[int, tuple[str, str]] = {
    1: ("種子期", "Seed"),
    2: ("萌芽期", "Sprout"),
    3: ("冒險期", "Explorer"),
    4: ("學習期", "Learner"),
    5: ("成長期", "Growing"),
    6: ("前進期", "Advancing"),
    7: ("穩紮期", "Rooted"),
    8: ("微光期", "Glimmer"),
    9: ("破繭期", "Breakthrough"),
    10: ("穩定期", "Steady"),
    15: ("蛻變期", "Transform"),
    20: ("綻放期", "Bloom"),
    30: ("閃耀期", "Radiant"),
    50: ("傳說期", "Legend"),
    100: ("永恆期", "Eternal"),
}

MAX_LEVEL = 100


def _build_level_table() -> list[int]:
    """Pre-compute total_xp threshold for each level 1..MAX_LEVEL via linear interpolation."""
    table: list[int] = [0] * (MAX_LEVEL + 1)  # idx 0 unused; idx 1 = LV.1 threshold = 0
    anchors = sorted(LEVEL_ANCHORS)
    for (lv_a, xp_a), (lv_b, xp_b) in zip(anchors, anchors[1:], strict=False):
        for lv in range(lv_a, lv_b + 1):
            ratio = (lv - lv_a) / (lv_b - lv_a) if lv_b != lv_a else 0
            table[lv] = round(xp_a + (xp_b - xp_a) * ratio)
    last_lv, last_xp = anchors[-1]
    table[last_lv] = last_xp
    return table


LEVEL_XP_TABLE: list[int] = _build_level_table()


def level_for_xp(total_xp: int) -> int:
    """Highest level reached given cumulative xp."""
    if total_xp < 0:
        raise ValueError("total_xp must be non-negative")
    lv = 1
    for candidate in range(1, MAX_LEVEL + 1):
        if total_xp >= LEVEL_XP_TABLE[candidate]:
            lv = candidate
        else:
            break
    return lv


def xp_for_level(level: int) -> int:
    """Cumulative XP threshold to reach `level`."""
    if level < 1 or level > MAX_LEVEL:
        raise ValueError(f"level out of range 1..{MAX_LEVEL}")
    return LEVEL_XP_TABLE[level]


def xp_to_next_level(total_xp: int) -> int:
    """How much more xp needed to advance one level. 0 if at MAX."""
    cur = level_for_xp(total_xp)
    if cur >= MAX_LEVEL:
        return 0
    return max(0, LEVEL_XP_TABLE[cur + 1] - total_xp)


def level_name(level: int) -> tuple[str, str]:
    """Return (zh, en) name. Uses the closest *lower or equal* anchor."""
    candidate = 1
    for anchor_lv in sorted(LEVEL_NAMES.keys()):
        if level >= anchor_lv:
            candidate = anchor_lv
        else:
            break
    return LEVEL_NAMES[candidate]


# ── Event catalog (catalog §3) ────────────────────────────────────────────


@dataclass(frozen=True)
class EventRule:
    """How to score a single event_kind."""

    source_app: str
    xp: int
    category: str  # passive | micro | milestone | major | bonus | achievement
    daily_cap_xp: int | None = None  # max XP this kind can contribute per day
    lifetime_unique: bool = False  # only counts once ever (per uuid)
    diminishing_after_n: int | None = None  # after N occurrences/day, drop XP
    diminishing_xp: int | None = None  # XP after diminishing kicks in


# Subset of catalog §3 — all events listed in docs/group-gamification-catalog.md
# §3.1-3.6. Keep keys stable: changing keys breaks idempotency_key history.
EVENT_CATALOG: dict[str, EventRule] = {
    # --- 3.1 dodo (潘朵拉飲食) ---
    "dodo.app_opened": EventRule("dodo", 1, "passive", daily_cap_xp=5),
    "dodo.meal_logged": EventRule(
        "dodo", 5, "micro", daily_cap_xp=30,
        diminishing_after_n=3, diminishing_xp=2,
    ),
    "dodo.meal_score_80_plus": EventRule("dodo", 10, "micro", daily_cap_xp=30),
    "dodo.daily_score_80_plus": EventRule("dodo", 15, "milestone", daily_cap_xp=15),
    "dodo.streak_3": EventRule("dodo", 20, "milestone"),
    "dodo.streak_7": EventRule("dodo", 50, "milestone"),
    "dodo.streak_14": EventRule("dodo", 100, "milestone"),
    "dodo.streak_30": EventRule("dodo", 200, "major"),
    "dodo.weekly_review_read": EventRule("dodo", 10, "micro"),
    "dodo.chat_daily": EventRule("dodo", 3, "passive", daily_cap_xp=3),
    "dodo.weight_logged": EventRule("dodo", 5, "micro", daily_cap_xp=5),
    "dodo.first_meal_of_day": EventRule("dodo", 5, "micro", daily_cap_xp=5),
    "dodo.new_food_discovered": EventRule("dodo", 8, "micro", daily_cap_xp=24),
    "dodo.card_correct": EventRule("dodo", 8, "micro", daily_cap_xp=40),
    "dodo.card_first_solve": EventRule("dodo", 5, "micro", lifetime_unique=True),
    # --- 3.2 jerosse (婕樂纖) ---
    "jerosse.app_opened": EventRule("jerosse", 1, "passive", daily_cap_xp=5),
    "jerosse.product_browsed": EventRule("jerosse", 1, "passive", daily_cap_xp=10),
    "jerosse.article_read": EventRule("jerosse", 3, "passive", daily_cap_xp=15),
    "jerosse.cart_added": EventRule("jerosse", 3, "micro", daily_cap_xp=9),
    "jerosse.first_browse": EventRule("jerosse", 30, "milestone", lifetime_unique=True),
    "jerosse.first_cart": EventRule("jerosse", 30, "milestone", lifetime_unique=True),
    "jerosse.first_order": EventRule("jerosse", 100, "major", lifetime_unique=True),
    "jerosse.order_paid": EventRule("jerosse", 20, "milestone"),
    "jerosse.review_written": EventRule("jerosse", 30, "milestone"),
    "jerosse.spend_1k_milestone": EventRule("jerosse", 30, "milestone", lifetime_unique=True),
    "jerosse.spend_5k_milestone": EventRule("jerosse", 100, "milestone", lifetime_unique=True),
    "jerosse.spend_10k_milestone": EventRule("jerosse", 300, "major", lifetime_unique=True),
    "jerosse.subscription_renewed": EventRule("jerosse", 50, "milestone"),
    "jerosse.referral_signed": EventRule("jerosse", 200, "major"),
    # --- 3.3 calendar (潘朵拉月曆) ---
    "calendar.app_opened": EventRule("calendar", 1, "passive", daily_cap_xp=3),
    "calendar.cycle_logged": EventRule("calendar", 5, "micro", daily_cap_xp=5),
    "calendar.symptom_logged": EventRule("calendar", 3, "micro", daily_cap_xp=9),
    "calendar.mood_logged": EventRule("calendar", 3, "micro", daily_cap_xp=3),
    "calendar.track_7_days": EventRule("calendar", 30, "milestone"),
    "calendar.full_cycle_tracked": EventRule("calendar", 100, "major"),
    "calendar.insight_read": EventRule("calendar", 5, "micro", daily_cap_xp=10),
    # --- 3.4 skin (潘朵拉肌膚) ---
    "skin.app_opened": EventRule("skin", 1, "passive", daily_cap_xp=3),
    "skin.skin_scan": EventRule("skin", 10, "micro", daily_cap_xp=20),
    "skin.routine_logged": EventRule("skin", 5, "micro", daily_cap_xp=10),
    "skin.product_added": EventRule("skin", 3, "micro", daily_cap_xp=9),
    "skin.weekly_report_read": EventRule("skin", 10, "milestone"),
    "skin.30_day_tracked": EventRule("skin", 100, "major"),
    # --- 3.5 academy (潘朵拉學院) ---
    "academy.app_opened": EventRule("academy", 1, "passive", daily_cap_xp=3),
    "academy.lesson_started": EventRule("academy", 3, "micro", daily_cap_xp=9),
    "academy.lesson_completed": EventRule("academy", 50, "milestone"),
    "academy.quiz_passed": EventRule("academy", 30, "milestone"),
    "academy.course_completed": EventRule("academy", 200, "major"),
    "academy.operator_track_progress": EventRule("academy", 100, "major"),
    # --- 3.6 group (cross-app combo) ---
    "group.multi_app_active_today": EventRule("group", 50, "bonus", daily_cap_xp=50),
    "group.multi_app_active_3_apps_today": EventRule("group", 100, "bonus", daily_cap_xp=100),
    "group.cross_app_streak_7": EventRule("group", 200, "bonus"),
    "group.cross_app_streak_30": EventRule("group", 500, "bonus"),
    "group.seasonal_event_completed": EventRule("group", 100, "bonus"),
    "group.referral_friend_signed": EventRule("group", 200, "major"),
    "group.referral_friend_first_order": EventRule("group", 500, "major"),
}


def get_event_rule(event_kind: str) -> EventRule:
    rule = EVENT_CATALOG.get(event_kind)
    if rule is None:
        raise KeyError(f"unknown event_kind: {event_kind}")
    return rule


# ── Achievement catalog (catalog §5) ──────────────────────────────────────


@dataclass(frozen=True)
class AchievementDef:
    """Built-in achievement definition. Gets seeded into the gamification_achievements
    table by the seed admin endpoint. Keys are stable; renames break dashboards."""

    code: str
    name: str
    description: str
    source_app: str
    tier: str  # bronze / silver / gold / legendary


# Tier → XP reward (catalog §5.1)
TIER_XP_REWARD: dict[str, int] = {
    "bronze": 30,
    "silver": 100,
    "gold": 300,
    "legendary": 1000,
}


# Subset of catalog §5.2 + a few cross-app group achievements. Add freely; the
# seed endpoint upserts new entries and updates name/description/tier of existing
# ones (xp_reward is derived from tier each seed).
ACHIEVEMENT_CATALOG: dict[str, AchievementDef] = {
    # 朵朵 (catalog §5.2)
    "dodo.first_meal": AchievementDef(
        "dodo.first_meal", "第一餐", "記錄了第一筆餐食", "dodo", "bronze",
    ),
    "dodo.streak_7": AchievementDef(
        "dodo.streak_7", "一週有你", "連續 7 天打卡", "dodo", "silver",
    ),
    "dodo.streak_30": AchievementDef(
        "dodo.streak_30", "一個月的陪伴", "連續 30 天打卡", "dodo", "gold",
    ),
    "dodo.foodie_10": AchievementDef(
        "dodo.foodie_10", "美食探索家", "圖鑑收集 10 種食物", "dodo", "silver",
    ),
    # 婕樂纖 (catalog §5.2)
    "jerosse.first_browse": AchievementDef(
        "jerosse.first_browse", "好奇探索家", "第一次逛婕樂纖", "jerosse", "bronze",
    ),
    "jerosse.first_order": AchievementDef(
        "jerosse.first_order", "首購達成", "第一筆婕樂纖訂單", "jerosse", "silver",
    ),
    "jerosse.spend_10k": AchievementDef(
        "jerosse.spend_10k", "金級夥伴", "累積消費滿 1 萬", "jerosse", "gold",
    ),
    # 跨 App group achievements (catalog §5.2)
    "group.multi_app_explorer": AchievementDef(
        "group.multi_app_explorer", "跨界探索家",
        "體驗 3 個以上潘朵拉系列 App", "group", "gold",
    ),
    "group.full_constellation": AchievementDef(
        "group.full_constellation", "潘朵拉全收",
        "集滿所有潘朵拉系列 App 的首次成就", "group", "legendary",
    ),
}


def get_achievement_def(code: str) -> AchievementDef:
    ach = ACHIEVEMENT_CATALOG.get(code)
    if ach is None:
        raise KeyError(f"unknown achievement code: {code}")
    return ach


def xp_reward_for_tier(tier: str) -> int:
    if tier not in TIER_XP_REWARD:
        raise ValueError(f"invalid tier: {tier} (expected one of {list(TIER_XP_REWARD)})")
    return TIER_XP_REWARD[tier]


# ── Outfit catalog (catalog §6) ───────────────────────────────────────────


@dataclass(frozen=True)
class OutfitDef:
    """Built-in outfit definition. Seeded into gamification_outfit_catalog.

    `unlock_condition` is a free-form string for now — apps interpret it for
    UX hint copy ("LV.5", "streak 7 days", "fp_lifetime tier"). Actual unlock
    grant flow is per-condition (see ADR-009 §6.3 future iteration).
    """

    code: str
    name: str
    unlock_condition: str
    tier: str  # "default" / "level" / "streak" / "fp" / "cross_app"
    species_compat: tuple[str, ...]  # () = all species


# Source: group-gamification-catalog.md §6.1 + §6.2
OUTFIT_CATALOG: dict[str, OutfitDef] = {
    # 6.1 既有（朵朵搬過來）
    "none": OutfitDef("none", "基本", "default", "default", ()),
    "scarf": OutfitDef("scarf", "溫暖圍巾", "LV.5", "level", ()),
    "glasses": OutfitDef("glasses", "圓框眼鏡", "LV.8", "level", ()),
    "headphones": OutfitDef("headphones", "玫瑰耳機", "LV.12", "level", ()),
    "straw_hat": OutfitDef("straw_hat", "草帽", "streak 7 days", "streak", ()),
    "angel_wings": OutfitDef("angel_wings", "天使翅膀", "LV.20", "level", ()),
    "fp_crown": OutfitDef("fp_crown", "FP 皇冠", "fp_lifetime tier", "fp", ()),
    "fp_chef": OutfitDef("fp_chef", "FP 主廚裝", "fp_lifetime tier", "fp", ()),
    # 6.2 規劃跨 App 解鎖
    "jerosse_vip_dress": OutfitDef(
        "jerosse_vip_dress", "VIP 禮服", "jerosse spend ≥ 10k", "cross_app", ()
    ),
    "calendar_moon": OutfitDef(
        "calendar_moon", "月相披風", "calendar full_cycle x3", "cross_app", ()
    ),
    "skin_glow": OutfitDef(
        "skin_glow", "透亮光環", "skin 30_day_tracked", "cross_app", ()
    ),
    "academy_grad": OutfitDef(
        "academy_grad", "學士袍", "academy course_completed", "cross_app", ()
    ),
    "group_eternal": OutfitDef(
        "group_eternal", "永恆冠冕", "LV.100", "level", ()
    ),
}


def get_outfit_def(code: str) -> OutfitDef:
    out = OUTFIT_CATALOG.get(code)
    if out is None:
        raise KeyError(f"unknown outfit code: {code}")
    return out


def parse_level_unlock(unlock_condition: str) -> int | None:
    """If `unlock_condition` is a level gate (e.g. "LV.5"), return the level int.

    Returns None for non-level conditions (streak / fp / cross_app), which
    are unlocked via separate per-tier flows.
    """
    if not unlock_condition.startswith("LV."):
        return None
    try:
        return int(unlock_condition[3:].strip())
    except ValueError:
        return None


def level_unlock_outfits_up_to(level: int) -> list[OutfitDef]:
    """All level-tier outfits whose unlock level is <= `level`."""
    out: list[OutfitDef] = []
    for d in OUTFIT_CATALOG.values():
        if d.tier != "level":
            continue
        gate = parse_level_unlock(d.unlock_condition)
        if gate is not None and gate <= level:
            out.append(d)
    return out
