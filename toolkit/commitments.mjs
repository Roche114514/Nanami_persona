// 承诺账 —— 她说出口的"说好了/等我/下次"，记成一本可核对、会到期、能反悔的账。
//
// 为什么单独一本账（2026-10-25）：
//   她以前的"约定"混在 autobio 里（只是一条素材），到期不到期、兑现没兑现，没人知道，
//   所以卡片上永远写着"她手上没有没兑现的约定"——那句话是真的，因为**从来没记过账**。
//   身份不是靠喜欢什么长出来的，是靠"答应了什么、做到了没有、欠了多久"长出来的。
//
// 为什么是**事件流**而不是一个可变的状态（2026-10-26 修）：
//   第一版把"落过账就不许改"当成了"不能读档"。主人当场指出：**不会反悔的还叫人吗。**
//   他说的对：不能读档约束的应该是"**已发生的事不许抹掉**"，不是"人不许改主意"。
//   所以账本记的是事件：opened → (cancelled → reopened →)* → kept / broken。
//   反悔 = 追加一条 cancelled（带理由）。她能改主意，但**反悔会留下痕迹、并且算一次代价**。
//   禁止的只有一种操作：把已经发生的事改成没发生（那是造假，不是反悔）。
//
//   node tools/persona-mind/commitments.mjs --list
//   node tools/persona-mind/commitments.mjs --due
//   node tools/persona-mind/commitments.mjs --summary [--json]
//   node tools/persona-mind/commitments.mjs --settle <id> --kept|--broken [--note "…"]
//   node tools/persona-mind/commitments.mjs --cancel <id> --note "改主意了，因为…"
//   node tools/persona-mind/commitments.mjs --reopen <id> --note "又想去了"
//
// 判定边界（诚实写在这里）：
//   - 到期不等于失约。到期只叫"欠着"，做没做到要显式落账。
//   - 她自己说的话里出现承诺句式才入账；"他邀她"那一路归 pendingInvite。
//   - 事件只追加、不删除；唯一的"更正"是追加一条 correction 并写明理由。

import { existsSync, mkdirSync, readFileSync, writeFileSync } from 'node:fs';
import { dirname, join } from 'node:path';

// [Shinsekai 移植] 承诺账的兜底根目录：调用方都会传 root，这里只是别指向某台机器。
const DEFAULT_ROOT = process.env.MIND_ROOT ?? 'data/nanami';
const LEDGER_NAME = 'commitments.json';
const DAY_MS = 24 * 3600 * 1000;
const DEDUPE_MINUTES = 30;
// 测试流量不许进她的账：自测的 chatKey 一律是 selftest:*／offline-*／probe*
const TEST_PEER = /^(?:selftest|offline|probe)/iu;

export const STATUS = {
    OPEN: 'open',
    KEPT: 'kept',
    BROKEN: 'broken',
    CANCELLED: 'cancelled',
    REOPENED: 'reopened',
};

// 事件类型：opened 是入账，其余都是她后来做的事
export const EVENTS = {
    OPENED: 'opened',
    KEPT: 'kept',
    BROKEN: 'broken',
    CANCELLED: 'cancelled',
    REOPENED: 'reopened',
    CORRECTION: 'correction',
};

const CLOSED = new Set([STATUS.KEPT, STATUS.BROKEN]);

// 她这句话在许什么
const PROMISE_RE = /说好了|说好了哦|答应过|答应你|我等你|等我|下次再|下次约|回头再|记得叫我|叫我一声|我陪你|一起打一局|再打一局|陪你打/u;
// 只有姿态、没有所指的空许愿：不算账
const EMPTY_PROMISE = /^(?:说好了|说好了哦|说好了喔|答应过|答应你|等我|我等你|我陪你)$/u;
// 时间状语不是"欠的那件事"。线上出过「我欠着：一会儿（说好的是 10/27）」——
// 抽取把"你等我一会儿"里的"一会儿"当成了所指，她读到卡片，就编出一段不存在的往事。
const TIME_NOISE = /^(?:一会儿|待会儿|等会儿|回头|晚点|等下|一下|一下下|的时候)$/u;

