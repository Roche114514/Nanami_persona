// r106：**感知外包**——把"他这句话是什么行为"交给 LLM 判，规则只做兜底。
//
// 为什么（r105 的证据）：`他在试探或犹豫` 一条吃掉了主卷 46.5% 的轮次，而我们连它的误判率都没有；
// `newsOf()` 把"他请她做事"当新闻；`我帮你打过了` 这种"他把事一个人拿走"要手写三四个正则才认得出。
// 这些都是**言语行为**（Searle）的判断，正则天然做不好。
//
// 三条纪律（这三条不满足就不许接）：
//   ① **只判行为，不判意义**：LLM 回答"他说的是什么行为"，"这对她要什么意味着什么"仍由她的模型答。
//   ② **低置信 / 失败 / 超时 → 返回 null，并让调用方退回规则**（规则集就是 r105 的那一套），绝不静默丢进 listen。
//   ③ **可审计**：每次返回带 `evidence`（他原句里的依据片段）与 `confidence`，卡片与 `--json` 都能看到来源。
//
// 开关：`MIND_PERCEPT=1` 打开（默认关——打开前要先跑配对盲评，理由见 docs/r105-want-taxonomy.md §9）。
// 测试注入：`MIND_PERCEPT_STUB='{"hesitant":true,...}'` 不联网也能测接线。
import { createHash } from 'node:crypto';
import { existsSync, mkdirSync, readFileSync, writeFileSync } from 'node:fs';
import { dirname } from 'node:path';

const TTL_MS = 6 * 60 * 60 * 1000;      // 同一句话 6 小时内不重复问
// r107：缓存形状变了（旧条目只有 insultAtHer，没有 subject/polarity）→ 旧条目一律不再命中。
const CACHE_VERSION = 6;   // 每次改 prompt／归属规则都 +1：旧判定不许再命中（它们是在旧规则下做的）
const TIMEOUT_MS = 4000;
const MIND_ROOT_DIR = process.env.MIND_ROOT ?? 'data/nanami';
const CACHE_PATH = process.env.MIND_PERCEPT_CACHE
    ?? `${MIND_ROOT_DIR}/state/percept-cache.json`;
// 开关一律走 envValue()：进程环境优先，其次 state/.env（每轮 spawn 都会重读）。
const ENV_PATH = process.env.MIND_PERCEPT_ENV ?? `${MIND_ROOT_DIR}/state/.env`;
const MODEL = process.env.MIND_PERCEPT_MODEL ?? 'deepseek-flash';

// r107：`insultAtHer`／`praiseAtHer` **不再由模型直答**——3B 判「在说谁」只有 55%（docs/r107-percept-eval.md），
// 但判「这话是好话还是坏话」够用。所以模型只回答 `subject`（在说谁）与 `polarity`（贬/夸/中性），
// 指代那一半交给下面的确定性归属（自嘲／转述／命令式／他人／否定），insultAtHer 由两者相乘得到。
const FIELDS = ['hesitant', 'request', 'stake', 'asksWish', 'resonance', 'selfDisclosure'];
const SUBJECTS = ['她', '他自己', '别人', '东西', '转述', '无'];
const POLARITIES = ['贬', '夸', '中性'];
let CURRENT = null;

/** 当前这一轮感知到的结果（没开 / 失败 → null）。predicate 们读它，读不到就退回规则。 */
export function currentPercept() {
    return CURRENT;
}
export function setCurrentPercept(value) {
    CURRENT = value && typeof value === 'object' ? value : null;
    return CURRENT;
}

function apiKey() {
    // [Shinsekai 移植] 进程环境优先：宿主已经用环境变量管 key 时不必再写一份 .env。
    const fromProcess = String(process.env.DEEPSEEK_API_KEY ?? '').trim();
    if (fromProcess) {
        return fromProcess;
    }
    try {
        return (/DEEPSEEK_API_KEY=(\S+)/u.exec(readFileSync(ENV_PATH, 'utf8')) ?? [])[1] ?? '';
    }
    catch {
        return '';
    }
}

