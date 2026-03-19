import os
import re
from collections import Counter, defaultdict
from datetime import datetime, timezone
from difflib import SequenceMatcher
from zoneinfo import ZoneInfo

import feedparser
import pandas as pd
import requests
import streamlit as st


# =========================================================
# 基本設定
# =========================================================
st.set_page_config(page_title="世界・マクロ・エネルギー大局把握", layout="wide")
st.title("世界・マクロ・エネルギー情勢：大局把握")
st.caption("育休中の学び直し向け。速報追跡より『大きな流れ』をつかむ設計。")

JST = ZoneInfo("Asia/Tokyo")
NOW_JST = datetime.now(JST)

TIMEOUT = 15
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/122.0 Safari/537.36"
)

HISTORY_FILE = "news_history.csv"


# =========================================================
# ソース定義
# =========================================================
SOURCES = [
    {
        "name": "Bloomberg（Markets）",
        "url": "https://feeds.bloomberg.com/markets/news.rss",
        "enabled": True,
        "source_weight": 3,
        "kind": "markets",
    },
    {
        "name": "Yahoo（経済）",
        "url": "https://news.yahoo.co.jp/rss/topics/business.xml",
        "enabled": True,
        "source_weight": 4,
        "kind": "ja_news",
    },
    {
        "name": "Yahoo（国際）",
        "url": "https://news.yahoo.co.jp/rss/topics/world.xml",
        "enabled": True,
        "source_weight": 4,
        "kind": "ja_news",
    },
    {
        "name": "EIA（Today in Energy）",
        "url": "https://www.eia.gov/rss/todayinenergy.xml",
        "enabled": True,
        "source_weight": 5,
        "kind": "energy_primary",
    },
    {
        "name": "EIA（What's New）",
        "url": "https://www.eia.gov/rss/whatsnew.xml",
        "enabled": True,
        "source_weight": 4,
        "kind": "energy_primary",
    },
    {
        "name": "EIA（Press Releases）",
        "url": "https://www.eia.gov/rss/press_rss.xml",
        "enabled": True,
        "source_weight": 4,
        "kind": "energy_primary",
    },
]


# =========================================================
# 簡易和訳
# =========================================================
TERM_MAP = {
    r"\boil\b": "原油",
    r"\bcrude\b": "原油",
    r"\bgas\b": "ガス",
    r"\bnatural gas\b": "天然ガス",
    r"\blng\b": "LNG",
    r"\bopec\+?\b": "OPEC+",
    r"\binflation\b": "インフレ",
    r"\brates?\b": "金利",
    r"\bfed\b": "米連邦準備制度（FRB）",
    r"\becb\b": "欧州中央銀行（ECB）",
    r"\bboj\b": "日銀",
    r"\btreasur(?:y|ies)\b": "米国債",
    r"\byields?\b": "利回り",
    r"\bdollar\b": "ドル",
    r"\byen\b": "円",
    r"\bfx\b": "為替",
    r"\bstocks?\b": "株",
    r"\bequities\b": "株",
    r"\bgold\b": "金",
    r"\bpower\b": "電力",
    r"\belectricity\b": "電力",
    r"\brenewables?\b": "再生可能エネルギー",
    r"\bsolar\b": "太陽光",
    r"\bwind\b": "風力",
    r"\bnuclear\b": "原子力",
    r"\bhydrogen\b": "水素",
    r"\bammonia\b": "アンモニア",
    r"\bsanctions?\b": "制裁",
    r"\bmiddle east\b": "中東",
    r"\brussia\b": "ロシア",
    r"\bchina\b": "中国",
    r"\bukraine\b": "ウクライナ",
    r"\battack\b": "攻撃",
    r"\bwar\b": "戦争",
    r"\btariffs?\b": "関税",
    r"\bshipping\b": "海運",
    r"\bsupply chain\b": "供給網",
    r"\brefinery\b": "製油所",
    r"\bgrid\b": "送電網",
    r"\bexports?\b": "輸出",
    r"\bimports?\b": "輸入",
    r"\brecession\b": "景気後退",
    r"\bgrowth\b": "成長",
    r"\bjobs?\b": "雇用",
}

