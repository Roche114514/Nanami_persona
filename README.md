# 七海千秋 · 人格模型

给 Shinsekai 的某个角色配一副"心理模型"：每轮算一张【此刻】状态卡插进提示词，发送前清掉 AI 味，
并且不知道你叫什么——直到你自己说。

它不是"一个更长的角色设定"。它是一层**在运行时工作**的东西：读你这句话 → 算她此刻想干什么 →
插进提示词 → 检查她说出口的样子 → 记住你是谁。

---

## 一、工作流程：一次对话里它到底做了什么

```
你发一句话
   │
   ├─[A] before_chat 钩子（每轮一次）
   │     ├─ 算「此刻」卡：把你这句 + 上一轮状态喂给 toolkit/mind.mjs（Node）
   │     │     → 精力 / 我想要 / 这一轮我只想 / 心里
   │     ├─ 插人格壳：soul/chiaki-soul.md（若提示词里已有一份就跳过，不叠第二份）
   │     └─ 拼一块塞进提示词（独立 system 消息，紧跟开场提示词之后）：
   │            【此刻】
   │            - 身上：精力 0.71（精神好）
   │            - 我想要：把我这件事说给他听
   │            - 这一轮我只想：我先开口
   │            - 心里：他问我在干什么，我乐得说
   │            （以上是状态，不是台词；你不知道对面是谁…）
   │            （「这一轮我只想」落地成句子时是这样：应一声＝几个字…顶回去＝一句否就停…）
   │            （第一轮才追加：这一轮不用问句）
   │
   ├─[B] 大模型生成（用你自己在 Shinsekai 里配的模型；插件不改采样参数、不改工具）
   │
   ├─[C] 回复被解析成一条条气泡 → 宿主的 dialog media 链
   │     └─ 本插件的 handler 先接住她的气泡，**只减不加**地过一遍：
   │          ① 删后置尾巴：「我爸爸也写过一点，益智游戏那种。」→「我爸爸也写过一点。」
   │          ② 删公式化共情的"盖章"：「啊……我也有过，X」→「X」
   │          ③ 记日志（系统句／登记表腔——只观测，不改词）
   │          ④ 删句首垫字「嗯，」（正则触发 + 大模型判；默认关）
   │          ⑤ 字面规则：破折号、离声腔、衬字、离声词表（纯本地，不花模型）
   │          ⑥ 判断"这一条要不要发"：
   │               整条只是"我懂"／"我也有过"     → 不发
   │               第一轮就盘问他"你在做什么"      → 不发
   │               AI 味（clean 档，大模型判）      → 不发（**第一条永远留着**，避免整轮哑掉）
   │     └─ 然后把消息交给「其它插件处理器 + 宿主内置链」：选图 / 立绘 / TTS / 上屏
   │
   └─[D] 下一轮 before_chat 的开头
         ├─ 回写：把上一轮她说过的话喂给 `mind.mjs --her`——状态才会因为"她说了什么"而变
         └─ 判官（report 档）：后台判这一轮像不像 AI 味，只记日志
```

### 三层各管什么

| 层 | 管什么 | 谁算的 | 在哪 |
| --- | --- | --- | --- |
| 人格壳 | 她是谁、怎么说话、什么不做（含原典读数与反面清单） | 静态文档，运行时注入 | `soul/chiaki-soul.md` |
| 心理模型 | 这一轮她想干什么（精力／想要／只做什么／心里什么感觉） | `mind.mjs`（Node，纯规则 + 状态，**不调模型**） | `toolkit/` + `mind_bridge.py` |
| 守卫 + 判官 | 说出口之前：形状对不对、像不像客服 | 字面规则本地；语义问**你配的大模型** | `guard_bridge.mjs` + `judge.py` |

**最关键的一条分工**：心理模型只决定"她**想**干什么"，**不决定"这句话长什么样"**。
把"想干什么"翻成"一句话的形状"，靠的是卡片尾那几行说明——这一段缺了，她会滑回
「我懂」「我也常常这样」这种客服式共情（实测过：消融实验里"带卡"那一组比"不带卡"还像客服，
原因就是只给了意图、没给形状）。

