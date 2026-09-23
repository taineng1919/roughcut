"""Build the current macOS arm64 Code-only default trial package.

The historic frozen builder (scripts/build_macos_arm64_trial_distribution.py)
is preserved untouched. This builder produces the new default trial that
contains only Code plus agent-facing guides/manifests — no FunASR/Torch/
models/Audalign/FFmpeg/Large Components/credentials/user data.

Formal packages must be built from a clean committed HEAD; the exact HEAD SHA
is injected as build identity into a shared staging tree (wheel + source
bundle share one SOURCE_COMMIT) and recorded in TRIAL-MANIFEST.json.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.build_core_release import (
    CORE_VERSION,
    SOURCE_BUNDLE_NAME,
    WHEEL_NAME,
    ReleaseAssemblyError,
    assemble as assemble_core,
)

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CORE_SOURCE = ROOT / "core"

_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
_BUILD_ID_SHORT_LEN = 12

CODE_DIR_NAME = "Code"
CODE_CHECKSUMS_NAME = "CODE-SHA256SUMS"
INSTALL_GUIDE_NAME = "INSTALL-AGENT.md"
MANIFEST_NAME = "TRIAL-MANIFEST.json"
ROOT_CHECKSUMS_NAME = "SHA256SUMS"
KNOWN_ISSUES_NAME = "KNOWN-ISSUES.md"
PACKAGING_REPORT_NAME = "PACKAGING-REPORT.md"

ROOT_PAYLOAD_NAMES = (
    f"{CODE_DIR_NAME}/{WHEEL_NAME}",
    f"{CODE_DIR_NAME}/{SOURCE_BUNDLE_NAME}",
    f"{CODE_DIR_NAME}/{CODE_CHECKSUMS_NAME}",
    INSTALL_GUIDE_NAME,
    MANIFEST_NAME,
    KNOWN_ISSUES_NAME,
    PACKAGING_REPORT_NAME,
)

FORBIDDEN_PACKAGE_SUBSTRINGS = (
    "funasr",
    "torch",
    "torchaudio",
    "audalign",
    "component-cache",
    "runtime.json",
    "Large-Components",
    ".pt",
    "model.pt",
    "campplus",
    "ffmpeg",
    "ffprobe",
)

SECRET_FILENAME_MARKERS = (
    "api_key",
    "apikey",
    "credential",
    "token",
    "private/",
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _regular_file(path: Path, *, label: str) -> None:
    try:
        details = os.lstat(path)
    except OSError as error:
        raise ReleaseAssemblyError(f"{label} is missing: {path}") from error
    if not stat.S_ISREG(details.st_mode):
        raise ReleaseAssemblyError(f"{label} is not a regular file: {path}")


def _git_head_and_clean(repo_root: Path) -> tuple[str, bool]:
    try:
        head = subprocess.run(
            ["git", "rev-parse", "--verify", "HEAD"],
            cwd=str(repo_root),
            check=False,
            capture_output=True,
            text=True,
        )
        status = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=str(repo_root),
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError as error:
        raise ReleaseAssemblyError(f"git inspection failed: {error}") from error
    if head.returncode != 0:
        raise ReleaseAssemblyError("git HEAD is unavailable")
    if status.returncode != 0:
        raise ReleaseAssemblyError("git status is unavailable")
    return head.stdout.strip(), status.stdout.strip() == ""


def _install_agent_text(*, source_commit: str, build_id: str) -> str:
    return f"""# Roughcut macOS arm64 Code-only 试用包（Agent 执行指引）

> 不要立即安装或下载组件。第一阶段必须先检查本机已有状态并尽可能 REUSE。

本包是 Code-only 默认试用包（`source_commit={source_commit}`，`build={build_id}`）。
包内只有 Code（wheel + source bundle + `CODE-SHA256SUMS`）与本文档/manifest；
不含 FunASR、Torch、TorchAudio、四模型、Audalign closure、BBC、FFmpeg binary、
component-cache、`runtime.json`、API Key、Qwen Workspace credential、Project 或 media。
Large Components v1 仅为完全离线 fallback，不在本包内，不修改/重打现有 v1。

