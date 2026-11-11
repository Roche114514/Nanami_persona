# 工具箱快照（vendor）

这里的 `.mjs` 是 `persona-mind` 的**只读快照**，由一个同步脚本复制而来；
**不要在 Shinsekai 里直接改这些文件**——要改就改上游，再重新同步一次：

```bash
./runtime/python.exe tools/nanami/vendor_toolkit.py
```

- 上游：`persona-mind` 工具箱（开发机上的路径由同步脚本的参数决定，不写进这里）
- 同步清单：13 个文件

## 同步过的文件（sha256 前 16 位 = 上游副本的指纹）

| 文件 | 指纹 |
| --- | --- |
| `mind.mjs` | `583183382d5052d0` |
| `commitments.mjs` | `084cefd437eb62c4` |
| `lib-stardew.mjs` | `292a38fea2b196dc` |
| `activation.mjs` | `64bfae72af0368ac` |
| `cost.mjs` | `e380801f14de5368` |
| `chat-key.mjs` | `3f624b2a4013a39e` |
| `canon-memory.mjs` | `ff901704ca48c593` |
| `percept.mjs` | `a33b8b1688e03c6b` |
| `expression.mjs` | `367d2b3646f8d7d5` |
| `tic-guard.mjs` | `f91e0215a9dcebf3` |
| `off-voice.mjs` | `0de58f8c269d29e4` |
| `state/off-voice.json` | `1e3eefe478910e44` |
| `state/expression-sprites.json` | `6179d4ae729728ce` |

## 本地改写（只改路径，不改人格逻辑）

- mind.mjs: 去掉那处硬编码的状态根（改成跟 MIND_ROOT 走）
- off-voice.mjs: 离声词表改用插件自带的副本（仍可用环境变量指向别处）
- expression.mjs: 立绘目视描述改用插件自带的副本
- canon-memory.mjs: 原典表路径改为可覆盖（缺文件时自检返回 ok:false，不影响运行）
- percept.mjs: 感知缓存跟状态根走
- percept.mjs: 感知用的 key 文件跟状态根走
- commitments.mjs: 承诺账的默认根目录不再是某台机器的绝对路径
- state/expression-sprites.json: 去掉 `source` 里的本机路径
- percept.mjs: 感知用的 key：进程环境优先于 state/.env
- percept.mjs: 感知缓存落盘前先建目录（否则第一次写就静默失败，缓存永远不生效）
- percept.mjs: 缓存落盘需要 mkdirSync / dirname
- percept.mjs: percept 能带着宿主那家的 key 去问大模型（原来只认无鉴权的本地端点）
- percept.mjs: percept 的端点与 key 都可由宿主注入（本地只是其中一种端点）

## 没带走的东西

- `ai-flavor.mjs` / `lead-trim.mjs`：都要额外的判官端点（本地 3B 或云端），
  先不接；需要时按 `docs/NANAMI-PORT.md` 的「守卫三档」一节再加。
- `docs/canon-src/` 语料（`scenes-chiaki.tsv` 等）：只被自检函数读，运行不需要。
- `percept.mjs` 的感知外包（`MIND_PERCEPT=1`）默认关闭，打开需要一个 OpenAI 兼容端点。