export function isNoiseWhat(value) {
    const what = String(value ?? '').trim();
    if (what === '') {
        return true;
    }
    return TIME_NOISE.test(what) || EMPTY_PROMISE.test(what) || /的时候/u.test(what);
}

const TIME_RULES = [
    { key: 'tonight', re: /今晚|今天晚上|夜里/u },
    { key: 'tomorrow', re: /明天/u },
    { key: 'dayAfter', re: /后天/u },
    { key: 'weekend', re: /这周末|周末/u },
    { key: 'nextWeek', re: /下周|下个星期/u },
    { key: 'soon', re: /回头|等会儿|待会儿|一会儿/u },
];

function pad2(value) {
    return String(value).padStart(2, '0');
}

function ledgerPath(root) {
    return join(root || DEFAULT_ROOT, 'state', LEDGER_NAME);
}

function stripBom(text) {
    return String(text ?? '').replace(/^\uFEFF/u, '');
}

// 旧版账目（单状态）迁成事件流：只追加、不改写语义
function migrate(item) {
    if (Array.isArray(item.events) && item.events.length > 0) {
        return item;
    }
    const events = [{ at: Number(item.at ?? 0), type: EVENTS.OPENED, note: '' }];
    if (item.status && item.status !== STATUS.OPEN && Number(item.settledAt ?? 0) > 0) {
        // 旧版的 dropped 就是"取消"（她那句后来不算了）；其余 kept/broken 同名
        const type = (item.status === 'dropped' || item.status === STATUS.CANCELLED)
            ? EVENTS.CANCELLED
            : item.status;
        events.push({ at: Number(item.settledAt), type, note: String(item.note ?? '') });
    }
    return { ...item, events };
}

function readLedger(root) {
    const file = ledgerPath(root);
    if (!existsSync(file)) {
        return { version: 2, items: [] };
    }
    try {
        const parsed = JSON.parse(stripBom(readFileSync(file, 'utf8')));
        if (!parsed || !Array.isArray(parsed.items)) {
            return { version: 2, items: [] };
        }
        return { ...parsed, version: 2, items: parsed.items.map(migrate) };
    }
    catch (error) {
        try {
            writeFileSync(file + '.broken', readFileSync(file, 'utf8'), 'utf8');
        }
        catch (ignored) {
            // 备份失败也不阻塞
        }
        return { version: 2, items: [] };
    }
}

function saveLedger(root, ledger) {
    const file = ledgerPath(root);
    mkdirSync(dirname(file), { recursive: true });
    writeFileSync(file, JSON.stringify(ledger, null, 2) + '\n', 'utf8');
}

function endOfDay(base) {
    const date = new Date(base);
    date.setHours(23, 59, 0, 0);
    return date.getTime();
}

// ── 派生状态：只看事件流，不改历史 ──────────────────────────────────────
export function statusOf(item) {
    const events = Array.isArray(item?.events) ? item.events : [];
    for (let i = events.length - 1; i >= 0; i -= 1) {
        const type = events[i].type;
        if (type === EVENTS.CORRECTION) {
            continue;
        }
        if (type === EVENTS.OPENED) {
            return STATUS.OPEN;
        }
        if (type === EVENTS.REOPENED) {
            return STATUS.REOPENED;
        }
        if (type === EVENTS.CANCELLED) {
            return STATUS.CANCELLED;
        }
        if (type === EVENTS.KEPT) {
            return STATUS.KEPT;
        }
        if (type === EVENTS.BROKEN) {
            return STATUS.BROKEN;
        }
    }
    return STATUS.OPEN;
}

// 她现在算不算"还欠着"：open / reopened 都算，cancelled 不算（她已经明确不做了）
function stillOwed(item) {
    const status = statusOf(item);
    return status === STATUS.OPEN || status === STATUS.REOPENED;
}

