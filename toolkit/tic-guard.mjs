// r107：**去掉"AI 味"的守门员**——纯字面规则，不过模型。
//
// 为什么不交给本地 3B（量过了）：同样 20 条手写卷，3B 判"有没有这些填充"只有 **40%**，
// 干净句被误伤 7/9（它把「我在图书馆」「这关我重打」都标成"我这边"那一类）。
// 而这六种味道恰好都是**字面模式**：正则 100% 抓得到，改动也小、可解释、可回滚。
//
// 范围（只动这些，别扩散）：
//   dash        破折号「——」→ 逗号（她的标点是「……」，不是破折号）
//   tail_zai    句尾孤零零的「我在／我在的」→ 删（带地点的「我在图书馆」不动）
//   side        「我这边／我那边」→「我」（「我在这边」不匹配，不动）
//   blank_ack   「这个我认。」→ 删（有具体所指的「我承认刚才是我说的」不动）
//   not_but     「不是…而是…」→ 去掉「而是」，改成逗号
//   filler_truth 句首「其实／说实话／老实说／说真的」→ 删
//
// 两条硬纪律：
//   ① **代码块、路径、命令一律不碰**（那是她在干活，不是在说话）；
//   ② 改完再清一遍多余标点，**不许把句子改出别的问题**（宁可少改一条）。
import { hitOffVoice } from './off-voice.mjs';

export const TIC_ORDER = ['dash', 'wu_to_pause', 'off_voice', 'gou_eval', 'tail_zai', 'side', 'blank_ack', 'not_but', 'filler_truth'];

