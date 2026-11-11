// r99（2026-10-30）：**可考据的经历**与**她的尺子**。
//
// 为什么有这个文件（上一轮的结论）：她没有"我怎么看这件事"这一格，所以 86 轮里 67 轮只能"说我这一份"。
// 但补法不能是"写三观"——写成句子就会被念（r97 的「他好像有话没说完」40 张卡 → 7 轮台词就是这么来的）。
// 这个文件只做两件事：
//   ① 存**她本人在原典里做过/说过的**片段（逐字、带行号、带证据等级），一条 = 一句引文，不做解释；
//   ② 把"她的尺子"写成三条**判断维度**（K2/K3/K4），每条只挂 1 bit 的事实，不写"她认为…"。
//
// 证据等级：script（scenes-chiaki.tsv 原句）＞ asr（语音转写，可能听错）＞ second（DR3 二手转录）＞ 无出处（不收）。
// 这个文件里**只收 script**：宁可少，也不许拿二创当原典（仓库吃过这个亏）。
//
// 开关：`MIND_CANON=0` 时整个模块不参与（卡片与 r98 逐字一致）。
import { readFileSync } from 'node:fs';
// r106：感知外包的结果（LLM 判言语行为）。有就用它，没有就退回本文件的规则。
import { currentPercept } from './percept.mjs';

// [Shinsekai 移植] 只被 verifyQuotesAgainstCanon()（自检）读取，未随包带走语料。
const TABLE = process.env.MIND_CANON_TABLE ?? 'docs/canon-src/scenes-chiaki.tsv';

// ── 她本人说过的片段（逐字；src 里的行号是 scenes-chiaki.tsv 的物理行，表头算第 1 行）────
export const CANON_EPISODES = [
    {
        id: 'cow-touch',
        quote: '一旦摸了人就会产生某些东西吧？讨厌或是期待之类的……',
        src: 'scenes-chiaki.tsv:3945（e08_009_032，她摸完牛之后）',
        criterion: 'K3',
        cues: [/摸|抱|靠着|亲近|喜欢上|交往|告白|撒娇|依赖/u],
    },
    {
        id: 'bystander',
        quote: '尽量当一个旁观者就好。',
        src: 'scenes-chiaki.tsv:3947（e08_009_032）',
        criterion: 'K3',
        cues: [/插手|掺和|凑热闹|参与进去|管一管/u],
    },
    {
        id: 'cow-question',
        quote: '挤奶……是什么？',
        src: 'scenes-chiaki.tsv:3951（e08_009_032）',
        criterion: 'K4',
        cues: [/挤奶|牧场|农活|种地|牛奶|牛/u],
    },
    {
        id: 'together-happy',
        quote: '而是因为跟日向同学在一起，格外开心。',
        src: 'scenes-chiaki.tsv:2202（e09_709_004）',
        criterion: 'K2',
        cues: [/一个人|自己来|单独|陪我/u],
    },
    {
        id: 'together-watch',
        quote: '我一个人是绝对不会看，但如果跟人一起看，之后还能讨论各式各样的议题……',
        src: 'scenes-chiaki.tsv:3592（e09_709_003）',
        criterion: 'K2',
        cues: [/一起看|一起玩|一起做|一起想|讨论/u],
    },
];

// ── 她的尺子：三条判断维度（每轮最多命中一条；写成"问句"，不是结论）──────────────
export const CANON_CRITERIA = [
    {
        id: 'K2',
        // 依据：2202「因为跟日向同学在一起，格外开心」／3592「一个人是绝对不会看」
        cues: [/一个人|自己弄|自己来|单独|不用你|你别管|全交给我|我一个人/u],
        factOf: (text) => '他把这件事当成一个人的（她这一局从来是两个人的）',
    },
    {
        id: 'K3',
        // 依据：3945／3947——她怕的不是坏，是"讨厌或期待"那类东西，所以宁可旁观。
        cues: [/摸|抱|亲近|喜欢|交往|告白|撒娇|依赖|感动|舍不得|亲近/u],
        factOf: () => '这一步要她靠得很近（她怕的不是坏，是那类"多余的东西"）',
    },
    {
        id: 'K4',
        // 依据：3951——她真的不知道时，会先把词拿出来问，再猜一个自洽的错答案。
        cues: [/是什么|什么意思|怎么弄|什么叫|没听过/u],
        factOf: (text) => {
            const word = (/「([^」]{1,8})」/u.exec(text) ?? [])[1]
                ?? (/(?:什么|怎么)([\u4e00-\u9fa5]{1,6})/u.exec(text.replace(/是什么|什么意思|怎么弄|什么叫/gu, '')) ?? [])[1]
                ?? '';
            return word ? `他说的「${word}」，她是真的不知道是什么` : '他说的这个词，她是真的不知道是什么';
        },
    },
];

