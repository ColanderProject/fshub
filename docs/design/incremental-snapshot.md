# 增量 Snapshot 设计

## 1. 状态

本文档定义 fshub 新的 snapshot 存储与加载格式：一个全量 base 加若干增量 inc，由不可变的 manifest revision 描述。

本设计不兼容现有的单文件 snapshot 格式，也不要求迁移或读取旧格式。

本文档是八轮设计评审的收敛结果。评审过程中被否决的方案及其原因记录在 §19，落稿后不再重复讨论。

## 2. 目标

- 完整扫描只生成一次 base，后续扫描只保存有变化的目录记录。
- base 和 inc 使用完全相同的目录记录格式。
- 支持文件和目录的新增、修改与删除。
- 不引入文件级或字段级 patch，不引入 delete、tombstone 或 rename 操作记录。
- 加载后从根目录重建可达文件树、路径索引及递归统计。
- 一次更新可以原子提交；加载方不会看到半写入的 inc 或 manifest。
- 在支持文件与目录持久化 flush、且 `os.replace()` 提供同文件系统原子替换的平台上，提交协议保证进程崩溃与操作系统崩溃后只暴露旧 generation 或新 generation。其他平台使用其可提供的最强 flush 语义，并通过 manifest 校验和拒绝半提交状态。
- 支持后续接入 Windows USN、`ReadDirectoryChangesW` 和 Linux inotify 等变化检测后端。
- 明确区分"事件是否连续"与"内容是否被观察到"两种不完整，并能如实向调用方暴露。

## 3. 非目标

- 不兼容当前的 `snapshot_*.jsonl.gz` 与 `_index.jsonl.gz` 格式；`--use-index` 及 `.bin.gz` 双文件形态一并删除。
- 本文档不规定第一版必须实现所有平台的变化检测后端。
- 存储格式不负责证明变化事件没有丢失；事件连续性由变化检测层与 manifest cursor 共同保证。
- 不提供文件系统级 point-in-time 一致性。扫描期间源文件系统仍可能变化。
- **不支持子树范围的完整性恢复**。恢复粒度是整个 snapshot：事件链一旦出现 gap，只能由一次成功的全量扫描重置。
- 不支持多个并发 writer。第一版只支持单个受控 writer；并发写入的保护目标是"不损坏"，而不是"受支持"。
- 不提供防伪造能力。producer 守门与 checksum 都只用于把事故变成响亮的失败，不是安全控制。

## 4. 术语

| 术语 | 含义 |
|---|---|
| snapshot | 一个逻辑快照，对应磁盘上一个目录和一个稳定的 `snapshot_id` |
| segment | 一个 base 加其后的全部 inc。compaction 结束旧 segment、开启新 segment |
| snapshot_generation | 源文件系统逻辑状态的版本号。base 或每个 inc 各对应一个 generation |
| manifest_revision | 描述 snapshot 的元数据版本号。每次提交（含纯 compaction）递增 |
| identity provider | 提供目录 incarnation identity 的具体实现，作用域为一个 segment |
| scan scope | 本次扫描声明的观察范围：root、skip 前缀、是否跨文件系统 |
| source | 被扫描到的一个文件系统 / 卷 / 挂载 |
| producer | 执行扫描并提交的 fshub 安装实例 |

## 5. 目录布局

```text
snapshots/
└── snapshot_1700000000_a1b2c3d4/
    ├── manifests/
    │   ├── manifest_rev_000000.json
    │   ├── manifest_rev_000001.json
    │   └── manifest_rev_000002.json
    ├── current                       # 指针文件，纯加速，可丢失
    ├── base_gen_000000.jsonl.gz
    ├── increments/
    │   ├── inc_gen_000001_1700000100.jsonl.gz
    │   └── inc_gen_000002_1700000250.jsonl.gz
    ├── gc.jsonl
    ├── writer.lock
    └── groups.jl
```

- `manifests/` 中每个文件是**不可变**的一个 manifest revision。
- `current` 保存最新 revision 号，只用于避免列目录；丢失、过期或损坏时 loader 回退到列举 `manifests/` 取最大且校验通过者。
- base 文件名带 generation，避免在 Windows 上对被 reader 打开的固定文件名执行替换。
- `groups.jl` 保存该逻辑 snapshot 的 group action log，绑定稳定的 `snapshot_id`。

文件名中的 generation / revision 为最小宽度六位（`%06d`），超出后自然加宽。**顺序的唯一权威是 manifest**，任何工具都必须解析而不是按文件名排序；loader 必须校验文件名中的编号与 manifest 条目一致。

## 6. 目录记录格式

base 和 inc 的每一行都是一条 gzip 压缩的 JSONL 目录记录：

```json
{
  "p": "/data/docs",
  "i": [1, "0x8f3a1c…"],
  "f": ["a.txt", "b.txt"],
  "s": [10, 20],
  "t": [[1700000000, 1700000001, 1700000002], [1700000010, 1700000011, 1700000012]],
  "c": null,
  "d": ["archive", "link-to-x", "restricted"],
  "T": [[1700000020, 1700000021, 1700000022], [...], [...]],
  "D": [[1, "0x8f3b04…"], [2, "41:539390"], null],
  "x": [0, 1, 3]
}
```

| 字段 | 含义 |
|---|---|
| `p` | 当前目录的绝对路径 |
| `i` | 本目录的 incarnation identity |
| `f` | 当前目录中的文件名 |
| `s` | 文件大小，与 `f` 平行 |
| `t` | 文件的 `[ctime, mtime, atime]`，与 `f` 平行 |
| `c` | 文件的云文件状态，与 `f` 平行，或标量 `null` |
| `d` | 当前目录中的子目录名 |
| `T` | 子目录的 `[ctime, mtime, atime]`，与 `d` 平行 |
| `D` | 子目录的 incarnation identity，与 `d` 平行 |
| `x` | 子目录的遍历状态，与 `d` 平行 |

### 6.1 强制存在与强制索引

除 §6.2 规定的 `c` 之外，所有字段**必须存在**，空数组必须写成 `[]`。gzip 已经吸收了重复键名与空数组的成本，而强制存在能让 producer 的字段遗漏在加载期就暴露，而不是在某个 API 里退化成静默的空结果。

§11.4 的结构校验必须在 overlay / rebuild **之前**完成；校验通过后，rebuild 及下游代码一律直接索引，不使用 `.get(field, [])`。

### 6.2 `c` 的唯一规范表示

- 所有文件的 cloud state 均为 `null` 时，producer **必须**写标量 `"c": null`；
- 仅当至少一个文件有非 null 状态时才写数组，且必须与 `f` 等长；
- `f == []` 时"全部为 null"平凡成立，因此 `c` **必须**是 `null`，不得是 `[]`；
- loader 拒绝全 `null` 的数组形式。

通用原则：**任何提供紧凑形式的字段，对同一逻辑值只允许一种合法编码。** 这是 canonical JSON 与 digest 稳定性的前提。当前只有 `c` 具备紧凑形式；`x`、`D` 不得引入标量哨兵。

### 6.3 `x`：子目录遍历状态

