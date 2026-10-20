[English](INSTALL.md) | 中文版

# 安装指南（给执行安装的 agent）

这是一份操作系统说明书：按顺序执行，每一步都给出**预期输出**与**失败处理**。全部通过后，跑完文末的「验收清单」并把结果报告给用户。

记 `$REPO` 为仓库根目录，所有命令都在 `$REPO` 下运行。平台：**macOS / Linux / Windows**；Windows 上把下面的 `python3` 换成 `python`（或 `py -3`）。Codex 配置目录是 `$CODEX_HOME`（若设置了该环境变量），否则是 `~/.codex`（Windows 为 `%USERPROFILE%\.codex`）——doctor 运行时用同样的方式解析，你在哪里验证它就用哪里。

## 0. 前置条件

| # | 检查 | 命令 | 预期 | 失败处理 |
| ---- | ---- | ---- | ---- | ---- |
| 0.1 | Python >= 3.11 | `python3 --version` | 3.11.x 或更新 | 先装 Python，再重开 shell |
| 0.2 | PATH 上有 Codex CLI | `codex --version` | 打印版本号 | 先装 Codex，或设 `CODEX_BIN` 指向二进制；doctor 只有 `doctor_verify.py` 和 audit 的 `--shadow-test` 才真正需要 CLI |
| 0.3 | 配置目录可解析 | `python3 -c "import os,pathlib;print(os.environ.get('CODEX_HOME') or str(pathlib.Path.home()/'.codex'))"` | 打印你的配置目录 | 打印的不是你预期的目录，先把 `CODEX_HOME` 显式设好再继续 |

## 1. 放置仓库

```bash
cd <你选的目录>        # 例如 ~/codex-thread-doctor
# 已按 README clone 好，或把文件夹复制到这里
```

预期：`$REPO` 下有 `SKILL.md`、`scripts/`、`references/`、`tests/`。
失败：缺文件 → clone/复制坏了，重来。

## 2. 冒烟测试三个入口

```bash
python3 -m py_compile scripts/doctor_lib.py scripts/doctor_fix.py scripts/doctor_verify.py scripts/doctor_audit.py
python3 scripts/doctor_audit.py --help >/dev/null && echo AUDIT_OK
python3 scripts/doctor_fix.py --help     >/dev/null && echo FIX_OK
python3 scripts/doctor_verify.py --help  >/dev/null && echo VERIFY_OK
```

预期：打印 `AUDIT_OK` `FIX_OK` `VERIFY_OK`。
失败：`py_compile` 报解释器错误 → Python 太旧或编码环境有问题；`ModuleNotFoundError` → 你不在 `$REPO` 下运行，或各脚本顶部的 `sys.path` 引导缺失。

## 3. 跑测试套件（可选但推荐）

```bash
python3 -m pytest tests/ -q        # 需要 pytest（仅开发依赖）
```

预期：全部通过（干净检出为 `34 passed`）。
失败：`ModuleNotFoundError: pytest` → `python3 -m pip install -r requirements.txt` 后重跑；工具本身零运行时依赖，这步可跳过。

## 4. 确认 doctor 能看到你的会话

```bash
python3 scripts/doctor_audit.py --all | head -20
```

预期：一张带列头的会话表格（归档标志、行数、最后模型），完整表格末尾有 `summary: {"ok": N, "risk": M}` 一行。
失败：报 "no session file found" 或打印出意外的配置目录 → 回头查 0.3。完整表格可能很长，`head` 够用。

本步骤**不要**运行 `doctor_fix.py`——它会写入。安装在此为止，下面是验收清单。

## 5. 验收清单——逐项报告给用户

| # | 项目 | 检查方式 | 通过条件 |
| ---- | ---- | ---- | ---- |
| 5.1 | CLI 入口可用 | 重跑第 2 步命令 | `AUDIT_OK` `FIX_OK` `VERIFY_OK` |
| 5.2 | 语法检查干净 | `python3 -m py_compile scripts/*.py` | 无输出，退出码 0 |
| 5.3 | doctor 解析到真实配置目录 | 第 4 步输出 | 表格里是你的会话；有 `summary` 行 |
| 5.4 | （可选）测试通过 | 第 3 步 | `34 passed` |
| 5.5 | 备份目录可写 | `python3 -c "import sys;sys.path.insert(0,'scripts');import doctor_lib;print(doctor_lib.default_backup_dir())"` | 打印 `.../.thread-doctor-backups`；尚未写入任何东西 |

## 6. 卸载

删除仓库目录即可。没有任何东西装到 `$REPO` 之外；你产生的备份会留在 `~/.codex/.thread-doctor-backups/`，要清理自己删。

## 7. 故障排查

| 症状 | 可能原因 | 处理 |
| ---- | ---- | ---- |
| `doctor_audit.py: error: pass exactly one of --session <id> or --all` | 选择器给了两个或都没给 | 只给其中一个 |
| `no session file found for <id>` | id 打错，或线程已归档且文件名不同 | 核对 id；归档重复会被报告，不靠猜 |
| `doctor_fix.py` 退出码 2 且 "post-write check failed" | 修复不变量在磁盘上没成立 | 原文件已从打印的备份自动恢复；读失败项名称，重跑对应处理 |
| `codex CLI not found`（仅 verify） | Codex 不在 PATH（macOS 上通常在 ChatGPT.app bundle 的 Resources 目录里） | 装 Codex、设 `CODEX_BIN` 指向二进制，或把它加进 PATH |
| Windows 上 `python3` 不认 | 该平台上解释器是 `python` / `py -3` | 换用它们（章节头已注明） |
| 影子验证哈希守卫触发 | 验证期间 Codex 应用往会话写了东西 | 关掉线程/应用后重跑；若一打开就写，说明应用持有该会话，这是证据不是异常 |