## 0. 先核对包完整性、平台与 Python 3.11 准备

```sh
set -eu
PACKAGE_ROOT="/absolute/path/to/roughcut-macos-arm64-trial-{build_id}"
cd "$PACKAGE_ROOT"
shasum -a 256 -c "$PACKAGE_ROOT/{ROOT_CHECKSUMS_NAME}"
cd "$PACKAGE_ROOT/{CODE_DIR_NAME}"
shasum -a 256 -c "$PACKAGE_ROOT/{CODE_DIR_NAME}/{CODE_CHECKSUMS_NAME}"
uname -m  # 须为 arm64
sw_vers -productVersion
```

Python 3.11 准备（Agent 标准流程自动完成，不甩给用户）：

```text
probe Python 3.11 → available: REUSE → missing: bootstrap native arm64 Python 3.11
→ verify exact 3.11 + arm64 → continue Roughcut install
```

- 优先 REUSE 已有 native arm64 Python 3.11（`command -v python3.11` + `python3.11
  --version` + `python3.11 -c "import platform; print(platform.machine())"` 为
  `arm64` + exact `3.11.x`）。
- 缺失时 Agent 自己完成用户级、无需 sudo 的可信 Python 3.11 bootstrap（支持方案
  之一：`uv` / `python-build-standalone` 获取 native arm64 3.11；不把 Homebrew 写成
  硬依赖，也不把完整 Python runtime 塞进本 Code-only 包）。
- 验证下载来源完整性，失败清理临时文件；最终必须 exact `3.11`（`3.12`/`3.13`
  不可用于 trial component profile）+ native `arm64` 才继续。
- 只有真正需要用户授权的系统级 mutation 才询问用户；是否准备 Python 3.11 不是
  产品选择题。
- 下文以 `TRIAL_PYTHON` 表示此次验证通过的 Python 3.11 解释器绝对路径
 （REUSE 或新 bootstrap 均可）。

## 1. 本机已有状态探测（只读，禁止先下载）

按顺序执行并记录 REUSE / MISSING / INCOMPATIBLE，不得在探测完成前下载：

```text
1. verify package SHA（上一步已完成）
2. inspect platform/architecture（Darwin + arm64 + TRIAL_PYTHON exact 3.11/arm64）
3. probe Python/FunASR 运行时（`$TRIAL_PYTHON --version` + 候选解释器 exact 3.11/arm64 +
   isolated import 核对版本；已验证的 external 1.3.8（FunASR 1.3.8 / Torch 2.12.0 /
   Torchaudio 2.11.0 CPU）继续优先只读 REUSE，不因 managed 锁不同而重装；
   managed 新安装才锁定 FunASR 1.3.14 / Torch 2.6.0 / Torchaudio 2.6.0；
   默认本地 ASR 为 FunASR，缺失时须获取完整 frozen closure，不得以 MLX-only 视为足够）
4. probe existing Roughcut（从当前 Host MCP/executable 与 runtime binding 解析真实
   root/custom managed root；不得套用 fresh 默认 `~/.roughcut` 猜测）
5. probe local managed components/cache（`roughcut diagnostics --json`、
   component PLAN（quick）、runtime binding、cache receipts）
6. probe models（ModelScope cache/本地 managed 模型目录 + SHA/size；只核对 frozen
   repository/revision/file path，不改 revision、不找 latest、不换相似模型；
   按 runtime-required 文件判定完整性：PUNC 缺 `jieba_usr_dict` 即不完整，
   不得仅凭 `model.pt` 存在判为可用）
7. probe Audalign（managed/external 1.3.1 选择 + validator；不升级、不覆盖）
8. probe FFmpeg/ffprobe + full capability validation
   （当前正式 `diagnose_ffmpeg(full=True)` 或等价现有 API：`>=8.1,<10`、
   ffmpeg/ffprobe 数字版本一致、filter_complex + libx264 + AAC 存在、full smoke PASS；
   满足即 REUSE，不下载不安装；不要因“9.x”就认为合格，也不要锁回 Martin Riedl 9.0 only）
9. 形成缺失清单（REUSE / MISSING / INCOMPATIBLE；只有后两者进入获取）
```