| 值 | 名称 | rebuild 行为 |
|---|---|---|
| 0 | `traversed` | 子记录必须存在，identity 按 §8.4 校验 |
| 1 | `symlink` | 不递归，不要求子记录 |
| 2 | `junction` | 不递归，不要求子记录 |
| 3 | `skipped` | 不递归，不要求子记录（scope 外，见 §9） |
| 4 | `denied` | 不递归，不要求子记录，降级 observation coverage |
| 5 | `error` | 不递归，不要求子记录，降级 observation coverage |
| 6 | `cross_device` | 不递归，不要求子记录 |

未知状态码必须**拒绝加载**，不得按"未遍历"静默处理。

`cross_device` **只在配置 `cross_filesystems = false` 时产生**。默认配置下跨挂载点扫描，`dev` 变化意味着该路径下的实际文件树可能整体改变，正确行为是重扫整棵子树（§10.3）。

`symlink` / `junction` / `skipped` / `cross_device` 是声明的扫描边界，不降低完整性；`denied` / `error` 会（§12.2）。

### 6.4 `i` / `D`：incarnation identity

形态为 `[provider_id, value]` 或 `null`。`provider_id` 是 §7.4 provider 表的下标，`value` 是该 provider 定义的 opaque 字符串。identity 用于检测"目录对象被替换"，从而防止路径复用导致已删除子树重新可达（§8.4）。

identity 是**纵深防御**，不能取代 cursor 连续性与全量 fallback。它能抓住"删除后路径复用导致旧子树复活"，抓不住"目录未被替换、但内部变化被 watcher 漏报导致内容陈旧"。

不得使用 `(dev, ino, ctime_ns)` 作为 identity：POSIX 的 `ctime` 是 metadata change time，目录项增删会改变目录自身的 ctime，它是 revision 而不是 identity。`(dev, ino)` 同样不行，inode 会被立即复用。用于 dirty 判定的 `(mtime, size)` 等信息属于 producer 内部状态，不写入记录，loader 的结构不变量绝不引用它们。

### 6.5 规范排序

producer 在写出记录前必须排序：

- 文件按**精确名称**（code point 序）排序，`f` / `s` / `t` / `c` 同步重排；
- 子目录按**精确名称**排序，`d` / `T` / `D` / `x` 同步重排；
- 不做 `normcase`，不做 Unicode normalization。

排序辅助函数必须在置换前**断言各平行数组等长**——某次 `os.stat` 失败导致一个数组少一项，是这里最容易出现的错误。

排序带来的收益：digest 无需再次规范化；相同目录内容产生逐字节相同的记录，inc 可复现，便于测试与跨代 diff；加载后可对 `f` / `d` 二分查找；rebuild 的 DFS 顺序完全确定，未变化子树的 `path_index` 编号在跨代之间趋于稳定。

### 6.6 路径不变量

- 除 root 外，每条记录的 `p` 必须**逐字节等于** `join_snapshot_path(父记录 p, 子目录名, snapshot_os=manifest.os_name)` 的输出；
- root 记录的 `p` 必须等于 `manifest.scan_scope.root_path`；
- record key 的相等性是**精确 code point 相等**：不做大小写折叠、不做 Unicode normalization、不做分隔符改写。

这条不变量与 §9.3 的 scope 等价判定是**两个不同的概念**，不得互相借用实现。

### 6.7 字符串与不可解码文件名

Linux 文件名是字节序列，不保证是合法 UTF-8。Python 以 `surrogateescape` 把不可解码字节表示为未配对代理项（如 `'bad\udcff.txt'`）。因此：

- 序列化**必须**使用 `ensure_ascii=True`。`ensure_ascii=False` 的输出在 `.encode('utf-8')` 时会抛 `UnicodeEncodeError: surrogates not allowed`，导致任何含不可解码文件名的目录无法写入。**这是禁止项，包括"为了可读性"的临时改动。**
- 未配对代理项通过 `\uXXXX` 转义保存，`json.loads()` 后可完整还原，再以 `surrogateescape` 编码回原始字节。
- 消费侧（backup、hash 等回到真实文件系统的操作）必须在同一 OS 上用 `surrogateescape` 反向编码，不得使用严格 UTF-8。
- **互操作边界**：本格式的字符串语义是"Python `str` 的 code point 序列，可能包含未配对代理项"。RFC 8259 允许 parser 拒绝未配对代理项，Go 的 `encoding/json` 会将其替换为 U+FFFD 从而静默损坏路径。不能处理未配对代理项的外部工具属于不支持的读取方。

### 6.8 覆盖语义

同一个 `p` 在后续 inc 中出现时，新的完整记录覆盖此前记录。不存在字段合并或数组 patch。

## 7. Manifest

### 7.1 结构

```json
{
  "payload": {
    "format_version": 1,
    "snapshot_id": "snapshot_1700000000_a1b2c3d4",
    "manifest_revision": 21,
    "parent_manifest_revision": 20,
    "kind": "increment",
    "snapshot_generation": 12,
    "current_logical_state_digest": null,

    "os_name": "Linux",
    "producer": {
      "producer_id": "6f1c…",
      "device": {
        "device_name": "server-1",
        "host_name": "server-1",
        "thumbprint": "…",
        "cpu_model": "Example CPU",
        "memory_size": 34359738368,
        "ip_addr": "192.0.2.10",
        "mac_addr": "00:11:22:33:44:55"
      }
    },

    "scan_scope": {
      "root_path": "/",
      "skip_prefixes_raw": ["/proc", "/sys"],
      "skip_prefixes_normalized": ["/proc", "/sys"],
      "cross_filesystems": true
    },

    "sources": [
      {"path": "/",          "source_id": "uuid:0f2c…", "kind": "filesystem"},
      {"path": "/mnt/data",  "source_id": "uuid:91ab…", "kind": "mount"},
      {"path": "/proc",      "source_id": null,          "kind": "procfs"}
    ],

    "identity_providers": [
      {"id": 0, "scheme": "none",         "strength": "none"},
      {"id": 1, "scheme": "posix.handle", "strength": "strong",
       "source": {"fs_uuid": "0f2c…"}},
      {"id": 2, "scheme": "posix.devino", "strength": "weak",
       "source": {"fs_uuid": "91ab…"}}
    ],

    "base": {
      "kind": "full_rescan",
      "file": "base_gen_000000.jsonl.gz",
      "snapshot_generation": 0,
      "created_at": 1700000000,
      "start_scan_time": 1699999900,
      "finish_scan_time": 1700000000,
      "record_count": 100000,
      "size": 8123456,
      "sha256": "…",
      "logical_state_digest": "sha256:…",
      "event_continuity": "not_applicable",
      "observation_coverage": "complete"
    },

    "increments": [
      {
        "snapshot_generation": 1,
        "file": "increments/inc_gen_000001_1700000100.jsonl.gz",
        "created_at": 1700000100,
        "start_scan_time": 1700000098,
        "finish_scan_time": 1700000100,
        "record_count": 12,
        "size": 4096,
        "sha256": "…",
        "change_backend": "inotify",
        "begin_cursor": "100",
        "end_cursor": "180",
        "event_continuity": "complete",
        "denied_count": 0,
        "error_count": 0
      }
    ]
  },
  "sha256": "…"
}
```

### 7.2 规则

