# 采集记录分类与重试审计

Python 3.11 标准库项目。模型以精确有理权重描述采集记录的处理路径，任务固定类别模型修订和先验；分类同时保存原始状态的访问审计。SQLite 是唯一持久存储，离线双代执行 SDK 均随项目提供。

运行完整回归和真实文件演示：

```sh
python3.11 -B -m unittest discover -s tests -v
python3.11 -B demo.py
```

无需安装依赖。测试和演示只在测试临时目录创建数据库。demo 构建完整旧库、分类、追加修订、建立新任务、关闭重开，并独立调用 SDK2 显式边端口。

## 模型与路径

模型是包含且仅包含 states、alphabet、initial、final、edges 的字典：

- states 是有序、非空、互异的状态名序列；SDK1 至多 12 个状态。状态名是非空字符串，是本修订内的业务审计身份。
- alphabet 是至多 4 个互异非空符号的序列，允许空字母表。
- initial、final 与 states 等长，分别是初始和终止权重。
- 每条边包含且仅包含 id、src、dst、symbol、weight。id 是修订内唯一的非空字符串，端点是状态名。symbol 为字母表符号，或 SDK1 的 None 静默符号。
- 权重接受 int、Fraction 或 Fraction 可解析的字符串，必须非负；浮点数、布尔值和非有限值不接受。显式出边权重无需归一化。每个状态的静默出边权重之和不超过 1/2，包括零权重边。
- 平行边、静默自环、多状态静默循环、不可达状态、零初始和零终止权重均合法。零权重静默边仍属于静默边。

完整路径从一个初始状态进入，乘初始权重及沿途所有边权重，恰好按顺序消费全部输入后乘终止权重。静默边不消费输入，显式边恰好消费一个匹配符号。终止和继续走静默边是不同路径。完整路径权重之和为 evidence；静默行和上界保证无限多条有限路径的和收敛。

每次进入状态计一次访问，包括初始进入、静默进入、显式边进入。每类别 visits 是该类别完整路径按权重归一化后的条件期望；所有原始状态都返回，包括访问期望为零的状态。不能只按是否到达计一次。

输入是有限的符号 iterable，允许空序列及单遍生成器；裸字符串和 bytes 不作为符号序列。未知符号抛 UnknownSymbol，不当作零证据输入。业务范围不含训练、分段推断或连续观测。

## 公开目录和分类接口

```python
from fractions import Fraction
from catalog import Catalog
from classifier import Classifier
from samples import retry_model, plain_model

with Catalog() as catalog:
    revision = catalog.save_model("retrying", retry_model())
    catalog.save_model("plain", plain_model())
    catalog.create_task("record-job", {
        "retry": ("retrying", revision, 1),
        "plain": ("plain", None, 2),
    })
    result = Classifier(catalog).classify("record-job", ["x"])
    assert result.probabilities["retry"] == Fraction(2, 3)
```

- Catalog(path=':memory:')、close() 及上下文管理器管理连接。
- save_model(model_id, payload) 验证并保存不可变修订，返回从 1 递增的编号，更新 head；每次调用均建立新修订，包括相同内容。
- get_model(model_id, revision=None) 返回独立载荷字典；None 表示当前 head。
- create_task(task_id, choices) 接受 category 到 (model_id, revision或None, prior) 的有序映射。None 在创建时解析成固定修订；以后 head 改变不影响任务。同任务各模型字母表集合必须相同。先验非负，至少一个大于零，任务不可覆盖。
- task(task_id) 按类别创建顺序返回 (category, model_id, revision, Fraction先验) 的 tuple。
- Classifier(catalog).classify(task_id, symbols) 返回 Classification，含 evidence、probabilities、visits 三个类别字典。全部数值为 Fraction；每类 evidence 不乘先验，probabilities 为 prior*evidence 除以全部类别之和。
- 零证据类别概率为 Fraction(0)，visits 为 None。正证据且零先验的类别概率为零，仍返回该类别的条件访问期望。全部先验加权证据为零抛 ZeroEvidence；全部类别零证据也属于此情形。
- 成功 classify 与一条分类审计在同一事务中完成；每次成功调用新增一条 run，不作请求去重。失败不追加审计，原异常传播。返回结果可修改，但修改不影响已持久化审计。
- audits(task_id) 返回按 run_id 排序的 tuple。每项包含 run_id、symbols tuple、bindings 字典和 result 字典；bindings 的值为 (model_id, revision)，result 含精确恢复的 evidence、probabilities、visits。每次读取均返回独立值。
- 不存在的模型、修订或任务抛 KeyError；非法名字、修订、先验或重复任务抛 ValueError；非法模型抛 ModelError(ValueError)，非法输入符号抛 UnknownSymbol(ValueError)。数据库 I/O 异常原样传播。