FFmpeg 缺失时的获取顺序（Homebrew 非硬依赖；mutation 须先披露并获批；优先当前
命令/环境变量限定国内 mirror，不永久污染用户配置）：

```text
国内 Homebrew channel（USTC 优先，其次 Aliyun）`brew install ffmpeg`
→ 常规来源（normal Homebrew 或项目已有可信 FFmpeg source）
→ Agent fallback（允许换 vendor/build/兼容 minor/patch，但必须 >=8.1.0,<10.0.0、
   ffmpeg+ffprobe 同版本、可信来源、许可证可识别，且经 Roughcut 正式完整验证全 PASS）
无 Homebrew 时可提议安装并获批；安装失败则继续其他可信方式，不得卡死。
```

Python/模型类获取顺序（frozen identity，不换版本；PyPI 国内源须经 simple index 按
exact filename 解析实际 href，禁止机械替换 URL host）：

```text
local verified cache → 阿里云 PyPI simple index → canonical PyPI/catalog canonical source
→ Agent 搜索其他可信 PyPI mirror（exact package/version/filename/platform wheel，
   最终 size PASS 且 SHA256 == catalog SHA256，否则拒绝；禁止 pip 自由 resolve 最新版）
模型：verified local → ModelScope.cn → catalog canonical/可信 mirror
→ Agent 搜索 exact revision artifact（最终 SHA 必须匹配）
```

## 2. 安装/更新 Code

```sh
set -eu
PACKAGE_ROOT="/absolute/path/to/roughcut-macos-arm64-trial-{build_id}"
CODE_DIR="$PACKAGE_ROOT/{CODE_DIR_NAME}"
CORE_WHEEL="$CODE_DIR/{WHEEL_NAME}"
INSTALL_DIR="/absolute/path/to/roughcut-install"
TRIAL_PYTHON="/absolute/path/to/verified-python3.11"
CORE_TMP="$(mktemp -d -t roughcut-core-{CORE_VERSION}-source)"
trap 'rm -rf -- "$CORE_TMP"' EXIT
tar -xzf "$CODE_DIR/{SOURCE_BUNDLE_NAME}" -C "$CORE_TMP"
CORE_ROOT="$CORE_TMP/roughcut-core-{CORE_VERSION}"
"$TRIAL_PYTHON" "$CORE_ROOT/scripts/bootstrap.py" --install-dir "$INSTALL_DIR" \\
  --core-wheel "$CORE_WHEEL" --json
"$INSTALL_DIR/venv/bin/roughcut" health --json
"$INSTALL_DIR/venv/bin/roughcut" diagnostics --json
```

`TRIAL_PYTHON` 即第 0 节验证通过的 exact 3.11 + native arm64 解释器（REUSE 或
新 bootstrap）。全篇后续 `python3.11` 均指该解释器。

bootstrap 按完整身份（schema/core/tool/`source_commit`）判定：同 `source_commit` →
REUSE，不同 → UPDATE（含同版本不同 commit → UPDATE）。wheel `source_commit` 必须与
`CORE_ROOT` 源码一致，否则 bootstrap 明确失败并停止。

## 3. 缺失组件的两阶段处理（Scheme B）

不得手工编造 cache receipt，不得在 stale plan 上 APPLY：