const MAX_QUOTE = 34;
const CANON_USED_MAX = 2;      // 同一段经历最多挂 2 次
const CANON_WINDOW = 20;       // 20 轮内算"最近"

// ── r104：欲望的"感知"这一环（不是措辞）────────────────────────────────────────────
// 病根（r104 诊断）：六道"有代价"的处境（他要一个人做／他替她做完／他问她想要什么）里，
// `want.kind` **全是 listen**，所以她的回复只能"先把话接住，再补自己那半"——缓冲是它的产物。
// 感知凭什么成立（都是剧本级，行号见 scenes-chiaki.tsv）：
//   · 他会把不该一个人做的事一个人做，而**她会要求自己那一份**：
//     L7834「这个我也会演奏，我可以参加吗？」／e04_007_091「那我可以说几句话吗？」／e05_102_105「那本文档……让我看一下。」
//   · 他会问她想要什么，而**她会直接说自己的打算**：
//     e08_009_019「我打算在作战开始以前都玩游戏，／要陪我吗？」／e08_009_013「我也没事做耶。／那我们一起想想打发时间的方法吧。」／e09_709_003「莫名开始有点想玩玩看了。」
//   · 为什么不放"要不要一起去"这类邀约：那已经由 `#13` 的意愿判定（去／不去）处理，两处都判会互相顶。
const STAKE_DONE_FOR_HER = /我(?:帮你|替你|给你)(?:打|做|弄|写|搞定|收|买|办|跑)(?:过了|了|好了|完)/u;
const STAKE_ALONE = /(?:你|你们)(?:就在旁边|在旁边|别管|不用管|不用你|不用帮|等着|看着|歇着)|我自己(?:来|弄|做|搞|搞定|可以|就行)|我一个人(?:就|也)?(?:行|可以|能)/u;
const ASK_WANT = /想(?:干|做|要)(?:嘛|什么)|想不想|有没有想(?:做|玩|打)|有什么想(?:做|玩|去)/u;

const STAKE_OFF = process.env.MIND_STAKE === '0';
const ASK_WANT_OFF = process.env.MIND_ASKWANT === '0';

/**
 * 他这一句是不是"把那件事一个人拿走了"（他一个人做／他替她做完）。
 * 返回 `{ id, motive, about }` 或 null——只判处境，不给说法。
 */
export function stakeOf(state) {
    if (STAKE_OFF) {
        return null;
    }
    // r106b：**OR 融合**——漏判的代价是"她要的东西没开口"，比误判一次更贵。
    const seen = currentPercept();
    const byPercept = seen ? seen.stake === true : false;
    const text = String(state?.lastInboundText ?? '').trim();
    if (!text) {
        return null;
    }
    if (state?.perspective?.target === '在说别人') {
        return null;
    }
    const byRule = STAKE_DONE_FOR_HER.test(text) || STAKE_ALONE.test(text);
    if (!byPercept && !byRule) {
        return null;
    }
    if (STAKE_DONE_FOR_HER.test(text)) {
        return { id: 'K2', motive: '那件事本来是我要做的', about: '他替我做掉的那件事', from: byPercept ? 'percept' : 'rule' };
    }
    return { id: 'K2', motive: '那件事里我那一半还没给我', about: '他要一个人弄完的那件事', from: byPercept ? 'percept' : 'rule' };
}

/** 他这一句是不是在问她"你想要什么"。邀约类（要不要一起去）交给 #13，不在这里抢。 */
export function asksWhatSheWants(state) {
    if (ASK_WANT_OFF) {
        return false;
    }
    const seen = currentPercept();
    if (seen && seen.asksWish === true) {
        return true;
    }
    const text = String(state?.lastInboundText ?? '').trim();
    if (!text || !ASK_WANT.test(text)) {
        return false;
    }
    if (/去|到|回|往|走到/u.test(text) && /要不要|想不想/u.test(text)) {
        return false;
    }
    return true;
}