// 这条她会反悔过几次
function cancelCount(item) {
    return (item?.events ?? []).filter((one) => one.type === EVENTS.CANCELLED).length;
}

// 时间词 → 到期时刻（毫秒）。认不出就返回 null。
export function dueOf(text, now) {
    const base = Number.isFinite(now) ? now : Date.now();
    const hit = TIME_RULES.find((rule) => rule.re.test(String(text ?? '')));
    if (!hit) {
        return null;
    }
    if (hit.key === 'tonight') {
        return endOfDay(base);
    }
    if (hit.key === 'tomorrow') {
        return endOfDay(base + DAY_MS);
    }
    if (hit.key === 'dayAfter') {
        return endOfDay(base + 2 * DAY_MS);
    }
    if (hit.key === 'soon') {
        return base + 3 * 3600 * 1000;
    }
    if (hit.key === 'weekend') {
        const date = new Date(base);
        const day = date.getDay();
        const delta = (7 - day) % 7;
        return endOfDay(base + delta * DAY_MS);
    }
    return endOfDay(base + 7 * DAY_MS);
}

// 从她的话里抠出"具体是什么事"：优先「」里的，其次承诺词后面那截
export function whatOf(text) {
    const raw = String(text ?? '');
    const quoted = raw.match(/[「『]([^」』]{2,24})[」』]/u);
    if (quoted) {
        return quoted[1].trim();
    }
    const tail = raw.match(/(?:说好了|答应过|答应你|我等你|等我|下次再|下次约|回头再|记得叫我|叫我一声|我陪你|再打一局|一起打一局|陪你打)[，,、\s]*([^，。！？,.!?\n]{2,20})/u);
    if (tail) {
        return tail[1].trim();
    }
    // 兜底：这句话本身就是所指（"记得叫我""我陪你"）——承诺词自己就是那件事。
    // 但"说好了哦""等我"这种是**空许愿**：说得漂亮，欠的是什么说不清，记账没意义，丢掉。
    const self = raw.match(PROMISE_RE);
    if (self && !EMPTY_PROMISE.test(self[0].trim())) {
        return self[0].trim();
    }
    return '';
}

export function kindOf(text) {
    const raw = String(text ?? '');
    if (/说好了|答应过|答应你/u.test(raw)) {
        return 'promise';
    }
    if (/我等你|等我|我陪你/u.test(raw)) {
        return 'wait';
    }
    return 'appointment';
}

/**
 * 检测一句话里有没有承诺；有就返回一条待入账的记录（不写盘）。
 */
export function detectPromise(text, now) {
    const raw = String(text ?? '').trim();
    if (!raw || !PROMISE_RE.test(raw)) {
        return null;
    }
    const what = whatOf(raw);
    if (!what || isNoiseWhat(what)) {
        return null;
    }
    return {
        what,
        kind: kindOf(raw),
        dueAt: dueOf(raw, now),
        phrase: raw.slice(0, 40),
    };
}

function newId(now) {
    return 'c' + Number(now).toString(36) + Math.random().toString(36).slice(2, 6);
}

/**
 * 她说了一句话 → 该记账就记进去。返回新账目或 null。
 */
export function recordPromise(params) {
    const safe = params ?? {};
    const root = safe.root || DEFAULT_ROOT;
    const now = Number.isFinite(safe.now) ? safe.now : Date.now();
    // 自测／离线跑的流量不记账：她的账本里只该有她自己说过的话
    if (TEST_PEER.test(String(safe.peer ?? '')) || TEST_PEER.test(String(safe.chat ?? ''))) {
        return null;
    }
    const detected = detectPromise(safe.text, now);
    if (!detected) {
        return null;
    }
    const ledger = readLedger(root);
    const recent = ledger.items.find((item) => stillOwed(item)
        && item.what === detected.what
        && now - Number(item.at ?? 0) < DEDUPE_MINUTES * 60 * 1000);
    if (recent) {
        return null;
    }
    const entry = {
        id: newId(now),
        at: now,
        peer: String(safe.peer ?? ''),
        chat: String(safe.chat ?? ''),
        what: detected.what,
        kind: detected.kind,
        dueAt: detected.dueAt,
        phrase: detected.phrase,
        source: String(safe.source ?? 'her'),
        events: [{ at: now, type: EVENTS.OPENED, note: '' }],
    };
    ledger.items = [...ledger.items, entry];
    saveLedger(root, ledger);
    return entry;
}

