# SDK2 迁移表示说明

本文件说明 v2 目标表示、原状态访问归属、首尾与循环重数不变量，以及维护窗口内的事务切换边界。`migration.upgrade(catalog, before_commit=None)` 是唯一切换入口。

## 持久表示（v2_ 前缀表）

| 表 | 内容 |
| --- | --- |
| v2_model_revisions | (model_id, revision) 主键；body 为规范旧格式载荷（get_model 兼容材料），states 为原状态名 JSON，compiled 为 SDK2 可加载的显式派生模型，empty_visits 为空输入访问期望 JSON（空输入证据为零时为 NULL） |
| v2_edge_visits | (model_id, revision, edge_id, state) 主键；value 是精确有理数字符串：单次经过派生边 edge_id 对原状态 state 的期望进入次数（只存非零值） |
| v2_model_heads / v2_tasks / v2_task_classes | 与 v1 同构，外键指向 v2_model_revisions；类别顺序、先验、固定修订原样迁移 |
| v2_classification_runs | 与 v1 同构；迁移逐行复制 run_id、symbols、bindings、result 快照，AUTOINCREMENT 使新 run_id 在原序列上继续递增；历史审计不重新计算 |

v1 五张表保留原行内容仅作审计；升级后任何业务接口不读不写它们（联合核验用 SQLite authorizer 拒绝旧表 READ 后全部接口仍工作）。目标表自含运行与兼容接口所需全部数据，不依赖进程缓存或指回旧表的视图。

## 编译：静默消除（upgrade/save_model 时一次完成）

对含静默边的旧模型，记 S 为静默权重矩阵、A_a 为符号 a 的显式权重矩阵、α/φ 为初始/终止向量。每状态静默出边权重和 ≤ 1/2 保证 I−S 非奇异，用 exact.solve 精确求静默闭包 B=(I−S)⁻¹（即无限条有限静默路径的 Neumann 和，不截断）。

派生显式模型（compiled）：

- 内部状态为 `q#entry` 加每个原状态一个 `q#i`，边 id 为 `e#[symbol,src,dst]` JSON。内部状态名、边名只在派生命名空间，业务原状态名只出现在 states 与 edge_visits.state 中，两者不混淆。
- 初始权重只在 `q#entry` 上为 1；终止权重 `q#entry` 为 αBφ（空输入证据），`q#i` 为 φ_i。
- 入口边 `q#entry --a--> q#j` 权重 (αBA_aB)_j，吸收"初始进入 + 静默前缀 + 第一个显式移动 + 静默后缀"。
- 常规边 `q#i --a--> q#j` 权重 (A_aB)_{ij}，吸收"一个显式移动 + 静默后缀"。零权重派生边不生成。

于是对任意输入（含空输入），SDK2 在派生模型上的证据与旧模型精确相等：αB·A_a1B·…·A_akB·φ，空输入时等于 `q#entry` 的终止权重 αBφ。

## 原状态归属与首尾、循环重数不变量

每次进入原状态（初始进入、静默进入、显式进入）恰好归属到一条派生边或空输入统计：

- 常规边 (a,i,j)：单次经过对原状态 s 的期望进入数 = (A_aB)_{is}·B_{sj} / (A_aB)_{ij}。分子覆盖显式落点 [m=s] 与静默后缀途经 s 的全部重数（(B−I) 项，即循环每绕一次计一次）。
- 入口边 (a,j)：= [(αB)_s·(BA_aB)_{sj} + (αBA_aB)_s·B_{sj}] / (αBA_aB)_j。首项覆盖初始进入 [i0=s] 与静默前缀重数，次项覆盖首个显式落点与其静默后缀。
- 空输入：整条路径只有初始闭包，visits(s) = (αB)_s·(Bφ)_s / (αBφ)，作为派生统计存入 empty_visits（空输入时 SDK2 无边可计）。

分类时只经 sdk2.load/sdk2.evaluate 执行 compiled，取边期望 counts，再与保存统计线性组合：visits(s) = Σ_e counts(e)·edge_visits(e,s)（空输入取 empty_visits）。首（初始进入与首个移动）尾（终止所在状态由最后一段静默后缀进入）与循环重数全部由 B、B−I 的精确和承载，无限路径不截断、不用浮点。所有与输入无关的编译在 upgrade/save_model 完成；分类时绝不解析兼容载荷重解静默系统，也不调用 sdk1。

## 事务切换边界

upgrade 在单个 Catalog.atomic 内：建 v2 DDL → 逐修订编译写入（含无任务引用的修订）→ 复制 heads/tasks/task_classes/classification_runs → meta.schema_version 置 "2"。before_commit 在全部目标数据与版本就绪、实际 COMMIT 之前调用一次；任何异常（含回调抛出）整体回滚目标 DDL、数据与版本，旧接口立即可用，异常原样传播，可安全重试。已为 v2 时 upgrade 无副作用且不调用回调。已打开的 Catalog/Classifier 与关闭重开的对象在切换后都走 v2 路径；save_model 续写旧格式模型时，目标执行表示与原状态统计随该修订在同一事务原子保存。