- `format_version` 必须被 loader 支持。loader **必须忽略未知字段**（manifest 与记录皆然），以便新增字段不必升 major。
- `snapshot_id` 必须与 snapshot 目录名一致。
- `manifest_revision` 严格递增，每个 revision 文件不可变。
- `snapshot_generation` 是源文件系统逻辑状态的版本：base 携带自己的 generation，每个 inc 递增 1，manifest 顶层的值等于最后一个 inc 的 generation（无 inc 时等于 base 的 generation）。**generation 单调不减**，compaction 不得重置。
- `kind ∈ {increment, compaction, full_rescan, metadata_only}`。`kind ∈ {compaction, metadata_only}` 时 `snapshot_generation` 必须与父 revision 相同。
- inc 必须严格按 manifest 中的顺序加载，不得通过扫描目录或文件名猜测提交顺序。
- 所有 `file` 字段必须是相对路径、不含 `..`、不含绝对路径或盘符，解析后必须仍位于 snapshot 目录内（可用 `ensure_within` 校验）。
- 设备与扫描级元数据存放在 manifest，**不再混入第一条目录记录**。

### 7.3 两个 digest

- `base.logical_state_digest`：base 所代表的 generation 的逻辑树摘要，**必填**。
- `current_logical_state_digest`：应用全部 inc 后当前逻辑树的摘要，**可为 `null`**（表示未计算）。

规则：

- 普通 inc 提交后，若 writer 未计算最终树摘要，必须把 `current_logical_state_digest` 明确写成 `null`，**绝不能沿用上一代的值**；
- compaction 后 base 即当前 generation，两个 digest 相同且非 null；
- digest 必须由本 revision 实际产生的内容计算，**禁止从父 manifest 复制**。

digest 的定义（§11.6）保证它由"本来就在遍历整棵树的一方"顺带算出，因此没有额外的 O(N) 开销：loader 每次 rebuild 都要走完可达树，writer 写 base / compaction 也在遍历完整记录流；只有"提交普通 inc 且不持有完整逻辑树"的 writer 无法免费获得，故允许其写 `null`。

digest 是 **writer 的 commitment，不是可独立验证的证明**。有 bug 的 compactor 可以写出逻辑不同的 base 却抄来旧 digest；真正验证必须读 base 并 rebuild。其风险半径很窄：generation 不同时 loader 根本不看 digest（§14.2），因此陈旧 digest 只可能在 generation 相同的 revision 上造成伤害，而那只由 compaction / metadata-only 产生——digest 存在的唯一目的，正是校验"声称不改变逻辑树的 revision 确实没有改变逻辑树"。任何新进程首次加载该 revision 时都会 rebuild，从而自然完成一次校验；也可由后台任务主动重算比对。

### 7.4 identity provider 表

- provider ID 的稳定范围是一个 **segment**：ID 一经分配，在该 segment 内不可复用、不可重新解释，表只能追加。
- 每个 manifest revision 引用一个 segment 并携带该 segment 的 provider 表。**loader 必须用它实际加载的那个 revision 的 manifest 解析记录中的 provider ID**，不得用最新 manifest 去解析旧 revision 的记录。
- compaction 创建新 segment，可以重新编号；新 base 与新 manifest 在同一 revision 内原子发布。
- `source` 字段承载 **provider epoch**：例如 `ntfs.usn_frn` 的 epoch 是 `(volume_serial, journal_id)`。epoch 变化必须**追加一个新的 provider ID**，而不是修改旧条目；跨 epoch 的 identity 视为"不可比较"而非"不匹配"。
- `strength ∈ {strong, weak, none}`，是 **(provider, 卷/文件系统)** 的属性，由 producer 在扫描时判定并登记，**默认必须保守取 `weak`**。含义限定为"在本 segment 与本 provider epoch 内足够稳定"，不是数学意义上的永不复用。
- 未知 provider ID 必须拒绝加载。

平台能力提示（非规范，仅供 provider 实现参考）：Windows NTFS 的 file reference number 结构上含 16 位 sequence number，因此**存在**实现 strong provider 的可能，但 Microsoft 并不保证 file ID 跨时间唯一，且 ReFS / FAT / SMB 语义不同，不能笼统地把 `os.stat().st_ino` 定义为 strong。Linux 的 `name_to_handle_at(2)` 可提供更强的句柄，但必须保持 opaque，且文件系统可能返回 `EOPNOTSUPP`。Python 在 Linux 上没有 `st_birthtime`。

### 7.5 Canonical JSON 与 checksum

manifest 为 `{"payload": {...}, "sha256": "..."}`，`sha256` 覆盖 payload 的规范序列化：

```python
json.dumps(payload, ensure_ascii=True, allow_nan=False,
           sort_keys=True, separators=(",", ":")).encode("utf-8")
```

读取侧必须显式设参，因为 Python 的默认行为不安全：

| 默认行为 | 必须的对策 |
|---|---|
| `json.loads('NaN') -> nan`、`loads('Infinity') -> inf` | `parse_constant=` 一律抛错 |
| `json.dumps(float('nan')) -> 'NaN'`（非法 JSON） | `allow_nan=False` |
| `json.loads('{"g":10,"g":11}') -> {'g': 11}`（静默 last-write-wins） | `object_pairs_hook=` 拒绝重复 key |
| 浮点数进入 payload 破坏可复现序列化 | `parse_float=` 抛错；manifest **禁止任何浮点数** |

数值范围：Unix 秒与纳秒时间戳为有符号 64 位；generation / revision / count / size 为无符号 64 位范围。**写入与读取两侧都要校验**（Python 整数任意精度，不会自然溢出）。

不做 Unicode normalization；checksum 针对 JSON 字符串中的 code point 序列。两个视觉相同但编码不同的路径仍是不同字符串。

checksum 只能检测截断、位翻转与非预期修改；**不能**把任何非原子写入变成原子提交，也不是安全控制（有写权限者可重算）。

## 8. 身份、作用域与 identity 校验

### 8.1 snapshot 身份

以下任一项变化都不得在同一逻辑 snapshot 内继续：

| 变化 | 处理 |
|---|---|
| `root_path` 或 `os_name` | 创建**新的 snapshot ID** |
| `skip_prefixes` 或 `cross_filesystems` | 在当前 snapshot 目录内写一个新的 `full_rescan` base |
| `producer_id` 不匹配 | 拒绝追加，要求显式 adopt（§8.3） |

**不允许**仅仅重置 completeness 后继续复用旧记录。

### 8.2 sources

一个 snapshot 通常跨多个文件系统：Linux 的多挂载、Windows "This PC" 的多个卷、混入的 NFS/SMB/procfs。因此 `sources` 是一张拓扑表而非单值。

- `source_id` 可为 `null`（procfs、tmpfs、部分 FUSE 与网络挂载给不出稳定卷身份）；
- `kind` 应显式区分伪文件系统（`procfs` / `sysfs` / `devfs`），以便默认策略与统计更干净；**是否默认 skip 属于产品策略，不是格式强制规则**，manifest 只需如实记录 scope 与 `x = skipped`；
- `st_dev` **不是**稳定的 source 身份，重启与重新挂载都会改变，只可用于单次扫描内区分文件系统。

source 变化的处理：

| 情形 | 处理 |
|---|---|
| source 首次出现 | 完整扫描该 source 子树 |
| 已知 source 且 watcher / journal 连续 | 按后端规则增量 |
| source 被替换或无法确认 | 完整重扫该子树 |
| event continuity 出现 gap | 整个 snapshot 全量重扫（§3 不支持子树级恢复） |

