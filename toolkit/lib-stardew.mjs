// 2026-10-26：把"她在游戏里的现实"读进来（**只读，不新增判定层**）。
// 数据源：PersonaBridge mod 写的 `state/stardew.json`。
// 容错（Lead 点名）：旧版 mod 有悬空逗号（`"location":"FarmHouse",}`），读之前先修一遍；
// 解析失败就当"没有这份现实"（返回 null），**绝不抛**。
import { readFileSync } from 'node:fs';
import { join } from 'node:path';

export const STARDEW_PATH = 'state/stardew.json';
export const FRESH_MS = 15 * 60 * 1000; // 15 分钟真实时间内写过 = 她此刻就在游戏里/刚离开
export const PRESENCE_FRESH_MS = 3 * 60 * 1000;

const LOCATION_LABEL = {
    Farm: '农场',
    FarmHouse: '农舍',
    BusStop: '公交站',
    Town: '镇上',
    Beach: '海滩',
    FishShop: '鱼店',
    Mine: '矿洞',
    Mountain: '山区',
    Forest: '森林',
};

const numOrNull = (value) => {
    const one = Number(value);
    return Number.isFinite(one) ? one : null;
};

/** 读一次；没有/坏了/字段缺 → 相应字段为 null，绝不抛。 */
export function readStardew(root = '.', now = Date.now()) {
    try {
        const path = join(String(root ?? '.'), STARDEW_PATH);
        const raw = String(readFileSync(path, 'utf8')).replace(/^\uFEFF/u, '');
        // 悬空逗号：`{"a":1,}` / `{"a":[1,2,]}` 都修掉
        const fixed = raw.replace(/,\s*([}\]])/gu, '$1');
        const one = JSON.parse(fixed);
        const at = numOrNull(one?.writtenAt);
        return {
            fresh: at !== null && now - at < FRESH_MS,
            day: numOrNull(one?.day),
            season: typeof one?.season === 'string' ? one.season : null,
            time: numOrNull(one?.time),
            money: numOrNull(one?.money),
            stamina: numOrNull(one?.stamina),
            location: typeof one?.player?.location === 'string' ? one.player.location : null,
            x: numOrNull(one?.player?.x),
            y: numOrNull(one?.player?.y),
            cropsNeedWater: numOrNull(one?.cropsNeedWater),
            at,
        };
    }
    catch {
        return null;
    }
}

/** 「农场那边还有 N 块地等着浇水」——只有 `fresh` 且真的有待浇的地才成立。 */
export function stardewWaterLine(state, now = Date.now()) {
    const game = readStardew(state?.root ?? '.', now);
    if (!game || game.fresh !== true) {
        return '';
    }
    const waiting = numOrNull(game.cropsNeedWater);
    if (waiting === null || waiting <= 0) {
        return '';
    }
    return `农场那边还有 ${Math.round(waiting)} 块地等着浇水`;
}

/** 游戏真的开着时，让她在卡片上看见自己已经进了哪张图。 */
export function stardewPresenceLine(state, now = Date.now()) {
    const game = readStardew(state?.root ?? '.', now);
    if (!game || game.at === null || now - game.at > PRESENCE_FRESH_MS) {
        return '';
    }
    const where = LOCATION_LABEL[game.location] ?? game.location;
    if (!where) {
        return '';
    }
    const time = String(Math.max(0, Math.floor(game.time ?? 0))).padStart(4, '0');
    const clock = `${time.slice(0, 2)}:${time.slice(2)}`;
    const day = game.day > 0 ? `第 ${Math.round(game.day)} 天 ` : '';
    return `星露谷已经开着，人在${where}，游戏内${day}${clock}`;
}
