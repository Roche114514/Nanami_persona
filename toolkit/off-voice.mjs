// r109：**离声检测**（off-voice）——主人给的清单：对"这句话/这个账/这件事"作点评、记账、结算的句子。
//
// 为什么不是 3B：实测 3B 判这一族只有 53%（当"AI 味"）／76%（当"点评记账腔"），
// 而那些漏掉的恰恰是「我跟」「我不拦」「回去睡」这种——**单看文本和普通短句没区别**，
// 模型没有"她的声音"这个先验。词表反而精准：在原典 2021 行上误伤 0.05%（1 条，还是正则穿透）。
//
// 用法：`hitOffVoice(text)` → `{ hit, phrases }`。只判不改；要不要删/改由调用方决定。
// 词表放在 `state/off-voice.json`（可随时加，按 mtime 生效）。
import { existsSync, readFileSync, statSync } from 'node:fs';

export const OFF_VOICE_PATH = process.env.MIND_OFF_VOICE_PATH
    ?? 'plugins/nanami_persona/toolkit/state/off-voice.json';

// 默认词表：每条 = 一条"点评/记账/结算"的句式；**都以分句边界锚定**，避免「确认这个」这种穿透。
export const DEFAULT_PATTERNS = [
    '^(?:这|那)?句话.{0,4}(?:稳|接得漂亮|我不接|我记着|我记下)',
    '^比(?:之前|刚才|那句|谁都).{0,4}(?:重|轻|稳|平|强|响)',
    '^我(?:记下|记得|记住|收下)了',
    '^(?:这个|这)账我记住',
    '^认(?:下来|这个|认?你)$',
    '^认得实$',
    '^(?:我|你)?不?(?:拦|催|跟)$',
    '^回去睡$',
    '^(?:你)?自己?掂量$',
    '^收不回去$',
    '^没接你那句',
    '^什么心思都写在',
    '^问别人还是问自己',
    '^睡吧[，,]我守着',
    '^很(?:平|直|稳)$',
    '^(?:这)?话我(?:留(?:着|下)|收下|记着|记下)',
    '^他记得这事$',
    '^说得比谁都响$',
    '^声音没抖',
];

const cache = { path: '', mtimeMs: -1, patterns: null };

function compile(list) {
    const out = [];
    for (const one of list) {
        try {
            out.push({ src: one, re: new RegExp(one, 'u') });
        }
        catch {
            // 坏正则跳过，不影响其它条
        }
    }
    return out;
}

export function loadOffVoice(path = OFF_VOICE_PATH) {
    try {
        const stat = statSync(path);
        if (cache.path === path && cache.mtimeMs === stat.mtimeMs && cache.patterns) {
            return cache.patterns;
        }
        const raw = JSON.parse(readFileSync(path, 'utf8').replace(/^\uFEFF/u, ''));
        const list = Array.isArray(raw?.patterns) && raw.patterns.length ? raw.patterns.map(String) : DEFAULT_PATTERNS;
        cache.path = path;
        cache.mtimeMs = stat.mtimeMs;
        cache.patterns = compile(list);
        return cache.patterns;
    }
    catch {
        cache.patterns = cache.patterns ?? compile(DEFAULT_PATTERNS);
        return cache.patterns;
    }
}

/** 按分句切，逐句看是否命中"点评/记账腔"。 */
export function hitOffVoice(text, patterns = loadOffVoice()) {
    const source = String(text ?? '').trim();
    if (!source) {
        return { hit: false, phrases: [] };
    }
    const clauses = source.split(/(?<=[。！？；\n])/u).flatMap((one) => one.split(/(?<=[，,])/u));
    const phrases = [];
    for (const clause of clauses) {
        const core = clause.replace(/^[\s「『“"]+/u, '').replace(/[\s。！？；，,」』”"]+$/u, '').trim();
        if (!core) {
            continue;
        }
        for (const one of patterns) {
            if (one.re.test(core)) {
                phrases.push(core);
                break;
            }
        }
    }
    return { hit: phrases.length > 0, phrases };
}