两个必须写明的挂载边界：

- **挂载覆盖非空目录**：新 source 出现在已有记录的路径上时，原有记录全部失效，必须**替换而非合并**——这是"路径复用"的一个实例，与 §10.3 的 `dev` 变化规则同源；
- **卸载后底层目录重新可见**：unmount 会让挂载前的内容重新出现，这是整棵子树的内容变化，而部分变化检测后端不会为此产生任何文件事件。规则与 source 出现时对称。

`source_id = null` 本身不会让一次 full rescan 的结果变成 partial。

### 8.3 producer 身份

- `producer_id` 是 fshub 安装实例的持久 UUID，保存在 **`local_state_path`**（Linux 默认 `~/.local/state/fshub/`，Windows 默认 `%LOCALAPPDATA%\fshub`；服务化部署推荐 `/var/lib/fshub/`）。
- **`local_state_path` 默认不在 `data_path` 内，启动时必须校验二者不重叠**（可复用 `ensure_within` 做反向断言），否则用户把两者配成同一目录就会静默废掉整个守门——`data_path` 正是会被整体复制/同步的目录。
- Web 与 CLI 必须使用同一个 local producer identity。
- `producer_id` 缺失或被重建时，**不得静默生成新 ID 后继续既有 inc chain**；应要求 adopt 或 full rescan。
- 同一台机器上的多用户 / 多部署可能产生不同 `producer_id`（systemd 服务用户与交互用户的 `~/.local/state` 不同）。因此不匹配时的正确行为是**要求显式 adopt，而不是直接拒绝**；adopt 时按 §8.2 校验 source identity，无法确认则要求 full rescan。
- 现有 `calculate_thumbprint()` **不得**用于守门：它混入了 `platform.release()`（内核升级即变）、`platform.node()`、`uuid.getnode()`（换网卡即变，且取不到硬件地址时返回随机值）。它降级为纯展示性 provenance。

### 8.4 identity 闭包不变量

rebuild 期对每条可达记录的每个下标 `k` 校验：

> 若 `x[k] == traversed`，则 `records[child_path]` 必须存在；且当 `i` 与 `D[k]` 均非 `null`、指向同一 provider、且该 provider 的 `strength == "strong"` 时，`records[child_path]["i"]` 必须等于 `D[k]`。

这是**最终 overlay 状态的纯函数**，不依赖与上一代比较，因此不受 compaction 折叠历史的影响，也能发现多代以前写入的错误。跨 provider 或跨 epoch 的比较视为"不可比较"，跳过而不报错。

`identity_strength == none` **不会**自动使 snapshot 不完整：一次刚完成的全量扫描即使没有 native identity 也可以是完整的。identity 的准入要求属于**增量 backend 的规则**（§16）：一个依赖 identity 防止路径复用、且没有其他连续事件保证的 partial incremental generation，不得被声明为完整。

## 9. 扫描作用域

### 9.1 记录内容

`scan_scope` 记录 `root_path`、`skip_prefixes`（原始与归一化两份）、`cross_filesystems`。原始值用于展示与排障，归一化值用于比较——分开保存是为了防止有人为了显示美观而改动比较用的值。

### 9.2 归一化时机

归一化**必须在源主机上完成**并以 snapshot-native 形式写入 manifest。后续比较依据 `manifest.os_name`，**不得**用 loader 主机的 `os.path` 重新归一化：`os.path.abspath` 会用 loader 的 cwd 补全相对路径，`os.path.normcase` 只在 Windows 上折叠大小写，跨平台加载必然出错。

### 9.3 scope 等价判定

`skip_prefixes` 按**集合**比较（顺序无关），比较键按 `manifest.os_name` 计算：

- **Linux**：大小写敏感，精确比较。`/MNT` 与 `/mnt` 是不同 scope。
- **Windows**：保守折叠——`/` 与 `\` 统一为 `\`；ASCII `a-z` 转 `A-Z`（盘符随之大写）；去除非 root 的末尾分隔符；`.` / `..` 在源主机生成 scope 时即解析；**非 ASCII 字符保持原样**；UNC 路径的 server 与 share 组件同样参与折叠。`C:\Data` 与 `c:\data\` 等价。

不使用 `str.casefold()`：它存在扩展映射，且与 NTFS 卷内 `$UpCase` 表不等价。保守规则可能对某些非 ASCII Windows 路径造成"不必要的 full rescan"，但不会把两个不同 scope 误判为相同——correctness 上 false negative 比 false positive 安全。

**该折叠函数只用于回答"两次扫描配置是否等价"，绝不可用于 record key、index、去重或任何记录合并路径**（§6.6）。

## 10. 增量记录生成规则

增量层维护 dirty directory 集合。每个 dirty directory 都重新读取其当前完整内容，并在 inc 中写入一条完整目录记录。

### 10.1 dirty 触发条件

| 文件系统变化 | 必须标记的 dirty directory |
|---|---|
| 文件内容或元数据变化（**不含 atime**） | 文件所在目录 |
| 文件创建或删除 | 文件所在目录 |
| 子目录创建或删除 | 父目录 |
| 子目录自身元数据变化 | 父目录（该元数据保存在父目录的 `T` 中） |
| 新目录及其已有内容 | 父目录，以及新目录子树中的所有目录 |
| 路径变化 | 旧父目录、新父目录，以及新路径下的整棵子树 |
| identity 变化（strong provider） | 该目录的父目录，以及该目录子树中的所有目录 |
| source 出现 / 消失 / 替换 | 对应挂载点的父目录与整棵子树（§8.2） |

**atime 不作为 dirty 触发条件**，否则一次 `grep -r` 就能产生成千条记录；记录中的 atime 可能陈旧，这是有意的取舍。

变化检测后端可以报告 rename，但存储格式不定义 rename 操作。扫描层只把它转换为对应的 dirty directories 与新路径子树扫描。

### 10.2 强制不变量

> **任何目录结构变化都必须重新扫描父目录，并把父目录的完整记录写入同一个 inc。**
>
> **任一 inc 中，若父记录的 `d` 相对上一 generation 新增了名字，或某子目录的 identity 发生变化，则该子目录及其整棵子树的记录必须出现在同一个 inc 中。**

违反第二条会导致已删除子树在路径复用后重新可达，且不产生任何显式错误。loader 侧的兜底是 §8.4 的 identity 闭包校验（strong provider 时有效）；producer 侧必须在写 inc 前做同样的断言。

如果父目录无法成功读取，不能根据不完整结果提交删除。该 generation 应失败、重试，或按 §12 标记并触发全量扫描策略。root 不可读时不得提交该 generation；root 被删除时 snapshot 标记失效，而不是提交一棵空树。

### 10.3 跨文件系统

```
cross_filesystems = true    # 默认，兼容既有行为：dev 变化 ⇒ 整棵子树重扫
cross_filesystems = false   # 不同 dev 的子目录标记为 x = cross_device，不递归
```

默认值不能改：把 `dev` 变化默认解释为"不遍历边界"会让整棵已挂载的树从 snapshot 中静默消失。挂载抖动应在**变化检测层**限速或合并（必要时升级为 full rescan），而不是在存储格式层面回避。bind mount / overlayfs 会让同一棵树呈现不同 `dev`，此时重扫虽然浪费但结果正确。

### 10.4 Windows 合成根

`p = '/'` 的 "This PC" 记录不是真实目录。驱动器插拔时无父目录可重扫，规则为：重新枚举驱动器并重写该合成根记录，同时按 §8.2 处理对应 source 的出现与消失。

### 10.5 单次提交内的合并

同一 generation 中同一路径可能被多次标记。写 inc 前必须合并 dirty set，每个 `p` 最多写入一次，以最终扫描结果为准。

## 11. 加载算法

### 11.1 捕获 manifest revision

loader 先读 `current`（失败或落后则列举 `manifests/` 取最大且校验通过者），完整读入该 revision 的 manifest 并将其视为本次加载的固定输入。manifest revision 不可变，因此加载期间出现新 revision 不影响本次加载的一致性。

打开 manifest 引用的文件时若遇到 `FileNotFoundError`（GC 可能已回收旧 segment），**必须放弃当前构建、重新读取最新 manifest、从头重试**，而不是重试单个文件——后者可能把新 base 的内容与旧 manifest 的 inc 列表混用，得到一棵从未被提交过的树。重试次数有上限（建议 3 次），超限后返回 `SnapshotBusy`，与表示损坏的 `InvalidSnapshot` 区分开：二者的运维处置完全不同。

### 11.2 Overlay

以路径为 key 构建临时 map，按 manifest 顺序 last-write-wins：

```python
records = {}
for record in read_gzip_jsonl(base_file):
    records[record['p']] = record