/**
 * 追加一条事件。**只追加，不删除**——反悔也是追加（cancelled），不是把答应过的事抹掉。
 */
export function appendEvent(params) {
    const safe = params ?? {};
    const root = safe.root || DEFAULT_ROOT;
    const now = Number.isFinite(safe.now) ? safe.now : Date.now();
    const type = String(safe.type ?? '');
    const allowed = [EVENTS.KEPT, EVENTS.BROKEN, EVENTS.CANCELLED, EVENTS.REOPENED, EVENTS.CORRECTION];
    if (!allowed.includes(type)) {
        throw new Error('追加的事件只接受 kept / broken / cancelled / reopened / correction');
    }
    const ledger = readLedger(root);
    const target = ledger.items.find((item) => item.id === String(safe.id));
    if (!target) {
        return null;
    }
    const note = String(safe.note ?? '');
    const current = statusOf(target);
    // kept / broken 是终态：再落一次就不是"反悔"了，那叫改写事实 —— 必须走 correction
    if ((type === EVENTS.KEPT || type === EVENTS.BROKEN) && CLOSED.has(current)) {
        throw new Error('这条已经落过账；要更正请用 correction 并写明理由');
    }
    if ((type === EVENTS.CANCELLED || type === EVENTS.CORRECTION) && !note) {
        throw new Error('反悔／更正必须写理由（note）——她可以改主意，但要留下为什么');
    }
    target.events = [...(target.events ?? []), { at: now, type, note }];
    saveLedger(root, ledger);
    return target;
}

// 兼容旧调用：settle = 追加 kept / broken
export function settle(params) {
    const safe = params ?? {};
    const status = String(safe.status ?? '');
    if (status === STATUS.DROPPED || status === STATUS.CANCELLED) {
        return appendEvent({ root: safe.root, id: safe.id, type: EVENTS.CANCELLED, note: safe.note, now: safe.now });
    }
    if (status !== STATUS.KEPT && status !== STATUS.BROKEN) {
        throw new Error('settle 只接受 kept / broken（取消请用 cancel）');
    }
    return appendEvent({ root: safe.root, id: safe.id, type: status, note: safe.note, now: safe.now });
}

export function cancel(params) {
    const safe = params ?? {};
    return appendEvent({ root: safe.root, id: safe.id, type: EVENTS.CANCELLED, note: safe.note, now: safe.now });
}

export function reopen(params) {
    const safe = params ?? {};
    return appendEvent({ root: safe.root, id: safe.id, type: EVENTS.REOPENED, note: safe.note, now: safe.now });
}

function withOverdue(entry, now) {
    const status = statusOf(entry);
    const due = Number(entry.dueAt ?? 0);
    const owed = stillOwed(entry);
    return {
        ...entry,
        status,
        owed,
        cancels: cancelCount(entry),
        overdue: owed && due > 0 && now > due,
    };
}

export function listItems(params) {
    const safe = params ?? {};
    const root = safe.root || DEFAULT_ROOT;
    const now = Number.isFinite(safe.now) ? safe.now : Date.now();
    const ledger = readLedger(root);
    const peer = safe.peer ? String(safe.peer) : '';
    return ledger.items
        // source='test' 的历史条目留着可追溯，但不进她的账、不进卡片
        .filter((item) => item.source !== 'test')
        .filter((item) => (peer ? item.peer === peer : true))
        .map((item) => withOverdue(item, now));
}

