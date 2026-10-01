# 协同事务平台

协同事务平台为需要多机构参与的业务提供统一的机构、授权、案件、证据、审批、资源预约、资金分录、消息归并、通知、定时任务和历史回放能力。项目只使用 Python 标准库与 SQLite，适合在单个 Linux 应用容器内运行。

## 政策融资连续档案

`src/civicflow/policy_finance.py` 在平台之上实现养老等政策融资（信用贷款、财政贴息、风险补偿、追偿）的连续档案，覆盖：

- **主体与关联关系**：企业主体、行业分类、关联企业组（双向传递闭包），行业资格与经营/用途证明及其有效期。
- **授信版本锁定**：审批时固化当时的资格快照、关联组和额度；产品规则版本化；关联企业获得的信用贷款、贴息等支持按组合并校验上限。
- **提款与支付**：提款用途、预期收款方；支付回执按回执键幂等去重，金额或收款方不一致的回执先隔离、绝不形成资金分录；资金支付后只能还款或凭用途复核结论冲正。
- **用途偏离**：冻结该授信下尚未支付部分，必须由提出人之外的另一名人员复核；客户经理不能批准自己提出的授信或例外。
- **履职可见性**：材料按机构与履职角色授权，参与机构只能看到履职所需材料。
- **风险分担与补偿追偿**：分担比例基点校验合计不超过 100%，核定补偿按比例分摊敞口；资格过期时受理补偿须附经他人批准的例外；追偿进展逐笔登记。
- **重启可恢复**：复核任务、补偿任务的责任人与截止期持久化，贷后核验或补偿处理进程重启后可凭 `pending_work` 接续办理。
- **资金流向报告**：`fund_flow_report` 说明每笔政策资金实际流向何处、哪些机构分担风险及追偿进展；`dossier_file` 给出连续档案。

所有写操作与资金分录在同一数据库事务内提交，并写入追加式审计链。


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

养老政策融资全周期演示（授信锁定、关联合并上限、回执隔离、用途偏离冻结、还款展期、风险补偿追偿、进程重启恢复）：

```bash
PYTHONPATH=src python3 -m civicflow.cli --db /tmp/civicflow-finance.sqlite3 finance-demo
```

查看当前案件：

```bash
PYTHONPATH=src python3 -m civicflow.cli --db /tmp/civicflow-demo.sqlite3 list-cases
```