const LOOKS_LIKE_WORK = /```|\bfile:\/\/|^[A-Za-z]:[\\/]|node |git |npm |pnpm |schtasks|powershell|curl /u;

/** 清一遍多余标点（连着两个逗号、逗号前有句号之类） */
function tidy(text) {
    return String(text)
        .replace(/[，,]{2,}/gu, '，')
        .replace(/[。！？…]\s*[，,]/gu, (m) => m.trim().slice(0, 1))
        .replace(/[，,]\s*[。！？]/gu, (m) => m.trim().slice(-1))
        .replace(/^[，,、\s]+/u, '')
        .replace(/\s{2,}/gu, ' ')
        .trim();
}

/** 句尾孤零零的「我在／我在的」：只删"没有信息量"的那一截 */
function dropTailZai(text) {
    const hit = /([。！？…])\s*(?:嗯[，,。]?\s*)?我在(?:的|啊|呢)?\s*[。！？…]?$/u.exec(text);
    if (!hit) {
        return null;
    }
    const head = text.slice(0, hit.index + 1).trim();
    // 删完只剩一句"嗯。"这种没内容的，就整句保留（她本来就只说一个词的时候）
    return head === '' ? null : head;
}

// 「…够…」式的评估分句（"这儿沙发够长""时间够用""声音够清楚"）——AI 中文的典型句式，原典 0 次。
// 只处理**短小的纯评估分句**：长度 ≤10 字、整句就是"某物＋够＋单字形容词"，删掉它不影响别的话。
const GOU_CLAUSE = /^(?:这儿|那里|这里|那边|这|那)?[^，。！？；\s]{0,8}?够(?!了|不|呛)(?:安静|清楚|明白|舒服|好玩|长|宽|高|大|小|多|远|近|早|晚|快|慢|轻|重|难|易|深|浅|亮|暗|冷|暖|用)的?$/u;

function dropGouClause(text) {
    const parts = String(text).split(/(?<=[，。！？；])/u);
    const kept = [];
    let dropped = false;
    for (const raw of parts) {
        const core = raw.replace(/[，。！？；\s]+$/u, '').trim();
        if (!dropped && GOU_CLAUSE.test(core)) {
            dropped = true;
            continue;
        }
        kept.push(raw);
    }
    return dropped ? kept.join('') : null;
}

// 「唔」：原典汉化里只出现 4 次（0.2%），我们的回复里 23.5%。它的功能是"卡壳/停顿"，
// 而她的停顿本来就是「……」——所以统一并过去。只并这一种填充音，不做用词映射（词频拟合已撤）。
function tameInterjections(text) {
    let out = String(text);
    let touched = false;
    if (/唔/u.test(out)) {
        // 句首的「唔……」直接删掉（不然一句话会以「……」开头，切气泡时会被并成一条）；
        // 句中的并成「……」（她的停顿本来就是这个）。
        out = out.replace(/^\s*唔\s*(?:…+|。{3,})/u, '')
            .replace(/^\s*唔[，,]?\s*/u, '')
            .replace(/唔\s*(?:…+|。{3,})/gu, '……')
            .replace(/唔[，,]?\s*/gu, '……');
        // 换个位置别留下「…………」这种双省略号
        out = out.replace(/(?:……)+/gu, '……').replace(/……[，,]/gu, '……').replace(/……！/gu, '……');
        touched = true;
    }
    return touched ? out : null;
}

// 「点评/记账腔」：对"这句话/这个账"作评价、记账、结算的分句（主人清单，见 state/off-voice.json）。
// 实测：原典 2021 行误伤 0.00%；3B 判这一族只有 53%（模型没有"她的声音"这个先验）。
function dropOffVoiceClauses(text) {
    const source = String(text ?? '');
    if (!source.trim()) {
        return null;
    }
    const parts = source.split(/(?<=[。！？；，,])/u);
    const kept = [];
    let dropped = false;
    for (const raw of parts) {
        const core = raw.replace(/[。！？；，,\s]+$/u, '').trim();
        if (core && hitOffVoice(core).hit) {
            dropped = true;
            continue;
        }
        kept.push(raw);
    }
    if (!dropped) {
        return null;
    }
    let joined = kept.join('').trim();
    if (!/[\u4e00-\u9fa5A-Za-z0-9]/u.test(joined)) {
        return null;
    }
    // 删完分句可能留下尾逗号（「这关我打过了，」）——收成句号
    joined = joined.replace(/[，,、]\s*$/u, '。');
    return joined;
}

/** 「我这边／我那边」当填充 → 「我」。带"在这边"的（我在这边守着）不动 */
function flattenSide(text) {
    return text.replace(/我(?<![在就])这边|我那边/gu, '我');
}

/** 「不是A，而是B」→「不是A，B」 */
function flattenNotBut(text) {
    return text.replace(/(不是[^。！？]{0,14}?)[，,]?\s*而是\s*/u, '$1，');
}

export function hasWork(text) {
    return LOOKS_LIKE_WORK.test(String(text ?? ''));
}

/**
 * 清一遍。返回 `{ text, tics }`——`tics` 是这次真的改到的规则名（空数组 = 原样）。
 * **纯函数**，不改入参。
 */
export function cleanTics(input) {
    const original = String(input ?? '');
    if (original.trim() === '' || hasWork(original)) {
        return { text: original, tics: [] };
    }
    let text = original;
    const tics = [];

    if (/——|—/u.test(text)) {
        text = text.replace(/——+/gu, '，').replace(/—/gu, '，');
        tics.push('dash');
    }
    const inter = tameInterjections(text);
    if (inter !== null) {
        text = inter;
        if (/唔/u.test(text) === false && /唔/u.test(original)) {
            tics.push('wu_to_pause');
        }
    }
    const offVoice = dropOffVoiceClauses(text);
    if (offVoice !== null) {
        text = offVoice;
        tics.push('off_voice');
    }
    const gou = dropGouClause(text);
    if (gou !== null) {
        text = gou;
        tics.push('gou_eval');
    }
    const zai = dropTailZai(text);
    if (zai !== null) {
        text = zai;
        tics.push('tail_zai');
    }
    if (/我(?<![在就])这边|我那边/u.test(text)) {
        text = flattenSide(text);
        tics.push('side');
    }
    if (/这个我认[了]?[。！？]?/u.test(text)) {
        text = text.replace(/[，,]?\s*这个我认[了]?[。！？]?/gu, '');
        tics.push('blank_ack');
    }
    if (/而是/u.test(text)) {
        const next = flattenNotBut(text);
        if (next !== text) {
            text = next;
            tics.push('not_but');
        }
    }
    if (/^(?:其实|说实话|老实说|说真的)[，,]?\s*/u.test(text)) {
        text = text.replace(/^(?:其实|说实话|老实说|说真的)[，,]?\s*/u, '');
        tics.push('filler_truth');
    }

    // 没改到任何一条就**原样返回**：不碰空白、不碰换行（日志里的截断片段也不该被"整理"）
    if (tics.length === 0) {
        return { text: original, tics: [] };
    }
    const cleaned = tidy(text);
    // 改坏了就别改（宁可漏一条，也不要发出半句话）
    if (cleaned === '' || Array.from(cleaned).length < 2) {
        return { text: original, tics: [] };
    }
    return { text: cleaned, tics: Array.from(new Set(tics)) };
}

/** 一串气泡逐条清（返回新数组，不改入参） */
export function cleanBubbles(bubbles) {
    const out = [];
    const hit = [];
    for (const one of bubbles ?? []) {
        const res = cleanTics(one);
        out.push(res.text);
        if (res.tics.length > 0) {
            hit.push({ before: one, after: res.text, tics: res.tics });
        }
    }
    return { bubbles: out, fixes: hit };
}