## 旧库格式与事务边界

catalog.py 的 SCHEMA 是完整 v1 DDL，连接启用 foreign_keys。meta 的 schema_version 为文本 "1"。

| 表 | 内容与身份 |
| --- | --- |
| model_revisions | (model_id, revision) 主键；body 保存 SDK1 规范有理数字符串 JSON |
| model_heads | model_id 主键及当前 revision，外键指向模型修订 |
| tasks | task_id 主键 |
| task_classes | (task_id, category) 主键及唯一 ordinal；保存 model_id、revision 和 prior，外键约束固定修订 |
| classification_runs | 自增 run_id、task_id、symbols JSON、bindings JSON、result JSON，保留每次分类的值快照 |

body 是实际执行的旧格式载荷，并非转换后的目标表示。分数原始输入写法规范化为 Fraction 字符串；不承诺保留用户输入的数字拼写。已保存的旧载荷、任务及运行结果均可由 samples.build_legacy(path) 在新路径构建；该函数关闭连接，拒绝重复填充已存在的目录。

Catalog.atomic(before_commit=None) 是现有通用事务设施：外层 BEGIN IMMEDIATE，内层使用 SAVEPOINT；异常回滚相应范围。before_commit 在本层提交或释放保存点之前执行，抛出的异常同样回滚。分类的模型读取、计算和审计发布处于同一事务。连接按单工作者使用，不提供跨线程共享接口。

## 双代离线 SDK

sdk1.load(payload) 返回不可变 Model；sdk1.evaluate(model, symbols) 返回 Execution(evidence, visits)。SDK1 精确求解静默循环，不截断路径。

sdk2.load(payload) 使用相同显式模型字段，但拒绝所有 symbol=None 的边，不限制目标状态数。sdk2.evaluate(model, symbols) 返回 Execution(evidence, edge_counts)。edge_counts 对每个边 id 给出条件期望经过次数，包含零计数；零证据时为 None。空输入正证据时所有边计数为零。SDK2 不返回状态访问数，不提供静默消除、目录、迁移或审计身份映射。两个 SDK 只共享格式校验和有理数基础设施，SDK2 不调用 SDK1。

samples.explicit_model() 是独立 SDK2 端口样例：输入 xx 的 evidence=1，stay、finish、tail 边期望分别为 3/4、3/4、1/2。samples.plain_model() 可被两代 SDK 直接读取：输入长度 k 时 evidence=(1/3)^k，SDK1 的 collector 访问期望=k+1，SDK2 的 read 边期望=k。它们是显式模型兼容样例。

## 有限样例与业务回归

samples.retry_model() 只有 collector 一个状态，初始和终止权重为 1，静默重试自环权重 1/2，x 边权重 1/3。空输入 evidence=2、访问期望=2；输入 x 的 evidence=4/3、访问期望=4。此例要求对无限路径求精确和。

完整旧库包含 retrying 的 1/2、1/4 两个重试修订，plain 模型及多状态 cycle 模型。frozen 任务固定 retrying 修订1，current 固定修订2，均与 plain 使用先验 1:2，并已有三条持久分类审计。frozen 输入 x 的类别概率为 2/3、1/3；current 输入 x 的概率为 8/17、9/17。

tests/test_business.py 是完整原回归，包括手算例、独立全局有理线性方程参照、固定种子循环组合、SDK2 有限路径穷举、零证据、未知符号、固定修订、审计值隔离、真实触发器写入拒绝、嵌套事务、文件重开和失败重试。测试参照不调用生产执行器或生产线性求解器。