```text
阶段 A（填充 verified cache）：对每个缺失 artifact，先经允许渠道取到 exact 文件，
  再经现有 Python core/bootstrap JSON 边界验收入 cache（自带 disk preflight，
  按 component + filename 定位 catalog artifact，按 filename/version/size/SHA256
  精确校验，经现有 cache layout/receipt 发布；SHA 仍来自 frozen catalog，不依赖
  用户输入 SHA 决定 identity）。
阶段 B（fresh PLAN）：填充后重新运行完整 component PLAN，获得新 plan hash/operation ID，
  向用户展示一致的 full plan（来源、下载量、许可证、plan hash），取得一次明确批准后 APPLY。
```

阶段 A 的可执行边界（每 artifact 一次；`--populate-source` 须为已取到 exact 文件的
绝对路径，`--populate-tier` 为 closed 五选一，仅用于报告，不改 catalog，不存
credentials/URL；不匹配 catalog size/SHA 即 FAIL）：

```sh
CACHE_DIR="/absolute/path/to/dedicated-component-cache"
"$TRIAL_PYTHON" "$CORE_ROOT/scripts/bootstrap.py" \\
  --component-cache "$CACHE_DIR" \\
  --populate-cache \\
  --populate-component "model_punc" \\
  --populate-filename "model.pt" \\
  --populate-source "/absolute/path/to/fetched/model.pt" \\
  --populate-tier "local" \\
  --json
```

定位规则为 `component + filename` 必须唯一命中 catalog artifact；仍无法唯一
命中则 fail closed。`--populate-component` 为可选：filename 在 catalog 中本来
唯一的仍可省略并向后兼容；有歧义而未给 component 时返回明确错误
`artifact filename is ambiguous; specify --populate-component`，不得静默猜测。

`local` 换成实际渠道（`aliyun` / `modelscope` / `canonical` / `agent-fallback`）。
填充后必须重跑完整 PLAN（新 hash）再获批 APPLY。

## 4. Host 集成（生成 Host Package 不等于集成完成）

当前正在执行本任务的 Agent 就是唯一接入目标；不用让用户选择，也不要连续运行不同
Agent 的接入命令。Core bootstrap、health/diagnostics 与组件 PLAN/APPLY 对所有 Agent
共用。Core 安装完成不代表当前 Agent 接入完成；只有完成下方自身 Agent 的 MCP、五个
canonical Skills、必要的 trust/reload 与 discovery 验证，才可报告整体安装完成。

- Roughcut 通用交付为 `roughcut-mcp` + 5 个 canonical Roughcut Skills：
  `roughcut`、`roughcut-basics`、`create-roughcut`、`revise-roughcut`、
  `render-roughcut`。五个目录的唯一本源为 `$CORE_ROOT/agent-skill/skills/`；不同 Agent
  共用这套 Skills，不复制或改写业务规则。

### 当前 Agent 为 Codex

按 Codex 自身已确认的本机机制接入：MCP 使用用户级 `~/.codex/config.toml`，Skills
使用 `~/.agents/skills/`。MCP 首次登记可运行：

```sh
codex mcp list
```

全新接入（列表中没有 `roughcut` 时）运行：

```sh
codex mcp add roughcut -- "$INSTALL_DIR/venv/bin/roughcut-mcp"
```

若 `roughcut` 已登记且 command 已指向本次 `$INSTALL_DIR/venv/bin/roughcut-mcp`，直接复用。
若仍指向旧安装，只更新 `~/.codex/config.toml` 中唯一的 `[mcp_servers.roughcut]` 项，
使它等同于下方内容；保留其他 MCP 项。不要对已登记的名称再次运行 `codex mcp add`。
若该配置形态或 Codex CLI 行为与此处不符，停止接入并报告差异，不要改 WorkBuddy 或其他
Agent 的配置。

```toml
[mcp_servers.roughcut]
command = "/absolute/path/to/roughcut-install/venv/bin/roughcut-mcp"
args = []
```

安装／更新五个 canonical Skills，并保留每个目录内全部文件：