for increment in manifest['increments']:
    for record in read_gzip_jsonl(increment['file']):
        records[record['p']] = record
```

base 与每个 inc **内部**不得重复出现同一个 `p`，发现重复即拒绝加载。不同 generation 之间重复出现同一个 `p` 是正常覆盖。

### 11.3 从 root 重建可达树

```python
snapshot_data, path_index, seen = [], {}, {}
pending = [root_path]

while pending:
    path = pending.pop()
    if path in seen:
        raise InvalidSnapshot(f'directory reachable from two parents or cycle: {path}')
    record = records.get(path)
    if record is None:
        raise InvalidSnapshot(f'missing directory record: {path}')
    seen[path] = True
    path_index[path] = len(snapshot_data)
    snapshot_data.append(record)

    for k in range(len(record['d']) - 1, -1, -1):
        if record['x'][k] != TRAVERSED:
            continue
        child = join_snapshot_path(path, record['d'][k], snapshot_os=snapshot_os)
        # identity closure check, see §8.4
        pending.append(child)
```

- 必须使用 snapshot OS 对应的路径函数，不得用运行 loader 的主机 `os.path` 解释 snapshot 路径。Windows snapshot 可以在 Linux 上加载，反之亦然。
- `snapshot_os` 取自 `manifest.os_name`，**不再**取自 `snapshot_data[0]`。
- overlay map 中存在但从 root 不可达的记录不是错误，它们是删除或路径变化后留下的旧记录，直接忽略。

### 11.4 拒绝加载的条件

- manifest checksum 不符、canonical JSON 违规（重复 key、浮点数、`NaN` / `Infinity`、整数越界）；
- `format_version` 不支持；`snapshot_id` 与目录名不符；
- generation / revision 序列无效；`kind` 与 generation 关系不符（§7.2）；
- manifest 引用的文件路径逃逸 snapshot 目录；
- 引用的 base/inc 不存在（按 §11.1 重试后仍缺失）；
- gzip 或 JSONL 损坏、截断；实际记录行数与 `record_count` 不符；文件 `size` / `sha256` 不符；
- 记录缺少 `p/i/f/s/t/c/d/T/D/x` 中任何一个，或平行数组长度不一致，或 `c` 为全 `null` 数组，或 `f == []` 时 `c` 不是 `null`；
- 未知的 `x` 状态码或未知的 provider ID；
- 记录未按 §6.5 排序，或 `p` 违反 §6.6；
- 同一个文件内部包含重复的 `p`；
- root 记录不存在；
- `x[k] == traversed` 但子记录缺失；identity 闭包校验失败（§8.4）；
- rebuild 发现目录环或同一路径被不同父目录引用。

### 11.5 重建统计

rebuild 完成后重新计算所有运行时字段，它们**不写入** base 或 inc：

- `S`：目录递归逻辑大小；
- `C`：目录递归文件数；
- `LS` / `LC`：排除 `not_fully_local` 文件后的对应值。

### 11.6 计算 logical_state_digest

digest 是对**重建后逻辑可达树**的哈希，不是对文件字节的哈希：

- 遍历顺序：从 `root_path` 起的确定性 DFS，子目录按记录中 `d` 的顺序（已按 §6.5 排序），与记录在 base/inc 中的物理顺序无关；
- 每条记录以 §7.5 的 canonical JSON 参与哈希，**包含** `p`、`f`、`s`、`t`、`c`、`d`、`T`、`x`；
- identity 按**解析后的 `(scheme, value)`** 参与，**不得使用 provider ID**——否则 compaction 重新编号就会改变 digest，与"纯 compaction digest 不变"直接冲突；
- **排除**运行时字段 `S` / `C` / `LS` / `LC`；
- 不可达记录不参与。

计算方：full rescan / compaction 对其输出的完整记录流计算；loader 在 rebuild 时顺带累计；只提交普通 inc 且不持有完整逻辑树的 writer 写 `null`。

## 12. 完整性模型

### 12.1 两个轴

| 轴 | 取值 | 计算方式 |
|---|---|---|
| `event_continuity` | `complete` / `gap` / `not_applicable` | **对 chain 的折叠**（历史 gap 不会因后续正常增量而消失） |
| `observation_coverage` | `complete` / `partial` | **最终可达树的纯函数**（由树中 `x` 状态实时派生） |

```
continuity(base) = base.event_continuity        # full_rescan 为 not_applicable 或 complete
continuity(n)    = gap             if gen[n].event_continuity == "gap"
                 = continuity(n-1) otherwise

coverage(current) = complete  if 最终可达树中不存在 x ∈ {denied, error}
                  = partial   otherwise         # 与历史无关

