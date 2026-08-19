# 增量 Snapshot 设计

## 1. 状态

本文档定义 fshub 新的 snapshot 存储和加载格式。该格式由一个全量 base 和若干增量 inc 组成。

本设计不兼容现有的单文件 snapshot 格式，也不要求迁移或读取旧格式。

## 2. 目标

- 完整扫描只生成一次 base，后续扫描只保存有变化的目录记录。
- base 和 inc 使用完全相同的目录记录格式。
- 支持文件和目录的新增、修改与删除。
- 不引入文件级或字段级 patch。
- 不引入 delete、tombstone 或 rename 操作记录。
- 加载后从根目录重建可达文件树、路径索引及递归统计。
- snapshot 的一次更新可以原子提交；加载方不会看到半写入的 inc。
- 支持后续增加 Windows USN、ReadDirectoryChangesW 和 Linux inotify 等变化检测后端。

## 3. 非目标

- 不兼容当前的 `snapshot_*.jsonl.gz` 和 `_index.jsonl.gz` 格式。
- 本文档不规定第一版必须实现所有平台的变化检测后端。
- snapshot 存储格式不负责证明变化事件没有丢失；事件连续性由变化检测层和 manifest cursor 共同保证。
- 不提供文件系统级 point-in-time 一致性。普通全量或增量扫描期间，源文件系统仍可能变化。

## 4. 核心模型

一个逻辑 snapshot 对应一个目录：

```text
snapshots/
└── snapshot_1700000000_a1b2c3d4/
    ├── manifest.json
    ├── base.jsonl.gz
    ├── groups.jl
    └── increments/
        ├── inc_000001_1700000100.jsonl.gz
        ├── inc_000002_1700000250.jsonl.gz
        └── inc_000003_1700000400.jsonl.gz
```

其中：

- `manifest.json` 描述 snapshot、base 和已提交的 inc。
- `base.jsonl.gz` 包含完整扫描得到的所有目录记录。
- `increments/` 中的每个文件包含该 generation 发生变化的完整目录记录。
- `groups.jl` 继续保存该逻辑 snapshot 的 group action log。

inc 文件名同时包含 generation 和 Unix 时间戳：

```text
inc_<generation:06d>_<unix_timestamp>.jsonl.gz
```

generation 是加载顺序和唯一性的依据；时间戳只用于展示和诊断。不能只按时间戳决定顺序，因为同一秒可能生成多个 inc。

## 5. 目录记录格式

base 和 inc 中的每一行都是一个 gzip 压缩的 JSONL 目录记录：

```json
{
  "p": "/data/docs",
  "f": ["a.txt", "b.txt"],
  "s": [10, 20],
  "t": [[1700000000, 1700000001, 1700000002], [1700000010, 1700000011, 1700000012]],
  "c": [null, "pinned"],
  "d": ["archive"],
  "T": [[1700000020, 1700000021, 1700000022]]
}
```

字段含义：

| 字段 | 含义 |
|---|---|
| `p` | 当前目录的绝对路径 |
| `f` | 当前目录中的文件名 |
| `s` | 文件大小，与 `f` 平行 |
| `t` | 文件的 `[ctime, mtime, atime]`，与 `f` 平行 |
| `c` | 文件的云文件状态，与 `f` 平行；普通或未知文件为 `null` |
| `d` | 当前目录中的子目录名 |
| `T` | 子目录的 `[ctime, mtime, atime]`，与 `d` 平行 |

同一个 `p` 在后续 inc 中出现时，新的完整记录覆盖此前记录。不存在字段合并或数组 patch。

## 6. Manifest 格式

建议的 `manifest.json`：