export function openItems(params) {
    return listItems(params).filter((item) => item.owed === true && !isNoiseWhat(item.what));
}

export function dueItems(params) {
    return listItems(params).filter((item) => item.overdue === true && !isNoiseWhat(item.what));
}

/**
 * 汇总：欠着几条、逾期几条、兑现/失约/取消各几次、反悔过几次。
 * 卡片和（下一步的）动机都读这个，别在别处再算一遍。
 */
export function summary(params) {
    const safe = params ?? {};
    const now = Number.isFinite(safe.now) ? safe.now : Date.now();
    const items = listItems({ root: safe.root, peer: safe.peer, now })
        // 旧账里的抽取噪声不再进她的视野（--list 仍能看到原条目，供核对）
        .filter((item) => !isNoiseWhat(item.what));
    const owed = items.filter((item) => item.owed === true);
    const overdue = owed.filter((item) => item.overdue === true);
    const kept = items.filter((item) => item.status === STATUS.KEPT);
    const broken = items.filter((item) => item.status === STATUS.BROKEN);
    const cancelled = items.filter((item) => item.status === STATUS.CANCELLED);
    const lastBroken = broken.length > 0
        ? broken.reduce((left, right) => (Number(lastEventAt(left)) >= Number(lastEventAt(right)) ? left : right))
        : null;
    const lastCancelled = cancelled.length > 0
        ? cancelled.reduce((left, right) => (Number(lastEventAt(left)) >= Number(lastEventAt(right)) ? left : right))
        : null;
    return {
        open: owed.length,
        overdue: overdue.length,
        kept: kept.length,
        broken: broken.length,
        cancelled: cancelled.length,
        cancelsTotal: items.reduce((sum, item) => sum + (item.cancels ?? 0), 0),
        oldestOverdue: overdue.length > 0 ? overdue.reduce((left, right) => (Number(left.dueAt) <= Number(right.dueAt) ? left : right)) : null,
        lastBrokenAt: lastBroken ? Number(lastEventAt(lastBroken)) : 0,
        lastBrokenWhat: lastBroken ? lastBroken.what : '',
        lastCancelledAt: lastCancelled ? Number(lastEventAt(lastCancelled)) : 0,
        lastCancelledWhat: lastCancelled ? lastCancelled.what : '',
        items,
    };
}

function lastEventAt(item) {
    const events = Array.isArray(item?.events) ? item.events : [];
    return events.length > 0 ? Number(events[events.length - 1].at ?? 0) : 0;
}

/**
 * 卡片那一行（她自己的话，不是给主人的报告）。
 * 没有账就返回 null —— 别写"没有约定"这种空话。
 */
export function cardLine(params) {
    const stats = summary(params);
    if (stats.overdue > 0 && stats.oldestOverdue) {
        const due = new Date(Number(stats.oldestOverdue.dueAt));
        const clock = `${due.getMonth() + 1}/${due.getDate()}`;
        return `- 我欠着：${stats.oldestOverdue.what}（说好的是 ${clock}，一直没做到）`;
    }
    const owedItem = stats.items.find((item) => item.owed === true);
    if (owedItem) {
        const reopened = owedItem.status === STATUS.REOPENED ? '（这次又答应了一次）' : '';
        return `- 我手上还悬着：${owedItem.what}${reopened}`;
    }
    if (stats.lastBrokenWhat) {
        return `- 上次没做到的：${stats.lastBrokenWhat}`;
    }
    if (stats.lastCancelledWhat) {
        return `- 我反悔过一次：${stats.lastCancelledWhat}`;
    }
    return null;
}

// ── CLI ──────────────────────────────────────────────────────────────────
function stamp(ms) {
    const date = new Date(Number(ms));
    const clock = `${pad2(date.getHours())}:${pad2(date.getMinutes())}`;
    return `${date.getFullYear()}-${pad2(date.getMonth() + 1)}-${pad2(date.getDate())} ${clock}`;
}

