// 守卫桥：把工具箱里的 tic-guard（字面规则 + 离声词表）包成一个 stdin/stdout 的 JSON 命令。
//
// 为什么要有这一层（而不是在 Python 里重写一遍规则）：
//   规则只有一份真相——`toolkit/tic-guard.mjs`。搬一份到 Python 里，两边就会渐渐不一致，
//   而"不一致"在这一层表现为"她的语气悄悄变了"，是最难查的那种 bug。
//
// 用法（由 plugins/nanami_persona/mind_bridge.py 调用）：
//   node guard_bridge.mjs <toolkit_dir>  <  in.json  >  out.json
//
// 入参：{"texts": ["一句", "另一句"], "skip": ["原样保留的句子"]}
// 出参：{"ok": true, "texts": [...], "fixes": [{"index":0,"before":"…","after":"…","tics":["dash"]}]}
//
// 纪律：
//   · 只减不加：cleanTics 自己保证「改坏了就原样返回」，这里不再二次加工；
//   · 失败就照原样交回去（exit 0 + ok:false），绝不因为守卫挂掉把台词吞掉。
import { pathToFileURL } from 'node:url';

const toolkitDir = String(process.argv[2] ?? '').trim();
if (!toolkitDir) {
    process.stdout.write(JSON.stringify({ ok: false, reason: 'missing toolkit dir', texts: [] }) + '\n');
    process.exit(0);
}

function readStdin() {
    return new Promise((resolve) => {
        let raw = '';
        process.stdin.setEncoding('utf8');
        process.stdin.on('data', (chunk) => {
            raw += chunk;
        });
        process.stdin.on('end', () => resolve(raw));
        process.stdin.on('error', () => resolve(raw));
    });
}

const raw = await readStdin();
let payload = {};
try {
    payload = JSON.parse(raw || '{}');
}
catch {
    process.stdout.write(JSON.stringify({ ok: false, reason: 'bad json', texts: [] }) + '\n');
    process.exit(0);
}

const texts = Array.isArray(payload.texts) ? payload.texts.map((one) => String(one ?? '')) : [];
const skip = new Set((Array.isArray(payload.skip) ? payload.skip : []).map((one) => String(one ?? '')));

let cleanTics = null;
try {
    const modulePath = `${toolkitDir.replace(/[\\/]+$/u, '')}/tic-guard.mjs`;
    const mod = await import(pathToFileURL(modulePath).href);
    cleanTics = mod.cleanTics;
}
catch (err) {
    process.stdout.write(JSON.stringify({
        ok: false,
        reason: `import tic-guard failed: ${String(err?.message ?? err)}`,
        texts,
    }) + '\n');
    process.exit(0);
}

const out = [];
const fixes = [];
for (let index = 0; index < texts.length; index += 1) {
    const source = texts[index];
    if (skip.has(source) || source.trim() === '') {
        out.push(source);
        continue;
    }
    let result;
    try {
        result = cleanTics(source);
    }
    catch (err) {
        out.push(source);
        continue;
    }
    const cleaned = typeof result?.text === 'string' ? result.text : source;
    out.push(cleaned);
    if (cleaned !== source) {
        fixes.push({ index, before: source, after: cleaned, tics: result?.tics ?? [] });
    }
}

process.stdout.write(JSON.stringify({ ok: true, texts: out, fixes }) + '\n');