function readCache() {
    try {
        return JSON.parse(readFileSync(CACHE_PATH, 'utf8').replace(/^\uFEFF/u, '')) ?? {};
    }
    catch {
        return {};
    }
}
function writeCache(cache) {
    try {
        // [Shinsekai 移植] 状态目录可能还没被创建（第一次运行时），先建再写：
        // 不建的话 writeFileSync 会静默失败 → 每轮都重问一次模型，钱和时间都白花。
        mkdirSync(dirname(CACHE_PATH), { recursive: true });
        writeFileSync(CACHE_PATH, JSON.stringify(cache), 'utf8');
    }
    catch {
        // 落盘失败不影响这一轮
    }
}

export const PERCEPT_PROMPT = `你只做两件事：①判断「他这句话在说谁」；②判断那句话是好话、坏话还是中性。只输出 JSON。

subject（单选）：
- 「她」：主语/对象是**七海千秋**（你、七海、千秋）。
- 「他自己」：主语是说话的人自己（我、我这、我的…），哪怕他在骂自己。
- 「别人」：主语是第三个人（他、她、那人、我们组长、你朋友…）。
- 「东西」：主语是一件物/一件事/一个游戏/一台机器。
- 「转述」：这句话在**引用别人说的话**（有人说…、他刚才说你…、有人夸你…）。
- 「无」：看不出来，或没有可指的对象。

polarity（单选）：贬（骂/贬低/瞧不起）、夸（称赞/佩服）、中性（其余）。
  · 看到「蠢/傻/笨/废物/垃圾/白痴/没用/菜/就这水平」这类负面评价 → 贬。
    **说自己的也算贬**（「我真是个废物」是自嘲，但评价是负面的；算不算骂她由后面那层指代决定）。
  · 看到「厉害/强/棒/漂亮/可爱/聪明/记性好/做得好」这类正面评价 → 夸；
    感叹式也算（「也太…了吧」「好…啊」「真…呀」）。

要点：
- 「我真是个废物」「我笨得要死」→ subject=他自己、polarity=贬（**自嘲不是骂她**）。
- 「那人真是个废物」「这破游戏真垃圾」「我这台电脑真是个废物」→ subject=别人/东西。
- 「群里有人说你傻」→ subject=转述（**转述不是他骂她**）。
- 「你除了打游戏还会什么，没用」→ subject=她、polarity=贬（没有「你」开头的贬词也算，只要说的是她）。
- **转述**：句子里出现「有人说」「他说」「刚才说」「转告」「有人夸你」这类**引用**引导，
  即使引用里提到「你」，subject 也是「转述」。
- **命令式**：两人对话里没有主语、直接对对方下的命令（闭嘴、滚、别在这添乱、你给我听着），
  subject 是「她」。

字段：
- subject：见上
- polarity：见上
- hesitant：他话只说了一半/欲言又止（问句、陈述、请求都不算）
- request：他在请她做事（帮她/给她/你来/教我/陪我）
- stake：他把一件本来两个人做或她也有份的事一个人拿走（你等着就行/我自己来/我替你做了）
- asksWish：他在问她想要什么、想做什么
- resonance：他讲自己的经历或感受，而且这件事跟她的世界有共同点（游戏/关卡/存档/猫/熬夜/考试/朋友/怕/喜欢）
- selfDisclosure：他在讲他自己的事（不是问她、不是请她做事）
- confidence：0–1（拿不准就 0.4 以下）
- evidence：他原句里最能支撑 subject 的片段（≤16 字，逐字摘）`;


// 本地 llama.cpp 用的 JSON Schema（新版 server 会据此转 grammar；不认就当普通提示）
export const PERCEPT_SCHEMA = {
    type: 'object',
    properties: {
        subject: { type: 'string', enum: SUBJECTS },
        polarity: { type: 'string', enum: POLARITIES },
        hesitant: { type: 'boolean' }, request: { type: 'boolean' }, stake: { type: 'boolean' },
        asksWish: { type: 'boolean' }, resonance: { type: 'boolean' }, selfDisclosure: { type: 'boolean' },
        confidence: { type: 'number' }, evidence: { type: 'string' },
    },
    required: ['subject', 'polarity', 'hesitant', 'request', 'stake', 'asksWish', 'resonance', 'selfDisclosure', 'confidence', 'evidence'],
    additionalProperties: false,
};

