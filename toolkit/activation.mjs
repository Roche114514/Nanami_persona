// r82（R1 / psych 规格 §1 的 A）：线索激活 `activationOf(线索) → 0..1`
//
// 一句话：账本和记忆不是"常驻在卡片上"，而是**被线索点亮的那一轮才进评估**。
// 点亮与否由"线索特征是否与编码时的特征匹配"决定（编码特异性），匹配不上就是 0，不许有底噪。
//
// 为什么单独一个模块：它是纯函数（吃 fact、吐分数），不读盘、不 import mind.mjs，
// 可以脱离 CLI 单测；`--his` / `--idle` / 群出口都能复用同一份判据。
//
// 三件它**不做**的事（每一条都有出处）：
//   1. 不落盘激活值（`persona-psyche-model-v2.md` §15.4）：落盘下一轮就会读到上一轮的值 =
//      它变成常驻量 = 把她改成焦虑型依恋。激活值每轮现算，只进 --json 与卡片那一行。
//   2. 不判"我讲过什么"（那是 `stories` 的事，psych §11.4 第 1 条）：两边**共用** mind.mjs 算好的
//      `gistOf`／`semanticOf` 结果，各算各的，不许互相调用。
//   3. 不写"该怎么说"：卡片那一行只写"哪件事被勾起来了"这个事实。
//
// 文献与常数出处见 docs/r82-psych-spec.md §1 ①（Tulving & Thomson 1973 编码特异性／
// Collins & Loftus 1975 扩散激活／Anderson 1974 扇形效应／Bower 1981 心境一致回忆／
// Groves & Thompson 1970 习惯化）。

// 两个阈值（psych §1 ② 的"记录线／出口线"）
export const ACTIVATION_RECORD = 0.45;   // ≥ 0.45 进 --json（replay/标定用），不出卡片、不改行为
export const ACTIVATION_EXIT = 0.65;     // ≥ 0.65 才有出口：卡片那一行 + appraisal 那一格
// 2026-10-26 标定：**光靠时间线不许点亮**（到期／逾期单独 0.80 就够 0.65，那等于"账一直在响"——
// 只要账是逾期的，他随便说句天气都会亮，正是 persona-psyche-model-v2.md §15.4 禁止的常驻权重，
// 也是 Lead 验收里"有账但没被点亮"那条反例断言测不出来的原因）。
// 所以出口比记录线多一个条件：这一轮**至少有一条内容线索**（语义键／字面／空间）对得上；
// 时间线只负责加响，不负责点亮。同一套自洽性 psych §1 也用过："空间单独 0.55 < 0.65 点亮不了"。
const CONTENT_KINDS = new Set(['semantic', 'literal', 'place', 'placeKind']);

export function activationFires(hit) {
    if (!hit || Number(hit.value ?? 0) < ACTIVATION_EXIT) {
        return false;
    }
    return hit.content === true;
}
// 特征分（0–1，单条线索）。数值与理由：
//   · 语义键完全相同 0.75 —— 这就是"同一个线索"本身，必须能单独点亮（0.75 > 0.65）
//   · 字面 Dice 0.75×D —— 同义改写也算，按重合比例打折
//   · 空间完全相同 0.55 / 同类 0.20 —— 场景只是上下文，**单独不能点亮**（防"到哪想起哪"）
//   · 时间：今天到期 0.80 / 已逾期 0.80→封顶 0.85 / 明天到期 0.60
const FEATURE = {
    semantic: 0.75,
    literalMany: 0.75,   // 共享 ≥2 个内容二元组：这就是"同一件事"被说到了
    literalOne: 0.65,    // 只共享 1 个、但那个词是**具体的东西**（拼图／存档／星露谷）→ 一根线索就够
    literalWeak: 0.55,   // 只共享 1 个泛用词（游戏／同学／知道）→ 和"空间"同档，单独点不亮
    place: 0.55,
    placeKind: 0.20,
    dueToday: 0.80,
    dueTomorrow: 0.60,
    overdueBase: 0.80,
    overdueCap: 0.85,
    overdueStepDays: 3,
    overdueStep: 0.05,
};
// 组合里次高线索的权重（扩散激活：多个来源叠加但边际递减）
const SECOND_WEIGHT = 0.5;
// 习惯化（防"焦虑型依恋"的那个闸）：同一件事连响 3 轮 → 强制静默 5 轮
export const ACTIVATION_STREAK_LIMIT = 3;
export const ACTIVATION_SILENT_ROUNDS = 5;
// 提过就放下：8 轮内她提过这条 → 0.25
const MENTION_DECAY = 0.25;
const MENTION_WINDOW = 8;
// 编码到现在的轮次衰减（只有"无到期日"的记忆条走这条）
const AGE_HALF = 12;

