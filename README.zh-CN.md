[English](README.md) | 中文版

# Codex Thread Doctor

诊断并修复无法继续恢复或压缩的 Codex 会话线程：`<model> is not supported` 的拦路报错、长线程自动压缩时的 HTTP 400、`invalid_encrypted_content` 的卡死。不用你把线程丢掉重来——它直接修复会话文件让线程恢复可用，并用真实 Codex CLI 实际恢复线程来证明修复有效。

## 工作方式

每次修复都是同一套四步流程：**诊断**（只读全库扫描或单线程检查）→ **保全**（任何写入前先做带时间戳、SHA-256 校验的备份）→ **处理**（每种病因一条命令；未改动的行保持原始字节）→ **验证**（对原始文件做哈希守卫的零污染影子恢复，用真实 Codex CLI 跑）。

```text
audit（只读）──► 备份 + SHA-256 ──► 修复（原子写、落盘后重断言）──► 影子恢复 ──► 结论
```

## 能修什么

| 故障 | 命令 |
| ---- | ---- |
| 恢复时 `model … is not supported` / 历史模型混杂 | `normalize --model <目标模型>` |
| 空或重复的 tool `call_id`（导致自动压缩 HTTP 400） | `call-ids` |
| reasoning 条目 `invalid_encrypted_content` | `remove-lines`（必须带精确 `--expect-count` 护栏） |
| 损坏的压缩检查点 `cmp_…` 阻断恢复 | `remove-compaction` |
| 按锚点消息裁剪历史（支持跨文件线程） | `trim` |
| 可选清理：网关错误文本、孤立启动、索引重复 | `scrub` |

工具绝不改写对话正文（显式 `--error-text` 清理除外）、`cwd`、权限、turn id、会话 id。历史里*提到*旧错误的文字是证据，不是缺陷——工具只读机器状态字段和生命周期记录。支持 macOS / Linux / Windows；Python 3.11+，零运行时依赖；唯一驱动的外部工具是 Codex CLI 本身。`session_index.jsonl` 默认不碰，除非你显式加 `--dedupe-index`。

## 安装

把下面整段复制粘贴给你的 agent（Codex、Claude Code 或任意编程 agent）。agent 会读 `INSTALL.md` 然后动手：

```
You are installing Codex Thread Doctor. The source repository is
https://github.com/JC-SYSU/codex-thread-doctor. Do the following:

1. Clone that repository to a sensible location (e.g. ~/codex-thread-doctor) and cd into it.
   If you cannot clone, read the installation spec directly:
   https://raw.githubusercontent.com/JC-SYSU/codex-thread-doctor/main/INSTALL.md
2. Read INSTALL.md in full (INSTALL.zh-CN.md if preferred) and execute it step by step:
   verify the prerequisites, confirm the Codex config directory, smoke-test the three
   entry points (doctor_audit.py / doctor_fix.py / doctor_verify.py --help), and run the
   test suite (python3 -m pytest tests/ -q) if pytest is available.
3. Before running any write-side command (doctor_fix) or --shadow-test, show me what
   you are about to do and wait for my confirmation.
4. When everything passes, run the acceptance checklist from INSTALL.md section 5 and
   report each item's result to me.
```

推荐让 agent 装；手动安装就是 clone 下来跑一遍验收清单——没有别的要装的。

## 日常使用

```bash
# 全库只读体检，标记某个你账户用不了的模型
python3 scripts/doctor_audit.py --all --blocked-model <model-id>

# 单线程体检 + 真实影子恢复测试
python3 scripts/doctor_audit.py --session <thread-id> --shadow-test

# 修复，然后证明它还能跑
python3 scripts/doctor_fix.py normalize --session-id <thread-id> --model <target-model>
python3 scripts/doctor_verify.py --session-id <thread-id>
```

备份在 `~/.codex/.thread-doctor-backups/`；每次修复打印 `sha256: before -> after`，并在返回前从磁盘重断言不变量。断言失败自动回滚备份并退出码 2。

## 不做什么

不改 Codex 应用、hooks、认证。不碰归档会话（除非你明确指定）。不是自动清洁工：孤立 `task_started` 和旧错误串只有显式 `scrub` 才删。账户层面的失败（你账户根本不可用的模型）只能修到你可用的模型为止。影子验证证明文件在你本机可恢复；它不能保证远端服务接受一份它没见过的转录。

## 数据与安全

修复流程：读会话 JSONL → 在 `~/.codex/.thread-doctor-backups/` 写校验过的备份 → 原子替换文件。撤销任何修复：`doctor_fix.py restore --backup <file> --target <file>`，或整个删掉备份目录。影子验证只写退出时自动清理的临时目录（运行日志落在备份目录里）。audit 和 verify 对真实 home 全程只读，可随时重跑。

agent 安装完后（INSTALL.md 第 5 节）逐一确认脚本报 0 errors、测试通过。Codex CLI 是外部依赖：`doctor_verify.py` 需要 PATH 里有 `codex` 二进制（macOS 上也会探测 ChatGPT.app 自带的）。

## License

MIT，见 [LICENSE](LICENSE)。病因与处理细节见 SKILL.md 和 `references/failure-modes.md`。