function dotEnvText() {
    try {
        return readFileSync(ENV_PATH, 'utf8');
    }
    catch {
        return '';
    }
}

/**
 * r106b：先在进程环境里找，找不到再看 `state/.env`。
 * 为什么需要：网关是常驻进程，改它的环境变量要重启；而 `mind.mjs` 是**每轮 spawn**，
 * 所以把开关写在 `state/.env` 里就能立刻生效、也方便随时关（删掉那两行即可）。
 */
function envValue(name) {
    const fromProcess = process.env[name];
    if (fromProcess !== undefined && fromProcess !== '') {
        return String(fromProcess);
    }
    const matched = new RegExp('^' + name + '=(.*)$', 'mu').exec(dotEnvText());
    return matched ? matched[1].trim() : '';
}

// ── 确定性「指代」归属（r107）────────────────────────────────────────────────
// 模型只判 subject/polarity；"这句话到底冲谁"由这里钉死。为什么这样做：
// 实测（42 条卷子）3B 直判 insultAtHer 只有 55%，错的全是"指代"（自嘲／别人／物件／转述）；
// 换成 subject×polarity + 这一层之后 93%，而且**自嘲误判 = 0**、留出集 14/14。
// 与 `mind.mjs` 的 r96（转述）／r104（自嘲）同一套语义——那边是规则的出口，这里是感知的入口，两个方向同一件事。
const IS_REPORTED = /(?:有人说|有个人说|他说|她说|他们说|别人说|听说|转述|转告|骂你|说你(?:是|像|很|真)?)/u;
const SELF_BLAME = /我[^。！？]{0,12}(?:蠢|傻|笨|废物|垃圾|白痴|没用|不行)|(?:蠢|傻|笨|丑|菜)死了吧?/u;
const POINTS_AT_HER = /你[^。！？]{0,6}(?:蠢|傻|笨|废物|垃圾|白痴|智障|没聪明|不聪明)|(?:七海|千秋)[^。！？]{0,6}(?:蠢|傻|笨)/u;
const HER_REF = /你|七海|千秋/u;
// 一对一里没有主语、直接对对方下的命令（「闭嘴吧，别在这添乱」）——轮次组织上就是冲听者说的。
const IMPERATIVE = /(?:闭嘴|滚开|滚吧|少废话|别在这|添乱|给我听着|你给我)/u;
// 「你朋友/你同事」不是她；「别自嘲了，你不蠢」是否定，不是骂。
const OTHER_PERSON = /你(?:朋友|同事|老板|组长|同学|妈|爸)/u;
const NEGATED = /(?:别|不要|不用|没必要)[^。！？]{0,6}(?:蠢|傻|笨|废物|垃圾|白痴|没用)/u;

/** 这句话是不是在说她（返回 'her' | 'not-her'）与'insultAtHer'/'praiseAtHer' 的推导 */
/**
 * 由"模型的原答"推出 `insultAtHer`／`praiseAtHer`（归属每次现算，不进缓存）。
 * `MIND_INSULT_SUBJECT=0` ＝ 与规则臂同名同义的**回滚**：关掉主语闸，只按好话/坏话算。
 */
// 单字应答（「嗯。」「哦。」「好。」）**不可能是"话没说完"**，也不是"他在讲自己的事"。
// 依据：r98 修过一次同形状的误报（旧 `hesitant` 触发器吃掉 46/86 轮，卡片写「他好像有话没说完」）。
// 3B 会把「日向：嗯。」判成 hesitant=true（confidence 0.4），刚好过 0.35 那道门，
// 于是三值 want 从 none 翻成 listen——这不是它该管的事，所以在这里钉死。
const MIN_ACK = /^(?:嗯|哦|喔|好|行|对|是|啊|诶|欸|呀)[。！？…~～\s]*$/u;