consistency = derived(continuity, coverage)     # complete / partial / stale_or_unknown
```

`consistency` 只是派生缓存，**不可独立写入**；loader 每次加载重算并与 manifest 缓存值比对，不一致视为 manifest 错误。API 与内存 entry 必须同时携带两个轴的原始值——派生值是有损的（`partial` 与 `gap` 同时成立时会坍缩），机器读取方应使用双轴。

### 12.2 denied 与 skipped

- `denied` / `error` **永不破坏** `event_continuity`；
- 只要当前可达树中仍存在 `denied` / `error`，`observation_coverage` 就保持 `partial`——一个连续多代不可读的目录，恰恰意味着无法知道其内部是否变化。去重与告警降级是 UI 行为，不改变 correctness 状态；
- 上一代 denied、本代成功重扫的目录，coverage 立即恢复，不必等待 full rescan；
- 曾 denied 的目录若在后续 generation 中被删除，它退出可达树，coverage 随之恢复；
- 用户主动配置排除的路径记为 `skipped`，属于**声明范围之外**，不降低 scope 内的完整性。这也是让完整性标志具备信号价值的关键：显式缩小 scope，而不是降低标志的含义；
- per-generation 保留 denied / error 的计数与样本用于诊断，权威值一律现算。

### 12.3 恢复

- 只有一次**真正成功的全量扫描**（`kind = full_rescan`）能把 `event_continuity` 重置为 complete；
- `checkpoint_compaction` **不能**恢复完整性，它只是把当前（可能缺数据的）树写成新 base，其 `event_continuity` / `observation_coverage` **必须等于被折叠区间的折叠结果**，不得取任何单代的值，也不得重置；
- compaction 会抹掉"哪一代出过 gap"的信息，manifest 应保留一小段只增不改的事件历史（最近 N 条 `{generation, kind, reason, at}`）用于排障。

### 12.4 变化检测层的义务

- Windows NTFS 可保存 USN Journal ID 与 USN cursor；
- Windows 其他场景可用 `ReadDirectoryChangesW`，进程停机或缓冲区溢出后可能需要全量扫描；
- Linux 可用 inotify，进程停机、`IN_Q_OVERFLOW`、watch 丢失或文件系统卸载后可能需要全量扫描。

同一 backend 下 `inc[n].begin_cursor` 必须等于 `inc[n-1].end_cursor`，否则该 generation 的 `event_continuity` 必须记为 `gap`。**无法证明 cursor 连续时，不得把生成的 inc 宣称为完整更新。**

## 13. 提交协议

### 13.1 写者互斥

- **进程内**：`threading.Lock`（fshub 是多线程 Flask 服务且 CLI 可能同机并发）；
- **进程间**：`flock`（或 Linux 的 `F_OFD_SETLK`）/ Windows `msvcrt.locking`。二者随进程退出自动释放，不会留下 stale lock。`msvcrt.locking` 是字节范围强制锁，需保持 fd 打开并锁定固定区间（约定锁第 0 字节 1 字节）。
- **显式排除 POSIX `fcntl(F_SETLK)`**：它是 per-process 语义且任一 fd 关闭即释放全部锁。
- `flock` 在 NFS 上由 POSIX 字节范围锁模拟，`-o local_lock` 会改变语义；网络文件系统上的锁语义不可靠，因此第一版明确**只支持单个受控 writer**。
- GC 与 compaction 使用**同一把** writer lock（§15）。

### 13.2 提交顺序

1. 在内存中确定并合并 dirty directories；
2. 重新扫描所有 dirty directories，任何必要目录读取失败时不提交该 generation；
3. 排序并写入 `increments/inc_gen_<gen>_<ts>.jsonl.gz.<pid>-<uuid>.tmp`；
4. 关闭 gzip 层 → `flush()` 底层 fd → `os.fsync(fd)` → `close()`；
5. `os.replace()` 为正式 inc 文件名；
6. POSIX：`fsync(increments/)`；
7. 计算新 manifest payload 与 checksum，写入 `manifests/manifest_rev_<n>.json.<pid>-<uuid>.tmp`，`flush` → `fsync` → `close`；
8. **断言目标 `manifest_rev_<n>.json` 不存在**，存在则报错而不是覆盖；
9. `os.replace()` 为正式 manifest 文件名；
10. POSIX：`fsync(manifests/)`；
11. best-effort 更新 `current`。

第 8 步在 `os.replace` 路径下是 TOCTOU-racy 的，**其正确性依赖 writer lock**；它是"manifest revision 不可变"这一协议不变量的断言，**不是并发控制**，不得被后人误当作 CAS。

gzip 实现注意：`gzip.open(path, 'wt')` 不暴露底层 fd。必须 `open(path, 'wb')` + `gzip.GzipFile(fileobj=raw)`，先关 gzip 层再 fsync raw fd，否则会出现"fsync 了但 gzip 尾部仍在缓冲区"。

### 13.3 平台能力

| 平台 | 文件 fsync | 目录 fsync | 替换原子性 |
|---|---|---|---|
| POSIX | `os.fsync(fd)` | 支持（`os.open(dir, O_RDONLY)` + `os.fsync`） | `os.replace` 同文件系统内原子 |
| Windows | `os.fsync(fd)`（`FlushFileBuffers`） | 不支持（无法 `os.open` 目录），依赖 NTFS 日志 | `os.replace` 同卷原子；目标被占用时可能失败 |

`fsync` 保证数据到达设备，不保证设备写缓存已落盘（无 barrier/FUA 时）。

### 13.4 中断处理

| 状态 | 含义 |
|---|---|
| `.tmp` 文件存在 | 未提交，loader 忽略，由 GC 按命名规则与最小年龄清理 |
| 正式 inc 存在但无 manifest 引用 | 未提交的 orphan，loader 忽略，可清理 |
| manifest revision 已发布 | 其引用的全部文件必定已完整写入 |

只有出现在某个已发布 manifest revision 中的文件才是已提交状态。

## 14. 内存发布与并发

### 14.1 entry 结构

```python
loaded_snapshots[snapshot_id] = {
    'data': snapshot_data,
    'index': path_index,
    'groups': groups,
    'os_name': manifest['os_name'],
    'root_path': scan_scope['root_path'],
    'snapshot_generation': 12,
    'manifest_revision': 21,
    'logical_state_digest': 'sha256:…',      # 本次 rebuild 顺带算出
    'event_continuity': 'complete',
    'observation_coverage': 'partial',
    'consistency': 'partial',
}
```

刷新流程：在全局 snapshot lock **外**完成读取、overlay、rebuild、校验、统计与 digest，构造一个完整的新 entry，再在 lock 内**一次性替换**。这样并发请求要么看到完整的旧版本，要么看到完整的新版本；**不得原地修改**正在被读取的 `data` 或 `index`。

### 14.2 compaction fast path

已加载视图跳过 rebuild 的完整条件（缺一不可）：

1. `manifest_revision` 变化；
2. `snapshot_generation` 相同；
3. 双方 `current_logical_state_digest` 均非 `null` 且相等；
4. `scan_scope` 相同；
5. `sources` 相同；
6. 双轴完整性相同。

**`snapshot_generation` 不同时，无论 digest 是否存在都必须刷新逻辑树**；fast path 只服务于 generation 相同的 compaction / metadata-only revision。

### 14.3 版本暴露

API 响应与任务状态应暴露 `snapshot_generation`、`manifest_revision`、双轴完整性。`getPath?index=N` 的下标**只在某个 generation 内有效**，响应必须回带 generation。§6.5 的排序让未变化子树的下标趋于稳定，但前面子树的增删仍会整体位移后续下标。

## 15. Compaction 与 GC

### 15.1 触发条件

- inc 数量达到阈值（例如 20）；
- inc 压缩后总大小超过 base 的一定比例（例如 30%）；
- inc 累计记录数超过 base 记录数的一定比例（例如 50%）；
- 不可达记录占比超过阈值（删除的成本只能靠 compaction 回收）；
- 实际加载时间超过阈值。

compaction 必须**限速**，否则会让 loader 反复触发 §11.1 的整体重试而活锁。

### 15.2 流程

1. 取 writer lock；
2. 加载当前 revision 并 rebuild 可达树；
3. 把可达记录按 §6.5 排序写入新 segment 的临时 base，顺带计算 `logical_state_digest`；
4. 关闭、fsync、校验；
5. 发布新 manifest revision：`kind = compaction`，`snapshot_generation` 不变，`base.kind = checkpoint_compaction`，双轴取被折叠区间的折叠结果，`increments` 置空，provider 表可重新编号；
6. 把被取代的文件追加进 `gc.jsonl`。

### 15.3 GC

GC 与 writer 共用同一把锁，因此运行时不存在活跃 writer，保护集可以简化：

```
protected = closure(current_manifest_revision)
          ∪ closure(最近 K 个已校验的 previous revision)
          ∪ {年龄 < min_age 的临时 / 孤儿文件}

