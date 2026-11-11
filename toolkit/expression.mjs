// r109：**立绘 → 语气**（动态提示词）
//
// 主人设想：卡片出来后 → 判一个情绪状态 → 映射到 `七海千秋.char` 里的立绘 → 让大模型"读"那个状态来模仿。
// 技术事实：大模型是文本接口，**看不见 webp**。所以"读立绘"必须落成**一句状态描述**：
// 立绘只是一个"离散的表情档位"，我们把它翻译成一行**状态**（不是动作、不是旁白）。
//
// 纪律（跟卡片同一条）：这行只写**状态**，不写身体动作（不写"皱眉/鼓起脸颊"），
// 否则模型会照念成舞台指示——SOUL §6 已经禁过。
//
import { existsSync, readFileSync, statSync } from 'node:fs';

// 立绘的**目视描述**（我看过原图写的；未核对的先用标签推）。路径与 mtime 缓存。
const LOOK_PATH = process.env.MIND_SPRITE_LOOKS
    ?? 'plugins/nanami_persona/toolkit/state/expression-sprites.json';
const lookCache = { path: '', mtimeMs: -1, value: null };

function loadLooks(path = LOOK_PATH) {
    try {
        const stat = statSync(path);
        if (lookCache.path === path && lookCache.mtimeMs === stat.mtimeMs && lookCache.value) {
            return lookCache.value;
        }
        const raw = JSON.parse(readFileSync(path, 'utf8').replace(/^\uFEFF/u, ''));
        lookCache.path = path;
        lookCache.mtimeMs = stat.mtimeMs;
        lookCache.value = raw?.sprites ?? {};
        return lookCache.value;
    }
    catch {
        return lookCache.value ?? {};
    }
}

// 映射用确定性规则（moodV/moodA/energy/evaluativeConcern/woundSkill/repeat），
// 不用 3B：这一族是多分类，3B 在"垫字/停顿"这类多分类上只有 5 成。
export const SPRITES = {
    1: { tag: '中性，平静', feel: '平平的' },
    2: { tag: '疲倦，困倦', feel: '有点困' },
    3: { tag: '指点，引导', feel: '在跟他讲一件事' },
    4: { tag: '生气，鼓起脸颊', feel: '不太高兴' },
    5: { tag: '开心，微笑', feel: '心里松快' },
    6: { tag: '筋疲力尽，流口水', feel: '困得撑不住' },
    7: { tag: '皱眉，严肃，担心', feel: '有点担心' },
    8: { tag: '沉思，好奇', feel: '在想' },
    9: { tag: '歪头，轻微好奇', feel: '有点好奇' },
    10: { tag: '解释，指向，果断', feel: '想把话说清楚' },
    11: { tag: '仔细查看，眺望', feel: '在看' },
    12: { tag: '不安，担忧', feel: '有点不安' },
    13: { tag: '兴奋，激动', feel: '有点来劲' },
    14: { tag: '沉思，低头，皱眉', feel: '在想一件不太好的事' },
    15: { tag: '困倦，打瞌睡', feel: '困得快睡着' },
    16: { tag: '不满，难过，沮丧', feel: '有点闷' },
    17: { tag: '警告，阻止，制止', feel: '不想让他做' },
    18: { tag: '埋怨，嘟嘴', feel: '有点委屈' },
    19: { tag: '疑惑，思考，好奇', feel: '没听懂' },
    20: { tag: '（备用 20）', feel: '平平的' },
    21: { tag: '（备用 21）', feel: '平平的' },
};

const num = (v, fallback = 0) => (typeof v === 'number' && Number.isFinite(v) ? v : fallback);

/** 从状态推出"这一刻的样子"（离散档位 + 一句状态）。规则顺序＝优先级。 */
export function expressionOf(state = {}) {
    const energy = num(state.energy, 0.7);
    const moodV = num(state.moodV, 0);
    const moodA = num(state.moodA, 0.3);
    const concern = num(state.evaluativeConcern, 0);
    const woundSkill = num(state.woundSkill, 0);
    const repeat = num(state.repeatCount, 0);
    const asleep = state.asleep === true || state.asleepAt > 0;

    if (asleep || energy < 0.2) {
        return pick(6);
    }
    if (energy < 0.32) {
        return pick(15);
    }
    if (energy < 0.45) {
        return pick(2);
    }
    if (woundSkill >= 0.6 || (moodV <= -0.3 && repeat < 3)) {
        return pick(16);
    }
    if (repeat >= 3) {
        return pick(18);
    }
    if (concern >= 0.6) {
        return pick(7);
    }
    if (moodV <= -0.25) {
        return pick(12);
    }
    if (moodA >= 0.6 && moodV >= 0.2) {
        return pick(13);
    }
    if (moodV >= 0.25) {
        return pick(5);
    }
    if (energy >= 0.6 && moodV >= 0.1) {
        return pick(9);
    }
    return pick(1);
}

function pick(index) {
    const one = SPRITES[index] ?? SPRITES[1];
    const look = loadLooks()[String(index)]?.look ?? '';
    return { index, tag: one.tag, feel: one.feel, look };
}

/**
 * 卡片那一行：**描述这张立绘的样子**（主人要的是"把图片描述出来，让大模型去演"）。
 * 有目视描述就用它；没有就退回一句短状态。
 * 注意：描述里可以有姿势/神情，但**不许写成旁白台词**——SOUL §6 禁括号动作，发送层也会剥。
 */
export function expressionLine(state) {
    const one = expressionOf(state);
    const text = one.look || one.feel;
    return `- 现在的样子：${text}`;
}
