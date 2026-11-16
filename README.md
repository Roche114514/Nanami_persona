# 七海千秋 · 人格模型（Nanami Persona v1.3.0）

面向 **Shinsekai（>= 2.6.0）** 的插件层面**多角色可扩展人格与心理动力学拓展**。  
它不是静态堆字数的角色设定集，而是一套**在对话运行时动态工作**的插件管线：每轮先读话、再推演心理状态与行为动力学、按场景路由注入人格壳、发送前过滤 AI 味与客服腔，并在用户自报名字后记住称呼规律。

- **默认内置参考角色包**：《超级弹丸论破2》「七海千秋」原典校准人格包（[profiles/nanami_chiaki/](profiles/nanami_chiaki/profile.yaml) + [soul/chiaki-soul.md](soul/chiaki-soul.md)）。
- **多角色通用扩展**：内置通用心理学模板 [profiles/_template/](profiles/_template/profile.yaml)，支持在前端直接**从宿主已添加的角色列表（`data/config/characters.yaml`）下拉选择角色包完成绑定**（例如七海千秋、哈贝特洛特等，无需手动输入），多角色同台各自隔离状态、互不串味。
- **不入侵宿主文件、无额外 Python 依赖**：不修改 `data/config/characters.yaml` 与 `data/character_templates/*.txt`；纯 Python 标准库实现，运行 `toolkit/*.mjs` 需要 **Node 18+**（缺失或超时均自动 `fail-open` 放行）。

---

## 一、v1.3.0 核心架构

### 1. 多角色指针与宿主角色包绑定（`pointers.yaml` + `profiles.py`）
- **指针隔离**：每个角色指针拥有独立的 `profile.yaml`、`soul.md`、`traits.json` 与运行时状态目录（默认主指针 `nanami_chiaki` 向后兼容 `<数据目录>/nanami/`，其余指针隔离存放在 `<数据目录>/nanami/profiles/<id>/runtime/`）。
- **宿主角色列表下拉联动**：设置页自动读取宿主 `data/config/characters.yaml` 中已添加的角色，无需手敲角色名即可下拉切换或新建绑定；从空白模板绑定宿主角色时，会自动同步宿主设定概要与模板场景路由段到独立 `soul.md`。
- **同台路由**：在 `init_chat` / `before_chat` / `DialogHandler` 中按发言角色名精确分流不同角色的记忆、状态下发、输出契约、禁用词表与判官基线。

### 2. L1–L6 分层心理模型 + ACT 情感控制 + 动力学中间件
由 `toolkit/mind.mjs`、`toolkit/percept.mjs`、`mind_bridge.py`、`persona_state.py`、`props.py` 与 `dynamics.py` 协同驱动：

| 层级 | 负责维度 | 关键机制 |
| --- | --- | --- |
| **L1 气质与唤醒** | `effortfulControl`、`arousalBaseline` | 决定 `moodA` 稳态回归点与自我调控阻尼，高唤醒慢回归、低唤醒轻平淡 |
| **L2 动机与需要** | `needProfile`（关系/胜任/安全/自主）、`socialEfficacy`、`surgency` | 控制初识开场门槛、自主度与社会互动倾向 |
| **L3 胜任域与自我** | `domainEfficacy`、`privateSelfConsciousness`、`publicSelfConsciousness`、`selfDisclosureThreshold` | 按能力域熟练度门控「胜任被邀请 / 上场排错」倾向；信任未过门槛时收敛自剖；支持框内自定义增删能力域 |
| **L4 评价性敏感点** | `evalSensitivity`、`woundThreshold`、三类敏感点衰减系数 | 区分自我归因、物件吐槽、友善打趣与真正冒犯；支持跨轮情绪余波（`appraisalCarry`）与道歉修复加速消退；支持框内自定义增删敏感点 |
| **L5 ACT 情感控制** | `act.*`（评价/效能/活跃锚点、敏感权重、过捧拉平阻尼） | 按性格锚点计算当前情景 EPA 偏移（Deflection），提供紧凑的身份校正张力与不夸大承诺边界 |
| **L6 动力学与语用节律** | `dynamics.*`（吸引盆滞回阈值、类比图式许可度、连续追问预算、省略号软上限、句幅上限） | 维护 `calm / engaged / guarded` 三态吸引盆滞回切换；对礼物按偏好域分化（`收到心头好` / `收到陌生礼物` / `被在意`）；通过话轮滑窗抑制连环反问与省略号滥用 |