def simple_ja(text: str) -> str:
    out = text
    for pattern, repl in sorted(TERM_MAP.items(), key=lambda x: len(x[0]), reverse=True):
        out = re.sub(pattern, repl, out, flags=re.IGNORECASE)
    return out


# =========================================================
# 前処理
# =========================================================
def is_japanese(text: str) -> bool:
    return bool(re.search(r"[ぁ-んァ-ン一-龥]", text))

def published_dt(entry):
    candidates = [
        getattr(entry, "published_parsed", None),
        getattr(entry, "updated_parsed", None),
        getattr(entry, "created_parsed", None),
    ]
    for pp in candidates:
        if pp:
            try:
                return datetime(*pp[:6], tzinfo=timezone.utc).astimezone(JST)
            except Exception:
                pass
    return None

def normalize_text(text: str) -> str:
    text = text.lower()
    text = re.sub(r"\([^)]*\)", " ", text)
    text = re.sub(r"【[^】]*】", " ", text)
    text = re.sub(r"\[[^\]]*\]", " ", text)
    text = re.sub(r"[^a-zA-Z0-9ぁ-んァ-ン一-龥\s]", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text

def dedup_key(title: str) -> str:
    return normalize_text(simple_ja(title))[:180]


# =========================================================
# トークン化
# =========================================================
STOP_EN = {
    "the", "a", "an", "and", "or", "to", "of", "in", "on", "for", "with", "as", "at", "by", "from",
    "after", "before", "amid", "over", "under", "into", "is", "are", "be", "will", "may", "could",
    "says", "said", "news", "update", "latest", "today", "market", "markets", "stock", "stocks",
    "shares", "bond", "bonds", "price", "prices", "report", "reports", "data"
}

STOP_JA = {
    "について", "など", "発表", "見通し", "速報", "更新", "記事", "写真", "関する", "受けて",
    "明らか", "可能性", "見方", "動き", "背景", "状況"
}

def tokenize_mix(text: str):
    text = simple_ja(text)
    text = normalize_text(text)
    en = re.findall(r"[a-z]{3,}", text)
    ja = re.findall(r"[ぁ-んァ-ン一-龥]{2,}", text)

    en = [w for w in en if w not in STOP_EN]
    ja = [w for w in ja if w not in STOP_JA]
    return en + ja


# =========================================================
# 類似判定
# =========================================================
def title_similarity(a: str, b: str) -> float:
    a1 = simple_ja(a)
    b1 = simple_ja(b)

    sa = set(tokenize_mix(a1))
    sb = set(tokenize_mix(b1))
    jaccard = len(sa & sb) / max(1, len(sa | sb))
    seq = SequenceMatcher(None, normalize_text(a1), normalize_text(b1)).ratio()

    return max(jaccard, seq * 0.7)


# =========================================================
# カテゴリ設計
# =========================================================
CATEGORIES = {
    "マクロ経済・中央銀行": [
        "fed", "ecb", "boj", "central bank", "rate", "inflation", "cpi", "ppi",
        "雇用", "景気", "成長", "金利", "インフレ", "日銀", "利下げ", "利上げ"
    ],
    "債券・為替": [
        "treasury", "yield", "dollar", "yen", "fx", "bond", "bonds",
        "米国債", "利回り", "ドル", "円", "為替", "債券"
    ],
    "地政学・安全保障": [
        "war", "attack", "sanction", "middle east", "russia", "ukraine", "china", "taiwan",
        "red sea", "tariff", "制裁", "中東", "ロシア", "ウクライナ", "中国", "台湾", "関税", "紅海"
    ],
    "原油・ガス・LNG": [
        "oil", "crude", "gas", "natural gas", "lng", "opec",
        "原油", "ガス", "天然ガス", "LNG", "OPEC"
    ],
    "電力・再エネ・原子力": [
        "power", "electricity", "renewable", "solar", "wind", "nuclear", "grid",
        "電力", "再生可能エネルギー", "太陽光", "風力", "原子力", "送電"
    ],
    "海運・供給網": [
        "shipping", "supply chain", "refinery", "export", "import",
        "海運", "供給網", "製油所", "輸出", "輸入"
    ],
    "株式・信用不安": [
        "stock", "equity", "bank", "credit", "default", "risk",
        "株", "銀行", "信用", "債務不履行", "リスク"
    ],
}

CATEGORY_WEIGHT = {
    "マクロ経済・中央銀行": 7,
    "債券・為替": 6,
    "地政学・安全保障": 7,
    "原油・ガス・LNG": 7,
    "電力・再エネ・原子力": 6,
    "海運・供給網": 5,
    "株式・信用不安": 3,
    "その他": 1,
}

BOOST_TERMS = {
    "金利": 4, "インフレ": 4, "日銀": 3, "雇用": 2, "景気": 2,
    "利下げ": 4, "利上げ": 4, "米国債": 4, "利回り": 3, "為替": 3,
    "中東": 4, "ロシア": 2, "中国": 4, "台湾": 3, "制裁": 3, "関税": 3,
    "原油": 3, "天然ガス": 4, "LNG": 5, "電力": 4, "原子力": 3, "再生可能エネルギー": 2,
    "海運": 3, "供給網": 3,
    "fed": 4, "ecb": 3, "boj": 3, "inflation": 4, "rate": 4,
    "treasury": 3, "yield": 3, "war": 2, "sanction": 3, "tariff": 3,
    "oil": 3, "crude": 3, "lng": 5, "gas": 3, "power": 3, "electricity": 3,
    "shipping": 3, "supply": 2
}

LOW_PRIORITY_HINTS = [
    "individual stock", "analyst says", "earnings beat", "shareholder",
    "個別株", "決算", "目標株価", "値動き", "上昇", "下落"
]

TOPIC_ORDER = [
    "マクロ経済・中央銀行",
    "債券・為替",
    "地政学・安全保障",
    "原油・ガス・LNG",
    "電力・再エネ・原子力",
    "海運・供給網",
    "株式・信用不安",
    "その他",
]

WATCH_TOPICS = {
    "金利・インフレ": ["金利", "インフレ", "利回り", "fed", "ecb", "boj", "rate", "inflation", "yield"],
    "中東": ["中東", "イラン", "イスラエル", "サウジ", "middle east"],
    "原油・LNG": ["原油", "天然ガス", "LNG", "oil", "crude", "gas", "lng", "opec"],
    "中国": ["中国", "china", "台湾", "taiwan"],
    "電力・再エネ・原子力": ["電力", "再生可能エネルギー", "原子力", "solar", "wind", "nuclear", "power", "electricity"],
}

def categorize(title: str) -> str:
    t = simple_ja(title).lower()
    for cat, keys in CATEGORIES.items():
        for k in keys:
            if k.lower() in t:
                return cat
    return "その他"

def recency_score(dt):
    if not dt:
        return 0
    hours = (NOW_JST - dt).total_seconds() / 3600
    if hours < 3:
        return 3
    if hours < 12:
        return 2
    if hours < 24:
        return 1
    if hours > 96:
        return -1
    return 0

def score_item(title: str, cat: str, dt, japanese: bool, source_weight: int, source_name: str) -> int:
    t = simple_ja(title).lower()
    s = CATEGORY_WEIGHT.get(cat, 1)

    if japanese:
        s += 4

    if source_name.startswith("Yahoo") and "ブルームバーグ" in title:
        s += 3

    if "EIA" in source_name:
        s += 2

    s += source_weight
    s += recency_score(dt)

    for term, w in BOOST_TERMS.items():
        if term.lower() in t:
            s += w

    for hint in LOW_PRIORITY_HINTS:
        if hint.lower() in t:
            s -= 2

    return s


# =========================================================
# 履歴保存
# =========================================================
def append_history(df):
    if df.empty:
        return

    if os.path.exists(HISTORY_FILE):
        try:
            old_df = pd.read_csv(HISTORY_FILE)
            combined = pd.concat([old_df, df], ignore_index=True)
            combined = combined.drop_duplicates(subset=["title", "link"])
        except Exception:
            combined = df.copy()
    else:
        combined = df.copy()

    combined.to_csv(HISTORY_FILE, index=False, encoding="utf-8-sig")


# =========================================================
# 今日の3行まとめ
# =========================================================
def build_daily_summary(items, all_items):
    if not items:
        return [
            "今日は十分な記事が取れてへん。",
            "『今日に絞る』を外すか、ソース設定を見直してや。",
            "まずは取得状況を確認してな。"
        ]

    cat_counter = Counter([it["cat"] for it in all_items[:20]])
    token_counter = Counter()
    for it in all_items[:30]:
        token_counter.update(tokenize_mix(it["title"]))

    lines = []

    if cat_counter["マクロ経済・中央銀行"] + cat_counter["債券・為替"] >= 3:
        if token_counter["インフレ"] or token_counter["金利"] or token_counter["利回り"]:
            lines.append("金利・物価・為替まわりの話が多く、世界のお金の流れを読む日や。")
        else:
            lines.append("中央銀行や景気の話が目立っていて、マクロの地合いを確認したい日や。")

    if cat_counter["地政学・安全保障"] >= 2:
        if token_counter["中東"] or token_counter["原油"]:
            lines.append("地政学の緊張が資源や物流に波及しやすく、中東とエネルギーを一緒に見たい日や。")
        else:
            lines.append("地政学リスクが意識されていて、市場心理より背景の構造を見たほうがええ日や。")

    if cat_counter["原油・ガス・LNG"] + cat_counter["電力・再エネ・原子力"] >= 3:
        if token_counter["LNG"] or token_counter["天然ガス"]:
            lines.append("エネルギーでは原油だけやなく、ガスやLNGの動きも押さえたい日や。")
        else:
            lines.append("エネルギー需給や電力の話が出ていて、供給構造の変化を追いたい日や。")

    if cat_counter["海運・供給網"] >= 2:
        lines.append("海運や供給網も出ていて、資源価格だけでなく運ぶ仕組みまで見たい日や。")

    if len(lines) < 3:
        lines.append("単発ニュースより、金利・地政学・エネルギーのつながりで見ると理解しやすい。")
    if len(lines) < 3:
        lines.append("米国・中国・中東のどこが動いているかを意識すると、大きな流れがつかみやすい。")
    if len(lines) < 3:
        lines.append("今日は『何が起きたか』より『何が波及するか』を意識して読むのがええ。")

    return lines[:3]


# =========================================================
# 今日の注目ポイント
# =========================================================
def build_key_points(items):
    if not items:
        return []

    points = []
    cat_counter = Counter([it["cat"] for it in items[:15]])
    token_counter = Counter()
    for it in items[:20]:
        token_counter.update(tokenize_mix(it["title"]))

    if cat_counter["マクロ経済・中央銀行"] or cat_counter["債券・為替"]:
        points.append("金利・為替・景気の流れを先に押さえる")

    if cat_counter["地政学・安全保障"]:
        points.append("地政学の緊張が資源と物流に波及していないか確認する")

    if cat_counter["原油・ガス・LNG"] or cat_counter["電力・再エネ・原子力"]:
        points.append("エネルギーは原油だけでなくLNG・電力まで見る")

    if token_counter["中国"] or token_counter["china"]:
        points.append("中国関連は景気・需要・地政学のどれかを切り分けて見る")

    if token_counter["中東"] or token_counter["middle"] or token_counter["原油"]:
        points.append("中東情勢は原油と海運の両方にどう波及するかで見る")

    return points[:3]


# =========================================================
# 取得
# =========================================================
@st.cache_data(ttl=900, show_spinner=False)
def fetch_feed(url: str):
    headers = {"User-Agent": USER_AGENT}
    try:
        res = requests.get(url, headers=headers, timeout=TIMEOUT)
        status = res.status_code
        res.raise_for_status()
        parsed = feedparser.parse(res.content)
        return parsed, status, None
    except Exception as e:
        return None, None, str(e)

def load_feed(source_def):
    feed, status_code, fetch_error = fetch_feed(source_def["url"])
    if fetch_error:
        return [], 0, True, fetch_error, status_code

    entries = getattr(feed, "entries", [])
    items = []

    for e in entries:
        title = getattr(e, "title", "").strip()
        link = getattr(e, "link", "").strip()
        if not title or not link:
            continue

        dt = published_dt(e)
        jp = is_japanese(title)
        cat = categorize(title)
        score = score_item(
            title=title,
            cat=cat,
            dt=dt,
            japanese=jp,
            source_weight=source_def["source_weight"],
            source_name=source_def["name"],
        )

        items.append({
            "source": source_def["name"],
            "kind": source_def["kind"],
            "title": title,
            "ja": title if jp else simple_ja(title),
            "link": link,
            "dt": dt,
            "cat": cat,
            "score": score,
            "japanese": jp,
        })

    bozo = bool(getattr(feed, "bozo", 0))
    bozo_ex = getattr(feed, "bozo_exception", None)
    return items, len(entries), bozo, bozo_ex, status_code


# =========================================================
# サイドバー
# =========================================================
st.sidebar.header("設定")

only_today = st.sidebar.checkbox("今日（JST）分だけに絞る", value=True)
top_n = st.sidebar.slider("重要本数", 3, 10, 5)
per_cat_limit = st.sidebar.slider("同カテゴリ上限", 1, 3, 2)
cluster_threshold = st.sidebar.slider("重複まとめ閾値", 0.30, 0.90, 0.45, 0.05)

enabled_source_names = st.sidebar.multiselect(
    "使うソース",
    options=[s["name"] for s in SOURCES],
    default=[s["name"] for s in SOURCES if s["enabled"]],
)

active_sources = [s for s in SOURCES if s["name"] in enabled_source_names]


# =========================================================
# 全取得
# =========================================================
all_items = []
status_rows = []

for src in active_sources:
    items, cnt, bozo, bozo_ex, status_code = load_feed(src)
    all_items.extend(items)
    status_rows.append({
        "source": src["name"],
        "count": cnt,
        "http_status": status_code,
        "bozo": bozo,
        "note": str(bozo_ex) if bozo_ex else "",
    })

status_df = pd.DataFrame(status_rows)

with st.expander("取得状況"):
    st.dataframe(status_df, width="stretch")

def in_today(it) -> bool:
    if not only_today:
        return True
    if it["dt"] is None:
        return True
    return it["dt"].date() == NOW_JST.date()

filtered = [it for it in all_items if in_today(it)]


# =========================================================
# 類似記事をまとめる
# =========================================================
def cluster_items(items, threshold=0.45):
    clusters = []

    sorted_items = sorted(
        items,
        key=lambda x: (
            x["score"],
            x["japanese"],
            x["dt"] or datetime(1970, 1, 1, tzinfo=JST),
        ),
        reverse=True,
    )

    for item in sorted_items:
        placed = False
        for cl in clusters:
            rep = cl["representative"]
            sim = title_similarity(item["title"], rep["title"])
            if sim >= threshold:
                cl["items"].append(item)

                if item["japanese"] and not rep["japanese"]:
                    cl["representative"] = item
                elif item["japanese"] == rep["japanese"] and item["score"] > rep["score"]:
                    cl["representative"] = item

                placed = True
                break

        if not placed:
            clusters.append({
                "representative": item,
                "items": [item],
            })

    return clusters

clusters = cluster_items(filtered, threshold=cluster_threshold)

unique = []
for cl in clusters:
    rep = cl["representative"]
    rep2 = rep.copy()
    rep2["dup_count"] = len(cl["items"])
    rep2["alt_titles"] = [x["title"] for x in cl["items"] if x["title"] != rep["title"]][:3]
    unique.append(rep2)

final_unique = []
seen = set()
for it in sorted(
    unique,
    key=lambda x: (
        x["score"],
        x["japanese"],
        x["dup_count"],
        x["dt"] or datetime(1970, 1, 1, tzinfo=JST),
    ),
    reverse=True,
):
    k = dedup_key(it["title"])
    if k in seen:
        continue
    final_unique.append(it)
    seen.add(k)


# =========================================================
# 重要本数抽出
# =========================================================
top_items = []
cat_count = defaultdict(int)

for it in final_unique:
    if cat_count[it["cat"]] >= per_cat_limit:
        continue
    top_items.append(it)
    cat_count[it["cat"]] += 1
    if len(top_items) >= top_n:
        break


# =========================================================
# 温度感
# =========================================================
topic_heat = defaultdict(int)
for it in final_unique[:40]:
    topic_heat[it["cat"]] += max(it["score"], 0)

heat_rows = []
for cat in TOPIC_ORDER:
    if cat not in topic_heat:
        continue
    score = topic_heat[cat]
    if score >= 30:
        level = "高"
    elif score >= 15:
        level = "中"
    else:
        level = "低"
    heat_rows.append({"テーマ": cat, "温度感": level, "点数": score})

heat_df = pd.DataFrame(heat_rows)


# =========================================================
# 見張り番テーマ
# =========================================================
def calc_watch_topic_score(items, keywords):
    score = 0
    matched = []
    for it in items[:50]:
        t = simple_ja(it["title"]).lower()
        hit = False
        for kw in keywords:
            if kw.lower() in t:
                score += max(it["score"], 1)
                hit = True
        if hit:
            matched.append(it["title"])
    return score, matched[:3]


# =========================================================
# 3行まとめ / 注目ポイント
# =========================================================
daily_summary = build_daily_summary(top_items, final_unique)
key_points = build_key_points(final_unique)


# =========================================================
# 表示：今日の3行まとめ
# =========================================================
st.subheader("今日の3行まとめ")
for i, line in enumerate(daily_summary, 1):
    st.write(f"{i}. {line}")

st.divider()


# =========================================================
# 表示：今日の注目ポイント
# =========================================================
st.subheader("今日の注目ポイント")
if not key_points:
    st.info("注目ポイントを作れるだけの記事がまだ足りへん。")
else:
    for i, p in enumerate(key_points, 1):
        st.write(f"{i}. {p}")

st.divider()


# =========================================================
# 表示：重要本数
# =========================================================
st.subheader(f"重要{top_n}本（大局把握向け）")

if not top_items:
    st.info("候補がありませんでした。『今日に絞る』をOFFにしてください。")
else:
    for i, it in enumerate(top_items, 1):
        dt_str = it["dt"].strftime("%Y-%m-%d %H:%M") if it["dt"] else "時刻不明"
        lang = "日本語" if it["japanese"] else "英語→簡易和訳"

        st.markdown(f"**{i}. [{it['title']}]({it['link']})**")
        st.caption(
            f"{dt_str} / {it['cat']} / {it['source']} / {lang} / score={it['score']} / 類似件数={it['dup_count']}"
        )
        st.write(f"→ {it['ja']}")

        if it["alt_titles"]:
            with st.expander("近い話題の別見出し"):
                for alt in it["alt_titles"]:
                    st.write(f"- {alt}")

st.divider()


# =========================================================
# 表示：テーマ温度感
# =========================================================
st.subheader("今日のテーマ温度感")
if heat_df.empty:
    st.info("温度感を出せるだけの記事がありません。")
else:
    st.dataframe(heat_df, width="stretch")


# =========================================================
# 表示：見張り番テーマ
# =========================================================
st.subheader("見張り番テーマ")
watch_rows = []

for topic, keywords in WATCH_TOPICS.items():
    score, matched_titles = calc_watch_topic_score(final_unique, keywords)

    if score >= 40:
        level = "高"
    elif score >= 18:
        level = "中"
    else:
        level = "低"

    watch_rows.append({
        "テーマ": topic,
        "注目度": level,
        "点数": score,
        "主な見出し": " / ".join(matched_titles) if matched_titles else ""
    })

watch_df = pd.DataFrame(watch_rows)
st.dataframe(watch_df, width="stretch")

st.divider()


# =========================================================
# 表示：カテゴリ別俯瞰
# =========================================================
st.subheader("カテゴリ別 上位3本（俯瞰）")

by_cat = defaultdict(list)
for it in final_unique:
    by_cat[it["cat"]].append(it)

for cat in TOPIC_ORDER:
    lst = by_cat.get(cat, [])
    if not lst:
        continue
    st.markdown(f"### {cat}")
    for it in lst[:3]:
        dt_str = it["dt"].strftime("%H:%M") if it["dt"] else "--:--"
        st.markdown(f"- [{it['title']}]({it['link']})  ({dt_str})")
        st.write(f"  → {it['ja']}")

st.divider()


# =========================================================
# 表示：頻出ワード
# =========================================================
st.subheader("頻出ワード Top10（見出しベース）")
cnt = Counter()
for t in [it["title"] for it in final_unique[:80]]:
    cnt.update(tokenize_mix(t))

top_terms = cnt.most_common(10)
if not top_terms:
    st.info("頻出ワードが抽出できませんでした。")
else:
    for w, c in top_terms:
        st.write(f"- {w} ({c})")

st.divider()


# =========================================================
# 保存用データ
# =========================================================
export_df = pd.DataFrame([
    {
        "source": it["source"],
        "datetime_jst": it["dt"].strftime("%Y-%m-%d %H:%M:%S") if it["dt"] else "",
        "category": it["cat"],
        "score": it["score"],
        "title": it["title"],
        "title_ja": it["ja"],
        "link": it["link"],
        "is_japanese": it["japanese"],
        "similar_count": it.get("dup_count", 1),
    }
    for it in final_unique
])

append_history(export_df)


# =========================================================
# 表示：過去7日ざっくり振り返り
# =========================================================
st.subheader("過去7日ざっくり振り返り")

if os.path.exists(HISTORY_FILE):
    try:
        hist_df = pd.read_csv(HISTORY_FILE)

        if "datetime_jst" in hist_df.columns and not hist_df.empty:
            hist_df["datetime_jst"] = pd.to_datetime(hist_df["datetime_jst"], errors="coerce")
            recent_df = hist_df.dropna(subset=["datetime_jst"]).copy()

            now_naive = datetime.now().replace(microsecond=0)
            cutoff = now_naive - pd.Timedelta(days=7)

            recent_df["dt_naive"] = pd.to_datetime(recent_df["datetime_jst"], errors="coerce")
            recent_df = recent_df[recent_df["dt_naive"] >= cutoff]

            if not recent_df.empty:
                trend_df = (
                    recent_df.groupby("category")
                    .size()
                    .reset_index(name="件数")
                    .sort_values("件数", ascending=False)
                )
                st.dataframe(trend_df, width="stretch")
            else:
                st.info("まだ過去データが少ない。何日か回したら傾向が見えてくるで。")
        else:
            st.info("履歴はあるけど、まだ集計できる形になってへん。")
    except Exception as e:
        st.warning(f"履歴集計でエラーが出た: {e}")
else:
    st.info("履歴ファイルはまだ無い。何回か実行したら蓄積されるで。")

st.divider()


# =========================================================
# 表示：今週よく出たテーマランキング
# =========================================================
st.subheader("今週よく出たテーマランキング")

if os.path.exists(HISTORY_FILE):
    try:
        hist_df = pd.read_csv(HISTORY_FILE)

        if "datetime_jst" in hist_df.columns and not hist_df.empty:
            hist_df["datetime_jst"] = pd.to_datetime(hist_df["datetime_jst"], errors="coerce")
            recent_df = hist_df.dropna(subset=["datetime_jst"]).copy()

            now_naive = datetime.now().replace(microsecond=0)
            cutoff = now_naive - pd.Timedelta(days=7)

            recent_df["dt_naive"] = pd.to_datetime(recent_df["datetime_jst"], errors="coerce")
            recent_df = recent_df[recent_df["dt_naive"] >= cutoff]

            if not recent_df.empty:
                theme_rank_df = (
                    recent_df.groupby("category")
                    .agg(
                        件数=("category", "size"),
                        平均スコア=("score", "mean")
                    )
                    .reset_index()
                    .sort_values(["件数", "平均スコア"], ascending=[False, False])
                )
                theme_rank_df["平均スコア"] = theme_rank_df["平均スコア"].round(1)
                st.dataframe(theme_rank_df, width="stretch")
            else:
                st.info("まだ今週分の履歴が少ない。")
        else:
            st.info("履歴データがまだ十分やない。")
    except Exception as e:
        st.warning(f"週次ランキングでエラーが出た: {e}")
else:
    st.info("履歴ファイルがまだ無い。")

st.divider()


# =========================================================
# 表示：一覧ダウンロード
# =========================================================
st.subheader("一覧ダウンロード")
st.dataframe(export_df.head(30), width="stretch")

csv_bytes = export_df.to_csv(index=False).encode("utf-8-sig")
st.download_button(
    label="CSVをダウンロード",
    data=csv_bytes,
    file_name=f"macro_energy_digest_{NOW_JST.strftime('%Y%m%d_%H%M')}.csv",
    mime="text/csv"
)