### 卡片上那五行是什么

| 行 | 含义 | 它影响什么 |
| --- | --- | --- |
| 身上 | 精力值与"精神好／有点累／快撑不住" | 句子的长度和收尾（累的时候话更短、更容易停住） |
| 我想要 | 这一轮她想要的东西（说自己的事／把话问清楚／安静一会儿） | 内容的落点 |
| 这一轮我只想 | 行为词：应一声就好／顶回去／反问／断言／跑题拆结构／半句 | 句子的**形状**（配合卡片尾的映射表） |
| 心里 | 一句心情 | 语气与留白 |

### 读到 / 写到的文件

| 路径 | 谁写 | 存什么 |
| --- | --- | --- |
| `<数据目录>/nanami/settings.yaml` | 首次运行自动复制一份 | **这份才生效**（升级插件不会冲掉） |
| `<数据目录>/nanami/state/mind.json` | `mind.mjs` | 她的状态：精力、账、承诺、记忆 |
| `<数据目录>/nanami/state/person.json` | 插件 | 「他叫什么」 |
| `<数据目录>/nanami/state/judge-cache.json` | 判官 | 判过的句子（6 小时，最多 600 条） |
| `<数据目录>/nanami/state/percept-cache.json` | `percept.mjs` | 读句缓存（6 小时） |

`<数据目录>` 就是宿主的项目数据目录（通常是 Shinsekai 安装目录下的 `data/`）。

### 失败一律放行（fail-open）

| 出问题 | 后果 |
| --- | --- |
| 没装 Node / 找不到 node | 心理模型与字面守卫这一层不参与，**聊天照常** |
| `mind.mjs` 超时或报错 | 这一轮不带卡，别的都不受影响 |
| 判官超时 / 连不上 | 这一条照发；日志写"没判出来"，**不会冒充"干净"** |
| 人格壳读不到 | 只记一条 warning，其余照常 |
| 守卫的"下游链"建不起来 | 不接管，直接把消息让给宿主内置链 |

### 一次真实回合的耗时（实测，DeepSeek）

```
读句 percept        0.9 – 2.0 s   ＊可选（mind.percept，默认开，每轮多一次调用）
算卡 mind.mjs       0.1 – 0.3 s
大模型生成          2 – 4 s
发送前守卫          < 1 ms（字面）  /  0.5 – 1 s（clean 档要判 AI 味）
```

`clean` 档只判"这一轮已经发过一条之后"的气泡，第一条不判也不丢——省钱，也避免她整轮哑掉。
嫌慢就把 `judge.ai_flavor` 改成 `report`（只记日志，后台判，不占回复时间）。

### 它不改宿主的东西

- 不改 `data/config/characters.yaml`、不改 `data/character_templates/*.txt`；
- 不接管 TTS：气泡交给宿主内置媒体链，语音用宿主自己的 `runtime.tts_manager`；
- 只补两条输出契约：`dialog` 允许 1–2 条、单句台词 ≤ 34 字（都在 `settings.yaml` 里可调）。

---

## 二、它做什么（速查）

| 层 | 做什么 | 在哪 |
| --- | --- | --- |
| ① 人格壳 | 她是谁、怎么说话——**运行时注入**，盖住角色包里那份简介 | `soul/chiaki-soul.md` + `plugin.py` |
| ② 心理模型 | 每轮算一张【此刻】卡（精力／想要／只做什么／心里） | `toolkit/` + `mind_bridge.py` |
| ③ 守卫 | 台词在送去 TTS / 上屏之前过一遍，**只减不加** | `guard_bridge.mjs` + `toolkit/tic-guard.mjs` |
| 记忆 | 「他是谁」：用户说「叫我小黑」才记住，说「别叫我小黑」就忘掉 | `names.py` |
| 判官 | 读句、判 AI 味、判句首垫字——都问**你正在用的那个大模型** | `judge.py` + `toolkit/percept.mjs` |
| AI 味拦截 | 默认 `clean`：发送前判，命中就不发那条；日志分"命中／干净／没判出来"三态 | `judge.py` + `plugin.py` |
| 契约 | `dialog` 至少两个元素 → 放宽成 1–2 条，并补单句字数上限 | `plugin.py` |

