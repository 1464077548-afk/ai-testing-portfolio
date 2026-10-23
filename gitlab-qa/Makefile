# 常用命令速查。Windows 上如没有 make，命令本身可直接复制执行。

PY ?= python
URL ?= http://localhost:8929
TIME ?= 03:00
SUITE ?= smoke

.PHONY: install doctor unit smoke api ui e2e perf security all report clean cleanup \
        schedule flake flake-check quarantine qadd

install:
	$(PY) -m pip install -e ".[dev]"

## 环境自检：换环境、换 Token 后第一件事
doctor:
	$(PY) tools/doctor.py --base-url $(URL)

## 框架自检，不联网
unit:
	$(PY) tools/run.py --suite unit

## 冒烟，只读，无 Token 也能过
smoke:
	$(PY) tools/run.py --suite smoke --base-url $(URL) --open

## 接口全量（需要 Token）
api:
	$(PY) tools/run.py --suite api --base-url $(URL) --open

ui:
	$(PY) tools/run.py --suite ui --base-url $(URL) --open

e2e:
	$(PY) tools/run.py --suite e2e --base-url $(URL) --open

## 性能：只在需要时跑，不要放进 PR 门禁
perf:
	$(PY) tools/run.py --suite perf --base-url $(URL) --open

## 安全：只对自有实例（白名单守卫），夜间跑
## 缺第二身份先跑：$(PY) tools/ensure_member.py
security:
	$(PY) tools/run.py --suite security --base-url $(URL) --open

all:
	$(PY) tools/run.py --suite all --base-url $(URL) --open

## 不重跑测试，只把已有结果重新生成报告
report:
	allure generate allure-results -o allure-report --clean

## 产物清理：按 .env 策略删过期截图 / trace / 日志 / 旧报告
## 先预览：make cleanup DRY=1
cleanup:
	$(PY) tools/cleanup.py $(if $(DRY),--dry-run,)

## 兼容旧名字。注意：history 永远保留，删了趋势就断了
clean: cleanup

## 注册每日定时清理（Windows 计划任务，时间可改：make schedule TIME=02:30）
schedule:
	powershell -ExecutionPolicy Bypass -File tools/schedule_cleanup.ps1 -Time $(TIME)

## ----------------------------------------------------------------------
## flake 治理
## ----------------------------------------------------------------------
ROUNDS ?= 5
MAXFLAKE ?= 0.05

## 稳定性采样：重复 ROUNDS 轮，按用例给出 稳定/抖动/持续失败/无数据
## make flake SUITE=api ROUNDS=7 MAXFLAKE=0.02
flake:
	$(PY) tools/flake.py --suite $(SUITE) --rounds $(ROUNDS) --max-flake-rate $(MAXFLAKE)

## 隔离区体检：缺原因 / 缺期限 / 已到期 / 规模超限都会退出非 0
flake-check:
	$(PY) tools/flake.py --check-quarantine --verify-nodeids

## 只看名单
quarantine:
	$(PY) tools/flake.py --list-quarantine

## 进隔离区（必须给期限）：make qadd NODEID=tests/ui/test_login.py::test_x DUE=2026-11-06
qadd:
	$(PY) tools/flake.py --quarantine-add "$(NODEID)" --reason "$(REASON)" --due $(DUE)
