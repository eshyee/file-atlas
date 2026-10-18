# 归序 · File Atlas

**把散落的文件整理清楚，每一步都先预览。**

归序是一款在本机运行的文件整理工具，将媒体与文档分类、精确内容去重、跨目录归并放进同一个中文界面。分类和归并默认复制，去重副本进入可恢复隔离区。

![归序 File Atlas 界面](docs/screenshot.png)

[下载 Windows 版 FileAtlas-Windows.zip](https://github.com/eshyee/file-atlas/releases/latest/download/FileAtlas-Windows.zip) · [全部 Releases](https://github.com/eshyee/file-atlas/releases) · [文件与隐私安全](SECURITY.md) · [原脚本整合说明](docs/legacy-integration.md)

## 三种整理方式

| 功能 | 适合做什么 | 默认行为 |
| --- | --- | --- |
| 智能分类 | 整理照片、视频、音频和常见文档 | 按类型、日期、设备或文档格式生成目录，预览后复制 |
| 精确去重 | 查找一个或多个目录中的相同内容 | 完整内容校验后保留一份，将选中的冗余副本移入隔离区 |
| 文件归并 | 把多个来源汇入一个目标目录 | 保留来源及相对路径，同名另起文件名 |

媒体采用原 v6 的时间优先目录：`picture_classify/2020/2020-01/SONY_ILCE-6700/照片.jpg`。日期比较媒体元数据、文件名日期和文件创建时间，选取最早的有效值。文档采用 `document_classify/PDF/2024-09/报告.pdf` 等目录。JSON、YAML 等结构化配置文件默认不参与分类。

## Windows 下载与使用

1. 从 [Releases](https://github.com/eshyee/file-atlas/releases) 下载 `FileAtlas-Windows.zip` 并解压。
2. 双击 `FileAtlas.exe`，程序会在浏览器打开本机界面。
3. 选择整理方式、来源和目标目录，点击扫描。
4. 查看预览中的文件、目标位置、重复项和跳过原因，再确认执行。

程序运行期间请保留启动窗口；关闭窗口或按 `Ctrl+C` 结束服务。浏览器地址只供本机使用，每次启动的会话凭据不同。

分类和归并默认保留原文件。需要移动时，应在界面中主动选择移动并确认。先用小目录熟悉预览与恢复流程，再处理较大的文件集合。

## 文件保护与恢复

- **扫描阶段不移动或删除文件。** 执行使用确认后的计划，预览与实际状态不一致时跳过并报告。
- **复制后校验。** 通过临时文件写入、落盘和内容校验完成复制；同名文件保留，避免覆盖。
- **去重副本进入隔离区。** 隔离区位于对应来源目录下的 `.fileatlas-quarantine`，任务历史提供恢复入口。界面不提供永久清理重复文件或清空隔离区的功能。
- **隔离不会释放磁盘空间。** 重复体积表示可处理的副本大小；如需腾出空间，应确认不再需要恢复后，在应用外自行处置隔离文件。
- **操作有日志。** 执行记录任务日志；恢复时重新校验内容，原位置已有新文件时保留现状并提示冲突。恢复前请保留任务日志和隔离区。
- **保护文件关联关系。** 默认跳过工程目录、系统与应用状态目录、符号链接、Windows Junction 与其他 reparse point；不自动解压或删除 ZIP，不清理零字节文件或空目录。
- **保留特殊文件信息。** 含 NTFS 附加数据流（包括下载来源标记）或 EFS 加密属性的文件会跳过，并显示原因，避免复制时丢失额外信息或加密属性。

任务日志默认保存在 Windows 的 `%LOCALAPPDATA%/FileAtlas`，其他平台使用本机用户状态目录。日志包含本地路径，请勿将其作为公开问题附件。具体边界见 [SECURITY.md](SECURITY.md)。

撤销复制时，程序先确认原文件仍存在、身份和内容未变，再移除本任务生成的目标副本。恢复移动或去重时，会先把已验证的副本还原到原位置，再移除任务生成的目标或隔离副本；遇到冲突则保留文件并报告。

## 从源码运行

需要 **Python 3.11 或更新版本**，以及用于构建界面的 **Node.js 24**。

```powershell
git clone https://github.com/eshyee/file-atlas.git
cd file-atlas
python -m pip install -e ".[dev]"
cd frontend
npm ci
npm run build
cd ..
python run.py
```

依赖安装与界面构建完成后，Windows 也可双击根目录的 `启动归序.cmd`。如需指定本地任务日志目录：

```powershell
python run.py --data-dir "D:/FileAtlasState"
```

可选独立窗口模式：

```powershell
python -m pip install -e ".[desktop]"
python run.py --desktop
```

### 可选媒体支持

Pillow 已包含在基础依赖中。HEIC/HEIF 可安装额外支持：

```powershell
python -m pip install -e ".[heif]"
```

安装 [ExifTool](https://exiftool.org/) 可改善 RAW 和相机元数据识别；安装 [FFmpeg/FFprobe](https://ffmpeg.org/ffprobe.html) 可补充视频和音频信息。将 `exiftool.exe`、`ffprobe.exe` 放入应用目录或加入系统 `PATH`。请使用 `exiftool.exe` 文件名，避免带 `(-k)` 的版本等待按键。未安装这些工具时，程序使用可用的回退信息，设备识别可能更有限。

第三方工具不会自动下载，也不随本仓库分发。

## 开发与构建

```powershell
python -m pytest -q
./scripts/build_windows.ps1
```

构建脚本会构建前端并生成 `dist/FileAtlas.exe`。CI 配置覆盖 Windows、Linux 和 Python 3.11、3.14，并单独构建 Windows 程序。

源码结构：

```text
fileatlas/             本地服务、分类规则和安全操作引擎
frontend/              中文图形界面
scripts/               Windows 构建脚本
tests/                 分类、文件安全与本地服务测试
docs/                  界面截图与原脚本整合说明
```

原有 `all_classify_cleanup` v1–v6、跨盘去重与 `merge_to_drive` 系列已整合。旧脚本、历史扫描数据库、私人处理日志和第三方二进制保留在原用户的本地目录，不进入公开仓库。整合后的代码不再执行内嵌脚本字符串；原版自动删空文件、自动解压删包和永久去重删除不进入默认流程。

## 许可

本项目使用 [MIT License](LICENSE)。可选第三方工具遵循各自许可证。