```sh
set -eu
PACKAGE_ROOT="/absolute/path/to/roughcut-macos-arm64-trial-{build_id}"
CODE_DIR="$PACKAGE_ROOT/{CODE_DIR_NAME}"
SOURCE_BUNDLE="$CODE_DIR/{SOURCE_BUNDLE_NAME}"
CODEX_SKILLS="$HOME/.agents/skills"
CORE_TMP="$(mktemp -d -t roughcut-core-{CORE_VERSION}-skills)"
trap 'rm -rf -- "$CORE_TMP"' EXIT

cd "$PACKAGE_ROOT"
shasum -a 256 -c "$PACKAGE_ROOT/{ROOT_CHECKSUMS_NAME}"
cd "$CODE_DIR"
shasum -a 256 -c "$CODE_DIR/{CODE_CHECKSUMS_NAME}"
tar -xzf "$SOURCE_BUNDLE" -C "$CORE_TMP"
CORE_ROOT="$CORE_TMP/roughcut-core-{CORE_VERSION}"

for SKILL in roughcut roughcut-basics create-roughcut revise-roughcut render-roughcut; do
  test -f "$CORE_ROOT/agent-skill/skills/$SKILL/SKILL.md"
  mkdir -p "$CODEX_SKILLS/$SKILL"
  cp -R "$CORE_ROOT/agent-skill/skills/$SKILL/." "$CODEX_SKILLS/$SKILL/"
done
```

重启当前 Codex CLI／IDE 会话；Codex 的本机 MCP 使用已有工具批准策略，无需为 Roughcut
放宽权限。若使用 project-scoped 配置，先确认项目已受信任；本指引默认使用用户级配置。
在 Codex 中用 `/mcp` 检查 Roughcut 服务与 `health`、`fake_project_roundtrip`、
`media_operation_status` 工具，用 `/skills` 检查上述五个 Skills 均可发现；仅
`codex mcp list` 中存在配置不算 discovery 验证。

### 其他当前 Agent

若当前执行 Agent 是 WorkBuddy，只能采用 source bundle 中
`host-integrations/workbuddy/README.md` 已确认的流程。该证据限 WorkBuddy 5.2.6.0、
Windows 11 x64；它没有确认本 macOS arm64 包上的配置或 Skills discovery 机制，因此此
组合须报告“Core 安装可完成；Agent 接入未完成”，并列出缺口：WorkBuddy 的 macOS arm64
MCP 注册位置、五个 Skills 安装／发现位置及 trust/reload 步骤未确认。不得改动 Codex。

其他具备本机文件、命令、MCP 和 Skills 能力的 Agent，可使用相同 Core 与 canonical
Skills，并只按自身已确认的机制注册 MCP、安装 Skills、执行所需 trust/reload 与验证。
无法确认自身某一步时，停止该 Agent 的接入，报告 `CORE_INSTALL=COMPLETE` 或实际 Core
状态、`AGENT_INTEGRATION=INCOMPLETE` 和具体缺口；不得猜用其他 Agent 的配置步骤。

长时间 PLAN/APPLY 不得放入生命周期不确定的临时 shell 后台任务（例如短命 Agent
shell 的 `nohup ... &` 会被回收）。优先前台保持执行，或使用宿主提供的
managed/persistent task。如果执行通道消失：使用 `operation_id` 查询已有 operation，
不重复猜测执行结果，不在旧 plan 上继续 APPLY（填充改变 hash 后旧 plan 已 stale，
必须 fresh PLAN）。

完整流程对照（`报告结果`为最后一步）：

```text
1. verify package SHA
2. inspect platform/architecture
3. probe Python (TRIAL_PYTHON exact 3.11/arm64; 缺失则 Agent 自动 bootstrap)
4. probe existing Roughcut
5. probe local managed components/cache
6. probe models
7. probe Audalign
8. probe FFmpeg/ffprobe + full capability validation
9. 形成缺失清单
10. 安装/update Code
11. 对缺失组件按多渠道顺序获取
12. 每个 artifact 校验 size/SHA
13. component PLAN
14. 明确必要 mutation/下载
15. APPLY
16. health
17. diagnostics
18. ASR smoke
19. continue integration as this task's Agent only
20. register/update this Agent's Roughcut MCP
21. install/update all 5 canonical Roughcut Skills through this Agent's own mechanism
22. perform this Agent's required trust/reload
23. verify MCP/tool and five-Skill discovery
24. report Core and Agent integration statuses separately
```