```json
{
  "format_version": 1,
  "snapshot_id": "snapshot_1700000000_a1b2c3d4",
  "root_path": "/data",
  "os_name": "Linux",
  "device": {
    "device_name": "server-1",
    "device_id": "device-thumbprint",
    "host_name": "server-1",
    "cpu_model": "Example CPU",
    "memory_size": 34359738368,
    "ip_addr": "192.0.2.10",
    "mac_addr": "00:11:22:33:44:55"
  },
  "base": {
    "file": "base.jsonl.gz",
    "generation": 0,
    "created_at": 1700000000,
    "start_scan_time": 1699999900,
    "finish_scan_time": 1700000000,
    "record_count": 100000
  },
  "current_generation": 3,
  "increments": [
    {
      "generation": 1,
      "file": "increments/inc_000001_1700000100.jsonl.gz",
      "created_at": 1700000100,
      "start_scan_time": 1700000098,
      "finish_scan_time": 1700000100,
      "record_count": 12,
      "change_backend": "inotify",
      "begin_cursor": "100",
      "end_cursor": "180"
    },
    {
      "generation": 2,
      "file": "increments/inc_000002_1700000250.jsonl.gz",
      "created_at": 1700000250,
      "start_scan_time": 1700000249,
      "finish_scan_time": 1700000250,
      "record_count": 3,
      "change_backend": "inotify",
      "begin_cursor": "180",
      "end_cursor": "220"
    },
    {
      "generation": 3,
      "file": "increments/inc_000003_1700000400.jsonl.gz",
      "created_at": 1700000400,
      "start_scan_time": 1700000395,
      "finish_scan_time": 1700000400,
      "record_count": 25,
      "change_backend": "inotify",
      "begin_cursor": "220",
      "end_cursor": "310"
    }
  ]
}
```

要求：

- `format_version` 必须是 loader 支持的版本。
- `snapshot_id` 必须与 snapshot 目录名一致。
- base 的 generation 固定为 `0`。
- inc generation 必须从 `1` 开始连续递增。
- `current_generation` 必须等于最后一个 inc generation；没有 inc 时为 `0`。
- inc 必须严格按照 manifest 中的顺序加载，不能通过扫描目录或文件名猜测提交顺序。
- `root_path` 和 `os_name` 是 rebuild 路径时的依据。
- `begin_cursor`、`end_cursor` 的具体类型由变化检测后端定义；尚未接入后端时可以为 `null`。
- 设备信息和扫描级元数据存放在 manifest，不再混入第一个目录记录。

## 7. 增量记录生成规则

增量层维护 dirty directory 集合。每个 dirty directory 都重新读取其当前完整内容，并在 inc 中写入一条完整目录记录。

| 文件系统变化 | 必须标记的 dirty directory |
|---|---|
| 文件内容或元数据变化 | 文件所在目录 |
| 文件创建或删除 | 文件所在目录 |
| 子目录创建或删除 | 父目录 |
| 子目录自身元数据变化 | 父目录，因为该元数据保存在父目录的 `T` 中 |
| 新目录及其已有内容 | 父目录，以及新目录子树中的所有目录 |
| 路径变化 | 旧父目录、新父目录，以及新路径下的目录子树 |

变化检测后端可以报告 rename，但 snapshot 存储格式不定义 rename 操作。扫描层只把它转换为对应的 dirty directories 和新路径子树扫描。

在同一个 generation 中，同一路径可能被多次标记。写 inc 前应合并 dirty set，并且每个 `p` 最多写入一次，以最终扫描结果为准。

## 8. 删除语义

### 8.1 文件删除

文件没有独立目录记录。删除文件时，重新扫描文件所在目录。新记录的 `f/s/t/c` 中不再包含该文件，加载时完整覆盖旧目录记录。

### 8.2 目录删除

删除目录时，重新扫描其父目录。新父目录记录的 `d/T` 中不再包含该目录。

旧目录及其后代的记录仍可能存在于 base 或旧 inc 合并得到的临时 map 中，但它们不再能从 root 到达。rebuild 阶段会丢弃所有不可达记录，所以不需要 tombstone 或 delete 操作。

该语义依赖以下强制规则：

> 任何目录结构变化都必须重新扫描父目录并把父目录完整记录写入 inc。

如果父目录无法成功读取，不能根据不完整结果提交删除。该 generation 应失败、重试，或被标记为不完整并触发全量扫描策略。

## 9. 加载算法

### 9.1 捕获 manifest generation

loader 首先完整读取一次 `manifest.json`，并将其视为本次加载的固定输入。base 和 inc 文件一旦提交后不可修改，因此即使加载期间出现新 generation，本次加载的旧 generation 仍然有效。

### 9.2 Overlay

以路径为 key 构建临时 map，按 manifest 顺序执行 last-write-wins：

```python
records = {}

for record in read_gzip_jsonl(base_file):
    records[record['p']] = record

for increment in manifest['increments']:
    for record in read_gzip_jsonl(increment['file']):
        records[record['p']] = record
```

base 和每个 inc 内不应重复出现同一个 `p`。发现重复记录时 loader 应拒绝加载，避免文件生成错误被静默掩盖。不同 generation 中重复出现同一个 `p` 是正常覆盖。

