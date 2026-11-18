# 七海千秋 · 人格模型（Nanami Persona v1.4.0）

面向 **Shinsekai（>= 2.6.0）** 的插件层面**多角色可扩展人格、科学量化心理学/社会学动力学与全自动角色构建流水线**。  
它不是静态堆字数的角色设定集，而是一套**在对话运行时动态推演、且可一键从零生成完整角色包**的插件管线：从输入一个角色名自动完成“百科资料 + B 站语音 ASR 6 桶语料 + 人格壳 + L1–L8 心理/社会学参数”建模，到对话中“读话 → 心理/态度推演 → 大脑决策 → 嘴巴原声表达 → 防重复/AI 味守卫”，全程本地运行、按角色指针严格隔离、零额外 Python 第三方依赖。

- **默认内置参考角色包**：《超级弹丸论破2》「七海千秋」原典校准人格包（[profiles/nanami_chiaki/](profiles/nanami_chiaki/profile.yaml) + [soul/chiaki-soul.md](soul/chiaki-soul.md) + [corpus.tsv](profiles/nanami_chiaki/corpus.tsv)）。
- **多角色通用扩展**：内置通用心理学模板 [profiles/_template/](profiles/_template/profile.yaml)，支持**一键全自动建模**或直接从宿主角色列表（`data/config/characters.yaml`）下拉绑定，多角色同台各自隔离状态、互不串味。
- **不入侵宿主文件、纯标准库实现**：不修改宿主核心配置；运行 `toolkit/*.mjs` 需要 **Node 18+**（缺失或超时自动 `fail-open` 放行）。

---

## 一、v1.4.0：装完就能跑（环境自举与失败可见）

上一版在**纯净机器**上会踩几类坑：宿主没有系统 `ffmpeg`、宿主 Python 3.10 的 `urllib` 不跟随 308、
插件页在 iframe 里 `window.confirm` 直接返回 false、长任务在宿主重启后状态丢失。
这一版把这些问题收在插件内部，用户不需要知道 `ffmpeg` 是什么。

### 1. ffmpeg 自举（`ffmpeg_setup.py`）
解析顺序：`SHINSEKAI_NANAMI_FFMPEG` → `<数据目录>/nanami/bin` → `~/.local/bin` → `/opt/homebrew/bin` →
`/usr/bin` → `PATH` → 本机已有的 `imageio-ffmpeg` 副本 → **自动下载静态构建**。
下载源是 `eugeneware/ffmpeg-static`，镜像顺序 `gh-proxy` → GitHub，优先 `.gz`（约 18 MB，边下边解压）。
装到插件自己的数据目录，不需要系统安装、不需要管理员权限。

### 2. 环境自检与一键修复（`env_check.py`）
插件加载时打一行结论，缺件当场 WARNING；「人格模型」页顶部会出现横幅列出缺什么，
`ffmpeg` 旁边直接给「🛠 安装 ffmpeg」按钮，后台下载并实时显示进度，装好横幅自动消失。

一键构建在两个入口都会先过 `preflight`：缺 `ffmpeg` 时**不会**跑到一半才失败，
异步任务里更是会自动装好再继续，日志里能看到 `正在安装 ffmpeg：x/y MB`。

### 3. 统一网络层（`http_client.py`）
所有外网请求走同一个 opener：内置 308 处理（修掉 hf-mirror 下载 `HTTP 308`）、统一 UA 与超时、多镜像回退、带进度可取消的下载。

### 4. 插件页不再用浏览器原生弹窗
宿主把插件页嵌在 iframe 里，`window.confirm` 是平台原生实现、直接返回 `false`，
所有「带确认文案」的动作会静默中止（点了没反应）。`web/persona.html` 与 `web/mind.html`
改为页面内弹层 `askConfirm()`，并给「上一个操作还没结束」这类早退补了可见提示。

### 5. 构建任务状态落盘
任务快照写入 `<数据目录>/nanami/state/jobs.json`（保留最近 5 条，日志裁到 40 行）。
宿主重启后，上次遗留的 `running` 会被判为 `interrupted`，页面提示「上次的构建被中断了」，不再卡在「递归采集中…」。

