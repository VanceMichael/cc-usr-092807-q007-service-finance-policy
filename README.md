# 协同事务平台

协同事务平台为需要多机构参与的业务提供统一的机构、授权、案件、证据、审批、资源预约、资金分录、消息归并、通知、定时任务和历史回放能力。项目只使用 Python 标准库与 SQLite，适合在单个 Linux 应用容器内运行。

## 政策融资连续档案

平台内建政策融资域（`financing`、`payments`、`settlements`、`financing_reports`），把养老等行业的贴息/信用贷款从授信到追偿串成一份连续档案：

- **主体与关联**：借款企业、关联企业与行业分类建档；关联群成员获得的信用贷款、贴息和其他支持在审批时**合并检查上限**。
- **资格与额度锁定**：行业资格、用途/经营证明在审批时点必须有效；锁定时把当时资格、产品规则与额度固化为快照，后续资格过期不改变已锁定授信，历史可按版本回放。
- **提款与支付**：提款限定用途与许可收款方且不超锁定额度；支付回执按回执号幂等，**相同回执不重复形成资金分录**；金额或收款方不一致的回执先隔离、不入账，交提出人之外的人员复核。
- **用途偏离**：发现偏离即冻结尚未支付部分；客户经理不能批准自己提出的例外，须由另一人复核后解除或终止。已支付资金只能通过还款或冲正调整。
- **风险分担与补偿追偿**：分担份额比例合计必须为 100% 并在授信后锁定；展期、补偿均双人审批；补偿申请持久化原责任人和截止期并登记可恢复定时任务，系统重启后不丢失；追偿逐笔入账直到补偿金额追回完毕。
- **最小知情**：参与机构按履职事项（授信放款、贷后、分担担保、财政）只查看所需材料与字段；贷后角色可通过资金流向总览说明每笔政策资金的实际去向、分担机构与追偿进展。

## 目录

- `src/civicflow/`：领域服务、SQLite 持久化、权限和命令行入口。
- `tests/`：核心流程、边界条件和异常路径测试。
- `examples/`：本地演示输入。

## 配置

通过 `CIVICFLOW_DB` 指定 SQLite 文件路径；不设置时命令行使用当前目录下的 `civicflow.sqlite3`。所有时间使用带时区的 ISO 8601 字符串。

## 测试

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

## 编译或构建

```bash
PYTHONPATH=src python3 -m compileall -q src
```

## 使用

初始化数据库并运行离线演示：

```bash
PYTHONPATH=src python3 -m civicflow.cli --db /tmp/civicflow-demo.sqlite3 demo
```

运行养老机构政策改造贷款的端到端连续档案演示（建档、授信锁定、关联群合并检查、回执隔离、用途偏离冻结复核、还款展期、风险补偿与追偿）：

```bash
PYTHONPATH=src python3 -m civicflow.cli --db /tmp/civicflow-demo.sqlite3 financing-demo
```

查看当前案件：

```bash
PYTHONPATH=src python3 -m civicflow.cli --db /tmp/civicflow-demo.sqlite3 list-cases
```