### 9.3 从 root 重建可达树

overlay map 允许包含已经不可达的旧记录。最终 `snapshot_data` 必须从 manifest 的 `root_path` 开始迭代重建：

```python
snapshot_data = []
path_index = {}
visited = set()
pending = [root_path]

while pending:
    path = pending.pop()
    if path in visited:
        continue

    record = records.get(path)
    if record is None:
        raise InvalidSnapshot(f"missing directory record: {path}")

    visited.add(path)
    path_index[path] = len(snapshot_data)
    snapshot_data.append(record)

    for dirname in reversed(record.get('d', [])):
        child_path = join_snapshot_path(
            path,
            dirname,
            snapshot_os=snapshot_os,
        )
        pending.append(child_path)
```

必须使用 snapshot OS 对应的路径函数，不能使用运行 loader 的主机 `os.path` 解释 snapshot 路径。Windows snapshot 可以在 Linux 上加载，Linux snapshot 也可以在 Windows 上加载。

### 9.4 完整性检查

以下情况必须拒绝加载：

- manifest 格式或 generation 序列无效；
- manifest 引用的 base/inc 不存在；
- gzip 或 JSONL 损坏、截断；
- 目录记录缺少合法的 `p/f/s/t/c/d/T`；
- 平行数组长度不一致；
- 同一个文件内部包含重复的 `p`；
- root record 不存在；
- 可达父目录的 `d` 声明了子目录，但 overlay map 中不存在对应子目录记录；
- rebuild 发现目录环或同一路径被不同父目录引用。

map 中存在但从 root 不可达的记录不是错误，它们是删除或路径变化后留下的旧记录，直接忽略。

### 9.5 重建统计

rebuild 完成后重新计算所有运行时字段：

- `S`：目录递归逻辑大小；
- `C`：目录递归文件数；
- `LS`：排除 `not_fully_local` 文件后的递归逻辑大小；
- `LC`：排除 `not_fully_local` 文件后的递归文件数。

这些字段不写入 base 或 inc。

## 10. 原子提交

提交一个新 generation 的顺序：

1. 在内存中确定并合并 dirty directories。
2. 重新扫描所有 dirty directories；任何必要目录读取失败时不提交该 generation。
3. 写入 `increments/inc_<generation>_<timestamp>.jsonl.gz.tmp`。
4. 关闭 gzip 文件，确认写入成功。
5. 使用 `os.replace()` 将临时 inc 改为正式文件名。
6. 基于旧 manifest 创建包含新 inc 的 `manifest.json.tmp`。
7. 使用 `os.replace()` 原子替换 `manifest.json`。

只有 manifest 中列出的 inc 才是已提交状态。

可能出现的中断及处理：

- 临时 inc 存在：未提交，loader 忽略，可清理。
- 正式 inc 存在但 manifest 未引用：未提交的 orphan inc，loader 忽略，可清理。
- manifest 已引用 inc：inc 必须已经完整写入并改为正式文件名。

如果需要抵抗系统掉电而不仅是进程崩溃，可以在正式发布前增加文件和目录 `fsync`；是否启用由后续 durability 要求决定。

## 11. 并发与内存发布

loaded snapshot 应记录其 generation：

```python
loaded_snapshots[snapshot_id] = {
    "data": snapshot_data,
    "index": path_index,
    "groups": groups,
    "generation": manifest["current_generation"]
}
```

生成新 inc 后不能原地修改正在被请求读取的 `data` 或 `index`。刷新流程应当：

1. 在全局 snapshot lock 外读取 manifest、base 和 inc；
2. 完成 overlay、rebuild、校验和递归统计；
3. 构造一个完整的新 entry；
4. 在 snapshot lock 内一次性替换 `loaded_snapshots[snapshot_id]`。

这样并发请求要么看到完整的旧 generation，要么看到完整的新 generation。

加载完成后可以再次读取 manifest generation：

- generation 未变化：当前内存状态是最新已提交状态；
- generation 已变化：当前内存状态仍然一致但不是最新，可以立即重试或安排下一次刷新。

API 响应和任务状态应暴露 generation，方便调用方判断其读取的是哪个版本。

## 12. 变化检测与完整性状态

本存储格式只描述如何保存变化。变化检测后端仍需判断事件是否连续：