function clamp01(value) {
    if (!Number.isFinite(value)) {
        return 0;
    }
    return Math.min(1, Math.max(0, value));
}

function dayStart(ms) {
    const date = new Date(Number(ms) || 0);
    date.setHours(0, 0, 0, 0);
    return date.getTime();
}

// 两组内容二元组共享哪些（grams 由 mind.mjs 注入，和 stories 用的是同一份 contentGrams）
function sharedGramList(left, right) {
    const a = Array.isArray(left) ? left : [];
    const b = Array.isArray(right) ? right : [];
    const out = [];
    for (const one of a) {
        if (b.includes(one) && !out.includes(one)) {
            out.push(one);
        }
    }
    return out;
}

// 这条记忆的"时间线索"分：到期/逾期是**最强的线索**（到期的账自己会浮上来）
function timeFeature(item, now) {
    const due = Number(item?.dueAt ?? 0);
    if (!(due > 0)) {
        return 0;
    }
    const today = dayStart(now);
    const dueDay = dayStart(due);
    if (dueDay > today && dueDay <= today + 86400000) {
        return FEATURE.dueTomorrow;
    }
    if (dueDay >= today) {
        return FEATURE.dueToday;
    }
    // 已逾期：欠着的事只会更响，不会自己消失——但封顶 0.85，不许饱和到 1.0
    const days = Math.floor((today - dueDay) / 86400000);
    const bump = FEATURE.overdueStep * Math.floor(days / FEATURE.overdueStepDays);
    return Math.min(FEATURE.overdueCap, FEATURE.overdueBase + bump);
}

// 心境一致回忆：编码时与现在同号且差得不多 → 1.15；反号 → 0.85；其余 1.0
// 幅度刻意压小（非临床人群里这个效应不对称且不大，见 psych §1 ①）
function moodFactor(itemMood, nowMood) {
    const a = Number(itemMood);
    const b = Number(nowMood);
    if (!Number.isFinite(a) || !Number.isFinite(b)) {
        return 1;
    }
    if (Math.abs(a - b) < 0.3 && a !== 0 && b !== 0 && (a > 0) === (b > 0)) {
        return 1.15;
    }
    if ((a > 0) !== (b > 0)) {
        return 0.85;
    }
    return 1;
}

/**
 * 算一轮的激活值。纯函数：不读盘、不改入参。
 *
 * @param {{
 *   items: object[],          // 候选记忆条：账（owed）／autobio。每条自带 text/semanticKey/place/placeKind/dueAt/round/moodV/mentionedAt
 *   signals: {                // 这一轮的事实（由 mind.mjs 算一次，两边共用）
 *     text?: string,
 *     gist?: string,
 *     semanticKeys?: string[],
 *     place?: string,
 *     placeKind?: string,
 *     moodV?: number,
 *     rounds?: number,
 *   },
 *   dice?: (a: string, b: string) => number,   // 注入：与 sameStory 同一口径的 Dice
 *   seen?: object,            // 习惯化记账 { [id]: { streak, until } }（落盘的只有它，不是激活值）
 *   now?: number,
 * }} input
 */