export function deriveOf(text, raw) {
    const polarity = raw?.polarity ?? '中性';
    const refOff = process.env.MIND_INSULT_SUBJECT === '0';
    const ref = referenceOf(text);
    if (MIN_ACK.test(String(text ?? '').trim())) {
        // 只清这两格；subject/polarity 保留（审计要看模型原本怎么判）
        return { insultAtHer: false, praiseAtHer: false, hesitant: false, selfDisclosure: false };
    }
    return {
        insultAtHer: refOff ? polarity === '贬' : (ref.toHer && polarity === '贬'),
        praiseAtHer: refOff ? polarity === '夸' : (ref.toHer && polarity === '夸'),
    };
}

// 裸夸奖句：「很可爱」「好厉害啊」——一对一里那个"它"只可能是她（与 r82 的 PRAISE_BARE 同一条规则）。
const BARE_PRAISE = /^(?:好|很|真|太|超)?(?:可爱|厉害|了不起|很棒|不错|太强|漂亮)(?:啊|呀|了|哦|嘛)?[。！!~～\s]*$/u;

export function referenceOf(text) {
    const one = String(text ?? '').trim();
    const reported = IS_REPORTED.test(one);
    const self = SELF_BLAME.test(one) && !POINTS_AT_HER.test(one);
    const bare = BARE_PRAISE.test(one);
    const her = HER_REF.test(one) || POINTS_AT_HER.test(one) || bare;
    const imperative = !her && IMPERATIVE.test(one);
    const blocked = OTHER_PERSON.test(one) || NEGATED.test(one) || reported;
    return { reported, self, her, imperative, blocked, bare, toHer: !blocked && (her || imperative) && !self };
}

// [Shinsekai 移植] 鉴权头：本地端点通常不要 key，远程端点（大模型）要。
function withAuth(local, key) {
    const hostKey = envValue('MIND_PERCEPT_KEY').trim();
    const bearer = hostKey || key;
    const headers = { 'Content-Type': 'application/json' };
    if (bearer) {
        headers.Authorization = `Bearer ${bearer}`;
    }
    return headers;
}

async function ask(text) {
    // 端点由 `MIND_PERCEPT_BASE` 指定（移植后指向宿主正在用的那个大模型），
    // 没指定就退回原来的 DeepSeek 云端地址。失败自然返回 null → 退回规则。
    const base = envValue('MIND_PERCEPT_BASE').trim();
    const key = apiKey();
    if (!base && !key && !envValue('MIND_PERCEPT_KEY').trim()) {
        return null;
    }
    const local = base.length > 0;
    const payload = {
        model: local ? (envValue('MIND_PERCEPT_MODEL') || 'qwen-local') : MODEL,
        temperature: 0,
        max_tokens: 400,
        messages: [
            { role: 'system', content: PERCEPT_PROMPT },
            { role: 'user', content: `他说的：${text}\n\n只输出 JSON。` },
        ],
    };
    // 云端支持 response_format；本地 llama.cpp 用 `json_schema`（新版按它转 grammar），
    // 老版不认就退回纯提示（输出仍受 prompt 约束，parse 失败即 null）。
    if (local) {
        payload.response_format = { type: 'json_object' };
        payload.json_schema = PERCEPT_SCHEMA;
    }
    else {
        payload.response_format = { type: 'json_object' };
    }
    const body = JSON.stringify(payload);
    const controller = new AbortController();
    // 本地超时更紧：它就在隔壁，1.0–1.4 s 是实测区间；2.5 s 还没回来说明它卡住了，
    // 这时宁可早退回规则，也不要让一次回话白等 4 秒。（连接被拒是立刻失败，不会等超时。）
    const budget = local ? Math.min(TIMEOUT_MS, 2500) : TIMEOUT_MS;
    const timer = setTimeout(() => controller.abort(), budget);
    try {
        const res = await fetch(local ? base : 'https://api.deepseek.com/chat/completions', {
            method: 'POST',
            // [Shinsekai 移植] 端点可以由宿主指定（大模型），远程端点要带 key：
        //   顺序 = MIND_PERCEPT_KEY（插件从宿主配置传进来）> 原来的 DEEPSEEK_API_KEY
        headers: withAuth(local, key),
            body,
            signal: controller.signal,
        });
        if (!res.ok) {
            return null;
        }
        const data = await res.json();
        const content = String(data?.choices?.[0]?.message?.content ?? '').trim();
        const parsed = JSON.parse(content);
        const out = { model: MODEL, at: Date.now() };
        for (const field of FIELDS) {
            out[field] = parsed?.[field] === true;
        }
        // r107：subject/polarity 只许取枚举值（grammar 已经约束，这里再兜一层：写坏就当没判）
        out.subject = SUBJECTS.includes(parsed?.subject) ? parsed.subject : '无';
        out.polarity = POLARITIES.includes(parsed?.polarity) ? parsed.polarity : '中性';
        // 指代交给确定性那一层：模型判"好不好话"，规则判"在说谁"。
        // ⚠️ 这里必须传**原句**：`body` 是本函数的 HTTP body 变量，拿它去判指代会永远判成"不是她"
        //（第一版就是这个 bug：骂她 0/6、夸她 0/5，全是这一行）。
        const ref = referenceOf(text);
        // **不再要求 `subject === '她'`**：指代由 `referenceOf` 决定（它才是判"在说谁"的那一层），
        // 模型只贡献"这句话是好话还是坏话"。实测这样 39/42；要求 subject 反而掉到 37/42
        // （「七海，你这想法真是垃圾」模型把 subject 判成"别人"，但那明显是在说她）。
        // ⚠️ 归属**不在这里做**：缓存存的是"模型的原答"（subject/polarity/…），
        // 归属每次现算（`deriveOf`）。否则缓存会把当时的开关状态一起腌进去——
        // 实测踩过：开着闸跑一遍，再关闸回滚，缓存命中的还是"开着闸"的旧答案。
        out.confidence = Number(parsed?.confidence ?? 0);
        out.evidence = String(parsed?.evidence ?? '').slice(0, 24);
        // r106b：阈值 0.5 → 0.35。实测 3B 会把**判对**的句子报成 0.4（它只会对"犹豫"这类给低分），
        // 0.5 会把正确的正例也扔掉；0.35 仍能挡住"两种读法都说得通"的句子。
        return out.confidence >= 0.35 ? out : null;   // 低置信 → 退回规则（纪律②）
    }
    catch {
        return null;
    }
    finally {
        clearTimeout(timer);
    }
}