最终安装报告必须列每组件 `REUSED / INSTALLED`、version/revision、source tier
（`local` / `aliyun` / `modelscope` / `canonical` / `agent-fallback`，sanitized、无
credentials/signed URL）、verification（SHA / capability）；FFmpeg 另列 resolved path、
version、vendor/source、full smoke 结果。Host 部分另列 `CORE_INSTALL`、
`AGENT_INTEGRATION`、当前 Agent 身份、MCP/tool discovery、5 Skills discovery 与未完成缺口。
只有 Core 与 Agent 两部分均完成时才报告整体安装完成。所有大组件已存在
且健康时只更新 Code。

## 5. Large v1 fallback（仅提示）

仅当国内 + canonical + Agent fallback 均失败时提示使用 Roughcut Large Components v1
offline package；不要在本包内寻找，也不要修改/重打现有 v1。
"""


def _known_issues_text() -> str:
    return """# Code-only 试用包已知问题

- 本包不含任何大组件与 FFmpeg；首次在干净机器安装须按 `INSTALL-AGENT.md` 完成两阶段
  组件获取与 fresh PLAN/APPLY。
- Trial component profile 限定 native arm64 Python 3.11（frozen catalog 为 py311、
  APPLY 强制 exact 3.11）；Core 本身 `requires-python >=3.11`、无 upper bound。
- FFmpeg contract 为 `>=8.1,<10` + 全能力 smoke；历史 Martin Riedl 9.0 仅为旧离线发行
  evidence/fallback，不是唯一运行时。
- 本包验证只用 fixture/合成 smoke；不读真实用户媒体；Qwen Cloud 不消耗真实调用。
- REAL_USER_FRESH_INSTALL_VALIDATION=PENDING
"""


def _packaging_report_text(
    *,
    source_commit: str,
    build_id: str,
    wheel_sha: str,
    wheel_size: int,
    bundle_sha: str,
    bundle_size: int,
) -> str:
    return f"""# Code-only 试用包构建报告

- source_commit: {source_commit}
- build: {build_id}
- core: {CORE_VERSION}
- wheel: {WHEEL_NAME} size={wheel_size} sha256={wheel_sha}
- source bundle: {SOURCE_BUNDLE_NAME} size={bundle_size} sha256={bundle_sha}
- identity: wheel 与 source bundle 来自同一共享 staging 注入（tracked 源码恒为 None，
  无 Git 自引用）；`health`/`diagnostics` 上报同一 `source_commit`。
- transport: Scheme B（多渠道填充 verified cache + fresh PLAN），core downloader 未扩大；
  provenance 独立报告，不改 catalog URL/receipt。
- separation: 不含 FunASR/Torch/models/Audalign/FFmpeg/component-cache/runtime.json/
  credentials/Project/media；Large v1 仅离线 fallback。
- determinism: `SOURCE_DATE_EPOCH=0`、排序、固定 uid/gid/mtime；同 commit 重建稳定。
- included NLE fixes after the prior Code-only package: `7e24fa6` permits genuinely unpaired
  setup main Sources to project as explicit auxiliary gaps; `59da976` limits the Alignment
  main group to the exact paired setup Sources and rejects dropped/reordered/foreign Sources.
  Fixture regressions cover projection/export success and fail-closed paired-main cases in
  `core/tests/application/test_nle_handoff.py`.