## 三、安装

| 方式 | 怎么做 |
| --- | --- |
| 插件市场 | 在 Shinsekai 的插件页搜「七海千秋 · 人格模型」，点安装 |
| 从 GitHub 装 | 填仓库地址；宿主会下载本仓库 zip，把**仓库根**当成 `plugins/nanami_persona/` |
| 手动 | 下载 Release 里的 `nanami_persona-<版本>.zip`，解压到 `plugins/`，再在 `data/config/plugins.yaml` 加一行 |

```yaml
- entry: plugins.nanami_persona.plugin:NanamiPersonaPlugin
  enabled: true
```

装完重启应用。**不需要 pip 依赖**（纯标准库），需要 **Node 18+**（没有也能跑，只是少了心理模型与守卫）。

> 插件会检测提示词里是否已经有一份人格壳：有就不插第二份。所以"把她写进 `characters.yaml`"那条路仍然可用，
> 只是本仓库不再需要它。

## 四、仓库结构（根 = 插件目录，别多套一层）

```
plugin.json     市场元数据（display_name / desc / author / repo / entry / logo / tags）
README.md       本文件
LICENSE         只覆盖本项目自己的代码（见 THIRD-PARTY.md）
THIRD-PARTY.md  toolkit/ 的来源与授权状态、原作版权、依赖
DISCLAIMER.md   同人性质声明
plugin.py       入口：plugins.nanami_persona.plugin:NanamiPersonaPlugin
judge.py        判官（AI 味 / 垫字），问宿主配置的大模型
mind_bridge.py  跑 toolkit/mind.mjs，算【此刻】卡
names.py        「他是谁」的记忆
settings.py / settings.yaml   设置与默认值
guard_bridge.mjs              守卫的字面规则桥
soul/chiaki-soul.md           人格壳（运行时注入）
toolkit/        persona-mind 的只读快照（**不要直接改**，见 toolkit/VENDOR.md）
.github/workflows/release.yml 打 tag 自动出包 + sha256
```

## 五、配置

第一次运行会把包内的 `settings.yaml` 复制到 **`<数据目录>/nanami/settings.yaml`**，之后改那一份
（升级插件不会冲掉，因为升级只替换 `plugins/nanami_persona/`）。保存即生效（按 mtime 热加载）：

| 键 | 默认 | 作用 |
| --- | --- | --- |
| `character_name` | 七海千秋 | 给哪个角色用（要和 `characters.yaml` 里的 `name` 一致） |
| `mind.enabled` | true | 心理模型总开关（关掉就不带卡） |
| `mind.percept` / `mind.percept_timeout` | true / 2.5 | 读句交给大模型；每轮多一次调用 |
| `mind.writeback` | true | 她说完是否回写状态（关掉状态就不会因她而变） |
| `guard.enabled` / `guard.mode` | true / clean | 守卫总开关；`report` 只记日志不改词 |
| `guard.lead_trim` | false | 句首「嗯，」是垫字就删（大模型判，只删逗号式） |
| `judge.ai_flavor` | clean | `off` / `report`（只记日志）/ `clean`（命中不发） |
| `judge.timeout` | 4.0 | 单次判定预算；超时按"人话"放行 |
| `memory.learn_user_name` | true | 要不要从他自己的话里学称呼 |
| `memory.address_suffix` | 同学 | 她叫他的形状 = 名字 + 这个后缀（原典里她对谁都叫「X同学」）；设成 `""` 就只叫名字 |
| `memory.address_honorific` | 先生 | 他明确不是同学时的那一档。**出口是中文写法**（日语 -さん → 先生）；他说过自己是女生就用小姐；「酱」不自动升级，只在他自己要的时候用。这一档是设计，不是原典 |
| `memory.address_gap_turns` | 8 | 两次叫他之间至少隔几轮。原典里带称呼的台词只占 4.58%（约 22 行一次）、十段发言九段不叫；**调小她会变成每轮都喊** |
| `contract.max_dialog_items` / `max_speech_chars` | 2 / 34 | 气泡条数与单句长度上限 |
| `persona.inject` / `persona.shell_path` | true / 空 | 注入人格壳；想换成自己写的壳就填绝对路径 |