deletable = (由历史 revision 差分得出的 superseded 集合) − protected
```

- **不得**对目录做"磁盘文件 − 当前 live set"的集合差删除；
- 孤儿与临时文件走独立清理策略（命名规则 + 最小年龄）；
- 删除必须幂等并容忍 `ENOENT`（`gc.jsonl` 可重放）；Windows 上文件被 reader 打开时 `os.remove` 会失败，GC 记录并延后重试；
- `gc.jsonl` 丢失后可由相邻 revision 差分重建，因此它是重试缓存而非权威；
- **GC 应分批、可中断**：候选集计算在锁外完成，取锁后重新校验保护集、删除一个有界批次即释放。GC 不是延迟敏感路径，但增量提交是。

慢 reader 的保护由三者叠加：保留最近 K 个 revision 的依赖闭包、最小年龄宽限期、以及 §11.1 的整体重试。

## 16. 变化检测后端要求

后端必须能回答"自 `begin_cursor` 起的事件是否连续"。无法回答时，其产出的 generation 必须记为 `event_continuity = gap`，并按 §12.3 触发全量扫描策略。

若某后端依赖 identity 防止路径复用（例如只上报"目录变化"而不上报子树内容），则该后端的准入条件包含"所涉卷的 identity provider 为 strong"；这是 backend 规则，不是通用完整性规则（§8.4）。

## 17. 对现有模块的影响

### `fshub/scanning.py`

- 全量扫描改为创建 snapshot 目录、base、provider 表与 manifest revision 0；
- 新增 dirty directory 扫描与 inc 写入；
- 记录按 §6.5 排序，写出前断言平行数组等长；
- 采集 `i` / `D` 必须使用 `os.stat`，**不得**改用 `DirEntry.stat()`（Windows 上不返回 `st_ino`/`st_dev`）；现有实现已对每个子目录调用 `os.stat`，无额外成本；
- `gzip.open` 改为 `open(path,'wb')` + `gzip.GzipFile(fileobj=raw)` 以便 fsync；
- 保持 `json.dumps` 默认的 `ensure_ascii=True`（§6.7），**不得改为 False**；
- 设备与扫描元数据写入 manifest，不再写进 `scan_result[0]`；
- 删除 `save_scan_result(use_index=...)` 及 `.bin.gz` 分支。

### `fshub/api/explorer.py`

- snapshot 列表改为枚举含合法 manifest 的目录，返回 `snapshot_generation`、`manifest_revision`、`record_count`、`inc_count`、`total_size`、双轴完整性；
- `_read_snapshot_records()` 改为读取 manifest、base 与 inc 并执行 overlay / rebuild / 校验 / 统计 / digest；
- 8 处 `data[0].get('os_name')`（`explorer.py:80,197,427,517,564,642,730`、`groups.py:42`、`search.py:80`）改为读取 entry 元数据；`EMPTY_SNAPSHOT` 同步补齐字段；
- 加载异常捕获列表补 `IndexError`（强制索引后平行数组问题会以 `IndexError` 形式出现，当前会变成 500 而非 400）；
- 区分 `SnapshotBusy`（重试）与 `InvalidSnapshot`（损坏）。

### API / 前端

- snapshot 标识由文件名改为 `snapshot_id`，`filename` 字段更名，`load_snapshot` / `unload_snapshot` / `getPath` / `groups/<id>` / backup / hashes 与 `templates/index.html` 同步；
- 响应回带 generation 与完整性状态。

### `fshub/config/__init__.py`

- 新增 `local_state_path`，默认与 `data_path` 分离，启动时校验不重叠（§8.3）。

### Groups / Search / Hash / Backup

- group log 位于 snapshot 目录内，绑定稳定的 `snapshot_id`；删除后残留的 group path 按最终 path index 忽略，清理可在 compaction 时进行；
- 这些模块只能读取 rebuild 后的 `snapshot_data` 与 `path_index`，不得直接遍历 overlay map；
- 启动异步任务时记录 `snapshot_generation`，以便任务日志说明其使用的数据版本；
- 回到真实文件系统的路径操作必须按 §6.7 用 `surrogateescape` 反向编码。

## 18. 测试要求

**基础**

1. 只有 base 的 snapshot 可以加载。
2. 一个或多个 inc 按 generation 正确覆盖相同路径。
3. 文件新增、修改、删除。
4. 目录新增与删除。
5. 删除目录后旧子树不进入最终 data / index / 搜索结果。
6. `x = traversed` 但子记录缺失时拒绝加载。
7. Windows "This PC" 根与驱动器路径可以 rebuild。
8. Linux 文件名中的反斜杠不被当作分隔符。
9. generation / revision 序列不合法时拒绝加载。
10. 未被任何 manifest 引用的临时或 orphan 文件被忽略。
11. 截断或损坏的 inc 不会发布为 loaded snapshot。
12. 加载期间提交新 revision 时，本次加载仍得到一致的旧版本。
13. 内存刷新以完整 entry 原子替换，不暴露半构建状态。
14. compaction 前后的最终可达树、index 与递归统计完全一致。

**遍历状态与 identity**

15. 父目录 `d` 含 symlink / junction / 不可读目录时仍能正常加载。
16. identity 语义回归：目录内新建文件后其 identity **不得**变化；删除后同名重建时 identity **必须**变化（strong provider）。
17. 同一 generation 窗口内删除并同名重建目录：dirty set 完整时旧子树不复活；不完整时被 identity 闭包校验拒绝。
18. `identity_strength == none` 时跳过闭包校验且不报错；full rescan 结果仍可为完整。
19. 同一 snapshot 混用 strong / weak / none provider 时加载正常；未知 provider ID 被拒绝。
20. provider epoch 变化（模拟 `journal_id` 改变）后追加新 ID，跨 epoch identity 判为"不可比较"而非"不匹配"。
21. compaction 重新编号 provider ID 后，旧 revision 仍用旧表正确解析；`logical_state_digest` 保持不变。
22. 未知 `x` 状态码导致拒绝加载。

**完整性**

23. `gen5.event_continuity = gap` + `gen6` 正常增量 ⇒ snapshot 仍不完整；一次成功 `full_rescan` 后恢复。
24. 对不完整 chain 执行 `checkpoint_compaction` 后仍不完整。
25. 持久 denied 保持 `observation_coverage = partial`；本代成功重扫后立即恢复。
26. 曾 denied 的目录被删除后退出可达树，coverage 恢复 complete。
27. manifest 中缓存的 `consistency` 与折叠结果不一致时按 manifest 错误处理。
28. cursor 断裂时该 generation 被记为 `gap`。
29. 仅 atime 变化不产生 inc。

**身份与作用域**

30. `skip_prefixes` 仅顺序或写法不同（`/mnt` vs `/mnt/`）时不触发 full rescan。
31. `skip_prefixes` 或 `cross_filesystems` 变化时拒绝普通 incremental commit 并要求 full rescan；`root_path` 变化时要求创建新的逻辑 snapshot。
32. Linux 下 `/MNT` 与 `/mnt` 为不同 scope；Windows 下 `C:\Data` 与 `c:\data\` 为同一 scope；两者都不影响 record key 的精确比较。
33. Windows snapshot 在 Linux 上加载时，scope 比较使用 `manifest.os_name` 规则。
34. 整个 `data_path` 被复制到另一台机器后继续提交 inc 被拒绝（验证 producer id 不在 `data_path` 内）。
35. 内核版本、主机名或 MAC 变化后仍可继续向同一 snapshot 提交（验证不依赖 thumbprint）。
36. 多 source 拓扑被正确记录；某路径 source identity 变化触发对应子树重扫；挂载覆盖非空目录时旧记录被替换而非合并；卸载后底层内容重新出现时子树被重扫。
37. `cross_filesystems=true` 时挂载点内容替换后整棵子树被重扫；`false` 时该子树标记为 `cross_device` 且不递归。

**提交、并发与 GC**

38. 写 manifest 中途崩溃后，下一次提交仍能成功发布新 revision。
39. 断电模拟：inc 已 replace 但 manifest 未落盘 ⇒ 加载到旧 revision；manifest 已落盘 ⇒ 加载到新 revision。
40. 写者持锁期间被 `SIGKILL`，新写者能立即取得锁（无 stale lock）。
41. 并发提交时，目标 revision 已存在的断言生效，不发生覆盖。
42. 慢 reader 撞上 compaction：整体重试后得到某一版本的一致视图；超过重试上限时返回 `SnapshotBusy` 而非 `InvalidSnapshot`。
43. Windows 下旧 base 被 reader 打开时 compaction 不失败，文件由后续 GC 删除。
44. GC 持锁期间的增量提交延迟有界（分批释放锁）。
45. `gc.jsonl` 中误列在用文件时不会被删除。

**格式与序列化**

46. 目录中含不可解码字节的文件名（如 `b'bad\xff.txt'`）时，扫描、写入、加载、digest、路径查询全链路正常，且可反向编码回原始字节。
47. manifest 的原始 JSON 字节与 `.jsonl.gz` **解压后**的每条 JSONL 字节必须为纯 ASCII（gzip 压缩数据本身不受此约束）。
48. manifest 含重复 key / `NaN` / `Infinity` / 浮点数 / 越界整数时拒绝加载；checksum 被篡改或截断时拒绝加载。
49. 空目录的 `c` 必须为 `null`；全 `null` 数组被拒绝。
50. 目录内容相同但枚举顺序不同的两次扫描产生逐字节相同的记录，且 digest 相同。
51. 实际行数与 `record_count` 不符、或 `size` / `sha256` 不符时拒绝加载。
52. inc 中记录的 `p` 与父记录派生路径大小写 / 分隔符不一致时被拒绝。
53. manifest 的 `file` 含 `../` 或绝对路径时拒绝加载。

**digest 与 fast path**

54. `snapshot_generation` 不同时，无论 digest 是否存在都必须刷新逻辑树；仅当 generation 相同的 compaction / metadata-only revision 中，digest 为 `null` 才禁止 fast path。
55. 纯 compaction（generation、digest、scope、sources、双轴均不变）不触发内存树重建；任一不一致时拒绝该 manifest 或强制重建。
56. compactor 发布逻辑不同的 base 却沿用旧 digest 时，首次加载 / 后台校验能检出不符。

## 19. 未来工作与已否决方案

### 19.1 未来可选

- **no-replace publish**：POSIX `os.link(tmp, target)`（目标存在即 `FileExistsError`，成功后 `st_nlink == 2`；NFS 的 ambiguous result 需以 `samefile` 校验）、Windows `os.rename(tmp, target)`（文档规定 dst 存在时总是抛 `FileExistsError`）、Linux `renameat2(RENAME_NOREPLACE)`（需 ctypes）。要求 tmp 与 target 同卷、发布前 fsync tmp、POSIX 发布后 fsync 目录。**属于平台 backend，不进入 v1 的正确性必需路径**；采用后可把单 writer 约束放宽为乐观并发。
- **Merkle / 目录级摘要**：使普通 inc 也能在 O(变化量) 内维护 `current_logical_state_digest`。v1 不具备该能力，故允许 digest 为 `null`。
- **递归统计增量更新**：只沿变化路径的祖先链更新 `S/C/LS/LC`，避免每次刷新全树重算。
- **子树级完整性恢复**：需要把完整性从标量变成按路径的格，v1 明确不做。

### 19.2 已否决（不要重新提出）

| 方案 | 否决原因 |
|---|---|
| `(dev, ino, ctime_ns)` 作 incarnation identity | POSIX 的 ctime 是 metadata change time，目录项增删即变；它是 revision 不是 identity。时间戳粒度也不保证纳秒 |
| `(dev, ino)` 作 identity | inode 删除后会被立即复用 |
| `os.stat().st_ino` 无条件视为 strong identity | Microsoft 不保证 file ID 跨时间唯一；ReFS/FAT/SMB 语义不同 |
| `open(path, 'xb')` 直接写最终 manifest 作 lock-free CAS | 目录项在写入前即可见；writer 崩溃会留下永久无效的 revision，使后续提交永久堵塞 |
| `fcntl(F_SETLK)` 作进程间锁 | per-process 语义，任一 fd 关闭即释放全部锁 |
| `O_CREAT\|O_EXCL` 锁文件 | 进程崩溃留下 stale lock |
| manifest generation "recheck" 作 CAS | 读—确认—替换存在 TOCTOU 窗口，不是原子条件替换 |
| full-coverage generation ≡ compaction | 存储形态相同但语义不同：compaction 不能恢复丢失的变化 |
| 单一 `consistency` 标量 | 无法区分事件断裂与未观察区域，二者的恢复方式完全不同 |
| GC 的 staged file registry | 需要声明持久化、崩溃清理、heartbeat 判活、网络 FS 可见性四套机制；共用 writer lock 即可 |
| `ensure_ascii=False` | 不可解码的 POSIX 文件名会导致 `UnicodeEncodeError`，整个 snapshot 无法写入 |
| digest 由"本次写出的记录流"计算 | 对普通 inc 只是 dirty 子集，算不出逻辑树摘要 |
| `data_path` 内存放 `producer_id` | 该目录会被整体复制/同步，守门形同虚设 |
| `calculate_thumbprint()` 作设备身份 | 内核升级、改主机名、换网卡均会改变；取不到 MAC 时返回随机值 |
| 省略空的核心数组字段 | gzip 下收益极小，却会掩盖 producer 的字段遗漏 |
| `x: null` 标量哨兵 | `x` 与子目录数平行，量级远小于 `c`，收益不足以换取第二种编码形式 |
| `str.casefold()` 作 Windows scope 折叠 | 存在扩展映射，与 NTFS `$UpCase` 不等价；过度折叠会把不同 scope 误判为相同 |
| `dev` 变化默认标记为 `cross_device` 边界 | 会让整棵已挂载的树从 snapshot 中静默消失 |
