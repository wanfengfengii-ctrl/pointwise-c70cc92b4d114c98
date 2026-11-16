# SDK2 分类库迁移公开契约

## 现有业务与本轮交付

README.md 是完整旧模型、接口、v1 Schema、异常和手算样例说明；catalog.py 的 SCHEMA 是实际旧 DDL，samples.build_legacy(path) 构造包含全部模型修订、任务和三条审计的完整旧库。sdk1.py、sdk2.py 是随库提供的离线双代依赖，不需另行安装。当前 Catalog 和 Classifier 实际使用 SDK1，本项目尚无迁移实现。

共同业务结果：升级后分类证据与概率不变，重试审计仍按原始状态精确计入每次访问。A 迁移模型、固定修订任务及持久表示；B 适配分类与审计消费，两者共同维护任务修订与原状态身份。只完成证据迁移而遗漏访问重数，不算完成；保留旧执行器替审计计算也不算完成。

原模型至多12个状态、4个符号，每状态静默出边权重和至多1/2；允许平行边、静默自环、多状态循环、零权重与不可达状态。输入可为空。完整路径、初始/终止权重、每次进入计访问的含义均沿用 README.md，所有结果仍为 Fraction。不得截断静默路径或用浮点近似替代精确和。

## 调用入口与切换

新增 migration.upgrade(catalog, before_commit=None)，catalog 是已打开的 Catalog，返回 None。调用方保证维护窗口内只有一个工作者、调用时该连接不在事务中；不要求在线迁移、竞争协调或进程终止恢复平台。

初次创建的 Catalog 仍创建 v1，原接口和原回归在未升级时保持原行为。upgrade 将指定目录切到 meta.schema_version 文本 "2"；Catalog 此后须能打开 v1 和 v2。迁移须覆盖全部模型修订、head、任务类别顺序、先验、固定修订和全部 classification_runs，包括没有任务引用的修订。

目标业务表使用 v2_ 前缀，具体 DDL、显式模型及原状态统计表示自行设计。v1 的 model_revisions、model_heads、tasks、task_classes、classification_runs 保留原行内容，仅作审计；升级后不得再增删改这些表，或以它们为任何业务接口的读取来源。meta 的版本标志可以更新。目标表须独立保存运行所需数据及兼容接口所需信息，不能依赖临时对象、进程缓存或指回旧表的视图。

完整目标数据、绑定和版本须原子发布。可复用 Catalog.atomic；失败须回滚目标 DDL、数据和版本，旧接口仍可使用，异常原样传播。before_commit 为无参回调，首次迁移在准备好全部目标数据和版本、实际提交之前调用一次；回调抛异常也须完整回滚。已为 v2 时 upgrade 无副作用，不再调用回调，不新增修订、任务或审计。

已创建的 Catalog 与 Classifier 对象在 upgrade 返回后立即使用新路径；关闭重开也相同。无需重建 Classifier 才生效。切换本身不产生分类审计。

## 分类、续写与审计兼容

保留 README.md 中 Catalog 的所有公开方法、Classification 和 Classifier.classify 的签名与外部语义。成功分类仍与审计同事务发布；失败不追加 run。原 run_id、symbols、bindings 和精确结果快照均保留，新 run_id 继续递增。迁移不得重新计算并替换历史审计。

save_model 继续接收旧格式含静默边的模型，原校验与异常不变；每次保存仍生成新修订并更新 head。新模型的目标执行表示和原状态统计关系必须随该修订原子保存。create_task 中 None 仍只在创建时解析 head，旧任务永不跟随 head。get_model 仍返回独立的规范旧格式载荷，可在目标表保留此兼容材料，但不得把它当作分类时的旧模型执行旁路。

切换后分类只经 sdk2.load/evaluate 执行目标显式模型；可调用多个派生模型，也可组合 SDK2 的边期望和已保存的派生统计。目标表示自行选择，可保存派生统计或派生模型，但不得保留SDK1执行旁路或在消费方重新执行旧静默模型。分类时不得解析兼容载荷重新求解静默系统；与输入无关的迁移编译工作须在 upgrade/save_model 完成。允许复用 exact.py 的校验和有理数求解能力，不要求另写线性代数库。

升级后的所有业务接口不得调用 sdk1.load 或 sdk1.evaluate。保留 sdk1.py、sdk2.py、exact.py 和 tests/test_business.py 原文件字节；不以修改 SDK2 为其补上静默执行或原状态审计端口。新表示中的内部状态、边名与业务原状态名不能混淆。

各类别 evidence 不乘先验；probabilities 按 prior*evidence 归一化。visits 按该类别证据条件化，与先验无关，并包含全部原始状态。零证据类别概率为零且 visits=None；正证据零先验类别仍有访问期望；加权证据全零仍抛 ZeroEvidence。未知符号仍抛 UnknownSymbol，不能悄悄变成零证据。单遍输入、异常回滚、结果隔离和审计绑定沿用旧接口。

## 可核对的兼容样例

samples.retry_model() 的空输入 evidence=2，collector 访问期望=2；输入 x 的 evidence=4/3，collector 访问期望=4。有限去环展开无法得到无限路径精确和。即使把显式模型改成空输入证据2、x证据4/3，只以初始一次加显式边次数还原访问，仍会错误得到1和2。

完整旧库的 frozen 固定 retrying 修订1，current 固定修订2。输入 x 时：
- frozen 的 evidence 为 retry=4/3、plain=1/3；概率2/3、1/3；collector 访问期望分别4、2。
- current 的 evidence 为 retry=16/27、plain=1/3；概率8/17、9/17；collector 访问期望分别8/3、2。
- 再保存 retry_model("0") 返回修订3；以此建立 continued，输入 x 时概率1/3、2/3，collector 访问期望均为2；frozen 与 current 不变。

samples.cycle_model() 空输入证据3/10，capture、retry 的访问期望均6/5，accepted、unused 均0。输入 y 的证据0；当任务只有该类别时抛 ZeroEvidence。SDK2 的独立显式端口样例及边期望见 README.md，不是静默迁移器。

## 核验与交付

保留全部原回归和旧库构造器；不得改原断言让测试绕过迁移。migration_checks.py 提供旧能力检查和联合核验入口，未实现迁移前只运行 legacy 模式即可核对支撑材料。verify 模式包含原完整套件及迁移联动检查，demo 模式展示真实文件升级、固定修订续写及重开结果。可在此基础上补充验证，不得删除已有断言。

联合核验须覆盖全部旧修订、固定任务与审计的迁移，SDK1 调用及旧表业务读取被禁止时的分类/续写，多状态循环、首尾静默路径、空输入、零证据、零先验、未知符号，以及提交前异常后的旧库可用、重试和重复升级。独立精确参照可复用 tests/test_business.py 的 reference；参照不得调用本轮编译或统计恢复逻辑。

原 .pointwise-runtime.json 格式保持，完成后使 verify 实际执行原回归和迁移联合核验，使 demo 展示上述真实迁移业务。提交简短 MIGRATION.md，说明持久表示、原状态归属、首尾及循环重数不变量和事务切换边界。无需增加训练、分段、在线切换或新产品接口。