- REAL_USER_FRESH_INSTALL_VALIDATION=PENDING
"""


def _write_manifest(
    destination: Path,
    *,
    source_commit: str,
    build_id: str,
    wheel_sha: str,
    wheel_size: int,
    bundle_sha: str,
    bundle_size: int,
) -> None:
    manifest = {
        "architecture": "arm64",
        "build": build_id,
        "code": {
            "bundle": f"{CODE_DIR_NAME}/{SOURCE_BUNDLE_NAME}",
            "bundle_sha256": bundle_sha,
            "bundle_size": bundle_size,
            "checksums": f"{CODE_DIR_NAME}/{CODE_CHECKSUMS_NAME}",
            "version": CORE_VERSION,
            "wheel": f"{CODE_DIR_NAME}/{WHEEL_NAME}",
            "wheel_sha256": wheel_sha,
            "wheel_size": wheel_size,
        },
        "component_acquisition": {
            "order": ["local", "domestic", "canonical", "agent-fallback"],
            "ffmpeg_contract": ">=8.1.0,<10.0.0 + matched pair + filter_complex/libx264/AAC + full smoke",
            "frozen_identity": True,
            "transport": "scheme-b-two-phase-verified-cache-then-fresh-plan",
        },
        "kind": "roughcut-code-only-trial",
        "large_components": {"included": False, "fallback": "offline-v1-only"},
        "platform": "macOS",
        "real_user_fresh_install_validation": "PENDING",
        "release_schema": "roughcut.code-only-trial.v1",
        "schema_version": 1,
        "separation": {
            "api_keys": False,
            "audalign": False,
            "credentials": False,
            "ffmpeg_binaries": False,
            "funasr": False,
            "media": False,
            "models": False,
            "projects": False,
            "runtime_binding": False,
            "torch": False,
            "user_data": False,
        },
        "source_commit": source_commit,
    }
    destination.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _write_checksums(root: Path, relatives: tuple[str, ...], destination: Path) -> None:
    lines = "".join(f"{_sha256(root / relative)}  {relative}\n" for relative in relatives)
    destination.write_text(lines, encoding="utf-8")


def _assert_no_forbidden_content(root: Path) -> None:
    names = [path.name for path in root.rglob("*") if path.is_file()]
    lowered = [name.lower() for name in names]
    for marker in FORBIDDEN_PACKAGE_SUBSTRINGS:
        for name in lowered:
            if marker in name:
                raise ReleaseAssemblyError(
                    f"Code-only package contains a forbidden payload: {name}"
                )
    for path in root.rglob("*"):
        if path.is_symlink():
            raise ReleaseAssemblyError(f"Code-only package contains a symlink: {path}")
        if path.is_file():
            try:
                relative = path.relative_to(root).as_posix().lower()
            except ValueError:
                relative = path.name.lower()
            for marker in SECRET_FILENAME_MARKERS:
                if marker in relative:
                    raise ReleaseAssemblyError(
                        f"Code-only package contains a possible secret path: {path.name}"
                    )


def _assert_no_absolute_build_paths(root: Path, *, forbidden_prefixes: tuple[str, ...]) -> None:
    for path in root.rglob("*"):
        if not path.is_file() or path.suffix not in {".md", ".json", ".txt"}:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for prefix in forbidden_prefixes:
            if prefix and prefix in text:
                raise ReleaseAssemblyError(
                    f"package text contains an absolute build path: {path.name}"
                )


def assemble(
    core_source: Path, output: Path, *, source_commit: str, require_clean: bool = True
) -> Path:
    if _COMMIT_RE.fullmatch(source_commit) is None:
        raise ReleaseAssemblyError(
            "source_commit must be a 40-character lowercase Git SHA"
        )
    core_source = Path(core_source).resolve()
    if not core_source.is_dir():
        raise ReleaseAssemblyError(f"Core source directory is missing: {core_source}")
    repo_root = core_source.parent
    actual_head, clean = _git_head_and_clean(repo_root)
    if require_clean:
        if actual_head != source_commit:
            raise ReleaseAssemblyError("source_commit does not match git HEAD")
        if not clean:
            raise ReleaseAssemblyError("git worktree is not clean")
    raw_output = Path(output)
    if os.path.lexists(raw_output) and raw_output.is_symlink():
        raise ReleaseAssemblyError(f"output must not be a symlink: {raw_output}")
    output = raw_output.absolute()
    if os.path.lexists(output):
        if not output.is_dir():
            raise ReleaseAssemblyError(f"output is not a directory: {output}")
        if any(output.iterdir()):
            raise ReleaseAssemblyError("output directory must be empty")
    output.parent.mkdir(parents=True, exist_ok=True)
    build_id = source_commit[:_BUILD_ID_SHORT_LEN]
    staging = Path(tempfile.mkdtemp(prefix=".code-only-trial.staging-", dir=output.parent))
    try:
        code_staging = staging / CODE_DIR_NAME
        assemble_core(core_source, code_staging, source_commit=source_commit)
        core_checksums = code_staging / "CORE-SHA256SUMS"
        code_checksums = code_staging / CODE_CHECKSUMS_NAME
        if core_checksums.is_file():
            os.replace(core_checksums, code_checksums)
        guide = code_staging / "CORE-UPGRADE-AGENT.md"
        if guide.exists():
            guide.unlink()
        expected_code = {WHEEL_NAME, SOURCE_BUNDLE_NAME, CODE_CHECKSUMS_NAME}
        if {path.name for path in code_staging.iterdir()} != expected_code:
            raise ReleaseAssemblyError("Code directory does not contain exactly the Code unit")
        wheel_path = code_staging / WHEEL_NAME
        bundle_path = code_staging / SOURCE_BUNDLE_NAME
        _regular_file(wheel_path, label="Code wheel")
        _regular_file(bundle_path, label="Code source bundle")
        _regular_file(code_checksums, label="Code checksums")
        wheel_sha = _sha256(wheel_path)
        bundle_sha = _sha256(bundle_path)
        (staging / INSTALL_GUIDE_NAME).write_text(
            _install_agent_text(source_commit=source_commit, build_id=build_id),
            encoding="utf-8",
        )
        (staging / KNOWN_ISSUES_NAME).write_text(_known_issues_text(), encoding="utf-8")
        (staging / PACKAGING_REPORT_NAME).write_text(
            _packaging_report_text(
                source_commit=source_commit,
                build_id=build_id,
                wheel_sha=wheel_sha,
                wheel_size=wheel_path.stat().st_size,
                bundle_sha=bundle_sha,
                bundle_size=bundle_path.stat().st_size,
            ),
            encoding="utf-8",
        )
        _write_manifest(
            staging / MANIFEST_NAME,
            source_commit=source_commit,
            build_id=build_id,
            wheel_sha=wheel_sha,
            wheel_size=wheel_path.stat().st_size,
            bundle_sha=bundle_sha,
            bundle_size=bundle_path.stat().st_size,
        )
        _write_checksums(staging, ROOT_PAYLOAD_NAMES, staging / ROOT_CHECKSUMS_NAME)
        _assert_no_forbidden_content(staging)
        _assert_no_absolute_build_paths(
            staging, forbidden_prefixes=(str(staging), str(repo_root))
        )
        if os.path.lexists(output):
            if not output.is_dir() or any(output.iterdir()):
                raise ReleaseAssemblyError("output directory became non-empty")
            output.rmdir()
        os.rename(staging, output)
        return output
    except ReleaseAssemblyError:
        raise
    except (OSError, UnicodeError) as error:
        raise ReleaseAssemblyError(f"Code-only trial assembly failed: {error}") from error
    finally:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--core-source", type=Path, default=DEFAULT_CORE_SOURCE)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--source-commit", required=True)
    args = parser.parse_args()
    try:
        assemble(args.core_source, args.output, source_commit=args.source_commit)
    except ReleaseAssemblyError as error:
        print(f"Roughcut Code-only trial assembly failed: {error}", file=sys.stderr)
        raise SystemExit(1) from error


if __name__ == "__main__":
    main()