---

## 一、v1.3.5（历史） 核心新特性

### 1. 一键全自动创建角色流水线（`auto_persona.py` + `wiki_crawler.py` + `bili_asr.py` + `corpus_builder.py`）
在「人格模型」设置页输入**角色名**与**作品名**（可选填入 B 站 BV 号），点击 **「🚀 一键全自动构建角色」** 即可自动完成：
1. **联网百科检索**：自动抓取角色背景、性格画像、人际关系与标志性细节。
2. **B 站语音与字幕递归采集**：
   - 优先直取 B 站视频字幕轨；无字幕时通过 DASH 多节点测速拉取纯音频流，并调用本机离线 ASR（**Faster-Whisper / FunASR / Vosk**）进行时间戳转写。
   - 结合 LLM 提取的《角色声纹与人设鉴别卡》，剔除旁白、对手戏角色与 Whisper 幻觉，按 `casual / curious / cold / annoyed / reject / repair_afterglow` **6 桶配额**递归深挖填满 `corpus.tsv`。
3. **自动生成全套角色资产**：写入独立指针的 `soul.md`（含场景路由段 `<!-- route: ... -->`）、守卫禁忌词、`profile.yaml` 与 L1–L8 心理/社会学量化参数。
4. **实时进度与安全取消**：前端展示 6 桶实时填充进度条与日志流，运行中支持随时点击 **「⏹ 取消构建」** 协作式安全停止。

### 2. 流水线 ASR 模型下载通道 & 本地模型挂载（`asr_downloads.py` + `web/persona.html`）
为确保无字幕 B 站视频也能开箱即用本地语音转写，设置页内置了独立的模型通道面板：
- **双通道下载 Faster-Whisper**：支持 `faster-whisper-small`（推荐 · 约 484 MB）、`medium`、`large-v3-turbo`、`large-v3`，可在**国内加速镜像（`hf-mirror.com`）**与 **HuggingFace 官方（`huggingface.co`）** 间切换，自动落盘至 `data/models/<model_id>`。
- **实时字节进度与随时取消**：展示百分比、已下载/总大小与实时速率（MB/s），支持一键 **「✕ 取消下载」** 并自动清理 `.nanami-partial` 未完成临时文件。
- **零复制挂载本机已有模型**：支持直接粘贴本机已有的 **Faster-Whisper**（含 `model.bin` 或 HuggingFace `snapshots`）、**FunASR** 或 **Vosk** 目录路径，点击 **「➕ 添加本地 ASR 模型」** 即可免复制挂载给流水线使用（支持随时移除挂载，不删除磁盘原文件）。

### 3. 科学量化的「态度转变模型」与离开沉默权限（`mind.mjs` + `social_page.py`）
将**短时情绪波动**与**对当前用户的长时态度印象**严格分离，彻底消除“前脚刚骂完、一哄就变脸成好朋友”的违和感：
- **四维长时态度状态（`state.attitude`）**：
  - `affectiveValence`（-1 ~ +1 好感度）：引入社会心理学负面偏倚（Negativity Bias），冒犯扣分力度远高于普通示好。
  - `respectTrust`（0 ~ 1 尊重与心理安全感）：受辱骂、居高临下说教、窥私或踩核心痛点时骤降，冷静期内零恢复，并同步驱动 `state.trust`。
  - `perceivedSincerity`（0 ~ 1 诚意感知）：遇到敷衍道歉、道德绑架或元探针调侃时折损，按 ELM 可信度折扣公式削弱后续单次道歉/送礼的修复效力。
  - `relationalDebt`（0 ~ 1 累积心结账本）：极慢自然衰减，保留历史冒犯记忆。
- **离开与不回应权限（`disengaged`）**：
  - 当长时态度跌破底线（或大脑在毫无意义的骚扰中主动行使 `leave` 权限）后，角色停止接话，统一只输出 `······`（避免模型被强迫硬答而暴露 AI 腔）。
  - 脱离内耗后心情（`moodV`）会稍稍缓和，但**普通闲聊、试探邀约、单次送礼不会让态度上涨**；唯有对方展现出不带索取、不敷衍、真正触动内心的诚恳态度，才会逐步破冰解除沉默并缓慢回温。