export function activationOf(input) {
    const safe = input ?? {};
    const signals = safe.signals ?? {};
    const items = Array.isArray(safe.items) ? safe.items : [];
    const dice = typeof safe.dice === 'function' ? safe.dice : () => 0;
    const seen = (safe.seen && typeof safe.seen === 'object') ? safe.seen : {};
    const gramsOf = typeof safe.grams === 'function' ? safe.grams : () => [];
    const isGeneric = typeof safe.isGeneric === 'function' ? safe.isGeneric : () => false;
    const now = Number(safe.now ?? Date.now());
    const rounds = Number(signals.rounds ?? 0);
    const text = String(signals.text ?? '');
    if (!items.length || !text.trim()) {
        return { value: 0, item: null, cue: '', features: [] };
    }

    // ① 每条候选记忆取它自己的孩子线索分（max，不是加权平均：一根对得上的线索就够）
    const scored = [];
    for (const item of items) {
        const features = [];
        const keys = Array.isArray(signals.semanticKeys) ? signals.semanticKeys : [];
        if (item?.semanticKey && keys.includes(item.semanticKey)) {
            features.push({ kind: 'semantic', score: FEATURE.semantic });
        }
        // 字面线索：用**共享内容二元组的个数**分档，不用 Dice。
        // 为什么不用 Dice（psych §1 写的是 0.75×Dice）：Dice 对"一边短一边长"极不友好，
        // 而这条链的两边天然不等长（账是短语、他这句是一整句）。实测「你之前说的那个一千片的拼图，
        // 还差几块？」vs 账「拼图少一块」共享「拼图」，Dice 只有 0.17 → 乘 0.75 = 0.13，
        // 永远点不亮；而"提到同一个东西"这件事在语义上就是同一根线索。
        // 分档与 psych 自己的自洽性检查对齐：一根线索 0.55（和"空间单独"同档，单独点不亮），
        // 两根及以上 0.75（和"语义键完全相同"同档，能单独点亮）。
        const sharedList = sharedGramList(gramsOf(item?.text), gramsOf(text));
        // r88：**短短语的字面包含**（实测 bug）。
        // `contentGrams` 把「会」「一」「说」这些当虚词滤掉，于是账「一会儿」一个二元组都没有，
        // 他就算一字不差地说「你说的一会儿是多久啊」，这条账也永远点不亮（实测 0 线索）。
        // 判据只有一条：这条记忆的原文（2–6 字）**整段出现在他这句话里** → 算一根具体线索。
        // 不做模糊、不做同义——"他说的就是这件事"本来就该是字面那件事。
        const shortCue = String(item?.text ?? '').trim();
        const hitShort = sharedList.length === 0
            && shortCue.length >= 2 && shortCue.length <= 6
            && text.includes(shortCue);
        if (hitShort) {
            features.push({ kind: 'literal', score: FEATURE.literalOne });
        }
        else if (sharedList.length >= 2) {
            features.push({ kind: 'literal', score: FEATURE.literalMany });
        }
        else if (sharedList.length === 1) {
            // 一根线索够不够用，看它是不是**具体的东西**：泛用词只当弱线索
            const substantive = !isGeneric(sharedList[0]);
            features.push({ kind: 'literal', score: substantive ? FEATURE.literalOne : FEATURE.literalWeak });
        }
        const place = String(signals.place ?? '');
        if (item?.place && place && item.place === place) {
            features.push({ kind: 'place', score: FEATURE.place });
        }
        else if (item?.placeKind && signals.placeKind && item.placeKind === signals.placeKind) {
            features.push({ kind: 'placeKind', score: FEATURE.placeKind });
        }
        const time = timeFeature(item, now);
        if (time > 0) {
            features.push({ kind: 'time', score: time });
        }
        features.sort((left, right) => right.score - left.score);
        const top = features[0]?.score ?? 0;
        const second = features[1]?.score ?? 0;
        scored.push({ item, features, top, second, base: clamp01(top + SECOND_WEIGHT * second) });
    }

    // ② 扇形效应：同一个最高线索命中 ≥3 条 → 每条 ×0.7
    const cueCount = new Map();
    for (const one of scored) {
        const cue = one.features[0]?.kind ?? '';
        cueCount.set(cue, (cueCount.get(cue) ?? 0) + 1);
    }

    // ③ 组合：base × 心境 × 年龄 × 提过就放下 × 扇形
    let best = null;
    let acted = null;
    for (const one of scored) {
        const item = one.item ?? {};
        const cue = one.features[0]?.kind ?? '';
        const fan = (cueCount.get(cue) ?? 0) >= 3 ? 0.7 : 1;
        const noDue = !(Number(item.dueAt ?? 0) > 0);
        const age = noDue ? Math.exp(-Math.max(0, rounds - Number(item.round ?? rounds)) / AGE_HALF) : 1;
        const mentionedAt = Number(item.mentionedAt ?? -1);
        const mentioned = mentionedAt >= 0 && rounds - mentionedAt <= MENTION_WINDOW;
        const mention = mentioned ? MENTION_DECAY : 1;
        const mood = moodFactor(item.moodV, signals.moodV);
        let value = clamp01(one.base * mood * age * mention * fan);
        // ④ 习惯化闸：这条最近连响过 3 轮 → 静默 5 轮（不是禁止，是"别一直提"）
        const book = seen[String(item.id ?? '')] ?? null;
        const until = Number(book?.until ?? -1);
        const silenced = until >= rounds;
        if (silenced) {
            value = 0;
        }
        // 内容线索有没有对得上（时间线不算）：出口的第二个条件
        const content = one.features.some((feature) => CONTENT_KINDS.has(feature.kind));
        const record = { value, content, item, cue, features: one.features, silenced };
        if (!best || record.value > best.value) {
            best = record;
        }
        // **出场的那一条**只在"有内容线索"的候选里挑：时间线单独不许点亮（见文件头标定说明）。
        // 不这么分的话，一条逾期的账会永远以 0.80 压过"他这句真正提到的那件事"。
        if (content && (!acted || record.value > acted.value)) {
            acted = record;
        }
    }

    if (!best) {
        return { value: 0, content: false, item: null, cue: '', features: [], recordValue: 0 };
    }
    const pick = acted ?? best;
    return { ...pick, recordValue: best.value };
}

