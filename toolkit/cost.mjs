// §19（2026-10-26）：**一个比较，不是一套阀门**——答全 / 短答 / 短答+说明状态 / 沉默。
//
// 硬的四条（Lead）：
//   ① `net` 只进日志与 `--json`，**绝不进卡片**（进了就是台账腔，r78 的坑）；
//   ② 不写"厌恶时不解释"这类规则——算术已经排除它（`+0.60*avoidance`），**加规则＝两个真相**；
//   ③ 只作用于**私聊**；群聊仍走已冻结的 `group-want`；
//   ④ 系数照 §19 的表，**一个数都不许自己改**（`explainedRecently` 用 0.20，psych 用 W5 复算否掉了 0.15）。
import { readFileSync, statSync } from 'node:fs';

export const COST_GATE_PATH = 'state/cost-gate.json';
let gateCache = { mtimeMs: -1, enabled: false };

export function costGateEnabled(root = '.') {
    try {
        const path = `${String(root).replace(/[\\/]+$/u, '')}/${COST_GATE_PATH}`;
        const stat = statSync(path);
        if (gateCache.mtimeMs === stat.mtimeMs) {
            return gateCache.enabled;
        }
        const raw = JSON.parse(readFileSync(path, 'utf8').replace(/^\uFEFF/u, ''));
        gateCache = { mtimeMs: stat.mtimeMs, enabled: raw?.enabled === true };
        return gateCache.enabled;
    }
    catch {
        return false;
    }
}

// 2026-10-26 裁决（Lead）：**"说全"的收益要乘 `capacity`**。
// 一句话：撑不住的时候，说全的收益她自己拿不到——她说到一半就散了。
// `capacity = 0.4 + 0.6*energy`，与 §18 readiness 同一形状，不新造。
export function capacityOf(energy) {
    const one = Number(energy);
    return 0.4 + 0.6 * Math.max(0, Math.min(1, Number.isFinite(one) ? one : 0));
}

// ── 得到（她得到的体验）——"说全"与"短答"都乘 `capacity` ────────────────
// r102：`state` 那一项里的 `0.25*regard` 现在**只在 `want=1` 时才有**。
// 规格（docs/r82-psych-spec.md §19）给它的理由是"应一句，并把『我撑不住』说出来——
// 清掉『他以为我烦他』的风险"。没有自己要给的东西时，就不存在"被误读成烦他"这件事可清；
// 原来那一项是无条件的，于是 want=0 时她反而最划算的是"解释状态"，实测（标定卷 r101）
// 24 题里 14 题落在 `state`——那不是她，是系数漏了一个条件。
export function gainsOf({ want, regard, energy }) {
    const capacity = capacityOf(energy);
    const mine = Number(want) || 0;
    return {
        full: mine * capacity,
        brief: 0.40 * mine * capacity,
        state: 0.40 * mine * capacity + 0.25 * (Number(regard) || 0) * mine,
        silent: 0,
    };
}

// ── 付出（她付出的体验）──────────────────────────────────────────────
// full = 0.30 + 0.70*strain；brief = 一半；state = 短答 + 0.08 + 0.60*avoidance + 0.20*explainedRecently；
// silent = 0.60*max(want, addressed)*waiting
//
// r102 改了一处：`silent` 原来只乘 `want`（"这一轮我自己有话说吗"），于是**她没有话说的时候沉默是免费的**
// —— 实测：日常卷 61/86 轮、标定卷 12/24 题都落在沉默，接上闸她就大半时间一个字不说。
// 原典里不是这样（`tools/persona-lab/canon-response.mjs` 量的）：他直接叫她／问她的时候，
// 3 行窗口内她接住 33%，其中 27% 只是"应一声"；她本人提到「困／睡」的行有 46 条。
// 会话分析也支持这件事：不回应是**被标记的缺席**（Sacks／Schegloff／Jefferson 1974 的轮次组织）。
// 所以这一项改成 `max(want, addressed)`——「他正在跟她说话」（`footing.who === 'me'`）本身就是一种局面，
// 不是一个该由人教她的话术。**系数一个没动（仍是 0.60），也没有新常数。**
export function costsOf({ want, strain, avoidance, waiting, explainedRecently, addressed }) {
    const brief = (0.30 + 0.70 * strain) / 2;
    const stake = Math.max(Number(want) || 0, Number(addressed) || 0);
    return {
        full: 0.30 + 0.70 * strain,
        brief,
        state: brief + 0.08 + 0.60 * avoidance + 0.20 * (explainedRecently ? 1 : 0),
        silent: 0.60 * stake * waiting,
    };
}

const ORDER = ['full', 'brief', 'state', 'silent'];
const NAME = { full: 'full', brief: 'brief', state: 'state', silent: 'silent' };

/** 唯一的比较：pick = argmax(net)。没有阈值、没有第二张权重表。 */
export function pickOf(input) {
    const gains = gainsOf(input);
    const costs = costsOf(input);
    const net = {};
    for (const one of ORDER) {
        net[one] = Number((gains[one] - costs[one]).toFixed(3));
    }
    let pick = ORDER[0];
    for (const one of ORDER) {
        // §19.2 的平局规则：**省力优先**（cost 低的赢）。
        // r83 修：原来只写 `>`，平局时留在 `ORDER[0]`＝`full`——那是四个选项里最费力的那个，
        // 正好和"平局按省力优先"相反。这个 bug 看不见，因为 `net` 小数四位才撞上平局；
        // 但 `net` 会被 `toFixed(3)` 抹平（例如 brief 与 state 都算到 -0.240 时），
        // 那时结果就变成"她累了还硬撑着说全"。**有断言钉着**（selftest-cost W6）。
        if (net[one] > net[pick] || (net[one] === net[pick] && costs[one] < costs[pick])) {
            pick = one;
        }
    }
    return { net, pick, gains, costs };
}

export function costLogLine(input, result) {
    const f = (one) => Number(one).toFixed(2);
    return `[cost] want=${f(input.want)} addressed=${f(input.addressed)} strain=${f(input.strain)} regard=${f(input.regard)}`
        + ` waiting=${f(input.waiting)} avoidance=${f(input.avoidance)}`
        + ` → net: full=${f(result.net.full)} brief=${f(result.net.brief)}`
        + ` state=${f(result.net.state)} silent=${f(result.net.silent)} pick=${NAME[result.pick]}`;
}