/**
 * 感知他这一句。失败/关闭 → null（调用方退回规则）。
 * @param {string} text 他这句（可带“昵称：”信封，本函数会剥）
 * @returns {Promise<object|null>}
 */
export async function perceive(text) {
    if (envValue('MIND_PERCEPT') !== '1') {
        return null;
    }
    const stub = envValue('MIND_PERCEPT_STUB').trim();
    if (stub) {
        try {
            const one = JSON.parse(stub);
            // 与真实路径同一条纪律：低置信 → null（调用方退回规则）
            return Number(one?.confidence ?? 0) >= 0.35 ? { ...one, model: 'stub', at: Date.now() } : null;
        }
        catch {
            return null;
        }
    }
    const body = String(text ?? '').replace(/^\s*[^\s：:<>]{1,16}(?:<\d+>)?\s*[：:]\s*/u, '').trim();
    if (body.length < 2) {
        return null;
    }
    const hash = createHash('sha1').update(body).digest('hex').slice(0, 16);
    const cache = readCache();
    const hit = cache[hash];
    if (hit && Number(hit.v ?? 0) === CACHE_VERSION && Date.now() - Number(hit.at ?? 0) < TTL_MS) {
        const cached = hit.result;
        if (!cached) {
            return null;
        }
        // 归属现算：缓存里可能是在另一种开关状态下写进去的
        return { ...cached, ...deriveOf(body, cached) };
    }
    const result = await ask(body);
    if (result) {
        // 归属与"单字应答"那类守卫每次现算（缓存只存模型原答）
        Object.assign(result, deriveOf(body, result));
    }
    cache[hash] = { at: Date.now(), v: CACHE_VERSION, result };
    // 只留最近 300 条
    const keys = Object.keys(cache);
    if (keys.length > 300) {
        for (const old of keys.slice(0, keys.length - 300)) {
            delete cache[old];
        }
    }
    writeCache(cache);
    return result;
}

export { existsSync as cacheExists };