## 六、兼容性与边界

- Shinsekai **2.6.0** 及以上（用 `plugin_system` + `sdk.hooks` 这套接口）；
- Windows / macOS / Linux 都行（node 会在 PATH、`SHINSEKAI_NANAMI_NODE` 和常见安装位置里找）；
- 只在本机跑，不联网同步任何数据；唯一的网络请求是**宿主已经配好的那个大模型**。

## 七、发版

```bash
# 改 plugin.json 里的 version
git tag v1.1.0 && git push origin v1.1.0
```

工作流会打出 `nanami_persona-1.1.0.zip`（唯一顶层目录就是 `nanami_persona/`），
并把 **sha256** 与 **字节数** 写进 Release 说明——上架要的就是这两个数。

## 八、许可与出处

- 本项目自己的代码：见 `LICENSE`；
- `toolkit/` 是 `persona-mind` 的只读快照，**上游没有 LICENSE 文件**，授权状态未知——商用前请先联系上游；
- 角色与台词版权归 **Spike Chunsoft / 相关权利人**，本仓库不主张任何相关权利，请勿商用。见 `THIRD-PARTY.md`、`DISCLAIMER.md`。

## 设置页（程序里直接调）

装了本插件后，设置中心里会多一页「人格模型设置」，9 个分组 36 项，改完保存即可（按 mtime 热加载，下一轮生效）：

| 分组 | 调什么 |
| --- | --- |
| 基础 | 启用、角色名、会话前缀、执行痕迹 |
| ① 人格壳 | 是否注入原典版人格壳 |
| ② 心理模型 | 读句开关、卡片/读句/回写/守卫超时、重试、卡片插哪、回写开关 |
| ② 意图 | 启用、生成方式（rules/model）、单次预算 |
| ③ 发送前守卫 | 启用、clean/report/off、长句跳过阈值 |
| ③ 输出契约 | 气泡数上限、单气泡字数上限 |
| ③ 判官 | 启用、AI 味模式、句首垫字、单次预算 |
| 称呼与记忆 | 学名字、每轮提醒、称呼后缀、非同学那一档、两次称呼间隔 |
| 运行环境 | node 路径、工具箱目录、状态根、卡片标记行 |

保存是**按行改** `settings.yaml` 的（`settings_write.py`）：注释和键顺序都保留——
那份文件里的注释写着每条参数的原典依据，用 `yaml.safe_dump` 会一次冲掉，等于丢研究记录。
自测：`tools/nanami/persona_settings_test.py`（schema↔取值一一对应、嵌套键写入、注释不丢、坏输入不炸）。

## 心理模型是单独一页（L1–L4 的量化属性）

「人格模型设置」管接线（守卫 / 契约 / 判官 / 称呼 / 超时）；
**「心理模型」单独一页**只管这个模型自己的量化属性，按模型里那四层摆：

| 层 | 属性 |
| --- | --- |
| L1 气质 | 努力控制、基础唤醒 |
| L2 需要剖面 | 关系 / 胜任 / 安全 / 自主（low·medium·high）、社会效能、外向度 |
| L3 自我结构 | 游戏域效能、非游戏域效能、私我意识、公我意识、自我披露门槛 |
| L4 评价性担忧 | 评价性敏感、疼的阈值、三种疼的衰减系数 |
| 当下状态 | 精力 / 睡眠压力 / 心情效价·唤醒 / 三根刺 / agenda（**只读**，是每轮算出来的量） |

这些原本是 `toolkit/mind.mjs` 里的 `TRAITS` 常量，改一次要动源码。现在改成**外部覆盖表**：

```
<state_root>/state/traits.json     ← 设置页写它
mind.mjs 启动时读它，覆盖同名键（点号键也认，只认已知键）
文件不存在 = 完全按出厂常量走（行为零变化）；删掉它就回出厂
```

自测：`tools/nanami/persona_mind_page_test.py`
（分层字段、只写差异项、夹取 0–1、**真起 node 验覆盖生效**、删文件回出厂、当下状态只读）。
