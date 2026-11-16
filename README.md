# MHXY iPhone Control

从 Windows 通过 ESP32-S3 的蓝牙键鼠操作 iPhone。ESP32 插在电脑上，iPhone 通过系统辅助触控接收输入，通过 AirPlay 把画面投到电脑。手机端无需安装 App。

```text
Windows   ── USB 串口 ──▶ ESP32-S3 ── 蓝牙 HID ──▶ iPhone
Windows   ◀── MHXY-PC / UxPlay ◀──── AirPlay ─────── iPhone
```

支持键盘、指针、点击、滚动、组合键、截图、OCR 和原速录屏。多个调用方通过本地服务轮流持有控制权。右键、媒体键、拖拽与游戏中的手势因 iOS、应用和指针设置而异。坐标配方只适用于经实测的布局。

## 1. 刷写固件

需要 Seeed XIAO ESP32-S3 Plus（16 MB Flash / 8 MB PSRAM）和 Arduino CLI。PowerShell 中运行以下命令，最后一条中的 `COM3` 换成板子的实际端口。

```powershell
arduino-cli core update-index --additional-urls https://espressif.github.io/arduino-esp32/package_esp32_index.json
arduino-cli core install esp32:esp32@3.3.7 --additional-urls https://espressif.github.io/arduino-esp32/package_esp32_index.json
arduino-cli lib install "NimBLE-Arduino@2.5.1"
arduino-cli board list
.\build-firmware.ps1 -UploadPort COM3
```

不传 `-UploadPort` 只编译，输出到 `build/firmware/`。若无法进入下载模式，按住板上的 BOOT 重新插线，再用 `arduino-cli board list` 确认端口。刷写后将板子留在电脑上。已有其他固件时先自行备份。

## 2. 安装 Windows 环境并配对

需要 Windows、Python 3.11 和 PATH 中的 FFmpeg，录屏依赖 FFmpeg。OCR 使用 Windows 已安装的识别语言。

```powershell
.\setup.ps1
.\phone.ps1 probe
.\phone.ps1 pair-hid
```

有多个 Espressif 串口时，在启动服务前设置 `$env:MHXY_SERIAL_PORT = 'COM3'`。首次配对时在 iPhone 的「设置 → 辅助功能 → 触控 → 辅助触控 → 设备 → 蓝牙设备」中选择 `MHXY iPhone HID`。在辅助触控的「指针设备」部分打开「显示屏幕键盘」，连接蓝牙后仍可以手动打字。已配对的板子会重新连回同一部手机。

## 3. 启动屏幕镜像

安装支持 `-vrtp` 的 [UxPlay](https://github.com/FDH2/UxPlay)，配好其 GStreamer 和 mDNS 依赖。Windows 图形版可在高级选项中配置同一个视频 RTP 输出管线。用两个 PowerShell 窗口分别启动 RTP 解码器和 UxPlay。电脑与 iPhone 应在能互相访问的局域网内。

```powershell
.\.venv\Scripts\python.exe -m mhxy_control.capture --rtp-port 5508
```

```powershell
uxplay -n MHXY-PC -a -s 332x720 -vrtp 'config-interval=1 ! udpsink host=127.0.0.1 port=5508'
```

在 iPhone 控制中心的「屏幕镜像」选择 `MHXY-PC`。若该名称不可见，检查 UxPlay 的启动输出和局域网发现。已有兼容取帧进程时，可在服务启动前用 `MHXY_FRAME_FILE` 指定现有帧文件。

## 4. 操作

```powershell
.\phone.ps1 begin --session demo
@'
{"commands":[{"cmd":"home","after_ms":100},{"cmd":"move 60 220","after_ms":200}]}
'@ | .\phone.ps1 run --session demo --stdin
.\phone.ps1 screenshot --session demo --out screen.png
.\phone.ps1 finish --session demo
```

首次移动后看镜像中的实际落点，再执行点击。 `home` 归零鼠标，`homescreen` 返回 iOS 主屏幕。`begin --record clip.mp4` 录制，`finish` 停止录制并释放控制权。`ocr --session demo --box 0 0 332 720` 读取指定区域。完整参数见 `phone.ps1 --help`。

安装 Codex Skill 时，把 [对应目录](skills/esp32-iphone-control) 放到自己的 skills 目录，并让调用方使用本仓库的实际路径。它也包含可选的中文备忘录和微信操作配方。配方依赖当前 UI 语言、屏幕布局与本机 OCR，需实地验收。

本地服务监听回环地址 `127.0.0.1:18766`，使用 Windows DPAPI 保存访问令牌。默认工作路径为 `%LOCALAPPDATA%\MHXY\Control\work`。本仓库发布源码，Arduino、NimBLE、UxPlay、FFmpeg 和 Python 依赖由使用方分别安装。
