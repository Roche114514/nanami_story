# 七海千秋 · 命运丝线故事引擎（nanami_story v0.3.0）

面向 **Shinsekai（>= 2.6.0）** 的命运丝线（Fate Weaver）故事引擎插件。已彻底移除早期预设剧本/开放舞台推理结构，由**「命运丝线」作为唯一核心引擎**，与 `nanami_world`、`nanami_persona` 保持零直接代码耦合，仅通过宿主的 `WorldBridge` 与只读人格目录协同工作。

## 安装与依赖

在 Shinsekai 的插件设置中，从 [GitHub 仓库](https://github.com/Roche114514/nanami_story) 安装并启用 `nanami_story`。用于本地开发的目录名为 `plugins/nanami_story`，入口为 `plugins.nanami_story.plugin:NanamiStoryPlugin`。

- 安装并启用 [`nanami_world`](https://github.com/Roche114514/nanami_world) 以提供舞台和角色行动。完整的观剧及关键对话修复需要包含本次配套改动、支持 `set_dialogue_focus` 的世界插件。公开的 `nanami_world` v0.5.5 缺少这个接口。
- [`nanami_persona`](https://github.com/Roche114514/nanami_persona) 可选，用于读取已有的人格资料。实时关键对话使用宿主的 LLM 配置。
- 插件使用宿主 SDK 和 Python 标准库。

## 本地测试

使用带有兼容 `nanami_world` 的 Shinsekai Python 运行测试。将 `SHINSEKAI_TEST_ROOT` 指向包含 `sdk/` 和 `plugins/nanami_world/` 的宿主安装目录，然后在本仓库根目录运行以下命令。

```sh
python -B -X utf8 -m unittest discover -s tests -p "test_*.py"
```

## 核心设计

1. **三引擎零耦合协同**：
   - `nanami_persona`：提供只读人格底色、性格维度与关系脉络，插件本身保持零侵入；
   - `nanami_world`：提供多层楼层（`1F / 2F / B1`）、推门点亮、独立密闭房间（`enclosed` zone）、自由行动意图（`propose_actor_commitment`）与环境氛围原语；
   - `nanami_story`：以命运丝线串联故事节点，持续记录每位角色的个人编年史，尊重人格的节点自主抉择，并顺着真实选择与世界因果滚动编排后续节点与章节落笔。
2. **双形态统一入口（新建故事 & 挂载小镇）**：
   - **新建故事（`standalone`）**：生成多层小镇或室内建筑群（`town` / `compound` / `room`），将角色分散安置在各个室内房间，并只在特定室内楼层/密闭隔间落下契机物证与微弱异动——**绝不把线索散落在地图广场中心**。删除故事舞台时，关联的临时世界、原生居民与隔离档案一并清理。
   - **挂载小镇（`attached`）**：直接附着到已有的常驻活态小镇，保留全部建筑、居民与记忆，随时拨动一根新丝线；解绑时完整保留原小镇。
3. **非全知个人编年史（L1 / L2 / L3）**：
   - **L1 亲历账本**：仅按角色身处同一空间与否及密闭门隔音过滤，记录真实亲历（`direct`）、同室目击（`witnessed`）或隔门隐约听闻（`overheard`）；
   - **L2 抉择轨迹**：记录角色在每个 `StoryNode` 上的自主选择、执行状态与个人承诺；
   - **L3 生命弧光**：沉淀心愿（`aspirations`）、未了牵挂与性格微调倾向。
4. **人格自主抉择与滚动编排**：
   - 每个 `StoryNode` 只布置场景处境与十字路口，不替角色演戏；在场角色依据自身性格与个人账本自由决定回应方式，并通过 `source="persona_choice"` 的自主行动意图跨楼层、跨房门前往应对、商议或回避。
   - 节点收敛后，编排器根据已发生的真实选择，通过 Storylet 与大模型实时长出分叉后的下一拍（`weave_next_beat`）或章节落笔（`chapter_chronicle`）。
5. **自然融入与心路留白（Decision 2.A & 3.A）**：
   - 支持 **自然融入（`co_play`）** 与 **纯观剧见证（`observer`）**：玩家以真实漫游、开门、物件互动与对话参与世界，也可通过侧栏编年史面板轻量表态或轻拨新丝线。
   - 世界画布只呈现外在言行与环境变化；角色`内心独白（inner_reason）`默认收起（`reveal_inner_thoughts=False`），可在编年史面板随时切换查看，或于该丝线收束至「篇章落笔（`afterglow`）」后自动揭晓。