### 3. 运行时完整处理流程

```text
用户发来一句话
   │
   ├─[A] before_chat 钩子
   │     ├─ 回写上一轮她说的话（mind.mjs --her）
   │     ├─ 读句 percept.mjs（可选，参数化角色名，识别冒犯 / 求助 / 道歉 / 送礼 / 打趣）
   │     ├─ 算卡 mind.mjs + 第 9 层意图 props.py + dynamics.py（产出精简三槽动力学约束）
   │     ├─ 按场景按需路由注入人格壳（soul_route.py：<!-- route: ... -->）
   │     └─ 追加独立【此刻】/【此刻｜角色名】状态卡与动作形状映射（action_shape_lines）
   │
   ├─[B] 宿主调用你配置的大模型生成回复
   │
   └─[C] DialogHandler 发前只减不加守卫 → 交给宿主立绘 / TTS / 上屏
         ├─ guard_bridge.mjs + tic-guard.mjs（纯本地字面规则、原典零命中禁忌词）
         └─ judge.py（按角色锚点调大模型判断 AI 味 / 客服腔，clean 档首条永保、其余命中丢弃）
```

---

## 二、前端控制面板（设置中心两页）

安装并启用后，Shinsekai 设置中心新增以下两页，**保存后按 mtime 热加载，无需重启应用**：

### 1. 「人格模型」（`persona_settings`，已融合角色指针管理）
- **全局开关与接线**：插件总开关、`persona.inject`、每轮追踪日志 `log_trace`。
- **角色指针切换与宿主绑定**：
  - 下拉切换当前编辑的角色指针（`nanami_chiaki` 或自定义指针）；
  - 从**宿主角色列表下拉框**选择已添加的角色（如 `哈贝特洛特`），配合底部动作按钮「从空白模板新建角色 / 复制当前角色为新指针 / 移除自定义指针」一键绑定；
  - `目标角色（绑定宿主角色包）` 下拉直接改绑已有指针对应的宿主角色，无需手动输入。
- **单角色人格壳与语言出口**：`soul.md` 路径与注入模式（`auto` / `all`）、立绘神情状态注入、单轮气泡数与字数上限、称呼后缀 / 敬称 / 间隔轮数、守卫与判官模式及单角色判官锚点提示词。
- **心理开关与运行环境**：`mind.percept`、各类超时预算（秒）、`node_exe`、`toolkit_dir` 与全局默认状态根。

### 2. 「心理模型」（`mind_settings`，独立 HTML 可视化页 `web/mind.html`）
- **右上角「目标角色」下拉**：快速在已有角色指针间切换，或直接点选尚未绑定指针的宿主角色（自动新建模板包并切换）。
- **L1–L6 全 28 项量化参数可调**：每项均附中文说明与出厂参考值范围；修改仅以增量覆盖表写入 `<state_root>/state/traits.json`，删除覆写即可恢复出厂值。
- **L3 / L4 框内增删能力域与敏感点**：每个卡片顶部先命名展示维度名称、紧随具体数值，支持点击卡片内「**＋ 增加能力域**」「**＋ 增加敏感点**」动态增删条目与正则触发词，实时写入对应角色 `profile.yaml`。
- **当下状态（只读）**：展示最近活跃会话中实时推导的精力、睡眠压力、心情效价/唤醒、`dynamicsBasin` 吸引盆状态、ACT 偏移度等动态指标。

---

## 三、安装方式

| 方式 | 操作说明 |
| --- | --- |
| **Shinsekai 插件市场** | 在宿主插件页搜索「七海千秋 · 人格模型」，点击安装 |
| **从 GitHub URL 安装** | 填入仓库地址 `https://github.com/Roche114514/Nanami_persona`，宿主将拉取仓库并直接以仓库根目录作为 `plugins/nanami_persona/`（仓库根即插件包根，无多余嵌套目录） |
| **手动安装** | 下载 Release 中的 `nanami_persona-<version>.zip` 解压到 `plugins/`，确保存在 `plugins/nanami_persona/plugin.json`，并在 `data/config/plugins.yaml` 中启用： |

```yaml
- entry: plugins.nanami_persona.plugin:NanamiPersonaPlugin
  enabled: true
```