### 4. 大脑–嘴巴双层解耦表达 + 跨轮防重复门控（`voice_mouth.py`）
- **大脑（Brain）**：读取完整设定、心理/社会状态卡与对话历史，输出纯语义决策 JSON（`action` / `stance` / `intent` / `keep_facts`），将元认知消化在内部。
- **嘴巴（Mouth）**：隔离长历史，只看角色说话底色（`soul.md` / `persona_hint`）与 `corpus.tsv` 原声语料生成 6 个口语候选，经本地 AI 味/两段式反问规则过滤后，结合固定人格参数与当下状态挑出最贴合人设的一句。
- **跨轮防重复惩罚**：对近期已说过台词存在 `3-gram / 4-gram` 重叠或高频雪克隆句式撞车的候选自动扣分淘汰，避免相邻轮次复读同一句式。

---

## 二、分层心理与社会学模型（L1–L8 + S1–S3）

由 `toolkit/mind.mjs`、`toolkit/percept.mjs`、`mind_bridge.py`、`persona_state.py`、`props.py` 与 `dynamics.py` 协同驱动：

| 层级 | 负责维度 | 关键机制 |
| --- | --- | --- |
| **L1 气质与唤醒** | `effortfulControl`、`arousalBaseline` | 决定 `moodA` 稳态回归点与自我调控阻尼 |
| **L2 动机与需要** | `needProfile`、`socialEfficacy`、`surgency` | 控制初识开场门槛、自主度与社会互动倾向 |
| **L3 胜任域与自我** | `domainEfficacy`、公私自我意识、自剖门槛 | 按能力域熟练度门控胜任反应；支持框内增删能力域 |
| **L4 评价性敏感点** | `evalSensitivity`、`woundThreshold`、余波衰减 | 区分打趣、物件吐槽与冒犯；支持跨轮余波与自定义敏感点 |
| **L5 ACT 情感控制** | `act.*`（评价/效能/活跃锚点、拉平阻尼） | 计算 EPA 偏移（Deflection），约束身份一致张力 |
| **L6 动力学与语用** | `dynamics.*`（吸引盆滞回、追问预算、句幅上限） | `calm / engaged / guarded` 滞回切换与语用防膨胀 |
| **L7 依恋与浪漫** | `attachment.*`、斯腾伯格爱的三角、尊重对等锁 | 区分纯友人/心动/伴侣阶段，居高临下施压时锁死浪漫推进 |
| **L8 社会动机** | 亲和/成就/权力/侵犯/规范遵循 | 驱动社会防线与互动姿态切换 |
| **S1–S3 社会学 & 态度模型** | 人际吸引/斥力、冷静期、态度转变与离开权限 | 量化追踪好感、尊重、诚意、心结与 `disengaged` 状态 |

---

## 三、前端控制面板（设置中心三页）

安装并启用后，Shinsekai 设置中心提供以下三页，**保存后按 mtime 热加载，无需重启应用**：

1. **「人格模型」（`persona_settings`，自定义页面 `web/persona.html`）**
   - 角色指针管理、宿主角色列表下拉绑定、模板新建/复制/删除。
   - **一键全自动构建角色**（6 桶实时进度 + 随时取消构建）与手工语料/文件导入。
   - **流水线 ASR 模型下载通道**（HF-Mirror / 官方双通道下载 Faster-Whisper、进度条与取消下载、挂载/移除本机已有 ASR 模型）。
   - `soul.md` 注入、神情立绘、称呼记忆、守卫与判官锚点、Node 运行环境配置。
2. **「心理模型」（`mind_settings`，独立页面 `web/mind.html`）**
   - 右上角快速切换/自动绑定宿主角色。
   - **✨ LLM 推填人格模型**（由宿主大模型按 `soul.md` 自动推填 L1–L8 参数、能力域与敏感点）。
   - L1–L8 全量量化参数调节、L3/L4 框内动态增删、当下状态实时只读监控。