- Windows NTFS 可保存 USN Journal ID 和 USN cursor；
- Windows 其他场景可使用 `ReadDirectoryChangesW`，但进程停机或缓冲区溢出后可能需要全量扫描；
- Linux 可使用 inotify，但进程停机、`IN_Q_OVERFLOW`、watch 丢失或文件系统卸载后可能需要全量扫描。

manifest 后续可以增加：

```json
{
  "change_tracking": {
    "backend": "inotify",
    "state": "complete",
    "cursor": "310",
    "generation": "watcher-generation-id"
  }
}
```

如果无法证明 cursor 连续，不得把生成的 inc 宣称为完整更新。应当执行全量扫描或明确标记 snapshot 状态为不完整。

## 13. Compaction

inc 数量持续增长会增加加载开销，因为 loader 每次都需要读取 base 和 manifest 中的所有 inc。

建议在满足任一条件时进行 compaction：

- inc 数量达到配置阈值，例如 20；
- inc 压缩后总大小超过 base 的一定比例，例如 30%；
- inc 累计记录数超过 base 记录数的一定比例，例如 50%；
- 实际加载时间超过配置阈值。

compaction 流程：

1. 加载当前 generation 并 rebuild 可达树；
2. 把可达目录记录写入新的临时 base；
3. 完整关闭并校验新 base；
4. 原子更新 manifest，使其引用新 base，并把 increments 重置为空；
5. 等待可能的并发 reader 结束后，再清理旧 base 和旧 inc。

第一版可以暂不实现 compaction，但 manifest 和目录结构不能阻碍后续加入该能力。

## 14. 对现有模块的预期影响

### `fshub/scanning.py`

- 全量扫描改为创建 snapshot 目录、base 和 manifest。
- 增加 dirty directory 扫描和 inc 写入逻辑。
- snapshot 级设备及扫描元数据写入 manifest。

### `fshub/api/explorer.py`

- snapshot 列表改为枚举包含合法 manifest 的目录。
- `_read_snapshot_records()` 改为读取 manifest、base 和 inc，并执行 overlay/rebuild。
- `load_snapshot_file()` 发布 generation。
- 路径 index 和递归统计继续在 load 时生成。

### Groups

- group log 放在逻辑 snapshot 目录中，绑定稳定的 `snapshot_id`。
- 删除后残留的 group path 在使用时按最终 path index 忽略。
- group 清理可以在后续 compaction 时执行，但不影响 snapshot 正确性。

### Search、Hash 和 Backup

- 这些模块只能读取 rebuild 后的 `snapshot_data` 和 `path_index`，不能直接遍历 overlay map。
- 启动异步任务时应记录 snapshot generation，保证任务日志可说明其使用的数据版本。

## 15. 测试要求

至少覆盖以下情况：

1. 只有 base 的 snapshot 可以加载。
2. 一个或多个 inc 按 generation 正确覆盖相同路径。
3. 文件新增、修改和删除。
4. 目录新增和删除。
5. 删除目录后，旧子树记录不会进入最终 data、index 或搜索结果。
6. 新父目录记录引用缺失子目录记录时拒绝加载。
7. Windows `This PC` 根节点和驱动器路径可以 rebuild。
8. Linux 文件名中的反斜杠不会被当作分隔符。
9. manifest generation 不连续时拒绝加载。
10. manifest 未引用的临时或 orphan inc 被忽略。
11. 截断或损坏的 inc 不会发布为 loaded snapshot。
12. 加载期间提交新 generation 时，本次加载仍得到一致的旧 generation。
13. 内存刷新以完整 entry 原子替换，不暴露半构建状态。
14. compaction 前后的最终可达树、index 和递归统计完全一致。

## 16. 设计结论

本设计采用以下原则：

- 一个 snapshot 是一个目录和一个稳定的逻辑 ID；
- base 与 inc 都只保存完整目录记录；
- inc 通过 generation 排序，并由 manifest 明确提交；
- 同路径记录采用 last-write-wins；
- 文件和目录删除通过父目录的最新完整记录表达；
- 不使用字段 patch、delete、tombstone 或 rename 操作；
- loader 从 root 重建可达树，丢弃所有 orphan records；
- index 和递归统计始终基于 rebuild 后的最终树重新生成；
- 新 generation 和内存 snapshot 都采用原子发布；
- 事件连续性无法确认时必须回退到全量扫描或明确标记不完整。