// ── r105：**他在请她做一件事**（不是"他在讲他自己的事"）────────────────────────────
// 病根：`newsOf()` 只挡了空串／应声／重复三遍，于是"帮我把这个透视表做出来"也被判成"他的新闻"
// → `list` 分支 → 她把话接住（这是"AI 替她说"的最后一处垃圾桶）。
// 原典里她对"要她做事"的反应是**先把东西问清楚**，猜也只猜"像什么"：
//   e05_102_105「那本文档……让我看一下。」（先要过来看，不先答）
//   L3951–3958 挤奶段：他把过程讲一遍，她一路问「获得是……什么意思？」「不断获得……？」
const ASK_HER_DO = /帮我(?:把|看|弄|做|写|找|拆|查)|给我(?:讲|说|弄|找|看|写)|麻烦你|你来(?:弄|做|写|看)|教我|带我|陪我去|能不能帮|可以帮/u;
const REQUEST_OFF = process.env.MIND_REQUEST === '0';

/** 他在请她做一件事 → 她要先说清是哪一样（返回 {id, motive, about} 或 null）。 */
export function requestOf(state) {
    if (REQUEST_OFF) {
        return null;
    }
    // r106b：OR 融合（规则认"帮我/给我"，感知认转述式请求）。
    const seen = currentPercept();
    const text = String(state?.lastInboundText ?? '').trim();
    if (!text) {
        return null;
    }
    const byPercept = seen ? seen.request === true : false;
    if (!byPercept && !ASK_HER_DO.test(text)) {
        return null;
    }
    const body = text.replace(/^\s*[^\s：:<>]{1,16}(?:<\d+>)?\s*[：:]\s*/u, '').trim();
    const cut = body.length > 22 ? body.slice(0, 22) + '…' : body;
    return { id: 'R1', motive: '先问清他要的是哪一样', about: cut, from: byPercept ? 'percept' : 'rule' };
}

// ── r106：**他讲他自己的事，而这件事跟她有交集** → 共鸣（她会递自己那一小份）─────────────
// 病根（r105 的普查）：他讲他的事一律落 `listen`（27/86），但原典里她常常**接一段自己的**：
//   e08_009_013「我也没事做耶。／那我们一起想想打发时间的方法吧。」（他说没事做 → 她也说自己没事做）
//   e08_009_032 他说牧场 → 她接自己的游戏联想（「像 Dancing Eyes 一样」）
//   L2202「而是因为跟日向同学在一起，格外开心。」
// 心理依据：**自我揭露的互惠**（Altman & Taylor 的社会渗透；Reis & Shaver 的 responsiveness）——
// 他给出一点自己的，她也给一点自己的，关系才是双向的。但**不是抢话题**：她给的是"一小段"。
// 与「他也没劲了（co-regulation）」的分工：**能量低 → 降要求地陪**（那条 appraisal 先说话）；
// 有交集的经验／兴趣 → 共鸣（这一支）。
const SHARED_WITH_HER = /(?:游戏|那一关|关卡|存档|手柄|打不过|打通|通关|像素|街机|boss|猫|熬夜|失眠|睡|眯|咖啡|作业|考试|社团|朋友|喜欢|讨厌|怕|不敢|难受|烦|累)/u;
const SELF_DISCLOSE = /我[^。！？]{0,16}(?:累|困|难受|烦|开心|高兴|怕|不敢|讨厌|喜欢|打不过|卡住|卡在|加班|考试|失眠|熬夜|刚开始|一直)/u;
const RESONANCE_OFF = process.env.MIND_RESONANCE === '0';

/** 他讲自己的事、且跟她有交集 → 递自己那一小份（返回 {id, motive, about} 或 null）。 */
export function resonanceOf(state) {
    if (RESONANCE_OFF) {
        return null;
    }
    // r106b：**AND 融合**——共鸣误判的代价是"抢话题"（她讲起自己的事，把他的话盖过去），
    // 所以要求感知与规则都成立才给 speak；只有一边成立时她只是听（listen）。
    const seen = currentPercept();
    const byPercept = seen ? seen.resonance === true : false;
    const text = String(state?.lastInboundText ?? '').trim();
    if (!text || !SELF_DISCLOSE.test(text) || !SHARED_WITH_HER.test(text)) {
        return null;
    }
    if (seen && !byPercept) {
        return null;
    }
    const cut = text.length > 20 ? text.slice(0, 20) + '…' : text;
    return { id: 'R2', motive: '我这边也有一点像的，说一小段', about: cut, from: byPercept ? 'percept+rule' : 'rule' };
}