// 卡片那一行（**只写事实**：哪件事被勾起来了。不写次数、不写"我该怎么提"）
export function activationLineOf(hit) {
    const label = String(hit?.item?.what ?? hit?.item?.text ?? '').trim();
    if (!label) {
        return '';
    }
    return `- 他这句勾起来的是：${label.slice(0, 30)}`;
}

// 习惯化记账：把这一轮的结果记进 seen（返回新的 seen，不改入参）
export function noteActivation(seen, hit, rounds) {
    const id = String(hit?.item?.id ?? '');
    if (!id) {
        return seen ?? {};
    }
    const book = (seen && typeof seen === 'object') ? { ...seen } : {};
    const current = (book[id] && typeof book[id] === 'object') ? book[id] : { streak: 0, until: -1 };
    const fired = Number(hit?.value ?? 0) >= ACTIVATION_RECORD;
    const streak = fired ? Number(current.streak ?? 0) + 1 : 0;
    const until = streak >= ACTIVATION_STREAK_LIMIT ? Number(rounds) + ACTIVATION_SILENT_ROUNDS : Number(current.until ?? -1);
    book[id] = { streak, until };
    // 只留最近 8 条，别让这张记账表长成第二个记忆库
    const keys = Object.keys(book);
    if (keys.length > 8) {
        for (const one of keys.slice(0, keys.length - 8)) {
            delete book[one];
        }
    }
    return book;
}
