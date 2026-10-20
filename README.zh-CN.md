# View

**面向 Linux（GNOME）和 Windows 的 macOS 预览风格查看器，同时提供 Chrome 扩展。** 在一个快速的 GTK4 + libadwaita 应用中查看、批注和签署 PDF、图片、3D 模型、表格和视频，还支持用笔记本触控板签名。

[![License: PolyForm Noncommercial](https://img.shields.io/badge/license-PolyForm%20Noncommercial%201.0.0-blue)](LICENSE)
[![Release](https://img.shields.io/github/v/release/Zdong104/View?include_prereleases)](https://github.com/Zdong104/View/releases)
[![Downloads](https://img.shields.io/github/downloads/Zdong104/View/total)](https://github.com/Zdong104/View/releases)
[![English](https://img.shields.io/badge/lang-English-blue)](README.md)

![在 View 中签署 PDF](docs/screenshots/pdf-sign.png)

<details>
<summary>更多截图</summary>

![为图片添加标注](docs/screenshots/image-markup.png)
![3D 模型查看器](docs/screenshots/model-3d.png)
![CSV 查看器](docs/screenshots/table.png)

</details>

## 功能特性

- **PDF**：处理超大文件依然流畅（已测试 1,000 页和 600 MB 的扫描版 PDF），支持缩略图、搜索、文本选择、表单填写、打印和获取信息。
- **页面编辑**：选中缩略图后按 Delete 删除，还可以调整顺序、旋转、插入、合并多个 PDF、将页面拖出。选中页面后按 Ctrl+S，可只把这些页面另存为新的 PDF。
- **导出与保护**：将页面导出为图片、设置密码加密、压缩文件大小。
- **触控板签名**：触控板变成 1:1 的绘图板，和 macOS 预览一样。签名以矢量形式保存。鼠标和手写笔同样可用。
- **标注** PDF 和图片：手绘、形状、箭头、文本框、批注，以及高亮 / 下划线 / 删除线。
- **图片**：缩放、平移、捏合手势、旋转、翻转、裁剪、调色、一键抠除背景（alpha）、调整尺寸、EXIF/GPS 信息。编辑后的 GIF / WebP / APNG 动图仍会保留动画。
- **批量工具**：批量转换所选图片，将多张图片合并为 PDF。
- **实况文本（OCR）**：选择并复制图片和扫描版 PDF 中的文字，导出可搜索的 PDF（RapidOCR，纯 CPU 运行，支持英文和中文）。
- **3D 模型**：支持 STL / OBJ / PLY，可用触控板旋转视角，显示尺寸 / 体积 / 表面积（已测试 1000 万三角面）。
- **表格与文本**：CSV / TSV 查看器和编辑器（支持 100 万行），以及带语法高亮的文本和代码查看器。
- **视频和音频**，在 Linux 上支持硬件（VA-API）解码。
- **GNOME 文件的快速预览（Quick Look）**：按空格键即可预览文件（取代 Sushi，需手动开启）。

## 安装

### Ubuntu / Debian（.deb）

下载 [`view_0.1.5_all.deb`](https://github.com/Zdong104/View/releases/download/v0.1.5/view_0.1.5_all.deb)（也可以从 [Releases 页面](https://github.com/Zdong104/View/releases)获取），然后执行：

```bash
sudo apt install ./view_0.1.5_all.deb
```

或者一条命令完成：

```bash
wget https://github.com/Zdong104/View/releases/download/v0.1.5/view_0.1.5_all.deb && sudo apt install ./view_0.1.5_all.deb
```

推荐使用 `apt`，因为它会一并安装依赖。如果你更习惯用 `dpkg`：

```bash
sudo dpkg -i view_0.1.5_all.deb && sudo apt -f install
```

### Snap（edge 通道）

```bash
sudo snap install view --edge && sudo snap connect view:raw-input
```

后半条命令用于让沙箱中的 snap 读取触控板数据以支持触控板签名；不执行的话，仍然可以用鼠标、手写笔或手指签名。如果已经安装了 snap，只需运行 `sudo snap connect view:raw-input`。和其他安装方式一样，触控板签名还需要把你的用户加入 `input` 组（`sudo usermod -aG input $USER`，然后注销并重新登录）。

在终端中请使用 `snap run view` 启动（直接输入 `view` 通常会进入 Vim 的只读模式）。由于 snap 运行在沙箱中，GNOME 文件的集成功能（空格键快速预览、“合并图片为 PDF”）仅在 .deb / 源码安装中可用。

### Windows（**实验性**）

下载 [`View-Setup-0.1.5.exe`](https://github.com/Zdong104/View/releases/download/v0.1.5/View-Setup-0.1.5.exe)（安装程序）或 [`View-0.1.5-windows-portable.zip`](https://github.com/Zdong104/View/releases/download/v0.1.5/View-0.1.5-windows-portable.zip)（免安装）。目前已在真实的 Windows 11 笔记本上测试过，但仍非常欢迎你的反馈。触控板签名的用法和 Linux 上一样：按 **Space**（或 **Start**），然后用一根手指签名，无需点击。它需要精确式触控板（Precision Touchpad，2016 年以来的大多数笔记本都有）；否则请用鼠标或手写笔书写。

**0.1.5 新增：**

- **更清晰的图片**：适应窗口的大尺寸照片会按屏幕分辨率以完整画质缩小，并且图片严格对齐屏幕像素，不再发虚（在高 DPI 屏幕上 100 % 显示时也是如此）。
- **打开文件夹**即可查看其中的图片。
- **安装后的首次启动**可能需要 20 秒甚至更久，因为 Windows 会逐个检查新文件（该版本尚未进行代码签名，见下文）。之后的启动大约只需一秒。

**已知问题：** Windows 版本尚未进行代码签名，因此在开启了**智能应用控制（Smart App Control）**的电脑上（或带有应用控制策略的工作电脑上），启动时会中断并提示 *"DLL load failed … An Application Control policy has blocked this file"*（[#3](https://github.com/Zdong104/View/issues/3)）。签名版本可以解决这个问题；在此之前，它只能在关闭智能应用控制的电脑上运行（Windows 安全中心 → 应用和浏览器控制 → 智能应用控制设置）。如果没有开启智能应用控制，Windows 可能会改为弹出 SmartScreen 警告：点击 **更多信息 → 仍要运行** 即可。

文本识别（OCR）从 0.1.1 版本起已包含在内。它运行在一个名为 `ocr\view-ocr.exe` 的小型辅助程序中，由 View 在后台启动。

### Chrome 扩展

View 也以扩展的形式支持 Chrome。

1. 下载 [`view-chrome-0.1.5.zip`](https://github.com/Zdong104/View/releases/download/v0.1.5/view-chrome-0.1.5.zip) 并解压。
2. 打开 `chrome://extensions`，开启 **开发者模式**，点击 **加载已解压的扩展程序**，然后选择解压出的文件夹。
3. 如需打开本地文件，请点击 View 的 **详细信息**，并开启 **允许访问文件网址**。

### 从源码安装

```bash
git clone https://github.com/Zdong104/View.git
cd View
make run
```

`make install` 会把当前检出的目录添加到应用网格中，名称为 **View (Dev)**，与 snap 或 .deb 版本并存；`make uninstall` 可将其移除（`make purge` 还会删除它的 Python 虚拟环境）。

### 更新

View 每天会向 GitHub 检查一次新版本（可在 **偏好设置 → 更新** 中关闭），每次打开 **关于 View** 或主菜单中的 **检查更新…** 时也会检查。发现新版本时，它会显示更新内容，并可以为你自动安装：

| 安装方式 | 更新方式 |
| --- | --- |
| Windows 安装程序 | **安装并重启** 会下载新的安装程序，原地更新 View 并重新启动 |
| .deb | **安装…** 会下载新的 .deb 并通过 `apt` 安装（会要求输入密码） |
| Snap | Snap Store 会自动更新 View；也可以运行 `sudo snap refresh view` 立即更新 |
| Windows 便携版 zip、源码 | **下载** 会在浏览器中打开新版本的下载页面 |

下载的文件在安装前会与该版本发布的 `SHA256SUMS` 进行校验。在终端中运行 `view-app --check-updates` 可以查看是否有新版本。所有版本（包括旧版本）都保留在 [Releases 页面](https://github.com/Zdong104/View/releases)；安装旧版 .deb 可使用 `sudo apt install --allow-downgrades ./view_<version>_all.deb`，旧版 Windows 安装程序则可直接覆盖运行在当前版本之上。

## 触控板签名

在 Windows 上，配备精确式触控板即可开箱即用：打开 **新建签名...** 并按 **Start**。签名时鼠标指针会暂停移动；按任意键结束。在 Linux 上：

1. 将自己加入 `input` 组（直接读取触控板需要此权限）：`sudo usermod -aG input $USER`，然后注销并重新登录。
2. 从应用菜单中打开 **新建签名...**（或运行 `view-app --new-signature`）。
3. 用一根手指书写。你触摸触控板的位置，就是墨迹出现的位置。按任意键结束，然后把签名放到页面上。

## 兼容性

| 系统 | 状态 |
| --- | --- |
| Ubuntu 26.04 | ✅ 每日测试 |
| Ubuntu 25.10、Debian 13 | 在容器中可安装并通过自检 |
| Ubuntu 24.04 | 已在 snap 安装方式下测试 |
| Fedora、Arch 等其他发行版 | ❓ 未测试 |
| Windows | ✅ 已测试，欢迎反馈 |
| Chrome 扩展 | ✅ 已测试 |
| macOS | 用 Mac 自带的预览吧 🙂 |

## 🙏 期待你的反馈

View 仅在 **联想 ThinkPad P1 Gen 8 上的 Ubuntu 26.04** 中开发和测试。其他环境都只是推测，你的反馈是最有价值的帮助。

- 在你的系统上试过了？请提交一份[**平台报告**](https://github.com/Zdong104/View/issues/new?template=platform_report.yml)，即使一切正常也请告诉我们。
- 发现问题？请提交[**Bug 报告**](https://github.com/Zdong104/View/issues/new?template=bug_report.yml)。
- 缺少某个功能？请提交[**功能建议**](https://github.com/Zdong104/View/issues/new?template=feature_request.yml)。
- 提问和想法：请使用 [Discussions](https://github.com/Zdong104/View/discussions)。

## 参与贡献

欢迎提交 Pull Request，无论是修正错别字还是新增查看器都可以。请阅读 [CONTRIBUTING.md](CONTRIBUTING.md)，了解如何从源码运行、项目结构以及 PR 检查清单。所有参与者都应遵守[行为准则](CODE_OF_CONDUCT.md)。

## 支持本项目

View 是免费的。如果它帮你节省了时间，欢迎支持项目的开发：

[![Buy Me a Coffee](https://img.shields.io/badge/Buy%20Me%20a%20Coffee-support-FFDD00?logo=buymeacoffee&logoColor=black)](https://buymeacoffee.com/zdong)

GitHub Sponsors 即将上线。

## 许可证

View 采用 [PolyForm Noncommercial License 1.0.0](LICENSE) 授权：个人、教育、研究及其他非商业用途免费。商业使用或分发需获得许可；请提交 GitHub issue 或联系维护者咨询。请注意，这意味着 View 是*源码可见（source-available）*项目，而非经 OSI 认证的开源软件。