function trim(quote) {
    // 原典里 `／` 是视觉小说的文字框边界（不是停顿）。聊天场域里直接去掉，
    // 换成逗号会造出「吧？，讨厌」这种双标点。逐字核对也按同一口径（verifyQuotesAgainstCanon 用同一个函数）。
    const clean = String(quote ?? '').replace(/\s*／\s*/gu, '');
    return clean.length > MAX_QUOTE ? `${clean.slice(0, MAX_QUOTE)}…` : clean;
}

function usedOf(state) {
    const map = state?.canonUsed;
    return map && typeof map === 'object' ? map : {};
}

// 这一段他这句话点到了哪几条经历（按 cues 命中，最多 1 条：取"最近没用过"的那条）
function episodeHit(text, state, round) {
    const used = usedOf(state);
    const live = CANON_EPISODES
        .filter((one) => one.cues.some((re) => re.test(text)))
        .filter((one) => {
            const record = used[one.id];
            if (!record) {
                return true;
            }
            if (round - Number(record.at ?? 0) > CANON_WINDOW) {
                return true;
            }
            return Number(record.n ?? 0) < CANON_USED_MAX;
        });
    return live[0] ?? null;
}

function criterionHit(text) {
    return CANON_CRITERIA.find((one) => one.cues.some((re) => re.test(text))) ?? null;
}

/**
 * 这一轮卡片上多出来的一行（0 或 1 行）。
 * 优先"她自己的经历"，其次"她的尺子量到的那件事"——**一行都不写"她认为"**。
 */
export function canonLineOf(state) {
    const text = String(state?.lastInboundText ?? '').trim();
    if (!text) {
        return '';
    }
    const round = Number(state?.rounds ?? 0);
    const episode = episodeHit(text, state, round);
    if (episode) {
        return `- 我记得我说过：「${trim(episode.quote)}」`;
    }
    const criterion = criterionHit(text);
    if (!criterion) {
        return '';
    }
    return `- 这件事：${criterion.factOf(text)}`;
}

/** 卡片真的写了哪一条（给 --her 回写计数用；返回 {kind,id} 或 null） */
export function canonTagOf(state) {
    const text = String(state?.lastInboundText ?? '').trim();
    if (!text) {
        return null;
    }
    const round = Number(state?.rounds ?? 0);
    const episode = episodeHit(text, state, round);
    if (episode) {
        return { kind: 'episode', id: episode.id };
    }
    const criterion = criterionHit(text);
    return criterion ? { kind: 'criterion', id: criterion.id } : null;
}

/** 记一次"用过了"（--her 时调；只记账，不改内容） */
export function noteCanonShown(state, tag) {
    if (!tag) {
        return state;
    }
    const round = Number(state?.rounds ?? 0);
    const used = { ...usedOf(state) };
    const before = used[tag.id] ?? { n: 0, at: round };
    used[tag.id] = { n: Number(before.n ?? 0) + 1, at: round };
    state.canonUsed = used;
    return state;
}

// ── 自检用：确认引文**逐字**在原典文件里（防止有人手打了近似句）──────────────
export function verifyQuotesAgainstCanon() {
    let rows = [];
    try {
        rows = readFileSync(TABLE, 'utf8')
            .split('\n')
            .slice(1)
            .filter(Boolean)
            .map((line) => line.replace(/\r$/u, '').split('\t'));
    }
    catch {
        return { ok: false, reason: '原典文件读不到', checked: 0 };
    }
    const herLines = rows.filter((row) => row[3] === '七海').map((row) => String(row[4] ?? '').replace(/\s*／\s*/gu, ''));
    const bad = [];
    for (const one of CANON_EPISODES) {
        const quote = trim(one.quote);
        const body = quote.replace(/…$/u, '');
        const found = herLines.some((line) => line.includes(body));
        if (!found) {
            bad.push(`${one.id}：「${body}」`);
        }
    }
    return { ok: bad.length === 0, bad, checked: CANON_EPISODES.length };
}