3. **「社会学模型」（`social_settings`，独立页面 `web/social.html`）**
   - 调节 S1 人际吸引/排斥、S2 关系跃迁/消气速率/冷静期、S3 防御阈值与共享 L8 社会动机。
   - 实时只读查看 `netAttraction`、`relStage`、`coolOffTurns`、`attitudeStance`、`attitudeScore`、`affectiveValence`、`respectTrust`、`perceivedSincerity`、`relationalDebt` 与 `disengaged`（是否已离开）。

---

## 四、安装方式

| 方式 | 操作说明 |
| --- | --- |
| **Shinsekai 插件市场** | 在宿主插件页搜索「七海千秋 · 人格模型」，点击安装 |
| **从 GitHub URL 安装** | 填入仓库地址 `https://github.com/Roche114514/Nanami_persona`，宿主将拉取仓库并直接以仓库根目录作为 `plugins/nanami_persona/` |
| **手动安装** | 下载 Release 中的 `nanami_persona-<version>.zip` 解压到 `plugins/`，确保存在 `plugins/nanami_persona/plugin.json`，并在 `data/config/plugins.yaml` 中启用： |

```yaml
- entry: plugins.nanami_persona.plugin:NanamiPersonaPlugin
  enabled: true
```

安装完成后重启 Shinsekai 即可生效。需要 **Node.js 18+**（可在环境变量 `SHINSEKAI_NANAMI_NODE` 或设置页 `mind.node_exe` 中显式指定路径）。

---

## 五、仓库结构

```text
.
├── __init__.py
├── plugin.json                # 市场元数据（v1.4.0）
├── plugin.py                  # 主入口：NanamiPersonaPlugin（priority=40）
├── logo.png                   # 插件图标
├── auto_persona.py            # 一键全自动角色建模流水线（百科 + B站ASR + 6桶 + 模型生成）
├── wiki_crawler.py            # 角色百科资料检索与清洗
├── bili_asr.py                # B站视频检索、字幕直取、DASH音轨下载与离线ASR转写
├── asr_downloads.py           # 流水线 Faster-Whisper 双通道下载、取消与本地ASR模型挂载
├── corpus_builder.py          # 6桶语料鉴真构建与声纹鉴别卡
├── acg_corpus.py              # 6桶情境少样本语料采样
├── voice_mouth.py             # 大脑-嘴巴双层解耦表达、原声选角与跨轮防重复惩罚
├── mind_autofill.py           # LLM 按 soul.md 自动推填 L1–L8 参数
├── profiles.py / profiles_page.py / web/persona.html   # 多指针管理、全自动建模与ASR下载前端
├── mind_params.py / mind_page.py / web/mind.html       # L1–L8 心理模型可视化页面
├── social_params.py / social_page.py / web/social.html # S1–S3 社会学与态度模型页面
├── mind_bridge.py / persona_state.py / props.py / dynamics.py
├── guard_bridge.mjs / judge.py / names.py / soul_route.py / trust.py
├── profiles/                  # 内置角色包（nanami_chiaki + _template）
├── soul/chiaki-soul.md        # 七海千秋原典人格壳
├── toolkit/                   # persona-mind 运行时（mind.mjs / percept.mjs / tic-guard.mjs）
├── .github/workflows/release.yml
├── LICENSE / THIRD-PARTY.md / DISCLAIMER.md
└── README.md
```

---

## 六、本地读写、隐私边界与兼容性

- **本地数据目录**：运行状态保存在 `<数据目录>/nanami/`（如 `settings.yaml`、`pointers.yaml`、`profiles/`、`state/mind.json`、`state/traits.json`、`state/social_traits.json`），ASR 模型下载与本地挂载登记位于 `data/models/`；升级插件不会覆盖用户数据。
- **网络与隐私**：不采集、不上报任何遥测数据；联网请求仅发生在用户主动触发「一键全自动构建」（百科/B站）、手动下载 ASR 模型或启用 LLM 感知/判官时。
- **失败保护（Fail-Open）**：未安装 Node、Node 脚本超时或网络异常时，相关环节自动降级跳过，不阻塞宿主正常对话与语音流程。