function printList(items) {
    if (items.length === 0) {
        console.log('（账是空的）');
        return;
    }
    for (const item of items) {
        const mark = item.owed ? (item.overdue ? '逾期' : '欠着') : item.status;
        const due = item.dueAt ? `到期 ${stamp(item.dueAt)}` : '没设到期';
        const cancels = item.cancels > 0 ? ` / 反悔过 ${item.cancels} 次` : '';
        console.log(`[${mark}] ${item.id}  ${item.what}  —— ${item.kind} / ${due} / 记于 ${stamp(item.at)}${cancels}`);
    }
}

function printHistory(items) {
    for (const item of items) {
        console.log(`${item.id}  ${item.what}`);
        for (const event of item.events ?? []) {
            console.log(`   ${stamp(event.at)}  ${event.type}${event.note ? '  —— ' + event.note : ''}`);
        }
    }
}

function main() {
    const argv = process.argv.slice(2);
    const rootFlag = argv.indexOf('--root');
    const root = rootFlag >= 0 && argv[rootFlag + 1] ? argv[rootFlag + 1] : DEFAULT_ROOT;
    const now = Date.now();
    const noteFlag = argv.indexOf('--note');
    const note = noteFlag >= 0 ? String(argv[noteFlag + 1] ?? '') : '';
    if (argv.includes('--list')) {
        printList(listItems({ root, now }));
        return;
    }
    if (argv.includes('--due')) {
        printList(dueItems({ root, now }));
        return;
    }
    if (argv.includes('--history')) {
        printHistory(listItems({ root, now }));
        return;
    }
    if (argv.includes('--summary')) {
        const stats = summary({ root, now });
        if (argv.includes('--json')) {
            console.log(JSON.stringify(stats, null, 2));
            return;
        }
        console.log(`欠着 ${stats.open} 条（逾期 ${stats.overdue}）／做到 ${stats.kept}／没做到 ${stats.broken}`
            + `／反悔 ${stats.cancelsTotal} 次`);
        const line = cardLine({ root, now });
        console.log('卡片会写：' + (line ?? '（不写这一行）'));
        return;
    }
    const actions = [
        { flag: '--settle', type: '' },
        { flag: '--cancel', type: EVENTS.CANCELLED },
        { flag: '--reopen', type: EVENTS.REOPENED },
    ];
    for (const action of actions) {
        const at = argv.indexOf(action.flag);
        if (at < 0 || !argv[at + 1]) {
            continue;
        }
        let type = action.type;
        if (action.flag === '--settle') {
            type = argv.includes('--kept') ? EVENTS.KEPT : EVENTS.BROKEN;
        }
        try {
            const done = appendEvent({ root, id: argv[at + 1], type, note, now });
            console.log(done ? `已记下：${done.id} → ${type}` : '没找到这条账');
        }
        catch (error) {
            console.log('记不下：' + error.message);
        }
        return;
    }
    // 发送层用这个入口记账（不依赖心智层开关：账本是事实，不是人格层的产物）
    const recordFlag = argv.indexOf('--record');
    if (recordFlag >= 0 && argv[recordFlag + 1]) {
        const peerFlag = argv.indexOf('--peer');
        const peer = peerFlag >= 0 ? String(argv[peerFlag + 1] ?? '') : '';
        const entry = recordPromise({ root, peer, chat: peer, text: argv[recordFlag + 1], now, source: 'outbound' });
        console.log(entry ? `记账：${entry.what}` : '（这句没有可记账的承诺）');
        return;
    }
    console.log('用法：--list | --due | --history | --summary [--json]'
        + ' | --settle <id> --kept|--broken [--note "…"] | --cancel <id> --note "理由" | --reopen <id>');
}

const scriptName = process.argv[1] ? join(process.argv[1]).split(/[\\/]/u).pop() : '';
if (scriptName === 'commitments.mjs') {
    main();
}