安装完成后重启 Shinsekai 即可生效。需要 **Node.js 18+**（可在环境变量 `SHINSEKAI_NANAMI_NODE` 或设置页 `mind.node_exe` 中显式指定路径）。

---

## 四、仓库结构（严格对齐 Shinsekai 插件规范）

```text
.
├── __init__.py
├── plugin.json                # 市场元数据（display_name / desc / author / repo / entry / logo / tags / version）
├── plugin.py                  # 主入口：NanamiPersonaPlugin（继承 PluginBase，priority=40）
├── logo.png                   # 插件图标
├── settings.py / settings.yaml / settings_write.py   # 全局设置读写与保注释按行写回
├── profiles.py                # PointerRegistry 多角色指针、宿主角色列表发现与模板脚手架
├── profiles_page.py           # 角色指针管理与模板表单分组（融合进「人格模型」页）
├── settings_page.py           # 「人格模型」设置页注册与统一保存逻辑
├── mind_params.py             # L1–L6 心理与动力学 28 项参数 Schema / 夹取 / traits.json 覆写
├── mind_page.py / web/mind.html # 「心理模型」独立可视化设置页（支持框内增删 L3/L4）
├── mind_bridge.py / persona_state.py / props.py / dynamics.py  # 状态机桥接、状态卡拼装与 ACT/动力学中间件
├── guard_bridge.mjs / judge.py / names.py / soul_route.py / trust.py
├── profiles/                  # 内置角色模板包（nanami_chiaki 原典校准 + _template 通用模板）
├── soul/chiaki-soul.md        # 七海千秋原典人格壳（含 <!-- route: ... --> 场景段）
├── toolkit/                   # persona-mind 运行时与出厂常量（mind.mjs / percept.mjs / tic-guard.mjs）
├── .github/workflows/release.yml  # 打 v* tag 自动出包并输出 sha256 与字节数
├── LICENSE / THIRD-PARTY.md / DISCLAIMER.md
└── README.md
```

---

## 五、本地读写、隐私边界与兼容性

- **本地数据目录**：所有运行状态与用户调整都保存在 `<数据目录>/nanami/`（如 `settings.yaml`、`pointers.yaml`、`profiles/`、`state/mind.json`、`state/traits.json`、`state/dynamics.json`、`state/person.json`、`state/judge-cache.json`、`state/percept-cache.json`），升级插件替换 `plugins/nanami_persona/` 不会覆盖用户数据。
- **网络与隐私**：本插件不采集、不上报任何遥测数据；唯一发生的外部请求仅在启用 `mind.percept` 或 `judge` 时按你自己的配置调用宿主 LLM。
- **失败保护（Fail-Open）**：未安装 Node、Node 脚本超时、判官网络异常或人格壳缺失时，相关环节自动跳过并记日志，不阻塞或吞掉宿主正常对话与语音流程。
- **兼容平台**：Shinsekai **>= 2.6.0**，支持 Windows / macOS / Linux。

---

## 六、开发自测与发版

在 Shinsekai 主仓库根目录可执行全套本地校验与打包验证：

```bash
./runtime/python.exe tools/nanami/persona_multipoint_test.py   # 多角色/动力学/宿主下拉联动自测
./runtime/python.exe tools/nanami/persona_mind_page_test.py    # 心理模型 28 项 Schema 与 Node 覆盖测试
./runtime/python.exe tools/nanami/persona_settings_test.py     # 设置写回与注释保留测试
./runtime/python.exe tools/nanami/package_plugin.py --check-only  # 市场元数据与绝对路径体检
./runtime/python.exe tools/nanami/selftest.py                  # 全链路 51 项核心自测
```

推送版本 tag 时（例如 `v1.3.0`），`.github/workflows/release.yml` 会自动生成可直接上架市场的 `nanami_persona-1.3.0.zip`，并在 Release 正文中输出对应的 `sha256` 与精确字节数。

---

## 七、许可与声明

- 本插件自主编写的代码与文档遵循 [LICENSE](LICENSE)（MIT）。
- `toolkit/` 为 `persona-mind` 工具箱只读运行时快照，上游授权与依赖说明见 [THIRD-PARTY.md](THIRD-PARTY.md)。
- 默认参考角色「七海千秋」及《超级弹丸论破2》原作设定与台词版权归原权利人所有，详见 [DISCLAIMER.md](DISCLAIMER.md)。
