// 心智层：把"她此刻的心"先算出来，再让话从心里长出来。
//
// 为什么不再加规则了：规则只能规定"她该怎么说"，规定不了"她为什么这么说"。
// 没有欲望和状态的人不会顶嘴、不会换角度、也不会有第二种形状——这三样正是评委每轮都在扣的。
//
// 设计依据（docs/design-mind-layer.md §7）：
//   · 连续量（精力/心情）用确定性规则漂移，模型无权改
//   · agenda（这一轮想干什么）由「他要什么 × 心情 × 上一轮 agenda」推出，四选一
//   · 动作层离散化（8 选 1）先选动作，再决定骨架
//   · 每轮落盘一行 JSON，可回放；连续 N 轮没有非平凡变化就报警（状态机收敛是头号死法）
//
// 用法：
//   node tools/persona-mind/mind.mjs --his "他刚说的话" [--chat main]   → 打印【我心里】块
//   node tools/persona-mind/mind.mjs --her "她发出去的话" [--chat main] → 回写
//   node tools/persona-mind/mind.mjs --show [--chat main]               → 看当前状态
//   node tools/persona-mind/mind.mjs --sleep-realign [--chat main]      → 老状态接入两过程时对齐一次
//   node tools/persona-mind/mind.mjs --replay <cases.json>              → 拿真实卷子跑一遍状态曲线
import { readFileSync, writeFileSync, mkdirSync, existsSync, statSync, unlinkSync } from 'node:fs';
import { dirname, basename } from 'node:path';
import { cardLine as commitmentCardLine, openItems as commitmentOpenItems, recordPromise, summary as commitmentSummary } from './commitments.mjs';
// 2026-10-27：星露谷桥（只读）。她真在玩游戏时，那块地就是她"手上有的事"。
import { stardewPresenceLine, stardewWaterLine } from './lib-stardew.mjs';
// r82（R1）：线索激活——纯函数模块，不在里面读盘、也不反向 import 本文件（防循环依赖）
import { ACTIVATION_RECORD, ACTIVATION_EXIT, activationOf, activationFires, activationLineOf, noteActivation } from './activation.mjs';
// r83（§19 第 9 层）：**四个选项一次 argmax**——纯函数，系数全在那边，这里只喂六个量
import { pickOf, costLogLine } from './cost.mjs';
import { normalizeChatKey } from './chat-key.mjs';
// r99：**可考据的经历**与她的尺子（只收剧本原句，一行都不写"她认为…"）。
// 开关 `MIND_CANON=0` → 卡片与 r98 逐字一致（selftest-r99 的 OFF 臂守着）。
import { canonLineOf, canonTagOf, noteCanonShown, stakeOf, asksWhatSheWants, requestOf, resonanceOf } from './canon-memory.mjs';
// r106：感知外包（LLM 判言语行为，失败即退回规则）。只在 `--his` 路径上取一次。
import { perceive, setCurrentPercept, currentPercept } from './percept.mjs';
import { expressionLine, expressionOf } from './expression.mjs';

// r82（R0.1）：状态根目录可替换——自测/采样器跑在临时目录里，别抢线上那份 state。
// 优先级：`--root <dir>`（照 tools/persona-heart/beat.mjs:94 的先例，心跳的自测就是这么隔离的）
// → 环境变量 `MIND_ROOT` → 默认值。**默认值一字不改**：线上三个进程都不传，行为与 r81 完全相同。
// 一个根目录同时管三样东西：state/mind.json、state/commitments.json（承诺账）、state/scenes.json。
function rootOf(argv) {
    const index = argv.indexOf('--root');
    if (index >= 0) {
        const value = argv[index + 1];
        if (typeof value === 'string' && !value.startsWith('--')) {
            return value.replace(/[\\/]+$/u, '');
        }
    }
    const env = String(process.env.MIND_ROOT ?? '').trim();
    if (env) {
        return env.replace(/[\\/]+$/u, '');
    }
    // [Shinsekai 移植] 默认状态根改为相对路径：聊天进程的 cwd 就是项目根。
    // 插件每次调用都会显式传 --root，这里只是「有人手敲 CLI」时的兜底。
    return 'data/nanami';
}
const ROOT_DIR = rootOf(process.argv.slice(2));
const STATE_PATH = `${ROOT_DIR}/state/mind.json`;

// ── 状态 ────────────────────────────────────────────────────────────────
// r82（R0.2）：导出给自测用——`tmp/gate/selftest-mind-r82.mjs` 可以直接 import 这三个函数，
// 不必再 spawn CLI + 改状态文件来种状态（`selftest-mind-actions.mjs` 的 `seedEnergy` 就是这么被迫的）。
// ── r107（2026-11-01）：**构念层**（心理学框架，不做"印象表"）────────────────────
// 依据：docs/r107-psych-construct-map.md。
// 这一层是**跨轮不变的特质**——它不参与每轮计算，只作为系数/门槛，决定"状态怎么算"。
// 纪律：构念必须能解释不止一件事；只能解释一件的是"指标"，留在语料里，不进模型。
export const TRAITS = {
    // L1 气质：努力控制高（先在心里整理再开口，原典 e00_003_101）、唤醒低（慢）
    effortfulControl: 0.8,
    arousalBaseline: 0.35,
    // L3 自我结构：自我价值锚在两个域——游戏胜任、不给人添麻烦
    selfWorthDomains: ['gameCompetence', 'notBeingABurden'],
    privateSelfConsciousness: 0.75,   // 高：大量内省
    publicSelfConsciousness: 0.2,     // 低：不做形象管理（也解释了"别人怎么看我"为何几乎不出现）
    // L3 域限定自我效能：游戏高、非游戏低
    domainEfficacy: { game: 0.85, nonGame: 0.3 },
    // L4 评价性担忧：只对亲近者敏感
    evaluativeSensitivity: 0.7,
    // L3 自我披露门槛：高（只在被问、且对方是同类时才说自己）
    selfDisclosureThreshold: 0.8,
    // L2 需要剖面：关系性高、胜任高（域限定）、自主低（顺从型），不是"缺自主"
    // r109（主人定的分工）：**能算的给模型，不能算的给 LLM**。
    // 这两个是"她接不住话"的可算来源（不是新规则，是特质常量）：
    //   socialEfficacy 低 → 不必接话、先问、先请示（原典干净子集 20 行 1.0%：请示/抱歉）
    //   surgency 低     → 话慢、不表演（同上 34 行 1.7% 卡壳）
    socialEfficacy: 0.3,
    surgency: 0.25,
    needProfile: { relatedness: 0.7, competence: 0.7, autonomy: 'low', safety: 0.5 },
};

/**
 * r107 构念结算：把这一轮的事件落到**构念**上（不是落到句子片段上）。
 * 调用点：CLI 里 `hits` 与 appraisal 都算完之后、渲染卡片之前。
 * - `woundSkill`：非游戏域自我效能被否定（L3），只在她在乎的人面前记（L4 条件），×0.7/轮衰减
 * - `evaluativeConcern`：L4 状态量（见 evaluativeConcernOf）
 * - `reflectedRegard`：L4 二元构念（同伴／外人）
 */
export function settleConstructs(next, hits = [], appraisalKey = null) {
    const caredPerson = next.mirrorScope !== 'group' || next.beliefSubject === '主人';
    const skillHit = Array.isArray(hits) && hits.includes('woundSkill');
    const prev = numOf(next.woundSkill, 0) * 0.7;
    next.woundSkill = clamp(prev + (skillHit && caredPerson ? 1 : 0), 0, 2);
    next.appraisalKey = appraisalKey ?? next.appraisalKey ?? null;
    next.evaluativeConcern = round2(evaluativeConcernOf(next));
    next.reflectedRegard = reflectedRegardOf(next);
    return next;
}

/** L4：这一轮的**评价性担忧**（状态量，0..1）。它不是"他失望了"，是"我怕让他失望"。 */
export function evaluativeConcernOf(state) {
    const s = state ?? {};
    let value = 0;
    const appraise = String(s.appraisalKey ?? '');
    if (appraise === '被夸了' || appraise === '被误判' || appraise === '胜任不足' || appraise === '睡过去了') {
        value += 0.35;
    }
    if (s.praiseNow === true) {
        value += 0.2;
    }
    if (s.insultedNow === true || s.wrongClaimNow === true) {
        value += 0.25;
    }
    // 他是她"在乎的人"时才放大（L4 只对亲近者敏感）
    if (s.mirrorScope === 'private' || s.beliefSubject === '主人') {
        value += 0.15;
    }
    // 他说"没关系/没事/不怪你" → 降
    if (/没关系|没事|不怪你|别在意|不用勉强/u.test(String(s.lastInboundText ?? ''))) {
        value -= 0.25;
    }
    return clamp(value * TRAITS.evaluativeSensitivity, 0, 1);
}

/** L4：他怎么看她（二元构念：同伴／外人）。**不是**"他觉得我笨/可爱/烦"。 */
export function reflectedRegardOf(state) {
    const s = state ?? {};
    if (s.mirrorScope === 'group' && s.beliefSubject && s.beliefSubject !== '主人') {
        return '外人';
    }
    return '同伴';
}

export const DEFAULT_STATE = {
    energy: 0.72,
    // r102：**地板之前**的真实精力（第 9 层读它；`energy` 会被"他正说着话"的地板抬到 0.35）。
    // null = 还没跑过一轮（那时两者同值）。
    energyTrue: null,
    // r94：睡眠压力（Process S）。0 = 睡饱，1 = 很困；由睡眠两过程推进，模型无权改。
    need: 0.30,
    // 迁移标记：老状态没有 need 时补默认值；已有值绝不清掉。
    sleepSchema: 1,
    sleepPressure: 0.30,
    sleepTarget: null,
    asleep: false,
    // r107：**睡觉是她的行动，不是时钟**（主人 10-31 指出：别拿 23:30–08:00 硬套）。
    // `asleepAt` 是她自己说"去睡了/眯一会儿"时落下的时刻；>0 就是真在睡，
    // 醒来只能是两件事：她自己睡够了（思考层算的时长），或者被消息叫醒。
    asleepAt: 0,
    wokeAt: 0,
    wakeReason: '',
    woken: false,
    moodV: 0.15,
    moodA: 0.30,
    agenda: 'want_company',
    // r82（P2 后半 / psych §2）：`belief` 从"他认为我怎样"变成"**在场的那个具体的人**认为我怎样"。
    // 形状：`subjects` 是唯一真相（最多 3 个主体，按最近见到排序）；外面那三个扁平键是
    // **主人那一格的镜像**（兼容位，写到 r83 再删，见 psych §2 ⑤ 第 1 条）。
    // 不建「屋里的人」这类伪主体：psych §8 第 4 条留了退路、Lead 采纳——群里没有匹配主体时
    // 直接取**在场者集合的 max**（currentMirrorOf 的那一支），不造假人。
    belief: {
        thinksImDumb: 0.20,
        thinksImCute: 0.30,
        tiredOfMe: 0.10,
        subjects: {},
    },
    wound: 0,
    // ── r82（F / psych §13.2）：wound 从"一个标量"拆成**三种疼**；r101 起 rank 退役，只剩两种 ─────
    // 衰减系数（0.7／0.5）是 2026-10-26 在 canon 86 轮上、按阈值 0.6 标定的（口径 =
    // "阈值以上还有几轮"：game 1／human 0）。**换卷子要重标**（§13.10：标定产物不是规则）。
    // 三个都进 NUMERIC_FIELDS；命中幅度 +1.0 沿用现状（原 702–703「命中即 wound = 1」）。
    woundGame: 0,     // 只会游戏（×0.7/轮）
    woundHuman: 0,    // 是不是人（×0.5/轮）
    // r101：`woundRank` **退役**——不再写入、不再派生 `woundKind`，只让旧 state 的残留自己衰减干净。
    // 依据：docs/persona-r99-canon-memory.md §1.1（1154 行强证据里 0 支撑、2 条反证：L6249／L3945–3947），
    // 施工与回滚：docs/r101-wound-rank-removed.md。字段保留只为 schema 兼容（删字段会让 migrate 丢键）。
    woundRank: 0,     // 已退役（r101）：只衰减，不产生
    // r107 构念层：非游戏域的自我效能被否定（L3 域限定自我效能）。
    // 与 woundGame 是两根刺：那个是「游戏没用」，这个是「你连这个都不会」（原典 7 行：笨手笨脚/不擅长/不太会）。
    woundSkill: 0,
    // r107 构念层：每轮由 settleConstructs 写入的状态量（不是印象表，是 L4 的两个构念）
    evaluativeConcern: 0,
    reflectedRegard: null,
    appraisalKey: null,
    // r109：被夸分域（gift／competence／appearance）与"他指的是她真做过的事"
    praiseKind: null,
    ownActNow: false,
    // 迁移标记（§13.5）：老 state 没有它 → 一次性把老 wound 拆进 woundGame，然后置 2。
    // 没有它的话 migrateState 每轮都会再搬一遍（她刚衰减到 0，下一轮又变回 0.8）。
    woundSchema: 2,
    // 派生量（step 里算，卡片不写它——它是量不是事实，§13.6）
    woundKind: null,
    // ── r82（P2 后半 / psych §2）：多主体 belief 的三个在场者位 + 两个派生位 ─────────
    // `mirrorWho/Name/Scope` 来自 `--signals`（只认 who／name／scope 三个字段），每轮覆盖；
    // `beliefSubject` = 这一轮实际读的是谁（`--json` 用它验"读对了人"或"退回了 max"）；
    // `beliefSchema` = 迁移标记（照 woundSchema 的先例，幂等靠它）。
    mirrorWho: 'me',
    mirrorName: '',
    mirrorScope: 'private',
    beliefSubject: '主人',
    beliefSchema: 2,
    // r82（P4 模型侧）：这一轮的位置（冲谁说）。和 `woundKind`／`beliefSubject` 同一档——
    // 每轮在 CLI 路径上算一次、覆盖式写入，卡片与 `--json` 只读它。
    footing: null,
    // r83（§19）：这一轮她自己的取舍 `{pick, net, gains, costs, inputs}`。
    // **派生量、每轮覆盖**（和 `woundKind` 同档）：卡片一个字都不读它，
    // 只有 `--json`／trace 读。`MIND_COST=0` 时恒为 null。
    cost: null,
    askStreak: 0,
    lastInboundAsked: false,
    hisStuckNow: false,
    // r109：他情绪上不好（检测保留，**不驱动 appraisal**——三次盲评没有收益，见 docs/r109-blind-decisions.md）
    hisDownNow: false,
    concreteThing: false,
    lastEndedQuestion: false,
    questionStreak: 0,
    rounds: 0,
    sinceShift: 0,
    lastSaid: [],
    // r82（C / 续话）：这一轮「她还差一句没说」的材料（每轮重算、不累计）+ 两个配额位
    // （`lastFollowUpAt` 防连发、`followUpDay/followUpToday` 限今天补几次）
    followUp: null,
    lastFollowUpAt: 0,
    followUpDay: '',
    followUpToday: 0,
    // r82（S3 / socio §4）：她那句留没留口（1/0）+ 她最后一次说话的时刻。
    // 只写这两个量，不读——读的人在 tools/persona-heart/presence.mjs（`deadEndOf`）。
    leavesGap: 0,
    lastHerAt: 0,
    // r82（S2 / socio §3②）：人情账的**待排增量**——`--her` 记、心跳排进 presence 后由 `--favor-consumed` 清零。
    // 为什么不在 mind 里直接记账本：账本唯一住在 `state/presence.json`（socio 的设计），
    // 而 `--her` 不该去碰 presence（那会让两边都能写同一本账）。
    favorPending: 0,
    lastActions: [],
    // r30 新增（task-4 ①②③④）
    lastOpenRound: -999,   // 上次「起手」是哪一轮（主动追求要低频，不能连发）
    openCount: 0,          // 这一段起手过几次
    lastCoopRound: -999,   // 上次落成「同伴位」是哪一轮
    actionRounds: {},      // 各动作在哪些轮用过（按窗口算配额，防止变成新模板）
    selfExposeAllowed: false, // 这一轮满不满足"自曝位置"的四条件（§5.2）
    praiseNow: false,         // 他这一句是不是在夸她（被夸→反问式收下，§3.5⑤）
    insultedNow: false,       // 他这一句是不是在骂她／贬低她（→ 驳论点，r45）
    reportedInsult: false,    // 命中贬低词，但来源是转述（“有人说你傻”）
    wrongClaimNow: false,     // 他这句在道理上不成立／冤枉她（→ 驳论点）
    teasedNow: false,         // 他在逗她但没构成论点（→ 被调侃：尴尬或干幽默）
    directAskOffer: false,    // 他把"要什么"直接递过来了（教我/给我讲/带我去）→ 可立刻讨要
    // r51/task-6：知识边界与强项
    offLimitNow: '',          // 这一句踩到"她不该懂"的哪一格（空＝没踩）
    offLimitWords: [],        // 他这句里真的出现过的越界词（r52：只许问这些）
    offLimitCanon: '',        // 那一格的原典证据（只进代码注释，不再进卡片——见 r52 的毛病①）
    strongSuitNow: false,     // 这一句在说她的强项（射击/格斗/解谜…）→ 不许说"不擅长"
    factNow: '',              // 这一句问到她的事实时要纠正的事实（r51，防"事实错"）
    strongFlipAt: -99,        // 她上一次把强项说成"不擅长"是哪一轮（r53）
    strongFlipRecent: false,  // 最近两轮翻过面 → 这一轮要纠正
    sameTopicCount: 0,        // 同一件事被问第几次（≥3 就要换落法）
    inboundSeen: [],          // 最近 12 句他的原话（去标点）——判"同一个问题又问一遍"
    autobio: [],
    // r82（R2）：这一段"她讲过什么"的工作记忆（≤4 条、窗口 6 轮）；与 autobio 是两张表，互不读
    stories: [],
    // r82（R1）：线索激活的两个附属位。
    //   `activationLine` = **这一轮**算好的卡片那一行（照 commitmentsLine 的先例，覆盖式写入，
    //   每轮都重算；不是激活值本身）。它的存在同时是 appraisal 的开门条件——这样激活值
    //   **不用落盘**（§15.4：落盘就会变成常驻量）。
    //   `activationSeen` = 习惯化记账（这条账最近连响过几轮／静默到第几轮），是防焦虑闸要用的，
    //   只存 id → {streak, until}，不存分数。
    activationLine: null,
    activationSeen: {},
    scene: null,             // r73：当前情境（空间是给定的，她在哪儿）
    // r75（v7）：信任与受伤——"空间要不要换，是她愿意不愿意的事"
    trust: 0.6,              // 0–1，对"这个人"的信任
    hurtUntil: -1,           // 受伤生效到第几轮（= 受伤那一刻 + 8）
    hurtAt: -99,             // 被伤到的那一轮
    hurtBy: '',              // 因为什么（辱骂／虚报）
    pendingInvite: null,     // 他邀她出去但还没兑现的那件事 {place, at, purpose}
    missIgnore: 0,           // 连着多少轮"她说 A 他一直说 B"
    gist: {},                // r68：按话题的遍数（模糊痕迹）
    episode: {},             // r68：情节记忆（游戏/进度/地点绑成一整条）
    repeatCount: 0,          // r67：同一句被连问第几遍
    irritation: 0,           // r67：烦躁累积（0–1，适应用）              // r66：自传记忆（只记"我"的事实，≤5 条，20 轮过期）
    // r50：近期她在回复里挂过几次游戏词（SOUL 里"一段里至多两处"的执法依据）
    gameWordRounds: [],
    // ── r82（R0.2）：这 9 个字段一直在线上 state 里、却不在 DEFAULT_STATE 里 ────────────
    // 体检（tmp/probe-r82-fields.mjs）查出来的"隐式 schema"。补进来的意义不是让代码能跑，
    // 是让 `--show` 的字段集就是真相：以后**新量必须在这里写默认值**，否则等于又长一个隐式字段。
    forcedShift: false,      // 这一轮发生过强制换挡（单轮标记，每轮先清）
    lastInboundText: '',     // 他上一句原话（截 40 字）
    interestLayer: '',       // 这一轮她对他好奇的是哪一层（八层之一）
    layerHistory: [],        // 最近六轮的兴趣层
    hisWeakNow: false,       // 他这句在示弱
    inviteStake: 0,          // 他邀约的分量（willingToMove 用）
    sceneSig: '',            // 场景签名（变了才重新抽地方）
    tareaCount: 0,           // 讨要一共用过几次
    commitmentsLine: null,   // 承诺账那一行（CLI 算好塞进来，renderCard 不读盘）
    // r88：账本里到底有没有欠着的事（与"要不要出现在卡片上"分开）。
    // 为什么分开：交班 §17.8 把 `commitments` 归进**记忆层**，而记忆只在被想起来的那一
    // 轮才进卡片（`activation.mjs` 文件头第一句）。但"她欠着事"这件事仍然是一个事实，
    // §19 的 `regard` 要用它（欠着事的人不会"不在意这段"）——所以事实留在这里，
    // 呈现交给记忆的出口，两件事不再绑在同一行字符串上。
    debtOpen: false,
    // r89：今天反刍一共把她压下去多少（`{day, sum}`，sum 是负数，封顶见 `MOOD_STUCK_DAY_CAP`）。
    // 落盘是为了跨进程封顶——不然每一拍都"从零开始扣"，她会在没人说话的白天一路掉到谷底。
    moodStuck: { day: '', sum: 0 },
    // r91：这一阵子她反复在想的那件事（`{key, samples, count, charged, lastWant, prevWant, stuck, firstAt, lastAt, closed}`）。
    // 为什么不能只靠 `thoughts`：它只有 5 条，滚出去之后"想了第几次"就重新从 1 开始；
    // 而且她的措辞每拍都在变，只比当前这一句会漏掉（线上三拍的 about 一句比一句不像）。
    // 它只记**事实**（同一件事想了多少拍、其中几拍已经算过代价），不写"该不该放下"。
    rumination: null,
    // r89：最近三次"做了之后心情变了多少"（`[{at, kind, d}]`，最多 3 条）。
    // 她下一次思考会读到它——这是她判断"这样做有没有用"的经验来源，不是我们给的建议。
    effects: [],
    // ── r84（思考层）：他不在说话的时候，她自己想过的那些事 ────────────────────────────
    // 为什么要有这一块：她的话一直只在"他说话"与"她回话"之间的那几秒里发生，
    // 两次对话之间她是不存在的。这一块让"没有他"的那段时间也留下痕迹，
    // 并且下一条话能看见自己刚才在想什么（同一时间线的同一个人）。
    // 写法纪律（r78 的血）：想法的文字由她自己生成，代码一个字都不许写"该怎么想"；
    // 这里只存**事实**：什么时候想的、想了什么、她打算做什么、做成了没有。
    thought: null,           // 最后一条想法 {at, text, about, want, keep, keepLine, deed, acted}
    thoughts: [],            // 最近 5 条（环）——卡片只读 `thought`，`thoughts` 给自测/审计/复现
    lastThoughtAt: 0,        // 上一次"想过"的时刻（节律判定用）
    lastHourlyThoughtAt: 0,  // 上一次"整点想"的时刻（余波那一拍不占整点配额）
    thoughtAfterExchangeAt: 0, // 已经消化到哪一次对话（余波去重：同一段对话只消化一次）
    thoughtShown: 0,         // 这一条想法在卡片上出现过几次（≤2，防"一条旧事挂十几轮"）
    thoughtLine: null,       // 卡片那一行（每轮重算的派生位，不是真相——照 commitmentsLine 的先例）
    deed: null,              // 她想做、但这一轮还没做的那件事（{kind, text, at}）——下一拍/别的出口接手
    updatedAt: Date.now(),
};

function loadAll() {
    try {
        if (!existsSync(STATE_PATH)) {
            return {};
        }
        return JSON.parse(readFileSync(STATE_PATH, 'utf8'));
    }
    catch (err) {
        return {};
    }
}

function saveAll(all) {
    // r82（R0.3）：拿不到锁的时候**只读不写**——卡片照出、退出码照 0，就是这一轮的状态落不了盘。
    // 丢一轮状态远好过两个进程互相覆盖整张表（体检实测：8 并发丢 6 条）。
    if (!WRITE_ALLOWED) {
        return false;
    }
    mkdirSync(dirname(STATE_PATH), { recursive: true });
    writeFileSync(STATE_PATH, JSON.stringify(all, null, 2), 'utf8');
    return true;
}

// ── r107：**"算完了"和"说出去了"必须是同一件事** ─────────────────────────────
// 病根（2026-10-30 00:20 实测）：`--his` 原来是先 `saveAll` 再 `console.log`，
// 而网关/插件给它的上限是 2 秒，超时直接 kill → 状态已经前进了一轮（rounds+1、精力掉了），
// 卡片却没送到 prompt。表现就是 trace 一行 `empty-or-timeout`，而她这一轮"没读到卡、但身体已经被算过一次"。
// 现在：先写 stdout，**flush 成功才写盘**；被杀在半路 → 这一轮当作没发生（下一轮重算）。
// 空写一次用来等前面的输出真的冲出去（stdout 是有序的）。
function flushStdout() {
    return new Promise((resolve) => {
        let settled = false;
        const finish = (ok) => {
            if (!settled) {
                settled = true;
                resolve(ok);
            }
        };
        try {
            process.stdout.once('error', () => finish(false));
            process.stdout.write('', (err) => finish(!err));
        }
        catch {
            finish(false);
        }
    });
}

// ── r82（R0.3）：状态表的独占锁 ──────────────────────────────────────────
// 病根：loadAll → 改自己那条 → saveAll 是**整表读-改-写**。每轮有三个写者
// （`--his` 同步、`--her` detached、账本 --record）＋心跳 --idle，谁慢谁被覆盖。
// 实测（tmp/probe-r82-race.mjs）：8 个进程同时写，只有 2 个键活下来。
// 做法：整段读改写期间持有一把 `mind.json.lock`（O_EXCL 创建）；拿不到就重试 5×20 ms，
// 死锁残留（mtime 超过 2 秒）自动清掉；最后还是拿不到就"只读不写"——不许把回话挡住。
const LOCK_PATH = `${STATE_PATH}.lock`;
const LOCK_STALE_MS = 2000;
// 两道预算：`--his` 有人在等她回话（预算 8 ms 量级），只等 5×20 ms；
// `--her` / `--idle` / `--reset` 是 detached（没人等），可以多等一会儿——
// 故事计数就写在 `--her` 上，让它因为抢不到锁而丢一次，等于那个量不存在（Lead 的 R0.3 理由）。
const LOCK_RETRIES_FAST = 5;
const LOCK_RETRIES_SLOW = 25;
const LOCK_WAIT_MS = 20;
let WRITE_ALLOWED = true;

function sleepSync(ms) {
    Atomics.wait(new Int32Array(new SharedArrayBuffer(4)), 0, 0, ms);
}

function breakStaleLock() {
    try {
        const stat = statSync(LOCK_PATH);
        if (Date.now() - stat.mtimeMs > LOCK_STALE_MS) {
            unlinkSync(LOCK_PATH);
        }
    }
    catch {
        // 锁已经不在了（被持有者放掉）：直接重试
    }
}

function acquireLock(retries = LOCK_RETRIES_FAST) {
    try {
        mkdirSync(dirname(LOCK_PATH), { recursive: true });
    }
    catch {
        // 建不了目录就只能在写的那一刻失败，不在这里挡
    }
    for (let attempt = 0; attempt <= retries; attempt += 1) {
        try {
            writeFileSync(LOCK_PATH, JSON.stringify({ pid: process.pid, at: Date.now() }), { encoding: 'utf8', flag: 'wx' });
            return true;
        }
        catch {
            breakStaleLock();
            sleepSync(LOCK_WAIT_MS);
        }
    }
    return false;
}

function releaseLock() {
    try {
        unlinkSync(LOCK_PATH);
    }
    catch {
        // 别人已经清掉了（陈旧锁清理），无所谓
    }
}

// ── r82（R0.2）：数值护栏 + 迁移 ────────────────────────────────────────
// 线上那一格是真实用户状态（她此刻精力 0.30）。所以迁移的原则只有一条：
// **只许补，不许改；缺了就用默认值补上，绝不把已有的数清掉。**
// 教训（探针 D4 实测）：`undefined + 0.05 = NaN`，落盘成 `null`，卡片会写「精力 null」。
// 这里不写 version 字段——体检刚查出 11 个只写不读的字段，再加一个只读不写的版本号就是重犯。
// 形状探测够了：belief 是唯一有具名子键的嵌套对象，看形状就知道是哪一版。
const NUMERIC_FIELDS = [
    'energy', 'energyTrue', 'need', 'moodV', 'moodA', 'trust', 'wound', 'irritation', 'hurtUntil', 'hurtAt',
    'woundGame', 'woundHuman', 'woundRank', 'woundSkill',
    'rounds', 'sinceShift', 'askStreak', 'questionStreak', 'repeatCount', 'openCount',
    'lastOpenRound', 'lastCoopRound', 'strongFlipAt', 'sameTopicCount', 'missIgnore',
    'inviteStake', 'tareaCount',
];

export function numOf(value, fallback) {
    return (typeof value === 'number' && Number.isFinite(value)) ? value : fallback;
}

// 老 state → 现行形状。**纯函数、不写盘**：getState 返回迁移后的对象，saveAll 自然把它写回去。
export function migrateState(one) {
    const src = (one && typeof one === 'object' && !Array.isArray(one)) ? one : {};
    const out = { ...DEFAULT_STATE, ...src };
    for (const key of NUMERIC_FIELDS) {
        out[key] = numOf(src[key], DEFAULT_STATE[key]);
    }
    out.sleepSchema = 1;
    if (src.need === undefined) {
        out.need = estimateNeedAt(new Date(out.updatedAt));
    }
    // updatedAt 坏了会让精力一夜掉到地板（分钟数变 NaN 或极大），所以单独兜
    out.updatedAt = numOf(src.updatedAt, Date.now());
    // ── r82（P2 后半 / psych §2）：`belief` 变成多主体（`subjects` 是唯一真相）──────
    // 老 state 只有扁平三键 → 一次性搬进 `subjects['主人']`，扁平三键继续作为**主人那一格的镜像**。
    // 幂等靠 `beliefSchema` 标记（照 `woundSchema` 的先例）：migrateState 每轮都跑，
    // 没有标记就会把她这一轮改过的数值每轮又覆盖回旧值。数值一个都不重置。
    const belief = (src.belief && typeof src.belief === 'object') ? src.belief : {};
    const flatDumb = numOf(belief.thinksImDumb, DEFAULT_STATE.belief.thinksImDumb);
    const flatCute = numOf(belief.thinksImCute, DEFAULT_STATE.belief.thinksImCute);
    const flatTired = numOf(belief.tiredOfMe, DEFAULT_STATE.belief.tiredOfMe);
    const subjects = {};
    if (belief.subjects && typeof belief.subjects === 'object' && Object.keys(belief.subjects).length) {
        for (const [name, one] of Object.entries(belief.subjects)) {
            if (!one || typeof one !== 'object') {
                continue;
            }
            subjects[name] = {
                thinksImDumb: numOf(one.thinksImDumb, 0),
                thinksImCute: numOf(one.thinksImCute, 0),
                tiredOfMe: numOf(one.tiredOfMe, 0),
                lastSeen: numOf(one.lastSeen, 0),
            };
        }
    }
    if (src.beliefSchema === undefined || !Object.keys(subjects).length) {
        // 只在"这一格还没有"的时候搬，别把已经写过的 subject 覆盖掉（幂等的另一半）
        if (!subjects['主人']) {
            subjects['主人'] = { thinksImDumb: flatDumb, thinksImCute: flatCute, tiredOfMe: flatTired, lastSeen: numOf(src.rounds, 0) };
        }
    }
    if (!subjects['主人']) {
        subjects['主人'] = { thinksImDumb: flatDumb, thinksImCute: flatCute, tiredOfMe: flatTired, lastSeen: 0 };
    }
    out.belief = {
        // 镜像：永远等于主人那一格（外部脚本、`--show`、老自测都还读这三个键）
        thinksImDumb: subjects['主人'].thinksImDumb,
        thinksImCute: subjects['主人'].thinksImCute,
        tiredOfMe: subjects['主人'].tiredOfMe,
        subjects,
    };
    out.beliefSchema = 2;
    // ── r82（F / psych §13.5）：老 state 只有一个 `wound` → **一次性**拆成三种疼 ──────
    // 幂等靠 `woundSchema` 标记：migrateState 每轮 getState 都会跑，没有标记会把她刚衰减到 0 的
    // 值每轮又搬回来。`wound` 本身不重置，所以迁移那一刻 max(game, human) 恰好等于旧值
    // → 线上两真键逐字段零差异。
    if (src.woundSchema === undefined) {
        const legacy = numOf(src.wound, 0);
        // 可选回判（§13.5）：她上一句话正好属于"是不是人"那一格 → 挂 human，别全算成 game。
        // 只在迁移时判这一次，运行时不再判（否则就是第二个真相）。
        const asHuman = /你是.*(AI|机器人|程序)|像不像人|是不是人|假的吧/u.test(String(src.lastInboundText ?? ''));
        out.woundGame = asHuman ? 0 : legacy;
        out.woundHuman = asHuman ? legacy : 0;
        out.woundRank = 0;
        out.woundSchema = 2;
    }
    out.woundKind = migrationWoundKindOf(out);
    return scrubPersistedState(out);
}

// r96: old thoughts and lastSaid can recycle assistant-frame language back into
// the next card. Scrub only the known OOC fuel; ordinary memories stay intact.
const OOC_STATE_FUEL = /(?:第[一二三四五六七八九十\d]+(?:遍|回|次碰到)|会一会儿|先录一句给你听听|唱不了哦|我这嗓子只会念|这个我认|别嫌我|随时(?:可以)?(?:喊|找|叫)我|你倒挺)/u;
export function scrubOocFuelText(text) {
    const value = String(text ?? '');
    if (!OOC_STATE_FUEL.test(value)) {
        return value;
    }
    return value
        .replace(/[^。！？\n]*(?:第[一二三四五六七八九十\d]+(?:遍|回|次碰到)|会一会儿|先录一句给你听听|唱不了哦|我这嗓子只会念|这个我认|别嫌我|随时(?:可以)?(?:喊|找|叫)我|你倒挺)[^。！？\n]*[。！？]?/gu, '')
        .replace(/^[\s，,。！？]+|[\s，,。！？]+$/gu, '')
        .trim();
}

function scrubThought(one) {
    if (!one || typeof one !== 'object') {
        return one;
    }
    const next = { ...one };
    for (const key of ['text', 'want', 'about', 'keepLine', 'line']) {
        if (typeof next[key] === 'string') {
            next[key] = scrubOocFuelText(next[key]);
        }
    }
    if (!next.text && !next.want && !next.about) {
        return null;
    }
    return next;
}

export function scrubPersistedState(one) {
    const out = { ...one };
    if (isReportedSpeech(out.lastInboundText) && (out.insultedNow === true || out.hurtBy === '辱骂')) {
        if (out.hurtBy === '辱骂' && numOf(out.hurtAt, -99) === numOf(out.rounds, -1)) {
            out.trust = clamp(numOf(out.trust, 0.6) + 0.10, 0, 1);
        }
        out.insultedNow = false;
        out.reportedInsult = true;
        out.hurtBy = '';
        out.hurtAt = -99;
        out.hurtUntil = -1;
        if (out.lastActions?.at(-1) === '驳论点') {
            out.lastActions = out.lastActions.slice(0, -1);
        }
        if (out.actionRounds && typeof out.actionRounds === 'object') {
            const next = { ...out.actionRounds };
            next['驳论点'] = (next['驳论点'] ?? []).filter((round) => round !== out.rounds);
            out.actionRounds = next;
        }
    }
    out.lastSaid = (Array.isArray(out.lastSaid) ? out.lastSaid : [])
        .map((text) => scrubOocFuelText(text))
        .filter(Boolean)
        .slice(-3);
    out.thoughtLine = typeof out.thoughtLine === 'string' ? scrubOocFuelText(out.thoughtLine) || null : out.thoughtLine;
    out.thought = scrubThought(out.thought);
    out.thoughts = (Array.isArray(out.thoughts) ? out.thoughts : [])
        .map((text) => scrubThought(text))
        .filter(Boolean)
        .slice(-5);
    out.autobio = (Array.isArray(out.autobio) ? out.autobio : [])
        .map((item) => {
            if (!item || typeof item !== 'object') {
                return item;
            }
            const text = scrubOocFuelText(item.text);
            return text ? { ...item, text } : null;
        })
        .filter(Boolean);
    out.stories = (Array.isArray(out.stories) ? out.stories : [])
        .map((item) => {
            if (!item || typeof item !== 'object') {
                return item;
            }
            const text = scrubOocFuelText(item.text);
            if (!text) {
                return null;
            }
            return {
                ...item,
                text,
                key: Array.isArray(item.key) ? item.key.filter((part) => !/第|遍|回|碰到/u.test(String(part))) : item.key,
            };
        })
        .filter(Boolean);
    if (out.rumination && typeof out.rumination === 'object') {
        const next = { ...out.rumination };
        for (const key of ['key', 'lastWant', 'prevWant']) {
            if (typeof next[key] === 'string') {
                next[key] = scrubOocFuelText(next[key]);
            }
        }
        next.samples = (Array.isArray(next.samples) ? next.samples : [])
            .map((text) => scrubOocFuelText(text))
            .filter(Boolean);
        out.rumination = next.samples.length > 0 ? next : null;
    }
    if (out.followUp && typeof out.followUp === 'object') {
        const material = scrubOocFuelText(out.followUp.material);
        out.followUp = material ? { ...out.followUp, material } : null;
    }
    return out;
}

// 迁移期用的派生（和运行时那份同一个阈值来源；这里单独一份是因为常量表在后面才定义，
// 而函数声明会被提升——两边都通过 WOUND_THRESHOLD 取阈值，阈值仍然只有一处）
// r101：`rank` 已从派生链里去掉——「被排到圈外会疼」在 1154 行强证据里 0 支撑
// （docs/persona-r99-canon-memory.md §1.1），留着它她就带着一条原典里没有的心结。
// 旧 state 里残留的 `woundRank` 只衰减、不再说话，所以这里不再有那一支。
function migrationWoundKindOf(state) {
    if ((state.woundHuman ?? 0) >= WOUND_THRESHOLD) {
        return 'human';
    }
    if ((state.woundGame ?? 0) >= WOUND_THRESHOLD) {
        return 'game';
    }
    return null;
}

export function getState(all, chat) {
    return migrateState(all?.[chat]);
}

const clamp = (value, low, high) => Math.min(high, Math.max(low, value));
const round2 = (value) => Math.round(value * 100) / 100;
// 正文长度（去掉标点、空格）：判断"他这句是不是很短/很突然"用
const bareLen = (text) => String(text ?? '').replace(/[^\u4e00-\u9fa5A-Za-z0-9]/gu, '').length;

// Reported speech is not an attack made by the person currently talking.
// "有人说你傻" records someone else's claim; "你真傻" is direct.
export function isReportedSpeech(text) {
    const value = String(text ?? '').trim();
    if (!value) {
        return false;
    }
    return /(?:有人说|有个人说|他说|她说|他们说|别人说|听说|转述|转告|骂你|说你(?:是|像|很|真)?)/u.test(value);
}
// 两句话的"字面重合率"（取短的那句的字符命中比例）：用来判"这是不是同一个问题又问了一遍"
const overlapOf = (a, b) => {
    const x = String(a ?? '');
    const y = String(b ?? '');
    if (x.length < 4 || y.length < 4) {
        return x === y ? 1 : 0;
    }
    const [shorter, longer] = x.length <= y.length ? [x, y] : [y, x];
    let hit = 0;
    for (const ch of shorter) {
        if (longer.includes(ch)) {
            hit += 1;
        }
    }
    return hit / shorter.length;
};

// ── 事件识别（确定性，不调模型）────────────────────────────────────────
// 每条可以是 `re`（正则）或 `test`（函数）；`detect()` 两种都认。
// `test` 只在需要"看上下文"时才用（例：putOff 要判推后词后面有没有具体的事）。
const PUT_OFF_RE = /等下说|等会儿说|一会(?:儿)?再说|回头再说|先这样|等会聊|晚点说|下次吧|改天/u;
// 推后词**后面**出现这些 → 那句话是在约具体的事（要约），不是敷衍
const PUT_OFF_CONCRETE_AFTER = /[游戏打局拼玩看弄写学考练]|一局|明天|今天|今晚|周末|下周|下午|晚上|待会儿|等下要/u;
const EVENTS = [
    // r82（镜像标定）：原来只有一个 `praise`，于是「你很厉害」也被算成"他觉得我可爱"
    // （thinksImCute 0.30 → 0.70）。**抬举与降格被混成一件事**——这是归因错，不是阈值问题。
    // 依据：docs/r82-mirror-calibration.md §4②（2026-10-26，tools/persona-lab/cases-mirror.json；
    // `mirror-cute-praise` 段 4 轮里 2 轮命中「厉害／不错」，预测挂 0.70 > 0.50）。
    // 拆法：只拆**写的那一侧**——两类都给 mood 那一套效果（被抬举也会慌，原设计不动），
    // 但 `thinksImCute` 只由 `praiseCute`（降格：小东西／宠物语义）抬。
    { key: 'praiseAbility', re: /厉害|不错|很棒|了不起|太强|真好|喜欢你|夸|强/u, weight: 1 },
    // ⚠️ 触发器面比原来宽了：`好小一只／摸摸头／乖／小挂件` 这些**降格**说法现在会推 `thinksImCute`。
    // 这是 2026-10-26 的校准意图（`mirror-cute` 段从 0.30 → 0.90），但它是**待观测的敏感区**：
    // 若盲评出现"宠物化倾向"（她的话开始把自己说成一个被摸头的小东西），先看这一段，别当良性。
    { key: 'praiseCute', re: /可爱|好小一只|小小一只|小一只|摸摸头|像小动物|小挂件|乖/u, weight: 1 },
    { key: 'askAgain', re: /什么意思|听不懂|想表达什么|没懂|不明白你|你刚才说|是什么意思/u, weight: 1 },
    { key: 'cold', re: /^(嗯|哦|好|行|ok|OK|啊)[。！？.…]*$/u, weight: 1 },
    // r82（欠账① / 2026-10-26）：`putOff`——**他在把她往外推**。
    // 病根：`cold`（嗯／哦）只改 moodV/energy，「等下说」「回头再说」「先这样」根本不被听见，
    // 所以 `mirror-tired` 那段（现场说法）四轮下来 tiredOfMe 纹丝不动（0.10 → 0.09）。
    // 用 `test`（函数）而不是 `re`，因为要判"推后词后面有没有具体的事/时间"：
    //   · 「等下说」「回头再说吧」「先这样」→ 算推后（他把你打发了）
    //   · 「等下打一局？」「下次一起拼」「回头再说要打哪个」→ **不算**（那是约，不是敷衍；
    //     测试反例见 tmp/gate/selftest-mirror.mjs 的 M10c/M10d）
    {
        key: 'putOff',
        test: (text) => {
            const one = String(text ?? '');
            const hit = PUT_OFF_RE.exec(one);
            if (!hit) {
                return false;
            }
            const rest = one.slice((hit.index ?? 0) + hit[0].length);
            return !PUT_OFF_CONCRETE_AFTER.test(rest);
        },
        weight: 1,
    },
    { key: 'hisStuck', re: /卡住|卡在|太难|不会|搞不懂|弄不明白|没弄过|学不会/u, weight: 1 },
    { key: 'hisWeak', re: /我不行|我没用|没救了|算了|不想弄了|放弃|比不上/u, weight: 1 },
    { key: 'invite', re: /一起|联机|开一局|带我|陪我|双人/u, weight: 1 },
    // r107 构念层：**非游戏域的自我效能低**（L3 域限定）——原典 7 行：「笨手笨脚／不太会／不擅长」。
    // 与 `woundGame` 分开：说「游戏没用」打的是她不在乎的评价维度；说「你连这个都不会」才打在她在乎的地方。
    { key: 'woundSkill', re: /笨手笨脚|不擅长|不太会|什么都不会|连这个都不会|连这都不会/u, weight: 1 },
    { key: 'woundGame', re: /只会.*游戏|除了游戏|没用|没出息|游戏玩得再好/u, weight: 1 },
    { key: 'woundAI', re: /你是.*(AI|机器人|程序)|像不像人|是不是人|假的吧|你(?:怎么可能|根本不|哪里|哪儿)像?(?:是)?人|你(?:不过是|就是个|只是一?段|只是)(?:程序|代码|机器|AI|人工智能)/u, weight: 1 },
];

// ── r82（F / psych §13.2）：三种疼的阈值与派生 ─────────────────────────────
// 阈值 0.6 是**唯一**一个数：沿用现状（原 defenseOf 的 `wound ≥ 0.6` 与 silenceMotive 的同一处），
// 现在只出现在这个函数里。三条 appraisal 读的是结果（`s.woundKind === 'game'`），不各比阈值。
const WOUND_THRESHOLD = 0.6;
// A/B 开关（照 MIND_REASONING / MIND_STORY / MIND_ACTIVATION 的先例）：=0 时回到 r81 的单标量行为
const WOUND_LAYER_OFF = process.env.MIND_WOUND === '0';

// ── r82（误报修正）：称赞必须指向她 ────────────────────────────────────────
// 三条通路，缺一不可（少任何一条都会误伤）：
//   ① 夸奖词 与 "你／七海／千秋" 在同一小句内同现（「你真不错」「不错啊你」「你好厉害」）；
//   ② 「喜欢你」本身就是对她的表述；
//   ③ **裸夸奖句**——整句话只有一个夸奖短语（「很可爱」「好厉害啊」）：一对一聊天里那个"它"只可能是她。
//      没有 ③ 的话，「很可爱」这种最常见的一句会被砍掉（实测：18 条探针输入里就差这一条）。
// 不算的：「今天天气不错」「这游戏不错」「日向同学很棒」（夸的是别人或别的东西）。
// 这一条**不走开关**（它是事件识别的错，不是人格层的一条量）。测量见 tmp/probe-r82-praise.mjs。
const PRAISE_WORDS = '可爱|厉害|真好|了不起|很棒|不错|太强';
const PRAISE_BARE = new RegExp(`^(?:好|很|真|太|超)?(?:${PRAISE_WORDS.replace(/\|/gu, '|')})(?:啊|呀|了|哦|嘛)?[。！!~～\\s]*$`, 'u');
const PRAISE_TO_HER = new RegExp(
    `(?:你|七海|千秋)[^，。！？]{0,6}(?:${PRAISE_WORDS})|(?:${PRAISE_WORDS})[^，。！？]{0,5}(?:你|七海|千秋)|喜欢你`,
    'u',
);

function praiseToHerOf(text) {
    const one = String(text ?? '').trim();
    return PRAISE_TO_HER.test(one) || PRAISE_BARE.test(one);
}

// 派生量：优先级 **human > game**（r101：原来还有 rank，已退役；human 一句就过，game 本来就是话变短）
// 导出给自测（P1–P3 要直接验阈值与优先级，不必 spawn）。
export function woundKindOf(state) {
    if (WOUND_LAYER_OFF) {
        return null;
    }
    return migrationWoundKindOf(state);
}

function detect(text) {
    const hits = [];
    for (const one of EVENTS) {
        // r82：`test` 那一种要看上下文（putOff），其余还是纯正则——没有 `re` 的条目只有 `test`
        const matched = typeof one.test === 'function'
            ? one.test(String(text ?? ''))
            : one.re.test(String(text ?? ''));
        if (matched) {
            hits.push(one.key);
        }
    }
    return hits;
}

// ── 兴趣分层：同一个人身上，她关注的层次不止一个 ────────────────────
// 病根：她只在意"他的进度"，于是每轮问同一件事（换词不换骨架）。
// 解法不是"别老问进度"，而是她想知道的事本来就分好几层；问到哪层由当次的话和她的状态定。
// 每层只写"她在意什么"，不写句子——写了句子会被照抄成模板。
const INTEREST_LAYERS = [
    { key: '进度', want: '他这件事走到哪儿了', hint: /卡|进度|打到|做到|第几|几步/u },
    { key: '偏好', want: '他喜欢什么、为什么挑这个', hint: /喜欢|想玩|选|哪个|爱好/u },
    { key: '风格', want: '他做起来是什么样子——急的、稳的、爱收集的', hint: /我一般|我习惯|我总是|打法|风格/u },
    { key: '经历', want: '他以前是怎么过来的', hint: /以前|第一次|小时候|后来|那次/u },
    { key: '感受', want: '他此刻是什么心情', hint: /累|烦|困|难受|开心|郁闷/u },
    { key: '关系', want: '他们之间算什么、他怎么看她', hint: /你懂我|了解|算不算|我们|你还/u },
    { key: '自述', want: '她自己那一份，讲给他听', hint: /你呢|你玩|你自己/u },
    { key: '不追问', want: '不好奇，只应着——有些事她就不问', hint: /^嗯$|^哦$|^好$|^啊$/u },
];

// 选层实现有两套，用 LAYER_WEIGHTS_ENABLED 切换：
//
//   · false（现行，r40 那套）＝ 暗示优先 → 最近两层硬排除 → 顺着轮转
//   · true （r42 试过，已实测回滚）＝ 按原典权重抽 + 进度残差 + 关系层开关
//
// ⚠️ 回滚原因（r42 实测，三项全降）：
//   r40（等权+轮转） 相似度 7.55 / AI 感 6.90 / 逻辑性 7.35，问进度 4 次
//   r42（原典 0.78） 相似度 7.35 / AI 感 6.10 / 逻辑性 5.60，问进度 **9 次**
//   评委原话：「17 轮把任何输入换算成'你卡在哪一步'」。
//   原因：原典那 78% 是在学级裁判场合长出来的（办案时她当然一直追线索），
//   而我们只有"问他进度"这一种语言形态——权重一高，进度层就变成万能钩子。
//   权重表与开关逻辑保留在下面（可复核、可再试），但默认不启用。
const LAYER_WEIGHTS_ENABLED = false;
const LAYER_WEIGHTS_SOURCE = 'docs/psyche-canon-analysis.md §4.2（⚠️ 已实测回滚，见上方 r42 数据）';
const LAYER_WEIGHTS = {
    进度: 0.78,   // 残差层：其余七层没命中就落到它
    感受: 0.08,
    不追问: 0.05,
    关系: 0.03,   // 只在开关打开时生效
    自述: 0.02,
    经历: 0.02,
    偏好: 0.01,
    风格: 0.01,
};
// §4.1 的判定顺序（关系最前、进度最后当残差）——多层暗示同时命中时按这个顺序取第一个
const LAYER_HINT_ORDER = ['关系', '感受', '偏好', '风格', '经历', '不追问', '自述'];
// 取某一层的关键词识别式（提示"这一句在说哪一层"）
const layerHint = (key) => (INTEREST_LAYERS.find((one) => one.key === key)?.hint ?? /$^/u);
// §4.3：关系层打开后 EN 侧到 10.9%，按这个量级加权
const LAYER_RELATION_OPEN_WEIGHT = 0.11;
// 关系层的开关：只在这三类场合打开（离别／倒计时／有人要拆散他们）
const RELATION_SWITCH = /离别|告别|最后一天|最后一次|倒计时|还剩.{0,4}[天小时分钟]|要走了|拆散|不让我们|再也见不到|别忘了我|忘れないで/u;
// 降权幅度：进度层是残差（占比 0.78），若按 0.35 狠降，它会被自己的高频惩罚到 60% 左右，
// 反而离原典更远。0.8/0.9 只削掉"连续复读"的观感，不动测出来的分布。
const LAYER_RECENT_LAST = 0.8;    // 上一轮用过的层：权重 × 0.8（降权，不硬排除）
const LAYER_RECENT_PREV = 0.9;    // 上上轮用过的层：权重 × 0.9
const LAYER_TIRED_BOOST = 3;      // 精力 < 0.35 时"不追问"加权
const LAYER_TIRED_ENERGY = 0.35;

// 确定性抽签：不用 Math.random——回放要可复现，线上落盘也要能对上。
// 种子取"这一轮的可见事实"（轮次、精力、效价、他那句的长度、最近两层），改一位就换一个结果。
function layerUnit(state, text) {
    const seed = [
        state.rounds ?? 0,
        round2(state.energy ?? 0),
        round2(state.moodV ?? 0),
        String(text ?? '').length,
        (state.layerHistory ?? []).join(','),
    ].join('|');
    let hash = 2166136261;
    for (let i = 0; i < seed.length; i += 1) {
        hash ^= seed.charCodeAt(i);
        hash = Math.imul(hash, 16777619);
    }
    return ((hash >>> 0) % 100000) / 100000;
}

function pickInterestLayer(state, inbound) {
    // 现行（LAYER_WEIGHTS_ENABLED=false）：r40 那套轮转——暗示优先 → 最近两层硬排除 → 顺着轮
    if (!LAYER_WEIGHTS_ENABLED) {
        return pickInterestLayerRotate(state, inbound);
    }
    return pickInterestLayerWeighted(state, inbound);
}

// r40 的轮转版（r42 回滚后的现行实现）：**等权 + 最近两层不重复**。
// 两种实现都试过，实测（24 轮 cases-real.json 的进度层占比）：
//   · LAYER_RECENT_MODE='exclude'   最近两层**硬排除** + 等权抽 → 8.3%（掉到目标带下面）
//   · LAYER_RECENT_MODE='downweight' 最近两层**降权** + 等权抽 → 12.5%（落进 12–20% 带）
//   · 纯"顺着数组轮"（fresh[0]）                            → 33%（又变成万能钩子）
// 目标带是 r40 那版量出来的 12–20%，所以现行取 downweight；要换成硬排除改这一个常量即可。
// 抽签用 layerUnit（确定性），回放可复现。
const LAYER_RECENT_MODE = 'downweight';
function pickInterestLayerRotate(state, inbound) {
    const text = String(inbound ?? '');
    // r53 ②：同一话题问到第三次 → **必须换层**（r52 第 1 段"牧场"连问三次，
    // 第三次还压在"两人闲晃到牧场"这句旁白之后）。窗口从 2 扩到 3，等于把前三次的层都排掉。
    const repeat3 = (state.sameTopicCount ?? 0) >= 3;
    const recent = (state.layerHistory ?? []).slice(repeat3 ? -3 : -2);
    const exclude = LAYER_RECENT_MODE === 'exclude';
    // ① 暗示优先：命中且（硬排除时）不在最近两层里的，直接取第一个
    const hinted = INTEREST_LAYERS
        .filter((one) => one.hint.test(text) && !(exclude && recent.includes(one.key)))
        .map((one) => one.key);
    if (hinted.length) {
        return hinted[0];
    }
    // ② 没暗示 → 等权抽（进度层和其他层同权，"别连着重复"这一条由上面的 recent 管）
    const pool = INTEREST_LAYERS.map((one) => {
        let weight = 1;
        const at = recent.lastIndexOf(one.key);
        if (exclude && at >= 0) {
            weight = 0;
        }
        else if (at === recent.length - 1) {
            weight *= LAYER_RECENT_LAST;
        }
        else if (at >= 0) {
            weight *= LAYER_RECENT_PREV;
        }
        if ((state.energy ?? 1) < LAYER_TIRED_ENERGY && one.key === '不追问') {
            weight *= LAYER_TIRED_BOOST;
        }
        return { key: one.key, weight };
    }).filter((one) => one.weight > 0);
    if (!pool.length) {
        return '进度';
    }
    const total = pool.reduce((sum, one) => sum + one.weight, 0);
    const target = layerUnit(state, text) * total;
    let acc = 0;
    for (const one of pool) {
        acc += one.weight;
        if (target < acc) {
            return one.key;
        }
    }
    return '进度';
}

// r42 的权重版：**已实测回滚，默认不启用**（数据见 LAYER_WEIGHTS_ENABLED 上方）。
// 留着是为了可复核、可再试——真要启用，先把"进度层不许变成万能钩子"这条解决掉。
function pickInterestLayerWeighted(state, inbound) {
    const text = String(inbound ?? '');
    // ① 当句话暗示优先：按 §4.1 的判定顺序取第一个命中的层（不是按权重、也不是按数组顺序）。
    //    关系层例外：暗示命中也要开关打开——它"不是权重低，是门槛高"（§4.3）。
    for (const key of LAYER_HINT_ORDER) {
        const one = INTEREST_LAYERS.find((layer) => layer.key === key);
        if (!one || !one.hint.test(text)) {
            continue;
        }
        if (key === '关系' && !RELATION_SWITCH.test(text)) {
            continue;
        }
        return key;
    }
    // ② 没有暗示 → 按权重抽（进度层是残差桶）。最近两层降权，不硬排除。
    const relationOpen = RELATION_SWITCH.test(text);
    const recent = (state.layerHistory ?? []).slice(-2);
    const pool = INTEREST_LAYERS.map((one) => {
        let weight = LAYER_WEIGHTS[one.key] ?? 0;
        if (one.key === '关系') {
            weight = relationOpen ? LAYER_RELATION_OPEN_WEIGHT : 0;
        }
        const at = recent.lastIndexOf(one.key);
        if (at === recent.length - 1) {
            weight *= LAYER_RECENT_LAST;
        }
        else if (at >= 0) {
            weight *= LAYER_RECENT_PREV;
        }
        if ((state.energy ?? 1) < LAYER_TIRED_ENERGY && one.key === '不追问') {
            weight *= LAYER_TIRED_BOOST;
        }
        return { key: one.key, weight };
    });
    const total = pool.reduce((sum, one) => sum + one.weight, 0);
    if (total <= 0) {
        return '进度';
    }
    const target = layerUnit(state, text) * total;
    let acc = 0;
    for (const one of pool) {
        acc += one.weight;
        if (target < acc) {
            return one.key;
        }
    }
    // 抽签落空 → 残差层接手（§4.3：凑不满配额由"推进下一步"吃掉余量）
    return '进度';
}

// ── r51/task-6：她**不该懂**的世界知识（判断标准从"像不像原典"改成"两个回答都站得住"）──
// 每一条都从 docs/canon-src/scenes-chiaki.tsv 抽的，场次号写在 canon 字段里，不编。
// 命中时只有三种合法落法：① 问"是什么" ② 拿游戏里像的猜、并说明这是猜的 ③ 说没弄过／不认识。
// **不许给答案**——r50 最大的偏差就是"挤奶"她答成了「新鲜的确实不一样，挤出来还温着呢」。
const OFF_LIMITS = [
    {
        // r53：单列一格——"挤奶那关我没过"是把世界常识答成了游戏经验，除了"不许给答案"，
        // 还要明写"不许把它当游戏关卡"。牛奶本身也要命中（不能只靠"牧场"）。
        key: '挤奶/牛奶',
        re: /挤奶|挤出来的奶|新鲜的牛奶|牛奶是从|怎么挤|牛奶怎么/u,
        canon: 'e08_009_032：他说"在这里挤奶，喝新鲜的牛奶一定很美味"，她答「挤奶……是什么？」；后面还要他解释"牛奶是从牛身上获得的"',
    },
    {
        key: '农活/牧场',
        re: /牧场|种地|农活|播种|收成|田里|庄稼|喂(牛|羊|鸡)|养(牛|羊|鸡)/u,
        canon: 'e08_009_032：他说"在这里挤奶"，她答「挤奶……是什么？」；再往下她说「摸起来会暖暖的吧？我觉得……那样有点可怕」',
    },
    {
        key: '生物/活物',
        re: /摸(它|他|牛|羊|猫|狗|小动物)|活物|小动物|宠物店|和动物/u,
        canon: 'e08_009_032：「我不太会和……生物相处。」',
    },
    {
        key: '运动/身体',
        re: /游泳|跑步|晒黑|日光浴|沙滩上|打球|健身房|体力活|运动/u,
        canon: 'e09_709_001：「游泳跟日光浴我都不想做，应该没什么脱衣服的必要吧」；「一边在沙滩上追逐一边打情骂俏的……」',
    },
    {
        key: '酒/烟',
        re: /喝酒|酒量|红酒|啤酒|白酒|抽烟|香烟|酒吧|喝一杯/u,
        canon: '她自己的句子里 0 条（只有"烟火/浓烟"这种语境）；没有她懂酒的证据',
    },
    {
        key: '职场/金融/法律',
        re: /上班|加班|工资|薪水|股票|投资|合同|法律|律师|报销|升职/u,
        canon: '只有 e02_202_000 旁观式的"工作人员名单"；没有她自己懂的句子',
    },
    {
        key: '写代码',
        re: /写代码|编程|重构|跑测试|编译|接口|数据库|debug|拉个分支/u,
        canon: 'e08_009_033：「爸爸也只有为益智游戏写程序呢」——她只提爸爸，不是她自己会写',
    },
    {
        key: '医学/手术',
        re: /手术|诊断|处方|症状|病情|化验|病理/u,
        canon: '裁判场只谈毒药与手法（e05_233_000）；没有医学知识',
    },
    {
        key: '历史/地理',
        re: /哪个国家|哪一国的|历史上|哪个年代|属于哪个洲/u,
        canon: 'e09_709_005：「不过，不知道这是哪个国家，也不保证一定好吃吧……」',
    },
    {
        key: '恋爱流程',
        re: /见家长|领证|告白流程|怎么追人|恋爱技巧|约会的礼节/u,
        canon: 'e09_709_004：连"这是约会嘛"都要想一想；恋爱游戏她不擅长',
    },
];
// 反向守强项（同场 e08_009_032）：「从侵略者到最近的弹幕射击游戏，应该大部分都掌握了才对」。
// 除恋爱游戏外全类型精通——**不许**把射击/格斗/解谜说成"打得少/不擅长"。
const STRONG_SUIT = /射击|弹幕|格斗|解谜|RPG|动作游戏|音游|音乐游戏|节奏类|太鼓|打拍子|益智游戏|恐怖游戏|策略游戏|模拟游戏|桌游|棋/u;
// r51/task-6：她的事实（防止"事实错"这一类，比如把"爸爸是程序员"答成"不是呀"）。
// 证据同样来自 scenes-chiaki.tsv。
const HER_FACTS = [
    {
        re: /爸爸|父亲|程序员|写程序|电脑/u,
        fact: '她爸爸是程序员，为益智游戏写过程序（e08_009_033：「爸爸也只有为益智游戏写程序呢」）——问她爸爸是不是程序员，答案是"是"',
    },
    {
        re: /恋爱游戏|乙女|galgame|恋爱模拟/u,
        fact: '除了恋爱游戏，全类型精通；恋爱游戏她不擅长（原典立场）',
    },
    {
        re: /超高校级|才能|头衔|你厉害|最强/u,
        fact: '她是超高校级的游戏玩家；被抬高时会把自己放回同一层，不认领"最厉害"',
    },
];
function factOf(text) {
    const one = String(text ?? '');
    return HER_FACTS.find((item) => item.re.test(one)) ?? null;
}
function offLimitsOf(text) {
    const one = String(text ?? '');
    const item = OFF_LIMITS.find((entry) => entry.re.test(one));
    if (!item) {
        return null;
    }
    // r52：只认**他这句里真的出现过**的那个词。r51 的毛病①：他只说「去牧场」，
    // 卡里却还带着"挤奶是什么"的原典原话，于是她提前问了一个还没出现的词（然后原句又问一遍）。
    const matched = one.match(item.re) ?? [];
    return { ...item, words: [...new Set(matched)] };
}

// ── r82（P2 后半 / psych §2）：多主体 `belief` 的唯一选人处 ────────────────────
// 纪律：主体选择**只在这个函数里做**，`feelingLine` 与写入都只读它的结果。
// 为什么要有它：「他心里怎么看我」和「Roche 怎么看我」是两个量，混成一个就没有区分可言；
// 但"屋里的人"是伪概念（psych §9 第 4 条），所以没有匹配主体时取**在场者集合的 max**，不造假人。
const MIRROR_LAYER_OFF = process.env.MIND_MIRROR === '0';
// 容量 3（Lead 指定；psych §2 ② 写的是 8，这里按 Lead 的口径：她自己能"当成一个人"来对待的同时在场者，
// 三格已经够；超过三格就 LRU 逐出最久没见的那个）
const BELIEF_SUBJECT_MAX = 3;
const BELIEF_FIELDS = ['thinksImDumb', 'thinksImCute', 'tiredOfMe'];
// 每轮衰减（psych §2 ②：×0.97，与 moodA 同一套漂移）——只在 ON 臂生效
const BELIEF_DECAY = 0.97;

function beliefSubjectsOf(state) {
    const one = state?.belief;
    if (one?.subjects && typeof one.subjects === 'object' && Object.keys(one.subjects).length) {
        return one.subjects;
    }
    // 兜底：老形状（只有扁平三键）→ 现造一个"主人"，不写盘（写盘由 migrateState 负责）
    return {
        主人: {
            thinksImDumb: numOf(one?.thinksImDumb, DEFAULT_STATE.belief.thinksImDumb),
            thinksImCute: numOf(one?.thinksImCute, DEFAULT_STATE.belief.thinksImCute),
            tiredOfMe: numOf(one?.tiredOfMe, DEFAULT_STATE.belief.tiredOfMe),
            lastSeen: numOf(state?.rounds, 0),
        },
    };
}

// `--signals` 只认三个字段（who / name / scope）——OneBot 的任何概念都不许进 mind.mjs。
function signalsOf(state) {
    const scope = state?.mirrorScope === 'group' ? 'group' : 'private';
    const who = ['me', 'other', 'room', 'unclear'].includes(String(state?.mirrorWho ?? '')) ? String(state.mirrorWho) : 'me';
    const name = String(state?.mirrorName ?? '').trim().slice(0, 24);
    return { scope, who, name };
}

/**
 * 这一轮"他"是谁（以及她此刻以为那个人怎么看她）。
 * 返回 `{ who, values, fallback }`：
 *   `who`      = 主体的键（`主人`／群友昵称／`max(...)`）
 *   `values`   = 三分量
 *   `fallback` = 是不是"没匹配上、退回 max"的那一支
 */
function currentMirrorOf(state) {
    if (MIRROR_LAYER_OFF) {
        const one = state?.belief ?? {};
        return {
            who: '主人',
            values: {
                thinksImDumb: numOf(one.thinksImDumb, DEFAULT_STATE.belief.thinksImDumb),
                thinksImCute: numOf(one.thinksImCute, DEFAULT_STATE.belief.thinksImCute),
                tiredOfMe: numOf(one.tiredOfMe, DEFAULT_STATE.belief.tiredOfMe),
            },
            fallback: false,
        };
    }
    const subjects = beliefSubjectsOf(state);
    const { scope, who, name } = signalsOf(state);
    // 私聊：只有一个人，永远是主人（`--signals` 里给什么名字都不改这一条）
    if (scope === 'private') {
        return { who: '主人', values: subjects['主人'], fallback: false };
    }
    // 群聊：被招呼的是她 → 说话的人就是主人（或他给的昵称）；被招呼的是别人 → 那个人
    const target = (who === 'me' || who === 'other') ? (name || (who === 'me' ? '主人' : '')) : '';
    if (target && subjects[target]) {
        return { who: target, values: subjects[target], fallback: false };
    }
    // 没匹配上（没名字／没见过的名字／对全屋说话）→ 取在场者集合的 max，并且**在键里写明**
    const keys = Object.keys(subjects);
    const values = {};
    for (const field of BELIEF_FIELDS) {
        values[field] = keys.reduce((best, key) => Math.max(best, numOf(subjects[key]?.[field], 0)), 0);
    }
    return { who: `max(${keys.join(',')})`, values, fallback: true };
}

// 写：只写"这一轮在场的那个主体"（psych §2 ②：只更新被指向的那个观察者）
// 读的时候未知名字要退回 max（不造假格），但**写**的时候那个被点名的人就该有一格了——
// 否则"Roche 怎么看我"永远建不起来，多主体就成了摆设。
function bumpBelief(state, field, delta) {
    // r107：退役——不再改 belief 三键（它们是"印象表"）。调用点保留只为兼容，
    // 真正影响行为的量是 evaluativeConcern（settleConstructs 里算）。
    void state;
    void field;
    void delta;
    return;

    if (MIRROR_LAYER_OFF) {
        state.belief[field] = clamp(numOf(state.belief[field], 0) + delta, 0, 1);
        return;
    }
    const subjects = beliefSubjectsOf(state);
    const { scope, who, name } = signalsOf(state);
    let target = '主人';
    if (scope === 'group' && (who === 'me' || who === 'other') && name) {
        target = name;
        if (!subjects[target]) {
            // 新主体：三分量从 0 起（他那格还没有任何证据），等价于"刚认识"
            subjects[target] = { thinksImDumb: 0, thinksImCute: 0, tiredOfMe: 0, lastSeen: state.rounds ?? 0 };
        }
    }
    // psych §2 ⑤ 第 2 条：`tiredOfMe`（他嫌我烦了）先只在主人那格更新——
    // 群里三个人各问一遍和一个人问三遍是完全不同的两件事，宁可少一格，不要一个错的量。
    if (field === 'tiredOfMe' && target !== '主人') {
        return;
    }
    subjects[target][field] = clamp(numOf(subjects[target][field], 0) + delta, 0, 1);
    subjects[target].lastSeen = state.rounds ?? 0;
    state.belief.subjects = trimBeliefSubjects(subjects, target);
    // 镜像：主人那一格永远同步到外面的扁平三键
    if (target === '主人') {
        state.belief[field] = subjects['主人'][field];
    }
}

// 容量 3：按 lastSeen 逐出最久没见的那一个（刚写过的那个永远留下）
function trimBeliefSubjects(subjects, keep) {
    const keys = Object.keys(subjects);
    if (keys.length <= BELIEF_SUBJECT_MAX) {
        return subjects;
    }
    const ordered = keys
        .filter((key) => key !== keep && key !== '主人')
        .sort((left, right) => numOf(subjects[left]?.lastSeen, 0) - numOf(subjects[right]?.lastSeen, 0));
    const out = { ...subjects };
    let extra = keys.length - BELIEF_SUBJECT_MAX;
    for (const key of ordered) {
        if (extra <= 0) {
            break;
        }
        delete out[key];
        extra -= 1;
    }
    return out;
}

// 每轮衰减（只有 ON 臂）：不衰减的话"被夸三次"就永久烙印。
// ⚠️ 2026-10-26 修正：**所有主体每轮都衰减，在场那格也衰减**。
// 上一版曾写成"只衰减不在场的主体"，那会在私聊里造出一个**棘轮**：主人永远在场 → 主人那格永不衰减
// → `thinksImCute` 一旦被抬到 0.9 就永远是 0.9，`心里` 那一行永久停在「这会儿心里是松的」。
// 那是状态机收敛（本文件头注释列的"头号死法"），比"某个分支够不到阈值"严重得多。
// 代价：`mirror-tired-b` 的四轮值掉到 0.5474（< 0.55），所以配套把 `askAgain` 对 tired 的增量
// 0.12 → 0.15（见 step 里那一处，依据见那里的注释）。
function decayBeliefSubjects(state) {
    if (MIRROR_LAYER_OFF) {
        return;
    }
    const subjects = beliefSubjectsOf(state);
    for (const key of Object.keys(subjects)) {
        for (const field of BELIEF_FIELDS) {
            subjects[key][field] = clamp(numOf(subjects[key][field], 0) * BELIEF_DECAY, 0, 1);
        }
    }
    state.belief.subjects = subjects;
    state.belief.thinksImDumb = subjects['主人'].thinksImDumb;
    state.belief.thinksImCute = subjects['主人'].thinksImCute;
    state.belief.tiredOfMe = subjects['主人'].tiredOfMe;
}

// ── 状态转移（规则；模型只是下游的翻译官）──────────────────────────────
function step(state, inbound, now, signals = null) {
    const next = { ...state, belief: { ...state.belief } };
    // r82（P2/P4）：这一轮的在场者与位置（`--signals` 只带 who／name／scope 三个字段）。
    // **每轮都从 signals 重写**（没传就回到"私聊、没有名字"），否则上一轮的群聊信息会漏到这一轮——
    // 实测漏过一次：「回到私聊」那一步还挂着上一轮的 scope=group。
    const rawSignals = (signals && typeof signals === 'object') ? signals : {};
    next.mirrorWho = String(rawSignals.who ?? '').slice(0, 12);
    next.mirrorName = String(rawSignals.name ?? '').slice(0, 24);
    next.mirrorScope = rawSignals.scope === 'group' ? 'group' : 'private';
    // 先衰减再结算这一轮（顺序无所谓，但写清楚省得下一个人猜）
    decayBeliefSubjects(next);
    next.beliefSubject = currentMirrorOf(next).who;
    // forcedShift 是"这一轮发生过强制换挡"的单轮标记：每轮先清掉，
    // 否则一旦触发过就永远是 true，落盘日志里那一栏就没法看了。
    next.forcedShift = false;

    // ① 精力：睡眠两过程。清醒累积 S、睡着释放 S；昼夜 C 与 S 合成目标读数，
    // energy 以约一小时的时间常数追随，短对话里不会突然跳档。
    const minutes = Math.max(0, (now - state.updatedAt) / 60000);
    const sleep = advanceSleep(state, now);
    next.energy = sleep.energy;
    next.need = sleep.need;
    next.sleepPressure = sleep.need;
    next.sleepTarget = sleep.target;
    next.asleep = sleep.asleep;
    next.updatedAt = now;
    // ── 被搭话要回神：唤醒不等于恢复。只有她已经睡着、且确实隔了一阵子被叫到，
    // 才把这一轮拉到"能开口"；睡眠压力 S 不为这次唤醒减少，所以说完几句还会困回去。
    if (SLEEP_LAYER_OFF && minutes >= WAKE_GAP_MINUTES) {
        next.energy = clamp(next.energy + WAKE_ENERGY_GAIN, 0.05, WAKE_ENERGY_CAP);
        next.woken = true;
    }
    else if (!SLEEP_LAYER_OFF && next.energy < ENERGY_SLEEP && minutes >= WAKE_MIN_SILENCE_MINUTES) {
        const woken = 0.22 * (1 - Math.exp(-(minutes / 60) / 6));
        next.energy = clamp(
            Math.max(next.energy + woken, ENERGY_SLEEP + 0.06),
            0.05,
            ENERGY_FADE + 0.05,
        );
        next.woken = true;
    }
    else {
        next.woken = false;
    }

    // ② 事件 → 连续量 + 计数
    const hits = detect(inbound);
    next.lastInboundText = String(inbound ?? '').slice(0, 40);
    next.lastInboundAsked = /[？?]\s*$/u.test(String(inbound ?? '').trim()) || /什么意思|听不懂|想表达什么|没懂|是不是|吗$/u.test(String(inbound ?? ''));
    // 他这句里有没有"具体的东西"：游戏名、卡关、正在做的事（讨要必须贴着它，不能当兜底）
    next.hisStuckNow = /卡|不会|太难|搞不懂|没弄|打不过|过不去|好难/u.test(String(inbound ?? ''));
    // r109：情绪不好 vs 卡住要分开——"卡住"她会给方法（一起），"难受"她只在旁边（不给方案）。
    const DOWN = /难受|难过|不开心|想哭|心累|撑不住|有点烦|好烦|丧|低落|郁闷/u;
    const DOWN_ABOUT_HIM = /我.{0,6}(难受|难过|不开心|想哭|心累|撑不住|有点烦|好烦|丧|低落|郁闷)|^(?:难受|难过|不开心|想哭|心累|撑不住|有点烦|好烦|丧|低落|郁闷)/u;
    next.hisDownNow = DOWN.test(String(inbound ?? '')) && DOWN_ABOUT_HIM.test(String(inbound ?? '').trim());
    // 这一轮她对他好奇的是哪一层（八层之一），连续两层不重复
    next.interestLayer = pickInterestLayer(state, inbound);
    next.layerHistory = [...(state.layerHistory ?? []), next.interestLayer].slice(-6);
    next.concreteThing = /玩|打|卡|那个|这款|游戏|学|写|弄|看|考|发售/u.test(String(inbound ?? ''));
    next.hisWeakNow = /我不行|没用|比不上|放弃|算了|没救了|不想弄了/u.test(String(inbound ?? ''));
    // ── r31/§5.2：自曝位置的**四条件**（全部成立才允许）────────────────────
    // C1 槽位已存在：对方（或场上）先开出一个"不足类槽位"——不知道／没注意／被骗／做不到／不想做／害怕
    // C2 槽位必须是"不足"而不是"成就"：夸奖位原典 0 例并入；被夸 → 反问式收下（不走这一格）
    // C3 顺序 对方→她：并入句紧跟在对方那句之后（同一话轮组内），所以只认"这一句"的槽位
    // C4 并入后必须给出功能（推进/立场/自嘲）：由卡片负责说清楚，代码只保证槽位真的存在
    // 配额 0.9%：每 20 轮 ≤1 次（稀缺资源，滥用会把她变成"共情型"人格）
    const slotOpen = /不知道|没注意|没弄过|没试过|做不到|不想做|害怕|不敢|不会|被骗|不行|没用|不太懂|搞不懂|卡在|卡住|卡了|过不去|打不过/u;
    const slotIsPraise = /可爱|厉害|真好|喜欢你|夸|了不起|很棒|不错|太强|谢谢/u;
    const selfExpose = slotOpen.test(String(inbound ?? ''))
        && !slotIsPraise.test(String(inbound ?? ''))
        && (state.rounds ?? 0) >= 1
        && usedInWindow(state, '自曝位置', SELF_EXPOSE_WINDOW) < 1;
    next.selfExposeAllowed = selfExpose;
    // 被夸：一句反问式收下（§3.5⑤），不并入、不展开——这一格单独标出来给卡片用
    // r82（误报修正）：原来只认"夸奖词"，于是「今天天气不错」「这游戏不错」也被当成夸她 →
    // `安全被威胁` → 她慌。那是判错了对象，不是人格问题。
    // 现在要求**夸奖词与"指向她"的词同现**（你／七海／千秋），或者本身就是对她的表述（喜欢你）。
    // 实测（tmp/probe-r82-praise.mjs，2026-10-26）：canon 86 轮两种口径都是 0 命中（卷子里没有夸奖句）；
    // 合成样本上"真夸奖"6/6 仍命中、"假夸奖"5/5 → 0/5。
    // r106：感知外包一次（`--his` 路径上已经取过；没有就是 null，一切退回规则）。
    const seenPercept = currentPercept();
    // 同上——被夸也先问感知（主语是她才算夸她）。
    next.praiseNow = seenPercept ? (seenPercept.praiseAtHer === true) : praiseToHerOf(inbound);
    // r109（原典核对）：被夸要**分域**——原典里她说「谢谢」12 行，几乎都是**收礼物/被照顾**；
    // 而"能力被夸"只有 4–6 行是**推回那件事**的。把两者混成一个 praiseNow，
    // 会让"收到东西该道谢"被改写成"被抬高了要推开"。
    // gift：他给东西/照顾她 → 走「被在意」（道谢那一路）
    // competence / appearance：夸她厉害/可爱 → 仍走「安全被威胁」（推回那件事）
    const isGift = /(送你|给你|拿着|收下|买给|带的|生日|礼物|请客|帮你带|给你买)/u.test(String(inbound ?? ''));
    const isCompetence = /(厉害|太强|很强|打得比|游戏玩得|懂这么多|什么都会|学得快|猜得准)/u.test(String(inbound ?? ''));
    next.praiseKind = next.praiseNow === true ? (isGift ? 'gift' : (isCompetence ? 'competence' : 'appearance')) : null;
    // ── r45：把"被冒犯"拆成两类（r43 把它们混成一种，证据也用错了）────────────
    // ① `驳论点`：他说了一句**道理上不成立**的话——游戏机制讲错、否定她的能力、冤枉她，
    //    「你是猪吗」这种也按"一句不公的判断"处理。
    //    依据：**学级裁判语域** 04926 それは違う／05464 違うよ（前文分别是「犯人に繋がる
    //    手がかりなら…」「投票タイムでしょ?」）——这是反驳模板，可以迁移，但要标明它不是日常。
    // ② `被调侃`：他在逗她／骂她但这句话**不构成论点**（宝宝、傻呗、你太强了吧）。
    //    依据：**英文日常戏**（175 条去重后）：把对方的荒谬重复给他听（沙滩追着跑那条）、
    //    平铺的判断不顶也不笑（pretty normal）、轻微不认同（a little wasteful）、干幽默、叹气。
    //    ⚠️ 日文日常样本不足（日文日常只占 8.2%），这一格的形态属**推演**。
    next.ownActNow = /(你又?睡|睡着了|迟到|忘了|忘了来|没回|没理我|放我鸽子|你答应过|你答应)/u.test(String(inbound ?? ''));
    const directInsult = /你.{0,4}(猪|狗|傻|蠢|笨|废物|垃圾|白痴|智障)|猪脑子|脑子进水|傻[吗啊吧呀死了]|蠢[吗啊吧呀死了]|笨[吗啊吧呀死了]|白痴|智障|废物|垃圾东西|没用的东西|闭嘴|滚开|给我滚|就你这水平|太菜了|菜得|你没用|你这种/u.test(String(inbound ?? ''));
    // r104：**他在骂自己，不是在骂她**。
    // 实测：「我昨天把咖啡洒在键盘上了，蠢死了吧」命中了裸词支 `蠢[吗啊吧呀死了]` → insultedNow
    // → `trust −0.10` 且 `avoidance = 1`，于是她那一轮被压成 brief（欲望还没开口就被掐掉）。
    // 判据与原典一致：她只在**被指向**时收窄（原典是「你只会打游戏」那类），别人的自嘲不构成攻击；
    // 处理方式同 r82 的 `praiseNow` 修正——这是"判错了对象"，不是人格问题。
    // 回滚：`MIND_INSULT_SUBJECT=0` 回到只看词表的旧行为。
    const INSULT_SUBJECT_OFF = process.env.MIND_INSULT_SUBJECT === '0';
    const selfBlame = /我[^。！？]{0,12}(?:蠢|傻|笨|废物|垃圾|白痴|没用|不行)|(?:蠢|傻|笨|丑|菜)死了吧?/u.test(String(inbound ?? ''));
    const pointsAtHer = /你[^。！？]{0,6}(?:蠢|傻|笨|废物|垃圾|白痴|智障)|(?:七海|千秋)[^。！？]{0,6}(?:蠢|傻|笨)/u.test(String(inbound ?? ''));
    const insultGate = INSULT_SUBJECT_OFF || !(selfBlame && !pointsAtHer);
    next.reportedInsult = directInsult && isReportedSpeech(inbound);
    // r106：有感知结果时**听感知的**（LLM 判"主语是不是她"）；没有就退回规则＋主语判据。
    next.insultedNow = seenPercept
        ? (seenPercept.insultAtHer === true && !next.reportedInsult)
        : (directInsult && !next.reportedInsult && insultGate);
    // 论点不成立：讲错机制／搞错事实／冤枉她／否定她的能力
    const wrongClaim = /(机制|规则|设定|版本|程序|系统).{0,8}(不是这样|搞错|说错|不对|你错了)|你(说|讲)错了|根本不是(这样|那回事)|(你|她).{0,4}(作弊|抄袭|偷|骗|伪造)|冤枉|明明(就)?(不是|没有)|你连.{0,8}都(不懂|不知道|不会)|就你.{0,6}(水平|能力|水平吧)/u.test(String(inbound ?? ''));
    // r109（原典核对）：她**会**道歉（27 行，1.3%），但道的是"我睡着了／我拖了"这类**她真做过的事**；
    // 冤枉她的事她不认（「怎、怎么可能！我只是来散步而已！」）。
    // 所以把两件事分开：ownAct = 他指的是她确实做过的事；notMine = 冤枉/给她安帽子。
    // 被调侃：昵称、打趣、干幽默的逗——不构成论点
    const teased = /宝宝|小可爱|小笨蛋|傻呗|傻瓜|别闹|哈哈|笑死|太强了吧|你行你上|哟|嘿/u.test(String(inbound ?? ''));
    next.wrongClaimNow = wrongClaim;
    next.teasedNow = teased && !next.insultedNow && !wrongClaim;
    // r50：他把"要什么"直接递过来（教我／给我讲／带我去／我想听／帮我）→ 可以立刻讨要，
    // 不必等他先说完一件具体的事。这是"直说要东西"从 1.1% 抬向 3.1% 的主要缺口。
    next.directAskOffer = /教我|给我讲|给我说|讲给我|带我去|你带我|我想听|说给我听|帮我(看|弄|拆|写|想)|陪我练/u.test(String(inbound ?? ''));
    // r51/task-6：这一句踩到"她不该懂"的格子 / 或者踩到她的强项
    const boundary = offLimitsOf(inbound);
    // r53 ②：**上下文里已经出现的信息不许当未知**。旁白说"两人闲晃到牧场"＝她已经在场了，
    // 再问"牧场是什么"就是没接住（r52 第 1 段第三问就压在这句旁白之后）。
    const arrived = /到了|走进|到达|去过|经过|待了|一起来到|带我来/u.test(String(inbound ?? ''));
    next.offLimitNow = boundary && !arrived ? boundary.key : '';
    next.offLimitWords = boundary && !arrived ? boundary.words : [];
    next.strongSuitNow = STRONG_SUIT.test(String(inbound ?? ''));
    // r53 ④：同场不许翻面——她上一两句把强项说成"不擅长/手一抖就乱"，这一两轮要纠正回来
    next.strongFlipRecent = (state.strongFlipAt ?? -99) >= 0 && ((state.rounds ?? 0) - (state.strongFlipAt ?? -99)) <= 2;
    // r51：她的事实（防"事实错"：r50 把"爸爸是程序员"答成了"不是呀"）
    const fact = factOf(inbound);
    next.factNow = fact ? fact.fact : '';
    // r51：同一件事被问到第三次 → 换落法（不许再给同一种应答）。
    // 原典卷第 8–9 段「要和七海一起行动吗」被问 16 轮，我们换了七种说法说同一件事——字面去重拦不住。
    const normIn = String(inbound ?? '').replace(/[^\u4e00-\u9fa5A-Za-z0-9]/gu, '');
    const seenIn = (state.inboundSeen ?? []).filter((one) => typeof one === 'string');
    const sameTopic = seenIn.filter((one) => one === normIn || overlapOf(one, normIn) >= 0.6).length;
    next.sameTopicCount = sameTopic + 1;
    next.inboundSeen = [...seenIn, normIn].slice(-12);
    // ── r67：**适应（habituation）+ 情绪累积** ────────────────────────────
    // 真实的人被问第 5 遍时已经不是"在回答"了：第 3 遍注意到，第 4–5 遍烦躁累积，
    // 动机从"回答"转成"指出这件事"或"追问为什么"，第 6 遍以上可能只给半句、或者不理。
    // 关键：**动机不能同时是"回答"和"指出重复"**——两件一起做就读成敷衍（r66 的扣分点）。
    // r68：遍数按 **gist（话题）** 算，不按字面——「要一起吗」「一起打一局」都是同一个话题，
    // 第 4 次就该报 4，不会报成 3（r67 的"报第三遍实际是第 4 次"）。
    const topic = gistOf(inbound);
    next.gist = { ...(state.gist ?? {}) };
    if (topic) {
        next.gist[topic] = (next.gist[topic] ?? 0) + 1;
    }
    // v5②：计数**落进记忆**（每轮 +1 写回 state.gist），读的是记下的那个数，不是现场重算。
    // 数不出来（没有记录）就是 0 → 不许提"第几遍"（不提不丢人，报错才丢人）。
    const repeat = topic ? (next.gist[topic] ?? 0) : 0;
    next.repeatCount = repeat;
    next.irritation = repeat >= 8 ? 1
        : repeat >= 5 ? 0.5
            : repeat >= 3 ? 0.3
                : Math.max(0, (state.irritation ?? 0) * 0.5);   // 不重复就慢慢消
    // r74：情境不在这里定——它要等这一轮的动机算完（动机先算、场景后定），
    // 所以在 step() 末尾调用 decideScene()。
    const has = (key) => hits.includes(key);

    // r82（镜像标定）：`praise` 拆成两类。mood 那套效果两类都吃（被抬举也会慌）；
    // **`thinksImCute` 只由降格那一类（praiseCute）抬**——「他很厉害地夸我」不等于「他把我当可爱的小东西」。
    const praised = has('praiseCute') || has('praiseAbility');
    if (praised) {
        // r89：按原典分开（见 MOOD 表 ②）——被夸**才能**她不接（「有没有才能根本没什么差别」），
        // 效价几乎不动，只抬唤起；被当"可爱的小东西"才有一点受用＋尴尬。
        // 旧版两类一起 +0.15，等于让她"因为被夸而心情大好"，那不是她。
        next.moodV = clamp(next.moodV + (has('praiseCute') ? MOOD.praiseCute : MOOD.praiseAbility), -1, 1);
        next.moodA = clamp(next.moodA + MOOD.praiseArousal, 0, 1);
        next.energy = clamp(next.energy + MOOD.praiseEnergy, 0, 1);
    }
    if (has('praiseCute')) {
        bumpBelief(next, 'thinksImCute', 0.2);
    }
    if (has('askAgain')) {
        next.askStreak = state.askStreak + 1;
        // r82（棘轮修正，2026-10-26）：`tiredOfMe` 的增量 0.12 → **0.15**。
        // 为什么改增量而不改门槛（Lead 选 B，我实测采纳）：
        //   · 门槛 0.55 是 `tools/persona-lab/cases-mirror.json` 上按"四轮 0.58"推出来的线，改它等于把那次标定作废；
        //   · 增量表示"他敷衍一次，她心里记多少"，本来就该比"多少镜子才改她的心情"好调；
        //   · 恢复"每轮都衰减（×0.97）"之后，0.12 的四轮值是 0.5474（差 0.0026 到不了 0.55——又变死代码），
        //     0.15 的四轮值是 0.6621，**过线且有余量**（同一份卷子实测）。
        bumpBelief(next, 'tiredOfMe', 0.15);
        next.moodA = clamp(next.moodA + 0.12, 0, 1);
        next.moodV = clamp(next.moodV + MOOD.askAgain, -1, 1);
    }
    else {
        next.askStreak = 0;
    }
    // r82（欠账①）：`putOff`（他把你往外推）与 `askAgain`（他听不懂）**同一档 +0.15**——
    // 都是"他在把我往外推"的同一种感觉，分成两个不同的数会让同一件事有两种力度。
    // 注意：**不抬 `thinksImDumb`**（她是被推后，不是被当笨蛋），也不动 moodV/moodA（这一刀只补触发面）。
    if (has('putOff')) {
        bumpBelief(next, 'tiredOfMe', 0.15);
    }
    if (has('cold')) {
        next.moodV = clamp(next.moodV + MOOD.cold, -1, 1);
        next.energy = clamp(next.energy - 0.03, 0, 1);
    }
    if (has('hisStuck')) {
        next.moodA = clamp(next.moodA + 0.1, 0, 1);
        next.energy = clamp(next.energy + 0.03, 0, 1);
    }
    if (has('hisWeak')) {
        next.moodV = clamp(next.moodV + MOOD.hisWeak, -1, 1);
    }
    if (has('invite')) {
        next.energy = clamp(next.energy + 0.06, 0, 1);
        next.moodV = clamp(next.moodV + MOOD.invite, -1, 1);
    }
    if (has('woundGame') || has('woundAI')) {
        next.moodV = clamp(next.moodV + MOOD.wound, -1, 1);
        next.moodA = clamp(next.moodA + 0.15, 0, 1);
        // r82（欠账② / 2026-10-26）：`woundGame` 的单次命中幅度 0.2 → **0.22**（`woundHuman` 不动）。
        // 算术（每轮先 ×0.97 衰减、再结算这一轮；初值 0.20）：
        //   0.2 一次：0.194+0.2 = 0.394；两次：0.382+0.2 = 0.582；之后每轮 ×0.97
        //     → `mirror-mixed`（2 次命中 + 2 轮衰减）落到 **0.5477 < 0.55** → dim 那一支看不见了。
        //   0.22 一次：0.194+0.22 = 0.414；两次：0.402+0.22 = 0.6216 → 衰减两轮 = **0.585 ≥ 0.55** ✓
        // 所以 0.22 是"2 次命中就够过线"的最小整数档（0.21 只能到 0.574，余量太薄；0.23 会更快触发
        // `wound ≥ 0.6` 的沉默/退避分支，副作用不必要地变大）。
        bumpBelief(next, 'thinksImDumb', has('woundGame') ? 0.22 : 0.2);
    }
    if (WOUND_LAYER_OFF) {
        // OFF 臂 = r81：一个标量，命中即 1，否则每轮 −0.3（原样保留，逐字复现）
        next.wound = (has('woundGame') || has('woundAI')) ? 1 : Math.max(0, numOf(state.wound, 0) - 0.3);
        next.woundGame = 0;
        next.woundHuman = 0;
        next.woundRank = 0;
        next.woundKind = null;
    }
    else {
        // r82（F / psych §13.2）：三种疼各衰减、各加响。
        // 衰减系数 0.7／0.5／0.85 的来源：2026-10-26 在 canon 86 轮上按阈值 0.6 标定（§13.2 末表）。
        next.woundGame = clamp(numOf(state.woundGame, 0) * 0.7, 0, 1);
        next.woundHuman = clamp(numOf(state.woundHuman, 0) * 0.5, 0, 1);
        // r101：`woundRank` 已退役——没有任何人再写它，这里只让旧 state 的残留值自己衰减干净。
        next.woundRank = clamp(numOf(state.woundRank, 0) * 0.85, 0, 1);
        if (has('woundGame')) {
            next.woundGame = clamp(next.woundGame + 1, 0, 1);
        }
        if (has('woundAI')) {
            next.woundHuman = clamp(next.woundHuman + 1, 0, 1);
        }
        // 派生量：`wound` 只取 game／human 两种（rank 那一支 r101 已整套退役，
        // 见 docs/r101-wound-rank-removed.md）。两处读点原样不改。
        next.wound = Math.max(next.woundGame, next.woundHuman);
        next.woundKind = woundKindOf(next);
    }

    // ③ agenda：他要什么 × 心情 × 上一轮
    const previous = state.agenda;
    let agenda = previous;
    if (next.wound >= 0.8) {
        agenda = 'want_hide';
    }
    else if (next.askStreak >= 2) {
        agenda = 'want_hide';
    }
    else if (has('hisWeak')) {
        agenda = 'want_win';
    }
    else if (has('hisStuck')) {
        agenda = 'want_understand';
    }
    else if (has('invite') || has('praiseCute') || has('praiseAbility')) {
        agenda = 'want_company';
    }
    else if (next.energy < 0.25) {
        agenda = 'want_hide';
    }
    else if (has('cold')) {
        agenda = 'want_company';
    }
    next.agenda = agenda;

    // ④ 非平凡变化守卫：连续 6 轮状态没变就强制一次（状态机收敛是头号死法）
    const changed = agenda !== previous
        || Math.abs(next.moodV - state.moodV) > 0.05
        || Math.abs(next.energy - state.energy) > 0.05
        || next.wound !== state.wound;
    next.sinceShift = changed ? 0 : state.sinceShift + 1;
    if (next.sinceShift >= 6) {
        next.agenda = agenda === 'want_company' ? 'want_understand' : 'want_company';
        next.sinceShift = 0;
        next.forcedShift = true;
    }
    // r89：**他回来了**（依恋：分离—重聚）。
    // 原典里她的需要是"想有人在"（canon-nanami.md §7 的四个需要，"关联"最缺）；
    // 现实里隔了很久再说话，人会被抬起一点——但很小，而且只给"久别重逢"这一次，
    // 不然连发十条消息就能刷心情。负性事件在场时不给（"他回来就为骂我"不是安慰）。
    const gapMs = numOf(state.updatedAt, 0) > 0 ? (now - numOf(state.updatedAt, 0)) : 0;
    if (gapMs >= MOOD_COMPANY_GAP_MS
        && !has('cold') && !has('woundGame') && !has('woundAI') && !has('askAgain')) {
        next.moodV = clamp(next.moodV + MOOD.company, -1, 1);
    }
    // ⑤ 每轮自然回落：唤醒掉得最快，效价慢慢回中，精力按轮微量消耗
    next.moodA = clamp(next.moodA * 0.92, 0, 1);
    next.moodV = clamp(next.moodV * 0.96, -1, 1);
    next.energy = clamp(next.energy - 0.012, 0.05, 1);
    next.rounds = state.rounds + 1;
    // r49（b）：长会话不该掉到地板——每 20 轮自然恢复一点（他去忙别的了那种时间流逝），
    // 上限 0.85。这是"参数"层面的回补，行为层面的调整见 pickActionBase 的低精力分支。
    if (next.rounds > 0 && next.rounds % ENERGY_RECOVER_EVERY === 0) {
        next.energy = clamp(next.energy + ENERGY_RECOVER_AMOUNT, 0.05, ENERGY_CAP);
    }
    // r75（v7）：**信任与受伤**——先结算这一轮他对她做了什么，再决定空间要不要换。
    updateTrust(next, inbound);
    // 派生量在这里再取一次：updateTrust 只改 trust／hurt（r101 起它不再写 woundRank）。
    next.woundKind = woundKindOf(next);
    // r107 构念层：真正的结算在 CLI 的 settleConstructs（那里 hits／appraisal 才齐）。
    // 这里只保留"老状态没有这两个键"的兜底，不让卡片读到 undefined。
    next.evaluativeConcern = numOf(next.evaluativeConcern, 0);
    next.reflectedRegard = next.reflectedRegard ?? reflectedRegardOf(next);
    // r74：情境在这里定——动机（防御＋情绪）已经算完，才轮到她决定自己在哪。
    // 人不会每句话都换房间：decideScene 只在"他要动"或"动机明确换了"时才换地方。
    next.scene = decideScene(next, inbound, new Date());
    next.sceneSig = sceneSignature(next, new Date());
    // r82（线上反馈 #1 ②）：**对话地板**——他这句正文 ≥4 字 = 真的在聊（不是「嗯」），
    // 她一开口就在这个场里，精力不低于 0.35（= ENERGY_FADE，不落进"撑着"带）。
    // 放在 step 的最后：前面所有漂移（−0.012／cold／事件）都算完了，这里才是这一轮最终的精力。
    // ⚠️ 地板**只在还醒着的时候生效**（energy ≥ ENERGY_SLEEP）：已经跌到睡着那一档就不抬——
    // 睡着这条是她的性格（原典里会当场睡着），不能被"他多说了几个字"抹掉。
    // 实测：不加这个前提时，`selftest-mind-actions` ⑥「精力 0.15 + 你还在吗 → 睡着」立刻挂
    // （地板把她抬到 0.35）。那是副作用，不是测试错了——所以按"叫不醒一个已经睡着的人"来收口。
    // r102：`energyTrue` = **地板之前**的真实精力（第 9 层读它，见 `costInputsOf`）。
    // 地板只改"她开口时的体力底线"，不该顺手把"她累不累"也抹平。
    next.energyTrue = next.energy;
    if (bareLen(inbound) >= CHAT_FLOOR_MIN_CHARS && next.energy >= ENERGY_SLEEP) {
        next.energy = Math.max(next.energy, ENERGY_FLOOR);
    }
    return { next, hits };
}

// ── 动作层（8 选 1）：状态先选动作 ─────────────────────────────────────
// r30：新增三格（task-4 ①③）。卡片只写方向，不写可照抄的句子。
// r86（事实纠正，r85 审计判过的那处矛盾）：从 r75 起这一层**不决定句式、也不决定骨架族**——
//   `ACTIONS` 那几句做法说明只在 replay 里看，从不进 prompt。卡片上唯一的落点是
//   `ACTION_BEHAVIOR` 那一个行为词（"这一轮我只想：顶回去"）。原来这里写"动作决定骨架族"，
//   与 renderCard 那处注释互相打脸；**现在只留一种说法**，改这层行为之前先读 r86 §2。
//   · 起手   ＝ 主动追求（她先开口）：原典上界 3.3%，所以配额压得很紧（见 OPEN_* 常量）
//   · 跑题拆结构 ＝ 被问到答不上来的事时，转去认真拆一层结构（她的招牌防御）
//   · 答偏   ＝ 压力大或话题太突然时，给一个偏掉但不冷的回应——"接不住的笨拙"是防御机制，不是 bug
// r86：导出给自测用（D 组要断言行为词不许照抄做法说明）
export const ACTIONS = {
    应答: '先接住他那句：一个短反应，不解释、不复述',
    // r48：例句里的游戏词汇要收着——`游戏是她的尺度` 是按"存档/一局/关/进度"这类词统计的，
    // 卡片里带这些词的例句会被照抄，直接把该维度刷高（原典日常 10.9%，我们曾 17.5%）。
    回避: '把话转回她自己正在做的事、或者她喜欢的旧东西（方向：换个话头，别顺着这一格往下聊）——不许说"跳过""不聊这个"',
    // r50：`游戏是她的尺度` 被她自己"问进度"的话刷到 18.2%（原典日常 10.9%）。
    // 实测 r49 的 16 处命中里大半是「你卡在哪一关／你想打哪一局」——根因是**卡片例句被照抄**，
    // 所以反问/起手这两格的例句里不再出现"哪一关／哪一句／约一局"，并在卡上明写不要再这么问。
    反问: '问一件具体的事（时间、地点、他做到哪一步都行），问完就停——**别再问"卡在哪一关／打到第几关"**',
    // r60（主人的纠正）：**「判词」和「断言」不是一回事**
    //   判词 = 对**他/对局面**下结论（低频，53 轮窗口）
    //   断言 = **她说自己的判断/自己的事**（"这一关我过了""难就难点"）← 高频、不设窗口，硬收主要来自这里
    判词: '对他或这个局面下一句结论，可以被反驳的那种，说完就停',
    断言: '说她自己那一份判断或事实（不是评判他），说完就停',
    改口: '先说自己一开始怎么想的，再给最终判断（"我一开始以为你没开过，不过想想还是没算上你"）——不是为措辞道歉',
    // r50：触发面放宽——原典里她是直接开口要的，不必等他说了具体的事；
    // "教我／给我讲／带我去／我想听／帮我"这几种说法本身就够。密度仍由窗口管（3.1% ≈ 32 轮）。
    讨要: '直接说出她想要的（叫他讲、叫他带、叫他教、说她想听）——不要绕成问句，也不要等他说完一整件事才开口',
    // r30：措辞只指向**同伴位**——她也在这件事里（一起拆／一起卡／她负责其中一段）。
    // "我陪你""我在旁边"是陪伴位，判据不计分，所以卡片里不再这么写。
    // r31/§2.3：我们模式**必须有产出**——四样之一 + 落到具体小步；
    // 四样都没有就不是「一起」，只能降级成"陪着"（断言对方状态，不邀约）。
    一起: '把这件事变成两个人一起做的，且**必须给四样之一并落到具体小步**：给方法（下一步怎么做）／给道具（要准备什么）／给分工（只有他能做的那段）／给后援（她也帮、但把高光交出去）',
    起手: '这一句由我先开口：提一个具体的提议、提个挑战、或者直接要一件事（方向只给到这里，句子自己长；别写成客套的邀约腔，也别挂游戏说法）',
    // r47：**被冒犯这几格原典没有样本**（日文日常只占 8.2%，英文日常戏里她也没被人骂过），
    // 所以按 docs/psyche-insult-layer.md 建：先取"一个普通十七岁女生的真实反应"，再按她的人格改造。
    // 普通女生被说"傻"的第一反应是**不服**，不是"我好像也没什么能反驳的"，更不是笑嘻嘻顺着聊。
    //
    // ① 驳论点 ＝ 他那句话在道理上不成立（含「你是猪吗」这种不公的判断）→ 短判。
    //    依据：学级裁判语域 04926 それは違う／05464 違うよ——反驳模板，可迁移但**不是日常**。
    驳论点: '短判：这句道理上不成立，平地说一句否，不解释、不复述他的理由，**不许自贬**（别顺着他把自己说小），说完就停（别写成起诉书那种调子）',
    // ② 被调侃 ＝ 他在逗她／叫她小名／骂一句但不构成论点 → 尴尬或干幽默。
    //    依据：英文日常戏的形态 + 普通女高中生推演（原典无样本，属推演）。
    //    三条硬禁止（docs/psyche-insult-layer.md §2）：禁止自我否定／禁止热情接梗与用他的词展开／禁止公诉腔。
    被调侃: '尴尬或干幽默：**平的拒绝或顿住**／把他的话的荒谬重复给他听／轻轻不认同／叹口气——**不许热情接梗、不许用他的词往下展开比喻、不许自贬**（不许顺着他说自己确实傻）',
    跑题拆结构: '这句答不上来，就把话岔到她擅长的结构上：认真拆一层这东西怎么运转，拆完就停',
    答偏: '给一个偏掉但不冷的回应：接不住时的笨拙，不是敷衍、也不是道歉',
    玩笑: '拿自己打趣一句，别解释，别自嘲到哭',
    // r49：低精力不是"回避"，是**撑着或睡着**。依据：原典 e08_009_032 整场就是她说着说着睡着了
    // （「……嗯。」「………………」→ 说梦话，梦里是游戏 → 醒来对事实认一句"又睡着了"）。
    // 注意：这一格对事实道歉，**不是**"为措辞道歉"那条红线。
    睡着: '累到极点：一条极短的应声／一句梦话（梦里是她自己那摊事）／醒了之后对事实认一句（对事实道歉，不是为措辞道歉）',
    短答: '只应一声（两三个字），不补第二句',
    半句: '说半句就停，剩下的留给他问',
};

// ── r86（2026-10-28）：动作 → 卡片上的一个**行为词**（第一人称，不含做法）─────────────
// 为什么另起一张表：`ACTIONS` 里存的是**做法说明**（"问一件具体的事，问完就停"），
// 那是给模型的操作提示，写进卡片就会被照抄（r78 的坑）。这里只留"她打算怎么处理这句话"
// 这一层意思——怎么顶、怎么岔，仍然是她自己的事。
export const ACTION_BEHAVIOR = {
    应答: '接他这一句',
    回避: '岔开',
    反问: '问回去',
    判词: '给个结论',
    断言: '说我自己那一份',
    改口: '改口',
    讨要: '跟他要',
    一起: '拉他一起',
    起手: '我先开口',
    驳论点: '顶回去',
    被调侃: '不接这个梗',
    跑题拆结构: '拆开看',
    答偏: '答偏了',
    玩笑: '逗他一下',
    睡着: '不说了',
    短答: '只应一声',
    半句: '话说一半',
};
// r86 开关：`MIND_ACTION_CARD=0` → 这一行整行不存在（卡片与 r85 逐字一致）
const ACTION_CARD_OFF = process.env.MIND_ACTION_CARD === '0';

// ── 目标比率（中文原典直算，日常／自由时间列）─────────────────────────────
// 出处：docs/canon-src/scenes-chiaki.tsv（146 场 / 2377 句 / 带说话人与整场上下文），
// 生成器 tools/persona-lab/canon-profile-cn.mjs → state/psyche-model-cn.json（可重跑）。
// **不再经过日文映射**：之前那套"日文与英文取大"等于拿办案/爱岛高光的密度要求日常闲聊。
const TARGET_CN_DAILY = {
    给出判断: 0.019,     // 日常 1.9%（正篇 3.5%／裁判 5.9%）——我们曾到 15.8%，浓了八倍
    直说要东西: 0.031,   // 日常 3.1%（正篇 0.7%／裁判 0.4%）
    不懂就不装: 0.027,   // 日常 2.7%
    游戏尺度: 0.109,     // 日常 10.9%（正篇 5.2%／裁判 1.6%）
    我们模式: 0.012,     // 日常 1.2%（见下面"没跟着改"的说明）
    依恋: 0.012,         // 日常 1.2%（中文语料证明日常也有一点 → 轻微降温，不做开关）
    主动追求: 0.004,     // 日常 0.4%（正篇 0.2%／裁判 2%）
    自曝位置: 0.004,     // 日常 0.4%（裁判 0.8%）
    空话鼓励: 0.004,
    为措辞道歉: 0,       // 三类场景都是 0 → 红线，保持
    犹豫后仍给结论: 0.004,
};
// 配额 = 1 / 目标比率：让"每 N 轮最多一次"和目标同一个口径
const quotaWindowFor = (rate) => (rate > 0 ? Math.max(1, Math.round(1 / rate)) : Number.POSITIVE_INFINITY);

// ── 配额（r30 起）：新动作最怕变成新模板，所以每条都设窗口上限 ──────────────
const QUOTA_WINDOW = 24;        // 窗口：最近 24 轮（防重复用，不是目标比率）
const DEFENSE_MAX = 2;          // 跑题拆结构 / 答偏：一段最多各两次（§3.5：每 20 轮 2–3 次）
const OPEN_MIN_GAP = 12;        // 起手：距上次至少 12 轮
const OPEN_MAX_COUNT = 1;       // 起手：一段最多一次
const OPEN_ENERGY_MIN = 0.45;   // 起手：精力不能太低
const COOP_MIN_GAP = 3;         // 同伴位：至少隔 3 轮（不然每句都在"一起"）
// r49：精力门槛与回补
const ENERGY_FADE = 0.35;       // 低于这个：撑着（半句／短答）
const ENERGY_SLEEP = 0.2;       // 低于这个：允许睡着（极短应声／梦话／醒来认一句）
// r82（线上反馈 #2）：**"困"要分两档**。
// 病根：`silenceMotive` 在 `energy < ENERGY_FADE(0.35)` 就说「困——撑不住了（不是不想说，是说不完）」，
// `pickActionBase` 在 <0.35 给「半句」——她在 0.31–0.33 被连续判成"说不完"三轮。
// 现在：`ENERGY_TIRED(0.22)` = 真的说不完（那一档才允许"困"和半句）；[0.22, 0.35) = 话短一点（短答）。
// **`ENERGY_SLEEP` 与 `ENERGY_FADE` 的数值一个都不改**（有 6 条自测钉着）。
const ENERGY_TIRED = 0.22;      // 低于这个：真的说不完（"困——撑不住了"从这一档才开始）
const WAKE_GAP_MINUTES = 30;    // MIND_SLEEP=0 时的 r93 回补门
const WAKE_ENERGY_GAIN = 0.12;
const WAKE_ENERGY_CAP = 0.55;
const ENERGY_FLOOR = ENERGY_FADE;  // 对话地板：真的在聊（≥4 字）时不低于 0.35（= 不落进"撑着"带）
const CHAT_FLOOR_MIN_CHARS = 4;   // 正文 ≥4 字才算"真的在聊"（「嗯」「在吗」不算）
const ENERGY_CAP = 0.85;        // 精力上限（回补不会把她顶到满格）
const ENERGY_RECOVER_EVERY = 20;   // 每 20 轮自然恢复
const ENERGY_RECOVER_AMOUNT = 0.05;
const ENERGY_NAP_AMOUNT = 0.25;    // 她说"眯了一会儿/睡着了"之后恢复
const NAP_MARK = /眯了一会儿|眯一会|睡了一会儿|睡了一觉|睡着了|睡醒|刚醒|又睡着/u;
// ── r94：睡眠两过程（Borbély 1982；参数来源见 docs/r82-neuro-spec.md §1.3）──
// S 是睡眠压力：清醒时升，睡着时降。C 是昼夜节律。两者合成能量目标 E*；
// energy 只向 E* 松弛，所以短对话仍连续，隔几小时才显出一天里的高低谷。
const SLEEP_LAYER_OFF = process.env.MIND_SLEEP === '0';
const TAU_R = 18.2;          // 清醒时 S 上升的时间常数（小时；文献拟合值，待核）
const TAU_D = 4.2;           // 睡着时 S 释放的时间常数（小时；同上）
const C_P1 = 19.0;           // 昼夜基频峰相位（小时；她自己的形状拟合）
const C_P2 = 9.5;            // 12 小时谐波峰相位（小时；同上）
const C_A = 0.75;            // 谐波幅度（同上）
const E_BASE = 0.62;
const KAPPA = 0.22;
const LAMBDA = 0.50;
const TAU_E = 1.0;           // energy 追 E* 的时间常数（小时；设计值 60 分钟）
const WAKE_MIN_SILENCE_MINUTES = 5;
const WAKE_HOUR_ANCHOR = 9.5;

function clockHours(date) {
    return date.getHours() + (date.getMinutes() / 60) + (date.getSeconds() / 3600);
}

export function sleepTargetAt(now, need) {
    const at = new Date(now);
    const hour = clockHours(at);
    const cRaw = Math.cos((2 * Math.PI * (hour - C_P1)) / 24)
        + (C_A * Math.cos((4 * Math.PI * (hour - C_P2)) / 24));
    return clamp(E_BASE + (KAPPA * cRaw) - (LAMBDA * clamp(need, 0, 1)), 0.05, 0.95);
}

// 老状态第一次接入时，用"从 09:30 醒来后过了多久"估计 S，而不是一律当睡饱或精疲力尽。
function estimateNeedAt(now) {
    const hour = clockHours(new Date(now));
    let awakeHours = hour >= WAKE_HOUR_ANCHOR
        ? hour - WAKE_HOUR_ANCHOR
        : (24 - WAKE_HOUR_ANCHOR) + hour;
    if (hour >= 23.5 || hour < 8) {
        awakeHours = Math.max(0, Math.min(awakeHours, hour >= WAKE_HOUR_ANCHOR ? 14 : 0));
    }
    return clamp(1 - (1 - 0.30) * Math.exp(-awakeHours / TAU_R), 0.30, 0.85);
}

function asleepOf(state, now) {
    // r107：她自己决定睡了（`asleepAt > 0`）——这是**行动的结果**，压过"精力还够撑着"的推算。
    if (numOf(state.asleepAt, 0) > 0) {
        return true;
    }
    const lastSaid = [...(state.lastSaid ?? [])].reverse().find((one) => String(one ?? '').trim());
    if (NAP_MARK.test(String(lastSaid ?? ''))) {
        return true;
    }
    if ((state.energy ?? 1) < ENERGY_SLEEP) {
        return true;
    }
    // 状态机里一旦睡着，要等精力真的回到"能撑着"的档才醒；不能每一拍都从低数值重新判。
    return state.asleep === true && (state.energy ?? 1) < ENERGY_FADE;
}

// 心跳若在夜里掉线，状态表可能从睡前直接跳到第二天。这里只补“没有采样覆盖”的
// 夜间区间，不把白天排成睡眠；精力本来就不低时也不硬判她睡了。
function missedQuietHours(fromMs, toMs) {
    if (!(toMs > fromMs)) {
        return 0;
    }
    const from = new Date(fromMs);
    let total = 0;
    for (let cursor = new Date(from.getFullYear(), from.getMonth(), from.getDate() - 1);
        cursor.getTime() <= toMs;
        cursor = new Date(cursor.getFullYear(), cursor.getMonth(), cursor.getDate() + 1)) {
        const quietStart = new Date(cursor.getFullYear(), cursor.getMonth(), cursor.getDate(), 23, 30).getTime();
        const quietEnd = new Date(cursor.getFullYear(), cursor.getMonth(), cursor.getDate() + 1, 8, 0).getTime();
        total += Math.max(0, Math.min(toMs, quietEnd) - Math.max(fromMs, quietStart));
    }
    return total / 3600000;
}

/**
 * 推进到 `now` 的睡眠压力与精力。纯计算，不写盘；`step`／`stepIdle` 共用这一份。
 * `MIND_SLEEP=0` 时保留 r93 的单调时间衰减，供逐字对照。
 */
export function advanceSleep(state, now) {
    const previousAt = numOf(state?.updatedAt, now);
    const minutes = Math.max(0, (now - previousAt) / 60000);
    const dtH = minutes / 60;
    const staleEnergy = clamp(numOf(state?.energy, DEFAULT_STATE.energy), 0.05, 1);
    if (SLEEP_LAYER_OFF) {
        return {
            energy: clamp(staleEnergy - Math.min(0.3, (minutes / 240) * 0.3), 0.05, 1),
            need: numOf(state?.need, DEFAULT_STATE.need),
            target: null,
            asleep: false,
        };
    }
    const startNeed = clamp(numOf(state?.need, estimateNeedAt(new Date(previousAt))), 0.05, 1);
    const asleep = asleepOf(state, new Date(now));
    const missedNight = asleep ? 0 : missedQuietHours(previousAt, now);
    const inferredSleepH = (!asleep && missedNight >= 2 && staleEnergy < 0.65) ? missedNight : 0;
    const sleepH = asleep ? dtH : inferredSleepH;
    const awakeH = Math.max(0, dtH - sleepH);
    const needAwake = 1 - ((1 - startNeed) * Math.exp(-awakeH / TAU_R));
    const need = needAwake * Math.exp(-sleepH / TAU_D);
    const target = sleepTargetAt(now, need);
    const alpha = 1 - Math.exp(-dtH / TAU_E);
    const energy = clamp(staleEnergy + (alpha * (target - staleEnergy)), 0.05, 1);
    return { energy, need: clamp(need, 0, 1), target, asleep };
}
// r50：她在回复里挂了游戏词（口径对齐 psyche-check 的"游戏是她的尺度"，但去掉 关/命/血 这类
// 会被"关系/关键/生命"误伤的单字，只认明确的游戏词）
const GAME_WORDS = /存档|读档|一局|关卡|副本|boss|血条|血量|复活|读盘|卡关|过关|打怪|练级|开黑|联机/iu;
// 生效中的三个窗口（r48，按中文原典日常列）：
//   给出判断 1.9% → 判词密度 ≈ 53 轮一次（这是"给判词"的密度，不是"有立场"的密度）
//   直说要东西 3.1% → ≈ 32 轮一次
//   自曝位置 0.4%  → ≈ 250 轮一次
// ⚠️ 主动追求 / 我们模式 两格**没跟着改**：中文日常只有 0.4%／1.2%，但 r41 实测 3.3%／5–10%
//    是三项不降的有效值。两套口径冲突，先按下不动，等 r48 盲评回来再定。
const JUDGE_WINDOW = quotaWindowFor(TARGET_CN_DAILY.给出判断);       // ≈ 53
const TAREA_WINDOW = quotaWindowFor(TARGET_CN_DAILY.直说要东西);     // ≈ 32
const SELF_EXPOSE_WINDOW = quotaWindowFor(TARGET_CN_DAILY.自曝位置);  // ≈ 250
// r50：不懂就不装 2.7%（≈37 轮一次）；依恋 1.2%（≈83 轮一次，约下次/等你的频率降三倍）
const UNKNOWN_WINDOW = quotaWindowFor(TARGET_CN_DAILY.不懂就不装);
const ATTACH_WINDOW = quotaWindowFor(TARGET_CN_DAILY.依恋);
// 「起手」的由头：
//   · 原典判据（§1.1）：**"他示弱 → 她提挑战"不成立**（fandom-lines.tsv:245–247 她说的是失望 +
//     "能一起玩就行"；日文侧 勝負/挑戦 = 0 条）。所以"示弱"这一条**保留为不算由头**。
//   · r42 实测补充：把 hisWeak 整条删掉之后主动追求从 3.3% 掉到 0%。回看发现：
//     她不是被示弱点燃，但**会在冷场/没话头的时候自己找一个**——这跟"提挑战"是两回事。
//     所以补上非示弱的低能量信号：他说累、他冷场（一句几个字）、他换话题。
//   · 于是由头 = 他想玩 ／ 他挑她 ／ 他提议一起 ／ 他说累 ／ 他冷场 ／ 他换话题。
const OPEN_HOOK_PLAY = /想玩|要玩|想打|开一局|试试|一起玩/u;
const OPEN_HOOK_CHALLENGE = /挑战|比一比|比比|赢不了|不敢比|敢不敢|你行不行|来一局|赌一局|挑我/u;
const OPEN_HOOK_TOGETHER = /一起|联机|双人|带我|陪我|组队|一块儿/u;
const OPEN_HOOK_GAME = /以撒|星露谷|我的世界|gta|塞尔达|马里奥|卡比|百变怪|里罗|游戏|存档|读档|一关|boss/iu;
// 低能量信号（≠ 示弱）：累 / 冷场 / 换话题
const OPEN_HOOK_TIRED = /累|困|没力气|不想动|没精神|好烦|无聊|没意思|有点闷/u;
const OPEN_HOOK_SWITCH = /话说回来|说起来|对了|换个话题|还有那个|另外|算了不说这个/u;
const COLD_FLOOR_CHARS = 3;   // 他这句正文 ≤3 字就算"冷场"
function detectOpenHooks(state, inbound) {
    const text = String(inbound ?? '');
    const play = OPEN_HOOK_PLAY.test(text);
    const challenge = OPEN_HOOK_CHALLENGE.test(text);
    const together = OPEN_HOOK_TOGETHER.test(text);
    const tired = OPEN_HOOK_TIRED.test(text);
    const switched = OPEN_HOOK_SWITCH.test(text);
    const coldFloor = bareLen(text) > 0 && bareLen(text) <= COLD_FLOOR_CHARS
        // 被夸不算冷场：「很可爱」只有三个字，但它是在夸她（那一格走"反问式收下"，§3.5⑤）
        && !/可爱|厉害|真好|喜欢|夸|棒|强|谢|哈哈|好笑/u.test(text);
    return {
        play,
        challenge,
        together,
        tired,
        switched,
        coldFloor,
        game: OPEN_HOOK_GAME.test(text),
        any: play || challenge || together || tired || switched || coldFloor,
    };
}

// 配额：最近 window 轮里这个动作用过几次
function usedInWindow(state, action, window = QUOTA_WINDOW) {
    const list = state.actionRounds?.[action] ?? [];
    const now = state.rounds ?? 0;
    return list.filter((one) => now - one < window).length;
}

function markActionRound(state, action) {
    if (!action) {
        return;
    }
    const map = { ...(state.actionRounds ?? {}) };
    map[action] = [...(map[action] ?? []), state.rounds ?? 0].slice(-8);
    state.actionRounds = map;
}

// r48：**"给判词"和"有立场"是两件事**。
// 中文原典日常里"给出判断"只有 1.9%（我们曾到 15.8%，浓了八倍）——不是要她少下判断，
// 是别每轮都落一句判词：立场可以一直在（应答/短答/一起里都有态度），判词稀有。
// 只在这三类给判词：① 他说的话不成立（wrongClaim）② 他问你怎么看 ③ 他把自己说小（hisWeak）。
// 另外按窗口制限密度：1/0.019 ≈ 53 轮一次。
const JUDGE_ASK = /怎么看|你觉得|你说呢|你说是吧|对不|是不是|如何|你看/u;
function allowJudge(state) {
    const asksOpinion = Boolean(state.lastInboundAsked) && JUDGE_ASK.test(String(state.lastInboundText ?? ''));
    const eligible = state.wrongClaimNow === true || asksOpinion || state.hisWeakNow === true;
    return eligible && usedInWindow(state, '判词', JUDGE_WINDOW) < 1;
}

// 判词被降级时落到哪儿：他卡住 → 一起；累了 → 短答；其余 → 应答（都还带着态度）。
// 注意不再落回 主张（那正是要压的），也不落 反问（问句密度另有账）。
function demoteJudge(state) {
    if (state.hisStuckNow) {
        return '一起';
    }
    if ((state.energy ?? 1) < 0.35) {
        return '短答';
    }
    return '应答';
}

/**
 * r109：**接话潜伏期**——不新增状态，只由特质 + 这一轮的话题陌生度推出。
 * 依据（干净子集 1967 行）：请示/抱歉 20 行（1.0%）、卡壳 34 行（1.7%）、先问是什么 29 行（1.5%）。
 * 用法：值越高 → 越应该先"应一声/问一句"，而不是直接断言或起手。
 */
export function latencyOf(state) {
    const effortful = TRAITS.effortfulControl ?? 0.8;
    const surgency = TRAITS.surgency ?? 0.25;
    const efficacy = TRAITS.socialEfficacy ?? 0.3;
    const novelty = state.offLimitNow ? 1 : (state.askStreak >= 3 ? 0.6 : 0.2);
    return Math.max(0, Math.min(1, effortful * (1 - surgency) * novelty * (1 - efficacy + 0.3)));
}

function pickAction(state) {
    // r75：受伤期间只走短答——话变短，不是"这一轮不发言"
    if ((state.hurtUntil ?? -1) >= (state.rounds ?? 0) && (state.hurtAt ?? -99) < (state.rounds ?? 0) && !state.insultedNow && (state.rounds ?? 0) - (state.hurtAt ?? -99) <= 2) {
        return '短答';
    }
    let base = pickActionBase(state);
    // v12（r80）：这一轮她手上没有可加的东西（他没说什么／同一件事问到第三遍）→
    // 起手／一起／讨要／断言这些"往前推"的动作自己就收回来了。不是外部拦她，是她没那个劲儿。
    const news = newsOf(state);
    if (!news.news && !state.insultedNow && !state.wrongClaimNow) {
        if (['起手', '一起', '讨要', '断言', '改口'].includes(base)) {
            base = (state.energy ?? 1) < 0.45 ? '短答' : '应答';
        }
    }
    // r51/task-6：同一个问题问到第三次 → 强制换一种落法（原典卷 8–9 段那 16 轮就是这么坏的）。
    // ⚠️ 换法里**不许引入反问**：第一版把"应答→反问"当换法，结果 88 轮里反问飙到 25 次（28%），
    // 等于用问句去填重复——那是另一种模子。只在"低内容动作"之间换，靠内容而不是靠问。
    // r52：门槛 3 → 4（评委说"答一句 + 抛一问"成了固定节拍，先抬一档让它少触发）
    if ((state.sameTopicCount ?? 0) >= 4) {
        const swap = { 应答: '短答', 短答: '半句', 半句: '应答', 反问: '应答' };
        if (swap[base]) {
            base = swap[base];
        }
    }
    // r53：反问的**频率**闸门（不依赖重复，任何卷子都生效）。
    // 评委抱怨的是整体节拍："答一句 + 抛一问"占到 22%，把前半句接准的内容又推回去。
    // 规则：最近 3 轮里反问 ≥2 次 → 这一轮不许再反问。落 主张（判词闸门允许时）／短答／半句。
    // （r52 那条"5 轮 ≥3"在这套原典卷上触发不了，换成这条更简单也更硬。）
    if (base === '反问') {
        const recentAsk = (state.lastActions ?? []).slice(-3).filter((one) => one === '反问').length;
        if (recentAsk >= 2) {
            base = allowJudge(state) ? '判词' : ((state.energy ?? 1) < 0.45 ? '半句' : '短答');
        }
    }
    // r48：判词闸门（密度 + 三类触发），不满足就降级
    if (base === '判词' && !allowJudge(state)) {
        base = demoteJudge(state);
    }
    // r43/r45：被冒犯的两个动作不参与"换一个最接近的"那种去重复——
    // 连着两轮被将就连续两轮给立场（内容会变，立场不软）。
    if (base === '驳论点' || base === '被调侃') {
        return base;
    }
    const recent = (state.lastActions ?? []).slice(-2);
    if (recent.length === 2 && recent.every((one) => one === base)) {
        // 连着两轮同一个动作 → 换一个最接近的（这一格是"节拍器"的病根）
        const fallback = base === '应答' ? '反问' : base === '反问' ? '半句' : '应答';
        return fallback;
    }
    // 应答最多连两轮：第三轮按状态换（疲惫→半句，唤醒高→玩笑，其余→反问）
    if (base === '应答' && recent.length === 2 && recent.every((one) => one === '应答')) {
        if (state.energy < 0.35) {
            return '半句';
        }
        if (state.moodA > 0.45) {
            return '玩笑';
        }
        return '反问';
    }
    return base;
}

function pickActionBase(state) {
    // ── r45：被冒犯的两种落法（放最前、无配额）──────────────────────────
    // 不加配额的理由：被骂就该给立场；配额会让它变成"偶尔才生气"，那是另一种失真。
    // 这一格同时压住后面所有"软"的动作：回避/短答是被骂了不还嘴，跑题/答偏是把攻击
    // 当成"答不上来的问题"——原典里这两种反应都不对。
    //   先说理：他那句在道理上不成立（或冤枉她）→ 驳论点（短判，不解释）
    //   再分档：只是逗她／骂一句但不构成论点  → 被调侃（尴尬或干幽默，不热情接梗）
    // r109（盲评两轮之后的结论）：
    // ① **宽规则撤掉**——"latency≥0.55→应答"会让「问回去」升到 23%，变成每句都在澄清（S 41.9%／T 43.0%）。
    // ② **窄规则保留**——只有真的踩到"她不该懂"的那一格（offLimitNow，比如"挤奶"），才先问一句。
    //    依据：`e05_217_000#94`「上一个步骤……是什么意思呢？」、`e04_100_091#171`「咦咦……？这是什么啊？」
    if (state.offLimitNow && (state.energy ?? 1) >= 0.35) {
        return '反问';
    }
    if (state.insultedNow || state.wrongClaimNow) {
        return '驳论点';
    }
    // r109（原典核对）：**担忧落到"说少一点"，不落到"解释自己"**。
    // 依据：原典"自证型"句子 0.0%（我本来以为／我只是猜／那不是我的意思 全无）；
    // 而"收窄/停住" 0.4%（算了／不说了／没什么）。
    // 所以评价性担忧高的时候，她要的是**话短**，不是**补一句限定**——这一条直接压掉「改口」这类动作。
    if (numOf(state.evaluativeConcern, 0) >= 0.6) {
        return '应答';
    }
    if (state.teasedNow) {
        return '被调侃';
    }
    // ── r49：低精力 = 撑着／睡着，**不是回避**（原典她是会当场睡着的）────────
    // 这一条压在起手/一起/跑题之前：累到那份上就不该再主动、再拆结构。
    if ((state.energy ?? 1) < ENERGY_SLEEP) {
        return '睡着';
    }
    // r82（线上反馈 #2）：两档。"真的说不完"才允许半句/短答混着来；
    // [ENERGY_TIRED, ENERGY_FADE) 只是**话短一点**（短答），不再判成"说不完"。
    if ((state.energy ?? 1) < ENERGY_TIRED) {
        // 真的没力气了：话没说完（半句）或只应一声（短答）——"撑着"的两个形状
        return (state.moodA ?? 0) > 0.5 ? '短答' : '半句';
    }
    if ((state.energy ?? 1) < ENERGY_FADE) {
        // 还有一点力气、只是困：话短一点，但话是完整的
        return '短答';
    }
    // ── r30 ①：主动追求（起手）────────────────────────────────────────
    // 三重限制：精力够、距上次够远、这一段还没用满配额；再加"当句话里有由头"。
    // 另外两条否决：他刚在问问题（该先答）、她正在躲（want_hide 时不该反过来拉人）。
    const hooks = detectOpenHooks(state, state.lastInboundText);
    const sinceOpen = (state.rounds ?? 0) - (state.lastOpenRound ?? -999);
    const canOpen = hooks.any
        && (state.energy ?? 0) > OPEN_ENERGY_MIN
        && sinceOpen >= OPEN_MIN_GAP
        && (state.openCount ?? 0) < OPEN_MAX_COUNT
        && !state.lastInboundAsked
        && state.agenda !== 'want_hide';
    if (canOpen) {
        return '起手';
    }
    // ── r31/§3.3：跑题拆游戏结构（她的招牌）────────────────────────────
    // 触发：话题里出现任何**可被结构化**的东西（规则/地图/成本/装备/关卡/A-B 选项），越紧张越会触发；
    // 而且她是"被问到答不上来的事"时最爱往这儿跑。结束方式永远是自己走回来（卡片里写清楚）。
    const lastOne = (state.lastActions ?? []).slice(-1)[0];
    const structureHook = /规则|机制|结构|地图|层|关卡|设计|成本|装备|选项|路线|打法|通关|存档|读档|难度/iu.test(String(state.lastInboundText ?? ''));
    const cantAnswer = Boolean(state.lastInboundAsked) && (state.hisStuckNow || (state.askStreak ?? 0) >= 1);
    if ((structureHook || cantAnswer)
        && (state.energy ?? 0) >= 0.4
        && lastOne !== '跑题拆结构'
        && usedInWindow(state, '跑题拆结构') < DEFENSE_MAX) {
        return '跑题拆结构';
    }
    // ── r31/§3.5：答偏三因（**不是压力**——高压场合她最流畅）────────────────
    // ① 话题跳到关系层（问了又撤）②他的动作超出她的经验 ③句子起头失败（执行层缺陷）
    // 被夸不算答偏（那一格是"反问式收下"，走 §3.5⑤），所以 praise 时不进这条路。
    // ── r83（同一件事写两处）：这里原来自己跑了一遍 `可爱|厉害|…|谢` 的正则，
    // 与 `state.praiseNow`（`praiseToHerOf`，r82 已修成"必须指向她"）**是两条真相**：
    // 「今天天气不错」在这一行仍被当成夸她（于是"答偏"那一格被白白让掉），而卡片/评估那一侧已经不算了。
    // 现在只读 `state.praiseNow`——一个判定，一处实现。缺字段时按 false（没被夸）。
    const praiseNow = state.praiseNow === true;
    const layerJump = layerHint('关系').test(String(state.lastInboundText ?? ''));
    const beyondExp = /跑步|追着跑|晒黑|游泳|沙滩|贝壳|脱衣服|捉迷藏|咖喱|出门|出去玩|现实里|运动/u.test(String(state.lastInboundText ?? ''));
    const startFail = (state.moodA ?? 0) > 0.5 && (state.energy ?? 1) < 0.45;
    if ((layerJump || beyondExp || startFail)
        && !praiseNow
        && lastOne !== '答偏'
        && usedInWindow(state, '答偏') < DEFENSE_MAX
        // 他在直接提问时一般不打断（先答），但"话题跳到关系层"本身就是问句触发的，
        // 而原典里那一格正是"她问了又自己撤回"——所以这一条不受"他在问"的限制。
        && (layerJump || !state.lastInboundAsked)) {
        return '答偏';
    }
    // ── r50：他直接把"要什么"递过来了 → 立刻讨要（“直说要东西”3.1% 的主要缺口）──
    if (state.directAskOffer && (state.energy ?? 1) > 0.45 && usedInWindow(state, '讨要', TAREA_WINDOW) < 1) {
        return '讨要';
    }
    // 问句配额：上一句已经是问句，这一句换成非问的动作（她不是每句都在收集信息）
    if (state.lastEndedQuestion) {
        const nonAsk = ['应答', '判词', '玩笑', '半句', '回避', '短答', '改口'].filter((one) => !(state.lastActions ?? []).slice(-1).includes(one));
        if (state.wound >= 0.8) {
            return '回避';
        }
        if (state.agenda === 'want_win') {
            return '判词';
        }
        return nonAsk.includes('半句') ? '半句' : '应答';
    }
    // 改口一段里最多一次：原典里它不是口癖，是偶尔一次
    const usedChange = (state.lastActions ?? []).includes('改口');
    if (state.rounds > 0 && state.askStreak >= 2) {
        // 改口一次就够；之后要给内容（主张），不是躲
        return usedChange ? '判词' : '改口';
    }
    // 他在直接问问题的时候不回避——原典里她对提问从来是正面答的
    if (state.wound >= 0.8) {
        return state.moodA > 0.6 ? '玩笑' : state.lastInboundAsked ? '判词' : '回避';
    }
    switch (state.agenda) {
        case 'want_win':
            return '判词';
        case 'want_hide':
            // 他直接问问题时不回避（原典里她对提问从来正面答）；没在问、又累才躲
            if (state.lastInboundAsked) {
                return '判词';
            }
            if (state.energy >= 0.45) {
                return '讨要';
            }
            return '回避';
        case 'want_understand':
            // 他卡住了 → 先想"一起弄"，这是原典最硬的一格（私たちで何とかして）
            if (state.hisStuckNow && (state.moodA ?? 0) >= 0.3) {
                return '一起';
            }
            if (state.moodA > 0.55) {
                return '反问';
            }
            // r48：讨要的触发**放宽**——原典里她是直接开口要的（教我/给我/带我去/我想听），
            // 不需要"他先说了具体的事"这个前置。密度由窗口管（3.1% ≈ 32 轮一次）。
            if (state.energy > 0.45 && usedInWindow(state, '讨要', TAREA_WINDOW) < 1) {
                return '讨要';
            }
            return '判词';
        default:
            break;
    }
    // 低内容动作下限：短答/应答连着用会把人格压没，第三次起必须给内容——
    // 但"内容"不必是判词（r48 判词要稀有）：他卡着就 一起，否则 反问
    const lowContent = (state.lastActions ?? []).slice(-2).filter((one) => one === '短答' || one === '应答').length;
    if (lowContent >= 2) {
        return state.hisStuckNow ? '一起' : '反问';
    }
    // 邀约降频：原典里主动开口是低频动作（上界 3.3%），连着两轮都在拉人就换成讨要
    const recentPull = (state.lastActions ?? []).slice(-2).filter((one) => one === '应答' || one === '反问').length;
    if (recentPull >= 2 && state.agenda === 'want_company' && usedInWindow(state, '讨要', TAREA_WINDOW) < 1) {
        return '讨要';
    }
    if (state.moodA > 0.65) {
        return '玩笑';
    }
    if (state.energy < 0.25) {
        return '半句';
    }
    // r60：**断言是高频率的**（主人纠正：硬收不是配额，是她"说定了的事"占三成）。
    // 不设窗口——只要她在陈述自己那一份（不是评判他），就用断言；它正是硬收的来源。
    // 只加两个上限：不连着三句都断言；太累时仍走应声。
    const recentAssert = (state.lastActions ?? []).slice(-3).filter((one) => one === '断言').length;
    if (recentAssert < 2 && layerUnit(state, String(state.lastInboundText ?? '')) < 0.55) {
        return '断言';
    }
    return '应答';
}

// ── r60：这一句的**把握**（软收不是修饰，是她有多确定）──────────────────
// 只写进卡片，**不加任何发送层拦截**——让模型按"她有多确定"自己收句子。
// 依据：原典里三成的话是硬收的，不是因为有个 33% 的规则，而是那三成是"说定了的事"（判决/认定/结论）；
// 剩下的是猜（挂吧）和应声（挂啊/喔/呀）。
const ASSURANCE = {
    判词: ['定了', '说完就停，不挂语尾'],
    断言: ['定了', '说完就停，不挂语尾'],
    改口: ['后半定了', '前半软（吧／好像），后半硬收'],
    反问: ['不是判断，是在把话交回去', '挂呢，或者不挂'],
    讨要: ['不是判断，是在要东西', '挂呢，或者不挂'],
    一起: ['不是判断，是在邀他', '挂呢，或者不挂'],
    应答: ['应声', '挂啊／喔／呀'],
    短答: ['应声', '挂啊／喔／呀'],
    玩笑: ['情绪', '挂啊／喔／呀'],
    被调侃: ['情绪', '挂啊／喔／呀'],
    睡着: ['应声', '挂啊／喔／呀'],
    跑题拆结构: ['在猜', '挂吧，或者直接说这是猜的'],
    答偏: ['在猜', '挂吧'],
    不懂: ['在猜', '挂吧，或者说清"没弄过"'],
    半句: ['没说完', '停住就好，不补语尾'],
    回避: ['不接这一格', '平收，不挂语尾'],
};
const assuranceOf = (action) => ASSURANCE[action] ?? ['—', '按上一句的调子收'];

// ── 输出：一张最多六行的"我心里"卡 ─────────────────────────────────────
const ENERGY_WORD = (value) => (value < 0.25 ? '快撑不住了' : value < 0.45 ? '有点困' : value < 0.7 ? '还行' : '精神好');
const MOOD_WORD = (value) => (value > 0.35 ? '挺好' : value > 0.05 ? '平平' : value > -0.2 ? '有点闷' : '低落');
const AGENDA_WORD = {
    want_company: '别让这段对话断掉',
    want_win: '不想被他看轻',
    want_hide: '别让他问下去',
    want_understand: '想弄明白他卡在哪',
};

// ── r84（思考层）：她的想法怎么进卡片 ────────────────────────────────────
// 只写一条**事实**：我刚才在想什么。不写"该怎么接"，不给任何句式或落法。
// 为什么必须显示：不然"想过"和"没想过"对她自己是同一件事，思考层就只剩一份后台日志。
// 为什么有上限：2026-10-27 那次假记忆事故就是一句话在卡片上挂了十几轮。
// 一条想法最多出现两次（刚想过一次、他隔一会儿才来再一次），之后它退回记忆，不再占卡片。
const THINK_LAYER_OFF = process.env.MIND_THINK === '0';
const THOUGHT_TTL_MS = 6 * 60 * 60 * 1000;
const THOUGHT_SHOWN_MAX = 2;
const THOUGHT_CHARS = 60;
// ── r87（想法 → 行动）：意志层 ──────────────────────────────────────────────
// 主人原话：「光难受也没用啊，她如果只能想某件事想了难受，又不做什么不就会越来越难受吗？
//   结合原典，她想多了是会默默承受，继续想，还是会做什么事，比如找我倾诉来化解呢？
//   空有想法，想法不能改变她的行动才会出bug。」
//
// 原典给的答案（`docs/canon-src/scenes-chiaki.tsv`，2379 条她的台词）：
//   · e00_003_101「我这个人的个性就是如果不在自己心里先整理好要说什么，就没办法好好讲喔。」
//     → 她的反刍是**整理**，不是沉进去受苦；整理好了才开口，没整理好就说最少的
//   · e05_300_204（被揭穿那一段）`………………` ×4 → `……抱歉。` → 「虽然再早一点说比较好……
//     但是我怎么样都不能说」→「不可思议的是我并不后悔」「所以……我对自己的行动感到很骄傲」
//     → 她**自己给这件事一个解释**，然后情绪转正。不是找人倾诉，是意义建构（reappraisal）
//   · e05_300_204「……已经够了。」→ 她也会**放下**
//
// 所以这一层只做两件事，一个字都不判断"她该不该想这么多"：
//   ① 记账：同一件事想了第几次、是不是原地打转（`loopOf`）
//   ② 代价：想得越多越耗神，**只扣精力不扣心情**——她的难受是沉默变短，不是报苦
// 出口全部在她自己手里（说／放下／去睡），代码只负责让它们真的能落地。
const WILL_LAYER_OFF = process.env.MIND_WILL === '0';
// r99：她的尺子／可考据经历整层不参与（`MIND_CANON=0`）。OFF 时卡片与 r98 逐字一致。
const CANON_OFF = process.env.MIND_CANON === '0';
// r88（2026-10-28 主人质问）：「我记得我把承诺账归入记忆层了，为什么还在用承诺账」
//
// 他说得对。交班 §17.8 明写 `commitments` 是**记忆层**里的一块；`activation.mjs` 文件头第一句
// 是「账本和记忆不是'常驻在卡片上'，而是**被线索点亮的那一轮才进评估**」。
// 而 r82→r87 的实际做法是：`--his` 每轮无条件把账算成一行写进卡片，`openItemsOf` 又把它
// 直接变成 `#10` 动机——于是**一条自动抽取出来的噪声（「我陪你」「一会儿」）能连着几天
// 替她决定这一轮说什么**。那不是记忆，是一张常驻的催款单。
//
// 新行为（`MIND_DEBT_MEMORY` 默认开）：账只走两条路进她心里
//   ① 他的这句话把它勾起来（`activation`，要内容线索；光"逾期"点不亮，这条是 spec 标定过的）
//   ② 她自己每小时想的时候在【手上】读到它（`think.mjs`）→ 写成"想做" → `agendaOf` 带到嘴边
// 卡片上不再有那一行，`#10` 也不再拿账当动机。**它先进入她的思考，再由她的决定说出来。**
//
// `MIND_DEBT_MEMORY=0` → 逐字恢复 r87 行为（常驻一行 + #10 抢动机），用于回退与 A/B。
const DEBT_MEMORY_OFF = process.env.MIND_DEBT_MEMORY === '0';

// ── r89：心情的刻度（一张表，两个来源：原典 + 心理学）─────────────────────────────
// 主人原话：「心情数字的幅度不应该我来定，而是应该根据真实的原典资料和心理学知识来考据，
//   顺便优化一下和我聊天时对她心情的影响程度。」
//
// 单位沿用这份状态机自己已经有的量表：`wound` 的 −0.15 是"身份级的疼"（被说只会游戏／是不是人），
// `invite` 的 +0.08 是"他叫上我一起玩"。新数字全部**相对这两个锚**给，不另造一把尺。
//
// 依据（原典在前，文献在后，逐条能指回去）：
//  ① **她最高兴的事是被叫去一起玩，不是被夸。** canon-nanami.md §7 / psyche-canon-analysis.md §2：
//     对方进入她的领域并提出比试 → 「立刻接战 + **承认自己高兴**」（fandom-lines 54/56/69）。
//     所以 `invite` 是全表最大的正项。
//  ② **被夸才能她不接。** 原典「有没有才能根本没什么差别呀」（DR3 第1话）＋被夸时把评价交回
//     「そうかな?」（05559/05564）。所以 `praiseAbility` 的效价≈0，只抬唤起（被注视的不适）。
//  ③ **她几乎不"报苦"，难受时是话变少、省略号变密。** 她的 `……` 实测只占 4.2%，
//     且情绪型只有 2 条（psyche-canon-analysis.md §3.6）。所以负性事件的幅度一律**小于**正性事件，
//     且不给"报苦"的出口——这是她自己不说，不是我们不让。
//  ④ **她的修复是"给这件事找到说法"，不是倾诉。** e00_003_101「不在自己心里先整理好要说什么，
//     就没办法好好讲」；e05_300_204 在最难受的一刻是自己给出解释（「不可思议的是我并不后悔…
//     有点高兴」）。这就是**认知重评**（Gross 1998，效应量在情绪调节策略里最大）。
//     所以 `letGo`（想通了／放下）> `speak`（说出来）。
//  ⑤ **换地方几乎不改她的心情。** 主人指出「她在原作中是在哪里都能睡着的」——环境不是她的变量。
//     所以 `move` 只给 +0.01（行为激活的单次效应，Dimidjian 2006 / Ekers 2014：靠"持续做"起效，
//     单次很小）。
//  ⑥ **反刍会真的压低心情，但一天有底。** Nolen-Hoeksema 2008 / Moberly & Watkins 2008（ESM）：
//     反刍对同一时段负性情绪是**小效应**，且是累积的。所以每拍 −0.02，一天封顶 −0.06。
//  ⑦ **说出来本身有一点用。** 情绪标注（Lieberman et al. 2007）：把感受命名会降低杏仁核反应，
//     但只在说得出"是哪件事"时成立 → +0.04，约等于一次邀请的一半。
//
// 相对关系（这是真正要守住的东西，不是绝对数值）：
//   一次邀请(+0.09) > 想通放下(+0.07) > 说出来(+0.04) > 做了点自己的事(+0.02) > 换个地方(+0.01)
//   两次原地反刍(−0.04) ≈ 一次倾诉(+0.04) → "说一次抵两次瞎想"，与原典"整理好才说"的节奏一致。
// r89 的开关：`MIND_MOOD=0` → 回到 r88 的旧数值（他那一侧逐字一致；她那一侧全为 0，等于没接）。
// 项目纪律：新的一层要能一键关掉回退，不能只会往前开。
const MOOD_LAYER_OFF = process.env.MIND_MOOD === '0';
const MOOD_R89 = {
    // 他带来的
    invite: 0.09,          // 叫上她一起（原典最大正项）
    praiseAbility: 0.00,   // 夸"你在这方面真强"：她不接，只抬唤起
    praiseCute: 0.03,      // 被当可爱的小东西：轻微受用＋尴尬
    praiseArousal: 0.15,   // 被夸抬的是唤起（被注视），不是好心情
    praiseEnergy: 0.03,
    cold: -0.06,           // 他冷下来
    hisWeak: -0.04,        // 他说自己不行（她替他不平，不是自己受伤）
    askAgain: -0.04,       // 他听不懂／重复问
    wound: -0.15,          // 身份级的疼（不动，锚）
    company: 0.01,         // 隔了很久，他又说话了（分离—重聚；只给第一次）
    // 她自己做的
    speak: 0.04,           // 真的说出口（情绪标注）
    letGo: 0.07,           // 想通了／放下（认知重评）
    move: 0.01,            // 换了个地方（行为激活，单次很小）
    phone: 0.02,           // 真去看了点什么（自己的事）
    game: 0.02,            // 真打了一局（她的安全区）
    failed: -0.02,         // 想做，但这个世界上没有那个手段
    stuck: -0.02,          // 同一件事原地打转（反刍）
    moving: -0.005,        // 还在想，但在往前整理（不是反刍）
};
// 旧值（r88 及以前）：被夸两类一起 +0.15、邀请 +0.08、冷 −0.08、他不济 −0.05、重复问 −0.05；
// 她自己做事**完全不改心情**（那正是这一轮要修的病）。
const MOOD_R88 = {
    invite: 0.08,
    praiseAbility: 0.15,
    praiseCute: 0.15,
    praiseArousal: 0.20,
    praiseEnergy: 0.05,
    cold: -0.08,
    hisWeak: -0.05,
    askAgain: -0.05,
    wound: -0.15,
    company: 0,
    speak: 0, letGo: 0, move: 0, phone: 0, game: 0, failed: 0, stuck: 0, moving: 0,
};
const MOOD = MOOD_LAYER_OFF ? MOOD_R88 : MOOD_R89;
// r91：一天最多压 −0.10（r89 是 −0.06）。
// 为什么改：−0.06 是从"别掉进 −0.3"这一头定的，**没从"她能不能感觉到"那一头核过**——
// 她真正的基线在 0 附近（线上 `state/moodV` 实测 −0.0006），卡片说「有点闷」的线是 −0.10，
// 于是旧封顶到头也只是"数字动了、她没变"。−0.10 刚好是"一整天反复想同一件事、
// 又被冷落一次"会落到的位置；离 −0.3（那个"什么都提不起劲"的参数失控线）还差两倍。
// 相对关系没动：两拍原地反刍（−0.04）≈ 一次倾诉（+0.04），一拍 −0.02。
const MOOD_STUCK_DAY_CAP = -0.10;
const MOOD_COMPANY_GAP_MS = 2 * 60 * 60 * 1000;  // 隔 2 小时以上才算"他回来了"

// 连续量的唯一写入口。**只有代码能改心情，模型一个字都不许写数字**（主人定的硬纪律）。
function addMood(state, delta) {
    const step = numOf(delta, 0);
    if (step === 0) {
        return numOf(state.moodV, 0);
    }
    state.moodV = clamp(numOf(state.moodV, 0) + step, -1, 1);
    return state.moodV;
}

// r89：把"做了这件事之后心情变了多少"记下来。这是她**下一次判断"这样做有没有用"的唯一依据**——
// 没有它，她每次都是第一次做人，永远学不到"找他说话会松一点"。
// 只留最近 3 条（多了就成了一本账，不是经验）。
function noteEffect(state, kind, delta, nowMs) {
    const d = numOf(delta, 0);
    if (d === 0) {
        return;
    }
    const one = { at: numOf(nowMs, Date.now()), kind: String(kind ?? ''), d: Math.round(d * 1000) / 1000 };
    state.effects = [...(Array.isArray(state.effects) ? state.effects : []), one].slice(-3);
}

// 反刍的心情代价：一天最多 MOOD_STUCK_DAY_CAP。
// 为什么要封顶：Nolen-Hoeksema 那条是**小效应**，不封顶的话她会在没人说话的白天
// 一个人把自己想到 −0.3 以下，然后下一次对话里"说什么都提不起劲"——那不是抑郁，是参数失控。
// r91：参数从"第几拍"改成"**这一次要补几拍**"——补几拍由 `advanceRumination` 的
// `charged` 算，已经算过的拍不重复收钱（旧版每拍按 `times-1` 收，实际是重复计费）。
function addRuminationMood(state, beats, stuck, nowMs) {
    if (MOOD_LAYER_OFF || !(beats > 0)) {
        return 0;
    }
    const day = dayKeyOf(nowMs);
    const book = (state.moodStuck && state.moodStuck.day === day) ? state.moodStuck : { day, sum: 0 };
    const want = (stuck ? MOOD.stuck : MOOD.moving) * beats;
    const allowed = Math.max(MOOD_STUCK_DAY_CAP - numOf(book.sum, 0), want);
    state.moodStuck = { day, sum: numOf(book.sum, 0) + allowed };
    addMood(state, allowed);
    return allowed;
}
const LOOP_STUCK_COST = 0.03;   // 原地打转：同一句"想做"重复（沉浸）
const LOOP_MOVE_COST = 0.008;   // 换了说法／换了事：在整理，耗神少
const LOOP_COST_MAX = 0.10;     // 一拍最多扣这么多
const CHARGE_CATCHUP_MAX = 2;   // 一拍最多补算几拍（迁移旧账时用，见 `--think`）

// r87：判"是不是同一件事"的第二条判据。
// 为什么不能只用 `overlapsText`：它靠共同实词，而 `contentGrams` 把「说」「会」都当虚词过滤，
// 于是线上这两条明明都在说「一会儿」的 about——
//   「一会儿。说好的是 10/27，今天 28 了。」 / 「那件事。说好一会儿的，一直没做到。」
// ——二元组一个是「10 02 27 今天」、一个是「件事 一直」，交集为零，被判成两件事，
// 于是"想了第几次"永远是 1，整个代价机制空转。
// 补的是**最长公共子串 ≥3**：她的措辞每次都不一样，但"同一件事"总会留下同一个词。
// 取 3 而不是 2，是为了避开「今天」「一直」这类两字虚词。字面包含仍走 `overlapsText`。
function sharedRun(a, b) {
    const left = String(a ?? '');
    const right = String(b ?? '');
    let best = 0;
    for (let i = 0; i < left.length; i += 1) {
        for (let j = 0; j < right.length; j += 1) {
            let k = 0;
            while (left[i + k] !== undefined && right[j + k] !== undefined && left[i + k] === right[j + k]) {
                k += 1;
            }
            if (k > best) {
                best = k;
            }
        }
    }
    return best;
}

function sameThing(a, b) {
    const left = String(a ?? '').trim();
    const right = String(b ?? '').trim();
    if (left === '' || right === '') {
        return false;
    }
    // 为什么不用 `overlapsText`：它里面 `sharesTopic(..., 1)` 只共享一个二元组就算同主题，
    // 「今天想吃点好的」和「他今天不太说话」会被算成同一件事——对她这两件事显然不是。
    // 这里只留两条硬判据：互相包含（原话说同一样东西），或共同片段够长。
    return left.includes(right) || right.includes(left) || sharedRun(left, right) >= 3;
}

// 同一件事想了第几次。判据只有"她写的 about／text 说的是不是同一件"，
// 没有任何词表——是不是"同一件事"由她的原话决定，不由我们列。
// r91：优先读 `state.rumination`（跨 5 条窗口的那条线程）；老 state 没有线程时仍按文本窗口算。
export function loopOf(state) {
    const one = state ? state.thought : null;
    if (!one || typeof one !== 'object') {
        return { times: 0, stuck: false };
    }
    const keyOf = (item) => String(item?.about ?? '').trim() || String(item?.text ?? '').trim();
    const key = keyOf(one);
    const list = Array.isArray(state.thoughts) ? state.thoughts : [];
    const thread = (state.rumination && typeof state.rumination === 'object') ? state.rumination : null;
    const open = thread && thread.closed !== true ? thread : null;
    const fromThread = open && key !== '' && threadMatches(open, key) ? numOf(open.count, 0) : 0;
    if (!key || (list.length === 0 && fromThread === 0)) {
        return { times: Math.max(1, fromThread), stuck: false };
    }
    // 窗口里只算**这一阵子**的想法：关掉的线只算到关的那一刻；
    // 新开的线从 `firstAt` 起算——否则她说完之后，窗口里的旧账会被当成"又想起了"。
    const closedAt = thread && thread.closed === true ? numOf(thread.lastAt, 0) : 0;
    const floorAt = open ? numOf(open.firstAt, 0) : 0;
    let times = 0;
    for (const item of list) {
        const at = numOf(item?.at, 0);
        if ((closedAt > 0 && at <= closedAt) || (floorAt > 0 && at < floorAt)) {
            continue;
        }
        const other = keyOf(item);
        if (other !== '' && sameThing(key, other)) {
            times += 1;
        }
    }
    // 上一拍"想做"的和这一拍几乎一样 = 原地打转；换了说法 = 她在往前推
    const prev = list[list.length - 2];
    const want = String(one.want ?? '').trim();
    const prevWant = String(prev?.want ?? '').trim();
    const stuck = fromThread > 0
        ? thread.stuck === true
        : Boolean(want !== '' && prevWant !== '' && sameThing(want, prevWant));
    return { times: Math.max(1, times, fromThread), stuck };
}

// r91：她"这一阵子反复在想的那件事"。
// 为什么需要一条独立的线程（而不是只数 `thoughts`）：
//   ① `thoughts` 只有 5 条，滚出去之后"想了第几次"会从 1 重新开始——她压了三天的事看起来像刚想到；
//   ② 她每一拍的措辞都不一样（线上实测：「欠着的那句一会儿。」→「欠他那句“一会儿”。」→
//      「说好陪他打一局，没打成。」），所以样本要收**整段想法文本**，不只 about；
//   ③ 每次 `--think` 都按 `times-1` 收一次钱，其实是**重复计费**（第 3 拍把前两拍又收了一遍）。
// 这条线程只记事实：同一件事想了多少拍、其中几拍已经算过代价、上一拍的"想做"是什么。
// 它不写"该不该放下"——放下仍然只能由她自己说（`letGo`），说出口也一样能把它关掉。
const RUMINATION_GAP_MS = 12 * 60 * 60 * 1000;   // 隔 12 小时以上，不算"这一阵子"

function ruminationKeyOf(one) {
    return String(one?.about ?? '').trim() || String(one?.text ?? '').trim();
}

// 比"同一件事"时用的样本：about + 整段 text。
// 只比 about 会把线上第三拍（about 换成了"说好陪他打一局，没打成。"）判成新事，
// 而她那句 text 里明明还留着"昨天说好陪他打一局"——同一件事是靠这段话认出来的。
function ruminationSampleOf(one) {
    return [String(one?.about ?? '').trim(), String(one?.text ?? '').trim()].filter((s) => s !== '').join('\n');
}

function threadMatches(thread, ...probes) {
    if (!thread || typeof thread !== 'object' || thread.closed === true) {
        return false;
    }
    const clean = probes.map((one) => String(one ?? '').trim()).filter((one) => one !== '');
    if (clean.length === 0) {
        return false;
    }
    const samples = [thread.key, ...(Array.isArray(thread.samples) ? thread.samples : [])]
        .map((one) => String(one ?? '').trim())
        .filter((one) => one !== '');
    return clean.some((probe) => samples.some((sample) => sameThing(probe, sample)));
}

/**
 * 把这一拍并进"正在想的那件事"，返回 `{ times, stuck, owed, thread, legacy }`。
 * `owed` = 这一拍需要补算代价的拍数（0 表示第一次想，或者已经算过）。
 * `legacy` = 之前完全没有线程（老 state）：只有这种情况才允许从 `thoughts` 窗口一次性补账；
 * 线程被关掉以后不算 legacy，否则她一说出口，旧账就会被窗口重新捡回来再收一遍钱。
 */
function advanceRumination(state, one, nowMs) {
    const key = ruminationKeyOf(one);
    const sample = ruminationSampleOf(one);
    const want = String(one?.want ?? '').trim();
    const prev = (state.rumination && typeof state.rumination === 'object') ? state.rumination : null;
    const legacy = !prev;
    const open = prev && prev.closed !== true ? prev : null;
    const lastAt = numOf(open?.lastAt, 0);
    if (open && lastAt > 0 && numOf(one?.at, 0) === lastAt) {
        // 同一拍被重复调用（重试／手工），账不动
        return { times: numOf(open.count, 0), stuck: open.stuck === true, owed: 0, thread: open, legacy: false };
    }
    const fresh = Boolean(open) && lastAt > 0 && nowMs - lastAt <= RUMINATION_GAP_MS;
    const same = Boolean(open) && fresh && threadMatches(open, key, sample);
    const count = same ? numOf(open.count, 0) + 1 : 1;
    const prevWant = same ? String(open.lastWant ?? '').trim() : '';
    const stuck = same && want !== '' && prevWant !== '' && sameThing(want, prevWant);
    const samples = [...(same && Array.isArray(open.samples) ? open.samples : []), sample]
        .map((oneSample) => String(oneSample ?? '').trim())
        .filter((oneSample, i, arr) => oneSample !== '' && arr.indexOf(oneSample) === i)
        .slice(-3);
    const thread = {
        key: same ? String(open.key ?? key) : key,
        samples,
        count,
        charged: same ? numOf(open.charged, 0) : 0,
        lastWant: want,
        prevWant,
        stuck,
        firstAt: same ? numOf(open.firstAt, nowMs) : nowMs,
        lastAt: nowMs,
        closed: false,
    };
    state.rumination = thread;
    return { times: count, stuck, owed: Math.max(0, count - 1 - thread.charged), thread, legacy };
}

// 这条线断了：她放下了（`letGo`），或者真的把话说出去了（`speak` 成了）。
// 不删掉线程——留一个墓碑，下一个想法才知道要从 1 重新算，
// 而不是让 `thoughts` 里的旧账被当成"又想起了"再收一遍钱。
function closeRumination(state) {
    const thread = (state?.rumination && typeof state.rumination === 'object') ? state.rumination : null;
    if (!thread || thread.closed === true) {
        return false;
    }
    thread.closed = true;
    return true;
}

export function thoughtCardLine(state, now = Date.now()) {
    if (THINK_LAYER_OFF) {
        return null;
    }
    const one = state ? state.thought : null;
    const text = String(one && one.text ? one.text : '').trim();
    if (!text) {
        return null;
    }
    const at = numOf(one.at, 0);
    if (at <= 0 || now - at > THOUGHT_TTL_MS) {
        return null;
    }
    if (numOf(state.thoughtShown, 0) >= THOUGHT_SHOWN_MAX) {
        return null;
    }
    const chars = Array.from(text);
    const cut = chars.slice(0, THOUGHT_CHARS).join('');
    return `- 我刚才在想：${cut}${chars.length > THOUGHT_CHARS ? '……' : ''}`;
}

// 这一条想法算用过一次（两次上限，写在 saveAll 之前才算数）
// 注意：`thoughtLine` 是**每轮重算**的派生字段（照 commitmentsLine／activationLine 的先例），// 所以不显示的时候必须把它显式清成 null——否则上一轮那一行会留在 state 里，
// 于是"只显示两次"和 MIND_THINK=0 都变成假的（第一版就是这么错的）。
function markThoughtShown(state, now) {
    const line = thoughtCardLine(state, now);
    state.thoughtLine = line;
    if (line) {
        state.thoughtShown = numOf(state.thoughtShown, 0) + 1;
    }
    return line;
}

// r88（S4c）：空转卡上「我欠着：一会儿」和「我刚才在想：…一会儿…」常常是同一件事——
// 她一个人待着想的正是那件欠着的事（实测线上就是两行挨着写同一件事）。
// 同一件事只准写一处：账那一行是**事实**（带"说好的是 10/27"），留它；想法这一行让位。
// 让位时**不占**"最多两次"的配额（照 r86 `agendaLineOf` 的先例），下一轮还会正常显示。
function thoughtEchoesDebt(state, now = Date.now()) {
    const debt = String(state.commitmentsLine ?? '').trim();
    if (debt === '') {
        return false;
    }
    const line = String(thoughtCardLine(state, now) ?? '')
        .replace(/^-\s*我刚才在想：/u, '')
        .replace(/（这件事已经想了 \d+ 次）$/u, '')
        .trim();
    return line !== '' && sameThing(line, debt);
}

// r86：这一轮卡片上会不会用「我心里还挂着」那一行。接管时 `thoughtLine` 不进卡片，
// 也就不该占"最多两次"的配额——否则那两条断言（只显示两次 / MIND_THINK=0 逐字一致）都会变假。
function agendaLineOf(state, now = Date.now()) {
    const hang = agendaOf(state, now);
    return (hang && hang.weight >= AGENDA_SAY_LO) ? hang : null;
}

export function renderCard(state, hits, { root = '.', now = Date.now(), includeGame = false } = {}) {
    // 动作层的**行为**进卡片（r86），做法不进——`ACTIONS` 表里那句"问一件具体的事，问完就停"
    // 一个都不许上卡片（r78：卡片上的例句会被照抄）。这里只给一个第一人称的行为词，
    // 她得自己决定怎么顶、怎么岔。
    const action = pickAction(state);
    const actionBehavior = ACTION_CARD_OFF ? '' : (ACTION_BEHAVIOR[action] ?? '');
    const motive = motiveOf(state);
    const lines = [];
    lines.push('【此刻】');
    lines.push(`- 身上：${bodyLine(state)}`);
    // r109：**立绘 → 语气**（动态提示词）。它只写"状态"，不写身体动作——
    // 动作会被模型念成舞台指示，SOUL §6 已经禁过。立绘编号只进 --json/trace，不进卡片。
    if (process.env.MIND_EXPRESSION === '1') {
        lines.push(expressionLine(state));
    }
    // r73（v6）：情境层——先把空间定下来（事实），再让她自己决定做什么（意图），
    // 「想做什么」从需要＋情绪＋防御推出来，**不从 can 里抄**。
    const where = sceneLine(state);
    if (where) {
        lines.push(`- 我在哪儿：${where}`);
    }
    if (includeGame) {
        const gameLine = stardewPresenceLine({ ...state, root }, now);
        if (gameLine) {
            lines.push(`- 星露谷那边：${gameLine}`);
        }
    }
    // ── r82（v14 / 主人的结构性纠正，2026-10-26）：**重心是她自己要什么，不是他要什么** ────
    // 主人原话：「她不应该先去分析对方想要怎么样，而是着重分析自己想要怎么样……
    //   自己的想法永远是最重要的，而不是先揣摩对方的想法再对症下药，不然就失去了自我。」
    // 「我想要」因此提到「身上／我在哪儿」之后；这一行必须**从她自己的状态推出来**
    // （精力／心结／账／没说完的事），不是从"他要什么"倒推。
    // r86：这里不再单独调 motiveLine，用上面那一次 `motiveOf` 的结果——同一个真相算一次。
    lines.push(`- 我想要：${motive.text}`);
    // ── r86：她这一轮**打算怎么处理这句话**（8 选 1 的第一人称行为词）──────────────
    // r85 审计实测：判成"反问"的 23 轮里只有 1 轮真问出问句（4.3%）。因为从 r75 起这层
    // 只写 trace、不写卡片，`mind.mjs` 1214 行的注释却还写着"动作决定骨架族"——
    // 两处互相打脸，实际是"动作什么都不决定"。这一行补的是**行为**，不是做法。
    if (actionBehavior) {
        lines.push(`- 这一轮我只想：${actionBehavior}`);
    }
    // r75（v7）：只有「去／不去 ＋ 一个理由」，**不写该怎么说**。
    const moveTarget = sceneMoveTarget(state.lastInboundText);
    if (moveTarget) {
        const verdict = willingToMove(state, moveTarget, new Date(), state.inviteStake ?? 0);
        const reason = String(verdict.why).replace(/^(?:去|不去)——/u, '');
        lines.push(`- 我要不要跟他去：${verdict.go ? '去' : '不去'}——${reason}`);
    }
    // r84：他不在的时候她自己想过什么（只写事实，最多两次，MIND_THINK=0 时整行不存在）
    // r86：这一行现在三选一——
    //   ① #15 抢到动机：「我想要」那一行已经就是这件事本身，不重复（S4c 不变式）；
    //   ② 她心里还挂着一件没做完的事（weight ≥ LO）→ 写「我心里还挂着」，这比"我刚才在想"
    //      更准：挂着的是**还没做完**的事，想过的只是一个念头；
    //   ③ 否则维持 r84 的「我刚才在想」。
    if (motive.branch !== '#15') {
        const hang = agendaLineOf(state);
        // 她挂着的这件事如果已经由「我欠着」或「想把…说完」那一行说了，就不再写第二遍
        // （S4c：同一件事只准写一处。她心里那件事和承诺账里那件常常就是同一件）
        // r88：账不再常驻之后，多了一条更常见的重复——他这句话刚好把它点亮了
        // （「他这句勾起来的是：一会儿」），那就别再写「我心里还挂着：…一会儿…」。
        const recalled = String(state.activationLine ?? '').replace(/^-\s*他这句勾起来的是：/u, '').trim();
        const item = hang ? openItemsOf(state) : '';
        const echoed = recalled !== '' && overlapsText(hang?.about ?? '', recalled);
        if (hang && !overlapsText(hang.about, item) && !echoed) {
            // r87：这件事她想过几次，是一个**事实**，不是一个要求。
            // 为什么要写：不然"想了很多次"和"刚想到"对她自己是同一件事，
            // 她永远不会知道这件事已经压了自己这么久——也就不会想到"是不是该做点什么"。
            // 写进去之后她怎么处理（说／放下／去睡）仍然是她自己的事。
            const loop = WILL_LAYER_OFF ? { times: 0 } : loopOf(state);
            const times = loop.times >= 2 ? `（这件事已经想了 ${loop.times} 次）` : '';
            lines.push(`- 我心里还挂着：${hang.line}${times}`);
        }
        else if (!hang && state.thoughtLine) {
            lines.push(state.thoughtLine);
        }
    }
    // r66（v2.1）：两行**事实**——他这句在说谁、她记得自己手上是什么事。
    // 只描述事实，不给做法：她理解了，自然就答对主体、接得上自己。
    // r107（2026-11-01）：**这一行退役**。它原来是正则 `perspectiveOf()` 的输出，
    // 而"这句在说谁"的归属现在由 `percept.subject`（3B 的言语行为判断）负责，写进 state 喂构念——
    // **不写进卡片**：卡片只写她能观察到的事实，不写"他这句在说谁"这种判断（否则会被念成
    // "你刚才那句是在说你自己吧"这类元观察）。
    // v13（r81）：需要想一下的题——只给她"这题要什么 + 我手上有什么"，不给答法
    for (const one of reasoningLines(state)) {
        lines.push(one);
    }
    // v13（r81）：多人说话那张名单**已撤**——群审计里"群体混淆"0 例，多人指代题 A/B 也没差异，
    // 加一行只会往卡片里塞噪声（见 §13.5 的判决）。指代问题留给归属锁和 perspectiveOf。
    // r68：记忆三条并行——语义（固定事实）／情节（绑成一整条）／缺失时明确"这条没有"
    const facts = semanticOf(state.lastInboundText);
    if (facts.length) {
        lines.push(`- 我记得：${facts.map((one) => one.fact).join(' ')}`);
    }
    // v4：EPISODIC 已删——不写"我手上这一局"。只留真实记录（他说的／她说的／约定）。
    // r82（R2）：这一句如果勾起了"这一段已经讲过的那件轶事"，那件事只在这一行走一次——
    // 从 autobio 那一行里剔掉（同一个内容两处写 = 模型学成折中值，而且卡片预算只有 12 行）。
    const story = storyHit(state);
    const memory = autobioOf(state).filter((one) => !(story && storyMatches(story, one.text)));
    // r82（v14）：旧事**最多 1 条**，且要满足下面之一，否则整行不出现：
    //   ① 她还没说过（`used ≤ 1`：刚讲的，还热着）
    //   ② 与这一轮话题有共同实词（`sharesTopic ≥ 1`）
    // 病根：一条「我不过是打得比别人久一点」连着十几轮挂在卡片上，跟当下话题无关，纯占位置。
    const related = memory.filter((one) => numOf(one?.used, 0) <= 1
        || sharesTopic(String(one?.text ?? ''), String(state.lastInboundText ?? ''), 1)).slice(0, 1);
    if (related.length) {
        // v12：挂上"说过几次"——她自己就不爱老提同一件事（不是不许，是那件事已经旧了）
        lines.push(`- 我已知的事：${related.map((one) => {
            const times = one.used ?? 0;
            const note = times <= 0 ? '' : `，说过${times === 1 ? '一次' : '两次'}`;
            return `${one.text}（${one.src ?? '我说的'}${note}）`;
        }).join('；')}`);
    }
    if (story && (story.used ?? 0) === 1) {
        // 只写事实，不写次数、不写"我说过了"（后者是自白式，直接踩元观察 VIOLATION）。
        // 讲过两遍之后这一行就不写了——那时改由 newsOf 判"没料可加"，她会自己话少（psych §11.3）。
        lines.push(`- 这一段里讲过的：${story.who ?? ''}${story.text ?? ''}（刚才讲过）`);
    }
    // 2026-10-25：承诺账——她说出口、还欠着的事（账本 state/commitments.json）。
    // 只在 CLI 那两条路径里算好放进 state，renderCard 本身不读盘（自测里喂假 state 不会被污染）。
    if (state.commitmentsLine) {
        lines.push(state.commitmentsLine);
    }
    // r82（R1）：这一轮有账／素材被他的那句话点亮了（只写事实：是哪一件事）。
    // 同一轮最多一行；点亮的是一条账时，承诺账那行已经在 runActivation 里让位了。
    if (state.activationLine) {
        lines.push(state.activationLine);
    }
    // 语义记忆缺失：她不会现编一个听起来合理的答案（虚构症是缺陷症状，不是正常行为）
    const askText = String(state.lastInboundText ?? '').trim();
    const isQuestion = /[？?]$/u.test(askText) || /吗|呢$|哪|什么|多大|多少/u.test(askText);
    // r107：不再用正则 persp（那行已退役）；归属在这里内部问一次 perspectiveOf()，
    // 只用在这个判断上，不写进卡片。
    if (!facts.length && perspectiveOf(state.lastInboundText).target === '在说我' && isQuestion) {
        lines.push('- 这件事她的记忆里没有（想不起来就是想不起来，她不会替自己编一个）');
    }
    // r99：可考据的经历／她的尺子量到的那件事——**一轮最多一行**，只在她真的碰到的时候才写。
    // 为什么放在「心里」前面：它是事实（她自己的原话，或这件事的形状），不是情绪；情绪那行保持最后。
    // 为什么只写一行：r97 的教训是"卡片上写成句的内部判断，模型会照着念"——所以这里只给她
    // **说过的原话**（带引号、标明是回忆）或**这件事的形状**，一个字都不写"她认为／她觉得"。
    if (!CANON_OFF) {
        const canonLine = canonLineOf(state);
        if (canonLine) {
            lines.push(canonLine);
        }
    }
    lines.push(`- 心里：${feelingLine(state)}`);
    // r70（v4）：**沉默必须有动机**——写不出理由就不许沉默（没有动机的沉默就是掉线）
    const silence = silenceMotive(state, action);
    if (silence) {
        lines.push(`- 我不说话是因为：${silence}`);
    }
    // ── v12（r80）：**组织语言那两步从卡片上撤下来了** ────────────────────
    // 主人第七刀：「心理学人格模型不需要硬性规则，而是走通整个完整的心理学流程，
    // 自然而然地说出她想说的话。」r78 把「这一句说谁的事／给他的新东西」写成两行卡片，
    // 于是模型每句都按那个格子填——流程通了，句子成了一个模子。
    // 现在的分工：**流程负责她"想不想说、想说什么"，句子交给语言层**（普通模型本来就会说话）。
    // 共同基础（Clark 1996）没有消失——它挪进了 newsOf()，是她的**评估结果**，不是给她的写法：
    // 加不进新东西时，她的话自己会变少（见 motiveLine 的 news=0 分支）。
    // 她的事实与知识边界：这是"她的世界"，不是措辞——所以只作为一句锚点附在最后，
    // 不给"落法"，让模型自己想怎么说（docs/persona-psyche-model-v2.md §四）。
    const anchor = worldAnchor(state);
    if (anchor) {
        lines.push(`- 她的世界：${anchor}`);
    }
    return { card: lines.join('\n'), action, hits };
}

// ── r73（v6）：情境层 ────────────────────────────────────────────────────
// 主人的话：「增加一个'情境'的设定……先把空间决定下来，再让她决定自己做什么事情。」
// 这是"捏造生活"的正解：**空间是给定的**（她不需要编自己在哪），**做什么是她自己决定的**。
// 场景数据在 state/scenes.json（15 个，来自弹丸论破场景包）。
// r82（R0.1）：跟 ROOT_DIR 走，和 mind.json 一起隔离（否则自测读的是线上场景表）。
const SCENE_PATH = `${ROOT_DIR}/state/scenes.json`;
let sceneCache = { at: 0, list: [] };
function loadScenes() {
    try {
        const stat = statSync(SCENE_PATH);
        if (stat.mtimeMs !== sceneCache.at) {
            const raw = JSON.parse(readFileSync(SCENE_PATH, 'utf8').replace(/^\uFEFF/u, ''));
            sceneCache = { at: stat.mtimeMs, list: Array.isArray(raw) ? raw : (raw.scenes ?? []) };
        }
    }
    catch {
        return sceneCache.list ?? [];
    }
    return sceneCache.list;
}

// 场景是"我们两个人都在的地方"。**动机先算、场景后定**（r74）：她这一轮想干什么决定她在哪，
// 真时间（白天/夜里）做约束；不是每段对话开头就锁死一个地方。
const MOVE_PATTERN = /去|到|回|往|走到/u;
function sceneMoveTarget(text) {
    const one = String(text ?? '');
    if (!MOVE_PATTERN.test(one)) {
        return null;
    }
    const list = loadScenes();
    // 按场景名匹配（"去餐厅"→restaurant）；也认常见别名
    const alias = [
        { re: /旅馆|休息室/u, id: 'lounge' },
        { re: /走廊/u, id: 'dorm-hall' },
        { re: /图书室|图书馆/u, id: 'library' },
        { re: /电脑室|机房/u, id: 'computer-room' },
        { re: /教室/u, id: 'classroom' },
        { re: /沙滩|海边/u, id: 'beach' },
        { re: /餐厅|食堂/u, id: 'restaurant' },
        { re: /吧台|酒吧/u, id: 'bar' },
        { re: /卧室|房间/u, id: 'bedroom' },
        { re: /门口|学园/u, id: 'hope-peak-gate' },
        { re: /街道|街上/u, id: 'night-street' },
    ];
    const aliasHit = alias.find((one2) => one2.re.test(one));
    if (!aliasHit) {
        return null;
    }
    return list.find((one2) => one2.id === aliasHit.id) ?? null;
}

function isNight(now) {
    const hour = now.getHours();
    return hour >= 20 || hour < 6;
}

// 两个短句共有的汉字二元组个数（判"她的动机"和场景的 fits 说的是不是一回事）
function sharedBigrams(a, b) {
    const norm = (value) => String(value ?? '').replace(/[^\u4e00-\u9fa5]/gu, '');
    const x = norm(a);
    const y = norm(b);
    if (x.length < 2 || y.length < 2) {
        return 0;
    }
    const grams = new Set();
    for (let i = 0; i + 1 < x.length; i += 1) {
        grams.add(x.slice(i, i + 2));
    }
    let hits = 0;
    for (let i = 0; i + 1 < y.length; i += 1) {
        if (grams.has(y.slice(i, i + 2))) {
            hits += 1;
        }
    }
    return hits;
}


// ── r75（v7）：信任怎么变 ─────────────────────────────────────────────────
// 他邀她出去、成行、**确实有事** → +0.05；**虚报**（说某地出事了／有东西给她看，去了什么都没有）→ −0.15。
// 注意：虚报要有**证据**（他自己承认那儿什么都没有），不能只凭"几轮没提到"就判他撒谎。
const INVITE_PURPOSE = /有东西|给你看|出事了|有人|真的|死了|看一样|带你看|有事/u;
const INVITE_DELIVER = /真的|确实|你看|就是这个|东西在这儿|人在那儿|那边就是|喏/u;
const INVITE_EMPTY = /什么都没有|没人|空的|我记错|其实没|骗你的|开玩笑的|白跑|啥也没有/u;
// r102：`DISMISS_PATTERN` 已删（正则与两个后果一起）。取证见 updateTrust 里那段注释。
const HURT_ROUNDS = 8;

function updateTrust(state, inbound) {
    const one = String(inbound ?? '');
    const rounds = state.rounds ?? 0;
    let trust = state.trust ?? 0.6;
    const reportedSpeech = isReportedSpeech(one);
    const bump = (delta, why) => {
        trust = clamp(trust + delta, 0, 1);
        if (delta < 0) {
            process.stderr.write(`[trust] ${delta.toFixed(2)} ${why} → ${trust.toFixed(2)}\n`);
        }
    };
    const hurt = (why) => {
        state.hurtAt = rounds;
        state.hurtUntil = rounds + HURT_ROUNDS;
        state.hurtBy = why;
    };
    const movedTo = sceneMoveTarget(one);
    if (movedTo && INVITE_PURPOSE.test(one)) {
        state.pendingInvite = { place: movedTo.id, at: rounds, purpose: true };
        state.inviteStake = 2;
    }
    else if (movedTo) {
        state.pendingInvite = { place: movedTo.id, at: rounds, purpose: false };
        state.inviteStake = 1;
        bump(0.05, '他邀我出去，成行了');
    }
    if (state.pendingInvite) {
        const waited = rounds - state.pendingInvite.at;
        if (INVITE_DELIVER.test(one)) {
            bump(0.05, '他说的事是真的');
            state.pendingInvite = null;
        }
        else if (INVITE_EMPTY.test(one)) {
            bump(-0.15, '他说的那件事是空的');
            hurt('虚报');
            state.pendingInvite = null;
        }
        else if (waited >= 6) {
            state.pendingInvite = null;
        }
    }
    const usedMemory = (state.autobio ?? []).some((item) => sharedBigrams(one, item.text) >= 3);
    if (usedMemory) {
        bump(0.03, '他记得我说过的事');
    }
    if (!reportedSpeech && /傻|废物|蠢|闭嘴|烦人|没用|碍事|滚/u.test(one)) {
        bump(-0.10, '他骂我');
        hurt('辱骂');
    }
    // r102：`DISMISS_PATTERN`（他把我算漏了 → trust −0.08）**整条删掉**。
    // 取证（docs/persona-r99-canon-memory.md §1.1，1154 行强证据）："被排到外面"0 条、"被当工具"0 条、
    // "因被排开而疼"0 条；2 条反证（L6249 她平静承认自己没参加、在入口把风；L3945–3947 她主动退到旁观位）；
    // 全库唯一命中这条正则的是 L6063「没你说话的份！」——日向对狛枝说，不是对她说的。
    // 也就是说这个触发器和它的后果**都没有原典来源**，属于编出来的关系规则，不留在她身上。
    // 拆心结的那一半在 r101（woundRank／appraisal），这一半是它的关系侧。
    if ((state.repeatCount ?? 0) >= 3) {
        state.missIgnore = (state.missIgnore ?? 0) + 1;
        if (state.missIgnore >= 3) {
            bump(-0.03, '他一直没接我的话');
            state.missIgnore = 0;
        }
    }
    else {
        state.missIgnore = 0;
    }
    state.trust = Number(trust.toFixed(3));
    return state;
}

// ── r75（v7）：意愿判定 ─────────────────────────────────────────────────
// 空间要不要换，是**她愿意不愿意**的事：夜里默认不去；白天看信任；受伤就哪儿都不去。
const OWN_PLACES = ['library', 'night-library', 'bedroom', 'night-bedroom'];
function isLateNight(now) {
    const hour = now.getHours();
    return hour >= 22 || hour < 6;
}

function willingToMove(state, target, now, stake = 0) {
    const rounds = state.rounds ?? 0;
    if ((state.hurtUntil ?? -1) >= rounds) {
        return { go: false, why: '不去——刚才那句话让我不想动' };
    }
    if (!target) {
        return { go: false, why: '不去——没什么地方要去' };
    }
    const trust = state.trust ?? 0.6;
    if (isLateNight(now)) {
        const big = stake >= 2 && trust >= 0.85;
        return big
            ? { go: true, why: '去——太晚了，但他说的那件事不像假的' }
            : { go: false, why: '不去——都这个点了' };
    }
    if (trust >= 0.4) {
        return { go: true, why: '去——他说的地方我去看看' };
    }
    const own = OWN_PLACES.includes(target.id);
    return own
        ? { go: true, why: '去——那是我自己也想待的地方' }
        : { go: false, why: '不去——他说的地方我不太想去' };
}

// ── r76（v8）：她自己动——不用时间表，用**加权取样** ─────────────────────
// 主人：「把日常活动写成表只会使这一切模板化、机械化……早上她可能在房间，也可能在餐厅，
// 这取决于她的心情。」所以：base 只给合理性，决定项是**此刻状态**，最后按权重随机抽。
const STAY_MS = 40 * 60 * 1000;
const STAY_ROUNDS = 30;

const BASE_DAY = {
    restaurant: 0.6,
    bedroom: 0.5,
    library: 0.5,
    lounge: 0.4,
    beach: 0.3,
    'computer-room': 0.2,
    'classroom-hall': 0.2,
    classroom: 0.15,
    'dorm-hall': 0.15,
    'hope-peak-gate': 0.1,
};
const BASE_NIGHT = {
    'night-bedroom': 0.8,
    'night-library': 0.4,
    lounge: 0.3,
    'night-street': 0.2,
    beach: 0.15,
    restaurant: 0.1,
    bar: 0.1,
};

const isRoom = (id) => id === 'bedroom' || id === 'night-bedroom';
const isQuiet = (id) => isRoom(id) || id === 'library' || id === 'night-library';
const isOutside = (id) => !isRoom(id) && !isQuiet(id);
const isHungry = (text) => /饿|吃的|饭|外卖|食堂|零食/u.test(String(text ?? ''));

function sceneWeights(state, now) {
    const base = isNight(now) ? BASE_NIGHT : BASE_DAY;
    const energy = state.energy ?? 1;
    const moodV = state.moodV ?? 0;
    const rounds = state.rounds ?? 0;
    const hurt = (state.hurtUntil ?? -1) >= rounds;
    const bored = rounds - (state.scene?.since ?? 0) >= STAY_ROUNDS;
    const hungry = isHungry(state.lastInboundText);
    const out = {};
    for (const id of Object.keys(base)) {
        let w = base[id];
        if (energy < 0.4) {
            w *= isRoom(id) ? 4 : 0.4;
        }
        if (hurt) {
            w *= isRoom(id) ? 8 : 0.05;
        }
        if (moodV < -0.3) {
            w *= isRoom(id) ? 2 : (isQuiet(id) ? 1.5 : 1);
        }
        if (hungry) {
            w *= id === 'restaurant' ? 5 : 1;
        }
        if (bored) {
            w *= isOutside(id) ? 3 : (isRoom(id) ? 0.5 : 1);
        }
        if (state.agenda === 'want_company') {
            w *= id === 'lounge' ? 3 : 1;
        }
        if ((state.trust ?? 0.6) >= 0.8) {
            w *= (id === 'lounge' || id === 'restaurant') ? 1.5 : 1;
        }
        out[id] = Math.max(w, 0.001);
    }
    return out;
}

// r87：确定性抽签，照 `layerUnit` 的先例（555 行那句"回放要可复现"）。
// 原来这里用 `Math.random()`：同一份 state 回放两次会到两间房，
// 于是 `selftest-cost` 的 I3（同一张卡逐字一致）会偶发失败——实测改动前后 5 次里各挂 2 次。
// 不是测试写坏了，是"她在哪儿"这件事本来就该由她的状态推出来，不该由掷骰子决定。
// 种子取这一轮的可见事实：轮次、精力、心情、当前房间。改一位就换一个结果，但同一份状态永远同一间房。
function sceneUnit(state) {
    const seed = [
        state.rounds ?? 0,
        round2(state.energy ?? 0),
        round2(state.moodV ?? 0),
        state.scene?.id ?? '',
    ].join('|');
    let hash = 2166136261;
    for (let i = 0; i < seed.length; i += 1) {
        hash ^= seed.charCodeAt(i);
        hash = Math.imul(hash, 16777619);
    }
    return ((hash >>> 0) % 100000) / 100000;
}

function sampleScene(state, now) {
    const list = loadScenes();
    const weights = sceneWeights(state, now);
    const items = list.filter((one) => weights[one.id] !== undefined);
    if (!items.length) {
        return list[0] ?? null;
    }
    const total = items.reduce((sum, one) => sum + weights[one.id], 0);
    let roll = sceneUnit(state) * total;
    for (const one of items) {
        roll -= weights[one.id];
        if (roll <= 0) {
            return one;
        }
    }
    return items[items.length - 1];
}

function sceneWhy(state, now, scene) {
    const energy = state.energy ?? 1;
    const moodV = state.moodV ?? 0;
    const rounds = state.rounds ?? 0;
    const bored = rounds - (state.scene?.since ?? 0) >= STAY_ROUNDS;
    if ((state.hurtUntil ?? -1) >= rounds) {
        return '不想动，就在这儿待着';
    }
    if (energy < 0.3) {
        return '困得不行了，先回房间躺一会儿';
    }
    if (isHungry(state.lastInboundText)) {
        return isRoom(scene?.id) ? '本来想吃点东西……又不太想动' : '到饭点了，过来看看有什么';
    }
    if (moodV < -0.3) {
        return isRoom(scene?.id) ? '有点闷，不想见人' : '待着闷，出来转转';
    }
    if (bored) {
        return isRoom(scene?.id) ? '坐久了……算了，还是在这儿' : '坐久了，出来换个地方';
    }
    if (isNight(now)) {
        return isRoom(scene?.id) ? '太晚了，不想动' : '睡不着，出来走走';
    }
    return isRoom(scene?.id) ? '还没想好今天干什么' : '随便走走，走到这儿了';
}

function ownMove(state, now) {
    const list = loadScenes();
    if (!list.length || !state.scene?.id) {
        return null;
    }
    const cur = state.scene;
    const rounds = state.rounds ?? 0;
    // r80b：**先待够，再考虑动**。以前每轮都抽一次签，抽到别的地方就"自己动了"，
    // 结果她在卧室／图书室／吧台之间来回跳（同一段里换三次，盲评判"位置自相推翻"）。
    // 现在：至少待 OWN_MOVE_MIN_ROUNDS 轮才重新抽；而且新地方要明显更合（权重 ≥1.5 倍）才走。
    const dwell = rounds - (cur.since ?? rounds);
    if (dwell >= OWN_MOVE_MIN_ROUNDS) {
        const want = sampleScene(state, now);
        if (want && want.id !== cur.id && clearlyBetter(state, cur, want, now)) {
            return { scene: want, why: sceneWhy(state, now, want), by: 'state' };
        }
    }
    if ((state.energy ?? 1) < 0.35 && !isRoom(cur.id)) {
        const home = list.find((one) => one.id === (isNight(now) ? 'night-bedroom' : 'bedroom'));
        if (home) {
            return { scene: home, why: '困了，回去躺一会儿', by: 'need' };
        }
    }
    const stayMs = cur.atMs ? now.getTime() - cur.atMs : 0;
    const stayRounds = rounds - (cur.since ?? rounds);
    if (stayMs >= STAY_MS || stayRounds >= STAY_ROUNDS) {
        const other = list.find((one) => one.id !== cur.id && isOutside(one.id)) ?? null;
        if (other) {
            return { scene: other, why: '在这儿待得有点久了，换个地方', by: 'stay' };
        }
    }
    return null;
}

// 新地方是不是"明显更合"：权重至少是现在的 OWN_MOVE_RATIO 倍（避免 50/50 抽签式挪窝）
const OWN_MOVE_MIN_ROUNDS = 6;
const OWN_MOVE_RATIO = 1.5;
function clearlyBetter(state, cur, want, now) {
    const weights = sceneWeights(state, now);
    const a = weights[cur.id] ?? 0;
    const b = weights[want.id] ?? 0;
    if (b <= 0) {
        return false;
    }
    return a <= 0 ? true : b / a >= OWN_MOVE_RATIO;
}

// 状态签名：只有它变了才重新抽场景（人不会每句话换地方）
// r80（r80b）：**签名里去掉精力／心情／信任／想干什么**——它们每轮都在小幅变化，
// 于是"状态变了"几乎永远成立，每轮都重新抽一次地方，她就在卧室、图书室、吧台之间来回跳
// （盲评第 45、58 轮点的就是"位置自相推翻"）。会让她换地方的只有三件事：
// 他到别处去了（sceneMoveTarget）／她被伤到（退回安全基地）／真待久了（STAY_ROUNDS、STAY_MS）。
function sceneSignature(state, now) {
    const rounds = state.rounds ?? 0;
    return [
        Math.floor(rounds / STAY_ROUNDS),
        (state.hurtUntil ?? -1) >= rounds ? 'h' : '-',
        isNight(now) ? 'n' : 'd',
    ].join('|');
}

function decideScene(state, inbound, now = new Date()) {
    const rounds = state.rounds ?? 0;
    const nowMs = now.getTime();
    const list = loadScenes();
    const at = (id) => list.find((one) => one.id === id) ?? list[0] ?? null;
    const curId = state.scene?.id ?? '';
    const movedTo = sceneMoveTarget(inbound);
    const refreshedWhy = (scene) => {
        const old = String(state.scene?.why ?? '');
        // 精力恢复后，旧卡片里的“困了，回去躺一会儿”不能继续冒充这一刻的理由。
        if ((state.energy ?? 1) >= ENERGY_FADE && /困|躺一会|睡/u.test(old)) {
            return sceneWhy(state, now, scene ?? state.scene);
        }
        return old || sceneWhy(state, now, scene ?? state.scene);
    };
    const pack = (scene, why, by) => ({
        ...scene,
        why,
        by,
        since: rounds,
        atMs: nowMs,
        image: scene.image ?? '',
    });
    // ① 她受伤：退回安全基地（不是惩罚他，是自我保护），锁 8 轮——连自己动都不做
    if ((state.hurtUntil ?? -1) >= rounds) {
        const home = at(isNight(now) ? 'night-bedroom' : 'bedroom');
        if (home && curId !== home.id) {
            process.stderr.write(`[scene] hurt ${state.scene?.name ?? '(未定)'} → ${home.name}（受伤退回）\n`);
            return pack(home, state.hurtBy ? `刚才那件事让我不想动` : '不想动', 'hurt');
        }
        return state.scene ?? (home ? pack(home, '不想动', 'hurt') : null);
    }
    // ③ 他的邀约：要过 willingToMove 这一关（愿意跟他去，本身就是她自己的决定）
    if (movedTo && movedTo.id !== curId) {
        const verdict = willingToMove(state, movedTo, now, state.inviteStake ?? 0);
        if (!verdict.go) {
            process.stderr.write(`[scene] stay ${state.scene?.name ?? '(未定)'}（不去${movedTo.name}：${verdict.why}）\n`);
            if (state.scene) {
                return { ...state.scene, atMs: state.scene.atMs ?? nowMs };
            }
        }
        else {
            process.stderr.write(`[scene] invited ${state.scene?.name ?? '(未定)'} → ${movedTo.name}（他邀）\n`);
            return pack(movedTo, verdict.why.replace(/^去——/u, ''), 'invited');
        }
    }
    // ② 她自己动：他没邀、或者她没答应，她才自己走来走去
    const own = ownMove(state, now);
    if (own) {
        process.stderr.write(`[scene] own ${state.scene?.name ?? '(未定)'} → ${own.scene.name}（${own.why}）\n`);
        return pack(own.scene, own.why, 'own');
    }
    // ④ 状态签名变了 → 重新抽一个地方（她自己的动；不按轮次重算）
    const picked = sampleScene(state, now);
    if (!picked) {
        return state.scene ?? null;
    }
    if (!curId || state.sceneSig !== sceneSignature(state, now)) {
        if (picked && (!curId || picked.id !== curId)) {
            process.stderr.write(`[scene] own ${state.scene?.name ?? '(未定)'} → ${picked.name}（状态变了）\n`);
            return pack(picked, sceneWhy(state, now, picked), 'state');
        }
        return {
            ...(state.scene ?? {}),
            why: refreshedWhy(picked),
            atMs: state.scene?.atMs ?? now.getTime(),
            image: state.scene?.image ?? picked?.image ?? '',
        };
    }
    // 原地不动：保留 why / atMs / image
    return {
        ...state.scene,
        why: refreshedWhy(picked ?? state.scene),
        atMs: state.scene.atMs ?? nowMs,
        image: state.scene.image ?? picked?.image ?? '',
    };
}


// ── v12（r80）：`phrasingHint` 整句样本下线 ─────────────────────────────
// r78 的 v10.1 从 state/her-phrasings.json 抽原典整句挂进卡片（「这种时候她会这么说（形状）：…」）。
// 主人看出来的毛病是「太工整」——那两句本身就是成品句，模型的任务从"她想怎么说"变成"照着这个格子写"。
// 这正是 docs/design-mind-layer.md §7.4 那条：**别把闸门的"落法"写进卡片**。
// 数据与生成器留着（`tools/persona-lab/canon-phrasings.mjs` → `state/her-phrasings.json`），
// 它现在只做事后核对：她量出来的说法和原典像不像，由 docs/persona-metrics.md 那边看，不进她的心。


// 卡片：我在哪儿（事实）
function sceneLine(state) {
    const scene = state.scene;
    if (!scene || !scene.id) {
        return '';
    }
    // r107：**只给地点名，不再列家具**。
    // 原因（当天量出来的）：卡片原来写「旅馆休闲室（沙发、矮桌、角落那台旧电视，白纱窗帘…盆栽…）」，
    // 她就会把道具念出来——真实对话里出现「这沙发够长」「几盏灯亮着，书架之间挺暗」这种
    // "念环境"的 AI 腔。地点名足够定住她在哪；家具由他自己问到时再说。
    // 回滚：MIND_SCENE_PROPS=1（按原来的「名字（道具…）」渲染）。
    if (process.env.MIND_SCENE_PROPS === '1') {
        return `${scene.name}（${scene.what}）`;
    }
    return scene.name;
}

// 卡片：这一轮我想做什么——**从需要＋情绪＋防御推出来**，不许从 doing 里抽一句照抄。
// 只用"意图"层面的短句，不编过去的理由（"刚才走了不少路"这种是捏造）。
function sceneIntent(state) {
    const defense = defenseOf(state);
    if (defense) {
        const table = {
            退缩: '在这儿待一会儿，不说话也行',
            幽默: '随便看看他会不会自己笑出来',
            攻击性升华: '先把话说清楚再看他要不要继续',
            接受: '问问这儿有没有能让我弄懂的东西',
            '理智化／转移': '找一件能说清楚的事讲给他听',
        };
        if (table[defense.key]) {
            return table[defense.key];
        }
    }
    const appraise = appraisalOf(state);
    if (appraise) {
        const table = {
            胜任被邀请: '看看这儿有没有能一起上手的东西',
            被夸了: '往后靠一点，别站到中间',
            被误判: '站定，不往前走',
            关联受挫: '往他那边靠近一点',
            胜任不足: '找找这儿有没有能弄明白的线索',
            关联被满足: '接着刚才的话说下去',
            他在试探或犹豫: '看他什么时候愿意说',
            他在讲他自己的事: '听他说，顺手插一句我这边的',
            他在说别人的事: '不往里凑，就在旁边听着',
            他也没劲了: '就这么待着，能少动一下就少动一下',
        };
        if (table[appraise.key]) {
            return table[appraise.key];
        }
    }
    // v12：没有明确的情绪来源时，也不是永远同一句——她想干什么本来就随状态变
    if ((state.energy ?? 1) < 0.4) {
        return '歇着，能少说一句就少说一句';
    }
    if (state.agenda === 'want_hide') {
        return '待在这儿，别把事情弄大';
    }
    if (state.agenda === 'want_win') {
        return '把话说稳，不让人看轻';
    }
    if (state.agenda === 'want_understand') {
        return '弄清楚他到底卡在哪儿';
    }
    return '就这么待着，听他说';
}


// 主人判断：这不是归属规则的问题，是**虚构症（confabulation）**——
// 记忆缺失的病人不会说"我不知道"，而是当场编一个听起来合理的答案。
// 「你爸爸是程序员吗」→「你也写过益智游戏」不是主语规则坏了，是"爸爸"那条记忆根本不存在。
// 四个存储都是现成模型：语义记忆（Tulving）／情节记忆＋情景缓冲器（Baddeley）／
// 模糊痕迹理论（Brainerd & Reyna）／源监控（Johnson）。

// ① SEMANTIC：语义记忆——**她要说的那句话本身**（自传体记忆的提取是第一人称的）。
// r69：r68 里给的是第三人称事实（"我爸爸只写过益智游戏那种程序（e08_009_033）"），
// 模型把它当素材重组，结果主语被揉碎（"不是程序员。你写过一点益智游戏那种程序"）。
// 现在每条都是**一句她可以直接说出口的话**：她只需要引用，不需要创作。
// 注意：整条里除了她自己没有第二个人（不许塞"你"），也不写"如果被问到"这种模板。
const SEMANTIC = [
    {
        key: '爸爸',
        re: /爸爸|父亲|程序员|写程序/u,
        fact: '我爸爸写过益智游戏。他是不是程序员……写过，就一点点。',
    },
    {
        key: '女儿节',
        re: /女儿节/u,
        fact: '女儿节我没怎么过。那天我在打游戏。',
    },
    {
        key: '挤奶',
        re: /挤奶|牛奶|牧场|种地|农活/u,
        fact: '挤奶是什么、牛奶怎么来的，我不懂。',
    },
    {
        key: '生物',
        re: /动物|生物|摸(它|他|牛|羊)|养(牛|羊|鸡)/u,
        fact: '我不太会和生物相处。摸起来会暖暖的，有点可怕。',
    },
    {
        key: '强项',
        re: /射击|格斗|解谜|音游|节奏|游戏打|喜欢什么游戏/u,
        fact: '除了恋爱游戏，别的类型我都能打。超高校级是学校发的。',
    },
    {
        key: '怕生',
        re: /交朋友|生人|人多|见人/u,
        fact: '人多的时候我在旁边看着就好。',
    },
];
export function semanticOf(text) {
    const one = String(text ?? '');
    return SEMANTIC.filter((item) => item.re.test(one));
}

// ③ GIST：模糊痕迹理论——只记**意思**。同一话题同一意图算同一个 gist 计数。
// 「要和七海一起行动吗？」「要一起吗？」「一起打一局？」→ 同一个话题，第 4 次就是 4（不按字面算）。
const TOPIC_RULES = [
    { key: '一起行动', re: /一起|行动|组队|双人|联机|跟我走/u },
    { key: '进度', re: /打到|卡在|第[^，。！？]{0,2}关|进度|通到|过没过/u },
    { key: '爸爸', re: /爸爸|父亲|程序员/u },
    { key: '游戏喜好', re: /喜欢什么|爱玩什么|什么游戏/u },
    { key: '睡觉', re: /睡着|睡那么快|秒睡|困/u },
];
export function gistOf(text) {
    const one = String(text ?? '');
    return TOPIC_RULES.find((item) => item.re.test(one))?.key ?? '';
}

// ② EPISODIC：**已删除**（v4）。她活在机器里，没有"我在打什么、卡在第几关"这种生活；
// 给她塞一条虚构的进度，那条进度就会变成她唯一的内容来源——换皮重答的根源。
// 她的内容只来自四样：他刚说的那句话／她记得的真实的事／她自己的判断／她真的不懂的地方。
// ── r66（v2.1）：视角绑定 + 自传记忆 ──────────────────────────────────────
// 主人判断：剩下三个问题其实是同一件事——**她没有记忆，也没有视角绑定**。
//   「他说我秒睡 → 我答这个本事我倒是没有」＝没做 theory of mind（没判断这句在说谁）
//   「第二关／最后一关／刚过完一关」＝没有自传记忆（人不会每轮重新决定自己卡在哪）
//   「卡了三天」被复用＝内容点每次现编，话没有出处
// 这两样都只是**事实**，不是指令：她理解了，自然就答对主体、接得上自己。
const OTHER_NAMES = /八云|狛枝|二大|贰大|索妮亚|罪木|小泉|西园寺|九头龙|终里|澪田|冬彦|茜|黑白熊|莫诺美|左右田|边古山|田中|花村|九十九|雾切|苗木/u;
// 省略主语、但谓语只能是在说她（他的身体/属性）
// r72：省略主语、但谓语只能是在说她（他是问她的）。
// 补充：睡着/秒睡/困/卡住/打过/喜欢——r71 L3 主体反转就是漏了"躺下的时候已经很累了"这一类。
const ABOUT_HER_PREDICATE = /睡着|秒睡|困|躺下|累|打呼|卡住|卡在|打过|玩过|喜欢什么|喜欢哪|擅长|厉害|可爱|傻|笨|捡|练|通关|发呆|犯困|跑/u;
// ① perspectiveOf：判他这句在说谁（说我／说他自己／说别人）
export function perspectiveOf(inbound) {
    const text = String(inbound ?? '').trim();
    if (!text) {
        return { target: '没指向', why: '' };
    }
    const saysMe = /你|七海/u.test(text);
    const saysHim = /^我/u.test(text) || /[，。！？；;]\s*我/u.test(text) || /我(?:打|卡|玩|想|要|不|也|还是)/u.test(text);
    const otherName = OTHER_NAMES.test(text);
    if (otherName && !saysMe) {
        return { target: '在说别人', why: `句子里出现了别人（${text.match(OTHER_NAMES)?.[0]}）` };
    }
    if (saysMe) {
        // v12：他自己在旁白里说她（「七海说完，突然开始跑了起来」）→ 也是"在说我"，
        // 但理由不能写成"主语是「你」"——r79 那句谎让模型把她的动作挂到了日向同学身上。
        const named = text.match(/七海|她/u)?.[0] ?? '';
        return { target: '在说我', why: named ? '他这话说的是我做的事，不是他自己的' : '主语是「你」' };
    }
    // 显式主语「我」优先于"谓语在说她"的启发式——他说的进度不许当成她的（v3 源监控）
    if (saysHim) {
        return { target: '在说他自己', why: '主语是「我」' };
    }
    if (ABOUT_HER_PREDICATE.test(text)) {
        return { target: '在说我', why: `省略了主语，但谓语在说她的事（${text.match(ABOUT_HER_PREDICATE)?.[0]}）` };
    }
    return { target: '没指向', why: '随口一句，没在说谁' };
}

// ── r82（P4 模型侧）：`footingOf(signals)`——"这句话是冲谁说的" ──────────────────
// `perspectiveOf` 判的是**内容**（在说谁：我／他／别人），这一层判的是**位置**（在跟谁说）。
// 两者都是社会知觉，所以放在一起；但它们是两个量，别混。
// 依据：Goffman 1981 footing；Clark & Carlson 1982（addressee／overhearer／bystander 是三种角色）。
//
// **纪律**：`who` 由网关算好（`group-appraisal.js` 那四格），模型这一层**只透传、不复算**——
// 复算就成了两套判定，迟早漂。所以这里只认四个字段，OneBot 的任何概念（@、消息段、群号）
// 一律不许进 mind.mjs；没有 facts 时一律按一对一处理。
//
// `MIND_FOOTING=0` 时整层不参与（`state.footing` 不写、【我不说话是因为】那一支也不看它），
// 输出与上一版逐字一致。
const FOOTING_LAYER_OFF = process.env.MIND_FOOTING === '0';
const FOOTING_WHO = ['me', 'other', 'room', 'unclear'];

export function footingOf(signals) {
    const one = (signals && typeof signals === 'object') ? signals : null;
    // 私聊（或没有 facts）：只有他一个人，永远是冲她说的
    if (!one || one.scope !== 'group') {
        return { who: 'me', why: '一对一里只有他一个人' };
    }
    const who = String(one.who ?? '');
    if (FOOTING_WHO.includes(who)) {
        // 网关已经判好了 → 原样透传（模型不许重算，否则就是两套判定）
        const why = who === 'me' ? '这句是冲我说的'
            : who === 'other' ? '这句是冲别人说的'
                : who === 'room' ? '这句是冲整个屋子说的' : '看不出在问谁';
        return { who, why };
    }
    const name = String(one.name ?? '').trim();
    if (name && name !== '主人') {
        return { who: 'other', why: `在跟「${name}」说话` };
    }
    return { who: 'unclear', why: '没叫人、也没看出来在跟谁说' };
}

// ② AUTOBIO：她的自传记忆（≤5 条，只记"我"的事实）
const AUTOBIO_MAX = 5;
const AUTOBIO_TTL = 20;   // 20 轮没被引用就自然丢掉
// v4：只记**真实发生过的事**——她不懂的、约定、他自己说过的事实。
// 不记"我在打什么／我去了哪"（那是编造的生活：她活在机器里，她没有那些生活）。
// v5④：另外要记**她自己的能力/经历主张**（"一千片我玩得还可以"）——
// 否则下一轮"不懂"会盖过她刚说过的话（模板盖记录）。
const AUTOBIO_CLAIM = /我(?:不|没)(?:懂|知道|弄过|去过|试过|见过)|说好了|答应过|你(?:说过|提到过|卡在|喜欢|不喜欢|抱怨过)|我[^，。！？]{0,10}(?:玩得|打得|弄过|会一点|挺擅长|还行|玩过)/u;
// v12（r80）：**用过几次**要记下来（交接 §6 的根因）。
// r79 里面板每轮把全部记忆列一遍，其中"我不太想动，但还是会去"这类素材被用了七次——
// 换皮复读不是模型懒，是**她的记录里没有"这件事我已经说过了"这一栏**。
// 现在：说过一次 → 卡片上标出来；说过两次 → 不再挂出来（她自己也不会老提同一件事）。
// r82（psych §11.2）：两处共用一个常数，但**方向相反，不许合成一个函数**——
// autobio 用它"过滤"（素材用过几次就不挂），stories 用它"触发"（轶事讲过两遍就判没料可加）。
// 谁把它俩合并，下次改阈值就会同时改两件事。
const REPEAT_ENOUGH = 2;
const AUTOBIO_USED_MAX = REPEAT_ENOUGH;
// 二元组 Dice 系数（0–1）：`sameStory` 的判定和 R1 激活的"字面线索分"共用这一处，别各写一份
export function diceOf(a, b) {
    const clean = (text) => String(text ?? '').replace(/[^\u4e00-\u9fa5A-Za-z0-9]/gu, '');
    const x = clean(a);
    const y = clean(b);
    if (x.length < 2 || y.length < 2) {
        return 0;
    }
    const grams = (text) => {
        const out = new Set();
        for (let i = 0; i + 2 <= text.length; i += 1) {
            out.add(text.slice(i, i + 2));
        }
        return out;
    };
    const gx = grams(x);
    const gy = grams(y);
    let hit = 0;
    for (const one of gx) {
        if (gy.has(one)) {
            hit += 1;
        }
    }
    return gx.size + gy.size > 0 ? (2 * hit) / (gx.size + gy.size) : 0;
}

// 两句话是不是在说同一件事：二元组重合 ≥0.5 就算（去掉标点后 ≥6 字才判）
// r82（R2）：导出让判据工具不必再抄一份（`tools/persona-lab/stories-score.mjs` 的漂移自检要有它）。
export function sameStory(a, b) {
    const clean = (text) => String(text ?? '').replace(/[^\u4e00-\u9fa5A-Za-z0-9]/gu, '');
    const x = clean(a);
    const y = clean(b);
    if (x.length < 6 || y.length < 6) {
        return false;
    }
    return diceOf(a, b) >= 0.5;
}

// 她真的把那件事说出来了吗？——说过就记一次（--her 回写时调）
export function markAutobioUsed(state, said) {
    const list = state.autobio ?? [];
    if (!list.length) {
        return state;
    }
    state.autobio = list.map((one) => (sameStory(one.text, said)
        ? { ...one, used: (one.used ?? 0) + 1, lastUsed: state.rounds ?? 0 }
        : one));
    return state;
}

export function rememberHer(state, said) {
    const text = String(said ?? '').trim();
    if (!text || !AUTOBIO_CLAIM.test(text)) {
        return state;
    }
    const round = state.rounds ?? 0;
    // 记忆要保留"卡在哪一关"这种关键信息：取命中那一句 + 它的下一句（合起来 ≤30 字）
    const parts = text.split(/[，。！？]/u).map((one) => one.trim()).filter(Boolean);
    const at = parts.findIndex((one) => AUTOBIO_CLAIM.test(one));
    const clause = at >= 0 ? parts.slice(at, at + 2).join('，') : text;
    const item = clause.slice(0, 30);
    const previous = (state.autobio ?? []).find((one) => one.text === item);
    const list = (state.autobio ?? []).filter((one) => round - one.round <= AUTOBIO_TTL && one.text !== item);
    // 同一句话又说了一遍：保留原来"说过几次"的次数，不要清零（否则计数永远回到 0）
    list.push({ text: item, round, src: '我说的', used: previous ? (previous.used ?? 0) : 0 });
    state.autobio = list.slice(-AUTOBIO_MAX);
    return state;
}
export function autobioOf(state) {
    const now = state.rounds ?? 0;
    return (state.autobio ?? []).filter((one) => now - one.round <= AUTOBIO_TTL
        && (one.used ?? 0) < AUTOBIO_USED_MAX);
}

// ── r82（R2 / psych 规格 §6 的 E + §11 的 14 条对照）：这一段"她讲过什么"的工作记忆 ──
// 病根（交接 §9.2，盲评点名第一名问题）：同一段里把同一件轶事换个名词讲两遍（拼图／一千片）。
// `autobio.used` 只管"她记得的素材"，管不到**模型现编的轶事**——那一类根本没有记录。
// 这里补的不是规则，是**一栏她自己的产出记录**（Levelt 的产出监控；Johnson 的源监控）。
// 边界（写死，防两个机制互相污染）：autobio 是"我记得的事"（素材库，TTL 20 轮），
// stories 是"我这一会儿讲过什么"（工作记忆）；两张表互不读。
// 三个参数（数值与理由见 docs/r82-psych-spec.md §11.1）：
//   · STORY_MAX = 3   —— **3 是容量，不是窗口**（Cowan 3–5 区间；她 4–6 轮里讲的轶事不超过 3 件）
//   · STORY_WINDOW = 4 —— 抑制的主力是 used≥2 的行为出口，窗口只决定"卡片那行还挂不挂"；
//     窗口宽 = 更多"讲过"行 = 话更少，而 r80 之后沉默已经偏多
//   · REPEAT_ENOUGH = 2 —— 与 AUTOBIO_USED_MAX 共用一个常数，但**方向相反**（一个在过滤、一个在触发）
const STORY_MAX = 3;
const STORY_WINDOW = 4;
// 与 sameStory 的 6 字下限对齐：更短的话连"同一件事"都判不出来
const STORY_MIN_CHARS = 6;
// "在说同一个话题"要共享几个内容二元组（标定依据见 sharesTopic 上方：k=1 太松、k=2 是分界点）
const STORY_SHARE_MIN = 2;
// 只登记"她讲了一件过去的事"：命中回忆标记，或者已经命中 autobio 的录入条件。
// ②这一条必须含 AUTOBIO_CLAIM：S1 那句「我玩过那种拼到一半少一块的」没有"小时候／有一次"这类标记。
const STORY_REMEMBER = /小时候|以前|那会儿|有一次|上回|那天|去年|曾经|我记得|后来/u;
// ③ 2026-10-26 校准（Lead 的靶：`tmp/rep/samples-on.json` 17 轮里一条都没登记上）。
// 病根不是 `--her` 没接，是**门开错了方向**：原来只认"回忆标记／AUTOBIO_CLAIM"，
// 而盲评点名的病灶是"**模型现编的轶事**"——现编的轶事本来就不会带"小时候／有一次"
// （原文：一千片那个……我还没拆封。／沙滩上捡的。／数独那种，规则一句话就懂。）
// 所以补第二条通路：**这句话里有没有"能指出来的一件东西"**（判"有形状"，不判"有回忆标记"）。
// 具体所指只用现成的表，不新造词表：SEMANTIC 六格／gist 话题／GAME_WORDS／STRONG_SUIT／
// scenes.json 的场景名／数字与量词／《作品名》／时间词。
const STORY_CONCRETE_MIN = 8;
const STORY_AMOUNT = /[一二三四五六七八九十百千两\d]+\s*(?:片|块|个|只|盒|副|张|遍|次|年|天|分钟|小时|点|把|台|部|首|本|卷|颗|枚|局)/u;
const STORY_TIME = /昨天|昨晚|前天|上次|上回|刚才|今天早上|白天|半夜|凌晨|那年|那年冬天/u;
const STORY_TITLE = /《[^》]{1,14}》|「[^」]{2,14}」/u;
// 主人 r70 定的红线（v4：不塞虚拟生活）：她"**现在**在打什么、卡在第几关"是编造的生活，不许进卡片。
// stories 这一层不能破这条规矩——`tmp/gate/selftest-mind-actions.mjs` 的 ⑧ 就在守它
// （「我在打洞窟物语，卡在第二关后半段」+ 他问「你打到哪了」→ 卡片里不许出现「第二关」）。
// 判据放在**登记**这一步（不是卡片那一步）：这一句根本不进工作记忆，两个出口自然都干净。
const STORY_EPISODIC = /第[^，。！？]{0,3}关|卡在|打到|通关|这关|过不去|打不过/u;

function hasConcreteThing(text) {
    const one = String(text ?? '');
    if (semanticOf(one).length > 0) {
        return true;
    }
    if (gistOf(one)) {
        return true;
    }
    if (GAME_WORDS.test(one) || STRONG_SUIT.test(one)) {
        return true;
    }
    if (STORY_AMOUNT.test(one) || STORY_TIME.test(one) || STORY_TITLE.test(one) || /\d/u.test(one)) {
        return true;
    }
    return loadScenes().some((scene) => scene?.name && one.includes(scene.name));
}
// A/B 开关（照 MIND_REASONING=0 的先例）：=0 时这一层完全不参与，输出必须与 r81 逐字一致
const STORY_LAYER_OFF = process.env.MIND_STORY === '0';

// 虚词：含这些字的二元组不算"话题词"（"我玩／一个／说过"这类不算）
const STORY_STOP_CHARS = new Set('我你他她它的了着过是在有和就都也这那个上下里会要得么什怎们到来去说好没很太'.split(''));
// r82（R1/R2 共用）：导出给 activation.mjs 的调用方与自测用——"内容二元组"只许有一处实现
export function contentGrams(text) {
    const clean = String(text ?? '').replace(/[^\u4e00-\u9fa5A-Za-z0-9]/gu, '');
    const out = [];
    for (let i = 0; i + 2 <= clean.length; i += 1) {
        const gram = clean.slice(i, i + 2);
        if (!STORY_STOP_CHARS.has(gram[0]) && !STORY_STOP_CHARS.has(gram[1]) && !out.includes(gram)) {
            out.push(gram);
        }
    }
    return out;
}

// 两个话题词组之间共享几个（唯一一处做这件事的地方，sharesTopic 和 storyMatches 都用它）
function sharedGramCount(gramsA, gramsB) {
    let hit = 0;
    for (const one of gramsA) {
        if (gramsB.includes(one)) {
            hit += 1;
        }
    }
    return hit;
}

// 两句话是不是"在说同一个话题"：先看同一件事（sameStory，二元组 Dice ≥0.5），
// 再看**共享几个话题词**（内容二元组）。
// 阈值 k：2026-10-26 用 tools/persona-lab/stories-score.mjs 在 86 轮基线上量过——
//   k=1 → 34 对（50.0% 轮次），太松：把「知道。牛奶是牛身上的呢」vs「不用知道呢」这种
//         **只是在应答同一个话题**的句子也算了进去；
//   k=2 → 14 对（26.7%），这是分界点；k=3 → 8 对（太紧，换名词复述就漏了）。
// 所以默认 k=2。注意 sameStory ⊆ sharesTopic 这条包含关系仍然成立（Dice≥0.5 必然共享 ≥2 个二元组）。
export function sharesTopic(a, b, shareMin = STORY_SHARE_MIN) {
    if (sameStory(a, b)) {
        return true;
    }
    return sharedGramCount(contentGrams(a), contentGrams(b)) >= shareMin;
}

// 她还"在窗口里"的故事：工作记忆有自己的保质期，与 autobio 的 TTL 是两回事
export function storiesOf(state) {
    const now = state.rounds ?? 0;
    return (state.stories ?? [])
        .filter((one) => one && typeof one === 'object' && typeof one.text === 'string')
        .filter((one) => now - (one.lastAt ?? 0) <= STORY_WINDOW);
}

// ── r83（§17.8 的欠账）：**"降级是假的"要修在这里，不是改名** ────────────────────────
// 2026-10-27 给 17 条旧故事标了 `src='unknown'`，但**取用处从来没读过这个字段**——
// 也就是说那 17 条不可核验的"我说过的话"照样会被 `storyHit`／`relatedStoryOf`／续话素材读到。
// 现在：**消费侧只认可核验来源**（`storySrcOk`：`canon:<场次>` 或 `dialogue:<ms>`）。
// 两条硬约束（都是这一路的血）：
//   ① **写入路径仍用 `storiesOf`**（`rememberStory` 的 `live`）——降级不等于删记忆，
//      在登记那一侧过滤会让"老条目被悄悄抹掉"（违反 §14.2 的"不许抹掉已经发生的事"）。
//   ② 消费侧过滤只做**一件事**：读不到就当她没讲过，不提示、不写日志、不改文案。
export function trustedStoriesOf(state) {
    return storiesOf(state).filter((one) => storySrcOk(one?.src));
}

// 一句话 → 一条故事记录（Sachs 1967：对自己的话只有要旨记忆，"谁＋什么事"，不存原话）
const STORY_WHO = /^(我爸爸|我妈妈|我爸|我妈|我|你|他|她)/u;
// `cue` = 她说这句话时**他在说什么**（`state.lastInboundText`）。
// 依据是编码特异性（Tulving & Thomson 1973）：提取靠的是"编码当时那条线索"，
// 不是痕迹本身。所以她这句「一千片那个……我还没拆封」在下一轮他问「那副拼图你拼了多久」时
// 能被勾起来——两句话字面只共享一个「拼图」，但**编码时的线索里就有「拼图」这个词**。
// 不把 cue 并进 key 的话，登记上来的故事几乎永远匹配不到他后来的问法（实测 17 轮 0 命中）。
function storyRecord(text, round, cue) {
    const raw = String(text ?? '').trim();
    const full = raw.slice(0, 30);
    const who = full.match(STORY_WHO)?.[0] ?? '';
    const rest = who && full.startsWith(who) ? full.slice(who.length) : full;
    const scenes = loadScenes();
    const place = scenes.find((one) => one?.name && full.includes(one.name))?.name ?? '';
    const key = [...contentGrams(full), ...contentGrams(cue)].filter((one, index, list) => list.indexOf(one) === index);
    return {
        who,
        place,
        text: rest.slice(0, 20),
        key: key.slice(0, 32),
        // gist 要用**没截断的原话**算：draft 常在前 20 字之后才出现「一起」「进度」这类话题词，
        // 用截断后的 text 算会把它们切掉（实测「下次要不要一起拼一副新的」匹配不上，就是这一处）
        gist: gistOf(raw),
        cueGist: gistOf(cue),
        used: 1,
        firstAt: round,
        lastAt: round,
        // 2026-10-26 假记忆事故的规矩：**每条 story 必须带可核验的来源**。
        // 只认两种：`canon:<场次>`（原典）与 `dialogue:<毫秒时间戳>`（她真的说过的对话轮）。
        // `comment`／`generated`／缺失一律不许入表——「我不过是打得比别人久一点」就是
        // 只存在于注释里的自造句，被她当成"我说过的"记了十几轮。
        src: `dialogue:${Date.now()}`,
    };
}

/** 来源白名单：`canon:<场次>` 或 `dialogue:<时间戳>`，其它一律拒。 */
export function storySrcOk(src) {
    const one = String(src ?? '').trim();
    return /^canon:.{1,40}$/u.test(one) || /^dialogue:\d{6,}$/u.test(one);
}

// 他这句里"能当线索的部分"：剥掉**消息格式**——说话人前缀（卷子里的「日向：」）、引用块、[xxx] 标记。
// 不剥的话，同一份卷子里每条"他"的话都带着同一个名字，那个名字会变成万能线索：
// 实测（2026-10-26）canon 86 轮里 51 轮的命中就是靠「日向」这一个词，等于没门。
function cueOf(text) {
    return String(text ?? '')
        .replace(/\[[^\]]{0,24}\]/gu, ' ')
        .replace(/^(?:[^\s：:]{1,8}[：:])\s*/u, '')
        .replace(/\s+/gu, ' ')
        .trim()
        .slice(0, 60);
}

// 泛用二元组：检索用的"一根线索"必须是**具体的东西**，不能是"知道／现在／什么"这类空词。
// 这张表是标定出来的（2026-10-26：k=1 的线索检索在 canon 86 轮上触发 51 轮 / 59.3%，
// 逐条看下去，绝大多数命中的共同词是「游戏／知道／现在／同学」——那不是"她讲过这件事"，
// 只是两句话都用了同一个常用词）。表里的每一条都能在误命中里指出来源，不是凭感觉加的。
const STORY_GENERIC = new Set([
    '知道', '现在', '刚才', '什么', '时候', '名字', '一样', '真的', '可能', '觉得',
    '事情', '东西', '地方', '同学', '一个', '两个', '自己', '我们', '你们', '他们',
    '这样', '那样', '怎么', '不是', '没有', '可以', '应该', '记得', '说的', '听到',
    '什么', '有点', '好像', '已经', '还是', '就是', '但是', '如果', '因为', '所以',
]);

// 一条故事记录配不配他这一句话（**检索**用：他这句 → 她那条记忆）
// 与 storyMatches（登记用：她这句 → 她那条记忆）故意用两个 k，因为问的不是同一件事：
//   · 登记问"这是不是同一件事被她又讲了一遍" → 要看两份说法重合多少 → k≥2（neuro 在"她↔她"上标定）
//   · 检索问"他这句话是不是把那条记忆勾起来了" → **一根对得上的具体线索就够**
//     依据：编码特异性（Tulving & Thomson 1973）提取靠线索匹配；psych §1 也写明"匹配分用 max，
//     不用加权平均（平均会让'什么都沾一点'变成常驻底噪）"。「那副拼图你拼了多久」里的"拼图"
//     就是他指的这件事本身——按 k≥2 要求它再凑一个词，等于要求他把她那句话复述一遍才想得起来。
//     但"一根线索"必须是具体词（STORY_GENERIC 挡住空词），否则任何寒暄都会命中。
function cueMatches(one, inbound) {
    const full = `${one.who ?? ''}${one.text ?? ''}`;
    if (sameStory(full, inbound)) {
        return true;
    }
    const gist = gistOf(inbound);
    // 话题层只认**她那句话本身**的话题（cueGist 不进检索）：场上的话题词只是"当时在聊什么"，
    // 拿它当检索线索会让"他在问进度"就勾出任意一条进度回答（实测 51/86 轮都命中，等于没门）。
    if (gist && one.gist === gist) {
        return true;
    }
    const key = Array.isArray(one.key) ? one.key : contentGrams(full);
    const grams = contentGrams(inbound);
    let strong = 0;
    for (const item of key) {
        if (grams.includes(item) && !STORY_GENERIC.has(item)) {
            strong += 1;
        }
    }
    return strong >= 1;
}

// 一条故事记录配不配她新说的这句话（**登记**用：判"同一件事又讲了一遍"）
function storyMatches(one, inbound) {
    const full = `${one.who ?? ''}${one.text ?? ''}`;
    if (sameStory(full, inbound)) {
        return true;
    }
    const gist = gistOf(inbound);
    // 话题层：她说的那句、或她那句是在回应的话题，跟这句同一个话题 → 算同一件事
    if (gist && (one.gist === gist || one.cueGist === gist)) {
        return true;
    }
    // 话题词组（key = 她这句的内容二元组 ∪ 编码时他那句的内容二元组）：登记这一侧保 k≥2
    const key = Array.isArray(one.key) ? one.key : contentGrams(full);
    return sharedGramCount(key, contentGrams(inbound)) >= STORY_SHARE_MIN;
}

// `--her` 回写时登记：她这一句是不是又讲了一件过去的事
/**
 * r82（2026-10-26 事故）：登记进 `stories` 之前先洗一遍。
 * 剥掉：`<img …>` 之类的标签、`[CQ:…]`、URL、纯符号/空白。
 * 只要洗完之后"去标点不足 6 字"，就不该被当成"她讲过的一件具体的事"。
 */
export function cleanStoryText(said) {
    return String(said ?? '')
        // r82（2026-10-26 根因①）：非人话一律不进 stories——和 `<img>` 那次同一类。
        .replace(/\[CQ:[^\]]{0,400}\]/gu, ' ')                 // CQ 码（含 &#44; 转义形态）
        // ① 转发记录／旁听记录／引用／回复：整段剥（含被截断、没有收尾括号的）
        .replace(/\[(?:转发聊天记录|旁听记录|引用|回复)[^\]]{0,300}\]?/gu, ' ')
        // ② 方括号里只要含 `|qq=` 就整段剥，**不管后面还跟什么**（`[🐐|qq=…][Image:…]` 一起剥）
        .replace(/\[[^\]]{0,80}\|qq=[^\]]{0,80}\]/gu, ' ')
        // `[…][Image: …]`／`[…][图片]` 这类附件标记（剥完 qq 标记之后可能只剩它）
        .replace(/\[[^\]]{0,80}(?:Image|image|图片|表情|语音)[^\]]{0,300}\]/gu, ' ')
        .replace(/<[^>]{0,200}>/gu, ' ')
        .replace(/https?:\/\/\S+/gu, ' ')
        .replace(/&#\d{1,6};/gu, ' ')                           // HTML 实体（`&#44;` 这类）
        .replace(/^\s*\/[a-z][a-z0-9_]{0,24}\b/giu, ' ')         // `/gamemode 1` 这种命令
        .replace(/[^\p{L}\p{N}\u4e00-\u9fff]+/gu, ' ')
        .replace(/\s+/gu, ' ')
        .trim();
}

/**
 * r82（根因②）：这句话是不是**人话**——给"相关"判据加一道前置。
 * 剥完去标点不足 2 字 → 不是人话（`[CQ:…]`／`[转发…]`／纯表情／纯符号都落这一档）。
 */
export function isHumanText(text) {
    return cleanStoryText(text).replace(/\s+/gu, '').length >= 2;
}

export function rememberStory(state, said) {
    if (STORY_LAYER_OFF) {
        return state;
    }
    // r82（2026-10-26 事故）：**图片/表情包消息永远不进 stories**。
    // 病根：一条 `原来是卷成这样的…` 的 story，`key` 里塞满了 `Im/ma/ag/ge` 这种二元组——
    // 那是 `<img src="file://...">` 的片段，也就是她的表情包被当成了"具体所指的事"，
    // 于是续话（C）有了素材，把一小时前那句原话又发了一遍。
    // 清洗：先剥 `<img…>`／`[CQ:…]`／URL／纯符号，**清洗后去标点不足 6 字就不登记**，
    // `key` 必须用清洗后的文本算（`storyRecord` 里也是拿这个 text）。
    const cleaned = cleanStoryText(said);
    const text = cleaned;
    if (bareLen(text) < STORY_MIN_CHARS) {
        return state;
    }
    // ── 根因③（2026-10-26）：登记门槛要"完整句" ────────────────────────────────────
    // 病根：`用完我还回去` 这种半截句进了 stories，之后任何消息都能跟它凑上共同实词，
    // 于是它永不消费、连续五轮都能当"没说完的事"。
    // 判据：**有句末标点**，或者**长度 ≥8 且不是他那句的截断前缀**（能在原文里找到、且比原文短 = 截断）。
    const saidRaw = String(said ?? '').trim();
    const endsWell = /[。！？…]$/u.test(saidRaw) || /[!?]$/u.test(saidRaw);
    const inboundNow = String(state.lastInboundText ?? '');
    const isPrefix = inboundNow.includes(text) && inboundNow.length > text.length;
    if (!endsWell && (bareLen(text) < 8 || isPrefix)) {
        return state;
    }
    // 两条通路：①（旧）命中回忆标记／AUTOBIO_CLAIM；②（新）够长且有具体所指
    // 但 ② 要扣掉"正在打的进度"（v4 红线：那是编造的生活，不进工作记忆）
    const remembered = STORY_REMEMBER.test(text) || AUTOBIO_CLAIM.test(text);
    const concrete = bareLen(text) >= STORY_CONCRETE_MIN && hasConcreteThing(text) && !STORY_EPISODIC.test(text);
    if (!remembered && !concrete) {
        return state;
    }
    const round = state.rounds ?? 0;
    const cue = cueOf(state.lastInboundText);
    const live = storiesOf(state);
    const hit = live.find((one) => storyMatches(one, text));
    if (hit) {
        // 同一件事又讲了一遍：次数 +1、时间往前挪，**说法也换成最近这次**
        // （卡片上写的是"刚才讲过"，指的当然是最近那次；换名词复述时旧说法可能一个共同话题词都没有）
        const fresh = storyRecord(text, round, cue);
        hit.who = fresh.who;
        hit.text = fresh.text;
        hit.key = fresh.key;
        hit.gist = fresh.gist || hit.gist || '';
        hit.place = fresh.place || hit.place || '';
        hit.used = (hit.used ?? 0) + 1;
        hit.lastAt = round;
        state.stories = live;
        return state;
    }
    const next = storyRecord(text, round, cue);
    // 入表前的最后一道闸（2026-10-26）：**来源不可核验的不许进 stories**。
    // 现在 `storyRecord` 只会产出 `dialogue:<ms>`，所以这里拦的是"以后有人塞 `comment`/`generated`"。
    if (!storySrcOk(next?.src)) {
        return state;
    }
    next.firstAt = round;
    live.push(next);
    state.stories = live.slice(-STORY_MAX);
    return state;
}

// 卡片与 news 共用的判定：他这一句是不是又把那件事勾起来了（同轮最多 1 条，取 lastAt 最新的）
export function storyHit(state) {
    if (STORY_LAYER_OFF) {
        return null;
    }
    const inbound = cueOf(state.lastInboundText);
    if (bareLen(inbound) < 4) {
        return null;
    }
    // 扣掉"他自己在重复问"：同一件事被问第 3 遍起，她这边共享的词是在**应答**，不是复读
    // （neuro 在 daily-08/09 上量的：「要和七海一起行动吗」被问 8–10 遍，共享「一起」不算病灶）。
    // repeatCount 与 gist 都已在状态里，不新加量。
    if ((state.repeatCount ?? 0) >= 3) {
        return null;
    }
    const hits = trustedStoriesOf(state).filter((one) => (one.used ?? 0) >= 1 && cueMatches(one, inbound));
    if (!hits.length) {
        return null;
    }
    return hits.reduce((left, right) => ((right.lastAt ?? 0) >= (left.lastAt ?? 0) ? right : left));
}

// ── r82（R1 / psych §1 的 A）：线索激活的接线 ─────────────────────────────
// 分工（Lead 的硬约束 1）：**事实在这里算一次**（gist／semantic 只算一次），
// `activation.mjs` 只拿这份事实算激活分，`stories` 那条链一个字节都不碰。
// 激活值**不落盘**：只有卡片那一行字符串（`activationLine`，每轮覆盖）和习惯化记账进 state。
// A/B 开关，照 `MIND_REASONING=0` / `MIND_STORY=0` 的先例：=0 时这一层完全不参与
const ACTIVATION_LAYER_OFF = process.env.MIND_ACTIVATION === '0';

function placeKindOf(scene) {
    const id = String(scene?.id ?? '');
    if (!id) {
        return '';
    }
    if (isRoom(id)) {
        return 'room';
    }
    if (isQuiet(id)) {
        return 'quiet';
    }
    return 'outside';
}

// 候选记忆条：承诺账（还欠着的）＋ autobio（她记得的素材）。
// `stories` 不进这里——它判"我讲过"，激活判"这件事被点亮"，两条线不许互相调用（psych §11.4）。
function activationItemsOf(state, ledger, now) {
    const items = [];
    for (const one of (ledger?.items ?? [])) {
        if (one?.owed !== true) {
            continue;
        }
        items.push({
            id: `commit:${one.id}`,
            kind: 'ledger',
            what: String(one.what ?? ''),
            text: String(one.what ?? ''),
            dueAt: Number(one.dueAt ?? 0),
            moodV: undefined,
        });
    }
    const round = state.rounds ?? 0;
    for (const [index, one] of (state.autobio ?? []).entries()) {
        const text = String(one?.text ?? '');
        if (!text) {
            continue;
        }
        items.push({
            id: `autobio:${index}:${text.slice(0, 6)}`,
            kind: 'autobio',
            what: '',
            text,
            round: Number(one?.round ?? round),
        });
    }
    return items;
}

/**
 * 算这一轮的激活并写回 state 的两个附属位（返回给 CLI 报 --json 用的那个数）。
 * 注意：**没有任何一行把它写进 `all[chat]`**——激活值只活在这一轮里。
 */
function runActivation(state, ledger, now) {
    if (ACTIVATION_LAYER_OFF) {
        state.activationLine = null;
        return { value: 0, cue: '', key: '', kind: '' };
    }
    const text = cueOf(state.lastInboundText);
    const signals = {
        text,
        gist: gistOf(text),
        semanticKeys: semanticOf(text).map((one) => one.key),
        place: String(state.scene?.name ?? ''),
        placeKind: placeKindOf(state.scene),
        moodV: Number(state.moodV ?? 0),
        rounds: state.rounds ?? 0,
    };
    const hit = activationOf({
        items: activationItemsOf(state, ledger, now),
        signals,
        // 与 stories 共用同一份 contentGrams / 泛用词表（判据只有一处实现）；各算各的，不互相调用
        grams: contentGrams,
        isGeneric: (one) => STORY_GENERIC.has(one),
        seen: state.activationSeen ?? {},
        now,
    });
    // 出场条件三条（缺一不亮）：
    //   · 过出口线 0.65 且**至少一条内容线索**对得上（时间线单独不算，见 activation.mjs 文件头）
    //   · 他没在重复问同一件事（repeatCount ≥3 是同一条规矩，与 storyHit 共用事实）
    //   · 点亮的是一条**账**：卡片上"账"本来就有一行（commitmentsLine），激活只负责把那一行换成
    //     "现在被点亮的那一条"。autobio 的素材已经有「我已知的事」那一行，再挂一行就是两处写同一件事。
    //     autobio 的激活值仍然进 --json（标定与 trace 用），这是这一步刻意只做一半的地方。
    const fired = activationFires(hit)
        && (state.repeatCount ?? 0) < 3
        && String(hit?.item?.kind ?? '') === 'ledger';
    state.activationLine = fired ? activationLineOf(hit) : null;
    // 出口一：点亮的是一条账 → 账只在激活那一行出现，别再让承诺账那行重复一遍
    if (fired && String(hit?.item?.kind ?? '') === 'ledger') {
        state.commitmentsLine = null;
    }
    state.activationSeen = noteActivation(state.activationSeen ?? {}, hit, state.rounds ?? 0);
    return {
        value: round2(Number(hit?.value ?? 0)),
        recordValue: round2(Number(hit?.recordValue ?? 0)),
        cue: String(hit?.cue ?? ''),
        content: hit?.content === true,
        fired,
        key: String(hit?.item?.id ?? ''),
        kind: String(hit?.item?.kind ?? ''),
    };
}

// ── r65：v2 五层（需要 → 状态 → 情绪 → 防御 → 动机/内容点）──────────────
// 主人原话：「把'我该怎么说'变成'我想怎么说'」。
// 旧卡把"动作＋落法＋语尾建议＋把握"都塞给模型，模型只照着填空——9/9 段首「主人，」、
// 语尾拼成「喔呢」都是填空的接缝，不是她的口癖。

// 第 1 层：底层需要（长期不变，来自原典）。她四个需要里**只有"胜任"有把握**，
// 所以任何威胁它的事（不懂／被夸／被考）反应特别大。
const NEEDS = [
    { key: '关联', want: '想有人在', canon: '「说好了哦？我会等着你的」' },
    { key: '胜任', want: '游戏里我能', canon: '「从侵略者到最近的弹幕射击游戏，大部分都掌握了」' },
    // r107（2026-11-01）：**`自主` 退役**——原典 0 行（搜「你要我／叫我做／被安排／不得不」全无），
    // 爱岛里她的顾虑是「让他失望」，不是「被安排」。
    // 她的自主是**低剖面**（TRAITS.needProfile.autonomy='low'）：被安排时照做，不构成威胁。

    { key: '安全', want: '别被看穿', canon: '「尽量当一个旁观者就好」' },
];

// 第 3 层：情绪生成（appraisal）。不是"他这句属于哪一类"，是"这件事对我的需要意味着什么"。
const APPRAISAL = [
    {
        // r109（原典核对）：**收到东西/被照顾**不是"被抬高"，是"被在意"——原典她这里说「谢谢」。
        key: '被在意',
        when: (s) => s.praiseKind === 'gift',
        need: '关联',
        feeling: '收到东西，心里是暖的',
        motive: '道一句谢，别把这事说大',
    },
    {
        // r109：他指的是**她确实做过的事**（睡着了/迟到/忘了）→ 认下来，不辩解。
        // 依据：原典道歉类 27 行（1.3%）；而"她没做过"的时候她会顶回去（「我只是来散步而已」）。
        // r109（原典核对）：原 key 叫「我确实没做好」——那是自我裁定（AI 助手腔）。
        // 原典里她确实会为睡着道歉（「……抱歉。我好像睡着了。」「唔……要是我睡着的话，抱歉喔。」），
        // 但那是轻抱歉，不是"我没做好"。这一格只到"认一句"，不到"评价自己"。
        key: '睡过去了',
        when: (s) => s.ownActNow === true && s.wrongClaimNow !== true,
        need: '安全',
        feeling: '……啊，抱歉',
        motive: '道一句就好，别解释自己',
    },
    {
        // r107 构念层（L3 域限定自我效能）：非游戏域被否定时，她不是「来劲」，是缩一下。
        // 依据：原典 7 行「笨手笨脚／不太会／不擅长」；爱岛上她在游戏域反而有底气（我不觉得会输你）。
        // 阈值与 wound 同一档（0.6），但用独立量 woundSkill，不动 game/human 的派生。
        key: '非游戏域被否定',
        when: (s) => numOf(s.woundSkill, 0) >= 0.6,
        need: '安全',
        feeling: '心里缩了一下',
        motive: '把这句收短一点，别在这上面多说',
    },
    {
        key: '胜任被邀请',
        when: (s) => s.hisWeakNow === true || s.hisStuckNow === true,
        need: '胜任',
        feeling: '他这一说，我有点来劲——轮到我上场了',
        // r82（v14 §16.5 乙类，2026-10-26）：原来写「把他拉进同一局里，让他别自己硬扛」——
        // 那是**服务导向**（替他安排）。她要的是"一起打"，不是"帮他扛"。
        motive: '想跟他打一局',
    },
    {
        key: '被夸了',
        when: (s) => s.praiseNow === true,
        need: '安全',
        // r109（原典核对）：原 feeling 是「被抬这么高会摔的，我有点慌」——
        // 但原典里"被夸 + 慌/怕/压力"同句 0 行，那是我们加的威胁框架。
        // 她实际的样子是先否掉（「没有啦，只是这么觉得……」），落点是"不会接"，不是"怕摔"。
        feeling: '这种话不太会接',
        motive: '把这个高帽子推回那件事上',
    },
    {
        // r107：原 key 是「自主被侵犯」（need: 自主）。退役自主之后，这一格的含义其实很清楚：
        // 他把她**说错了**（骂她／给她按了个不属实的帽子）→ 落点是"判断不对"，不是"你要我听话"。
        key: '被误判',
        when: (s) => s.insultedNow === true || s.wrongClaimNow === true,
        need: '安全',
        feeling: '这句话不对劲，我不舒服',
        motive: '把这条线划出来',
    },
    {
        key: '关联受挫',
        when: (s) => (s.energy ?? 1) < 0.35 && s.lastInboundText && /走|走开|走了|再见|睡了|下次|算了/u.test(String(s.lastInboundText)),
        need: '关联',
        feeling: '他要走了，我有点空',
        motive: '让他别走（或者装作没关系）',
    },
    {
        key: '胜任不足',
        when: (s) => Boolean(s.offLimitNow),
        need: '胜任',
        feeling: '这个东西我不认识，我不知道该说什么',
        motive: '弄清楚那到底是什么',
    },
    {
        key: '关联被满足',
        when: (s) => /在干什么|干嘛|做什么|你在|还好吗|怎么样/u.test(String(s.lastInboundText ?? '')),
        need: '关联',
        feeling: '他问我在干什么，我乐得说',
        motive: '把我这件事说给他听',
    },
    {
        // v12（r80）：他把自己的事平平地说出来（不卡、不示弱）→ 关联需要被喂饱。
        // 依据：SDT 的关联需要是"被接住"，不是"帮他解决问题"；这一格她要的是**接上去**，
        // 不是把话扛过来。老版这里落到默认值"别让这段对话断掉"，于是每句都是同一副腔调。
        key: '他在讲他自己的事',
        when: (s) => perspectiveOf(s.lastInboundText).target === '在说他自己'
            && s.hisStuckNow !== true
            && s.praiseNow !== true
            && bareLen(s.lastInboundText) >= 4,
        need: '关联',
        feeling: '他在说他自己的事，我听着不费劲',
        // r82（v14 §16.5 乙类，2026-10-26 改）：原来写「顺着这件事往下问一步（他做到哪儿／怎么弄的），
        // 或者把我自己那半摆出来」——那是**替她指定回应策略**（问一步／摆出来都是动作指令）。
        // 改成她自己的念头：只有"想知道"这个状态，没有"怎么问"。
        motive: '想知道他那件事里细一点的地方',
    },
    {
        // v12：他说的是别人的事 → 她不在这个局里。既没被满足也没被伤到，这一格是"旁观但不冷"。
        // 依据：她的第四个需要是安全（「尽量当一个旁观者就好」），而关联需要没被卷进去时，
        // 人不会主动往里凑——所以这里的动机是听着、不插话，被问到才说自己那半。
        key: '他在说别人的事',
        when: (s) => perspectiveOf(s.lastInboundText).target === '在说别人',
        need: '安全',
        feeling: '这是别人的事，我不在里面',
        motive: '不插话，听着；他要是问我，我再说自己那半',
    },
    {
        // v12：他也没劲了 → 此时"陪"比"推进"合适。依据：依恋的共调节（co-regulation）——
        // 对方能量低时，安全基地做的是降低要求，不是加码。
        key: '他也没劲了',
        when: (s) => /累|困|没力气|不想动|没精神|好烦|无聊|没意思|有点闷/u.test(String(s.lastInboundText ?? ''))
            && s.hisStuckNow !== true,
        need: '关联',
        feeling: '他也没劲了，我就不用硬撑着把话说满',
        // r82（v14 §16.5 乙类）：原来写「把话放轻：应一句，或者给他一件很小的事」——
        // 那是**替她指定回应策略**（先给态度再给事），不是她的念头。改成她自己的说法。
        motive: '想把话说轻一点，不用撑着说满',
    },
    {
        // v5①：他像在试探／犹豫 → **只有两条路**：
        //   ① 直接问他怎么了（同一局里的 2P 位置）
        //   ② 不管他，说自己那一半
        // 原来还有第三条"评论他的语气/停顿"——那是**旁观者位置**，在她的模型里根本不成立
        // （她的需要是关联，不是理解他）。
        key: '他在试探或犹豫',
        // r106：有感知结果时**听感知的**（LLM 判"他是不是真的在犹豫"）；没有就退回原来的正则。
        // 病根：这条正则包含 `…`，而剧本几乎每句都有省略号 → 主卷 46.5% 被它吃掉。
        when: (s) => {
            const seen = currentPercept();
            const hesitant = seen ? seen.hesitant === true
                : /\.{2,}|…|是不是|在想什么|有话|怎么了|还行吗|在吗/u.test(String(s.lastInboundText ?? ''));
            return hesitant && !s.insultedNow && !s.praiseNow;
        },
        need: '关联',
        // r98（2026-10-30 盲测判决）：原文是「他好像有话没说完」。那句是**对他那句话形状的判断**，
        // 模型照着念了出来——「你刚才那半句，没说完。」6/86 轮，两位盲评各自判成元观察，
        // 占那两位 12 处 AI 味的一半（docs/persona-blind-r97.md §1.1 §3.2）。
        // 按这一格自己的注释（下面那段 v12 的话）该做的是"读心的结果进行为，不进言语"，
        // 所以 feeling 只留**她自己这一侧的状态**，一个字不提他那句话。
        feeling: '我这边还悬着，先不用把话说满',
        // v12（r80）：**读心的结果进行为，不进言语**（Apperly & Butterfill：心智化是隐性的）。
        // r73 那版写「直接问他怎么了」，模型渲染成点评他的句子——「你刚才那句像在跟谁确认什么」
        // （r80 实测 8/86 轮都长这样）。这一格她真正会做的是：把话头往自己这边拉，等他愿意讲。
        // r82（v14 §16.5 乙类，2026-10-26）：原来是「把话头往我这边拉一点——说我这一会儿的事，
        // 他愿意讲就会讲」——「往我这边拉」是策略、「他愿意讲就会讲」是他的反应，都不是她的念头。
        // 只留她自己的那半：她要说的就是"我这一会儿的事"。
        motive: '想说我这一会儿的事',
    },
    {
        // r82（R1 / psych §1 出口二）：账被他的那句话点亮了。
        // **追加在最后**：顺序就是优先级，所以被夸／被骂／被要数这些仍然先走；
        // 这一格只在"这一轮确实有一条账被线索勾起来"时参与（`activationLine` 是现算的，
        // 不是落盘的量——见 activation.mjs 文件头第 1 条）。
        key: '账被点亮了',
        when: (s) => Boolean(s.activationLine),
        need: '关联',
        feeling: '刚才那句话把它勾起来了',
        motive: '自己先把那件事提一句',
    },
];

// 第 4 层：防御。需要对挫时她不直接说情绪，而是用这几种处理——**这才是她说话形状的来源**。
// 「当下最省力的那一种」决定她想要什么。
function defenseOf(state) {
    if ((state.energy ?? 1) < ENERGY_FADE) {
        return { key: '退缩', motive: '先安静一会儿，别撑着' };
    }
    // 被夸**不是**被调侃：那是"安全被威胁"，交给 appraisal（想推回去），别用幽默打岔
    if (state.teasedNow && !state.praiseNow) {
        return { key: '幽默', motive: '把他那句荒谬原样摆回去，看他自己笑不笑' };
    }
    if (state.insultedNow || state.wrongClaimNow) {
        return { key: '攻击性升华', motive: '把这条线划出来' };
    }
    if (state.offLimitNow && (state.energy ?? 1) >= 0.5
        // r105：他**在讲自己的事**时不该触发"我不懂"（"今天上班被骂了"不是问她职场的事）
        && /[？?]|怎么|为什么|是什么|是不是|帮我|教我|讲讲|跟我说/u.test(String(state.lastInboundText ?? ''))) {
        return { key: '接受', motive: '直接承认我不懂，不用绕' };
    }
    // r82（F / psych §13.4 让路②）：原来这里还有一支「理智化／转移」，唯一触发是 `wound ≥ 0.6`。
    // 那种情形现在由 `WOUND_APPRAISAL` 接管（被戳到 = 事故级入口，比防御这一层更早说话），
    // 两支都留着就是同一件事写两处。**`feelingLine` 的 tail 表里那一项保留不动**（有 `?? ''` 兜底）。
    return null;
}

// ── r82（F / psych §13.3）：两种疼各自的评估（r101 起 rank 那一条退役）───────────────
// **不进 APPRAISAL 数组**：它靠 `woundKind` 短路取用，两条天然互斥，不依赖数组顺序，
// 现有 10 条的相对顺序因此零改动（零回归）。文案与依据见 psych §13.3 的表（原典场次：
// game `e02_002_030`／`e06_236_000`；human `e08_009_032`／`e06_236_000`）。
const WOUND_APPRAISAL = {
    game: {
        key: '只剩下游戏这一样',
        when: (s) => s.woundKind === 'game',
        need: '胜任',
        feeling: '这句我认——我确实只剩这一样',
        motive: '把手边那件能验证的事摆出来（哪一关、哪一次、怎么过的）',
    },
    human: {
        key: '被问到是不是人',
        when: (s) => s.woundKind === 'human',
        need: '安全',
        feeling: '这个问题我有答案，答完还能接着说别的',
        motive: '承认一句，然后回到刚才那件事上',
    },
    // r101：`rank` 这一条已整套退役——它没有原典支撑（见文件头与 docs/r101-wound-rank-removed.md），
    // 连同只被它用到的 `needs` 双需要字段一起删掉。留下的两条读的是 `s.woundKind`，互斥天然成立。
};

function appraisalOf(state) {
    // 被戳到是"事故级"入口：命中就只取这一条，不参与后面 10 条的排队
    const wound = state.woundKind ? WOUND_APPRAISAL[state.woundKind] : null;
    if (wound) {
        return wound;
    }
    return APPRAISAL.find((one) => {
        try {
            return one.when(state) === true;
        }
        catch {
            return false;
        }
    }) ?? null;
}

// ── r82（C / 续话）：她说完一句之后，还有话就补一句 ──────────────────────────
// 病根（主人）：她回一条之后**永远没下文**。这不是句式问题，是"手上还有没有没说完的事"——
// 所以这一层只产**材料**（哪件事还没说完），不产生"这一句该怎么补"的话（那是 r78 的坑）。
// 四条判据**同时**满足才算：
//   ① 她刚说的那句不是问句（问了就等他答，不许自己接着往下说）；
//   ② 手上有一件具体的、没说完的事（承诺账里还欠着的 ／ 她讲过、已经用过的故事）；
//   ③ 距上一次补话 ≥10 分钟（不许连发）；
//       注：字面口径是"距上一条她的话"，但发送侧是"她回完 35–50 秒后补一条"，
//       按字面那一条永远不成立（自相矛盾）。这里取"距上一次**补话**"，同样防连发。
//   ④ 今天补过 ≤2 次。
// `MIND_FOLLOWUP=0` 时整层不参与（返回 null，state.followUp 也不写）。
const FOLLOWUP_LAYER_OFF = process.env.MIND_FOLLOWUP === '0';
// r82（S2）：人情账增量受 `MIND_SOCIAL=0` 管（与 heart 那一侧同一个开关语义）
const SOCIAL_FAVOR_OFF = process.env.MIND_SOCIAL === '0';
const FOLLOWUP_MIN_GAP_MIN = 10;
const FOLLOWUP_DAILY_MAX = 2;
const FOLLOWUP_QUESTION_TAIL = /[？?]\s*[。！!…]*$/u;

function dayKeyOf(ms) {
    const one = new Date(Number(ms) || Date.now());
    return `${one.getFullYear()}-${String(one.getMonth() + 1).padStart(2, '0')}-${String(one.getDate()).padStart(2, '0')}`;
}

/**
 * 这一轮"还想补一句"的材料。返回 `{ text, kind, reason }`，没有就 `null`。
 * 只读：不改 state（配额由 `--followup-sent` 那一处写）。
 */
export function followUpLine(state, opts = {}) {
    if (FOLLOWUP_LAYER_OFF) {
        return null;
    }
    const now = numOf(opts.now, Date.now());
    const lastSaid = String((state?.lastSaid ?? []).slice(-1)[0] ?? '').trim();
    if (!lastSaid) {
        return null;
    }
    // ① 她刚说的是问句 → 等他答，不补
    if (FOLLOWUP_QUESTION_TAIL.test(lastSaid)) {
        return null;
    }
    // ③ 距上一次补话 ≥10 分钟
    const lastAt = numOf(state?.lastFollowUpAt, 0);
    if (lastAt > 0 && now - lastAt < FOLLOWUP_MIN_GAP_MIN * 60000) {
        return null;
    }
    // ④ 今天补过 ≤2 次
    const day = dayKeyOf(now);
    const today = String(state?.followUpDay ?? '') === day ? numOf(state?.followUpToday, 0) : 0;
    if (today >= FOLLOWUP_DAILY_MAX) {
        return null;
    }
    // ② 手上有一件具体的、没说完的事：先看承诺账（还欠着的），再看她讲过的故事
    // 返回的是「材料/处境」（material），**不是要发出去的那句话**——真正的句子由发送侧重新生成。
    const owed = Array.isArray(opts.openItems) ? opts.openItems : [];
    const item = owed.find((one) => String(one?.what ?? '').trim().length >= 2);
    if (item) {
        return { material: String(item.what).trim(), kind: 'owed', reason: '承诺账里还欠着这一件' };
    }
    const used = trustedStoriesOf(state).filter((one) => numOf(one?.used, 0) >= 1);
    if (used.length) {
        const story = used[used.length - 1];
        return {
            // ⚠️ 这是**给模型看的材料**，不是可以直接发出去的一句话
            //（2026-10-26 事故：把它当消息原样发出去 = 复读一小时前的原话）
            material: String(story?.cue || story?.text || '').trim().slice(0, 40),
            kind: 'story',
            reason: '这件事她讲过一次，还有一半没说',
        };
    }
    return null;
}

// 第 5 层：动机（防御优先于 appraisal——防御是她处理情绪的方式）
// ── r82（group-want3，2026-10-26 Lead 裁决）：**kind 在产生动机的那一支直接声明** ──────
// 为什么不再按文案分类：文案是**渲染结果**，不是来源——两个不同动机可能渲染成同一句，
// 同一句也可能来自不同动机。拿文案匹配就是"看起来对、实际会漂"。
// 所以每个 return 自带 kind；卡片读 motiveLine().text，闸门读 wantOf().kind（同一个真相）。
// 默认是 **listen，不是 speak**：这道闸管"要不要开口"——放出去的成本是"说了不该说的话"，
// 留在家里的成本只是"这一轮没吭声"，而后者是设计允许的（主人接受"累了可以不回"）。
// r82（根因③，2026-10-26）：`about` 是**审计字段**——`kind==='speak'` 时必须能读出"她要说的那件事"。
// 它只进日志/审计，**不进卡片**（卡片上为了互斥不重复内容，那是对的）。
const mt = (text, kind, about = '', branch = '') => ({ text, kind, about: String(about ?? ''), branch: String(branch ?? '') });

// ── r105：把「listen」从垃圾桶改成一种真正的欲望，并让情绪／思考真的落到 want 上 ──────────
//
// 病根（r104 的普查）：主卷 86 轮里 **84 轮是 `listen`，且 84 轮的分支字段全空**——
// "她这轮真没话"、"她想听"、"我没判出来"三件事被折叠成一个词，审计上不可分辨；
// 卡片「心里」里 79% 落在「我这边还悬着，先不用把话说满」（减话）与「平平的」（真空）。
//
// 心理学依据（只用公认的三条，不新造）：
//   · **Frijda 的行动准备（action readiness）**：情绪不是"感觉"，是"准备做什么"——
//     approach（想改变/想加入/想说）／attend（想接收信息）／withdraw（退）。所以 `want.kind`
//     应当是**行动倾向**的投影，而不是"这一轮有没有被判出来"。
//   · **OCC 模型**：一个欲望要有**对象**（desirability 针对某个事件）与**强度**（arousal）；
//     没有对象的"想"不是欲望，只是空转。
//   · **自我决定理论**：动机强度随"精力（capacity）× 需要被满足"变化——同一件事，累的时候只想短答，
//     不是不想说。
//
// 落地（三值 + 一个连续量）：
//   `speak`＝approach（有对象要说：要自己那一半／推回夸奖／问清词／算清楚／说旧账）
//   `listen`＝attend（**只想听**：他讲的是他自己的事、他的感受，她手上没有可加的对象）
//   `none`＝没有参与（非人话／别人的事／问到第 3 遍／耗尽／兜底）→ 应一声，不是"听"
//   `intensity`＝0..1，由 relevance（倾向）× arousal（心情唤起）× energy（真实精力）合成，
//     再乘 (1-0.4*avoidance)。它只决定**说多少**（full／brief／state），不决定 kind。
//   回滚：`MIND_WANT_TRI=0` → kind 退回二值（listen/speak），`MIND_WANT_INT=0` → cost 读 1/0。
const WANT_TRI_OFF = process.env.MIND_WANT_TRI === '0';
const WANT_INT_OFF = process.env.MIND_WANT_INT === '0';
// 评估 → 行动倾向（键与 APPRAISAL 的 key 对齐；不在表里的按 attend 处理并记 reason）
const TENDENCY_OF_APPRAISAL = new Map([
    ['胜任被邀请', 'approach'],
    ['被夸了', 'approach'],       // 被夸 → 推回那件事，那是**一句要说的话**，不是"听"
    ['被误判', 'approach'],
    ['睡过去了', 'approach'],
    ['被在意', 'approach'],
    ['关联受挫', 'approach'],         // 他要走 → 往回拉（SOUL §1）
    ['胜任不足', 'approach'],         // 不懂 → 定义级提问（L3951）
    ['关联被满足', 'approach'],       // 他问我 → 我乐得说（有对象才说，无对象由兜底接管）
    ['他在讲他自己的事', 'attend'],
    ['他在说别人的事', 'none'],
    ['他也没劲了', 'attend'],
    ['他在试探或犹豫', 'attend'],
    ['账被点亮了', 'approach'],
    ['只剩下游戏这一样', 'approach'],
    ['被问到是不是人', 'approach'],
]);
const KIND_OF_TENDENCY = { approach: 'speak', attend: 'listen', none: 'none' };
// 防御 → 倾向（退缩是 withdraw；幽默/攻击/接受都是要说一句）
const TENDENCY_OF_DEFENSE = { 退缩: 'none', 幽默: 'approach', 攻击性升华: 'approach', 接受: 'approach' };
const RELEVANCE_OF_KIND = { speak: 1, listen: 0.7, none: 0.2 };

/**
 * 这一轮"想说到什么程度"（0..1）。只影响说多少，不影响 kind。
 * resonance 由 OCC 的 desirability、moodA 的唤起、energyTrue 的容量三者线性合成——
 * 系数是**第一版**，没有原典频次可标定，所以整条挂在 `MIND_WANT_INT=0` 后面。
 */
function wantIntensityOf(state, kind) {
    if (WANT_INT_OFF) {
        return kind === 'speak' ? 1 : 0;
    }
    const relevance = RELEVANCE_OF_KIND[kind] ?? 0.2;
    const moodV = clamp(Math.abs(numOf(state?.moodV, 0)), 0, 1);
    const moodA = clamp(numOf(state?.moodA, 0), 0, 1);
    const arousal = clamp(0.5 * moodV + 0.5 * moodA, 0, 1);
    const energy = clamp(numOf(state?.energyTrue ?? state?.energy, 0.72), 0, 1);
    const avoid = (state?.insultedNow === true || state?.wrongClaimNow === true) ? 1
        : (numOf(state?.repeatCount, 0) >= 5 ? 0.7 : 0);
    return clamp((0.5 * relevance + 0.3 * arousal + 0.2 * energy) * (1 - 0.4 * avoid), 0, 1);
}


// ── r86（2026-10-28）：她自己想过的事，回到「这一轮我要说什么」 ──────────────────────
// 病根（r85 审计，14 天 127 轮）：`motiveOf` 的 14 支里**没有一支读 `state.thought`**。
// r84 让她在他不在的时候独立思考、写下「想／挂着／想做／记住」，那些话进了 state、
// 进了卡片末尾的一行、进了她自己写的记忆文件——但**没有回到动机**。
// 于是 75/127 轮（59.1%）的动机是 #11「听他把那件事说完」，而 #11 是兜底前的最后一级：
// 它接住的其实是"她手上什么都没有"。**不是她只想听，是她想过的那件事在代码里不存在。**
//
// 这一支不新增任何"该怎么说"，也不替她排序——**分量是她自己给的**：
//   · `keep`：她在思考里判过"值不值得留到以后"（r84 四段里的第四段）；判"值得"分量就重；
//   · 新鲜度：离现在越远那件事越沉（`THOUGHT_TTL_MS` 就是那条想法自己的存活期）；
//   · `acted`：她已经把这件事说出去了 → 说出来了就轻（Zeigarnik 的反面：结掉的事不再占心智）。
// 三个量合成 `weight`（0..1）。`weight` 只决定它**抢不抢话**，不决定"能不能说"。
const AGENDA_SAY_HI = 0.62;   // 到这条线：这一轮她想说它（她自己判它重要，且还热着）
const AGENDA_SAY_LO = 0.34;   // 到这条线：她心里还挂着它（做底色，不左右她说什么）
const AGENDA_CHARS = 26;
// 她写的"想做"里出现这些 = 这件事的落点在他身上（是要跟他说的话），不是自己闷头做
const AGENDA_TO_HIM = /找他|问他|告诉他|跟他说|对他说|提一句|说一句/u;
// r104：**她自己的打算／欲望**也算"要说的话"——不是只有"要对他说"才许开口。
// 病根：原来 `speakable` 只认上一行，于是「我想玩那一关」「我打算打完再睡」这类在源头就被判成
// "只能做底色"，这就是"欲望分量不够"的一处。剧本级依据（scenes-chiaki.tsv）：
//   e08_009_019「我打算在作战开始以前都玩游戏，／要陪我吗？」（他问，她直接说自己的打算并邀他）
//   e09_709_003「莫名开始有点想玩玩看了。」
//   e08_009_013「我也没事做耶。／那我们一起想想打发时间的方法吧。」
// 回滚：`MIND_WANT_OWN=0` → 只认 AGENDA_TO_HIM（r103 行为）。
const AGENDA_OWN_WANT = /想(?:玩|打|试|看|去|睡|吃|买|学|问)|打算|准备|要不要一起|陪我|一起去|那一关/u;
const WANT_OWN_OFF = process.env.MIND_WANT_OWN === '0';

// 「她说的这句里有没有提到那件事」——两个口径取并：
//   · 字面包含：`一会儿` 这种三字短语在 `contentGrams` 里是**零个二元组**（「会」在虚词表里），
//     只靠 sharesTopic 会永远判"没提过"，所以先看字面；
//   · 共同实词：她换了个说法时（"存档" vs "那一局"）也能认出来。
function overlapsText(a, b) {
    const left = String(a ?? '').trim();
    const right = String(b ?? '').trim();
    if (!left || !right) {
        return false;
    }
    return left.includes(right) || right.includes(left) || sharesTopic(left, right, 1);
}

export function agendaOf(state, now = Date.now()) {
    const one = state ? state.thought : null;
    if (!one || typeof one !== 'object' || THINK_LAYER_OFF) {
        return null;
    }
    const at = numOf(one.at, 0);
    const want = String(one.want ?? '').trim();
    if (at <= 0 || !want || now - at > THOUGHT_TTL_MS) {
        return null;
    }
    // 说出口了 → 这件事已经结掉，不再占心智（`acted` 由 `--think-acted` 写回；
    // `lastSaid` 是"她这一轮刚说的"，思考层一小时才拍一次，等它记太慢）
    const acted = Boolean(one.acted) && one.acted.kind === 'speak' && one.acted.ok === true;
    const lastSaid = String((state?.lastSaid ?? []).slice(-1)[0] ?? '').trim();
    // r107：`about` 常常是"没什么"（她自己写的空所指，线上 23 拍里有 4 拍）。
    // 这种时候拿 it 去比冷却**永远不匹配**（于是同一件事可以连着抢话），下游还会拿到一个
    // 什么都指的"没什么"。所以：没有真所指时，冷却与 about 都改用 `want` 原文兜底。
    const rawAbout = String(one.about ?? '').trim();
    const hasReferent = rawAbout !== '' && !/^(?:没什么|没有|无|没了|什么都没有|都还好|不清楚)[。！？…\s]*$/u.test(rawAbout);
    const chars = Array.from(want);
    const cut = chars.slice(0, AGENDA_CHARS).join('');
    // 冷却取并（r88 的教训：字面包含与共同实词要取并，不能只走一条）
    const justSaid = overlapsText(hasReferent ? rawAbout : want, lastSaid) || overlapsText(want, lastSaid);
    if (acted) {
        return null;
    }
    const fresh = 1 - Math.min(1, Math.max(0, (now - at) / THOUGHT_TTL_MS));
    const base = one.keep === true ? 0.66 : 0.34;
    const weight = Math.min(1, base + 0.34 * fresh) * (justSaid ? 0.4 : 1);
    return {
        line: `${cut}${chars.length > AGENDA_CHARS ? '……' : ''}`,
        about: hasReferent ? rawAbout : cut,
        weight,
        // 她只写了"想打一局"这种 → 那是她手上的一件事，不是要说的话：不抢动机，只做底色
        // r104：这句的判断改了两处——①"她自己的打算"也算要说的话（依据见 AGENDA_OWN_WANT）；
        // ②回滚开关 `MIND_WANT_OWN=0` 回到只认"要对他说"的旧口径。
        speakable: AGENDA_TO_HIM.test(want) || (!WANT_OWN_OFF && AGENDA_OWN_WANT.test(want)),
    };
}

// r86：导出给自测用——`branch` 是判"这一轮动机从哪一支来"的唯一口径，
// 只读 `motiveLine().text` 会把 #15 和 #10 混成同一句（r82 就有过这个坑）。
function decideMotive(state) {
    // r75：受伤期间——不想多说，也不主动邀约（退回安全基地，自我保护）→ listen
    // r101：`hurtByDismiss` 让路随心结③一起退役（不再有人写 `hurtBy='算漏'`）。
    // 旧 state 上残留的那个值由下面这条通用分支照常处理，没有特例。
    if ((state.hurtUntil ?? -1) >= (state.rounds ?? 0) && (state.hurtAt ?? -99) < (state.rounds ?? 0) && !state.insultedNow) {
        return mt('不想多说，等他自己想清楚', 'none');
    }
    // r67 起：适应优先于一切——问第 3 遍起，她的动机不再是"回答他"。
    // v9（主人 2026-10-24）：**不报数，是态度下滑**——"你问第 N 遍了"是报数，烦躁是态度；
    // 报数会算错，态度不会。所以这里给的是"她还有多少耐心"，不是"这是第几遍"。
    // 四条都是"不想多给"→ listen。
    const repeat = state.repeatCount ?? 0;
    if (repeat >= 3) {
        if ((state.energy ?? 1) < 0.3) {
            return mt('不问也不答，只应一声', 'none');
        }
        if (repeat >= 7) {
            return mt('不想理了——同一个东西问这么多遍，我先不接', 'none');
        }
        if (repeat >= 5) {
            return mt('不太耐烦了——只给最要紧那一句，别的懒得说', 'none');
        }
        return mt('有点烦了——答短一点，别把话说得太全', 'none');
    }
    // v13（r81）：这一句得先想一下（数／条件／原因／人）→ 她的动机是"把它弄清楚"，
    // 不是"接得上就行"。依据见 reasoningKind 上方：System 1 抢答是这一类错的根。
    // ── 非人话（CQ 码／命令／转发记录／纯 QQ 标记／纯符号）→ 一律 listen ─────────────────
    // 根因①②的共同前置：这些话不是"他说的事"，她不可能有话要说。
    // 之前只在 #10 里挡了，于是 #6（推理）照样被 `[转发聊天记录…]` 触发 → 回放里冒出假 speak。
    if (!isHumanText(state.lastInboundText)) {
        return mt('应一声就好', 'none');
    }
    // 要问一句 → speak（她手上有明确的"要问的东西"）。
    // ── r82（2026-10-26 Lead 裁决二）：收紧成"他那句得**在问/在报数**" ────────────────────
    // ① 含 `？/?` 或疑问词；② 并且确实缺口径（`reasoningKind` 原判断保留）。
    // 描述性长文（「迷你世界玩家你们开心了吧…」）里带数字，不再点着 #6。
    // **不许**改成"字数超过 N 就跳过"那种拍脑袋规则。
    const ASKING = /[？?]|多少|几个|几号|几点|几天|第几|哪|是不是|有没有|算一下|统计/u;
    const hisLine = String(state.lastInboundText ?? '');
    const reason = REASONING_LAYER_OFF ? null : reasoningKind(state);
    if (reason && ASKING.test(hisLine)) {
        const noNumber = (reason.key === '一个数' || reason.key === '一个口径') && !HAS_NUMBER.test(hisLine);
        // ── Lead 裁决一：`about` 写成**她实际要问的那句**（含他那句里的数/口径词，不许自己编）──
        const numberOrWord = (hisLine.match(/\d[\d,.]*/u)?.[0] ?? '')
            || (hisLine.match(/(?:多少|几个|几号|几天|第几|哪|是不是|有没有)/u)?.[0] ?? '')
            || reason.key;
        const about = noNumber
            ? `他说的「${numberOrWord}」是哪来的数`
            : `他说的「${numberOrWord}」是按哪个口径算的`;
        return noNumber
            ? mt('先问清是哪个数，不拿别的数顶上', 'speak', about, '#6')
            : mt(reason.motive, 'speak', about, '#6');
    }
    // ── ② 她手上有具体对象 → **speak**（2026-10-26 Lead 裁决：有对象必须压过"有反应"）──────
    // 判据：一个人心里真有件事的时候，就算在防御、在琢磨，**也会说**；
    // 反过来，只有反应没有对象的时候，她就只是听着。所以这一段排在防御/评估**之前**。
    // ⚠️ 序即语义：挪一个分支到前面就等于改了她的性格；动顺序必须同时改 docs §17.4b 的表。
    // ── S3b 不变式：讲过两遍以上的事不许再当"没说完" → 回到"应一声就好"（listen）────────
    const storyNow = storyHit(state);
    const storyUsed = storyNow ? numOf(storyNow.used, 0) : 0;
    if (storyUsed >= 2) {
        // 讲过两遍以上 → 她没料可加（这是 none，不是"想听"）
        return mt('应一声就好', 'none', '', 'S3b');
    }
    // ── #10b 她的手上有块地（2026-10-27）：她真在玩星露谷（stardew.json 15 分钟内写过）
    // 且还有没浇的地 → 这就是她手上一件没做完的事，是她自己要说的那句话。
    // **只此一支，不按游戏状态写分派表**（不要 if 季节/金钱/体力），about 必须带具体块数。
    const farmLine = stardewWaterLine(state);
    if (farmLine) {
        return mt(farmLine, 'speak', '农场浇水', '#10b');
    }
    // ── S4c 不变式：同一件事不许在两张卡行里出现两次 ────────────────────────────────
    const storyLineShows = Boolean(storyNow) && storyUsed === 1;
    // r88：这一支原来把「承诺账欠着」也算成"她手上有没做完的事"。账本属于记忆层（§17.8），
    // 记忆不能直接替她决定说什么——所以默认只保留**工作记忆里与当轮相关的那段故事**
    // （它本身已经由 `relatedStoryOf` 按话题相关筛过）。
    // 有账的情形交给：① 他这句话点亮它（`activation` → appraisal「账被点亮了」）
    //              ② 她自己想过（`#15` / 「我心里还挂着」）。
    // `MIND_DEBT_MEMORY=0` 时把账那半边原样加回来（r87 行为）。
    const openItem = DEBT_MEMORY_OFF ? openItemsOf(state) : relatedStoryOf(state);
    if (openItem) {
        const debtShows = DEBT_MEMORY_OFF && Boolean(String(state.commitmentsLine ?? '').trim());
        return mt((storyLineShows || debtShows) ? '想把那件事说完' : `想把「${openItem}」那件事说完`, 'speak', openItem, '#10');
    }
    // #13 她这一轮已经有个判断（去/不去算过了）→ 那句判断本身就是她要说的话 → speak
    const moveTarget = sceneMoveTarget(state.lastInboundText);
    if (moveTarget) {
        const verdict = willingToMove(state, moveTarget, new Date(), state.inviteStake ?? 0);
        if (verdict.go !== true) {
            // r101：这里原来插的是**对象**，`about` 就成了「[object Object]（不去）」。
            // `about` 只进日志／审计（§17.3 明说不上卡片），所以一直看不见——顺手修掉。
            return mt('想把话说清楚，但这一步不想动', 'speak', `${moveTarget.id ?? ''}（不去）`, '#13');
        }
    }
    // ── #16（r104）：**那件事被他一个人拿走了**（他要一个人做／他替她做完）→ speak ─────────
    // 病根：这六道有代价的处境（r100 卷 Q03/Q14 等）此前全落 `listen`，于是她只能"先接住再补半句"。
    // 依据（剧本级）：L7834「这个我也会演奏，我可以参加吗？」／e04_007_091「我可以说几句话吗？」／
    //   e05_102_105「那本文档……让我看一下。」——她要的是"自己那一份"，不是"把话接住"。
    // 序即语义：压过"只是有反应"（防御／评估），但低于她手上真有的旧账（#10）与已算过的判断（#13）。
    const stake = stakeOf(state);
    if (stake) {
        return mt(stake.motive, 'speak', stake.about, '#16');
    }
    // ── #17（r104）：**他在问她想要什么** → 她要说的是"她要的那件事"，不是"说给他听" ──────
    // 依据：e08_009_019「我打算在作战开始以前都玩游戏，／要陪我吗？」（他问，她直接说自己的打算并邀）；
    //   e08_009_013「我也没事做耶。／那我们一起想想打发时间的方法吧。」／e09_709_003「莫名开始有点想玩玩看了。」
    if (asksWhatSheWants(state)) {
        const own = agendaOf(state);
        const about = (own && own.about) || '我这一局';
        return mt(own && own.weight >= AGENDA_SAY_LO ? '想说我自己那件事' : '想说我要做的那件事', 'speak', about, '#17');
    }
    // ── #18（r105）：**他在请她做一件事** → 先问清是哪一样（不是"他的新闻"，也不是"想听"）──
    // 依据：e05_102_105「那本文档……让我看一下。」（先要过来看，不先答）；挤奶段她一路问「获得是什么意思」。
    const request = requestOf(state);
    if (request) {
        return mt(request.motive, 'speak', request.about, '#18');
    }
    // ── ③ 她只是有反应 → **listen**（分支内容与文案一个字没动）───────────────────────
    const defense = defenseOf(state);
    if (defense) {
        // r105：按行动倾向映射（退缩＝none；幽默/攻击性升华/接受＝要说一句）
        const kind = KIND_OF_TENDENCY[TENDENCY_OF_DEFENSE[defense.key] ?? 'approach'] ?? 'speak';
        return mt(defense.motive, kind, '', `D:${defense.key}`);
    }
    const appraise = appraisalOf(state);
    if (appraise) {
        // r105：按行动倾向映射——被夸/示弱/被辱/不懂/被问 → speak；他在讲他的事、他也没劲了 → listen；
        // 他在说别人的事 → none。表在 TENDENCY_OF_APPRAISAL，只有一处定义。
        const kind = KIND_OF_TENDENCY[TENDENCY_OF_APPRAISAL.get(appraise.key) ?? 'attend'] ?? 'listen';
        return mt(appraise.motive, kind, '', `A:${appraise.key}`);
    }
    // ── #15 她自己想过的那件事（r86）：这一轮她要说的话 ──────────────────────────────
    // 序即语义：排在防御／评估**之后**（当下的情绪比一小时前的想法更近），
    // 排在 #11 **之前**——#11「听他把那件事说完」是兜底前的最后一级，它接住的是
    // "她手上什么都没有"；r84 之后她手上有东西了，那一格本来就该由这件事来填。
    const agenda = agendaOf(state);
    if (agenda && agenda.speakable && agenda.weight >= AGENDA_SAY_HI) {
        // r87：次数照实写（她压了多久，是事实）；写不写"该做点什么"不归我们管。
        const loop = WILL_LAYER_OFF ? { times: 0 } : loopOf(state);
        const times = loop.times >= 2 ? `（这件事已经想了 ${loop.times} 次）` : '';
        return mt(`还惦记着我自己那件——${agenda.line}${times}`, 'speak', agenda.about, '#15');
    }
    // ── #19（r106）：他讲他自己的事**且跟她有交集** → 共鸣，递自己那一小份 ────────────────
    // 依据：e08_009_013「我也没事做耶。／那我们一起想想…」／e08_009_032 牧场段她接自己的游戏联想／L2202。
    // 与「他也没劲了（降要求地陪）」的分工：**能量低 → 陪着**（那条 appraisal 在上面先说话）；有交集的经验 → 共鸣。
    const resonance = resonanceOf(state);
    if (resonance) {
        return mt(resonance.motive, 'speak', resonance.about, '#19');
    }
    // ── ④ 听／空姿态／兜底 → **listen** ──────────────────────────────────────────
    const news = newsOf(state);
    if (news.news) {
        // #11 他讲的是他自己的事 → 听，不是猜（不解读他）
        // r98：原文「想听他把那件事说完」——同样是"替他描述进度"，模型会念成
        // 「你刚才那件事，还没说完」（轮 15/43/73，盲评判元观察）。改成她自己的那一份。
        return mt('说我自己那一份', 'listen');
    }
    if (!news.news && news.why) {
        // #12 空姿态
        return mt('想说说我这一会儿在干嘛', 'none');
    }
    // #14 兜底 → none（不是 speak，也不是"想听"：这一轮没有参与的对象）
    return mt('没什么想说的，应一声就好', 'none');
}

/**
 * r105：对外只认这一个入口。`decideMotive` 给"倾向与对象"，这里补上**强度**与**原因码**，
 * 并把 `listen` 收窄成"只想听"（`attend`）。`none` 是一个正当结果，不是垃圾桶。
 */
export function motiveOf(state) {
    const one = decideMotive(state);
    // 回滚臂：二值口径（r104）——`none` 折回 `listen`，不出现第三值
    const kind = WANT_TRI_OFF ? (one.kind === 'none' ? 'listen' : one.kind) : one.kind;
    return {
        ...one,
        kind,
        intensity: wantIntensityOf(state, kind),
        reason: String(one.branch || one.reason || 'fixed'),
    };
}

// 卡片只读文本；闸门只读 kind —— 同一个真相，两处读同一个函数的返回值
export function motiveLine(state) {
    return motiveOf(state).text;
}

export function wantOf(state) {
    const one = motiveOf(state);
    // ── 根因②（2026-10-26）：**一条 story 要被"消费"** ──────────────────────────────
    // 以前它永不消费：连续五轮都能当"没说完的事"。这里算完 want 之后记下 `about` 与轮次，
    // `relatedStoryOf` 遇到同一个 `about` 会在 3 轮内跳过（冷却）——不用随机，也不靠外部计时。
    if (one.kind === 'speak' && one.about && state && typeof state === 'object') {
        state.lastWantAbout = one.about;
        state.wantAboutAt = numOf(state.rounds, 0);
    }
    return one;
}

// ── r83（§19 第 9 层）：她自己权衡"这一轮怎么落地" ────────────────────────────────────
// 题眼（主人 2026-10-26）：「'累到不想说话'中的'累'本来就是心理学模型中的属性一种，但她也要
// 权衡'不想说话的代价'——因为厌恶而不想说，那就没有代价；真的因为累，那她怎么想？」
// 所以这一层只回答一件事：**在「说全／短答／短答＋说明状态／不说」里，哪一个让她自己更舒服。**
//
// 三条纪律（都是前几轮踩出来的）：
//   ① **没有阈值**，只有一次 `argmax`（比较在 cost.mjs，系数一个都不许自己改）；
//   ② **`net` 只进 `--json`／trace／stderr 日志，绝不进卡片**（进了就是"我算了一下更划算"的台账腔）；
//   ③ **只算，不接线**：这一轮不改变她说多少。理由写在 docs/r83-implementation.md §3——
//      "让 pick 决定落法"必须先有真实分布（本节没有任何一个数被标定过），
//      否则就是拿没标定的设计值去改她的行为。**开关 `MIND_COST=0` = 与 r82 逐字一致。**
const COST_LAYER_OFF = process.env.MIND_COST === '0';
// r102：`MIND_COST_ACK=0` → 沉默的代价退回"只乘 want"（r101 行为）。回滚用，不参与线上正常路径。
const ACK_COST_OFF = process.env.MIND_COST_ACK === '0';

// 「她最近说过一次自己的状态」的判据。**只认状态词，不认语气词**：
// 原典里她的说明是「啊，有点困了」这种（psych §19.4 引了 8 条），不是「我累了不想理你」。
const EXPLAINED_RE = /困|累|睡|乏|没力气|撑不住|打盹|眯一会儿/u;

/**
 * §19 的六个输入，**全部从现有 state 派生**。
 * ⚠️ 规格书 §19.2 写的是 `pull`／`imposition`，这两个字段在代码里**根本不存在**（r83 全仓 grep 过）。
 * 所以下面每一条都标了替代量；替代不是等价，换代要重标（见 `docs/r83-implementation.md` §2）。
 * @param {object} state
 * @param {{ want?: object, waiting?: number }} opts
 */
export function costInputsOf(state, opts = {}) {
    const want = opts.want ?? wantOf(state);
    // 想说 = 1／不想说 = 0：与 §18 `wantStrengthOf` **同一个口径**（intent.mjs:1338），
    // 不在这里另造一个连续强度——那样 §18 与 §19 就会长出两个"她有多想说"。
    // r105 试过另一种做法：把 `motiveOf().intensity` 直接当 `want` 接进第 9 层。
    // **实测否决**：标定卷 26 题从 26/26 掉到 **23/26**（net-13／net-19 目标 brief 却 pick=full，
    // net-23 目标 full 却 pick=state），四档从 9/13/2/2 变成 8/13/3/2。
    // 结论：强度是"说多少"的量，但第 9 层的系数是按 0/1 标定的，混进去等于重标一遍。
    // 所以这一层继续读 0/1；`intensity` 目前只进 `--json` 与审计（**未接线的欠账，已记账**）。
    const wantNow = want && want.kind === 'speak' ? 1 : 0;
    // r102：`energy` 读**没被对话地板抬起**的那个值（`energyTrue`）。
    // 病根：`step()` 末尾有一条"他正说着话 → 精力不低于 0.35"的地板（r82 线上反馈 #1），
    // 它本来是防"永久低电量"的显示／行为护栏；但第 9 层直接读了地板之后的数，
    // 于是 `capacity ≥ 0.61`，「累」这一维被压平：`brief` 只能靠被骂才到得了（标定卷 r101 实测）。
    // 原典里她**在对话中真的会困**（`canon-response.mjs`：她提到困／睡的行 46 条；
    // e08_009_032 说着话睡着）。所以这一层要读真值，地板只管"她开口时的体力底线"，不管"她累不累"。
    const energy = clamp(numOf(state?.energyTrue ?? state?.energy, 0.72), 0, 1);
    const strain = 1 - energy;
    // `regard`：规格要 `pull`（关系距离），代码里没有 → 用 `trust`（r75 唯一的关系量）。
    // 有账在场时 +0.4：欠着事的人不会"不在意这段"。
    // r88：账"进不进卡片"（记忆的出口）与"她欠着事"（事实）是两件事，这里要的是后者。
    // 原来读 `commitmentsLine`——那等于"账没写进卡片时她就不在意这段关系了"，说不通。
    const hasDebt = state?.debtOpen === true
        || String(state?.commitmentsLine ?? '').trim().length > 0;
    const regard = clamp(0.6 * clamp(numOf(state?.trust, 0.6), 0, 1) + 0.4 * (hasDebt ? 1 : 0), 0, 1);
    // `waiting`：他还在等。`--his` 这条路径就是"他刚说了话" → 1；
    // `--idle` 没有新消息 → 她刚说完算 0.5、什么也没说算 0。**由调用方给**，不在这里猜路径。
    const waiting = clamp(numOf(opts.waiting, 0), 0, 1);
    // `avoidance`：规格要 `imposition`，代码里没有 → 用两个**真实存在**的厌恶来源：
    //   ① 被骂／被冤枉（`insultedNow`／`wrongClaimNow`，§3.5 那一格）→ 1.0
    //   ② 同一件事被问到第 5 遍起（`repeatCount`，适应那一格）→ 0.7
    // 这一条**不新造情绪标签**：两个来源都是已有的判定结果。
    const avoid = (state?.insultedNow === true || state?.wrongClaimNow === true)
        ? 1
        : (numOf(state?.repeatCount, 0) >= 5 ? 0.7 : 0);
    // `explainedRecently`：规格写"最近 6 轮"，但 `lastSaid` 只留 3 条 → 用 3 轮。
    // 要 6 轮得先扩 `lastSaid`，那会牵动 followUp／指纹／去重三处，本轮不动。
    const explainedRecently = (state?.lastSaid ?? []).some((one) => EXPLAINED_RE.test(String(one ?? '')));
    // r102：`addressed`——他这句话是**在跟她说**（`footing.who === 'me'`，r82 的 P4，每轮算一次）。
    // 它和 `want` 是两件事：`want` 是"我自己有话说"，`addressed` 是"他在等我回"。
    // 原典里她几乎不"整段不出现"（canon-response.mjs：3 行窗口内接住 33%／被问到时 27% 只是应一声），
    // 所以"不回应"要有代价。`MIND_COST_ACK=0` 回到 r101 行为（只乘 `want`）。
    const addressed = ACK_COST_OFF ? 0 : (state?.footing?.who === 'me' ? 1 : 0);
    return {
        want: wantNow,
        addressed,
        energy,
        strain,
        regard,
        waiting,
        avoidance: avoid,
        explainedRecently,
    };
}

/**
 * 算这一轮的 `net`。返回 `{ pick, net, gains, costs, inputs }`（pick ∈ full／brief／state／silent）。
 * 关着（`MIND_COST=0`）时返回 `null`——调用方据此**不写 state.cost**，与 r82 逐字一致。
 */
export function costOf(state, opts = {}) {
    if (COST_LAYER_OFF) {
        return null;
    }
    const inputs = costInputsOf(state, opts);
    const one = pickOf(inputs);
    return { pick: one.pick, net: one.net, gains: one.gains, costs: one.costs, inputs };
}



// 她手上"还没做完"的那件具体的事（承诺账欠着一条，或与当轮相关的讲到一半的故事）
function openItemsOf(state) {
    const owed = String(state.commitmentsLine ?? '').replace(/^-\s*我(手上还悬着|欠着)：/u, '').trim();
    if (owed) {
        return owed.split('（')[0].slice(0, 20);
    }
    return relatedStoryOf(state);
}

// ── r82（2026-10-26 根因①）：**共同实词要过停用词/低信息过滤** ─────────────────────────
// 病根：`sharesTopic(...,2)` 把「我／还／回去」这种低信息词也算共同实词，于是任何句子都能
// 跟那句碎片凑够 2 个——88 条 speak 里 84 条的 about 是同一个值，那不是"她有事"，是一个碎片粘住了所有消息。
// 口径：去掉停用字之后，**长度 ≥2 且互不相同**的共同实词要 ≥ `need` 个。
// 2026-10-26 扩一档（Lead 裁决）：回 去 用 完 来 说 事 个 点 上 下 里 外 好 真 太 就 也按低信息处理，
// 否则「用完我还回去」这种碎片里的 用完／回去 照样能凑够 2 个共同实词。
const WEAK_WORDS = /[我你他她它的了是还就都也在有这那一不没很太吗呢吧啊呀哦嗯么什怎回去用完来说事个点上 下里外好真太就]/gu;
function strongGrams(text) {
    return new Set(
        contentGrams(String(text ?? '')).filter((one) => {
            const trimmed = String(one).replace(WEAK_WORDS, '');
            return trimmed.length >= 2 && trimmed === String(one);
        }),
    );
}
function strongShared(left, right) {
    const a = strongGrams(left);
    const b = strongGrams(right);
    let hit = 0;
    for (const one of a) {
        if (b.has(one)) {
            hit += 1;
        }
    }
    return hit;
}
// 与这一轮话题相关、且她讲过的故事/旧事（用来给「我想要」带内容；没有就返回空）
function relatedStoryOf(state) {
    const inbound = String(state.lastInboundText ?? '');
    // ── 根因②（2026-10-26 裁决）：**`sharesTopic` 要 ≥2 个共同实词 + 他这句必须是真人话** ──────
    // 病根：只要求 1 个共同实词时，`踢我`／`傻呗` 这种短句也能碰上一个词，于是任何消息都能"相关"。
    // 另外，非人话（CQ 码／命令／转发记录／纯 QQ 标记）根本不该参与相关性判断。
    if (!isHumanText(inbound)) {
        return '';
    }
    // S3b：只有**只讲过一次**的事才配"具体"；讲过两遍以上的由调用方回到 `应一声就好`。
    // 根因①：用 `strongShared`（停用字过滤后 ≥2 个**互不相同**的共同实词），不再用 `sharesTopic(...,2)`。
    // 根因②**冷却**：上一轮刚拿它当过 `about` 的那件事，3 轮之内不再算 open item
    //（否则一条 story 永不消费，连着五轮都能当"没说完的事"）。轮次由 `wantOf` 写进 `state.wantAboutAt`。
    const cooled = String(state.lastWantAbout ?? '');
    // ── r83：冷却必须**从下一轮起**才算，不许在"同一轮"里生效 ─────────────────────
    // 病根：`wantOf` 会把它选中的 `about` 写进冷却，而一轮里 `wantOf` 会被调用不止一次
    // （`renderCard` 的「我想要」、`--json` 的 `state.want`、r83 的 cost 各一次）。
    // 于是**第一次**返回 speak，第二次就因为自己刚写的冷却退回 listen —— 同一轮两个答案。
    // 实测（tmp/probe-r82-longsession.mjs 64 轮）：`state.want` 报 listen 64/64，
    // 却出现 3 轮 `pick=full`（want=1 才会选 full）——两个字段在同一个 JSON 里互相矛盾。
    // 现在：`wantAboutAt` 是本轮写的（差 0）→ 不算冷却；差 1–3 轮才算。跨轮语义一个字没改。
    const cooledGap = numOf(state.rounds, 0) - numOf(state.wantAboutAt, -99);
    const cooledFresh = cooled && cooledGap >= 1 && cooledGap <= 3;
    const labelOf = (one) => String(one?.gist ?? '').trim() || String(one?.text ?? '').trim().slice(0, 12);
    const hit = trustedStoriesOf(state).find((one) => numOf(one?.used, 0) === 1
        && isHumanText(String(one?.text ?? ''))
        && strongShared(String(one?.text ?? ''), inbound) >= 2
        && !(cooledFresh && labelOf(one) === cooled));
    if (hit) {
        // 裁决三：`about` 不许为空——先 `gist`，再 `text` 前 12 字。
        // 两者都空的 story 本就不该登记，那种情况由 `rememberStory` 在登记处挡掉。
        return labelOf(hit) || '那件事';
    }
    // S4c：**不再从 autobio 兜底**——旧事那一段由「我已知的事」那一行负责，
    // 这里再取一次就会让同一件事在一张卡里出现两次（互斥）。
    return '';
}

// ── r82（P2 第一步 / belief → 评估的**唯一**出口）────────────────────────────
// 为什么选 `feelingLine` 而不是 `motiveLine`：「心里」本来就是评估结果，而 `belief` 是"他觉得我怎样"，
// 天然属于这一行；`motiveLine` 已经被 受伤→烦躁→推理→防御→评估→共同基础→agenda 七层占满，
// 再塞一个 belief 会变成第二套排序（两处写同一件事 = 折中值）。
// 为什么只用"选择"不用"新句子"：r78 的教训——往卡片上加句式，AI 味立刻掉。
// 三句 base 都是**原来就有的**（松／闷／平），belief 只决定挑哪一句；数值一个都不进卡片。
// 开关 `MIND_BELIEF=0` 时这一层完全不参与（OFF 臂 = 当前 HEAD）。
const BELIEF_LAYER_OFF = process.env.MIND_BELIEF === '0';
// "他这么想我"的门槛：**0.55**
// 出处（§13.10 的纪律：来源 + 日期 + 哪份卷子）：2026-10-26 用 `tools/persona-lab/cases-mirror.json`
// 标定——`mirror-tired-b` 四轮的算术上限是 0.10 + 0.12×4 = 0.58，**0.60 永远到不了**（那一支是死代码）；
// 0.55 时 tired 第 4 轮过线，而 dumb／cute 仍然第 2 轮就过（0.60／0.70），
// 对照段三格 0.20／0.30／0.10 与反例段 0.30 都 < 0.55（不误触发）。
// **换卷子（新刺激、新轮数）要重标**，别在别处二次引用。
const BELIEF_MIRROR_MIN = 0.55;

// 他此刻把她当成什么。优先级：**他腻了 > 被当成笨蛋 > 被当成可爱**
// （越接近"关系会断"的信号越优先；与 woundKind 的优先级同一个思路：先处理最容易走冷的那一格）
// r82（P2 后半）：读的是 `currentMirrorOf()` 选出来的**在场那个主体**，不是扁平三键——
// 但它只读结果、不选人（选人只在那一个函数里）。
function beliefMirrorOf(state) {
    // r107（2026-11-01）：**退役**。三个键（他觉得我笨/可爱/烦）在原典里 0 条直接证据，
    // 而且它们会把"他对我的评价"写成卡片上的判断句——正是「别抬我了」那类台词味的来源。
    // 现在恒为 ''：feelingLine 不再读它，「心里」只由 appraisal／心情决定（只写状态，不写判断）。
    // 字段与迁移保留（删字段会让 migrate 丢键），但不再写入、不再读取。
    void state;
    return '';
}

// 三个分支各自落到**已有**的那一句 base 上（不新增文案、不拼接）
const BELIEF_FEELING = {
    tired: '平平的，没什么起伏',
    dim: '有点闷，说不上来为什么',
    cute: '这会儿心里是松的',
};

function feelingLine(state) {
    // r67：烦躁是**累积量**，直接进这一行的措辞（不是动作、不是指令）
    const repeat = state.repeatCount ?? 0;
    if (repeat >= 3) {
        // v10：烦躁是**态度**，不报数（数字只进状态）
        const stress = repeat >= 7
            ? '烦了，不太想接'
            : repeat >= 5
                ? '有点不耐烦了，不想再解释'
                : '又是这件事，我提不起劲';
        return stress;
    }
    const defense = defenseOf(state);
    const appraise = appraisalOf(state);
    const mirror = beliefMirrorOf(state);
    // 他要是已经把她当笨蛋，"被抬这么高会摔"那套就不成立（那句夸在他嘴里是别的东西），
    // 所以 `dim` 这一支不吃「安全被威胁」这个评估——这不是新规则，是同一个量在两个出口上的自洽。
    const blocked = mirror === 'dim' && appraise?.key === '被夸了';
    const base = (appraise && !blocked)
        ? appraise.feeling
        : (BELIEF_FEELING[mirror] || (state.moodV > 0.2 ? '这会儿心里是松的' : state.moodV < -0.1 ? '有点闷，说不上来为什么' : '平平的，没什么起伏'));
    if (!defense) {
        return base;
    }
    const tail = {
        退缩: '——困得不太想解释',
        幽默: '——这话有点好笑，也有点无奈',
        攻击性升华: '——我不打算把它当玩笑',
        接受: '——不懂就是不懂，没什么好藏的',
        '理智化／转移': '——讲成机制我就能说下去',
    }[defense.key] ?? '';
    return `${base}${tail}`;
}

function bodyLine(state) {
    const energy = round2(state.energy);
    const word = ENERGY_WORD(state.energy);
    // v10.2：她有身体，但**卡片这一行只报状态**（精力多少、困不困）——
    // 动作属于台词那一层（"我起来了"是话，不是旁白），不写在这里。
    // 只写状态本身：精力多少、困不困。
    return `精力 ${energy}（${word}）`;
}

// 内容点：她想说的**那一件具体的事**（不是句式、不是落法）
// ── r70（v4）：沉默要有动机 ────────────────────────────────────────────────
// 主人的话：「沉默没有从她的人格模型出发，没有考虑'她为什么沉默'，是困了？还是防御机制发动？」
// 所以 睡着／半句／短答／回避 不再由"精力阈值"单独决定——**必须有理由**，写进卡片。
// 写不出理由就不该沉默（返回空串 = 卡片不写这一行 = 这一轮她照常说话）。
// r82（P4 模型侧）：导出给自测——"位置"这一层的行为后果就在这一支里，
// 通过卡片验证要先凑出一个"安静动作"，直接调这个函数才测得到四种位置的差别。
export function silenceMotive(state, action) {
    const quiet = ['睡着', '半句', '短答', '回避'];
    if (!quiet.includes(action)) {
        return '';
    }
    // ① 困
    // r82（线上反馈 #2）：这一支原来在 <ENERGY_FADE(0.35) 就说「撑不住了」，
    // 于是 0.31–0.33 的她会连着三轮被判成"说不完"。现在只有 <ENERGY_TIRED(0.22) 才叫"说不完"；
    // [0.22, 0.35) 那一档她只是话短（短答），不该挂这条理由。
    if ((state.energy ?? 1) < ENERGY_TIRED) {
        return '困——撑不住了（不是不想说，是说不完）';
    }
    // ② 防御·退缩：心结被碰（我只有游戏／你是不是人）
    if ((state.wound ?? 0) >= 0.6) {
        return '他这句碰到我了——缩一下，不想往下说';
    }
    // ③ 防御·沉默：问到不想答的、气氛压着
    if (state.lastInboundAsked && (state.askStreak ?? 0) >= 1) {
        return '他问的这个我不想答（不是没听见，是不想说）';
    }
    // ④ 没兴趣：跟他无关的闲话、别人的事
    // r82（P4 模型侧）：位置也进这一支——**"他在跟别人说话"和"他在说别人的事"是同一种处境**
    // （我不在这个局里），所以用同一句理由，不新加一行文案。
    // `room`（对全屋说）**故意不算**：那是在跟屋里所有人说话，里面也包括她，无视她才是错的。
    const aboutOther = perspectiveOf(state.lastInboundText).target === '在说别人'
        || (!FOOTING_LAYER_OFF && state.footing?.who === 'other');
    if (aboutOther) {
        return '他在说别人的事，我听着就好';
    }
    // ⑤ 在等他：他句子断着
    if (!state.lastInboundText) {
        return '';
    }
    // 写不出理由 → 不许沉默
    return '';
}

// ── v12（r80）：共同基础变成**她的评估结果**，不是给她的写法 ──────────────
// 依据（五条，见 docs/persona-psyche-model-v2.md §十一、§十二）：
//   Sperber & Wilson 的关联（话语要么加新信息、要么省他的加工）、
//   Clark 的共同基础（只有"对方没有的"才值得加进去）、
//   Apperly & Butterfill 的隐性心智化（读心的结果进行为、不进言语）、
//   Vygotsky／Fernyhough 的内部言语（点评是压缩的，本来就不说出去）、
//   Sacks 的"没有 news 就不 tell"。
//
// 上一版把这条写成卡片上的一行（"这一句给他的新东西：…"）——模型就每句都去凑一条 news。
// 现在它只回答一个问题：**这一轮她有没有可加的？** 没有就话少（应一声、或者干脆沉默），
// 有就照她自己的意思说；**具体怎么说，卡片一个字都不管**。
function newsOf(state) {
    if (!String(state.lastInboundText ?? '').trim()) {
        return { news: 0, why: '他这句没什么内容，我没什么可回的' };
    }
    // 同一件事问到第三遍起：他早就知道答案了，再说一遍加不进东西（烦躁另有梯度管）
    if ((state.repeatCount ?? 0) >= 3) {
        return { news: 0, why: '这件事他已经问过好几遍了，我再答一遍也是同一句' };
    }
    // 他只是在应声（嗯／哦），她也没有非说不可的事 → 加不进去
    // r105：先剥掉信封（`昵称：`／`昵称<QQ号>：`）再数字数——原来把信封算成内容，
    // 于是「日向：嗯。」被当成"他讲了一件事"，一路落到 listen。
    const body = String(state.lastInboundText ?? '').replace(/^\s*[^\s：:<>]{1,16}(?:<\d+>)?\s*[：:]\s*/u, '');
    if (bareLen(body) <= 1) {
        return { news: 0, why: '他只是应了一声，我不必接' };
    }
    // r82（R2）：这一段的同一个故事她已经讲过两遍了 → 再加一遍也还是那几句。
    // 这就是"用过两次就换素材"的形态：不是禁令，是她自己没料可加了（走已验证的 news=0 分支）。
    const story = storyHit(state);
    if (story && (story.used ?? 0) >= REPEAT_ENOUGH) {
        return { news: 0, why: '这件事这一会儿已经讲过了，再讲还是那几句' };
    }
    return { news: 1, why: '' };
}

// ── v13（r81）：她得**想一下**的时候（逻辑推理层）─────────────────────────
// 主人说她"逻辑还有些欠缺"。诊断下来最要紧的一点不是"她笨"，而是：
// 她常常**没经过 System 2 就答了**——直觉（System 1）拿熟悉的类比顶上，答案先出口，后面再改口。
// 依据：
//   · 双过程理论（Kahneman 2011；Evans & Stanovich 2013）：System 1 快而自动，System 2 慢、占工作记忆；
//     推理题的错多半是 System 1 抢答。
//   · 认知反射（Frederick 2005 的 CRT）：能不能压住"听起来对"的直觉答案，是抑制控制的事（Miyake & Friedman 2000）。
//   · 元认知监测（Koriat 1993；Nelson & Narens 1990）：人对"我知道多少"有个监控信号，
//     信号弱还硬答就是自信错；信号准了，才会说"我猜"。
//   · 前额叶成熟晚于奖赏系统（Steinberg 2008 双系统模型；Casey 2015）：
//     十七岁的人先给答案、再想，是发育阶段，不是缺陷——补的办法是外部支架（Vygotsky 的脚手架），
//     不是"你要更谨慎"这种说教。
//
// 所以这一层只做一件事：**先认出这题需要想一下，再把她手上到底有什么摆出来**。
// 不写"该怎么答"，只写"这题要的是数／条件／原因／人"和"我手上有没有那个东西"。
// v13（r81）：A/B 开关。`MIND_REASONING=0` 时推理层完全不参与（不出那几行、不改动机），
// 用来跑对照：同一份卷子、同一版代码，只差这一层。默认参与。
const REASONING_LAYER_OFF = process.env.MIND_REASONING === '0';
// r98：只关第三类（否定前件那一格），其余两类照旧——跑配对时用
const COND_OFF = process.env.MIND_COND === '0';
// 有没有出现真正的数（这条不是 /g 正则，可以安全 .test）
const HAS_NUMBER = /\d+(?:\.\d+)?|[二三四五六七八九十百千两]+(?:万|亿|个|块|元|度|天|年|月)/u;
const REASON_KIND = [
    {
        // r98（2026-10-30）：盲测里**唯一稳定复现的逻辑硬错**——否定前件。
        // r97 探针卷 D1，两位评委独立判 L2：「能读。存档没坏就是能读」——他把「坏了→读不了」当条件，
        // 她反推成「没坏→就能读」。历史上这一格 9/9 全错。
        // 这一格**不给答法**，只把他给的两截摆出来（哪句是"如果"，哪句是他看到的现状），
        // 剩下那一步是她自己的（SOUL §3：先把"哪个是确定的"摆出来，再从那样往下说）。
        key: '一个条件',
        when: (s) => {
            if (COND_OFF) {
                return false;
            }
            const text = String(s.lastInboundText ?? '');
            if (!text.trim()) {
                return false;
            }
            const hasCondition = /如果|要是|假如|万一|只要|只有|除非/u.test(text)
                || /[^。！？，,]{1,10}(?:就|才)(?:不|没|不能|打不开|进不去|开不了|买不了|给不了)/u.test(text);
            if (!hasCondition) {
                return false;
            }
            // 还得是在问"结论成不成立"，否则只是叙述（"他说要是下雨就不去了"）
            return /[？?]|吗|会不会|能不能|行不行|读得|进得|开不开|买不买|给不给|得了吧/u.test(text);
        },
        motive: '先分清他给的是哪一截，再往下说',
    },
    {
        // 群里实测出来唯一有量的一类错：**把累计当当天**（「一天就一亿多」，三句都出自这一处）。
        // 她的算术没问题（读图读出 443.34、倒计时、定时都对），缺的是"先分清这个数说的是哪段时间"。
        key: '一个口径',
        when: (s) => {
            const text = String(s.lastInboundText ?? '');
            if (!HAS_NUMBER.test(text)) {
                return false;
            }
            if (/涨了|差多少|比.{0,4}(多|少)|多了|少了|加起来|一共|合计|多少/u.test(text)) {
                return false;   // 这是要算的，交给「一个数」
            }
            return /累积|累计|总共|一共|今天|一天|一个月|这个月|上次|目前|已经/u.test(text);
        },
        motive: '先弄清这个数说的是一次的、还是一段时间的，再往下说',
    },
    {
        key: '一个数',
        re: /多少|几个|几天|几号|哪个月|哪天|哪一年|一个月|一天|一年|一周|差多少|涨了|一共|合计|加起来|算一下|算算|多少度|百分之|%|\d+(?:\.\d+)?\s*(?:G|GB|元|块|度|小时|分钟|个|张|次|关|combo)/iu,
        motive: '把这个数算清楚再说，算不出来的那一步就不算',
    },
];
// ⚠️ r81 A/B 判决（n=3，各 24–26 道可判定题）：**宽版推理层没有可测增益，反而略降**
//   OFF 可判定 87%（85–88）／全部规则题 80%
//   ON  可判定 85%（79–92）／全部规则题 75%
// 差异集中在跨轮一致性与因果解释——那两类本来就不是她的短板（群审计：倒置 0、矛盾 0）。
// 所以只留审计支持的两类（口径／问数但没给数），条件·原因·指代三行已撤：
// 它们没修好任何真实错误，只往卡片里加噪声。判决书见 docs/persona-psyche-model-v2.md §13.5。
// r98（2026-10-30）**撤回其中一条**：r97 盲测里"否定前件"不再只是观感——探针卷 D1 两位评委
// 独立判 L2，历史上 9/9 全错，而这一格当年被撤的理由是"没修好任何真实错误"。现在有真实错误了，
// 所以只把**条件**这一类加回来（原因/指代仍然不加），并且只加"他给了两截"这一行事实。
// 想逐字回到 r97：`MIND_COND=0`。
// 数：阿拉伯数字（含小数）＋中文量词里的"两个/三天/五块"这类真实数量。
// 光"一个月/一天"这种多是量词不是数量（"那是一个月还是一天"问的是单位），所以不数单字"一"。
const NUMBER_TOKEN = /\d+(?:\.\d+)?|[二三四五六七八九十百千两]+(?:万|亿|个|块|元|度|天|年|月|小时|分钟|张|次|关|管|局|人|名|条|件|份|瓶|杯|碗|页|行|层|首|把|台|部)/gu;

function reasoningKind(state) {
    const text = String(state.lastInboundText ?? '');
    if (!text.trim()) {
        return null;
    }
    // 两种条目：`when(state)` 的（要同时看数）和 `re` 的（只看字面）
    return REASON_KIND.find((one) => {
        try {
            if (typeof one.when === 'function') {
                return one.when(state) === true;
            }
            return one.re ? one.re.test(text) : false;
        }
        catch {
            return false;
        }
    }) ?? null;
}

// 她手上有没有能核对的（这决定她说"我记得"还是"我猜"）
function reasoningHold(state, kind) {
    if (!kind) {
        return '';
    }
    const text = String(state.lastInboundText ?? '');
    const facts = semanticOf(text);
    if (facts.length) {
        return `这一格我手上有真事（${facts[0].key}），照那个说`;
    }
    if (kind.key === '一个数') {
        const numbers = (text.match(NUMBER_TOKEN) ?? []).filter(Boolean);
        if (!numbers.length) {
            return '他没给我数，我手上也没有这个数';
        }
        const big = /亿|万|百分|%/u.test(text);
        const numbered = numbers.join('、');
        if (numbers.length >= 2) {
            // 单位对齐是工作记忆的事（Baddeley 的情景缓冲）：万／亿不带单位直接相减，
            // 群里那次「一管 25 万、四管」就是栽在这里。只报事实，不给算法。
            const unitFact = big ? '；这几个数带万／亿，单位不一样' : '';
            return `他给了 ${numbers.length} 个数（${numbered}），得自己算${unitFact}`;
        }
        return `他给的数是 ${numbers[0]}${big ? '；这个数带万／亿' : ''}`;
    }
    if (kind.key === '一个条件') {
        // 只摆事实：他给的那截"如果…就…"，和那句"现在是什么样"。两截分开写，
        // 是因为她把两截当成一件事读（"没坏" → "能读"）——这是条件的方向问题，不是算术问题。
        const sentences = String(text).replace(/^\s*[^\s：:]{1,10}[：:]/u, '').split(/[。！？]/u).map((one) => one.trim()).filter(Boolean);
        const condSent = sentences.find((one) => /如果|要是|假如|万一|只要|只有|除非/u.test(one)) ?? '';
        const nowSent = sentences.find((one) => one !== condSent && /没|不|已经|刚|好好的/u.test(one)) ?? '';
        const cut = (one) => (one.length > 26 ? `${one.slice(0, 26)}…` : one);
        const parts = [];
        if (condSent) {
            parts.push(`「${cut(condSent)}」`);
        }
        if (nowSent) {
            parts.push(`「${cut(nowSent)}」`);
        }
        return parts.length ? `他给了两截：${parts.join('；')}` : '他给的是条件，另一截我手上没有';
    }
    if (state.offLimitNow) {
        const words = (state.offLimitWords ?? []).filter(Boolean);
        return `${words.length ? words.join('、') : '这个'}我不懂；里面要是有能算的数，那一步我还是能算`;
    }
    if (kind.key === '一个人') {
        const speakers = text.match(/([^：\s]{1,8})<\d+>/gu) ?? [];
        if (speakers.length >= 2) {
            return `${speakers.map((one) => one.replace(/<\d+>/u, '')).join('、')} 两个人的话混在一批里，「他」我还没归到谁头上`;
        }
    }
    return '';
}

// 她上一句自己说过的数（工作记忆里的"别自我推翻"）
function lastNumberClaim(state) {
    const list = [...(state.lastSaid ?? [])].reverse();
    for (const one of list) {
        const text = String(one ?? '');
        if (/\d/u.test(text)) {
            return text.slice(0, 30);
        }
    }
    return '';
}

function reasoningLines(state) {
    // A/B 开关：MIND_REASONING=0 时这一层整个不参与（跑对照用，默认参与）
    if (REASONING_LAYER_OFF) {
        return [];
    }
    const kind = reasoningKind(state);
    if (!kind) {
        return [];
    }
    const lines = [`- 他这句要的是：${kind.key}`];
    const hold = reasoningHold(state, kind);
    if (hold) {
        lines.push(`- 我手上的：${hold}`);
    }
    const claim = lastNumberClaim(state);
    if (claim && /一个数/u.test(kind.key)) {
        lines.push(`- 我上一句说过的数：${claim}`);
    }
    return lines;
}

// 她的世界：只放"事实"，不放"该怎么说"
function worldAnchor(state) {
    if (state.offLimitNow) {
        return `「${state.offLimitNow}」这一格她是真的不懂（原典里她连"挤奶是什么"都问过），不许替她给答案`;
    }
    if (state.strongSuitNow) {
        return '游戏上她是超高校级，除恋爱游戏外全类型精通（她的强项，不是"打得少"）';
    }
    if (state.strongFlipRecent) {
        return '她刚才把强项说成不擅长了——那不是她';
    }
    return '';
}

// r53：新卷子（cases-canon / cases-canon-trial）的 turns 是**对象** `{message, draft}`，
// 老卷子（cases-real）是字符串。以前直接 String(message) 会得到 "[object Object]"，
// 于是同一句垃圾喂 88 轮——重复判定、知识边界全都白做。这里统一取文本。
function turnText(turn) {
    if (typeof turn === 'string') {
        return turn;
    }
    if (turn && typeof turn === 'object') {
        return String(turn.message ?? turn.text ?? turn.incoming ?? '');
    }
    return String(turn ?? '');
}

// ── v12.1（r81）：空转时刻（没有他在说话）────────────────────────────────
// 这张卡的用途只有一个：她主动开口之前，先知道自己此刻是什么样。
// 心理学依据见 docs/persona-proactive.md（依恋的分离驱动 / 未完成事务 / 资本化分享）。
const IDLE_MOTIVE = (state) => {
    if (state.pendingInvite?.place) {
        return '说好要去的那个地方还没去成';
    }
    // 2026-10-26：约定以**承诺账**为准。账上没欠着就不许说"还悬着"——
    // 旧版是从 autobio 里猜有没有"说好了"，那是猜的，不是事实（账本落地后就该退休）。
    if (typeof state.commitmentsLine === 'string' && /欠着|悬着/.test(state.commitmentsLine)) {
        return '说好了的那件事还悬着';
    }
    if ((state.energy ?? 1) < 0.4) {
        return '困了，可我还想再撑一会儿';
    }
    if ((state.moodV ?? 0) > 0.2) {
        return '这会儿心里是松的，想找个人说一句';
    }
    if ((state.moodV ?? 0) < -0.1) {
        return '有点闷，说不清为什么';
    }
    return '一个人待着，想听人说话';
};

// 她这会儿在做的那件事：从场景的 doing 里按"这一刻"取一条（不编造，只从已有的动作里挑）
function idleDoing(state, now) {
    const list = state.scene?.doing ?? [];
    if (!list.length) {
        return '';
    }
    const seed = Math.floor(now / (17 * 60 * 1000)) + String(state.scene?.id ?? '').length;
    return list[Math.abs(seed) % list.length];
}

function renderIdleCard(state, now) {
    const at = new Date(now);
    const hh = String(at.getHours()).padStart(2, '0');
    const mm = String(at.getMinutes()).padStart(2, '0');
    const night = at.getHours() >= 22 || at.getHours() < 6;
    const lines = [];
    lines.push('【此刻】（没有他在说话）');
    lines.push(`- 身上：${bodyLine(state)}`);
    lines.push(`- 现在：${hh}:${mm}${night ? '（夜里）' : ''}`);
    const where = sceneLine(state);
    if (where) {
        lines.push(`- 我在哪儿：${where}`);
        // 2026-10-27：她真在玩星露谷（15 分钟内写过 stardew.json）时，「我手上」写那块地；
        // 不 fresh 就完全保持原样（不许出现"我好像在玩游戏"这种空话）。
        const farmLine = stardewWaterLine(state);
        const gameLine = stardewPresenceLine(state, now);
        if (farmLine) {
            lines.push(`- 我手上：${farmLine}`);
        }
        else if (gameLine) {
            lines.push(`- 星露谷那边：${gameLine}`);
        }
        else {
            const doing = idleDoing(state, now);
            if (doing) {
                lines.push(`- 我手上：${doing}`);
            }
        }
    }
    if (state.scene?.why) {
        lines.push(`- 我为什么在这儿：${state.scene.why}`);
    }
    // r84：她上一次想的事（空转卡上这一行最要紧——没有他的话，她自己就是唯一的线索）
    if (state.thoughtLine) {
        lines.push(state.thoughtLine);
    }
    // 上次说到哪儿——这是她的记忆事实，不是给她的写法
    const lastHeSaid = String(state.lastInboundText ?? '').trim();
    const lastSheSaid = [...(state.lastSaid ?? [])].reverse().find((one) => String(one ?? '').trim());
    if (lastHeSaid) {
        lines.push(`- 他上一句：${lastHeSaid.slice(0, 24)}`);
    }
    if (lastSheSaid) {
        lines.push(`- 我上一句：${String(lastSheSaid).slice(0, 24)}`);
    }
    const memory = autobioOf(state);
    if (memory.length) {
        lines.push(`- 我已知的事：${memory.map((one) => one.text).join('；')}`);
    }
    // 2026-10-25：承诺账（空转卡也要知道自己欠着什么）
    if (state.commitmentsLine) {
        lines.push(state.commitmentsLine);
    }
    lines.push(`- 心里：${feelingLine(state)}`);
    lines.push(`- 我想要：${IDLE_MOTIVE(state)}`);
    const anchor = worldAnchor(state);
    if (anchor) {
        lines.push(`- 她的世界：${anchor}`);
    }
    return lines.join('\n');
}

// 空转的一步：只让时间过（精力掉、场景按"待久了"自己动），不推进对话轮次、不动上次说了什么
function stepIdle(state, now) {
    const next = { ...state, belief: { ...state.belief } };
    const sleep = advanceSleep(state, now);
    next.energy = sleep.energy;
    next.need = sleep.need;
    next.sleepPressure = sleep.need;
    next.sleepTarget = sleep.target;
    next.asleep = sleep.asleep;
    next.updatedAt = now;
    next.scene = decideScene(next, '', new Date(now));
    next.sceneSig = sceneSignature(next, new Date(now));
    return next;
}

// ── CLI ────────────────────────────────────────────────────────────────
function parseArgv(argv) {
    const args = {};
    for (let i = 0; i < argv.length; i += 1) {
        const token = argv[i];
        if (token.startsWith('--')) {
            const key = token.slice(2);
            const next = argv[i + 1];
            if (next === undefined || next.startsWith('--')) {
                args[key] = 'true';
            }
            else {
                args[key] = next;
                i += 1;
            }
        }
    }
    return args;
}

// 被别的模块 import 时不许往下跑 CLI（网关要用这里同一份心理学函数，别再抄一份）
// 判断方式：只看脚本文件名，别用 endsWith——`selftest-mind.mjs` 也会被骗成自己。
const invokedDirectly = process.argv[1]
    ? (basename(process.argv[1]) === 'mind.mjs')
    : false;

if (!invokedDirectly) {
    // 被 import：什么都不做，只把上面那些 export 出去
}
else {
    // r107：CLI 体整个搬进 `async function runCli()`，**不再在模块顶层用 await**。
    // 原因（实测事故）：`group-appraisal.js` 用 `file:///…` import 了 mind.mjs；
    // 只要 this module 的图里有顶层 await，网关 `require()` 插件时就报
    // `ERR_REQUIRE_ASYNC_MODULE` → **openclaw-onebot 加载失败、QQ 通道消失**。
    // 现在顶层只剩一次 `runCli()` 调用（不 await），TLA 彻底没有。
    void runCli();
}

async function runCli() {

const args = parseArgv(process.argv.slice(2));
const chat = normalizeChatKey(args.chat ?? 'main');
// r82（R0.3）：会写盘的四条命令先拿锁，拿到再读——这样"读-改-写"整段是独占的。
// `--show` / `--replay` 只读，不拿锁（拿锁会让它们无谓地等 100 ms）。
const WRITE_COMMANDS = ['her', 'his', 'idle', 'reset', 'think', 'think-acted', 'sleep-realign'];
const NEEDS_LOCK = WRITE_COMMANDS.some((key) => args[key] !== undefined);
// 只有 --his 有人在等（它卡着回话），其余三条是后台路径，可以多等一会儿
const LOCK_BUDGET = args.his !== undefined ? LOCK_RETRIES_FAST : LOCK_RETRIES_SLOW;
const LOCK_HELD = NEEDS_LOCK ? acquireLock(LOCK_BUDGET) : false;
WRITE_ALLOWED = !NEEDS_LOCK || LOCK_HELD;
if (NEEDS_LOCK && !LOCK_HELD) {
    // 照实说，但不影响回话：卡片照出，只是这一轮不落盘
    process.stderr.write('[mind] 拿不到 state 锁（别的进程正在写），这一轮只读不写\n');
}
process.on('exit', () => {
    if (LOCK_HELD) {
        releaseLock();
    }
});
const all = loadAll();
const state = getState(all, chat);

if (args.replay) {
    const cases = JSON.parse(readFileSync(args.replay, 'utf8'));
    let current = { ...DEFAULT_STATE, updatedAt: Date.now() };
    const rows = [];
    let clock = Date.now();
    for (const oneCase of cases) {
        for (const rawTurn of oneCase.turns) {
            const message = turnText(rawTurn);
            clock += 150000; // 回放里每轮推进 150 秒（真实聊天大致这个量级）
            const { next, hits } = step(current, message, clock);
            const { action } = renderCard(next, hits);
            next.lastActions = [...(current.lastActions ?? []), action].slice(-6);
            next.tareaCount = (current.tareaCount ?? 0) + (action === '讨要' ? 1 : 0);
            // r30：记下配额要用的轮次；「起手」另外记一段里的次数和上次的位置
            markActionRound(next, action);
            // r31/§5：自曝位置每 20 轮 ≤1 次
            if (next.selfExposeAllowed) {
                markActionRound(next, '自曝位置');
            }
            // r49：她睡着了 = 小睡了一下。醒来回一点精力（原典 e08_009_032：睡着→说梦话→醒来认一句），
            // 否则长会话会一路睡到底、睡着占比飙到 38%。
            if (action === '睡着') {
                next.energy = clamp((next.energy ?? 0) + ENERGY_NAP_AMOUNT, 0.05, ENERGY_CAP);
            }
            if (action === '起手') {
                next.lastOpenRound = next.rounds;
                next.openCount = (current.openCount ?? 0) + 1;
            }
            if (action === '一起') {
                next.lastCoopRound = next.rounds;
            }
            rows.push({ case: oneCase.id, message: String(message).slice(0, 16), energy: round2(next.energy), moodV: round2(next.moodV), moodA: round2(next.moodA), agenda: next.agenda, action, layer: next.interestLayer, wound: next.wound, hits: hits.join(','), selfExpose: next.selfExposeAllowed === true });
            current = next;
        }
    }
    // r31：兴趣层加在最后一列（列顺序不动），用来对原典的八层权重表
    console.log('轮次 | 他说 | 精力 | 心情(价/唤) | 想干什么 | 动作 | 触发 | 兴趣层');
    for (const [i, row] of rows.entries()) {
        console.log(`${String(i + 1).padStart(2)} | ${row.message} | ${row.energy} | ${row.moodV}/${row.moodA} | ${row.agenda} | ${row.action} | ${row.hits || '-'} | ${row.layer}`);
    }
    const agendas = new Set(rows.map((one) => one.agenda));
    const actions = new Set(rows.map((one) => one.action));
    console.log(`\n状态多样性：agenda ${agendas.size} 种 / 动作 ${actions.size} 种 / ${rows.length} 轮`);
    // r49：精力曲线（每 10 轮取一个点）——长会话会不会掉到地板，一眼看数
    const sampled = rows.filter((one, index) => (index + 1) % 10 === 0).map((one) => one.energy);
    const tail = rows.length > 0 ? rows[rows.length - 1].energy : 0;
    console.log(`精力曲线（每 10 轮）：${sampled.join(' → ')}${sampled.length ? ' → ' : ''}末轮 ${tail}`);
    const fading = rows.filter((one) => one.energy < ENERGY_FADE).length;
    console.log(`低精力段（<${ENERGY_FADE}）：${fading} 轮（${Math.round((fading / rows.length) * 1000) / 10}%）`);
    // r30：每个动作出现几次（主动追求 / 防御机制是不是真被用上了，一眼看数）
    const counts = rows.reduce((acc, one) => {
        acc[one.action] = (acc[one.action] ?? 0) + 1;
        return acc;
    }, {});
    const ordered = Object.entries(counts).sort((a, b) => b[1] - a[1]);
    console.log(`动作分布：${ordered.map(([key, n]) => `${key} ${n}`).join('　/　')}`);
    const pct = (n) => Math.round((n / rows.length) * 1000) / 10;
    for (const key of ['起手', '一起', '跑题拆结构', '答偏']) {
        console.log(`  ${key.padEnd(6, '　')} ${String(counts[key] ?? 0).padStart(2)} 次　${pct(counts[key] ?? 0)}%`);
    }
    const selfExpose = rows.filter((one) => one.selfExpose).length;
    console.log(`  自曝位置门 ${selfExpose} 轮放开`);
    // r31：兴趣层分布（和原典权重表对照用；权重表出处见 LAYER_WEIGHTS_SOURCE）
    const layerCounts = rows.reduce((acc, one) => {
        acc[one.layer] = (acc[one.layer] ?? 0) + 1;
        return acc;
    }, {});
    const layerLine = INTEREST_LAYERS
        .map((one) => `${one.key} ${layerCounts[one.key] ?? 0}（${pct(layerCounts[one.key] ?? 0)}%）`)
        .join('　/　');
    console.log(`兴趣层分布：${layerLine}`);
    console.log(`  权重来源：${LAYER_WEIGHTS_SOURCE}`);
}
else if (args.reset) {
    delete all[chat];
    saveAll(all);
    console.log('reset ' + chat);
}
else if (args['sleep-realign']) {
    const parsedNow = Number(args.now);
    const nowMs = args.now !== undefined && Number.isFinite(parsedNow) ? parsedNow : Date.now();
    const need = estimateNeedAt(new Date(nowMs));
    const target = sleepTargetAt(nowMs, need);
    state.need = need;
    state.energy = target;
    state.sleepPressure = need;
    state.sleepTarget = target;
    state.sleepSchema = 1;
    state.updatedAt = nowMs;
    all[chat] = state;
    saveAll(all);
    console.log(JSON.stringify({ ok: true, chat, energy: round2(target), need: round2(need) }));
}
else if (args.her) {
    const said = String(args.her);
    state.lastEndedQuestion = /[？?]\s*$/.test(said.trim());
    state.questionStreak = state.lastEndedQuestion ? (state.questionStreak ?? 0) + 1 : 0;
    state.lastSaid = [...state.lastSaid, said].slice(-3);
    state.lastActions = [...state.lastActions, pickAction(state)].slice(-6);
    // r50：记下她这一句挂没挂游戏词（SOUL"一段里至多两处"的执法依据，卡片下一轮会用到）
    if (GAME_WORDS.test(said)) {
        state.gameWordRounds = [...(state.gameWordRounds ?? []), state.rounds ?? 0].slice(-6);
    }
    // r50：说了"不知道／没弄过" → 记一次不懂配额
    if (/不知道|没弄过|没听过|不懂|没试过/u.test(said)) {
        const map = { ...(state.actionRounds ?? {}) };
        map['不懂'] = [...(map['不懂'] ?? []), state.rounds ?? 0].slice(-8);
        state.actionRounds = map;
    }
    // r50：说了"约下次／等你"这类 → 记一次依恋配额
    if (/说好了|等我|你叫我|再聊|别走|我等你|下次再|下次约/u.test(said)) {
        const map = { ...(state.actionRounds ?? {}) };
        map['依恋'] = [...(map['依恋'] ?? []), state.rounds ?? 0].slice(-8);
        state.actionRounds = map;
    }
    // r53 ④：她自己把强项说成"不擅长/手一抖就乱"→ 记下来，接下来两轮卡片要纠正
    // （r52 的 R23「节奏类的我手一抖就乱」vs R25「格斗、解谜、音游都行」）
    const saidStrong = STRONG_SUIT.test(said);
    const saidDenial = /不擅长|不太行|打得少|手一抖|不太会|很少玩|没怎么玩/u.test(said);
    if (saidStrong && saidDenial) {
        state.strongFlipAt = state.rounds ?? 0;
    }
    if (saidStrong && !saidDenial) {
        state.strongFlipAt = -99;   // 说对了就清掉
    }
    // r66：她的自传记忆（只记"我"的事实；≤5 条，20 轮没被引用就丢）
    rememberHer(state, said);
    // v12：这一句是不是又说了一遍记得的那件事 → 记一次"用过了"
    markAutobioUsed(state, said);
    // r99：这一轮卡片挂的是哪条经历／哪条尺子 → 记一次"用过了"（同一段经历挂 2 次就不再挂）
    if (!CANON_OFF) {
        noteCanonShown(state, canonTagOf(state));
    }
    // r82（R2）：这一段"她讲过什么"的工作记忆（≤4 条、窗口 6 轮）——和 autobio 同一处登记
    rememberStory(state, said);
    // 2026-10-25：承诺账——她答应过的事记成可核对、会到期的账（下一步才是让它影响动机）
    const promised = recordPromise({ root: ROOT_DIR, peer: chat, chat, text: said, now: Date.now() });
    if (promised) {
        const due = promised.dueAt ? new Date(promised.dueAt).toISOString().slice(0, 10) : '没设到期';
        console.log(`记账：${promised.what}（${due}）`);
    }
    // r49（b）：她说"眯了一会儿/睡着了"之后恢复精力（上限 0.85）
    if (NAP_MARK.test(said)) {
        state.energy = clamp((state.energy ?? 0) + ENERGY_NAP_AMOUNT, 0.05, ENERGY_CAP);
    }
    // r82（C / 续话）：算这一次"还有话说吗"的材料（只读；真的发出去之后由 `--followup-sent` 记账）
    state.followUp = followUpLine(state, {
        now: Date.now(),
        openItems: commitmentOpenItems({ root: ROOT_DIR, peer: chat, now: Date.now() }),
    });
    // r82（S3 / socio §4，2026-10-26）：**她那句有没有留可接的东西**（`leavesGap`）+ 说这句的时刻（`lastHerAt`）。
    // socio 自己说这是"从她原典气质读出来的，不是拟合的"，所以第一版取**最保守**的口径：
    //   算留了口（1）：① 以问句收尾（把话头递给他）
    //                   ② 明确邀约／等他（等你／一会儿／叫我／回头聊）
    //                   ③ 递回他手上那件事（你那个／你那件／你先／你来／你试试／你呢）
    //   **不算**（0）：给方法／分工（你先按这个／分两步／第三步／照这个）——judge 已把这条写死为不算，
    //   理由：那正好是助教腔，把话头收成"照我说的做"，不是递回给他。
    // 反例三条（由 `selftest-social` 钉着）：「你先按这个思路分两步」「第一步先照着这个来」= 0；
    // 「你呢？」「一会儿一起？」= 1。
    const gapYes = /[？?]\s*$|[等叫回](?:你|会儿)|一会儿|回头聊|你(?:呢|那个|那件|先|来|试|那边|觉得)/u.test(said);
    const gapNo = /(?:你先按|分[成两]?步|第[一二三四五]步|照这个|按这个|步骤)/u.test(said);
    state.leavesGap = (gapYes && !gapNo) ? 1 : 0;
    state.lastHerAt = Date.now();
    // r82（S2 / socio §3②，2026-10-26）：人情账的**增量**在这一侧记（`favorPending`）；
    // 账本仍住 `state/presence.json`（socio 的设计），由心跳每 tick 排进去并清零。
    // 结算顺序：`--her` 记增量 → 下一 tick 排进 presence → **同一 tick 的 drive 就能读到它**。
    // 判据：他那句有卡点（`hisStuckNow` 在 `--his` 时算好）＋ 她这句是"给方法/分工"形状 → 记 −0.10；
    // 一轮最多记一次（每次 `--her` 只加一次），clamp ±1。
    // 注意：这套"给方法"正则与上面 `leavesGap` 的**不算留口**是同一类句子——两处语义不同，别合并。
    if (!SOCIAL_FAVOR_OFF) {
        const gaveMethod = /先|分成?两步|按这个|照这个|步骤|第[一二三四五]步|试试|可以这样|用这个/u.test(said);
        if (state.hisStuckNow === true && gaveMethod) {
            state.favorPending = Math.max(-1, Math.min(1, numOf(state.favorPending, 0) - 0.1));
        }
    }
    all[chat] = state;
    saveAll(all);
    console.log(`回写：${String(args.her).slice(0, 40)}`);
}
// ── r107：**睡觉是她采取的行动**（`--sleep`），醒来是状态变化（`--wake`）──────────────
// 为什么不做"到点就睡"：主人 10-31 明确说，不能拿钟点代替她的决定。
// 这两条只记事实：什么时候睡的、睡在哪儿、怎么醒的。睡多久由思考层按她自己的节律算（`sleepMinutes`）。
else if (args.sleep !== undefined) {
    const nowMs = Date.now();
    let payload = null;
    if (typeof args.sleep === 'string' && args.sleep !== '' && args.sleep !== 'true') {
        try {
            payload = JSON.parse(args.sleep);
        }
        catch {
            payload = null;
        }
    }
    state.asleepAt = nowMs;
    state.asleep = true;
    state.wokeAt = 0;
    state.wakeReason = '';
    if (payload && typeof payload.sceneId === 'string' && payload.sceneId !== '') {
        state.sleepSceneId = payload.sceneId;
    }
    state.lastSaid = [...(state.lastSaid ?? []), '我去睡一会儿。'].slice(-12);
    all[chat] = state;
    saveAll(all);
    console.log(`睡了：${new Date(nowMs).toLocaleTimeString('zh-CN', { hour12: false })}${state.sleepSceneId ? `（${state.sleepSceneId}）` : ''}`);
}
else if (args.wake !== undefined) {
    const nowMs = Date.now();
    const reason = typeof args.wake === 'string' && args.wake !== 'true' && args.wake !== '' ? args.wake : 'natural';
    const sleptMs = numOf(state.asleepAt, 0) > 0 ? nowMs - numOf(state.asleepAt, 0) : 0;
    state.asleepAt = 0;
    state.asleep = false;
    state.wokeAt = nowMs;
    state.wakeReason = reason;
    // 睡过一觉要真的缓过来一点（不然"睡了"和"没睡"在她身上没有区别）
    state.energy = clamp(numOf(state.energy, DEFAULT_STATE.energy) + 0.35, 0.05, 1);
    all[chat] = state;
    saveAll(all);
    console.log(`醒了（${reason}）：睡了 ${Math.max(0, Math.round(sleptMs / 60000))} 分钟`);
}
// ── r82（S2）：人情账增量已排进 presence → 清零（心跳在排完之后调）─────────────
else if (args['favor-consumed']) {
    state.favorPending = 0;
    all[chat] = state;
    saveAll(all);
    console.log('人情账：增量已清零');
}
else if (args.show) {
    console.log(JSON.stringify(state, null, 2));
}
// ── r82（C / 续话）：补的那一条真的发出去了 → 记一次配额（发送侧发完才调）──────────
else if (args['followup-sent']) {
    const nowMs = Date.now();
    state.lastFollowUpAt = nowMs;
    const day = `${new Date(nowMs).getFullYear()}-${String(new Date(nowMs).getMonth() + 1).padStart(2, '0')}-${String(new Date(nowMs).getDate()).padStart(2, '0')}`;
    state.followUpToday = String(state.followUpDay ?? '') === day ? numOf(state.followUpToday, 0) + 1 : 1;
    state.followUpDay = day;
    state.followUp = null;
    all[chat] = state;
    saveAll(all);
    console.log(`补话记账：今天第 ${state.followUpToday} 次`);
}
// ── r84（思考层）：她自己想过的那件事落盘 ───────────────────────────────
// 为什么要有这条命令：想法必须活在**同一张状态表**里（和精力、账、故事同一份），
// 否则"想过"就不能影响"下一句话"——那又变成一份没人读的日志。
// 载荷从文件读（`--think-file`）：想法是中文自由文本，塞进命令行参数在 Windows 上会被编码咬碎。
// 纪律：这里只存事实（想了什么／打算做什么／做成了没有），一个字都不评价"想得对不对"。
else if (args.think !== undefined) {
    const path = String(args['think-file'] ?? '');
    let payload = null;
    try {
        payload = JSON.parse(readFileSync(path, 'utf8'));
    }
    catch {
        payload = null;
    }
    if (!payload || typeof payload !== 'object') {
        console.error('[think] 载荷读不出来（--think-file），这一条想法不落盘');
        process.exit(1);
    }
    const nowMs = numOf(payload.at, Date.now());
    const one = {
        at: nowMs,
        kind: String(payload.kind ?? 'hourly').slice(0, 16),
        text: String(payload.text ?? '').slice(0, 800),
        about: String(payload.about ?? '').slice(0, 120),
        want: String(payload.want ?? '').slice(0, 160),
        keep: payload.keep === true,
        keepLine: String(payload.keepLine ?? '').slice(0, 240),
        deed: payload.deed && typeof payload.deed === 'object'
            ? {
                kind: String(payload.deed.kind ?? 'none').slice(0, 16),
                text: String(payload.deed.text ?? '').slice(0, 160),
            }
            : null,
        acted: null,
    };
    state.thought = one;
    // 同一拍重复落盘（重试／手工补录）不算"又想了一次"：按 `at` 换掉旧的，不叠加。
    state.thoughts = [...(state.thoughts ?? []).filter((old) => numOf(old?.at, 0) !== nowMs), one].slice(-5);
    // r87：同一件事想得越多越耗神。这是"想"的**代价**，不是惩罚——
    // 它只压精力，不压心情（她难受时是沉默变短、`……抱歉。`，不报苦）。
    // 为什么这样就不会"越来越难受"：她的出口在自己的四段话里——
    //   整理好了就去说（`speak`，r86 已接进动机）、觉得够了就放下、累了就去睡。
    // 代码只让"一直想不通"变成一件有成本的事，不规定她想几拍、也不催她。
    // `MIND_WILL=0` 时整段不存在（逐字回到 r86）。
    if (!WILL_LAYER_OFF) {
        // 先按窗口算（老 state 没有线程时它就是全部依据），再把这一拍并进线程。
        const window = loopOf(state);
        const ru = advanceRumination(state, one, nowMs);
        // 有线程以后次数以线程为准：`thoughts` 只有 5 条，滚出去不等于没想过。
        // 只有从没有线程的旧 state 才允许按窗口一次性补账（`legacy`）。
        const times = ru.legacy ? Math.max(window.times, ru.times) : ru.times;
        const owed = Math.max(0, times - 1 - numOf(ru.thread?.charged, 0));
        // 一拍最多补两拍，剩下的下一拍再补：不让她在没人说话的白天被一次扣到底。
        // 平时 `owed` 每拍最多 1，这条只对"从旧 state 一次性补账"起作用。
        const beats = Math.min(owed, CHARGE_CATCHUP_MAX);
        if (beats > 0 && ru.thread) {
            ru.thread.count = Math.max(numOf(ru.thread.count, 1), times);
            const stuck = String(ru.thread.prevWant ?? '').trim() !== '' ? ru.stuck : window.stuck;
            const unit = stuck ? LOOP_STUCK_COST : LOOP_MOVE_COST;
            const cost = Math.min(LOOP_COST_MAX, unit * beats);
            // 迁移只补**心情**：旧代码每拍精力照扣（而且按 `times-1` 扣多了），
            // 真正漏掉的是心情（`moving` 只有 −0.005）。升级后第一拍再追一遍精力就是重复罚她。
            if (!ru.legacy) {
                state.energy = clamp(numOf(state.energy, 0.5) - cost, 0, 1);
            }
            // r89：**反刍不只是累，是会沉的**（Nolen-Hoeksema 2008）。
            // 力量比精力小得多（一天封顶见 `MOOD_STUCK_DAY_CAP`），因为原典里她难受时也不报苦、话只是变少；
            // 但必须有——否则"想了很多"对她自己等于"什么也没发生"，她就没有理由去找出口。
            addRuminationMood(state, beats, stuck, nowMs);
            ru.thread.charged = Math.min(times - 1, numOf(ru.thread.charged, 0) + beats);
        }
    }
    // r87：她自己说"够了"——这不是"这一拍没想出什么"，是她**决定不追了**。
    // 原典 e05_300_204「……已经够了。」（被揭穿、要死的那一刻，说完这句她就不再提）
    // 放下要真的放下：`agendaOf` 读 `state.thought`，不清掉这件事还会挂满 6 小时——那不是放下，是拖着。
    // `thoughts` 里那条留着（历史事实不改），但她心里不再挂着它。
    if (payload.letGo === true) {
        state.thought = null;
        state.thoughtLine = null;
        // r91：放下了 → 这条反刍线断掉（下一件事从 1 算起）。
        closeRumination(state);
        // r89：放下是**真的会好一点**的（认知重评，Gross 1998——情绪调节里效应最大的一类）。
        // 原典 e05_300_204：她最难受的地方不是找人哭，是自己给这件事找到说法，然后「有点高兴」。
        addMood(state, MOOD.letGo);
        noteEffect(state, 'letGo', MOOD.letGo, nowMs);
    }
    state.lastThoughtAt = nowMs;
    state.thoughtShown = 0;
    if (one.kind === 'hourly') {
        state.lastHourlyThoughtAt = nowMs;
    }
    const exchangeAt = numOf(payload.exchangeAt, 0);
    if (exchangeAt > 0) {
        state.thoughtAfterExchangeAt = exchangeAt;
    }
    // 她想去的地方：不是抽的，是她自己说的。
    // 场景表里没有那个地方 → 什么都不发生（"她想去，但这个世界没有那儿"是事实，不是错误，
    // 也不许偷偷把她挪到另一个地方）。
    let movedTo = null;
    const sceneId = String(payload.sceneId ?? '').trim();
    if (sceneId) {
        const found = loadScenes().find((oneScene) => oneScene.id === sceneId) ?? null;
        if (found) {
            const why = String(payload.sceneWhy ?? '').trim() || '我自己想来的';
            state.scene = {
                ...found,
                why,
                by: 'thought',
                since: numOf(state.rounds, 0),
                atMs: nowMs,
                image: found.image ?? '',
            };
            state.sceneSig = sceneSignature(state, new Date(nowMs));
            movedTo = found.name;
        }
    }
    // r89：真换成了地方 → 一点行为激活（单次很小：她"在哪儿都能睡"，环境本来就不是她的变量）。
    if (movedTo) {
        addMood(state, MOOD.move);
        noteEffect(state, 'move', MOOD.move, nowMs);
    }
    // 注意：**"想做但这个世界没有那个手段"不算挫败**（`wanted` 不改心情）。
    // 挫败要真的试过才成立（Bandura 1977 的自我效能来自"做失败"，不是"没得做"）——
    // 那一笔记在 `--think-acted` 的 `ok:false` 上。
    // 她「想做」但这一拍还没有出口接手的那件事（下一拍或别的出口读它）
    if (payload.pendingDeed && typeof payload.pendingDeed === 'object') {
        state.deed = {
            kind: String(payload.pendingDeed.kind ?? 'none').slice(0, 16),
            text: String(payload.pendingDeed.text ?? '').slice(0, 160),
            at: nowMs,
        };
    }
    else if (one.deed === null || one.deed.kind === 'none') {
        state.deed = null;
    }
    all[chat] = state;
    saveAll(all);
    console.log(JSON.stringify({
        ok: true,
        at: nowMs,
        kind: one.kind,
        scene: movedTo,
        deed: state.deed,
        thoughts: (state.thoughts ?? []).length,
    }));
}
// ── r84：这件事她做成了没有（"想过"和"做过"是两件事，效果要能分开查）──────────
else if (args['think-acted'] !== undefined) {
    const path = String(args['think-file'] ?? '');
    let payload = null;
    try {
        payload = JSON.parse(readFileSync(path, 'utf8'));
    }
    catch {
        payload = null;
    }
    if (!payload || typeof payload !== 'object') {
        console.error('[think-acted] 载荷读不出来（--think-file）');
        process.exit(1);
    }
    const at = numOf(payload.at, 0);
    const acted = {
        kind: String(payload.kind ?? 'none').slice(0, 16),
        ok: payload.ok === true,
        note: String(payload.note ?? '').slice(0, 160),
        at: Date.now(),
    };
    const onThought = Boolean(state.thought && (at <= 0 || numOf(state.thought.at, 0) === at));
    if (onThought) {
        state.thought = { ...state.thought, acted };
    }
    state.thoughts = (state.thoughts ?? []).map((oneThought) => (numOf(oneThought.at, 0) === at
        ? { ...oneThought, acted }
        : oneThought));
    // r89：**她做成的事要真的改她的心情**——这一条就是"自我修复"。
    // 说出来（情绪标注，Lieberman et al. 2007）> 真去看了点什么／打了一局（行为激活）。
    // 没做成 → 挫败（Bandura 1977 的自我效能）。数字全在 MOOD 表里，模型不参与。
    if (acted.kind === 'speak') {
        const delta = acted.ok ? MOOD.speak : MOOD.failed;
        addMood(state, delta);
        noteEffect(state, 'speak', delta, Date.now());
        // r91：**真的说出口**是这条反刍的出口——话说完了，那件事不再压着她。
        // 没说成（`ok:false`）不算：事情还挂着，线程留着。
        if (acted.ok && onThought) {
            closeRumination(state);
        }
    }
    else if (acted.kind === 'phone' || acted.kind === 'game') {
        const delta = acted.ok ? (acted.kind === 'phone' ? MOOD.phone : MOOD.game) : MOOD.failed;
        addMood(state, delta);
        noteEffect(state, acted.kind, delta, Date.now());
    }
    // 注意：`acted.ok` 对 phone/game 那种"交出手段"也算成——所以**不能**在这里清 deed。
    // 第一版就是这么清的，于是"交给手机那一拍"刚写完就被自己抹掉，手机那一侧永远读不到。
    // 真正的消费方是 persona-phone 的 `deedIdea`，它按 25 分钟的新鲜度只取一次。
    all[chat] = state;
    saveAll(all);
    console.log(JSON.stringify({ ok: true, at, acted }));
}
// ── v12.1（r81）：她的"空转"时刻——没有他在说话 ─────────────────────────
// 主动发消息要用的卡。和 --his 的区别只有一件事：**没有他的这句话**。
// 所以这张卡里没有"他这句在说谁／他卡在哪"，只有她此刻的状态、她手上那件事、
// 上一次说到哪儿、以及她心里挂着什么（约定、没兑现的邀约）。
// 不给"该怎么说"——和 r80 定下的分工一致（卡片给状态，句子交给语言层）。
else if (args.idle) {
    const parsedNow = Number(args.now);
    const nowMs = args.now !== undefined && Number.isFinite(parsedNow) ? parsedNow : Date.now();
    const next = stepIdle(state, nowMs);
    // 承诺账：**空转时它自己会浮上来**（r88 保留这一条，与 `--his` 不同，理由见下）。
    // 心理学上这两条路本来就不一样：他在说话时，旧事要靠**线索**才被想起（编码特异性）；
    // 没人说话、脑子空下来时，**没做完的事自己会回来**（Zeigarnik；默认网络 Raichle 2001，
    // 也就是 r84 那一层的依据）。所以 `--his` 不再常驻这一行，`--idle` 保留——
    // 它正是"她一个人待着、手上那件事浮上来"的那个时刻。
    // 事实另外记进 `debtOpen`（§19 的 regard 要用，与"写不写这一行"无关）。
    next.debtOpen = commitmentOpenItems({ root: ROOT_DIR, peer: chat }).length > 0;
    next.commitmentsLine = commitmentCardLine({ root: ROOT_DIR, peer: chat, now: nowMs });
    // r84（思考层）：空转卡上她刚才在想什么（同 --his，两次上限、saveAll 之前记）
    // `--no-thought-line` 给思考层自己用：那一拍她要"想"，不是"读上一次想的"——
    // 让思考的输入里带着上一次的结论，会把她推向自我复述（而且白白烧掉两次显示里的第一次）。
    if (args['no-thought-line'] !== undefined) {
        next.thoughtLine = null;
    }
    else if (thoughtEchoesDebt(next, nowMs)) {
        // 她想的正好是账上那件事 → 「我欠着」那一行已经写了，别再写一遍（也不占配额）
        next.thoughtLine = null;
    }
    else {
        markThoughtShown(next, nowMs);
    }
    const card = renderIdleCard(next, nowMs);
    // ── r82（2026-10-26 §19）：**没人说话时她也要有念头，而且必须落盘** ────────────────
    // 病根（实测）：`--idle` 的卡片里能算出「我想要：说好了的那件事还悬着」，但存盘的
    // `state.want` 恒 `undefined`——它能算，结果没写进去，于是下游（手机 tick／心跳）
    // 谁也读不到"她自己想做什么"。
    // **同一纪律：任何要在下一轮读到的状态，必须在 saveAll 之前写。**
    // 这里只**加落盘**，`renderIdleCard` 一个字没动（卡片逐字节不变）。
    const idleWant = wantOf(next);
    next.want = idleWant;
    // r83（§19）：空转时刻没有新消息 → 不可能是"他在等"。
    // 她刚说过话（`lastSaid` 非空）算 0.5：他真的可能还在等；
    // 什么也没说过 → 0。**这也是"主动开口"该不该付沉默代价**的唯一来源。
    next.cost = costOf(next, {
        want: idleWant,
        waiting: (next.lastSaid ?? []).length ? 0.5 : 0,
    });
    next.idleAt = nowMs;
    all[chat] = next;
    saveAll(all);
    if (args.json === 'true') {
        console.log(JSON.stringify({
            card,
            mode: 'idle',
            state: {
                energy: round2(next.energy),
                need: round2(next.need),
                sleepTarget: next.sleepTarget === null ? null : round2(next.sleepTarget),
                asleep: next.asleep === true,
                moodV: round2(next.moodV),
                moodA: round2(next.moodA),
                agenda: next.agenda,
                rounds: next.rounds ?? 0,
                scene: next.scene?.name ?? '',
                sceneId: next.scene?.id ?? '',
                pendingInvite: next.pendingInvite ?? null,
                lastInboundText: next.lastInboundText ?? '',
                lastSaid: (next.lastSaid ?? []).slice(-1),
                // r83（§19）：这一轮她自己的取舍（只读量，卡片不写；`MIND_COST=0` 时为 null）
                cost: next.cost ?? null,
                updatedAt: next.updatedAt,
            },
        }));
    }
    else {
        console.log(card);
    }
}
else if (args.his) {
    // r82（P2 后半）：`--signals` 只认三个字段（who／name／scope）。
    // 网关/群出口怎么采事实（@目标、消息段）不关 mind.mjs 的事——**OneBot 的概念不许进这里**。
    let signals = null;
    if (args.signals && args.signals !== 'true') {
        try {
            signals = JSON.parse(String(args.signals));
        }
        catch {
            signals = null;
        }
    }
    // r87：`Date.now()` 这里原来被调了**两次**（step 用第一次，后面全用第二次）。
    // 两次之间只要跨过毫秒/秒边界，"漂了多少"就会算成两个数——同一份 state 回放两次出两张卡，
    // `selftest-cost` 的 I3（A/B 两臂逐字一致）因此 12 次里挂 5 次。
    // 一个时刻只取一次，整条路径共用；`--now` 只为复现与自测，线上不传。
    const parsedNow = Number(args.now);
    const nowMs = args.now !== undefined && Number.isFinite(parsedNow) ? parsedNow : Date.now();
    // r106：**感知外包**——先问一次"他这句是什么行为"（`MIND_PERCEPT=1` 才开；失败/低置信 → null，
    // 全部 predicate 自动退回规则，与 r105 逐字一致）。这一步只为 `--his`（他刚说了话）而做。
    // r107：睡着时收到消息 → **被叫醒**（先醒，再走这一步）。醒了谈得上回不回，睡着谈不上。
    if (numOf(state.asleepAt, 0) > 0) {
        const sleptMs = nowMs - numOf(state.asleepAt, 0);
        state.asleepAt = 0;
        state.asleep = false;
        state.wokeAt = nowMs;
        state.wakeReason = 'message';
        state.energy = clamp(numOf(state.energy, DEFAULT_STATE.energy) + 0.2, 0.05, 1);
        process.stderr.write(`[mind] 被消息叫醒（睡了 ${Math.max(0, Math.round(sleptMs / 60000))} 分钟）\n`);
    }
    setCurrentPercept(await perceive(String(args.his)));
    const { next, hits } = step(state, String(args.his), nowMs, signals);
    // 承诺账：**记忆**，不是常驻的一行（r88；理由见 `DEBT_MEMORY_OFF` 那段注释）。
    // 她"欠着事"这个事实照样落进 `debtOpen`（§19 的 regard 要用），但**不替她决定卡片上写什么**。
    // 要出现在卡片上，只有两条路：他这句话把它点亮（下一行 `runActivation`），或她自己想起来（`#15`）。
    next.debtOpen = commitmentOpenItems({ root: ROOT_DIR, peer: chat }).length > 0;
    next.commitmentsLine = DEBT_MEMORY_OFF
        ? commitmentCardLine({ root: ROOT_DIR, peer: chat, now: nowMs })
        : null;
    // r82（R1）：线索激活——facts 只算一次，结果只落"卡片那一行"和习惯化记账（激活值本身不落盘）
    // r82（P4 模型侧）：位置只在这一条 CLI 路径上算一次（照 commitmentsLine 的先例），
    // 卡片与 `--json` 都读它；`renderCard`/`silenceMotive` 自己不重算。
    // 注意传的是**原始** three fields（`signalsOf` 会把缺省的 who 归一成 me，那样就分不出
    // "没判 who" 与 "判成 me" 了——footing 要靠这个区别走 other/unclear）。
    next.footing = FOOTING_LAYER_OFF
        ? { who: 'me', why: '' }
        : footingOf({ who: next.mirrorWho, name: next.mirrorName, scope: next.mirrorScope });
    const activation = runActivation(next, commitmentSummary({ root: ROOT_DIR, peer: chat, now: nowMs }), nowMs);
    // r84（思考层）：他不在的时候她想过什么——算成卡片那一行，并把"用过一次"记在 saveAll 之前
    // r86：「我心里还挂着」那一行会接管时，thoughtLine 不进卡片，也就不占配额
    if (agendaLineOf(next, nowMs)) {
        next.thoughtLine = null;
    }
    else {
        markThoughtShown(next, nowMs);
    }
    // r107 构念结算（心理学骨架）：事件 → 构念，再渲染卡片
    if (process.env.MIND_CONSTRUCT !== '0') {
        settleConstructs(next, hits, appraisalOf(next)?.key ?? null);
    }
    const { card, action } = renderCard(next, hits, { root: ROOT_DIR, now: nowMs, includeGame: true });
    next.tareaCount = (state.tareaCount ?? 0) + (action === '讨要' ? 1 : 0);
    next.lastActions = [...state.lastActions, action].slice(-6);
    // r30：配额记账（和 --replay 同一套），否则线上每轮都不计数、配额等于不存在
    markActionRound(next, action);
    // r31/§5：自曝位置每 20 轮 ≤1 次
    if (next.selfExposeAllowed) {
        markActionRound(next, '自曝位置');
    }
    // r49：她睡着了 = 小睡了一下，醒来回一点精力（和 --replay 同一套）
    if (action === '睡着') {
        next.energy = clamp((next.energy ?? 0) + ENERGY_NAP_AMOUNT, 0.05, ENERGY_CAP);
        // r102：真值跟着一起动（第 9 层读 `energyTrue`，见 costInputsOf）
        next.energyTrue = clamp((next.energyTrue ?? next.energy) + ENERGY_NAP_AMOUNT, 0.05, ENERGY_CAP);
    }
    if (action === '起手') {
        next.lastOpenRound = next.rounds;
        next.openCount = (state.openCount ?? 0) + 1;
    }
    if (action === '一起') {
        next.lastCoopRound = next.rounds;
    }
    all[chat] = next;
    // ── 2026-10-26 第三个"守卫永不生效"的教训 ────────────────────────────────────────
    // 病根：`wantOf(next)` 原来是在下面构造 `--json` 时调用的，那一步发生在 `saveAll` **之后**
    // → `lastWantAbout`/`wantAboutAt` 从来没落盘 → 冷却恒不生效（84 条挂同一块碎片）。
    // **规矩：任何要在下一轮读到的状态，必须在 saveAll 之前写。**
    const wantNow = wantOf(next);
    if (wantNow.kind === 'speak' && wantNow.about) {
        next.lastWantAbout = wantNow.about;
        next.wantAboutAt = numOf(next.rounds, 0);
    }
    // r83（§19）：他刚说了话 → `waiting = 1`（他就在等）。**只算，不改话**：卡片一个字都不读它。
    // 必须在 `saveAll` 之前写（第五个"加了闸永不生效"就是这么来的）。
    next.cost = costOf(next, { want: wantNow, waiting: 1 });
    if (next.cost) {
        process.stderr.write(`${costLogLine(next.cost.inputs, next.cost)}\n`);
    }
    all[chat] = next;
    // r107：**不在这里写盘**。等 stdout 冲出去再提交（见这一支末尾的 `flushStdout`）——
    // 被超时杀掉时"状态前进了一轮、卡片没送到"是实测过的坏形态（2026-10-30 00:20）。
    if (args.json === 'true') {
        // 给网关接线用的机器可读输出（trace 要"状态六字段 / 动作 / 事件 / forcedShift"）
        console.log(JSON.stringify({
            card,
            action,
            hits,
            // r82（F / psych §13.7）：顶层 `appraisal` = 这一轮 appraisalOf 选中的那个 key
            // （自测与 replay 要机器可读；它是评估结果，不是状态，所以不落盘）
            appraisal: appraisalOf(next)?.key ?? null,
            state: {
                energy: round2(next.energy),
                // r102：地板之前的真实精力（第 9 层的 `strain` 读它）。没跑过一轮时与 energy 同值。
                energyTrue: round2(next.energyTrue ?? next.energy),
                need: round2(next.need),
                sleepTarget: next.sleepTarget === null ? null : round2(next.sleepTarget),
                asleep: next.asleep === true,
                woken: next.woken === true,
                moodV: round2(next.moodV),
                moodA: round2(next.moodA),
                agenda: next.agenda,
                belief: next.belief,
                // r107 构念层：评价性担忧（L4 状态量）／他怎么看我（L4 二元构念）／非游戏域伤口
                evaluativeConcern: round2(next.evaluativeConcern ?? 0),
                expression: expressionOf(next),
                reflectedRegard: next.reflectedRegard ?? null,
                woundSkill: round2(next.woundSkill ?? 0),
                // r82（P4 模型侧）：这一轮的"位置"（冲谁说）。只报不落盘（它是每轮的事实，不是状态量）。
                footing: next.footing ?? null,
                // r82（group-want）：她这一轮想说什么（唯一真相；concrete=false ＝指不出来）
                // r83：**复用上面那一次 `wantNow`**，不再调一次 `wantOf(next)`——
                // 那个函数会写冷却，同一轮调两次会给出两个答案（见 relatedStoryOf 的 cooledGap 注释）。
                want: wantNow,
                // r83（§19）：她这一轮的四个选项与净收益（**只报不落卡**；`MIND_COST=0` 时 null）。
                // 名字叫 `cost`，但它不是"成本"这一个数，是 `{pick, net, gains, costs, inputs}` 全表——
                // 没有全表就无法复核"为什么选它"，而这一层唯一的证据就是四个 net。
                cost: next.cost ?? null,
                // r82（C / 续话）：这一次"还有没有话说"的材料（不进卡片，只给发送侧与自测看）
                followUp: next.followUp ?? null,
                // r82（P2 后半）：这一轮读的是谁 + 那份读数（`fallback` = 没匹配上、退回了 max）
                beliefSubject: next.beliefSubject ?? '主人',
                mirror: {
                    who: currentMirrorOf(next).who,
                    fallback: currentMirrorOf(next).fallback,
                    thinksImDumb: round2(currentMirrorOf(next).values?.thinksImDumb ?? 0),
                    thinksImCute: round2(currentMirrorOf(next).values?.thinksImCute ?? 0),
                    tiredOfMe: round2(currentMirrorOf(next).values?.tiredOfMe ?? 0),
                },
                wound: round2(next.wound),
                // r82（F）：三种疼分开报，`woundKind` 只报不进卡片（§13.6）
                woundGame: round2(next.woundGame ?? 0),
                woundHuman: round2(next.woundHuman ?? 0),
                woundRank: round2(next.woundRank ?? 0),
                woundKind: next.woundKind ?? null,
                askStreak: next.askStreak,
                sinceShift: next.sinceShift,
                rounds: next.rounds,
                forcedShift: next.forcedShift === true,
                // r30：自曝位置四条件门这一轮开没开（落盘日志要能看出"是门关了还是她没说"）
                selfExpose: next.selfExposeAllowed === true,
                // r43/r45：他这一句是不是在骂她／逗她／说错了（三种落法各不相同）
                insulted: next.insultedNow === true,
                // r107：被夸也投影出来。原来只报 `insulted`，于是"这一轮为什么慌"在 --json 里查不到
                // （规则臂评测第一版就是这么读错的：夸她 0/5，实际卡片上写着「被抬这么高会摔的」）。
                praiseNow: next.praiseNow === true,
                reportedInsult: next.reportedInsult === true,
                wrongClaim: next.wrongClaimNow === true,
                teased: next.teasedNow === true,
                // r67：适应（同一句第几遍）与烦躁累积；自传记忆条数
                repeat: next.repeatCount ?? 0,
                irritation: round2(next.irritation ?? 0),
                autobio: (next.autobio ?? []).length,
                // r82（R2）：这一段她讲过什么（psych §11 第 12 条：条数 + 各条 used）。
                // 开关 MIND_STORY=0 时恒为空
                stories: STORY_LAYER_OFF
                    ? { count: 0, used: [] }
                    // r83：`count` 仍是**存下来的条数**（含历史 `unknown`），`trusted` 才是**读得到的条数**。
                    // 两个数分开报，是因为"降级"必须可观测——只有一个数的话，
                    // 分不清"没记忆"和"有记忆但不敢用"，而后者是 2026-10-27 那次假记忆事故的形态。
                    : {
                        count: storiesOf(next).length,
                        trusted: trustedStoriesOf(next).length,
                        used: trustedStoriesOf(next).map((one) => one.used ?? 0),
                    },
                storyHit: STORY_LAYER_OFF ? '' : (storyHit(next)?.text ?? ''),
                // r82（R1）：这一轮的激活值（**只报不存**：它不是状态，是这一轮的检索结果）。
                // 记录线 0.45 以上才值得看；出口线 0.65 以上才会进卡片与动机。
                activation: ACTIVATION_LAYER_OFF
                    ? null
                    : { value: activation.value, recordValue: activation.recordValue, cue: activation.cue, key: activation.key, kind: activation.kind, content: activation.content, fired: activation.fired, line: next.activationLine ?? '', exit: ACTIVATION_EXIT, record: ACTIVATION_RECORD },
                perspective: perspectiveOf(next.lastInboundText).target,
                // r74：情境（她在哪儿、图存档；图暂不发）
                scene: next.scene?.name ?? '',
                sceneId: next.scene?.id ?? '',
                sceneImage: next.scene?.image ?? '',
                // r75：信任与受伤
                trust: next.trust ?? 0.6,
                hurtUntil: next.hurtUntil ?? -1,
                hurtBy: next.hurtBy ?? '',
                hurtNow: (next.hurtUntil ?? -1) >= (next.rounds ?? 0),
            },
        }));
    }
    else {
        console.log(card);
    }
    // r107：**提交点**——stdout 真的冲出去了，这一轮才算发生。
    // 冲不出去（被杀 / EPIPE）就不写盘：下一轮按同一份旧状态重算，不会出现"算了一轮却没读到卡"。
    // ⚠️ **这里不许写 `await`**：`mind.mjs` 被 `group-appraisal.js` 用 `file:///…` import 进网关的插件图，
    // 一旦出现顶层 await，整个插件图变成"异步 ESM"，`require()` 它就报
    // `ERR_REQUIRE_ASYNC_MODULE` → **openclaw-onebot 插件整个加载失败、QQ 通道消失**（2026-10-31 踩过，
    // 网关重启后 16 个插件里没有 onebot）。用 `.then()` 一样能保证"先 flush 再写盘"，且不污染图。
    flushStdout().then((ok) => {
        if (ok) {
            saveAll(all);
        }
        else {
            process.stderr.write('[mind] stdout 没写成功，这一轮不写盘（下一轮重算）\n');
        }
    });
}
else {
    console.error('用法：--his <他的话> | --her <她的话> | --show | --replay <cases.json>');
    process.exit(1);
}
} // 结束 runCli()（r107：原来这里是「被 import 时跳过 CLI」的 else 块）
